# YouTube Downloader

A self-hosted YouTube downloader with a **custom engine** — no yt-dlp, no youtube-dl,
no page scraping. It talks directly to YouTube's Innertube player API the way the
official Android/iOS apps do, downloads the streams over plain HTTPS, and muxes
with ffmpeg. Ships as a **web app** plus a **command-line tool**.

```
youtube-downloader/
├── app.py               # Flask web app (UI + job API)
├── cli.py               # command-line downloader
├── engine/
│   ├── innertube.py     # the download engine (single self-contained module)
│   └── probe.py         # diagnostic: which YouTube client contexts work right now
├── templates/index.html # the web interface
└── downloads/           # fetched files land here (created at runtime)
```

## Why not yt-dlp?

yt-dlp asks YouTube's API as the *web browser* client. YouTube now answers that
client from server/datacenter IPs with **“Sign in to confirm you're not a bot”** —
which is exactly the error this replaces. The same API asked as the **Android or
iOS app client** returns full stream URLs from the very same IP. This engine uses
those mobile client contexts, plus:

- **parallel ranged downloads** (4 workers per stream, resumable),
- **CDN-edge failover**: stream URLs pin a Google edge server; if an edge answers
  403, the engine silently re-asks the API for a fresh URL and resumes,
- **codec-aware picking**: H.264 → AV1 → VP9 preference, AAC audio, MP4 output
  whenever the codec combo allows it (WebM/MKV otherwise),
- **MP3 extraction** via ffmpeg.

## Requirements

- Python 3.10+ with `requests` and (for the web app) `flask`
- **ffmpeg** on PATH (merging video+audio, MP3 conversion)
  - Debian/Ubuntu: `sudo apt install ffmpeg` · macOS: `brew install ffmpeg` · Windows: `winget install ffmpeg`

## Web app

```bash
pip install requests flask
python3 app.py          # → http://localhost:8000
```

Paste a link (`watch?v=…`, `youtu.be/…`, Shorts, embeds, live-page URLs, or a bare
11-char id) → **Fetch** shows title, thumbnail, duration and every quality from
2160p down to MP3 with size estimates → **Download** shows live progress →
**Save file** stores it on your device. Finished files auto-delete after 2 hours.

## CLI

```bash
python3 cli.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"          # best quality
python3 cli.py URL -q 1080                                            # cap at 1080p
python3 cli.py URL -q audio                                           # MP3
python3 cli.py URL -o ~/Videos
python3 cli.py URL -F                                                 # list streams
python3 cli.py URL --proxy http://user:pass@host:port                 # stubborn IPs
```

## Using the engine in your own project

`engine/innertube.py` is one dependency-light module (`requests` + ffmpeg subprocess):

```python
from engine import innertube as yt

info = yt.get_info("https://www.youtube.com/watch?v=...")   # metadata + formats
print(yt.quality_options(info))                             # picker entries
path = yt.download(url, "1080", "/tmp/out",
                   on_progress=lambda done, total, speed: ...,
                   on_phase=lambda phase: ...)              # -> pathlib.Path
```

If your website currently wraps yt-dlp and hits the bot check, swapping in this
module is the fix — the difference is the client context, not the library.

## Troubleshooting

| Symptom | Meaning / fix |
| --- | --- |
| “YouTube demands a login…” | Age-restricted video or a hard IP block. Route through a residential proxy: `YT_DOWNLOADER_PROXY=… python3 app.py` or `cli.py --proxy …` |
| “CDN keeps refusing this stream (HTTP 403)” | Every edge refused; rare and usually transient. Retry in a minute, or use a proxy. |
| Merging/MP3 fails | ffmpeg missing or not on PATH. |
| “That doesn't look like a YouTube video link” | Only YouTube URLs/ids are supported by design. |
| Want to see what YouTube accepts right now | `python3 engine/probe.py <video_id>` prints per-client playability. |
| A new YouTube change breaks something | Only the client-context dict at the top of `engine/innertube.py` usually needs a version bump. |

## Please use it responsibly

Download only content you own, freely licensed content (e.g. Creative Commons),
or where downloading is explicitly permitted. Bulk-downloading copyrighted
material violates YouTube's Terms of Service and, in many places, the law.
