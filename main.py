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
Return ONLY valid JSON with keys: title, narration, visual_search.
Requirements: title under 65 characters; narration 75-105 words; compelling first sentence;
one surprising but accurate fact; no medical/financial advice, politics, or fabricated claims.
visual_search must be a concise Pexels stock-footage search phrase matching the story.
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
    for key in ("title", "narration", "visual_search"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Gemini response missing {key}")
    return data


def download_pexels_video(query):
    headers = {"Authorization": required("PEXELS_API_KEY")}
    response = requests.get(
        "https://api.pexels.com/videos/search",
        headers=headers,
        params={"query": query, "orientation": "portrait", "per_page": 8},
        timeout=45,
    )
    response.raise_for_status()
    videos = response.json().get("videos", [])
    if not videos:
        raise RuntimeError(f"No Pexels videos found for: {query}")
    # Prefer a medium-sized downloadable file to reduce render time.
    for video in videos:
        files = sorted(video.get("video_files", []),
                       key=lambda f: (f.get("width", 0) < 720, abs((f.get("height", 1) / max(f.get("width", 1), 1)) - 1.777)))
        for item in files:
            link = item.get("link")
            if link and item.get("file_type") == "video/mp4":
                r = requests.get(link, timeout=90)
                r.raise_for_status()
                path = WORK / "background.mp4"
                path.write_bytes(r.content)
                return path
    raise RuntimeError("Pexels returned no downloadable MP4 file")


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


def render(video, voice, captions, duration):
    output = WORK / "curioloop_short.mp4"
    # Crop to vertical 9:16, loop stock clip if shorter than narration, and burn captions.
    filter_value = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,ass=captions.ass"
    subprocess.run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(video), "-i", str(voice),
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
    video = download_pexels_video(script["visual_search"])
    voice = asyncio.run(make_voice(script["narration"]))
    captions, duration = make_ass(script["narration"])
    rendered = render(video, voice, captions, duration)
    upload(rendered, script["title"], script["narration"])


if __name__ == "__main__":
    main()
