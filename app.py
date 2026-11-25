"""Local podcast RSS reader backend with asynchronous CPU transcription."""
from __future__ import annotations

import asyncio, hashlib, json, logging, os, re, sqlite3, threading, urllib.error, urllib.parse, urllib.request, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from translation_runner import DEFAULT_MODEL as DEFAULT_AGY_TRANSLATION_MODEL, MAX_BATCH_INPUT_CHARS, MAX_BATCH_SENTENCES, TranslationRunnerError, translate_sentence_with_expressions, translate_sentences_with_expressions

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PODCAST_DATA_DIR", BASE_DIR / "data")).resolve()
AUDIO_DIR = DATA_DIR / "audio"
DATA_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "podcasts.sqlite3"
app = FastAPI(title="Podcast Reader", version="0.1.0")
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcription")
job_lock = threading.Lock()
jobs: dict[str, dict[str, Any]] = {}
_active_episode_jobs: set[int] = set()
AUTO_REFRESH_INTERVAL_SECONDS = max(30, int(os.environ.get("AUTO_REFRESH_INTERVAL_SECONDS", "900")))
_refresh_lock = threading.Lock()
_scheduler_stop: asyncio.Event | None = None
_scheduler_task: asyncio.Task | None = None
USER_AGENT = "PodcastReader/0.1 (personal RSS reader)"
logger = logging.getLogger(__name__)
AGY_TRANSLATION_MODEL = os.environ.get("AGY_TRANSLATION_MODEL", DEFAULT_AGY_TRANSLATION_MODEL).strip() or DEFAULT_AGY_TRANSLATION_MODEL
_translation_lock_guard = threading.Lock()
_translation_locks: dict[tuple[int, str], tuple[threading.Lock, int]] = {}
_agy_request_lock = threading.Lock()
_translation_prefetch_runtime_lock = threading.Lock()
_translation_prefetch_control_lock = threading.Lock()
_translation_prefetch_executor: ThreadPoolExecutor | None = None
_translation_prefetch_future: Any = None
_translation_prefetch_stop: threading.Event | None = None
_translation_prefetch_wakeup: threading.Event | None = None
TRANSLATION_PREFETCH_INTERVAL_SECONDS = max(1, int(os.environ.get("TRANSLATION_PREFETCH_INTERVAL_SECONDS", "5")))

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS feeds(id INTEGER PRIMARY KEY,url TEXT NOT NULL UNIQUE,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',image_url TEXT NOT NULL DEFAULT '',added_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS episodes(id INTEGER PRIMARY KEY,feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,guid TEXT NOT NULL,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',audio_url TEXT NOT NULL,published_at TEXT NOT NULL DEFAULT '',duration TEXT NOT NULL DEFAULT '',transcript_json TEXT,transcript_status TEXT NOT NULL DEFAULT 'none',transcript_error TEXT NOT NULL DEFAULT '',local_audio TEXT,UNIQUE(feed_id,guid));
        CREATE TABLE IF NOT EXISTS sentence_translations(episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,content_key TEXT NOT NULL,target_language TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,translation TEXT NOT NULL,created_at TEXT NOT NULL,expressions_json TEXT,PRIMARY KEY(episode_id,content_key,target_language,provider,model));
        CREATE TABLE IF NOT EXISTS translation_prefetch_jobs(episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,content_key TEXT NOT NULL,model TEXT NOT NULL,segment_index INTEGER NOT NULL,source_fingerprint TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('pending','running','ready','error','stale')),error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,PRIMARY KEY(episode_id,content_key,model));
        CREATE TABLE IF NOT EXISTS translation_prefetch_control(id INTEGER PRIMARY KEY CHECK(id=1),paused INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL);
        INSERT OR IGNORE INTO translation_prefetch_control(id,paused,error,updated_at) VALUES(1,0,'','');
        """)
        translation_columns = {r[1] for r in c.execute("PRAGMA table_info(sentence_translations)")}
        if "expressions_json" not in translation_columns:
            # NULL means a legacy translation has not been enriched yet; "[]" is a completed analysis with no notable phrases.
            c.execute("ALTER TABLE sentence_translations ADD COLUMN expressions_json TEXT")
        # Additive migrations preserve existing local feeds, episodes, and transcripts.
        feed_columns = {r[1] for r in c.execute("PRAGMA table_info(feeds)")}
        if "auto_since" not in feed_columns:
            c.execute("ALTER TABLE feeds ADD COLUMN auto_since TEXT")
        if "last_poll_at" not in feed_columns:
            c.execute("ALTER TABLE feeds ADD COLUMN last_poll_at TEXT")
        if "last_poll_error" not in feed_columns:
            c.execute("ALTER TABLE feeds ADD COLUMN last_poll_error TEXT NOT NULL DEFAULT ''")
        episode_columns = {r[1] for r in c.execute("PRAGMA table_info(episodes)")}
        if "transcript_origin" not in episode_columns:
            c.execute("ALTER TABLE episodes ADD COLUMN transcript_origin TEXT NOT NULL DEFAULT 'manual'")
        if "auto_eligible" not in episode_columns:
            c.execute("ALTER TABLE episodes ADD COLUMN auto_eligible INTEGER NOT NULL DEFAULT 0")
        if "publication_time" not in episode_columns:
            c.execute("ALTER TABLE episodes ADD COLUMN publication_time TEXT NOT NULL DEFAULT ''")
        # Existing feeds are activated at upgrade time: their current archive is baseline.
        now = datetime.now(timezone.utc).isoformat()
        c.execute("UPDATE feeds SET auto_since=? WHERE auto_since IS NULL OR auto_since=''", (now,))
        # Non-auto in-flight jobs were never durable; retain the previous retry behavior.
        c.execute("UPDATE episodes SET transcript_status='error',transcript_error='Transcription was interrupted by a server restart. Retry it.' WHERE transcript_status IN ('queued','downloading','processing') AND transcript_origin!='auto'")

def _new_job(eid: int) -> str | None:
    jid = str(uuid.uuid4())
    with job_lock:
        if eid in _active_episode_jobs:
            return None
        _active_episode_jobs.add(eid)
        jobs[jid] = {"id": jid, "episode_id": eid, "status": "queued", "progress": 0}
    return jid

def _submit_job(eid: int, jid: str) -> bool:
    """Submit work or persist a terminal error if the executor is unavailable."""
    try:
        future = executor.submit(transcribe_worker, eid, jid)
        future.add_done_callback(lambda f: _job_cancelled(eid, jid, f))
        return True
    except RuntimeError as e:
        message = f"Transcription could not be queued: {e}"[:500]
        set_state(eid, "error", error=message)
        with job_lock:
            _active_episode_jobs.discard(eid)
            if jid in jobs:
                jobs[jid].update(status="error", progress=0, error=message)
        return False

def _job_cancelled(eid: int, jid: str, future: Any) -> None:
    if not future.cancelled():
        return
    with db() as c:
        row = c.execute("SELECT transcript_origin FROM episodes WHERE id=?", (eid,)).fetchone()
    # Automatic work stays durably queued for the next process start. Manual work
    # is surfaced as an error because it is not part of automatic recovery.
    if row is not None and row["transcript_origin"] != "auto":
        set_state(eid, "error", error="Transcription was interrupted by server shutdown. Retry it.")
        with job_lock:
            if jid in jobs:
                jobs[jid].update(status="error", progress=0, error="Transcription was interrupted by server shutdown. Retry it.")
    with job_lock:
        _active_episode_jobs.discard(eid)

def enqueue_episode(eid: int, origin: str = "auto", allow_ready: bool = False) -> str | None:
    """Atomically claim a transcript and enqueue it on the one-worker CPU queue."""
    jid: str | None = None
    with db() as c:
        row = c.execute("SELECT transcript_status FROM episodes WHERE id=?", (eid,)).fetchone()
        if row is None or row["transcript_status"] in {"queued", "downloading", "processing"} or (row["transcript_status"] == "ready" and not allow_ready):
            return None
        condition = "transcript_status NOT IN ('queued','downloading','processing','ready')" if not allow_ready else "transcript_status NOT IN ('queued','downloading','processing')"
        cur = c.execute(f"UPDATE episodes SET transcript_status='queued',transcript_error='',transcript_origin=? WHERE id=? AND {condition}", (origin, eid))
        if not cur.rowcount:
            return None
        jid = _new_job(eid)
    if jid is None:
        return None
    if not _submit_job(eid, jid):
        raise HTTPException(503, "Transcription queue is unavailable")
    return jid

def recover_auto_jobs() -> int:
    """Resume durable automatic jobs once at server startup; failures do not loop."""
    with db() as c:
        # Queued jobs survive a crash. A worker interrupted while downloading/model
        # execution was in progress is also safe to resume once.
        c.execute("UPDATE episodes SET transcript_status='queued',transcript_error='' WHERE transcript_origin='auto' AND transcript_status IN ('downloading','processing')")
        ids = [r[0] for r in c.execute("SELECT id FROM episodes WHERE transcript_origin='auto' AND transcript_status='queued' ORDER BY id")]
    for eid in ids:
        jid = _new_job(eid)
        if jid is not None:
            _submit_job(eid, jid)
    return len(ids)

async def _auto_refresh_loop() -> None:
    assert _scheduler_stop is not None
    while not _scheduler_stop.is_set():
        try:
            await poll_feeds_once(_scheduler_stop)
        except Exception:
            # A failed poll must not stop future polling.
            pass
        try:
            await asyncio.wait_for(_scheduler_stop.wait(), timeout=AUTO_REFRESH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass

@app.on_event("startup")
async def startup() -> None:
    global _scheduler_stop, _scheduler_task
    init_db()
    recover_auto_jobs()
    start_translation_prefetch_worker()
    _scheduler_stop = asyncio.Event()
    _scheduler_task = asyncio.create_task(_auto_refresh_loop(), name="rss-auto-refresh")

@app.on_event("shutdown")
async def shutdown() -> None:
    global _scheduler_stop, _scheduler_task
    if _scheduler_stop is not None:
        _scheduler_stop.set()
    if _scheduler_task is not None:
        await _scheduler_task
    stop_translation_prefetch_worker()
    # Running work may finish in its worker thread; queued work is canceled. Auto
    # jobs remain persisted as queued and are recovered on the next server start.
    executor.shutdown(wait=False, cancel_futures=True)
    _scheduler_stop = None
    _scheduler_task = None

def val(el: ET.Element | None, default: str = "") -> str:
    if el is None: return default
    return " ".join("".join(el.itertext()).split()).strip() or default

def child(el: ET.Element, name: str) -> ET.Element | None:
    direct = el.find(name)
    return direct if direct is not None else next((x for x in el if x.tag.rsplit("}", 1)[-1].lower() == name.lower()), None)

def fetch(url: str, limit: int | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            if limit is None: return response.read()
            data = response.read(limit + 1)
            if len(data) > limit: raise HTTPException(413, "RSS feed exceeds 10 MB")
            return data
    except HTTPException: raise
    except (urllib.error.URLError, TimeoutError, OSError) as e: raise HTTPException(502, f"Could not fetch URL: {e}") from e

def parse_feed(url: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname: raise HTTPException(422, "RSS URL must use http or https")
    try: root = ET.fromstring(fetch(url, 10 * 1024 * 1024))
    except ET.ParseError as e: raise HTTPException(422, "URL did not return valid RSS or Atom XML") from e
    channel = root.find("channel")
    atom = root if root.tag.rsplit("}", 1)[-1].lower() == "feed" else None
    meta = channel or atom or root
    title = val(child(meta, "title"), "Untitled podcast")
    desc = val(child(meta, "description"), val(child(meta, "subtitle")))
    image_el = child(meta, "image")
    image = (image_el.attrib.get("href", "") or val(child(image_el, "url"))) if image_el is not None else ""
    if not image: image = meta.attrib.get("image", "")
    items = list(channel.findall("item")) if channel is not None else list(atom.findall("{*}entry")) if atom is not None else []
    episodes = []
    for item in items:
        enclosure = child(item, "enclosure")
        audio = enclosure.attrib.get("url", "") if enclosure is not None else ""
        if not audio and atom is not None:
            for link in item.findall("{*}link"):
                if link.attrib.get("rel", "alternate") == "enclosure" or link.attrib.get("type", "").startswith("audio/"):
                    audio = link.attrib.get("href", "")
                    if audio: break
        if not audio: continue
        audio_parts = urllib.parse.urlsplit(urllib.parse.urljoin(url, audio))
        if audio_parts.scheme not in ("http", "https") or not audio_parts.hostname:
            raise HTTPException(422, "RSS contains an audio enclosure that is not an http(s) URL")
        guid = val(child(item, "guid"), val(child(item, "id"), audio))
        episodes.append({"guid": guid, "title": val(child(item, "title"), "Untitled episode"),
          "description": val(child(item, "description"), val(child(item, "summary"))),
          "audio_url": urllib.parse.urljoin(url, audio), "published_at": val(child(item, "pubDate"), val(child(item, "published"), val(child(item, "updated")))),
          "publication_time": val(child(item, "pubDate"), val(child(item, "published"))),
          "duration": val(child(item, "duration"))})
    return {"title": title, "description": desc, "image_url": image}, episodes

def feed_json(row: sqlite3.Row) -> dict[str, Any]: return dict(row)
def episode_json(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row); result.pop("transcript_json", None)
    result["has_transcript"] = bool(row["transcript_json"])
    result["audio_path"] = f"/api/episodes/{row['id']}/audio"
    return result

class FeedInput(BaseModel):
    url: HttpUrl

@app.get("/api/health")
def health() -> dict[str, str]: return {"status": "ok"}

@app.get("/api/feeds")
def list_feeds() -> list[dict[str, Any]]:
    with db() as c: return [feed_json(r) for r in c.execute("SELECT * FROM feeds ORDER BY id DESC")]

@app.post("/api/feeds")
def add_feed(payload: FeedInput) -> dict[str, Any]:
    url = str(payload.url); meta, eps = parse_feed(url)
    activated = datetime.now(timezone.utc).isoformat()
    with db() as c:
        try:
            cur = c.execute("INSERT INTO feeds(url,title,description,image_url,added_at,auto_since) VALUES(?,?,?,?,?,?)", (url, meta["title"], meta["description"], meta["image_url"], activated, activated))
        except sqlite3.IntegrityError: raise HTTPException(409, "This RSS feed is already registered")
        fid = cur.lastrowid
        # RSS feeds normally list newest episodes first; insertion is reversed because
        # the episode endpoint returns rows newest-id-first.
        c.executemany("INSERT OR IGNORE INTO episodes(feed_id,guid,title,description,audio_url,published_at,duration,publication_time) VALUES(?,?,?,?,?,?,?,?)", [(fid,e["guid"],e["title"],e["description"],e["audio_url"],e["published_at"],e["duration"],e["publication_time"]) for e in reversed(eps)])
        row = c.execute("SELECT * FROM feeds WHERE id=?", (fid,)).fetchone()
    return {**feed_json(row), "episode_count": len(eps)}

@app.delete("/api/feeds/{feed_id}")
def delete_feed(feed_id: int) -> dict[str, bool]:
    with db() as c:
        cur = c.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
        if not cur.rowcount: raise HTTPException(404, "Feed not found")
    return {"deleted": True}

@app.post("/api/feeds/{feed_id}/refresh")
def refresh_feed(feed_id: int) -> dict[str, int]:
    return {"episode_count": refresh_feed_internal(feed_id)}

def _published_time(value: str) -> datetime | None:
    """Parse RSS/Atom publication timestamps; unknown values are ineligible."""
    if not value or not value.strip(): return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try: parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError): return None
    if parsed is None: return None
    # RFC 5322 uses -0000 to mean UTC with an unknown originating timezone.
    # email.utils returns a naive datetime for this specific offset. Attach UTC
    # only after successful parsing and only for that explicit suffix; genuinely
    # timezone-less dates remain ineligible.
    if parsed.tzinfo is None:
        if re.search(r"\s-0000$", value) is None:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def refresh_feed_internal(feed_id: int, auto_queue: bool = True) -> int:
    """Shared manual/scheduled RSS refresh. New eligible rows and auto queue state commit together."""
    queued_ids: list[int] = []
    with _refresh_lock:
        with db() as c: feed = c.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        if feed is None: raise HTTPException(404, "Feed not found")
        meta, eps = parse_feed(feed["url"])
        activation = _published_time(feed["auto_since"] or "")
        now = datetime.now(timezone.utc)
        with db() as c:
            c.execute("UPDATE feeds SET title=?,description=?,image_url=? WHERE id=?", (meta["title"],meta["description"],meta["image_url"],feed_id))
            # Insert in feed order expected by the API (newest rows get largest ids).
            for e in reversed(eps):
                cur = c.execute("INSERT OR IGNORE INTO episodes(feed_id,guid,title,description,audio_url,published_at,duration,publication_time) VALUES(?,?,?,?,?,?,?,?)", (feed_id,e["guid"],e["title"],e["description"],e["audio_url"],e["published_at"],e["duration"],e["publication_time"]))
                # Only an episode discovered after feed activation is known to
                # be new. Older rows with auto_eligible=0 may be baseline items,
                # so do not infer provenance from their publication dates.
                if cur.rowcount and auto_queue and activation is not None:
                    published = _published_time(e["publication_time"])
                    if published is not None and published >= activation:
                        c.execute("UPDATE episodes SET auto_eligible=1 WHERE id=? AND transcript_status='none'", (cur.lastrowid,))
            if auto_queue and activation is not None:
                candidates = c.execute("SELECT id,publication_time FROM episodes WHERE feed_id=? AND auto_eligible=1 AND transcript_status='none'", (feed_id,)).fetchall()
                for candidate in candidates:
                    published = _published_time(candidate["publication_time"])
                    if published is not None and activation <= published <= now:
                        eid = candidate["id"]
                        c.execute("UPDATE episodes SET transcript_status='queued',transcript_error='',transcript_origin='auto' WHERE id=? AND transcript_status='none'", (eid,))
                        if c.execute("SELECT changes()").fetchone()[0]: queued_ids.append(eid)
            count = c.execute("SELECT COUNT(*) FROM episodes WHERE feed_id=?", (feed_id,)).fetchone()[0]
    # Durable queued statuses are already committed before work reaches the executor.
    for eid in queued_ids:
        jid = _new_job(eid)
        if jid is not None:
            _submit_job(eid, jid)
    return count

async def poll_feeds_once(stop_event: asyncio.Event | None = None) -> None:
    """Poll feeds serially, isolating per-feed errors so one bad URL cannot stall others."""
    with db() as c: feed_ids = [r[0] for r in c.execute("SELECT id FROM feeds ORDER BY id")]
    for feed_id in feed_ids:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            await asyncio.to_thread(refresh_feed_internal, feed_id, True)
            with db() as c:
                c.execute("UPDATE feeds SET last_poll_at=?,last_poll_error='' WHERE id=?", (datetime.now(timezone.utc).isoformat(), feed_id))
        except Exception as e:
            logger.warning("RSS auto-refresh failed for feed_id=%s (%s)", feed_id, type(e).__name__)
            # Keep a generic, non-secret user-visible status without persisting URLs
            # or the exception text, which may contain request details.
            with db() as c:
                c.execute("UPDATE feeds SET last_poll_at=?,last_poll_error=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), f"RSS refresh failed ({type(e).__name__})", feed_id))
            continue

@app.get("/api/feeds/{feed_id}/episodes")
def list_episodes(feed_id: int) -> list[dict[str, Any]]:
    with db() as c:
        if c.execute("SELECT 1 FROM feeds WHERE id=?", (feed_id,)).fetchone() is None: raise HTTPException(404, "Feed not found")
        rows = c.execute("SELECT * FROM episodes WHERE feed_id=? ORDER BY id DESC", (feed_id,)).fetchall()
    return [episode_json(r) for r in rows]

def get_episode(eid: int) -> sqlite3.Row:
    with db() as c: row = c.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
    if row is None: raise HTTPException(404, "Episode not found")
    return row

@app.get("/api/episodes/{episode_id}")
def episode_detail(episode_id: int) -> dict[str, Any]: return episode_json(get_episode(episode_id))

@app.get("/api/episodes/{episode_id}/audio")
def episode_audio(episode_id: int) -> RedirectResponse:
    row = get_episode(episode_id)
    # Local files are served as files; remote enclosure URLs are redirected for native browser playback.
    if row["local_audio"] and Path(row["local_audio"]).is_file(): return RedirectResponse(f"/api/episodes/{episode_id}/audio-file")
    parts = urllib.parse.urlsplit(row["audio_url"])
    if parts.scheme not in ("http", "https") or not parts.hostname: raise HTTPException(422,"Unsafe audio URL")
    return RedirectResponse(row["audio_url"])

@app.get("/api/episodes/{episode_id}/audio-file")
def episode_audio_file(episode_id: int) -> FileResponse:
    row = get_episode(episode_id)
    if not row["local_audio"] or not Path(row["local_audio"]).is_file(): raise HTTPException(404, "Audio not cached")
    return FileResponse(row["local_audio"])

def set_state(eid: int, status: str, transcript: dict[str, Any] | None = None, error: str = "", local_audio: str | None = None) -> None:
    with db() as c:
        c.execute("UPDATE episodes SET transcript_status=?,transcript_json=COALESCE(?,transcript_json),transcript_error=?,local_audio=COALESCE(?,local_audio) WHERE id=?", (status,json.dumps(transcript,ensure_ascii=False) if transcript is not None else None,error,local_audio,eid))
    if status == "ready":
        enqueue_episode_translation_prefetch(eid)

def line_text(words: list[dict[str, Any]]) -> str:
    # Whisper's word timestamps are tokenized without reliable leading spaces.
    punctuation = set(",.!?:;%)]}»”’")
    result = ""
    for i, word in enumerate(words):
        token = word["word"]
        if i and token[:1] not in punctuation: result += " "
        result += token
    return result.strip()

def is_period_end(word: str) -> bool:
    # Keep sentence boundaries tied to periods. A trailing quote or bracket belongs
    # to the sentence; question/exclamation marks alone do not end a line here.
    terminal = re.sub(r"[\"')\]»”’]+$", "", word)
    if not terminal.endswith("."):
        return False
    bare = terminal[:-1]
    # Common English abbreviations and initials usually continue the sentence.
    # Decimal tokens such as 3.14 do not end with a period and remain intact.
    if bare.lower() in {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e"}:
        return False
    if re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]?", terminal):
        return False
    if re.fullmatch(r"[A-Z]", bare):
        return False
    return True

def sentence_segments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group word records into period-terminated sentences, preserving each record."""
    result: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for word in words:
        pending.append(word)
        if is_period_end(str(word.get("word", ""))):
            result.append({"start": pending[0]["start"], "end": pending[-1]["end"],
                           "text": line_text(pending), "words": pending})
            pending = []
    # Keep a final sentence even when the source has no terminal period.
    if pending:
        result.append({"start": pending[0]["start"], "end": pending[-1]["end"],
                       "text": line_text(pending), "words": pending})
    return result

