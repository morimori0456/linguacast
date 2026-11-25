"""Translation endpoint tests backed by synthetic text and a temporary SQLite DB.

The provider is always replaced by a local stub; these tests never invoke agy
or send transcript content to an external service.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import app as backend


class TranslationEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = backend.DB_PATH
        self.old_model = backend.AGY_TRANSLATION_MODEL
        backend.DB_PATH = Path(self.temp.name) / "translation-test.sqlite3"
        backend.AGY_TRANSLATION_MODEL = "gemini-4-hydrogen-medium"
        backend.init_db()
        with backend.db() as conn:
            feed = conn.execute(
                "INSERT INTO feeds(url,title,added_at) VALUES(?,?,?)",
                ("https://example.test/feed.xml", "Synthetic feed", "2026-01-01T00:00:00+00:00"),
            )
            episode = conn.execute(
                "INSERT INTO episodes(feed_id,guid,title,audio_url,transcript_json,transcript_status) "
                "VALUES(?,?,?,?,?,'ready')",
                (feed.lastrowid, "synthetic", "Synthetic episode", "https://example.test/audio.mp3", json.dumps({
                    "segments": [
                        {"start": 1.0, "end": 2.5, "text": "A synthetic sentence.", "words": []},
                        {"start": 3.0, "end": 4.5, "text": "Another synthetic sentence.", "words": []},
                    ]
                })),
            )
            self.episode_id = episode.lastrowid
        self.client = TestClient(backend.app)
        self.old_lock_guard = backend._translation_lock_guard
        self.old_locks = backend._translation_locks
        self.old_agy_request_lock = backend._agy_request_lock
        backend._translation_lock_guard = threading.Lock()
        backend._translation_locks = {}
        backend._agy_request_lock = threading.Lock()

    def tearDown(self) -> None:
        backend._translation_lock_guard = self.old_lock_guard
        backend._translation_locks = self.old_locks
        backend._agy_request_lock = self.old_agy_request_lock
        backend.AGY_TRANSLATION_MODEL = self.old_model
        self.client.close()
        backend.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def current_fingerprint(self, segment: int = 0) -> str:
        response = self.client.get(f"/api/episodes/{self.episode_id}/transcript")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["transcript"]["segments"][segment]["source_fingerprint"]

    def post_translation(self, segment: int = 0, fingerprint: str | None = None):
        if fingerprint is None:
            fingerprint = self.current_fingerprint(segment)
        return self.client.post(
            f"/api/episodes/{self.episode_id}/translations/{segment}",
            json={"expected_source_fingerprint": fingerprint},
        )

    def rows(self):
        with backend.db() as conn:
            return conn.execute(
                "SELECT provider,model,translation FROM sentence_translations ORDER BY provider,model"
            ).fetchall()

    def test_translation_works_without_gemini_api_key_and_is_cached(self) -> None:
        provider = Mock(return_value="架空の英文です。")
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}), patch.object(backend, "translate_sentence", provider):
            first = self.post_translation()
            second = self.post_translation()

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["translation"], "架空の英文です。")
        self.assertFalse(first.json()["cached"])
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["cached"])
        provider.assert_called_once_with("A synthetic sentence.", model="gemini-4-hydrogen-medium")
        rows = self.rows()
        self.assertEqual([(row["provider"], row["model"], row["translation"]) for row in rows], [
            ("agy", "gemini-4-hydrogen-medium", "架空の英文です。")
        ])

    def test_cache_is_separate_for_model_and_legacy_gemini_rows(self) -> None:
        fingerprint = self.current_fingerprint()
        legacy_identity = json.dumps({
            "source_fingerprint": fingerprint,
            "target_language": "ja",
            "provider": "gemini",
            "model": "gemini-4-hydrogen",
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        legacy_key = hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()
        with backend.db() as conn:
            conn.execute(
                "INSERT INTO sentence_translations(episode_id,content_key,target_language,provider,model,translation,created_at) "
                "VALUES(?,?,'ja','gemini','gemini-4-hydrogen','legacy cached value','2026-01-01T00:00:00+00:00')",
                (self.episode_id, legacy_key),
            )

        provider = Mock(side_effect=["First model translation.", "Second model translation."])
        with patch.object(backend, "translate_sentence", provider):
            first = self.post_translation(fingerprint=fingerprint)
            backend.AGY_TRANSLATION_MODEL = "gemini-4-hydrogen-alternate"
            second = self.post_translation(fingerprint=fingerprint)

        self.assertEqual(first.json()["translation"], "First model translation.")
        self.assertEqual(second.json()["translation"], "Second model translation.")
        self.assertEqual(provider.call_args_list, [
            unittest.mock.call("A synthetic sentence.", model="gemini-4-hydrogen-medium"),
            unittest.mock.call("A synthetic sentence.", model="gemini-4-hydrogen-alternate"),
        ])
        records = [(row["provider"], row["model"], row["translation"]) for row in self.rows()]
        self.assertEqual(records, [
            ("agy", "gemini-4-hydrogen-alternate", "Second model translation."),
            ("agy", "gemini-4-hydrogen-medium", "First model translation."),
            ("gemini", "gemini-4-hydrogen", "legacy cached value"),
        ])

    def test_changed_transcript_identity_is_rejected_without_provider_call(self) -> None:
        provider = Mock(return_value="Should not run")
        with patch.object(backend, "translate_sentence", provider):
            response = self.post_translation(fingerprint="0" * 64)
        self.assertEqual(response.status_code, 409)
        provider.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_provider_failure_is_sanitized_and_not_cached(self) -> None:
        provider = Mock(side_effect=backend.TranslationRunnerError("Translation is unavailable."))
        with patch.object(backend, "translate_sentence", provider):
            response = self.post_translation()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Translation is unavailable.")
        self.assertEqual(self.rows(), [])

    def test_concurrent_requests_for_same_sentence_share_translation(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def fake_translate(sentence: str, *, model: str) -> str:
            started.set()
            if not release.wait(timeout=3):
                raise AssertionError("test failed to release the mocked provider")
            return "Concurrent synthetic translation."

        with patch.object(backend, "translate_sentence", side_effect=fake_translate) as provider:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.post_translation)
                self.assertTrue(started.wait(timeout=2))
                second = pool.submit(self.post_translation)
                release.set()
                responses = (first.result(timeout=3), second.result(timeout=3))

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual([response.json()["translation"] for response in responses], [
            "Concurrent synthetic translation.", "Concurrent synthetic translation."
        ])
        provider.assert_called_once()
        self.assertEqual(len(self.rows()), 1)

    def test_opening_existing_ready_transcript_enqueues_durable_batched_work(self) -> None:
        with patch.object(backend, "translate_sentences") as provider:
            response = self.client.get(f"/api/episodes/{self.episode_id}/transcript")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(backend.translation_prefetch_progress(self.episode_id), {
            "status": "preparing", "total": 2, "ready": 0, "pending": 2,
            "running": 0, "errors": 0, "stale": 0,
        })
        provider.assert_not_called()
        with backend.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM translation_prefetch_jobs WHERE status='pending'").fetchone()[0], 2)

    def test_new_ready_transcript_enqueues_prefetch_and_batch_results_are_cached(self) -> None:
        transcript = json.loads(backend.get_episode(self.episode_id)["transcript_json"])
        with patch.object(backend, "translate_sentences", return_value=["一つ目。", "二つ目。"]) as provider:
            backend.set_state(self.episode_id, "ready", transcript=transcript)
            with backend.db() as conn:
                batch = conn.execute("SELECT * FROM translation_prefetch_jobs WHERE status='pending' ORDER BY segment_index").fetchall()
            backend._prefetch_batch(batch)

        provider.assert_called_once_with(["A synthetic sentence.", "Another synthetic sentence."], model="gemini-4-hydrogen-medium")
        self.assertEqual(backend.translation_prefetch_progress(self.episode_id)["status"], "ready")
        self.assertEqual(len(self.rows()), 2)
        with patch.object(backend, "translate_sentence") as individual:
            response = self.post_translation(0)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["translation"], "一つ目。")
        self.assertTrue(response.json()["cached"])
        individual.assert_not_called()

    def test_failed_batch_is_terminal_until_explicit_episode_open_resumes_queue(self) -> None:
        self.client.get(f"/api/episodes/{self.episode_id}/transcript")
        with backend.db() as conn:
            batch = conn.execute("SELECT * FROM translation_prefetch_jobs WHERE status='pending' ORDER BY segment_index").fetchall()
            conn.execute("UPDATE translation_prefetch_jobs SET status='running'")
        stop = threading.Event()
        wake = threading.Event()
        with patch.object(backend, "translate_sentences", side_effect=backend.TranslationRunnerError("Translation is unavailable.")):
            original = backend._prefetch_batch
            def fail_then_stop(items, stop=None):
                with backend.db() as conn:
                    conn.execute("UPDATE translation_prefetch_jobs SET status='running'")
                original(items)
                stop.set()
            with patch.object(backend, "_next_prefetch_batch", return_value=batch), patch.object(backend, "_prefetch_batch", side_effect=fail_then_stop):
                backend._translation_prefetch_loop(stop, wake)
        self.assertEqual(backend.translation_prefetch_progress(self.episode_id)["status"], "paused")
        self.client.get(f"/api/episodes/{self.episode_id}/transcript")
        with backend.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM translation_prefetch_jobs WHERE status='error'").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT paused FROM translation_prefetch_control WHERE id=1").fetchone()[0], 0)

    def test_changed_transcript_marks_old_pending_prefetch_rows_stale(self) -> None:
        self.client.get(f"/api/episodes/{self.episode_id}/transcript")
        with backend.db() as conn:
            old_keys = {row[0] for row in conn.execute("SELECT content_key FROM translation_prefetch_jobs WHERE status='pending'")}
            transcript = json.loads(conn.execute("SELECT transcript_json FROM episodes WHERE id=?", (self.episode_id,)).fetchone()[0])
            transcript["segments"][0]["text"] = "An edited synthetic sentence."
            conn.execute("UPDATE episodes SET transcript_json=? WHERE id=?", (json.dumps(transcript), self.episode_id))
        backend.enqueue_episode_translation_prefetch(self.episode_id)
        with backend.db() as conn:
            stale_keys = {row[0] for row in conn.execute("SELECT content_key FROM translation_prefetch_jobs WHERE status='stale'")}
        self.assertEqual(stale_keys, old_keys - {backend._translation_content_key(
            backend._segment_source_fingerprint(self.episode_id, transcript["segments"][1]), backend.AGY_TRANSLATION_MODEL
        )})
        self.assertEqual(backend.translation_prefetch_progress(self.episode_id)["total"], 2)

    def test_prefetch_rechecks_transcript_after_waiting_for_provider_lock(self) -> None:
        self.client.get(f"/api/episodes/{self.episode_id}/transcript")
        with backend.db() as conn:
            item = conn.execute("SELECT * FROM translation_prefetch_jobs WHERE segment_index=0").fetchone()
            conn.execute("UPDATE translation_prefetch_jobs SET status='running' WHERE content_key=?", (item["content_key"],))
        provider_lock = backend._agy_request_lock
        provider_lock.acquire()
        checked = threading.Event()
        original_fingerprint = backend._current_segment_fingerprint
        calls = 0
        def signal_after_first_check(eid, index):
            nonlocal calls
            calls += 1
            value = original_fingerprint(eid, index)
            if calls == 1:
                checked.set()
            return value
        with patch.object(backend, "_current_segment_fingerprint", side_effect=signal_after_first_check), \
                patch.object(backend, "translate_sentences") as provider:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(backend._prefetch_batch, [item])
                self.assertTrue(checked.wait(timeout=2))
                with backend.db() as conn:
                    transcript = json.loads(conn.execute("SELECT transcript_json FROM episodes WHERE id=?", (self.episode_id,)).fetchone()[0])
                    transcript["segments"][0]["text"] = "Changed while waiting for the provider lock."
                    conn.execute("UPDATE episodes SET transcript_json=? WHERE id=?", (json.dumps(transcript), self.episode_id))
                provider_lock.release()
                future.result(timeout=2)
        provider.assert_not_called()
        with backend.db() as conn:
            status = conn.execute("SELECT status FROM translation_prefetch_jobs WHERE episode_id=? AND content_key=?",
                                  (self.episode_id, item["content_key"])).fetchone()[0]
        self.assertEqual(status, "stale")

    def test_prefetch_stopped_while_waiting_for_provider_lock_restores_pending(self) -> None:
        self.client.get(f"/api/episodes/{self.episode_id}/transcript")
        with backend.db() as conn:
            item = conn.execute("SELECT * FROM translation_prefetch_jobs WHERE segment_index=0").fetchone()
            conn.execute("UPDATE translation_prefetch_jobs SET status='running' WHERE content_key=?", (item["content_key"],))
        provider_lock = backend._agy_request_lock
        provider_lock.acquire()
        stop = threading.Event()
        checked = threading.Event()
        original_fingerprint = backend._current_segment_fingerprint
        calls = 0
        def signal_after_first_check(eid, index):
            nonlocal calls
            calls += 1
            value = original_fingerprint(eid, index)
            if calls == 1:
                checked.set()
            return value
        with patch.object(backend, "_current_segment_fingerprint", side_effect=signal_after_first_check), \
                patch.object(backend, "translate_sentences") as provider:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(backend._prefetch_batch, [item], stop)
                self.assertTrue(checked.wait(timeout=2))
                stop.set()
                provider_lock.release()
                future.result(timeout=2)
        provider.assert_not_called()
        with backend.db() as conn:
            status = conn.execute("SELECT status FROM translation_prefetch_jobs WHERE episode_id=? AND content_key=?",
                                  (self.episode_id, item["content_key"])).fetchone()[0]
        self.assertEqual(status, "pending")

    def test_prefetch_worker_start_is_idempotent(self) -> None:
        started = threading.Event()
        release = threading.Event()
        def hold_worker(stop, wake):
            started.set()
            release.wait(timeout=2)
        with patch.object(backend, "_translation_prefetch_loop", side_effect=hold_worker) as worker:
            backend.start_translation_prefetch_worker()
            self.assertTrue(started.wait(timeout=2))
            backend.start_translation_prefetch_worker()
            self.assertEqual(worker.call_count, 1)
            release.set()
            backend.stop_translation_prefetch_worker()


if __name__ == "__main__":
    unittest.main()
