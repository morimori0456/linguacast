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
        backend._translation_lock_guard = threading.Lock()
        backend._translation_locks = {}

    def tearDown(self) -> None:
        backend._translation_lock_guard = self.old_lock_guard
        backend._translation_locks = self.old_locks
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


if __name__ == "__main__":
    unittest.main()
