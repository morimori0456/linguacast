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
                        "words": [
                            {"start": 1.1, "end": 1.42, "word": "Another"},
                            {"start": 1.43, "end": 1.8, "word": "sentence."},
                        ],
                    },
                ]
            },
        }
        transcribe_started = threading.Event()

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
                route.fulfill(json=transcript)
            elif path == "/api/episodes/102/transcript":
                route.fulfill(json={"status": "none", "transcript": None})
            elif path == "/api/episodes/102/transcribe" and method == "POST":
                transcribe_started.set()
                route.fulfill(status=202, json={"job_id": "acceptance-job", "status": "queued"})
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
