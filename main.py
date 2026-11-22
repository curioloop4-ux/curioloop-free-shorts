import asyncio
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import edge_tts
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).parent
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def required(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def make_script():
    api_key = required("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    prompt = """Create one original, fact-checkable English YouTube Shorts script for a global audience.
Return ONLY valid JSON with keys: title, narration, visual_theme.
Requirements: title under 65 characters; narration 75-105 words; compelling first sentence;
one surprising but accurate fact; no medical/financial advice, politics, or fabricated claims.
visual_theme must be a concise visual direction for an abstract science-style background.
Avoid repeating obvious topics such as tardigrades. Choose a fresh science, nature, history,
technology, or everyday-curiosity topic. Do not include markdown."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = requests.post(
        url,
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.85, "responseMimeType": "application/json"}},
        timeout=90,
    )
    response.raise_for_status()
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(raw)
    for key in ("title", "narration", "visual_theme"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Gemini response missing {key}")
    return data



async def make_voice(text):
    path = WORK / "voice.mp3"
    communicate = edge_tts.Communicate(text, "en-US-AriaNeural", rate="+2%")
    await communicate.save(str(path))
    return path


def make_ass(narration):
    # Word-group captions, with simple timing across the narration.
    words = narration.split()
    groups = [" ".join(words[i:i+5]) for i in range(0, len(words), 5)]
    # The voice is usually ~145 wpm; total duration is refined from ffprobe below.
    probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(WORK / "voice.mp3")
    ], text=True).strip()
    duration = float(probe)
    def stamp(seconds):
        cs = int(seconds * 100)
        h, rem = divmod(cs, 360000)
        m, rem = divmod(rem, 6000)
        s, c = divmod(rem, 100)
        return f"{h}:{m:02d}:{s:02d}.{c:02d}"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,66,&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,1,0,1,4,1,2,90,90,250,1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    step = duration / max(len(groups), 1)
    for i, group in enumerate(groups):
        lines.append(f"Dialogue: 0,{stamp(i*step)},{stamp(min((i+1)*step, duration))},Default,,0,0,0,,{{\\an2}}{group}")
    path = WORK / "captions.ass"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path, duration


def render(title, voice, captions, duration):
    output = WORK / "curioloop_short.mp4"
    # Generate a vertical abstract motion-card background locally; no stock-video API is needed.
    title_path = WORK / "title.txt"
    title_path.write_text(title.replace("\\n", " ").strip(), encoding="utf-8")
    filter_value = (
        "drawgrid=width=120:height=120:thickness=2:color=0x29415f@0.45,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "textfile=title.txt:fontcolor=white:fontsize=68:"
        "x=(w-text_w)/2:y=260:box=1:boxcolor=0x000000@0.25:boxborderw=28,"
        "ass=captions.ass"
    )
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        "color=c=0x101b35:s=1080x1920:r=30", "-i", str(voice),
        "-t", f"{duration:.2f}", "-vf", filter_value, "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(output)
    ], cwd=WORK, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return output


def upload(path, title, narration):
    creds = Credentials(
        None,
        refresh_token=required("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("YOUTUBE_CLIENT_ID"),
        client_secret=required("YOUTUBE_CLIENT_SECRET"),
        scopes=SCOPES,
    )
    youtube = build("youtube", "v3", credentials=creds)
    privacy = os.getenv("YOUTUBE_PRIVACY_STATUS", "private").lower()
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("YOUTUBE_PRIVACY_STATUS must be private, unlisted, or public")
    body = {
        "snippet": {
            "title": title[:100],
            "description": narration + "\n\n#Shorts #CurioLoop #InterestingFacts",
            "tags": ["Shorts", "CurioLoop", "facts", "curiosity"],
            "categoryId": "27",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True),
    )
    result = request.execute()
    print("Uploaded video ID:", result.get("id"))
    print("Privacy status:", privacy)


def main():
    script = make_script()
    print("Selected:", script["title"])
    voice = asyncio.run(make_voice(script["narration"]))
    captions, duration = make_ass(script["narration"])
    rendered = render(script["title"], voice, captions, duration)
    upload(rendered, script["title"], script["narration"])


if __name__ == "__main__":
    main()
