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
        "script": script,
        "duration": duration_seconds(mp3_path, script),
        "bytes": mp3_path.stat().st_size,
    }

    (EPISODES / (date_text + ".json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def prune():
    dates = sorted((p.stem for p in EPISODES.glob("*.json")), reverse=True)

    for old in dates[KEEP_EPISODES:]:
        for ext in (".json", ".mp3", ".html"):
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
        description = escape(shown(meta["description"]))
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


PAGE_TITLE = os.environ.get("PAGE_TITLE", "Morning Brief for Shahriar")
PAGE_TAGLINE = "Your daily spoken brief: weather, schedule and the top AI news."

PAGE_CSS = """
:root{--bg:#f6f8fb;--card:#ffffff;--ink:#12263f;--muted:#5a6b80;--line:#dbe3ee;
--accent:#f28c28;--on-accent:#1b0f02;--accent-ink:#a84a05;--accent-soft:#fdebd7}
@media (prefers-color-scheme:dark){:root{--bg:#0a1626;--card:#11233a;--ink:#e8eef6;
--muted:#9fb0c4;--line:#223653;--accent:#f5a04a;--on-accent:#1b0f02;--accent-ink:#f5a04a;--accent-soft:#3a2a14}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:44rem;margin:0 auto;padding:2rem 1rem 3rem}
header{display:flex;gap:1.25rem;align-items:center;margin-bottom:1.75rem}
header img{width:96px;height:96px;border-radius:16px;flex:none}
h1{font-size:1.6rem;line-height:1.2;margin:0 0 .25rem}
header p{margin:0;color:var(--muted)}
.label{display:inline-block;font-size:.75rem;font-weight:700;letter-spacing:.08em;
text-transform:uppercase;color:var(--accent-ink);background:var(--accent-soft);
padding:.2rem .6rem;border-radius:999px;margin-bottom:.75rem}
.latest{background:var(--card);border:1px solid var(--line);border-radius:18px;
padding:1.5rem;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.latest h2{font-size:1.35rem;line-height:1.3;margin:0 0 .25rem}
.meta{color:var(--muted);font-size:.9rem;margin:0 0 1rem}
audio{width:100%;margin:.25rem 0 1rem}
.summary{margin:0}
.actions{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:1.25rem}
.btn{display:inline-block;font:inherit;font-size:.9rem;padding:.5rem .9rem;border-radius:10px;
border:1px solid var(--line);background:transparent;color:var(--ink);text-decoration:none;cursor:pointer}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}
h3{font-size:1rem;margin:2.25rem 0 .75rem;color:var(--muted);font-weight:600}
ul{list-style:none;margin:0;padding:0}
li{border-top:1px solid var(--line);padding:.9rem 0}
li strong{display:block}
li span{color:var(--muted);font-size:.85rem}
li audio{margin:.5rem 0 0;height:36px}
.empty{color:var(--muted)}
footer{margin-top:2.5rem;color:var(--muted);font-size:.85rem;word-break:break-all}
code{background:var(--accent-soft);padding:.1rem .35rem;border-radius:6px}
a{color:var(--accent-ink)}
.back{text-decoration:none;font-size:.9rem}
.transcript p{margin:0 0 1rem}
li .links{display:block;margin-top:.35rem;font-size:.85rem}
"""


def shown(text):
    # The script spells the name "Shahriaar" so the voice pronounces it well;
    # show the normal spelling on the web page and in the feed.
    return str(text).replace("Shahriaar", "Shahriar")


def pretty_meta(meta):
    day = dt.date.fromisoformat(meta["date"])

    minutes = max(1, int(round(meta["duration"] / 60.0)))

    return "%s, %d %s %d · %d min" % (
        day.strftime("%A"), day.day, day.strftime("%B"), day.year, minutes
    )


def page_shell(title, inner, prefix=""):
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link rel="icon" href="%scover.jpg">'
        '<link rel="alternate" type="application/rss+xml" href="%sfeed.xml">'
        "<title>%s</title><style>%s</style></head>"
        '<body><div class="wrap">%s</div></body></html>'
    ) % (prefix, prefix, escape(title), PAGE_CSS, inner)


def build_index(base):
    episodes = load_episodes()

    feed_url = base + "/feed.xml"

    inner = (
        '<header><img src="cover.jpg" alt=""><div><h1>%s</h1><p>%s</p></div></header>'
        % (escape(PAGE_TITLE), escape(PAGE_TAGLINE))
    )

    if not episodes:
        inner += '<p class="empty">No episodes yet.</p>'

    else:
        latest = episodes[0]

        actions = ""

        if latest.get("script"):
            actions += (
                '<a class="btn primary" href="episodes/%s.html">Read full transcript</a>'
                % latest["date"]
            )

        actions += (
            '<a class="btn" href="episodes/%s.mp3" download>Download MP3</a>'
            % latest["date"]
        )

        actions += (
            '<button class="btn" type="button" '
            "onclick=\"navigator.clipboard.writeText('%s').then(function(){this.textContent='Copied'}.bind(this))\">"
            "Copy podcast feed link</button>" % feed_url
        )

        inner += (
            '<section class="latest"><span class="label">Latest episode</span>'
            "<h2>%s</h2><p class=\"meta\">%s</p>"
            '<audio controls preload="metadata" src="episodes/%s.mp3"></audio>'
            '<p class="summary">%s</p>'
            '<div class="actions">%s</div></section>'
            % (
                escape(latest["title"]),
                escape(pretty_meta(latest)),
                latest["date"],
                escape(shown(latest["description"])),
                actions,
            )
        )

        older = episodes[1:]

        if older:
            rows = []

            for meta in older:
                links = ""

                if meta.get("script"):
                    links = (
                        '<span class="links"><a href="episodes/%s.html">Transcript</a></span>'
                        % meta["date"]
                    )

                rows.append(
                    "<li><strong>%s</strong><span>%s</span>"
                    '<audio controls preload="none" src="episodes/%s.mp3"></audio>%s</li>'
                    % (escape(meta["title"]), escape(pretty_meta(meta)), meta["date"], links)
                )

            inner += "<h3>Previous episodes</h3><ul>%s</ul>" % "".join(rows)

    inner += (
        "<footer>Podcast feed for your podcast app: <code>%s</code></footer>" % feed_url
    )

    (DOCS / "index.html").write_text(page_shell(PAGE_TITLE, inner), encoding="utf-8")


def build_transcripts():
    for meta in load_episodes():
        script = meta.get("script")

        if not script:
            continue

        paragraphs = "".join(
            "<p>%s</p>" % escape(shown(p.strip()))
            for p in script.split("\n\n")
            if p.strip()
        )

        inner = (
            '<a class="back" href="../">← All episodes</a>'
            "<h1 style=\"margin-top:1rem\">%s</h1>"
            '<p class="meta">%s</p>'
            '<audio controls preload="metadata" src="%s.mp3"></audio>'
            '<div class="transcript">%s</div>'
            % (escape(meta["title"]), escape(pretty_meta(meta)), meta["date"], paragraphs)
        )

        (EPISODES / (meta["date"] + ".html")).write_text(
            page_shell(meta["title"], inner, prefix="../"), encoding="utf-8"
        )



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
    build_transcripts()

    print("Episode %s ready. Feed: %s/feed.xml" % (date_text, base))


if __name__ == "__main__":
    main()
