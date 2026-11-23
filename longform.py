import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

import edge_tts
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).parent
WORK = ROOT / "work_longform"
WORK.mkdir(exist_ok=True)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
GENERIC = {"the", "and", "with", "from", "about", "into", "that", "this", "what",
           "how", "why", "your", "their", "real", "video", "footage", "cinematic",
           "close", "up", "wide", "shot", "background", "documentary"}


def required(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def generate_documentary():
    model = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
    if not model:
        model = "gemini-3.5-flash-lite"
    print("Using Gemini model:", model)
    prompt = """Create a rigorous, engaging 4-6 minute English-language mini-documentary for a global YouTube audience.
Choose one evergreen subject in science, psychology, history, human biology, nature or technology.
Avoid medical advice, sensationalism, conspiracy claims and unsupported statistics.
Return ONLY valid JSON with keys: title, description, sections, sources.
sections must contain exactly 6 objects, each with heading, narration, visual_queries.
Each narration must be 90-110 words, clear spoken English, with a distinct purpose and a logical progression.
Total narration must be 540-660 words. Structure: a compelling evidence-based hook; explain the central question;
present mechanisms or historical context; examine a concrete example; discuss what evidence does and does not show;
finish with a concise memorable conclusion.
Use careful qualification where evidence is debated. Never invent studies, quotations, dates or statistics.
Cite 3-5 real, reputable source URLs in sources (universities, peer-reviewed journals, museums, government science agencies).
Do not invent URLs. If unsure of a precise URL, cite the institution's known topic page.
Each section must have exactly 2 specific English Pixabay VIDEO search queries for relevant documentary B-roll.
Queries must describe visible subjects/actions, not abstract concepts. No generic city/landscape filler.
The first scene should be a visually arresting, topic-relevant real-world shot, not a presenter portrait.
Title under 75 characters. Description should be a concise factual summary. Return JSON only."""
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": required("GEMINI_API_KEY")},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.45, "responseMimeType": "application/json"}},
        timeout=120,
    )
    response.raise_for_status()
    data = json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
    if not isinstance(data.get("sections"), list) or len(data["sections"]) != 6:
        raise ValueError("Documentary must contain exactly six sections.")
    total_words = 0
    for section in data["sections"]:
        for key in ("heading", "narration"):
            if not isinstance(section.get(key), str) or not section[key].strip():
                raise ValueError(f"Missing section field: {key}")
        words = len(section["narration"].split())
        if not 90 <= words <= 110:
            raise ValueError(f"Section '{section['heading']}' has {words} words; expected 90-110.")
        total_words += words
        queries = section.get("visual_queries")
        if not isinstance(queries, list) or len(queries) != 2:
            raise ValueError("Each section must have exactly two topic-specific video queries.")
    if not 540 <= total_words <= 660:
        raise ValueError(f"Total narration is {total_words} words; expected 540-660.")
    if not isinstance(data.get("sources"), list) or len(data["sources"]) < 3:
        raise ValueError("At least three references are required.")
    data["word_count"] = total_words
    return data


async def make_voice(text, index):
    path = WORK / f"voice_{index:02d}.mp3"
    await edge_tts.Communicate(text, "en-US-GuyNeural", rate="-3%", volume="+0%").save(str(path))
    return path


def duration_of(path):
    result = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ], text=True).strip()
    return float(result)