def normalize_transcript(transcript: dict[str, Any]) -> dict[str, Any]:
    """Rebuild sentence lines from timestamped words, including across old chunks."""
    old_segments = transcript.get("segments")
    if not isinstance(old_segments, list):
        return transcript
    words: list[dict[str, Any]] = []
    for segment in old_segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("words"), list):
            # Older transcripts without word timing cannot be safely re-segmented.
            return transcript
        if not segment["words"] and segment.get("text"):
            # Do not drop recognized text if a source segment has no word timings.
            return transcript
        for word in segment["words"]:
            if not isinstance(word, dict) or not {"start", "end", "word"}.issubset(word):
                return transcript
            words.append(word)
    normalized = dict(transcript)
    normalized["segments"] = sentence_segments(words)
    return normalized

def transcribe_worker(eid: int, jid: str) -> None:
    try:
        row = get_episode(eid)
        with job_lock: jobs[jid].update(status="downloading", progress=0)
        audio = Path(row["local_audio"]) if row["local_audio"] and Path(row["local_audio"]).is_file() else None
        if audio is None:
            suffix = Path(urllib.parse.urlsplit(row["audio_url"]).path).suffix.lower()
            if suffix not in {".mp3",".m4a",".mp4",".wav",".ogg",".opus",".aac",".flac",".webm"}: suffix = ".audio"
            audio = AUDIO_DIR / f"{eid}-{hashlib.sha256(row['guid'].encode()).hexdigest()[:12]}{suffix}"
            req = urllib.request.Request(row["audio_url"], headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=60) as response, audio.open("wb") as out:
                    while True:
                        part = response.read(1024 * 1024)
                        if not part: break
                        out.write(part)
            except Exception:
                audio.unlink(missing_ok=True); raise
        set_state(eid,"processing",local_audio=str(audio))
        with job_lock: jobs[jid].update(status="transcribing",progress=10)
        from faster_whisper import WhisperModel
        model = WhisperModel(os.environ.get("WHISPER_MODEL","small.en"),device="cpu",compute_type="int8",cpu_threads=max(1,int(os.environ.get("WHISPER_CPU_THREADS",os.cpu_count() or 2))))
        iterator, info = model.transcribe(str(audio),language="en",word_timestamps=True,vad_filter=True,beam_size=5)
        raw_segments = []
        for seg in iterator:
            words = [{"start":round(w.start,3),"end":round(w.end,3),"word":w.word.strip()} for w in (seg.words or [])]
            raw_segments.append({"start":round(seg.start,3),"end":round(seg.end,3),"text":seg.text,"words":words})
        transcript = {"segments":raw_segments,"language":getattr(info,"language","en"),"duration":getattr(info,"duration",None)}
        set_state(eid,"ready",transcript=transcript)
        with job_lock: jobs[jid].update(status="ready",progress=100)
    except Exception as e:
        message = str(e)[:500]
        try: set_state(eid,"error",error=message)
        except Exception: pass
        with job_lock: jobs[jid].update(status="error",progress=0,error=message)
    finally:
        with job_lock:
            _active_episode_jobs.discard(eid)

