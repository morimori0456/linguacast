"""Backend checks for automatic transcription of newly published episodes.

These tests use a temporary SQLite database, synthetic RSS records and a fake
executor. No podcast endpoint, audio download, or speech model is contacted.
Run with ``python -m unittest discover -s tests -v``.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import app as backend


def episode(guid: str, published_at: str) -> dict[str, str]:
    return {
        "guid": guid,
        "title": guid,
        "description": "Synthetic episode",
        "audio_url": f"https://example.test/{guid}.mp3",
        "published_at": published_at,
        "publication_time": published_at,
        "duration": "00:01:00",
    }


class AutoTranscriptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = backend.DB_PATH
        self.old_audio_dir = backend.AUDIO_DIR
        self.old_executor = backend.executor
        backend.DB_PATH = Path(self.temp.name) / "test.sqlite3"
        backend.AUDIO_DIR = Path(self.temp.name) / "audio"
        backend.AUDIO_DIR.mkdir()
        self.executor = Mock()
        backend.executor = self.executor
        backend.jobs.clear()
        backend._active_episode_jobs.clear()
        self.feed_counter = 0
        backend.init_db()
        self.feed_id = self.add_feed([])

    def tearDown(self) -> None:
        backend.DB_PATH = self.old_db_path
        backend.AUDIO_DIR = self.old_audio_dir
        backend.executor = self.old_executor
        backend.jobs.clear()
        backend._active_episode_jobs.clear()
        self.temp.cleanup()

    def add_feed(self, episodes: list[dict[str, str]]) -> int:
        self.feed_counter += 1
        with patch.object(backend, "parse_feed", return_value=(
            {"title": "Synthetic Podcast", "description": "", "image_url": ""}, episodes
        )):
            result = backend.add_feed(backend.FeedInput(url=f"https://example.test/feed-{self.feed_counter}.xml"))
        return result["id"]

    def parse_records(self, records: list[dict[str, str]]):
        return patch.object(backend, "parse_feed", return_value=(
            {"title": "Synthetic Podcast", "description": "", "image_url": ""}, records
        ))

    def rows(self) -> list[dict]:
        with backend.db() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM episodes WHERE feed_id=? ORDER BY guid", (self.feed_id,)
            )]

    def insert_legacy_feed(self, url: str, published: str) -> int:
        """Create a pre-feature feed with a publication date older than migration time."""
        with backend.db() as conn:
            cursor = conn.execute(
                "INSERT INTO feeds(url,title,added_at,auto_since) VALUES(?,?,?,?)",
                (url, "Legacy", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
            )
            fid = cursor.lastrowid
            conn.execute(
                "INSERT INTO episodes(feed_id,guid,title,audio_url,published_at) VALUES(?,?,?,?,?)",
                (fid, "legacy-old", "Legacy old", "https://example.test/old.mp3", published),
            )
        return fid

    def test_registration_is_baseline_and_only_new_published_episode_is_queued(self) -> None:
        old = episode("archive", "Tue, 01 Jan 2019 00:00:00 +0000")
        with patch.object(backend, "parse_feed", return_value=(
            {"title": "Synthetic Podcast", "description": "", "image_url": ""}, [old]
        )):
            result = backend.add_feed(backend.FeedInput(url="https://example.test/baseline.xml"))
        feed_id = result["id"]
        self.assertEqual(result["episode_count"], 1)
        self.executor.submit.assert_not_called()

        fresh = episode("fresh", datetime.now(timezone.utc).isoformat())
        with patch.object(backend, "parse_feed", return_value=(
            {"title": "Synthetic Podcast", "description": "", "image_url": ""}, [fresh, old]
        )):
            backend.refresh_feed_internal(feed_id)

        with backend.db() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT guid,transcript_status,transcript_origin FROM episodes WHERE feed_id=? ORDER BY guid", (feed_id,)
            )]
        self.assertEqual(rows, [
            {"guid": "archive", "transcript_status": "none", "transcript_origin": "manual"},
            {"guid": "fresh", "transcript_status": "queued", "transcript_origin": "auto"},
        ])
        self.assertEqual(self.executor.submit.call_count, 1)

    def test_unknown_and_pre_activation_dates_are_never_automatically_queued(self) -> None:
        records = [
            episode("unknown", ""),
            episode("invalid", "not a date"),
            episode("old", "Tue, 01 Jan 2019 00:00:00 +0000"),
        ]
        with self.parse_records(records):
            backend.refresh_feed_internal(self.feed_id)
        self.assertEqual(self.executor.submit.call_count, 0)
        self.assertTrue(all(row["transcript_status"] == "none" for row in self.rows()))

    def test_existing_feed_migration_treats_archive_as_baseline(self) -> None:
        old = episode("pre-upgrade-archive", "Tue, 01 Jan 2019 00:00:00 +0000")
        with self.parse_records([old]):
            backend.refresh_feed_internal(self.feed_id, auto_queue=False)
        with backend.db() as conn:
            # NULL models a database created before the auto_since migration.
            conn.execute("UPDATE feeds SET auto_since=NULL WHERE id=?", (self.feed_id,))
        backend.init_db()
        fresh = episode("post-upgrade", datetime.now(timezone.utc).isoformat())
        with self.parse_records([fresh, old]):
            backend.refresh_feed_internal(self.feed_id)
        by_guid = {row["guid"]: row for row in self.rows()}
        self.assertEqual(by_guid["pre-upgrade-archive"]["transcript_status"], "none")
        self.assertEqual(by_guid["post-upgrade"]["transcript_status"], "queued")
        self.assertEqual(self.executor.submit.call_count, 1)

    def test_future_episode_waits_until_publication_then_queues_once(self) -> None:
        published_at = datetime.now(timezone.utc) + timedelta(days=1)
        future = episode("future", published_at.isoformat())

        class Clock(datetime):
            current = datetime.now(timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current if tz is not None else cls.current.replace(tzinfo=None)

        with self.parse_records([future]), patch.object(backend, "datetime", Clock):
            backend.refresh_feed_internal(self.feed_id)
            row = self.rows()[0]
            self.assertEqual(row["transcript_status"], "none")
            self.assertEqual(self.executor.submit.call_count, 0)

            Clock.current = published_at + timedelta(seconds=1)
            backend.refresh_feed_internal(self.feed_id)

        row = self.rows()[0]
        self.assertEqual((row["transcript_status"], row["transcript_origin"]), ("queued", "auto"))
        self.assertEqual(self.executor.submit.call_count, 1)

    def test_repeated_and_concurrent_refreshes_enqueue_one_job(self) -> None:
        fresh = episode("one-time", datetime.now(timezone.utc).isoformat())
        with self.parse_records([fresh]):
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda _: backend.refresh_feed_internal(self.feed_id), range(12)))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.executor.submit.call_count, 1)

    def test_manual_queue_and_recovery_are_deduplicated(self) -> None:
        old = episode("manual", "Tue, 01 Jan 2019 00:00:00 +0000")
        with self.parse_records([old]):
            backend.refresh_feed_internal(self.feed_id)
        row_id = self.rows()[0]["id"]
        first = backend.enqueue_episode(row_id, "manual", allow_ready=True)
        duplicate = backend.enqueue_episode(row_id, "manual", allow_ready=True)
        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(self.executor.submit.call_count, 1)

        # Manual tasks are not automatically resumed after a process restart.
        backend.jobs.clear()
        backend._active_episode_jobs.clear()
        backend.init_db()
        self.assertEqual(self.rows()[0]["transcript_status"], "error")
        self.assertEqual(backend.recover_auto_jobs(), 0, "manual jobs are not auto-recovered")
        self.assertEqual(self.executor.submit.call_count, 1)

    def test_auto_recovery_resets_interrupted_work_and_does_not_touch_manual_jobs(self) -> None:
        fresh = episode("interrupted", datetime.now(timezone.utc).isoformat())
        with self.parse_records([fresh]):
            backend.refresh_feed_internal(self.feed_id)
        row_id = self.rows()[0]["id"]
        with backend.db() as conn:
            conn.execute("UPDATE episodes SET transcript_status='processing' WHERE id=?", (row_id,))
        backend._active_episode_jobs.clear()
        self.assertEqual(backend.recover_auto_jobs(), 1)
        row = self.rows()[0]
        self.assertEqual((row["transcript_status"], row["transcript_origin"]), ("queued", "auto"))
        backend.recover_auto_jobs()
        self.assertEqual(self.executor.submit.call_count, 2)

    def test_cancelled_auto_job_remains_recoverable_on_shutdown(self) -> None:
        fresh = episode("shutdown-auto", datetime.now(timezone.utc).isoformat())
        with self.parse_records([fresh]):
            backend.refresh_feed_internal(self.feed_id)
        row_id = self.rows()[0]["id"]
        jid = next(jid for jid, job in backend.jobs.items() if job["episode_id"] == row_id)

        class CancelledFuture:
            @staticmethod
            def cancelled() -> bool:
                return True

        backend._job_cancelled(row_id, jid, CancelledFuture())
        row = self.rows()[0]
        self.assertEqual((row["transcript_status"], row["transcript_origin"]), ("queued", "auto"))
        self.assertEqual(backend.recover_auto_jobs(), 1)

    def test_scheduler_isolates_a_broken_feed_and_continues_to_healthy_feeds(self) -> None:
        other_id = self.add_feed([])
        good = episode("healthy-new", datetime.now(timezone.utc).isoformat())
        def fake_parse(url: str):
            if url.endswith("feed-1.xml"):
                raise RuntimeError("fetch failed for https://private.example/feed?token=SECRET_VALUE")
            return ({"title": "Healthy", "description": "", "image_url": ""}, [good])

        # The first registered feed is deliberately broken; the next one still polls.
        broken_id = self.feed_id
        self.feed_id = other_id
        with patch.object(backend, "parse_feed", side_effect=fake_parse):
            asyncio.run(backend.poll_feeds_once())
        with backend.db() as conn:
            row = conn.execute("SELECT transcript_status FROM episodes WHERE guid='healthy-new'").fetchone()
            failed = conn.execute(
                "SELECT last_poll_at,last_poll_error FROM feeds WHERE id=?", (broken_id,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["transcript_status"], "queued")
        self.assertIsNotNone(failed["last_poll_at"])
        self.assertEqual(failed["last_poll_error"], "RSS refresh failed (RuntimeError)")
        self.assertNotIn("private.example", failed["last_poll_error"])
        self.assertNotIn("SECRET_VALUE", failed["last_poll_error"])

        # A successful subsequent poll records a timestamp and clears the stale error.
        with patch.object(backend, "parse_feed", return_value=(
            {"title": "Recovered", "description": "", "image_url": ""}, []
        )):
            asyncio.run(backend.poll_feeds_once())
        with backend.db() as conn:
            recovered = conn.execute(
                "SELECT last_poll_at,last_poll_error FROM feeds WHERE id=?", (broken_id,)
            ).fetchone()
        self.assertIsNotNone(recovered["last_poll_at"])
        self.assertEqual(recovered["last_poll_error"], "")
        self.assertEqual(self.executor.submit.call_count, 1)


if __name__ == "__main__":
    unittest.main()