def stamp(seconds):
    centiseconds = int(seconds * 100)
    hours, rem = divmod(centiseconds, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def make_captions(narration, duration, path):
    words = narration.split()
    groups, current = [], []
    for word in words:
        current.append(word)
        if len(current) >= 9 or re.search(r"[.!?]$", word):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    weights = [sum(max(2, len(re.sub(r"[^A-Za-z0-9']", "", w))) for w in group) for group in groups]
    total = sum(weights) or 1
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2",
        "PlayResX: 1920", "PlayResY: 1080", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,DejaVu Sans,43,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,0,0,1,3,1,2,150,150,76,1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    cursor = 0.0
    for group, weight in zip(groups, weights):
        span = duration * weight / total
        text = " ".join(group).replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{stamp(cursor)},{stamp(min(duration, cursor + span))},Default,,0,0,0,,{text}")
        cursor += span
    path.write_text("\n".join(lines), encoding="utf-8")


def fetch_section_videos(section, section_index, credits):
    api_key = required("PIXABAY_API_KEY")
    session = requests.Session()
    session.headers.update({"User-Agent": "CurioLoop Documentary/1.0"})
    found, seen = [], set()
    for query in section["visual_queries"]:
        response = session.get(
            "https://pixabay.com/api/videos/",
            params={"key": api_key, "q": query, "per_page": 40, "safesearch": "true"},
            timeout=35,
        )
        response.raise_for_status()
        query_terms = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) >= 4 and w.lower() not in GENERIC]
        hits = sorted(response.json().get("hits", []), key=lambda x: x.get("duration", 0), reverse=True)
        for item in hits:
            tags = (item.get("tags") or "").lower()
            matched = [term for term in query_terms if term in tags]
            if not matched or item.get("id") in seen:
                continue
            variants = item.get("videos", {})
            variant = variants.get("large") or variants.get("medium")
            if not variant or not variant.get("url"):
                continue
            path = WORK / f"scene_{section_index:02d}_{len(found)+1}.mp4"
            with session.get(variant["url"], stream=True, timeout=90) as download:
                download.raise_for_status()
                with path.open("wb") as output:
                    for chunk in download.iter_content(1024 * 1024):
                        if chunk:
                            output.write(chunk)
            # Normalize each licensed stock clip to a high-quality 16:9 intermediate.
            normalized = path.with_name(path.stem + "_hd.mp4")
            subprocess.run([
                "ffmpeg", "-y", "-i", str(path), "-vf",
                "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30,format=yuv420p",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-movflags", "+faststart", str(normalized)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
            path.unlink(missing_ok=True)
            found.append(normalized)
            seen.add(item.get("id"))
            credits.append(
                f"Pixabay contributor: {item.get('user','Not listed')} | "
                f"{item.get('pageURL','')} | Pixabay Content License (review current terms)"
            )
            if len(found) == 2:
                return found
    if len(found) < 2:
        raise RuntimeError(
            f"Insufficient topic-matched licensed video footage for section '{section['heading']}'. "
            "The documentary was stopped rather than filled with unrelated footage."
        )
    return found


def render_section(section, index, voice, videos):
    audio_duration = duration_of(voice)
    captions = WORK / f"captions_{index:02d}.ass"
    make_captions(section["narration"], audio_duration, captions)
    chapter = WORK / f"chapter_{index:02d}.txt"
    chapter.write_text(section["heading"].upper(), encoding="utf-8")
    output = WORK / f"section_{index:02d}.mp4"
    half = audio_duration / 2
    cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(videos[0]),
           "-stream_loop", "-1", "-i", str(videos[1]), "-i", str(voice)]
    filters = (
        f"[0:v]trim=duration={half:.3f},setpts=PTS-STARTPTS[v0];"
        f"[1:v]trim=duration={audio_duration-half:.3f},setpts=PTS-STARTPTS[v1];"
        "[v0][v1]concat=n=2:v=1:a=0,"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        f"textfile={chapter}:fontcolor=white:fontsize=30:"
        "x=82:y=66:box=1:boxcolor=0x101820@0.72:boxborderw=16,"
        f"subtitles={captions}:fontsdir=/usr/share/fonts/truetype/dejavu[vout]"
    )
    subprocess.run(cmd + [
        "-filter_complex", filters, "-map", "[vout]", "-map", "2:a:0",
        "-t", f"{audio_duration:.3f}", "-c:v", "libx264", "-preset", "medium",
        "-crf", "21", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(output)
    ], cwd=WORK, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
    return output


def upload_video(path, data, credits):
    creds = Credentials(
        None, refresh_token=required("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("YOUTUBE_CLIENT_ID"),
        client_secret=required("YOUTUBE_CLIENT_SECRET"), scopes=SCOPES,
    )
    youtube = build("youtube", "v3", credentials=creds)
    privacy = os.getenv("YOUTUBE_PRIVACY_STATUS", "unlisted").lower()
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("Invalid YOUTUBE_PRIVACY_STATUS.")
    sources = "\n".join(f"- {s}" for s in data["sources"])
    attribution = "\n".join(dict.fromkeys(credits))
    description = (
        data["description"] + "\n\nCHAPTERS\n" +
        "\n".join(f"{i+1}. {s['heading']}" for i, s in enumerate(data["sections"])) +
        "\n\nSOURCES\n" + sources +
        "\n\nVISUAL CREDITS\n" + attribution +
        "\n\nCurioLoop — Evidence-led stories about science, psychology, history and the world."
    )
    body = {
        "snippet": {
            "title": data["title"][:100], "description": description[:4900],
            "tags": ["CurioLoop", "Documentary", "Science", "Psychology", "History",
                     "Education", "Explained", "Knowledge"],
            "categoryId": "27",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    request = youtube.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024),
    )
    result = request.execute()
    print("Uploaded long-form video ID:", result.get("id"))
    print("Privacy status:", privacy)


def main():
    print("Generating CurioLoop evidence-led mini-documentary...")
    data = generate_documentary()
    print("Selected:", data["title"])
    print("Narration word count:", data["word_count"])
    (WORK / "documentary_script.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    credits, sections = [], []
    for index, section in enumerate(data["sections"], 1):
        print(f"Preparing chapter {index}/6: {section['heading']}")
        voice = asyncio.run(make_voice(section["narration"], index))
        videos = fetch_section_videos(section, index, credits)
        sections.append(render_section(section, index, voice, videos))
    concat_file = WORK / "concat.txt"
    concat_file.write_text("\n".join(f"file '{p.resolve()}'" for p in sections), encoding="utf-8")
    output = WORK / "curioloop_documentary.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c", "copy", "-movflags", "+faststart", str(output)
    ], cwd=WORK, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
    print("Rendered:", output, "| Duration:", round(duration_of(output), 1), "seconds")
    (WORK / "visual_credits.txt").write_text("\n".join(dict.fromkeys(credits)), encoding="utf-8")
    if os.getenv("OUTPUT_ONLY", "false").lower() in {"1", "true", "yes"}:
        print("OUTPUT_ONLY enabled; upload skipped.")
    else:
        upload_video(output, data, credits)
    print("Long-form production complete.")


if __name__ == "__main__":
    main()