@app.post("/api/episodes/{episode_id}/transcribe",status_code=202)
def start_transcription(episode_id: int) -> dict[str, str]:
    get_episode(episode_id)
    jid = enqueue_episode(episode_id, "manual", allow_ready=True)
    if jid is None: raise HTTPException(409,"Transcription is already in progress")
    return {"job_id":jid,"status":"queued"}

@app.get("/api/episodes/{episode_id}/transcript")
def get_transcript(episode_id: int) -> dict[str, Any]:
    row = get_episode(episode_id)
    transcript = json.loads(row["transcript_json"]) if row["transcript_json"] else None
    if isinstance(transcript, dict):
        transcript = normalize_transcript(transcript)
        segments = transcript.get("segments")
        if isinstance(segments, list):
            for segment in segments:
                if isinstance(segment, dict):
                    segment["source_fingerprint"] = _segment_source_fingerprint(episode_id, segment)
        if row["transcript_status"] == "ready":
            enqueue_episode_translation_prefetch(episode_id, resume_paused=True)
    return {"status":row["transcript_status"],"error":row["transcript_error"],"transcript":transcript}


@app.get("/api/episodes/{episode_id}/translation-progress")
def get_translation_progress(episode_id: int) -> dict[str, Any]:
    get_episode(episode_id)
    return translation_prefetch_progress(episode_id)

