"""Translation endpoint checks using an isolated SQLite database and fake Gemini.

No request is sent to Google's API. Run with ``python -m unittest discover -s tests -v``.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as backend


class TranslationEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = backend.DB_PATH
        backend.DB_PATH = Path(self.temp.name) / "translation-test.sqlite3"
        backend.init_db()
        with backend.db() as conn:
            feed = conn.execute(
                "INSERT INTO feeds(url,title,added_at) VALUES(?,?,?)",
                ("https://example.test/feed.xml", "Synthetic feed", "2026-01-01T00:00:00+00:00"),
            )
            self.feed_id = feed.lastrowid
            episode = conn.execute(
                "INSERT INTO episodes(feed_id,guid,title,audio_url,transcript_json,transcript_status) "
                "VALUES(?,?,?,?,?,'ready')",
                (self.feed_id, "synthetic", "Synthetic episode", "https://example.test/audio.mp3", json.dumps({
                    "segments": [
                        {"start": 1.0, "end": 2.5, "text": "A synthetic sentence.", "words": []},
                        {"start": 3.0, "end": 4.5, "text": "Another synthetic sentence.", "words": []},
                    ]
                })),
            )
            self.episode_id = episode.lastrowid
        self.client = TestClient(backend.app)
        self.api_key_patch = patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-never-real"})
        self.api_key_patch.start()
        self.old_lock_guard = backend._translation_lock_guard
        self.old_locks = backend._translation_locks
        backend._translation_lock_guard = threading.Lock()
        backend._translation_locks = {}

    def tearDown(self) -> None:
        backend._translation_lock_guard = self.old_lock_guard
        backend._translation_locks = self.old_locks
        self.api_key_patch.stop()
        self.client.close()
        backend.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def endpoint(self, segment: int = 0):
        return f"/api/episodes/{self.episode_id}/translations/{segment}"

    def current_fingerprint(self, segment: int = 0) -> str:
        response = self.client.get(f"/api/episodes/{self.episode_id}/transcript")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["transcript"]["segments"][segment]["source_fingerprint"]

    def post_translation(self, segment: int = 0, fingerprint: str | None = None):
        if fingerprint is None:
            fingerprint = self.current_fingerprint(segment)
        return self.client.post(self.endpoint(segment), json={"expected_source_fingerprint": fingerprint})

    def translation_count(self) -> int:
        with backend.db() as conn:
            return conn.execute("SELECT COUNT(*) FROM sentence_translations").fetchone()[0]

    def test_translation_is_cached_for_same_sentence(self) -> None:
        fake_provider = Mock(return_value="架空の英文です。")
        with patch.object(backend, "_gemini_translate_sentence", fake_provider):
            fingerprint = self.current_fingerprint()
            first = self.post_translation(fingerprint=fingerprint)
            second = self.post_translation(fingerprint=fingerprint)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), {"translation": "架空の英文です。", "cached": False, "target_language": "ja", "source_fingerprint": fingerprint})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["translation"], "架空の英文です。")
        self.assertTrue(second.json()["cached"])
        self.assertEqual(second.json()["source_fingerprint"], fingerprint)
        fake_provider.assert_called_once_with("A synthetic sentence.", "test-key-never-real")
        self.assertEqual(self.translation_count(), 1)

    def test_missing_key_returns_configuration_error_without_provider_call(self) -> None:
        fake_provider = Mock()
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}), patch.object(backend, "_gemini_translate_sentence", fake_provider):
            response = self.post_translation()

        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", response.json()["detail"])
        fake_provider.assert_not_called()
        self.assertEqual(self.translation_count(), 0)

    def test_changed_sentence_content_or_timing_gets_new_cache_identity(self) -> None:
        fake_provider = Mock(side_effect=["最初の訳。", "時間変更後の訳。", "文章変更後の訳。"])
        with patch.object(backend, "_gemini_translate_sentence", fake_provider):
            first = self.post_translation()
            with backend.db() as conn:
                conn.execute(
                    "UPDATE episodes SET transcript_json=? WHERE id=?",
                    (json.dumps({"segments": [{"start": 1.25, "end": 2.5, "text": "A synthetic sentence.", "words": []}]}), self.episode_id),
                )
            second = self.post_translation()
            with backend.db() as conn:
                conn.execute(
                    "UPDATE episodes SET transcript_json=? WHERE id=?",
                    (json.dumps({"segments": [{"start": 1.25, "end": 2.5, "text": "The source sentence changed.", "words": []}]}), self.episode_id),
                )
            third = self.post_translation()

        for response in (first, second, third):
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([response.json()["translation"] for response in (first, second, third)], ["最初の訳。", "時間変更後の訳。", "文章変更後の訳。"])
        self.assertEqual(fake_provider.call_count, 3)
        self.assertEqual(self.translation_count(), 3)

    def test_concurrent_duplicate_requests_share_one_provider_call(self) -> None:
        entered_provider = threading.Event()
        release_provider = threading.Event()

        def fake_translate(sentence: str, api_key: str) -> str:
            entered_provider.set()
            if not release_provider.wait(timeout=3):
                raise AssertionError("test did not release mocked provider")
            return "同時実行の訳。"

        with patch.object(backend, "_gemini_translate_sentence", side_effect=fake_translate) as fake_provider:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fingerprint = self.current_fingerprint()
                first = pool.submit(self.post_translation, 0, fingerprint)
                self.assertTrue(entered_provider.wait(timeout=2), "provider should be called")
                second = pool.submit(self.post_translation, 0, fingerprint)
                time.sleep(0.1)
                release_provider.set()
                responses = [first.result(timeout=3), second.result(timeout=3)]

        self.assertTrue(all(response.status_code == 200 for response in responses), [r.text for r in responses])
        self.assertEqual([response.json()["translation"] for response in responses], ["同時実行の訳。"] * 2)
        self.assertEqual(fake_provider.call_count, 1)
        self.assertEqual(self.translation_count(), 1)

    def test_slow_translation_does_not_block_an_unrelated_cached_sentence(self) -> None:
        sentence_a_started = threading.Event()
        release_sentence_a = threading.Event()

        def fake_translate(sentence: str, _api_key: str) -> str:
            if sentence == "A synthetic sentence.":
                sentence_a_started.set()
                if not release_sentence_a.wait(timeout=4):
                    raise AssertionError("test did not release slow translation A")
                return "Sentence A translation."
            if sentence == "Another synthetic sentence.":
                return "Sentence B translation."
            raise AssertionError(f"unexpected source sentence: {sentence}")

        with patch.object(backend, "_gemini_translate_sentence", side_effect=fake_translate) as fake_provider:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fingerprint_a = self.current_fingerprint(0)
                fingerprint_b = self.current_fingerprint(1)
                pending_a = pool.submit(self.post_translation, 0, fingerprint_a)
                self.assertTrue(sentence_a_started.wait(timeout=2), "translation A should reach the mocked provider")
                pending_b = pool.submit(self.post_translation, 1, fingerprint_b)
                try:
                    response_b = pending_b.result(timeout=1.5)
                finally:
                    release_sentence_a.set()
                response_a = pending_a.result(timeout=3)

        self.assertEqual(response_b.status_code, 200, response_b.text)
        self.assertEqual(response_b.json()["translation"], "Sentence B translation.")
        self.assertEqual(response_a.status_code, 200, response_a.text)
        self.assertEqual(fake_provider.call_count, 2)
        self.assertEqual(self.translation_count(), 2)

    def test_provider_error_is_redacted(self) -> None:
        secret_key = "simulated-private-api-key"
        submitted_text = "sensitive synthetic source text"
        with patch("app.urllib.request.urlopen", side_effect=RuntimeError(f"failed for {submitted_text} using {secret_key}")):
            with self.assertRaises(HTTPException) as caught:
                backend._gemini_translate_sentence(submitted_text, secret_key)

        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn(secret_key, caught.exception.detail)
        self.assertNotIn(submitted_text, caught.exception.detail)
        self.assertEqual(caught.exception.detail, "Translation service returned an incomplete or invalid response.")

    def test_provider_http_failures_have_sanitized_status_and_message(self) -> None:
        cases = {
            429: (429, "Translation service quota or rate limit reached. Please try again later."),
            401: (503, "Translation service credentials are invalid or lack access. Contact the server administrator."),
            403: (503, "Translation service credentials are invalid or lack access. Contact the server administrator."),
            500: (503, "Translation service is temporarily unavailable. Please try again."),
            503: (503, "Translation service is temporarily unavailable. Please try again."),
            400: (502, "Translation service rejected the request. Please try again later."),
        }

        for provider_status, expected in cases.items():
            with self.subTest(provider_status=provider_status):
                private_body = b"secret source sentence and simulated-private-api-key"
                provider_error = HTTPError(
                    "https://provider.invalid/private", provider_status, "private error", {}, None
                )
                provider_error.read = Mock(return_value=private_body)
                with patch("app.urllib.request.urlopen", side_effect=provider_error):
                    with self.assertRaises(HTTPException) as caught:
                        backend._gemini_translate_sentence("synthetic sentence", "simulated-private-api-key")

                self.assertEqual((caught.exception.status_code, caught.exception.detail), expected)
                self.assertNotIn("synthetic sentence", caught.exception.detail)
                self.assertNotIn("simulated-private-api-key", caught.exception.detail)
                provider_error.read.assert_not_called()

    def test_network_failures_are_reported_as_temporary_without_details(self) -> None:
        with patch("app.urllib.request.urlopen", side_effect=URLError("private host details")):
            with self.assertRaises(HTTPException) as caught:
                backend._gemini_translate_sentence("synthetic sentence", "simulated-private-api-key")

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail, "Translation service is temporarily unavailable. Please try again.")
        self.assertNotIn("private host details", caught.exception.detail)

    def test_only_complete_and_well_formed_provider_responses_are_accepted(self) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size: int) -> bytes:
                return self.payload

        invalid_payloads = [
            {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]},
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "   "}]}}]},
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": 12}]}}]},
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "x" * 8001}]}}]},
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "one"}]}}, {"finishReason": "STOP"}]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=str(payload)[:60]):
                response = FakeResponse(json.dumps(payload).encode("utf-8"))
                with patch("app.urllib.request.urlopen", return_value=response):
                    with self.assertRaises(HTTPException) as caught:
                        backend._gemini_translate_sentence("synthetic sentence", "simulated-private-api-key")
                self.assertEqual(caught.exception.status_code, 502)
                self.assertEqual(caught.exception.detail, "Translation service returned an incomplete or invalid response.")

        with patch("app.urllib.request.urlopen", return_value=FakeResponse(b"{")):
            with self.assertRaises(HTTPException) as malformed:
                backend._gemini_translate_sentence("synthetic sentence", "simulated-private-api-key")
        self.assertEqual(malformed.exception.status_code, 502)

        oversized = FakeResponse(b" " * (256 * 1024 + 1))
        with patch("app.urllib.request.urlopen", return_value=oversized):
            with self.assertRaises(HTTPException) as too_large:
                backend._gemini_translate_sentence("synthetic sentence", "simulated-private-api-key")
        self.assertEqual(too_large.exception.status_code, 502)

        complete = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": " 完了した訳。 "}]}}]}
        with patch("app.urllib.request.urlopen", return_value=FakeResponse(json.dumps(complete).encode("utf-8"))):
            self.assertEqual(backend._gemini_translate_sentence("synthetic sentence", "simulated-private-api-key"), "完了した訳。")

    def test_provider_failure_is_not_cached(self) -> None:
        with patch.object(backend, "_gemini_translate_sentence", side_effect=HTTPException(429, "rate limited")):
            response = self.post_translation()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.translation_count(), 0)

    def test_stale_source_fingerprint_is_rejected_without_provider_call(self) -> None:
        stale_fingerprint = self.current_fingerprint()
        fake_provider = Mock(side_effect=["Old cached translation.", "Replacement translation."])
        with patch.object(backend, "_gemini_translate_sentence", fake_provider):
            old_translation = self.post_translation(fingerprint=stale_fingerprint)
        with backend.db() as conn:
            conn.execute(
                "UPDATE episodes SET transcript_json=? WHERE id=?",
                (json.dumps({"segments": [{"start": 1.0, "end": 2.5, "text": "Replacement sentence.", "words": []}]}), self.episode_id),
            )
        with patch.object(backend, "_gemini_translate_sentence", fake_provider):
            stale = self.post_translation(fingerprint=stale_fingerprint)
            fresh = self.post_translation()

        self.assertEqual(old_translation.status_code, 200, old_translation.text)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"], "Transcript sentence changed. Refresh the transcript and try again.")
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual(fake_provider.call_count, 2)
        self.assertEqual(fake_provider.call_args_list[-1].args, ("Replacement sentence.", "test-key-never-real"))
        self.assertEqual(self.translation_count(), 2)

    def test_retranscription_during_provider_call_does_not_cache_stale_translation(self) -> None:
        old_fingerprint = self.current_fingerprint()
        provider_started = threading.Event()
        allow_provider_to_finish = threading.Event()

        def slow_provider(_sentence: str, _api_key: str) -> str:
            provider_started.set()
            if not allow_provider_to_finish.wait(timeout=3):
                raise AssertionError("test did not release mocked provider")
            return "Translation of the old transcript."

        with patch.object(backend, "_gemini_translate_sentence", side_effect=slow_provider) as fake_provider:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.post_translation, 0, old_fingerprint)
                self.assertTrue(provider_started.wait(timeout=2), "translation request should reach the mocked provider")
                with backend.db() as conn:
                    conn.execute(
                        "UPDATE episodes SET transcript_json=? WHERE id=?",
                        (json.dumps({"segments": [{"start": 1.0, "end": 2.5, "text": "Transcript replaced during translation.", "words": []}]}), self.episode_id),
                    )
                allow_provider_to_finish.set()
                response = pending.result(timeout=3)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "Transcript sentence changed. Refresh the transcript and try again.")
        fake_provider.assert_called_once_with("A synthetic sentence.", "test-key-never-real")
        self.assertEqual(self.translation_count(), 0)

    def test_missing_or_malformed_source_fingerprint_is_rejected(self) -> None:
        fake_provider = Mock()
        with patch.object(backend, "_gemini_translate_sentence", fake_provider):
            missing = self.client.post(self.endpoint())
            malformed = self.client.post(self.endpoint(), json={"expected_source_fingerprint": "not-a-hash"})

        self.assertEqual(missing.status_code, 422)
        self.assertEqual(malformed.status_code, 422)
        fake_provider.assert_not_called()
        self.assertEqual(self.translation_count(), 0)


if __name__ == "__main__":
    unittest.main()
