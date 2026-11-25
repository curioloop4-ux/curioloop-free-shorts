import base64
import json
import os
import re
import subprocess
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

ROOT = Path(__file__).parent
WORK = ROOT / "work_fancamverse"
WORK.mkdir(exist_ok=True)
HISTORY = ROOT / "history" / "fancamverse_processed.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/youtube.upload"]
SUPPORTED = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm", "video/x-matroska"}
SHORTS_PER_SOURCE = 4
MIN_SHORT_SECONDS = 15
MAX_SHORT_SECONDS = 20
INTRO_SKIP_SECONDS = 10

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub secret/environment variable: {name}")
    return value

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True)

def probe_duration(source):
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source)])
    duration = float(result.stdout.strip())
    if duration < MIN_SHORT_SECONDS:
        raise RuntimeError(f"Source video is too short ({duration:.1f}s).")
    return duration

def make_services():
    creds = Credentials(None, refresh_token=required("FANCAMVERSE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("FANCAMVERSE_CLIENT_ID"), client_secret=required("FANCAMVERSE_CLIENT_SECRET"),
        scopes=SCOPES)
    return build("drive", "v3", credentials=creds), build("youtube", "v3", credentials=creds)

def load_history():
    if not HISTORY.exists():
        return {"processed": []}
    data = json.loads(HISTORY.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("processed"), list):
        raise RuntimeError("history/fancamverse_processed.json has an invalid format.")
    return data

def list_new_videos(drive, folder_id, processed, limit):
    result = drive.files().list(q=f"'{folder_id}' in parents and trashed = false",
        pageSize=100, orderBy="createdTime",
        fields="files(id,name,mimeType,createdTime,size)",
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    candidates = [f for f in result.get("files", [])
        if f.get("mimeType") in SUPPORTED and f.get("id") not in processed]
    candidates.sort(key=lambda f: f.get("createdTime", ""))
    return candidates[:limit]

def download_video(drive, item):
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", item["name"]).strip("._") or "fancam_video"
    source = WORK / safe_name
    request = drive.files().get_media(fileId=item["id"], supportsAllDrives=True)
    with source.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"Drive download: {int(status.progress() * 100)}%")
    if source.stat().st_size == 0:
        raise RuntimeError(f"Downloaded file is empty: {item['name']}")
    return source

def make_contact_sheets(source, duration):
    # Sample the entire performance sparsely, while excluding the opening section.
    start = min(INTRO_SKIP_SECONDS, max(3, duration * 0.08))
    usable = max(1, duration - start)
    count = min(80, max(12, int(usable / 3)))
    times = [start + (usable * i / max(1, count - 1)) for i in range(count)]
    frames_dir = WORK / "analysis_frames"
    frames_dir.mkdir(exist_ok=True)
    frames = []
    for i, stamp in enumerate(times):
        path = frames_dir / f"frame_{i:03d}.jpg"
        subprocess.run(["ffmpeg", "-y", "-ss", f"{stamp:.2f}", "-i", str(source),
            "-frames:v", "1", "-vf", "scale=240:-1", "-q:v", "5", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if path.exists():
            frames.append((stamp, Image.open(path).convert("RGB")))
    if not frames:
        raise RuntimeError("Could not sample frames for highlight analysis.")
    sheets = []
    font = ImageFont.load_default()
    for page in range(0, len(frames), 20):
        batch = frames[page:page + 20]
        cell_w, cell_h, label_h = 240, 160, 24
        sheet = Image.new("RGB", (cell_w * 4, (cell_h + label_h) * 5), "black")
        draw = ImageDraw.Draw(sheet)
        for j, (stamp, frame) in enumerate(batch):
            x, y = (j % 4) * cell_w, (j // 4) * (cell_h + label_h)
            frame.thumbnail((cell_w, cell_h))
            sheet.paste(frame, (x, y))
            draw.text((x + 5, y + cell_h + 4), f"{stamp:.1f}s", fill="white", font=font)
        out = WORK / f"contact_{page // 20:02d}.jpg"
        sheet.save(out, quality=82)
        sheets.append(out)
    return sheets, start

def analyze_highlights(source, duration, filename):
    sheets, first_allowed = make_contact_sheets(source, duration)
    model = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()
    prompt = f"""You are selecting compelling moments from a user-recorded K-pop concert fancam for YouTube Shorts.
Source filename: {filename}
Full duration: {duration:.1f} seconds. The opening/intro before {first_allowed:.1f}s must be excluded.
The attached contact sheets show sampled frames with their exact source timestamps.
Choose exactly four DISTINCT, non-overlapping highlight windows. Prioritize visually engaging performance moments:
sharp choreography, expressive facial moments, memorable gestures, energetic stage presence, or strong audience-facing moments.
Avoid empty stage, waiting, walking to position, camera setup, long transitions, and repeated near-identical moments.
Each window must be 15 to 20 seconds long and fully inside the source video. Start at or after {first_allowed:.1f}s.
Use the timestamps on the images to estimate boundaries. Return JSON only:
{{"clips":[{{"start":12.0,"end":29.0,"hook":"A short natural English intro caption, 3-7 words"}}]}}
Exactly 4 clips. No markdown. Hooks must not invent names, song titles, event details, or claims not visible in the source."""
    parts = [{"text": prompt}]
    for path in sheets:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
            "data": base64.b64encode(path.read_bytes()).decode("ascii")}})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = requests.post(url, params={"key": required("GEMINI_API_KEY")},
        json={"contents": [{"parts": parts}],
              "generationConfig": {"temperature": 0.15, "responseMimeType": "application/json"}},
        timeout=180)
    response.raise_for_status()
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(raw)
    clips = data.get("clips", [])
    valid = []
    for clip in clips:
        try:
            start, end = float(clip["start"]), float(clip["end"])
        except (KeyError, TypeError, ValueError):
            continue
        length = end - start
        if start >= first_allowed and end <= duration and MIN_SHORT_SECONDS <= length <= MAX_SHORT_SECONDS:
            valid.append({"start": start, "end": end,
                "hook": re.sub(r"[^A-Za-z0-9 '!?-]", "", str(clip.get("hook", "A Must-Watch Stage Moment")))[:55]})
    valid.sort(key=lambda x: x["start"])
    distinct = []
    for clip in valid:
        if all(clip["start"] >= old["end"] or clip["end"] <= old["start"] for old in distinct):
            distinct.append(clip)
    if len(distinct) != SHORTS_PER_SOURCE:
        raise RuntimeError(f"Highlight analysis returned {len(distinct)} valid distinct clips; exactly 4 are required. No upload was made.")
    return distinct

def render_clips(source, clips, source_stem):
    outputs = []
    fontfile = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    for index, clip in enumerate(clips, 1):
        output = WORK / f"{source_stem}_short_{index}.mp4"
        hook_file = WORK / f"hook_{index}.txt"
        hook_file.write_text(clip["hook"], encoding="utf-8")
        vf = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            f"drawtext=fontfile={fontfile}:textfile={hook_file}:reload=0:"
            "fontcolor=white:fontsize=58:borderw=3:bordercolor=black:"
            "box=1:boxcolor=black@0.48:boxborderw=22:"
            "x=(w-text_w)/2:y=h*0.13:"
            "enable='between(t,0,2.7)'"
        )
        run(["ffmpeg", "-y", "-ss", f"{clip['start']:.3f}", "-i", str(source),
            "-t", f"{clip['end'] - clip['start']:.3f}", "-map", "0:v:0", "-map", "0:a?",
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(output)])
        outputs.append((output, clip))
    return outputs

