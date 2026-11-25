import json
import os
import re
import subprocess
from pathlib import Path
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

ROOT = Path(__file__).parent
WORK = ROOT / "work_fancamverse"
WORK.mkdir(exist_ok=True)
HISTORY = ROOT / "history" / "fancamverse_processed.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/youtube.upload"]
SUPPORTED = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm", "video/x-matroska"}

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub secret/environment variable: {name}")
    return value

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

def render_short(source):
    output = WORK / f"{source.stem}_fancamverse.mp4"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, check=True)
    duration = float(probe.stdout.strip())
    if duration <= 0 or duration > 180:
        raise RuntimeError(f"Video duration must be between 1 and 180 seconds; got {duration:.1f}s.")
    subprocess.run(["ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", "180", str(output)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return output

def generate_metadata(filename):
    model = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()
    prompt = f"""Create YouTube Shorts metadata for an English-language K-pop fan-cam channel named FANCAMVERSE.
Source video filename: {filename!r}
Use ONLY information clearly present in the filename. Translate Korean words/names into standard English spelling where reasonably clear, but never guess an idol, group, song, event, or venue. If details are unclear, use a neutral title such as 'K-pop Fan Cam | Live Stage Moment'.
Return valid JSON only with keys title, description, tags.
title: concise natural English, maximum 90 characters, searchable, not misleading.
description: one or two short English sentences; say this is fan-captured footage. Do not claim official affiliation or endorsement.
tags: 8 to 12 relevant English tags only about named group/member/song/performance and K-pop fancams. No unrelated trends."""
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
    channel_result = youtube.channels().list(part="snippet", mine=True).execute()
    channels = channel_result.get("items", [])
    if len(channels) != 1 or channels[0].get("snippet", {}).get("title", "").strip().casefold() != "fancamverse":
        found = [item.get("snippet", {}).get("title", "(unnamed)") for item in channels]
        raise RuntimeError(f"OAuth token is not connected to the FANCAMVERSE YouTube channel. Detected channel(s): {found}")
    print("YouTube channel verified: FANCAMVERSE")
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
        print(f"Processing Drive file: {item['name']} ({item['id']})")
        source = download_video(drive, item)
        rendered = render_short(source)
        title, description, tags = generate_metadata(item["name"])
        print(f"Prepared title: {title}")
        if output_only:
            print(f"OUTPUT_ONLY: rendered file ready at {rendered}; upload skipped and source remains unprocessed.")
            continue
        upload(youtube, rendered, title, description, tags, privacy)
        history["processed"].append(item["id"])
        history["processed"] = history["processed"][-2000:]
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        completed += 1
    print(f"FANCAMVERSE run finished: {completed} uploaded.")

if __name__ == "__main__":
    main()
