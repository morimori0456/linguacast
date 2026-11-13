# Player acceptance test

This Playwright test drives the app in a 390 × 844 mobile viewport. It stubs the app API and creates a short WAV in memory, so it does not contact podcast feeds, download model weights, or transcribe real audio.

## Setup

Use Python 3.10 or newer, install Playwright, and install its standard Chromium build:

```sh
python -m pip install playwright
python -m playwright install chromium
```

On Linux systems missing Chromium runtime libraries, install those system dependencies with:

```sh
python -m playwright install --with-deps chromium
```

If Chromium or Chrome is already installed at a known path, set `CHROMIUM_PATH` instead; this bypasses Playwright's bundled browser download:

```sh
CHROMIUM_PATH=/usr/bin/chromium python -m unittest discover -s tests -v
```

## Run

From the project root:

```sh
python -m unittest discover -s tests -v
```

The test checks playback-rate persistence across episodes, seek-slider value retention during a drag and seek commit, sentence-loop behavior across its end boundary, and the ready, missing, and queued transcript states.