def generate_metadata(filename, hook, index):
    model = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()
    prompt = f"""Create YouTube Shorts metadata for FANCAMVERSE, an English-language K-pop fan-cam channel targeting US viewers.
Source filename: {filename!r}
On-screen English hook: {hook!r}
Clip number: {index}
Use ONLY details clearly present in the filename and hook. Never guess an idol, group, song, event, or venue.
Return valid JSON only with keys title, description, tags.
title: concise natural English, maximum 90 characters, searchable and engaging.
description: one or two short English sentences; identify this as fan-captured footage. No official affiliation claim.
tags: 8 to 12 relevant English tags; no unrelated trending tags."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = requests.post(url, params={"key": required("GEMINI_API_KEY")},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}},
        timeout=90)
    response.raise_for_status()
    data = json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
    title = str(data.get("title", "")).strip()[:100]
    description = str(data.get("description", "")).strip()
    tags = data.get("tags", [])
    if not title or not isinstance(tags, list):
        raise RuntimeError("Gemini returned invalid upload metadata.")
    clean, seen = [], set()
    for tag in ["FANCAMVERSE", "K-pop", "K-pop fancam", "Shorts"] + tags:
        tag = re.sub(r"\s+", " ", str(tag)).strip().lstrip("#")
        key = tag.casefold()
        if tag and key not in seen and len(tag) <= 60 and len(",".join(clean + [tag])) <= 480:
            clean.append(tag); seen.add(key)
    hashtags = " ".join("#" + re.sub(r"[^A-Za-z0-9]", "", tag) for tag in clean[:4])
    description = (description + "\n\n" if description else "") + "Fan-captured performance footage. FANCAMVERSE.\n\n" + hashtags
    return title, description[:5000], clean

def upload(youtube, path, title, description, tags, privacy):
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("Privacy must be private, unlisted, or public.")
    body = {"snippet": {"title": title, "description": description, "tags": tags, "categoryId": "10"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    result = youtube.videos().insert(part="snippet,status", body=body,
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)).execute()
    video_id = result.get("id")
    if not video_id:
        raise RuntimeError("YouTube upload completed without returning a video ID.")
    print(f"Uploaded: https://www.youtube.com/shorts/{video_id} (privacy={privacy})")
    return video_id

def main():
    drive, youtube = make_services()
    folder_id = required("FANCAMVERSE_DRIVE_FOLDER_ID")
    history = load_history()
    processed = set(history["processed"])
    limit = max(1, min(int(os.getenv("FANCAMVERSE_MAX_FILES", "1")), 5))
    files = list_new_videos(drive, folder_id, processed, limit)
    if not files:
        print("No new supported videos found in the FANCAMVERSE Drive inbox.")
        return
    output_only = os.getenv("FANCAMVERSE_OUTPUT_ONLY", "true").lower() in {"1", "true", "yes"}
    privacy = os.getenv("FANCAMVERSE_PRIVACY_STATUS", "unlisted").lower()
    completed = 0
    for item in files:
        print(f"Processing long-form source: {item['name']} ({item['id']})")
        source = download_video(drive, item)
        duration = probe_duration(source)
        clips = analyze_highlights(source, duration, item["name"])
        rendered = render_clips(source, clips, source.stem)
        for index, (path, clip) in enumerate(rendered, 1):
            title, description, tags = generate_metadata(item["name"], clip["hook"], index)
            print(f"Short {index}: {clip['start']:.1f}-{clip['end']:.1f}s | {title}")
            if output_only:
                print(f"OUTPUT_ONLY: preview ready at {path}; upload skipped.")
                continue
            upload(youtube, path, title, description, tags, privacy)
        if not output_only:
            history["processed"].append(item["id"])
            history["processed"] = history["processed"][-2000:]
            HISTORY.parent.mkdir(parents=True, exist_ok=True)
            HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            completed += 1
    print(f"FANCAMVERSE finished: {completed} source video(s) fully processed.")

if __name__ == "__main__":
    main()
