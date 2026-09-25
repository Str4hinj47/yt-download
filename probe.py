#!/usr/bin/env python3
"""Probe YouTube's Innertube player API directly (no yt-dlp) to see which
client contexts return playable streams from this IP."""
import json
import sys

import requests

VIDEO = sys.argv[1] if len(sys.argv) > 1 else "jNQXAC9IVRw"
API = "https://www.youtube.com/youtubei/v1/player"

CLIENTS = {
    "android": {
        "clientName": "ANDROID", "clientVersion": "20.10.38",
        "androidSdkVersion": 33, "platform": "MOBILE",
        "userAgent": "com.google.android.youtube/20.10.38 (Linux; U; Android 13; en_US) gzip",
    },
    "android_19": {
        "clientName": "ANDROID", "clientVersion": "19.09.37",
        "androidSdkVersion": 33, "platform": "MOBILE",
        "userAgent": "com.google.android.youtube/19.09.37 (Linux; U; Android 13; en_US) gzip",
    },
    "ios": {
        "clientName": "IOS", "clientVersion": "20.10.4",
        "deviceMake": "Apple", "deviceModel": "iPhone16,2",
        "platform": "MOBILE", "osName": "iPhone", "osVersion": "18.3.0.22D50",
        "userAgent": "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_0 like Mac OS X; en_US)",
    },
    "mweb": {
        "clientName": "MWEB", "clientVersion": "2.20250311.03.00", "platform": "MOBILE",
        "userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
    },
    "tv": {
        "clientName": "TV", "clientVersion": "1.0", "platform": "TV",
        "userAgent": "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version",
    },
    "tv_embedded": {
        "clientName": "TV_EMBEDDED", "clientVersion": "1.0", "platform": "TV",
        "userAgent": "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version",
    },
    "web_embedded": {
        "clientName": "WEB_EMBEDDED", "clientVersion": "1.20250310.01.00", "platform": "DESKTOP",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    },
    "web": {
        "clientName": "WEB", "clientVersion": "2.20250311.03.00", "platform": "DESKTOP",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    },
}

KEYS = [None, "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11ioW8"]

for name, client in CLIENTS.items():
    ua = client.pop("userAgent")
    for key in KEYS:
        body = {"context": {"client": client}, "videoId": VIDEO}
        params = {"prettyPrint": "false"}
        if key:
            params["key"] = key
        try:
            r = requests.post(API, params=params, json=body,
                              headers={"User-Agent": ua, "Content-Type": "application/json"},
                              timeout=20)
            d = r.json()
        except Exception as e:
            print(f"{name:15s} key={bool(key)}: EXC {e}")
            continue
        ps = d.get("playabilityStatus", {})
        sd = d.get("streamingData", {})
        n_fmt = len(sd.get("formats", []) or []) + len(sd.get("adaptiveFormats", []) or [])
        has_url = any(f.get("url") for f in (sd.get("formats", []) or []) + (sd.get("adaptiveFormats", []) or []))
        status = ps.get("status")
        reason = (ps.get("reason") or "")[:60]
        print(f"{name:15s} key={bool(key)}: HTTP {r.status_code} | {status} | formats={n_fmt} direct_url={has_url} | {reason}")
        if status == "OK" and n_fmt:
            break
