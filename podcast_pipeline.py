#!/usr/bin/env python3
"""
The Canadian Numbers Guy — Automated Podcast Pipeline
Groq script → Edge TTS audio → Cloudflare R2 → Podcasting 2.0 RSS
"""

import asyncio
import io
import json
import os
import sys
import tempfile
import uuid
import requests
import edge_tts
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

# ─── CONFIG ───────────────────────────────────────────────────────────────────

GROQ_API_KEY         = os.environ["GROQ_API_KEY"]

R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME       = os.environ["R2_BUCKET_NAME"]
R2_PUBLIC_URL        = os.environ["R2_PUBLIC_URL"].rstrip("/")

NODE_PUBKEY          = os.environ["NODE_PUBKEY"]
FEED_URL             = f"{R2_PUBLIC_URL}/feed.xml"

# Canadian English neural voice — free via Microsoft Edge TTS
TTS_VOICE            = "en-CA-LiamNeural"

ARTWORK_URL          = f"https://pub-6b50685205f14f8d8838ec07c7deb217.r2.dev/artwork.jpg.png"

PODCAST_TITLE        = "The Canadian Numbers Guy"
PODCAST_DESCRIPTION  = (
    "Daily Canadian economic analysis. Data-driven breakdowns of what "
    "the numbers actually mean — for renters, workers, investors, and everyone in between."
)
MAX_FEED_EPISODES    = 20


# ─── R2 CLIENT ────────────────────────────────────────────────────────────────

_r2_client = None

def r2():
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
    r2().put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return f"{R2_PUBLIC_URL}/{key}"


def download_json(key: str, default):
    try:
        resp = r2().get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return json.loads(resp["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return default
        raise


# ─── STEP 1: GENERATE SCRIPT ──────────────────────────────────────────────────

def generate_script(topic: str) -> str:
    print("  [1/4] Generating script via Groq...")

    prompt = f"""Write a 2-minute solo podcast script about this Canadian economic topic:

"{topic}"

Structure it exactly like this:

Open with a warm good morning greeting welcoming the listener to The Canadian Numbers Guy. Keep it natural and brief — one or two sentences max. Something like "Good morning and welcome to The Canadian Numbers Guy — the show where we cut through the noise and tell you what the numbers actually mean."

Then cover three angles:
1. Everyday Canadians — renters, workers, people living paycheque to paycheque.
2. The investor class — TFSA holders, landlords, Bay Street types.
3. The raw data — one or two hard numbers from Statistics Canada or the Bank of Canada.

Close with a short sign-off that feels like a daily show ending — something like "That's the data for today. I'll see you tomorrow morning with more numbers that matter. Take care."

Tone: punchy, plain-spoken, analytically sharp. No fluff, no jargon.
No speaker labels, no section headers, no sound cues.
Continuous clean prose, exactly as it will be spoken."""

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ─── STEP 2: GENERATE AUDIO ───────────────────────────────────────────────────

async def _tts_to_file(text: str, path: str):
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    await communicate.save(path)


def generate_audio(script: str) -> bytes:
    print(f"  [2/4] Rendering audio via Edge TTS ({TTS_VOICE})...")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        temp_path = f.name
    try:
        asyncio.run(_tts_to_file(script, temp_path))
        with open(temp_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(temp_path)


# ─── STEP 3: BUILD RSS FEED ───────────────────────────────────────────────────

def build_feed(episodes: list) -> bytes:
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes":  "http://www.itunes.com/dtds/podcast-1.0.dtd",
        "xmlns:podcast": "https://podcastindex.org/namespace/1.0",
        "xmlns:atom":    "http://www.w3.org/2005/Atom",
    })
    ch = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text       = PODCAST_TITLE
    ET.SubElement(ch, "link").text        = FEED_URL
    ET.SubElement(ch, "description").text = PODCAST_DESCRIPTION
    ET.SubElement(ch, "language").text    = "en-ca"
    ET.SubElement(ch, "atom:link", {
        "href": FEED_URL, "rel": "self", "type": "application/rss+xml",
    })
    ET.SubElement(ch, "itunes:author").text   = "The Canadian Numbers Guy"
    ET.SubElement(ch, "itunes:explicit").text  = "false"
    ET.SubElement(ch, "itunes:category", {"text": "Business"})
    ET.SubElement(ch, "itunes:image", {"href": ARTWORK_URL})

    image = ET.SubElement(ch, "image")
    ET.SubElement(image, "url").text   = ARTWORK_URL
    ET.SubElement(image, "title").text = PODCAST_TITLE
    ET.SubElement(image, "link").text  = FEED_URL
    ET.SubElement(ch, "podcast:locked").text = "yes"

    value = ET.SubElement(ch, "podcast:value", {
        "type": "lightning", "method": "keysend", "suggested": "0.00000010000",
    })
    ET.SubElement(value, "podcast:valueRecipient", {
        "name": "The Canadian Numbers Guy", "type": "node",
        "address": NODE_PUBKEY, "split": "100",
    })

    for ep in episodes[:MAX_FEED_EPISODES]:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text                         = ep["title"]
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = ep["guid"]
        ET.SubElement(item, "pubDate").text                       = ep["pub_date"]
        ET.SubElement(item, "description").text                   = ep["title"]
        ET.SubElement(item, "itunes:duration").text               = "00:02:00"
        ET.SubElement(item, "enclosure", {
            "url": ep["audio_url"], "length": ep["audio_size"], "type": "audio/mpeg",
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


# ─── TOPIC RESOLUTION ─────────────────────────────────────────────────────────

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
