#!/usr/bin/env python3
"""Morning Brief podcast builder.

1. Fetches today's script from your Apps Script web app (secret token).
2. Turns it into an MP3 with Edge TTS (no API key, no card).
3. Keeps the last few episodes and rebuilds docs/feed.xml + docs/index.html.

Required environment variables (GitHub Actions secrets):
  APPS_SCRIPT_URL   your web app URL (ends in /exec)
  PODCAST_TOKEN     the token created by setPodcastToken() in Apps Script

Optional:
  TTS_VOICE         default en-US-AriaNeural  (list: edge-tts --list-voices)
  TTS_RATE          default +0%               (e.g. -5% for slower speech)
  KEEP_EPISODES     default 14
  SHOW_TITLE        default "Morning Brief"
  BASE_URL          override the public site URL
"""

import asyncio
import datetime as dt
import email.utils
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

DOCS = Path("docs")
EPISODES = DOCS / "episodes"

KEEP_EPISODES = int(os.environ.get("KEEP_EPISODES", "14"))
VOICE = os.environ.get("TTS_VOICE", "en-US-AriaNeural")
RATE = os.environ.get("TTS_RATE", "+0%")
SHOW_TITLE = os.environ.get("SHOW_TITLE", "Morning Brief")


# ------------------------------------------------------------
# 1. FETCH THE SCRIPT
# ------------------------------------------------------------

def fetch_script():
    base = os.environ["APPS_SCRIPT_URL"].strip()
    token = os.environ["PODCAST_TOKEN"].strip()

    query = urllib.parse.urlencode({"action": "podcast", "token": token})
    url = base + ("&" if "?" in base else "?") + query

    last_error = "unknown error"

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))

            if data.get("ok") and data.get("script"):
                return data["date"], data["script"]

            last_error = data.get("error", "unexpected response")

        except Exception as err:  # never print the URL: it holds the token
            last_error = type(err).__name__ + ": " + str(err)[:150]

        print("Fetch attempt %d failed: %s" % (attempt, last_error), file=sys.stderr)
        time.sleep(30 * attempt)

    sys.exit("Could not fetch today's script: " + last_error)


# ------------------------------------------------------------
# 2. TEXT TO SPEECH
# ------------------------------------------------------------

async def _tts(text, out_path):
    import edge_tts

    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(out_path))


def synthesize(text, out_path):
    last_error = "unknown error"

    for attempt in range(1, 4):
        try:
            asyncio.run(_tts(text, out_path))

            if out_path.exists() and out_path.stat().st_size > 10_000:
                return

            last_error = "audio file too small"

        except Exception as err:
            last_error = type(err).__name__ + ": " + str(err)[:150]

        print("TTS attempt %d failed: %s" % (attempt, last_error), file=sys.stderr)
        time.sleep(10 * attempt)

    sys.exit("Text-to-speech failed: " + last_error)


def duration_seconds(path, text):
    try:
        from mutagen.mp3 import MP3

        return int(MP3(str(path)).info.length)
    except Exception:
        return int(len(text.split()) / 150 * 60)  # rough estimate


# ------------------------------------------------------------
# 3. EPISODES
# ------------------------------------------------------------

def save_episode(date_text, script):
    EPISODES.mkdir(parents=True, exist_ok=True)

    mp3_path = EPISODES / (date_text + ".mp3")

    synthesize(script, mp3_path)

    day = dt.date.fromisoformat(date_text)

    pretty = "%s, %s %d" % (day.strftime("%A"), day.strftime("%B"), day.day)

    description = " ".join(script.split())[:400]

    meta = {
        "date": date_text,
        "title": "%s: %s" % (SHOW_TITLE, pretty),
        "description": description,
        "duration": duration_seconds(mp3_path, script),
        "bytes": mp3_path.stat().st_size,
    }

    (EPISODES / (date_text + ".json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def prune():
    dates = sorted((p.stem for p in EPISODES.glob("*.json")), reverse=True)

    for old in dates[KEEP_EPISODES:]:
        for ext in (".json", ".mp3"):
            (EPISODES / (old + ext)).unlink(missing_ok=True)


# ------------------------------------------------------------
# 4. FEED + INDEX PAGE
# ------------------------------------------------------------

def base_url():
    override = os.environ.get("BASE_URL", "").strip()

    if override:
        return override.rstrip("/")

    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")

    return "https://%s.github.io/%s" % (owner.lower(), repo)


def load_episodes():
    metas = []

    for path in sorted(EPISODES.glob("*.json"), reverse=True):
        metas.append(json.loads(path.read_text(encoding="utf-8")))

    return metas


def build_feed(base):
    items = []

    for meta in load_episodes():
        date_text = meta["date"]

        published = email.utils.format_datetime(
            dt.datetime.fromisoformat(date_text).replace(
                hour=5, tzinfo=dt.timezone.utc
            )
        )

        title = escape(meta["title"])
        description = escape(meta["description"])
        audio_url = "%s/episodes/%s.mp3" % (base, date_text)

        items.append(
            "    <item>\n"
            "      <title>%s</title>\n"
            "      <description>%s</description>\n"
            "      <pubDate>%s</pubDate>\n"
            '      <guid isPermaLink="false">morning-brief-%s</guid>\n'
            '      <enclosure url="%s" length="%d" type="audio/mpeg"/>\n'
            "      <itunes:duration>%d</itunes:duration>\n"
            "    </item>"
            % (title, description, published, date_text, audio_url,
               meta["bytes"], meta["duration"])
        )

    show = escape(SHOW_TITLE)

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
        "  <channel>\n"
        "    <title>%s</title>\n"
        "    <link>%s/</link>\n"
        "    <description>Daily spoken morning brief: weather, schedule and AI news.</description>\n"
        "    <language>en</language>\n"
        "    <itunes:author>%s</itunes:author>\n"
        "    <itunes:explicit>false</itunes:explicit>\n"
        '    <itunes:image href="%s/cover.jpg"/>\n'
        "    <image><url>%s/cover.jpg</url><title>%s</title><link>%s/</link></image>\n"
        "%s\n"
        "  </channel>\n"
        "</rss>\n"
    ) % (show, base, show, base, base, show, base, "\n".join(items))

    (DOCS / "feed.xml").write_text(feed, encoding="utf-8")


def build_index(base):
    rows = []

    for meta in load_episodes():
        rows.append(
            "<li><strong>%s</strong><br>"
            '<audio controls preload="none" src="episodes/%s.mp3"></audio></li>'
            % (escape(meta["title"]), meta["date"])
        )

    page = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<link rel=\"icon\" href=\"cover.jpg\">"
        "<title>%s</title></head><body style=\"font-family:sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem\">"
        "<img src=\"cover.jpg\" alt=\"\" style=\"width:160px;border-radius:12px\">"
        "<h1>%s</h1><p>Podcast feed: <code>%s/feed.xml</code></p><ul>%s</ul></body></html>"
    ) % (escape(SHOW_TITLE), escape(SHOW_TITLE), base, "".join(rows))

    (DOCS / "index.html").write_text(page, encoding="utf-8")


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    for name in ("APPS_SCRIPT_URL", "PODCAST_TOKEN"):
        if not os.environ.get(name, "").strip():
            sys.exit("Missing secret: " + name)

    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").touch()

    date_text, script = fetch_script()

    save_episode(date_text, script)

    prune()

    base = base_url()

    build_feed(base)
    build_index(base)

    print("Episode %s ready. Feed: %s/feed.xml" % (date_text, base))


if __name__ == "__main__":
    main()
