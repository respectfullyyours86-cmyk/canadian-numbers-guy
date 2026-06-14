#!/usr/bin/env python3
"""
The Canadian Numbers Guy — Automated Podcast Pipeline
Gemini script → ElevenLabs audio → Cloudflare R2 → Podcasting 2.0 RSS

Run manually:  python podcast_pipeline.py
With a topic:  python podcast_pipeline.py "CPI release June 2025 and what it means for Canadians"
Via cron:      GitHub Actions reads topic.txt automatically
"""

import io
import json
import os
import sys
import uuid
import anthropic
import requests
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

# ─── CONFIG ───────────────────────────────────────────────────────────────────
# Everything comes from environment variables. Never hardcode keys.

ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
ELEVENLABS_API_KEY   = os.environ["ELEVENLABS_API_KEY"]
# Default: ElevenLabs "Adam" — deep, authoritative, good for finance/econ
# Browse voices at elevenlabs.io/voice-library and swap the ID in your .env
ELEVENLABS_VOICE_ID  = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")

R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME       = os.environ["R2_BUCKET_NAME"]
# Your R2 bucket public URL — from R2 → bucket → Settings → Public Access
R2_PUBLIC_URL        = os.environ["R2_PUBLIC_URL"].rstrip("/")

NODE_PUBKEY          = os.environ["NODE_PUBKEY"]
FEED_URL             = f"{R2_PUBLIC_URL}/feed.xml"

PODCAST_TITLE        = "The Canadian Numbers Guy"
PODCAST_DESCRIPTION  = (
    "Daily Canadian economic analysis. Data-driven breakdowns of what "
    "the numbers actually mean — for renters, workers, investors, and everyone in between."
)
MAX_FEED_EPISODES    = 20   # How many episodes to keep visible in the RSS feed


# ─── R2 CLIENT ────────────────────────────────────────────────────────────────

_r2_client = None

def r2():
    """Singleton R2 client — created once, reused across all uploads."""
    global _r2_client
    if _r2_client is None:
        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _r2_client


def upload(key: str, data: bytes, content_type: str) -> str:
    """Upload bytes to R2 and return the public URL."""
    r2().put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return f"{R2_PUBLIC_URL}/{key}"