def _segment_source_fingerprint(episode_id: int, segment: dict[str, Any]) -> str:
    """Identify server transcript content/timing so stale browser indexes are rejected."""
    try:
        start, end = round(float(segment.get("start")), 3), round(float(segment.get("end")), 3)
        text = segment.get("text")
        if not isinstance(text, str) or start < 0 or end < start:
            return ""
    except (TypeError, ValueError, OverflowError):
        return ""
    identity = json.dumps({"episode_id": episode_id, "text": text.strip(), "start": start, "end": end},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()

def _current_segment_fingerprint(episode_id: int, segment_index: int) -> str:
    """Read current transcript state for a final stale-index check around provider work."""
    row = get_episode(episode_id)
    if row["transcript_status"] != "ready" or not row["transcript_json"]:
        return ""
    try:
        transcript = normalize_transcript(json.loads(row["transcript_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    segments = transcript.get("segments") if isinstance(transcript, dict) else None
    if not isinstance(segments, list) or segment_index < 0 or segment_index >= len(segments):
        return ""
    segment = segments[segment_index]
    return _segment_source_fingerprint(episode_id, segment) if isinstance(segment, dict) else ""


def _translation_content_key(source_fingerprint: str, model: str) -> str:
    identity = json.dumps({"source_fingerprint": source_fingerprint, "target_language": "ja",
                           "provider": "agy", "model": model},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _mark_translation_prefetch_ready(episode_id: int, content_key: str, model: str) -> None:
    with db() as c:
        c.execute("UPDATE translation_prefetch_jobs SET status='ready',error='',updated_at=? WHERE episode_id=? AND content_key=? AND model=?",
                  (datetime.now(timezone.utc).isoformat(), episode_id, content_key, model))


def enqueue_episode_translation_prefetch(episode_id: int, *, resume_paused: bool = False) -> int:
    """Persist missing translations for a ready transcript; safe to call repeatedly."""
    try:
        row = get_episode(episode_id)
    except HTTPException:
        return 0
    if row["transcript_status"] != "ready" or not row["transcript_json"]:
        return 0
    try:
        transcript = normalize_transcript(json.loads(row["transcript_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    segments = transcript.get("segments") if isinstance(transcript, dict) else None
    if not isinstance(segments, list):
        return 0
    model = AGY_TRANSLATION_MODEL
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    with db() as c:
        active_keys: list[str] = []
        prepared: list[tuple[int, str, str, str, str]] = []
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            sentence = segment.get("text")
            fingerprint = _segment_source_fingerprint(episode_id, segment)
            if not fingerprint or not isinstance(sentence, str) or not sentence.strip() or len(sentence.strip()) > 4_000:
                continue
            content_key = _translation_content_key(fingerprint, model)
            active_keys.append(content_key)
            cached = c.execute(
                "SELECT expressions_json FROM sentence_translations WHERE episode_id=? AND content_key=? AND target_language='ja' AND provider='agy' AND model=?",
                (episode_id, content_key, model),
            ).fetchone()
            status = "ready" if cached is not None and cached["expressions_json"] is not None else "pending"
            prepared.append((index, content_key, fingerprint, status, model))
        active_key_set = set(active_keys)
        old_jobs = c.execute("SELECT content_key FROM translation_prefetch_jobs WHERE episode_id=? AND model=? AND status IN ('pending','running')",
                             (episode_id, model)).fetchall()
        for old_job in old_jobs:
            if old_job["content_key"] not in active_key_set:
                c.execute("UPDATE translation_prefetch_jobs SET status='stale',error='Transcript sentence changed.',updated_at=? WHERE episode_id=? AND content_key=? AND model=? AND status IN ('pending','running')",
                          (now, episode_id, old_job["content_key"], model))
        for index, content_key, fingerprint, status, selected_model in prepared:
            c.execute(
                "INSERT INTO translation_prefetch_jobs(episode_id,content_key,model,segment_index,source_fingerprint,status,error,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(episode_id,content_key,model) DO UPDATE SET "
                "segment_index=excluded.segment_index,source_fingerprint=excluded.source_fingerprint,"
                "status=CASE WHEN excluded.status='ready' THEN 'ready' WHEN translation_prefetch_jobs.status IN ('stale','ready') THEN 'pending' ELSE translation_prefetch_jobs.status END,"
                "error=CASE WHEN excluded.status='ready' OR translation_prefetch_jobs.status IN ('stale','ready') THEN '' ELSE translation_prefetch_jobs.error END,updated_at=excluded.updated_at",
                (episode_id, content_key, selected_model, index, fingerprint, status, "", now),
            )
            added += 1
        if resume_paused:
            c.execute("UPDATE translation_prefetch_control SET paused=0,error='',updated_at=? WHERE id=1", (now,))
    if added or resume_paused:
        wake_translation_prefetch_worker()
    return added


def wake_translation_prefetch_worker() -> None:
    global _translation_prefetch_executor, _translation_prefetch_future
    global _translation_prefetch_stop, _translation_prefetch_wakeup
    with _translation_prefetch_control_lock:
        # Tests and one-off imports do not start provider work unless the app
        # lifespan explicitly enabled the background worker.
        if _translation_prefetch_wakeup is None:
            return
        if _translation_prefetch_future is None or _translation_prefetch_future.done():
            _translation_prefetch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="translation-prefetch")
            _translation_prefetch_stop = threading.Event()
            _translation_prefetch_wakeup = threading.Event()
            _translation_prefetch_future = _translation_prefetch_executor.submit(
                _translation_prefetch_loop, _translation_prefetch_stop, _translation_prefetch_wakeup
            )
        _translation_prefetch_wakeup.set()


def start_translation_prefetch_worker() -> None:
    global _translation_prefetch_executor, _translation_prefetch_future
    global _translation_prefetch_stop, _translation_prefetch_wakeup
    with _translation_prefetch_control_lock:
        if _translation_prefetch_future is not None and not _translation_prefetch_future.done():
            if _translation_prefetch_wakeup is not None:
                _translation_prefetch_wakeup.set()
            return
        _translation_prefetch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="translation-prefetch")
        _translation_prefetch_stop = threading.Event()
        _translation_prefetch_wakeup = threading.Event()
        _translation_prefetch_future = _translation_prefetch_executor.submit(
            _translation_prefetch_loop, _translation_prefetch_stop, _translation_prefetch_wakeup
        )


def stop_translation_prefetch_worker() -> None:
    global _translation_prefetch_executor, _translation_prefetch_future
    global _translation_prefetch_stop, _translation_prefetch_wakeup
    with _translation_prefetch_control_lock:
        if _translation_prefetch_stop is not None:
            _translation_prefetch_stop.set()
        if _translation_prefetch_wakeup is not None:
            _translation_prefetch_wakeup.set()
        executor = _translation_prefetch_executor
        _translation_prefetch_executor = None
        _translation_prefetch_future = None
        _translation_prefetch_stop = None
        _translation_prefetch_wakeup = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def recover_translation_prefetch_jobs() -> int:
    """Return interrupted work to pending without retrying completed failures."""
    with db() as c:
        c.execute("UPDATE translation_prefetch_jobs SET status='pending',error='',updated_at=? WHERE status='running'",
                  (datetime.now(timezone.utc).isoformat(),))
        return c.execute("SELECT COUNT(*) FROM translation_prefetch_jobs WHERE status='pending'").fetchone()[0]


def _next_prefetch_batch() -> list[sqlite3.Row]:
    with db() as c:
        first = c.execute(
            "SELECT episode_id,model FROM translation_prefetch_jobs WHERE status='pending' ORDER BY episode_id,segment_index LIMIT 1"
        ).fetchone()
        if first is None:
            return []
        episode = c.execute("SELECT transcript_status,transcript_json FROM episodes WHERE id=?", (first["episode_id"],)).fetchone()
        rows = c.execute(
            "SELECT * FROM translation_prefetch_jobs WHERE episode_id=? AND model=? AND status='pending' ORDER BY segment_index LIMIT ?",
            (first["episode_id"], first["model"], MAX_BATCH_SENTENCES),
        ).fetchall()
        try:
            transcript = normalize_transcript(json.loads(episode["transcript_json"] or "{}")) if episode else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            transcript = {}
        segments = transcript.get("segments") if isinstance(transcript, dict) else None
        batch: list[sqlite3.Row] = []
        chars = 0
        for item in rows:
            index = item["segment_index"]
            text = segments[index].get("text", "") if isinstance(segments, list) and 0 <= index < len(segments) and isinstance(segments[index], dict) else ""
            sentence = text.strip() if isinstance(text, str) else ""
            if not sentence:
                batch.append(item)
                continue
            if batch and chars + len(sentence) > MAX_BATCH_INPUT_CHARS:
                break
            # Sentence contents are loaded from the transcript and are never
            # copied into the durable job table.
            batch.append(item)
            chars += len(sentence)
        now = datetime.now(timezone.utc).isoformat()
        claimed: list[sqlite3.Row] = []
        for item in batch:
            cur = c.execute(
                "UPDATE translation_prefetch_jobs SET status='running',updated_at=? WHERE episode_id=? AND content_key=? AND model=? AND status='pending'",
                (now, item["episode_id"], item["content_key"], item["model"]),
            )
            if cur.rowcount:
                claimed.append(item)
        return claimed


def _prefetch_batch(items: list[sqlite3.Row], stop: threading.Event | None = None) -> None:
    if not items:
        return
    episode_id = items[0]["episode_id"]
    row = get_episode(episode_id)
    try:
        transcript = normalize_transcript(json.loads(row["transcript_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        transcript = {}
    segments = transcript.get("segments") if isinstance(transcript, dict) else None
    if row["transcript_status"] != "ready" or not isinstance(segments, list):
        _finish_prefetch_items(items, "stale", "Transcript is no longer available.")
        return

    candidates: list[tuple[sqlite3.Row, str]] = []
    for item in items:
        index = item["segment_index"]
        if index < 0 or index >= len(segments) or not isinstance(segments[index], dict):
            _finish_prefetch_items([item], "stale", "Transcript sentence changed.")
            continue
        segment = segments[index]
        sentence = segment.get("text")
        if (_segment_source_fingerprint(episode_id, segment) != item["source_fingerprint"]
                or not isinstance(sentence, str) or not sentence.strip()):
            _finish_prefetch_items([item], "stale", "Transcript sentence changed.")
            continue
        candidates.append((item, sentence.strip()))
    if not candidates:
        return

    locks: list[tuple[tuple[int, str], threading.Lock]] = []
    try:
        for item, _ in candidates:
            key = (episode_id, item["content_key"])
            locks.append((key, _acquire_sentence_translation_lock(key)))
        missing: list[tuple[sqlite3.Row, str]] = []
        for item, sentence in candidates:
            with db() as c:
                cached = c.execute(
                    "SELECT translation,expressions_json FROM sentence_translations WHERE episode_id=? AND content_key=? AND target_language='ja' AND provider='agy' AND model=?",
                    (episode_id, item["content_key"], item["model"]),
                ).fetchone()
            if cached is not None and cached["expressions_json"] is not None:
                _finish_prefetch_items([item], "ready", "")
            elif _current_segment_fingerprint(episode_id, item["segment_index"]) != item["source_fingerprint"]:
                _finish_prefetch_items([item], "stale", "Transcript sentence changed.")
            else:
                missing.append((item, sentence))
        if not missing:
            return
        try:
            with _agy_request_lock:
                if stop is not None and stop.is_set():
                    _finish_prefetch_items([item for item, _ in missing], "pending", "")
                    return
                current_missing = [(item, sentence) for item, sentence in missing
                                   if _current_segment_fingerprint(episode_id, item["segment_index"]) == item["source_fingerprint"]]
                stale_missing = [item for item, _ in missing
                                 if _current_segment_fingerprint(episode_id, item["segment_index"]) != item["source_fingerprint"]]
                if stale_missing:
                    _finish_prefetch_items(stale_missing, "stale", "Transcript sentence changed.")
                if not current_missing:
                    return
                results = translate_sentences_with_expressions([sentence for _, sentence in current_missing], model=items[0]["model"])
        except TranslationRunnerError as exc:
            # A batch fails closed and is not automatically retried. A later tap
            # can still translate an individual sentence through the normal API.
            message = str(exc) if str(exc) in {"Translation timed out. Please try again.",
                                                "Translation is unavailable.",
                                                "Translation could not be completed."} else "Translation could not be completed."
            _finish_prefetch_items([item for item, _ in current_missing], "error", message)
            return
        if len(results) != len(current_missing):
            _finish_prefetch_items([item for item, _ in current_missing], "error", "Translation could not be completed.")
            return
        now = datetime.now(timezone.utc).isoformat()
        with db() as c:
            for (item, _), result in zip(current_missing, results):
                current = _current_segment_fingerprint(episode_id, item["segment_index"])
                if current != item["source_fingerprint"]:
                    c.execute("UPDATE translation_prefetch_jobs SET status='stale',error='Transcript sentence changed.',updated_at=? WHERE episode_id=? AND content_key=? AND model=? AND status='running'",
                              (now, episode_id, item["content_key"], item["model"]))
                    continue
                expressions_json = json.dumps(result["expressions"], ensure_ascii=False, separators=(",", ":"))
                c.execute("INSERT OR IGNORE INTO sentence_translations(episode_id,content_key,target_language,provider,model,translation,created_at,expressions_json) VALUES(?,?,'ja','agy',?,?,?,?)",
                          (episode_id, item["content_key"], item["model"], result["translation"], now, expressions_json))
                # Keep a translation generated before this feature unchanged while filling its missing notes.
                c.execute("UPDATE sentence_translations SET expressions_json=COALESCE(expressions_json,?) WHERE episode_id=? AND content_key=? AND target_language='ja' AND provider='agy' AND model=?",
                          (expressions_json, episode_id, item["content_key"], item["model"]))
                c.execute("UPDATE translation_prefetch_jobs SET status='ready',error='',updated_at=? WHERE episode_id=? AND content_key=? AND model=?",
                          (now, episode_id, item["content_key"], item["model"]))
    finally:
        for key, lock in reversed(locks):
            _release_sentence_translation_lock(key, lock)


def _finish_prefetch_items(items: list[sqlite3.Row], status: str, error: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        for item in items:
            c.execute("UPDATE translation_prefetch_jobs SET status=?,error=?,updated_at=? WHERE episode_id=? AND content_key=? AND model=? AND status='running'",
                      (status, error, now, item["episode_id"], item["content_key"], item["model"]))


def pause_translation_prefetch(error: str) -> None:
    with db() as c:
        c.execute("UPDATE translation_prefetch_control SET paused=1,error=?,updated_at=? WHERE id=1",
                  (error, datetime.now(timezone.utc).isoformat()))


def _translation_prefetch_loop(stop: threading.Event, wakeup: threading.Event) -> None:
    # Keep a stale worker from a just-closed test/server lifespan from overlapping
    # the replacement worker while its bounded CLI request is still finishing.
    with _translation_prefetch_runtime_lock:
        recover_translation_prefetch_jobs()
        with db() as c:
            paused = c.execute("SELECT paused FROM translation_prefetch_control WHERE id=1").fetchone()[0]
        if paused:
            return
        while not stop.is_set():
            items = _next_prefetch_batch()
            if not items:
                wakeup.wait(timeout=30)
                wakeup.clear()
                continue
            try:
                _prefetch_batch(items, stop)
            except Exception:
                logger.warning("Translation prefetch batch failed (episode_id=%s)", items[0]["episode_id"])
                _finish_prefetch_items(items, "error", "Translation could not be completed.")
                pause_translation_prefetch("Translation could not be completed.")
                return
            with db() as c:
                failed = any(c.execute("SELECT 1 FROM translation_prefetch_jobs WHERE episode_id=? AND content_key=? AND model=? AND status='error' LIMIT 1",
                                       (item["episode_id"], item["content_key"], item["model"])).fetchone() for item in items)
            if failed:
                pause_translation_prefetch("Translation is unavailable.")
                return
            if not stop.is_set():
                stop.wait(TRANSLATION_PREFETCH_INTERVAL_SECONDS)


def translation_prefetch_progress(episode_id: int) -> dict[str, Any]:
    with db() as c:
        jobs = c.execute("SELECT content_key,status FROM translation_prefetch_jobs WHERE episode_id=? AND model=?",
                         (episode_id, AGY_TRANSLATION_MODEL)).fetchall()
        episode = c.execute("SELECT transcript_status,transcript_json FROM episodes WHERE id=?", (episode_id,)).fetchone()
        control = c.execute("SELECT paused,error FROM translation_prefetch_control WHERE id=1").fetchone()
    active_keys: set[str] = set()
    if episode and episode["transcript_status"] == "ready" and episode["transcript_json"]:
        try:
            transcript = normalize_transcript(json.loads(episode["transcript_json"]))
            segments = transcript.get("segments") if isinstance(transcript, dict) else None
            if isinstance(segments, list):
                for segment in segments:
                    if isinstance(segment, dict):
                        fingerprint = _segment_source_fingerprint(episode_id, segment)
                        if fingerprint:
                            active_keys.add(_translation_content_key(fingerprint, AGY_TRANSLATION_MODEL))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    counts: dict[str, int] = {}
    for job in jobs:
        if job["content_key"] in active_keys:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
    total = sum(counts.values())
    pending, running = counts.get("pending", 0), counts.get("running", 0)
    errors, stale = counts.get("error", 0), counts.get("stale", 0)
    ready = counts.get("ready", 0)
    if total == 0:
        status = "not_started"
    elif control and control["paused"]:
        status = "paused"
    elif pending or running:
        status = "preparing"
    elif errors or stale:
        status = "partial"
    else:
        status = "ready"
    return {"status": status, "total": total, "ready": ready, "pending": pending,
            "running": running, "errors": errors, "stale": stale}

def _acquire_sentence_translation_lock(key: tuple[int, str]) -> threading.Lock:
    with _translation_lock_guard:
        lock, users = _translation_locks.get(key, (threading.Lock(), 0))
        _translation_locks[key] = (lock, users + 1)
    lock.acquire()
    return lock

def _release_sentence_translation_lock(key: tuple[int, str], lock: threading.Lock) -> None:
    lock.release()
    with _translation_lock_guard:
        current_lock, users = _translation_locks[key]
        if users <= 1:
            del _translation_locks[key]
        else:
            _translation_locks[key] = (current_lock, users - 1)

class TranslationInput(BaseModel):
    expected_source_fingerprint: str

@app.post("/api/episodes/{episode_id}/translations/{segment_index}")
def translate_episode_segment(episode_id: int, segment_index: int, payload: TranslationInput) -> dict[str, Any]:
    """Return a cached or newly generated Japanese translation for one transcript line."""
    row = get_episode(episode_id)
    if row["transcript_status"] != "ready" or not row["transcript_json"]:
        raise HTTPException(409, "A transcript is not available for this episode")
    try:
        transcript = normalize_transcript(json.loads(row["transcript_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(409, "A transcript is not available for this episode") from None
    segments = transcript.get("segments") if isinstance(transcript, dict) else None
    if not isinstance(segments, list) or segment_index < 0 or segment_index >= len(segments):
        raise HTTPException(422, "Invalid transcript sentence")
    segment = segments[segment_index]
    if not isinstance(segment, dict):
        raise HTTPException(422, "Invalid transcript sentence")
    sentence = segment.get("text")
    if not isinstance(sentence, str):
        raise HTTPException(422, "Invalid transcript sentence")
    sentence = sentence.strip()
    if not sentence or len(sentence) > 4000:
        raise HTTPException(422, "Invalid transcript sentence")
    try:
        start = round(float(segment.get("start")), 3)
        end = round(float(segment.get("end")), 3)
        if start < 0 or end < start:
            raise ValueError("invalid timing")
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, "Invalid transcript sentence") from None

    expected_fingerprint = payload.expected_source_fingerprint
    if not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint):
        raise HTTPException(422, "Invalid transcript sentence identity")
    source_fingerprint = _segment_source_fingerprint(episode_id, segment)
    if not source_fingerprint or expected_fingerprint != source_fingerprint:
        raise HTTPException(409, "Transcript sentence changed. Refresh the transcript and try again.")

    provider = "agy"
    model = AGY_TRANSLATION_MODEL
    content_key = _translation_content_key(source_fingerprint, model)
    def cached_translation() -> sqlite3.Row | None:
        with db() as c:
            return c.execute("SELECT translation,expressions_json FROM sentence_translations WHERE episode_id=? AND content_key=? AND target_language='ja' AND provider=? AND model=?",
                             (episode_id, content_key, provider, model)).fetchone()
    cached = cached_translation()
    if cached is not None and cached["expressions_json"] is not None:
        _mark_translation_prefetch_ready(episode_id, content_key, model)
        return {"translation": cached["translation"], "expressions": json.loads(cached["expressions_json"]),
                "expressions_ready": True, "cached": True, "target_language": "ja", "source_fingerprint": source_fingerprint}

    lock_key = (episode_id, content_key)
    lock = _acquire_sentence_translation_lock(lock_key)
    try:
        # Recheck after acquiring the per-sentence lock so concurrent taps share
        # one provider request while unrelated cache hits remain nonblocking.
        if _current_segment_fingerprint(episode_id, segment_index) != source_fingerprint:
            raise HTTPException(409, "Transcript sentence changed. Refresh the transcript and try again.")
        cached = cached_translation()
        if cached is not None and cached["expressions_json"] is not None:
            _mark_translation_prefetch_ready(episode_id, content_key, model)
            return {"translation": cached["translation"], "expressions": json.loads(cached["expressions_json"]),
                    "expressions_ready": True, "cached": True, "target_language": "ja", "source_fingerprint": source_fingerprint}
        try:
            with _agy_request_lock:
                if _current_segment_fingerprint(episode_id, segment_index) != source_fingerprint:
                    raise HTTPException(409, "Transcript sentence changed. Refresh the transcript and try again.")
                generated = translate_sentence_with_expressions(sentence, model=model)
        except TranslationRunnerError as error:
            if cached is not None:
                if _current_segment_fingerprint(episode_id, segment_index) != source_fingerprint:
                    raise HTTPException(409, "Transcript sentence changed. Refresh the transcript and try again.") from None
                return {"translation": cached["translation"], "expressions": [], "expressions_ready": False,
                        "explanation_error": str(error), "cached": True, "target_language": "ja",
                        "source_fingerprint": source_fingerprint}
            message = str(error)
            if message == "Translation timed out. Please try again.":
                raise HTTPException(504, message) from None
            status = 502 if message == "Translation could not be completed." else 503
            raise HTTPException(status, message) from None
        if _current_segment_fingerprint(episode_id, segment_index) != source_fingerprint:
            raise HTTPException(409, "Transcript sentence changed. Refresh the transcript and try again.")
        expressions_json = json.dumps(generated["expressions"], ensure_ascii=False, separators=(",", ":"))
        with db() as c:
            c.execute("INSERT OR IGNORE INTO sentence_translations(episode_id,content_key,target_language,provider,model,translation,created_at,expressions_json) VALUES(?,?,'ja',?,?,?,?,?)",
                      (episode_id, content_key, provider, model, generated["translation"], datetime.now(timezone.utc).isoformat(), expressions_json))
            c.execute("UPDATE sentence_translations SET expressions_json=COALESCE(expressions_json,?) WHERE episode_id=? AND content_key=? AND target_language='ja' AND provider=? AND model=?",
                      (expressions_json, episode_id, content_key, provider, model))
            c.execute("UPDATE translation_prefetch_jobs SET status='ready',error='',updated_at=? WHERE episode_id=? AND content_key=? AND model=?",
                      (datetime.now(timezone.utc).isoformat(), episode_id, content_key, model))
            stored = c.execute("SELECT translation,expressions_json FROM sentence_translations WHERE episode_id=? AND content_key=? AND target_language='ja' AND provider=? AND model=?",
                                (episode_id, content_key, provider, model)).fetchone()
            c.execute("UPDATE translation_prefetch_control SET paused=0,error='',updated_at=? WHERE id=1",
                      (datetime.now(timezone.utc).isoformat(),))
        wake_translation_prefetch_worker()
        return {"translation": stored["translation"], "expressions": json.loads(stored["expressions_json"]),
                "expressions_ready": True, "cached": cached is not None, "target_language": "ja",
                "source_fingerprint": source_fingerprint}
    finally:
        _release_sentence_translation_lock(lock_key, lock)

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with job_lock: job = jobs.get(job_id)
    if job is None: raise HTTPException(404,"Job not found; jobs reset when server restarts")
    return dict(job)

@app.get("/")
def index() -> FileResponse:
    file = BASE_DIR / "index.html"
    if not file.exists(): raise HTTPException(503,"Frontend is not installed yet")
    return FileResponse(file, headers={"Cache-Control": "no-cache"})

assets = BASE_DIR / "assets"
if assets.is_dir(): app.mount("/assets",StaticFiles(directory=assets),name="assets")

@app.get("/{static_name}")
def static_file(static_name: str) -> FileResponse:
    # Serve only the public frontend files; never expose the application, DB, or audio cache.
    if static_name not in {"app.js", "styles.css", "manifest.json"}:
        raise HTTPException(404, "Not found")
    file = BASE_DIR / static_name
    if not file.is_file(): raise HTTPException(404, "Not found")
    return FileResponse(file)
