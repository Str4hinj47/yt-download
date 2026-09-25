#!/usr/bin/env python3
"""
cli.py — download a YouTube video from the command line (custom engine, no yt-dlp).

Examples:
  python3 cli.py "https://www.youtube.com/watch?v=XXXX"
  python3 cli.py URL -q 720            # cap resolution at 720p
  python3 cli.py URL -q audio          # extract audio as MP3
  python3 cli.py URL -o ~/Videos --proxy http://user:pass@host:port
"""

import argparse
import sys
import threading
from pathlib import Path

from engine import innertube as yt


def fmt_dur(s):
    if not s:
        return "--:--"
    s = int(s)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def main():
    p = argparse.ArgumentParser(description="Download a YouTube video (custom Innertube engine).")
    p.add_argument("url", help="YouTube link or 11-char video id")
    p.add_argument("-q", "--quality", default="best",
                   help="best | max height in px (2160, 1080, 720, …) | audio")
    p.add_argument("-o", "--output", default=".", help="output directory")
    p.add_argument("--proxy", default=None, help="HTTP(S) proxy URL for stubborn IPs")
    p.add_argument("-F", "--list-formats", action="store_true",
                   help="show available streams and exit")
    args = p.parse_args()

    q = args.quality.strip().lower()
    if q not in ("best", "audio") and not q.isdigit():
        p.error("-q must be 'best', 'audio', or a number like 1080")

    try:
        info = yt.get_info(args.url, args.proxy)
    except yt.YouTubeError as e:
        print(f"  Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  {info['title']}")
    print(f"  {info['channel']} · {fmt_dur(info['duration'])}")

    if args.list_formats:
        print(f"\n  {'itag':>5}  {'kind':<10} {'res':<9} {'codec':<8} {'fps':>3}  size")
        for f in sorted(info["formats"], key=lambda f: (f["audio_only"], -(f["height"] or 0))):
            kind = "audio" if f["audio_only"] else ("video" if f["video_only"] else "video+aud")
            print(f"  {f['itag']:>5}  {kind:<10} {f['quality_label'] or '-':<9} "
                  f"{f['codec']:<8} {f['fps'] if not f['audio_only'] else '':>3}  "
                  f"{yt._human(f['size']) or '?'}")
        return

    lock = threading.Lock()
    state = {"done": 0, "total": None}

    def on_progress(done, total, speed):
        with lock:
            state.update(done=done, total=total)
            pct = done / total * 100 if total else 0
            width = 30
            bar = "#" * int(width * pct / 100) + "-" * (width - int(width * pct / 100))
            spd = f"{speed/1048576:.1f}MB/s" if speed else "--"
            eta = fmt_dur((total - done) / speed) if (speed and total) else "--:--"
            sys.stdout.write(f"\r  [{bar}] {pct:5.1f}%  {spd}  ETA {eta}   ")
            sys.stdout.flush()

    def on_phase(phase):
        if phase != "Downloading…":
            sys.stdout.write("\r" + " " * 70 + f"\r  {phase}\n")
            sys.stdout.flush()

    try:
        out = yt.download(args.url, q, Path(args.output),
                          on_progress=on_progress, on_phase=on_phase, proxy=args.proxy)
        sys.stdout.write("\r" + " " * 70 + "\r")
        print(f"  Saved: {out}")
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        sys.exit(130)
    except yt.YouTubeError as e:
        print(f"\n  Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