def download_json(key: str, default):
    """Download a JSON file from R2, or return `default` if it doesn't exist yet."""
    try:
        resp = r2().get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return json.loads(resp["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return default
        raise


# ─── STEP 1: GENERATE SCRIPT ──────────────────────────────────────────────────

def generate_script(topic: str) -> str:
    print("  [1/4] Generating script via Claude...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Write a 2-minute solo podcast script about this Canadian economic topic:

"{topic}"

Cover three angles in order:
1. Everyday Canadians — renters, workers, people living paycheque to paycheque. What does this mean for them on the ground?
2. The investor class — TFSA holders, landlords, Bay Street types. How are they positioned?
3. The raw data — one or two hard numbers from Statistics Canada or the Bank of Canada that cut through the noise.

Tone rules:
- Punchy, plain-spoken, analytically sharp. No fluff, no academic jargon.
- Sound like a knowledgeable friend who actually understands the numbers.
- No speaker labels, no section headers, no sound effect cues.
- Continuous clean prose, exactly as it will be spoken."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


# ─── STEP 2: GENERATE AUDIO ───────────────────────────────────────────────────

def generate_audio(script: str) -> bytes:
    print("  [2/4] Rendering audio via ElevenLabs...")
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": ELEVENLABS_API_KEY,
        },
        json={
            "text": script,
            "model_id": "eleven_turbo_v2_5",   # Fast + high quality
            "voice_settings": {
                "stability": 0.55,              # Consistent, professional delivery
                "similarity_boost": 0.75,       # Stays true to the chosen voice
                "style": 0.30,                  # Slight expressiveness — not robotic
                "use_speaker_boost": True,
            },
        },
    )
    resp.raise_for_status()
    return resp.content


# ─── STEP 3: BUILD RSS FEED ───────────────────────────────────────────────────

def build_feed(episodes: list) -> bytes:
    """Build a complete, valid Podcasting 2.0 RSS feed from a list of episode dicts."""
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes":  "http://www.itunes.com/dtds/podcast-1.0.dtd",
        "xmlns:podcast": "https://podcastindex.org/namespace/1.0",
        "xmlns:atom":    "http://www.w3.org/2005/Atom",
    })
    ch = ET.SubElement(rss, "channel")

    # ── Required channel fields ────────────────────────────────────────────────
    ET.SubElement(ch, "title").text       = PODCAST_TITLE
    ET.SubElement(ch, "link").text        = FEED_URL
    ET.SubElement(ch, "description").text = PODCAST_DESCRIPTION
    ET.SubElement(ch, "language").text    = "en-ca"

    # Self-referencing atom:link — required by podcast directories
    ET.SubElement(ch, "atom:link", {
        "href": FEED_URL,
        "rel":  "self",
        "type": "application/rss+xml",
    })

    # ── iTunes namespace ───────────────────────────────────────────────────────
    ET.SubElement(ch, "itunes:author").text   = "The Canadian Numbers Guy"
    ET.SubElement(ch, "itunes:explicit").text  = "false"
    ET.SubElement(ch, "itunes:category", {"text": "Business"})

    # ── Podcasting 2.0 — lock + Lightning value tag ───────────────────────────
    ET.SubElement(ch, "podcast:locked").text = "yes"  # Blocks unauthorized ownership transfers

    value = ET.SubElement(ch, "podcast:value", {
        "type":      "lightning",
        "method":    "keysend",
        "suggested": "0.00000010000",   # ~10 sats per minute
    })
    ET.SubElement(value, "podcast:valueRecipient", {
        "name":    "The Canadian Numbers Guy",
        "type":    "node",
        "address": NODE_PUBKEY,
        "split":   "100",
    })

    # ── Episode items (newest first) ──────────────────────────────────────────
    for ep in episodes[:MAX_FEED_EPISODES]:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text                         = ep["title"]
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = ep["guid"]
        ET.SubElement(item, "pubDate").text                       = ep["pub_date"]
        ET.SubElement(item, "description").text                   = ep["title"]
        ET.SubElement(item, "itunes:duration").text               = "00:02:00"
        ET.SubElement(item, "enclosure", {
            "url":    ep["audio_url"],
            "length": ep["audio_size"],
            "type":   "audio/mpeg",
        })

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="\t", level=0)
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def create_episode(topic: str):
    print(f"\n🎙  The Canadian Numbers Guy")
    print(f"    Topic: {topic}\n")

    now      = datetime.now(tz=timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    guid     = str(uuid.uuid4())

    script      = generate_script(topic)
    audio_bytes = generate_audio(script)

    print("  [3/4] Uploading audio to Cloudflare R2...")
    audio_url = upload(f"episodes/{date_str}.mp3", audio_bytes, "audio/mpeg")

    print("  [4/4] Updating episode archive and RSS feed...")
    history = download_json("episodes.json", default=[])

    history.insert(0, {
        "title":      f"{PODCAST_TITLE} — {date_str}",
        "guid":       guid,
        "pub_date":   format_datetime(now),
        "audio_url":  audio_url,
        "audio_size": str(len(audio_bytes)),
        "topic":      topic,
    })

    upload("episodes.json", json.dumps(history, indent=2).encode(), "application/json")
    upload("feed.xml", build_feed(history), "application/rss+xml")

    print(f"\n✅  Episode live!")
    print(f"    Audio  → {audio_url}")
    print(f"    Feed   → {FEED_URL}")
    print(f"    Archive: {len(history)} episode(s) total")
    print(f"\n    → Submit {FEED_URL} to Fountain once — all future")
    print(f"      episodes appear automatically.")


# ─── TOPIC RESOLUTION ─────────────────────────────────────────────────────────
# Priority order:
#   1. CLI argument            python podcast_pipeline.py "Your topic here"
#   2. EPISODE_TOPIC env var   Set by GitHub Actions workflow_dispatch
#   3. topic.txt file          Edit this in your repo before the daily cron fires
#   4. Hardcoded fallback      Generic topic if nothing else is set

if __name__ == "__main__":
    if len(sys.argv) > 1:
        topic = " ".join(sys.argv[1:])
    elif os.environ.get("EPISODE_TOPIC", "").strip():
        topic = os.environ["EPISODE_TOPIC"].strip()
    else:
        try:
            topic = open("topic.txt").read().strip()
            if not topic:
                raise ValueError("topic.txt is empty")
        except (FileNotFoundError, ValueError):
            topic = (
                "The Bank of Canada's latest rate decision — "
                "what it means for variable mortgage holders, GIC investors, "
                "and whether Canadian inflation is actually under control."
            )

    create_episode(topic)
