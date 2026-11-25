# LinguaCast

Turn an English podcast into a listening lesson. LinguaCast creates a transcript on your own computer, with word-level timing, so you can tap a word or sentence to hear it again.

This 390 × 844 demo was captured in a mobile browser emulator. It uses a fictional show, transcript, and generated audio. [View a still screenshot](docs/mobile-demo.png).

![LinguaCast on a mobile-sized screen: transcript lines follow playback and the current word is highlighted](docs/mobile-demo.gif)

## Listen, read, repeat

- Add a podcast with its **RSS feed URL** and browse its episodes.
- Create an English transcript locally with `faster-whisper` on your CPU. No transcription API key or paid transcription service is required.
- Follow the audio with sentence-by-sentence text and word-level timestamps. Tap a word to seek to it, or replay a sentence.
- Adjust playback from 0.5× to 2.0×. LinguaCast follows the current line as you listen and remembers playback position and speed in your browser.
- Use the responsive web interface on a phone, including an iPhone browser.

## Get started

You need Python 3.10–3.12. From the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> in a browser on the same computer.

The default speech model is `small.en`. On its first use, faster-whisper downloads that model; this requires an internet connection. LinguaCast also needs internet access to fetch the RSS feed and episode audio. Once the model and audio are downloaded, speech recognition runs locally on the CPU.

## Your first episode

1. In LinguaCast, tap the **+** button and paste the podcast's RSS feed URL.
2. Choose a show, then open an episode.
3. Choose **Create transcript**. The episode audio is downloaded and cached, then transcription begins. Progress is shown on the episode screen.
4. When the transcript is ready, press play. Tap any word to jump to that point, or use the repeat control beside a sentence to practice it again.

## Automatic transcripts for new episodes

LinguaCast checks registered RSS feeds for new releases while its server is running. It checks every 15 minutes by default; set `AUTO_REFRESH_INTERVAL_SECONDS` to change the interval. Automatic transcription starts only for episodes published after a feed was registered. Episodes already in the feed at registration are the baseline and are not transcribed automatically, so this does not create a backlog. When upgrading an existing library, the episodes currently in each feed become its baseline. Episodes with a missing, invalid, or older publication date are skipped because LinguaCast cannot reliably determine that they are new.

The server downloads eligible episode audio and transcribes it on the CPU, one episode at a time. The episode list shows whether a transcript is queued, downloading, in progress, ready, or failed. A failed episode can be retried from its player screen with **Create transcript**. Keep the LinguaCast server running for feed checks and automatic transcription; opening the app in a browser is not required. Automatic jobs that were queued or in progress when the server stopped resume after restart. Interrupted manual jobs are marked failed and can be retried from the episode screen.

An Apple Podcasts **share link** points to an episode page and is not the RSS feed URL LinguaCast expects. Look for the podcast's RSS link on its website or in the podcast provider's settings. Some providers give subscribers a private RSS URL; treat it like a password because it may contain an access token.

## Use it from an iPhone

LinguaCast is a web app, so there is no iOS app to install. To use it on an iPhone, open its address in Safari while the phone can securely reach the computer running LinguaCast—for example, through your own private VPN or an authenticated private proxy. The default server command above listens only on that computer (`127.0.0.1`), so it will not be reachable from your phone as-is.

LinguaCast has **no login or access control**. Do not expose it directly to your local network or the public internet. Anyone who can reach an exposed instance could see registered feed URLs and cached audio. Configure and secure any remote access separately.

## Models and speed

The English-only models trade transcription speed for accuracy:

| Setting | Use when |
| --- | --- |
| `tiny.en` | You want the quickest, lightest option |
| `base.en` | You want a middle ground |
| `small.en` (default) | You want the default accuracy/speed balance |

Set the model before starting LinguaCast, for example:

```sh
WHISPER_MODEL=base.en uvicorn app:app --host 127.0.0.1 --port 8000
```

`WHISPER_CPU_THREADS` can set the number of CPU threads. Transcription time depends on your processor, model, audio, and other workloads; long episodes can take a while. LinguaCast processes one transcription at a time.

## Optional Japanese sentence translations

When a transcript is ready, tap **Show Japanese** under a sentence to request its Japanese translation. Translations are hidden by default. The first request for a sentence is sent to Google's Gemini API using `gemini-4-hydrogen`; later requests for the same transcript sentence use LinguaCast's local cache. Cached translations are hidden again when the episode is reopened. If the transcript sentence changes, its old translation is not reused. If the feature is not configured, LinguaCast displays an informational message. Provider failures are shown as generic retry guidance; API keys and raw provider error responses are not sent to the browser.

To enable translations, create a Gemini API key in [Google AI Studio](https://aistudio.google.com/app/apikey), then set it in the environment used to start the LinguaCast server:

```sh
export GEMINI_API_KEY="your-api-key"
uvicorn app:app --host 127.0.0.1 --port 8000
```

Keep the key on the server: do not put it in browser code, commit it, or share it. Set it through your environment or a secret manager before starting the app. The app does not need a Gemini API key for local speech transcription or podcast playback.

**Privacy and cost:** Translation is on demand. When you tap a sentence for the first time, LinguaCast sends that transcript sentence to Google's Gemini API over the internet for translation. It does not send the episode audio or the entire transcript for this request. The sentence and returned translation are cached on the LinguaCast server; the browser also keeps a local copy so it can stay hidden and be shown again without another API request. Do not use this feature for text you do not want to send to Google. Google's Gemini API [pricing and data-use terms](https://ai.google.dev/gemini-api/docs/pricing) can change: the free tier has usage limits and may use submitted content to improve Google products, while paid API usage is billed by token and is not used for product improvement under the current terms. Check the current pricing and terms before use. A Gemini app or Google AI plan subscription is separate from Gemini API access and does not pay for this app's API requests; API usage is associated with the Google AI Studio project and its billing settings. See Google's [API setup guide](https://ai.google.dev/gemini-api/docs/get-started) for key and billing details.

## Data and limitations

- Feed details, episode records, transcripts, and downloaded episode audio are stored locally in `data/` by default. Change the location with `PODCAST_DATA_DIR=/path/to/data`.
- The downloaded speech model is cached by faster-whisper in the user's Hugging Face cache, separately from `data/`.
- Playback position and speed are saved in the current browser's local storage. They are not synced between devices.
- Automatic RSS checks and transcription run only while the LinguaCast server is running. The default check interval is 15 minutes and can be changed with `AUTO_REFRESH_INTERVAL_SECONDS`.
- Transcription is English-focused and may make mistakes, especially with noisy audio, accents, music, or specialized vocabulary. Word timing and sentence boundaries are generated automatically and may need interpretation.
- LinguaCast reads RSS/Atom feeds that include HTTP(S) audio enclosures. It does not extract an RSS feed URL from an Apple Podcasts share link.

## Licensing

This repository does not currently include a `LICENSE` file, so no license for LinguaCast itself is stated here. The application, its dependencies, and the selected speech model have separate licensing terms. Check each upstream project's license and model terms before redistribution or commercial use. You are also responsible for following the podcast provider's terms when accessing or using its feed and audio.
