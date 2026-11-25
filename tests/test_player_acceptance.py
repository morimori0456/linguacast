"""Browser acceptance checks for the podcast player's core listening flow.

Run with:
  python -m unittest discover -s tests -v

Install Playwright's Chromium with `python -m playwright install chromium`, or
set CHROMIUM_PATH to an existing Chromium/Chrome executable.

The test serves the checked-out frontend locally, stubs only API responses, and
uses a generated short WAV. It never calls a real podcast or transcription API.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import threading
import unittest
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

APP_ROOT = Path(__file__).resolve().parents[1]


def make_test_audio(seconds: float = 7.0, sample_rate: int = 16_000) -> bytes:
    """Create a small tone WAV that Chromium can seek and play deterministically."""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        samples = bytearray()
        for i in range(int(seconds * sample_rate)):
            sample = int(3000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            samples.extend(sample.to_bytes(2, "little", signed=True))
        wav.writeframes(samples)
    return output.getvalue()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass


class PlayerAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            lambda *args, **kwargs: QuietHandler(*args, directory=str(APP_ROOT), **kwargs),
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.audio_bytes = make_test_audio()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

    def test_player_controls_and_transcript_states(self) -> None:
        feed = {"id": 10, "title": "Acceptance Test Podcast", "image_url": ""}
        episodes = [
            {
                "id": 101,
                "feed_id": 10,
                "title": "Episode One",
                "description": "Ready transcript and repeat test",
                "duration": "00:00:07",
                "audio_path": "/api/episodes/101/audio",
                "has_transcript": True,
            },
            {
                "id": 102,
                "feed_id": 10,
                "title": "Episode Two",
                "description": "Transcript not created yet",
                "duration": "00:00:07",
                "audio_path": "/api/episodes/102/audio",
                "has_transcript": False,
            },
        ]
        transcript = {
            "status": "ready",
            "transcript": {
                "segments": [
                    {
                        "start": 0.2,
                        "end": 0.85,
                        "text": "A short sentence.",
                        "source_fingerprint": "a" * 64,
                        "words": [
                            {"start": 0.2, "end": 0.38, "word": "A"},
                            {"start": 0.39, "end": 0.58, "word": "short"},
                            {"start": 0.59, "end": 0.85, "word": "sentence."},
                        ],
                    },
                    {
                        "start": 1.1,
                        "end": 1.8,
                        "text": "Another sentence.",
                        "source_fingerprint": "b" * 64,
                        "words": [
                            {"start": 1.1, "end": 1.42, "word": "Another"},
                            {"start": 1.43, "end": 1.8, "word": "sentence."},
                        ],
                    },
                ]
            },
        }
        transcribe_started = threading.Event()
        translation_requests = []
        transcript_requests = []

        def fulfill_api(route) -> None:
            request = route.request
            path = request.url.split(self.base_url, 1)[-1].split("?", 1)[0]
            method = request.method
            if path == "/api/feeds" and method == "GET":
                route.fulfill(json=[feed])
            elif path == "/api/feeds/10/refresh":
                route.fulfill(json={"episode_count": len(episodes)})
            elif path == "/api/feeds/10/episodes":
                route.fulfill(json=episodes)
            elif path in ("/api/episodes/101", "/api/episodes/102"):
                episode_id = int(path.rsplit("/", 1)[-1])
                route.fulfill(json=next(ep for ep in episodes if ep["id"] == episode_id))
            elif path == "/api/episodes/101/transcript":
                transcript_requests.append(path)
                route.fulfill(json=transcript)
            elif path == "/api/episodes/102/transcript":
                route.fulfill(json={"status": "none", "transcript": None})
            elif path == "/api/episodes/102/transcribe" and method == "POST":
                transcribe_started.set()
                route.fulfill(status=202, json={"job_id": "acceptance-job", "status": "queued"})
            elif path == "/api/episodes/101/translations/0" and method == "POST":
                translation_requests.append(path)
                self.assertEqual(request.post_data_json, {"expected_source_fingerprint": "a" * 64})
                if len(translation_requests) == 1:
                    route.fulfill(json={"translation": "架空の短い文です。", "expressions": [], "expressions_ready": False, "explanation_error": "Translation is unavailable.", "cached": True, "target_language": "ja", "source_fingerprint": "a" * 64})
                else:
                    route.fulfill(json={"translation": "架空の短い文です。", "expressions": [{"phrase": "short sentence", "explanation": "短い文という意味です。<img src=x onerror=alert(1)>"}], "expressions_ready": True, "cached": True, "target_language": "ja", "source_fingerprint": "a" * 64})
            elif path == "/api/episodes/101/translations/1" and method == "POST":
                translation_requests.append(path)
                self.assertEqual(request.post_data_json, {"expected_source_fingerprint": "b" * 64})
                route.fulfill(status=409, json={"detail": "Transcript sentence changed. Refresh the transcript and try again."})
            elif path.startswith("/api/episodes/") and path.endswith("/audio"):
                route.fulfill(
                    status=200,
                    body=self.audio_bytes,
                    content_type="audio/wav",
                    headers={"Accept-Ranges": "bytes"},
                )
            else:
                route.fulfill(status=404, json={"detail": f"Unexpected API request: {method} {path}"})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH") or None,
                headless=True,
                args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"],
            )
            page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            page.set_default_timeout(5000)
            page.add_init_script("""() => localStorage.setItem('linguacast-translations-101', JSON.stringify({
              0: {source_fingerprint: 'a'.repeat(64), translation: 'Legacy cached translation.', expressions: [null]}
            }))""")
            page.route("**/api/**", fulfill_api)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(self.base_url, wait_until="domcontentloaded")
                page.locator('[data-feed-id="10"]').click()
                page.locator('[data-episode-id="101"]').click()
                page.locator(".transcript-line").first.wait_for()
                page.wait_for_function("document.querySelector('#audio').readyState >= 1")
                self.assertEqual("Transcript ready", page.locator("#transcript-status").inner_text())

                # Pick a non-default rate and confirm it belongs to the player,
                # then survives changing episodes.
                page.locator("#speed-button").click()
                page.locator('.speed-option[data-speed="1.5"]').click()
                self.assertEqual(page.locator("#speed-button").inner_text(), "1.5×")
                self.assertAlmostEqual(page.locator("#audio").evaluate("audio => audio.playbackRate"), 1.5)

                # Seek while paused. The slider should settle at the requested
                # position after the media element processes the seek event.
                seek = page.locator("#seek")
                seek.dispatch_event("pointerdown", {"pointerId": 1, "pointerType": "mouse", "isPrimary": True})
                seek.evaluate(
                    "el => { el.value = '600'; el.dispatchEvent(new Event('input', {bubbles: true})); document.querySelector('#audio').dispatchEvent(new Event('timeupdate')); }"
                )
                self.assertEqual(seek.input_value(), "600", "timeupdate must not pull the thumb away during drag")
                seek.dispatch_event("change")
                page.wait_for_function("Math.abs(Number(document.querySelector('#seek').value) - 600) < 20")
                self.assertAlmostEqual(page.locator("#audio").evaluate("audio => audio.currentTime"), 4.2, delta=0.25)

                # Japanese is hidden until requested. Toggling it must not
                # trigger word seeking or otherwise move the audio position.
                translation_toggle = page.locator('.translation-toggle[data-translation-line="0"]')
                translation_panel = page.locator('#translation-0')
                self.assertFalse(translation_panel.is_visible())
                page.set_viewport_size({"width": 320, "height": 740})
                narrow_english = page.locator('.transcript-line[data-line="0"] .line-text').bounding_box()
                self.assertGreater(narrow_english["width"], 210, "English should remain readable on narrow phones")
                self.assertLessEqual(page.locator("body").evaluate("el => el.scrollWidth"), 320, "transcript should not cause horizontal page overflow")
                self.assertTrue(page.get_by_role("button", name="Show Japanese").first.is_visible(), "translation control should have an accessible name")
                page.set_viewport_size({"width": 390, "height": 844})
                english_box = page.locator('.transcript-line[data-line="0"] .line-text').bounding_box()
                toggle_box = translation_toggle.bounding_box()
                repeat_button = page.locator('.line-loop[data-loop-line="0"]')
                repeat_box_before = repeat_button.bounding_box()
                self.assertGreater(english_box["width"], 280, "English should use the transcript width on iPhone")
                self.assertGreaterEqual(toggle_box["height"], 44, "translation control should be easy to tap")
                time_before_translation = page.locator("#audio").evaluate("audio => audio.currentTime")
                translation_toggle.click()
                page.get_by_text("架空の短い文です。", exact=True).wait_for()
                self.assertTrue(translation_panel.is_visible())
                translation_box = translation_panel.bounding_box()
                repeat_box_after = repeat_button.bounding_box()
                self.assertAlmostEqual(translation_box["x"], english_box["x"], delta=1, msg="Japanese should align with English")
                self.assertGreater(translation_box["width"], 280, "Japanese should use the transcript width on iPhone")
                self.assertAlmostEqual(repeat_box_after["y"], repeat_box_before["y"], delta=1, msg="sentence action row should stay in place")
                self.assertEqual(translation_toggle.get_attribute("aria-expanded"), "true")
                self.assertAlmostEqual(page.locator("#audio").evaluate("audio => audio.currentTime"), time_before_translation, delta=0.05)
                page.get_by_text("Translation is unavailable.", exact=True).wait_for()
                translation_toggle.click()
                self.assertFalse(translation_panel.is_visible())
                self.assertEqual(translation_toggle.get_attribute("aria-expanded"), "false")
                self.assertEqual(len(translation_requests), 1, "the translation is requested only on first reveal")
                translation_toggle.click()
                self.assertTrue(translation_panel.is_visible(), "a saved translation remains showable after note generation fails")
                self.assertEqual(len(translation_requests), 1, "showing a saved translation does not retry expression generation")
                page.locator("#translation-0 [data-translation-retry]").click()
                page.locator("#translation-0 .expression-note strong").wait_for()
                self.assertEqual(len(translation_requests), 2, "expression notes have an explicit retry action")
                self.assertEqual(page.locator("#translation-0 .expression-note strong").inner_text(), "short sentence")
                self.assertEqual(page.locator("#translation-0 .expression-note img").count(), 0, "model output must be escaped, not inserted as markup")
                self.assertIn("<img src=x onerror=alert(1)>", page.locator("#translation-0 .expression-note span").inner_text())
                translation_toggle.click()
                self.assertFalse(translation_panel.is_visible())
                self.assertAlmostEqual(page.locator("#audio").evaluate("audio => audio.currentTime"), time_before_translation, delta=0.05)

                # A 409 offers transcript refresh. The refresh rerenders the
                # transcript and collapses an earlier translation; its first
                # click afterward must reopen the cached Japanese text.
                translation_toggle.click()
                self.assertTrue(translation_panel.is_visible())
                second_line_toggle = page.locator('.translation-toggle[data-translation-line="1"]')
                second_line_toggle.click()
                page.get_by_text("This transcript sentence changed. Refresh the transcript before translating it.", exact=True).wait_for()
                self.assertEqual(second_line_toggle.inner_text(), "Refresh transcript")
                second_line_toggle.click()
                page.wait_for_function("document.querySelectorAll('#transcript-lines .transcript-line').length === 2")
                self.assertEqual(len(transcript_requests), 2, "refresh action should reload transcript data")
                page.get_by_text("Transcript refreshed", exact=True).wait_for()
                self.assertFalse(translation_panel.is_visible(), "refresh collapses the previously open translation")
                self.assertEqual(translation_toggle.inner_text(), "Show Japanese")
                translation_toggle.click()
                self.assertTrue(translation_panel.is_visible(), "the first post-refresh click should expand the cached translation")
                self.assertEqual(translation_toggle.inner_text(), "Hide Japanese")
                self.assertEqual(translation_requests.count("/api/episodes/101/translations/0"), 2, "reopening cached translation should not POST again")

                page.locator("#player-back").click()
                page.locator('[data-feed-id="10"]').click()
                page.locator('[data-episode-id="101"]').click()
                page.locator(".transcript-line").first.wait_for()
                page.locator('.translation-toggle[data-translation-line="0"]').click()
                self.assertEqual(page.locator("#translation-0 .expression-note strong").inner_text(), "short sentence", "saved expression notes should survive reopening the episode")
                self.assertEqual(translation_requests.count("/api/episodes/101/translations/0"), 2, "reopening must reuse saved translation notes")

                # A sentence loop must wrap to the selected sentence boundary
                # and remain active across the boundary.
                page.locator('.line-loop[data-loop-line="0"]').click()
                self.assertTrue(page.locator("#repeat-button").get_attribute("class").find("active") >= 0)
                page.wait_for_timeout(1200)
                current_time = page.locator("#audio").evaluate("audio => audio.currentTime")
                self.assertLess(current_time, 1.1, "audio should wrap before leaving the selected sentence")
                self.assertFalse(page.locator("#audio").evaluate("audio => audio.paused"))

                # Switching episodes clears old sentence-loop state, keeps the
                # selected speed, and surfaces both missing and queued states.
                page.locator("#player-back").click()
                page.locator('[data-feed-id="10"]').click()
                page.locator('[data-episode-id="102"]').click()
                page.wait_for_function("document.querySelector('#transcribe-button').hidden === false")
                self.assertEqual("Not created", page.locator("#transcript-status").inner_text())
                self.assertEqual(page.locator("#speed-button").inner_text(), "1.5×")
                self.assertFalse(page.locator("#repeat-button").get_attribute("class").find("active") >= 0)
                page.locator("#transcribe-button").click()
                page.wait_for_function("document.querySelector('#transcript-status').textContent.includes('Transcribing')")
                self.assertTrue(transcribe_started.wait(timeout=2))
                self.assertIn("Transcribing", page.locator("#transcript-notice").inner_text())
                self.assertEqual(errors, [], f"Unexpected browser errors: {errors}")
            finally:
                browser.close()

    def test_delayed_transcript_from_previous_episode_is_ignored(self) -> None:
        asyncio.run(self._exercise_delayed_transcript_race())

    def test_translation_button_ignores_duplicate_taps_while_pending(self) -> None:
        asyncio.run(self._exercise_translation_pending_click())

    async def _exercise_translation_pending_click(self) -> None:
        feed = {"id": 40, "title": "Pending Translation Test"}
        episode = {"id": 401, "feed_id": 40, "title": "Pending translation", "audio_path": "/api/episodes/401/audio"}
        fingerprint = "c" * 64
        transcript = {"status": "ready", "transcript": {"segments": [{
            "start": 0.2, "end": 0.85, "text": "One pending sentence.", "source_fingerprint": fingerprint,
            "words": [{"start": 0.2, "end": 0.85, "word": "One pending sentence."}],
        }]}}
        request_started = asyncio.Event()
        release_response = asyncio.Event()
        translation_requests: list[str] = []
        errors: list[str] = []

        async def fulfill_api(route) -> None:
            path = route.request.url.split(self.base_url, 1)[-1].split("?", 1)[0]
            method = route.request.method
            if path == "/api/feeds" and method == "GET":
                await route.fulfill(json=[feed])
            elif path == "/api/feeds/40/refresh":
                await route.fulfill(json={"episode_count": 1})
            elif path == "/api/feeds/40/episodes":
                await route.fulfill(json=[episode])
            elif path == "/api/episodes/401":
                await route.fulfill(json=episode)
            elif path == "/api/episodes/401/transcript":
                await route.fulfill(json=transcript)
            elif path == "/api/episodes/401/translations/0" and method == "POST":
                translation_requests.append(path)
                self.assertEqual(route.request.post_data_json, {"expected_source_fingerprint": fingerprint})
                request_started.set()
                await release_response.wait()
                await route.fulfill(json={"translation": "待機中のテスト訳です。", "expressions": [], "expressions_ready": True, "cached": False, "target_language": "ja", "source_fingerprint": fingerprint})
            elif path.endswith("/audio"):
                await route.fulfill(body=self.audio_bytes, content_type="audio/wav")
            else:
                await route.fulfill(status=404, json={"detail": f"Unexpected API request: {method} {path}"})

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH") or None,
                headless=True,
                args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"],
            )
            page = await browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            page.set_default_timeout(5000)
            await page.route("**/api/**", fulfill_api)
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                await page.goto(self.base_url, wait_until="domcontentloaded")
                await page.locator('[data-feed-id="40"]').click()
                await page.locator('[data-episode-id="401"]').click()
                toggle = page.locator('.translation-toggle[data-translation-line="0"]')
                await toggle.click()
                await asyncio.wait_for(request_started.wait(), timeout=3)
                self.assertTrue(await toggle.is_disabled())
                await toggle.evaluate("button => button.dispatchEvent(new MouseEvent('click', { bubbles: true }))")
                await page.wait_for_timeout(100)
                self.assertEqual(translation_requests, ["/api/episodes/401/translations/0"], "a second tap during the pending request must not start another translation")
                release_response.set()
                await page.get_by_text("待機中のテスト訳です。", exact=True).wait_for()
                self.assertEqual(errors, [], f"Unexpected browser errors: {errors}")
            finally:
                release_response.set()
                await browser.close()

    async def _exercise_delayed_transcript_race(self) -> None:
        feed = {"id": 20, "title": "Race Test Podcast"}
        episodes = [
            {"id": 201, "feed_id": 20, "title": "Episode A", "audio_path": "/api/episodes/201/audio"},
            {"id": 202, "feed_id": 20, "title": "Episode B", "audio_path": "/api/episodes/202/audio"},
        ]
        transcript_a = {"status": "ready", "transcript": {"segments": [{"start": 0, "end": 1, "text": "A_ONLY_TRANSCRIPT", "words": [{"start": 0, "end": 1, "word": "A_ONLY_TRANSCRIPT"}]}]}}
        transcript_b = {"status": "ready", "transcript": {"segments": [{"start": 0, "end": 1, "text": "B_ONLY_TRANSCRIPT", "words": [{"start": 0, "end": 1, "word": "B_ONLY_TRANSCRIPT"}]}]}}
        transcript_a_requested = asyncio.Event()
        release_transcript_a = asyncio.Event()

        async def fulfill_api(route) -> None:
            path = route.request.url.split(self.base_url, 1)[-1].split("?", 1)[0]
            method = route.request.method
            if path == "/api/feeds" and method == "GET":
                await route.fulfill(json=[feed])
            elif path == "/api/feeds/20/refresh":
                await route.fulfill(json={"episode_count": 2})
            elif path == "/api/feeds/20/episodes":
                await route.fulfill(json=episodes)
            elif path in ("/api/episodes/201", "/api/episodes/202"):
                episode_id = int(path.rsplit("/", 1)[-1])
                await route.fulfill(json=next(ep for ep in episodes if ep["id"] == episode_id))
            elif path == "/api/episodes/201/transcript":
                transcript_a_requested.set()
                await release_transcript_a.wait()
                await route.fulfill(json=transcript_a)
            elif path == "/api/episodes/202/transcript":
                await route.fulfill(json=transcript_b)
            elif path.startswith("/api/episodes/") and path.endswith("/audio"):
                await route.fulfill(body=self.audio_bytes, content_type="audio/wav")
            else:
                await route.fulfill(status=404, json={"detail": f"Unexpected API request: {method} {path}"})

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH") or None,
                headless=True,
                args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"],
            )
            page = await browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            page.set_default_timeout(5000)
            await page.route("**/api/**", fulfill_api)
            try:
                await page.goto(self.base_url, wait_until="domcontentloaded")
                await page.locator('[data-feed-id="20"]').click()
                await page.locator('[data-episode-id="201"]').click()
                await asyncio.wait_for(transcript_a_requested.wait(), timeout=3)

                # A is already the visible player, but its transcript response
                # is held. Switch away and load B before releasing A's response.
                await page.locator("#player-back").click()
                await page.locator('[data-feed-id="20"]').click()
                await page.locator('[data-episode-id="202"]').click()
                await page.get_by_text("B_ONLY_TRANSCRIPT", exact=True).wait_for()
                release_transcript_a.set()
                await page.wait_for_timeout(150)

                visible_text = await page.locator("#transcript-lines").inner_text()
                self.assertIn("B_ONLY_TRANSCRIPT", visible_text)
                self.assertNotIn("A_ONLY_TRANSCRIPT", visible_text, "stale transcript A must not replace episode B")
                self.assertEqual(await page.locator("#player-title").inner_text(), "Episode B")
            finally:
                release_transcript_a.set()
                await browser.close()

    def test_pagehide_before_metadata_does_not_overwrite_saved_position(self) -> None:
        asyncio.run(self._exercise_pagehide_before_metadata())

    async def _exercise_pagehide_before_metadata(self) -> None:
        feed = {"id": 30, "title": "Position Test Podcast"}
        episode = {"id": 301, "feed_id": 30, "title": "Resume Episode", "audio_path": "/api/episodes/301/audio"}
        metadata_requested = asyncio.Event()
        release_metadata = asyncio.Event()

        async def fulfill_api(route) -> None:
            path = route.request.url.split(self.base_url, 1)[-1].split("?", 1)[0]
            method = route.request.method
            if path == "/api/feeds" and method == "GET":
                await route.fulfill(json=[feed])
            elif path == "/api/feeds/30/refresh":
                await route.fulfill(json={"episode_count": 1})
            elif path == "/api/feeds/30/episodes":
                await route.fulfill(json=[episode])
            elif path == "/api/episodes/301":
                await route.fulfill(json=episode)
            elif path == "/api/episodes/301/transcript":
                await route.fulfill(json={"status": "none", "transcript": None})
            elif path == "/api/episodes/301/audio":
                metadata_requested.set()
                await release_metadata.wait()
                await route.fulfill(body=self.audio_bytes, content_type="audio/wav")
            else:
                await route.fulfill(status=404, json={"detail": f"Unexpected API request: {method} {path}"})

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH") or None,
                headless=True,
                args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"],
            )
            page = await browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            page.set_default_timeout(5000)
            await page.add_init_script("localStorage.setItem('linguacast-position-301', '4.25')")
            await page.route("**/api/**", fulfill_api)
            try:
                await page.goto(self.base_url, wait_until="domcontentloaded")
                await page.locator('[data-feed-id="30"]').click()
                await page.locator('[data-episode-id="301"]').click()
                await asyncio.wait_for(metadata_requested.wait(), timeout=3)

                self.assertEqual(await page.evaluate("localStorage.getItem('linguacast-position-301')"), "4.25")
                self.assertEqual(await page.locator("#audio").evaluate("audio => audio.readyState"), 0)
                # Simulate the browser backgrounding/leaving while audio metadata
                # is still unavailable; pagehide must not persist the initial 0s.
                await page.evaluate("window.dispatchEvent(new Event('pagehide'))")
                self.assertEqual(await page.evaluate("localStorage.getItem('linguacast-position-301')"), "4.25")

                release_metadata.set()
                await page.wait_for_function("document.querySelector('#audio').readyState >= 1")
                await page.wait_for_function("state.positionReady === true")
                self.assertEqual(await page.evaluate("localStorage.getItem('linguacast-position-301')"), "4.25")
            finally:
                release_metadata.set()
                await browser.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
