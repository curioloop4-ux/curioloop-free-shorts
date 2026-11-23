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
    # GitHub Actions may define GEMINI_MODEL as an empty secret/variable.
    # Treat an empty value the same as an unset value.
    model = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
    # Gemini 2.5 generation access is restricted for newly created projects.
    # Force the supported current lightweight model if the old value is configured.
    if not model or model == "gemini-2.5-flash":
        model = "gemini-3.5-flash-lite"
    prompt = """Create one original, fact-checkable English YouTube Shorts script for a global audience.
Return ONLY valid JSON with keys: title, narration, visual_theme, visual_queries.
Requirements: title under 65 characters; narration 75-105 words; compelling first sentence;
one surprising but accurate fact; no medical/financial advice, politics, or fabricated claims.
visual_theme must describe the visual mood. visual_queries must be an array of exactly 3
short, concrete English search phrases for real photographs that directly illustrate the
narration (people, places, objects, animals, scientific equipment, or real-world scenes).
Never use abstract backgrounds, book titles, metaphors, or generic color/style phrases as
visual_queries. For a topic about waiting in lines, use phrases like "people waiting in queue",
"supermarket checkout line", and "airport security queue".
Avoid repeating obvious topics such as tardigrades. Choose a fresh science, nature, history,
technology, or everyday-curiosity topic. Do not include markdown."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    print(f"Generating script with Gemini model: {model}")
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
    if not isinstance(data.get("visual_queries"), list) or len(data["visual_queries"]) < 3:
        raise ValueError("Gemini response must include 3 concrete visual_queries")
    data["visual_queries"] = [str(q).strip() for q in data["visual_queries"][:3] if str(q).strip()]
    if len(data["visual_queries"]) < 3:
        raise ValueError("Gemini returned fewer than 3 usable visual_queries")
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



def fetch_video_clips(title, visual_theme, visual_queries, count=3):
    """Fetch a small number of real video clips from Pixabay's official video API."""
    api_key = required("PIXABAY_API_KEY")
    api = "https://pixabay.com/api/videos/"
    headers = {"User-Agent": "CurioLoopShorts/2.0"}
    queries = list(visual_queries or [])
    query = next((" ".join(str(q).split()[:7]) for q in queries if str(q).strip()), "")
    if not query:
        query = " ".join(title.split()[:6])
    query = query[:100]

    params = {
        "key": api_key,
        "q": query,
        "video_type": "film",
        "per_page": 20,
        "safesearch": "true",
    }
    try:
        response = requests.get(api, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        results = response.json().get("hits", [])
        print(f"Pixabay video search '{query}' returned {len(results)} result(s).")
    except Exception as exc:
        raise RuntimeError(f"Pixabay video search failed: {type(exc).__name__}: {exc}") from exc

    clips, credits, seen = [], [], set()
    for item in results:
        if len(clips) >= count:
            break
        if int(item.get("duration") or 0) < 3:
            continue
        files = item.get("videos") or {}
        choices = [files.get(k) for k in ("medium", "large", "small", "tiny") if files.get(k)]
        choices = [v for v in choices if v.get("url")]
        if not choices:
            continue
        # Prefer medium resolution; use the largest available if medium is absent.
        selected = next((v for v in choices if v.get("width", 0) >= 720), choices[0])
        url = selected["url"]
        if url in seen:
            continue
        seen.add(url)
        try:
            r = requests.get(url, headers=headers, timeout=60)
            r.raise_for_status()
            data = r.content
            if len(data) < 50000 or len(data) > 60 * 1024 * 1024:
                print(f"Skipped Pixabay clip with unsuitable size: {len(data)} bytes.")
                continue
            if len(data) < 12 or data[4:8] != b"ftyp":
                print("Skipped Pixabay result: downloaded file is not an MP4 video.")
                continue
            path = WORK / f"clip_{len(clips)+1}.mp4"
            path.write_bytes(data)
            clips.append(path)
            creator = item.get("user", "Pixabay contributor")
            page_url = item.get("pageURL", "https://pixabay.com/videos/")
            credits.append(f"Video by {creator} on Pixabay: {page_url}")
            print(f"Using video clip {len(clips)}: {creator}, {len(data)} bytes.")
        except Exception as exc:
            print(f"Pixabay video download failed: {type(exc).__name__}: {exc}")

    print(f"Downloaded {len(clips)} real video clip(s) from Pixabay.")
    return clips, credits


def render(title, voice, captions, duration, visual_theme, visual_queries):
    output = WORK / "curioloop_short.mp4"
    title_path = WORK / "title.txt"
    title_path.write_text(title.replace("\\n", " ").strip(), encoding="utf-8")
    clips, credits = fetch_video_clips(title, visual_theme, visual_queries, count=3)
    if not clips:
        raise RuntimeError("No usable stock video clips found; stopping to prevent a still-image or blank upload.")

    segment = duration / len(clips)
    cmd = ["ffmpeg", "-y"]
    for clip_path in clips:
        cmd += ["-stream_loop", "-1", "-i", str(clip_path)]
    cmd += ["-i", str(voice)]

    filters = []
    labels = []
    for i in range(len(clips)):
        filters.append(
            f"[{i}:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,trim=duration={segment:.3f},"
            f"setpts=PTS-STARTPTS,setsar=1[v{i}]"
        )
        labels.append(f"[v{i}]")
    filters.append(
        "".join(labels) + f"concat=n={len(clips)}:v=1:a=0,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "textfile=title.txt:fontcolor=white:fontsize=62:"
        "x=(w-text_w)/2:y=150:box=1:boxcolor=0x000000@0.45:boxborderw=24,"
        "ass=captions.ass[vout]"
    )
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", f"{len(clips)}:a:0",
        "-t", f"{duration:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        "-movflags", "+faststart", str(output)
    ]
    subprocess.run(cmd, cwd=WORK, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return output, credits

def upload(path, title, narration, credits=None):
    creds = Credentials(
        None,
        refresh_token=required("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("YOUTUBE_CLIENT_ID"),
        client_secret=required("YOUTUBE_CLIENT_SECRET"),
        scopes=SCOPES,
    )
    youtube = build("youtube", "v3", credentials=creds)
    privacy = os.getenv("YOUTUBE_PRIVACY_STATUS", "public").lower()
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("YOUTUBE_PRIVACY_STATUS must be private, unlisted, or public")
    body = {
        "snippet": {
            "title": title[:100],
            "description": narration + ("\n\nVisual credits:\n" + "\n".join(credits) if credits else "") + "\n\n#Shorts #CurioLoop #InterestingFacts",
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
    import time

    try:
        count = int(os.getenv("SHORTS_COUNT", "1"))
    except ValueError:
        count = 1
    count = max(1, min(count, 10))
    print(f"Starting batch: {count} Short(s).")
    uploaded = 0

    for index in range(1, count + 1):
        print(f"\\n========== SHORT {index}/{count} ==========")
        try:
            script = make_script()
            print("Selected:", script["title"])
            voice = asyncio.run(make_voice(script["narration"]))
            captions, duration = make_ass(script["narration"])
            rendered, credits = render(
                script["title"], voice, captions, duration,
                script["visual_theme"], script["visual_queries"]
            )
            upload(rendered, script["title"], script["narration"], credits)
            uploaded += 1
        except Exception as exc:
            print(f"Short {index} failed: {type(exc).__name__}: {exc}")
        if index < count:
            time.sleep(8)

    print(f"\\nBatch finished: {uploaded}/{count} Short(s) uploaded.")
    if uploaded == 0:
        raise RuntimeError("No Shorts were uploaded successfully.")


if __name__ == "__main__":
    main()
