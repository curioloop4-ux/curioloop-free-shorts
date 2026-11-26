import base64
import json
import os
import re
import subprocess
import time
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

ROOT = Path(__file__).parent
WORK = ROOT / os.getenv("FANCAMVERSE_WORK_DIR", "work_curioloop_fancam")
WORK.mkdir(exist_ok=True)
HISTORY = ROOT / os.getenv("FANCAMVERSE_HISTORY_FILE", "history/curioloop_fancam_processed.json")
CHANNEL_BRAND = os.getenv("FANCAMVERSE_CHANNEL_BRAND", "CurioLoop").strip() or "CurioLoop"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
SUPPORTED = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm", "video/x-matroska"}
SHORTS_PER_SOURCE = 5
MIN_SHORT_SECONDS = 25
MAX_SHORT_SECONDS = 26
INTRO_SKIP_SECONDS = 10

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub secret/environment variable: {name}")
    return value

def gemini_post_with_retry(url, api_key, payload, timeout, task_name):
    """Retry transient Gemini API failures with exponential backoff."""
    retryable_statuses = {408, 429, 500, 502, 503, 504}
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=timeout
            )
            if response.status_code not in retryable_statuses:
                response.raise_for_status()
                return response
            if attempt == max_attempts:
                detail = (response.text or "")[:1200]
                raise RuntimeError(
                    f"Gemini {task_name} failed after {max_attempts} attempts "
                    f"(HTTP {response.status_code}): {detail}"
                )
            delay = min(60, 5 * (2 ** (attempt - 1))) + random.uniform(0, 2)
            print(
                f"Gemini {task_name} returned HTTP {response.status_code}; "
                f"retry {attempt + 1}/{max_attempts} in {delay:.1f}s."
            )
            time.sleep(delay)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            detail = (exc.response.text or "")[:1200] if exc.response is not None else str(exc)
            raise RuntimeError(
                f"Gemini {task_name} returned non-retryable HTTP {status}: {detail}"
            ) from exc
        except requests.RequestException as exc:
            if attempt == max_attempts:
                raise RuntimeError(
                    f"Gemini {task_name} request failed after {max_attempts} attempts: {exc}"
                ) from exc
            delay = min(60, 5 * (2 ** (attempt - 1))) + random.uniform(0, 2)
            print(
                f"Gemini {task_name} network error; "
                f"retry {attempt + 1}/{max_attempts} in {delay:.1f}s: {exc}"
            )
            time.sleep(delay)

def gemini_generate_with_fallback(primary_model, api_key, payload, timeout, task_name):
    """Try the primary model, then a lighter supported model after capacity errors."""
    fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
    models = list(dict.fromkeys([primary_model, fallback_model]))
    last_error = None
    for index, model in enumerate(models):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            if index:
                print(f"Gemini {task_name}: switching to fallback model {model}.")
            else:
                print(f"Gemini {task_name}: using model {model}.")
            return gemini_post_with_retry(
                url, api_key, payload, timeout, task_name
            )
        except RuntimeError as exc:
            last_error = exc
            message = str(exc)
            transient = any(
                f"HTTP {status}" in message
                for status in (408, 429, 500, 502, 503, 504)
            ) or "request failed after" in message
            if not transient or index == len(models) - 1:
                raise
            print(
                f"Gemini {task_name}: primary model {model} unavailable; "
                f"trying fallback model {fallback_model}."
            )
    raise last_error


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True)

def probe_duration(source, allow_short=False):
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source)])
    duration = float(result.stdout.strip())
    minimum = 3 if allow_short else MIN_SHORT_SECONDS
    if duration < minimum:
        raise RuntimeError(f"Source video is too short ({duration:.1f}s; minimum {minimum}s).")
    return duration

def verify_vertical_output(path):
    """Fail closed unless the rendered MP4 is exactly 1080x1920."""
    result = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(path)
    ])
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError(f"Rendered video has no video stream: {path.name}")
    width = int(streams[0].get("width", 0))
    height = int(streams[0].get("height", 0))
    if (width, height) != (1080, 1920):
        raise RuntimeError(
            f"Refusing upload: {path.name} is {width}x{height}, expected 1080x1920 (9:16)."
        )
    print(f"Verified vertical output: {path.name} | {width}x{height} | 9:16")


def make_services():
    drive_creds = Credentials(None, refresh_token=required("FANCAMVERSE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("FANCAMVERSE_CLIENT_ID"),
        client_secret=required("FANCAMVERSE_CLIENT_SECRET"),
        scopes=DRIVE_SCOPES)
    youtube_creds = Credentials(None, refresh_token=required("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("YOUTUBE_CLIENT_ID"),
        client_secret=required("YOUTUBE_CLIENT_SECRET"),
        scopes=YOUTUBE_SCOPES)
    return build("drive", "v3", credentials=drive_creds), build("youtube", "v3", credentials=youtube_creds)

def load_history():
    if not HISTORY.exists():
        return {"processed": []}
    data = json.loads(HISTORY.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("processed"), list):
        raise RuntimeError("history/fancamverse_processed.json has an invalid format.")
    return data

def list_source_videos(drive, folder_id):
    # Search the inbox recursively so files placed in artist subfolders are included.
    pending = [folder_id]
    visited = set()
    candidates = []
    while pending:
        parent_id = pending.pop(0)
        if parent_id in visited:
            continue
        visited.add(parent_id)
        page_token = None
        while True:
            params = {
                "q": f"'{parent_id}' in parents and trashed = false",
                "pageSize": 100,
                "orderBy": "createdTime",
                "fields": "nextPageToken,files(id,name,mimeType,createdTime,size)",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True
            }
            if page_token:
                params["pageToken"] = page_token
            result = drive.files().list(**params).execute()
            for item in result.get("files", []):
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    pending.append(item["id"])
                elif item.get("mimeType") in SUPPORTED:
                    candidates.append(item)
            page_token = result.get("nextPageToken")
            if not page_token:
                break
    candidates.sort(key=lambda f: (f.get("createdTime", ""), f.get("id", "")))
    print(f"Drive inbox scan: {len(candidates)} supported video(s) found across {len(visited)} folder(s).")
    if not candidates:
        folder = drive.files().get(fileId=folder_id, fields="id,name,mimeType").execute()
        print(f"Configured Drive inbox: {folder.get('name')} ({folder.get('id')})")
    else:
        for item in candidates:
            print(f"Drive source: {item['name']} | {item.get('mimeType')} | {item.get('size', 'unknown')} bytes")
    return candidates


def search_drive_target_videos(drive, terms):
    clauses = []
    for term in terms:
        escaped = term.replace("\\", "\\\\").replace("'", "\\'")
        clauses.append(f"name contains '{escaped}'")
    query = "trashed = false and (" + " or ".join(clauses) + ")"
    results = []
    page_token = None
    while True:
        params = {
            "q": query,
            "pageSize": 100,
            "orderBy": "createdTime",
            "fields": "nextPageToken,files(id,name,mimeType,createdTime,size,parents)",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "corpora": "allDrives"
        }
        if page_token:
            params["pageToken"] = page_token
        response = drive.files().list(**params).execute()
        results.extend(item for item in response.get("files", [])
                       if item.get("mimeType") in SUPPORTED)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    unique = {item["id"]: item for item in results}
    found = sorted(unique.values(), key=lambda item: (item.get("createdTime", ""), item.get("id", "")))
    print(f"Drive-wide filename search found {len(found)} supported video(s).")
    for item in found:
        print(f"Drive match: {item['name']} | {item.get('mimeType')} | {item.get('size', 'unknown')} bytes")
    return found

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
    # Never sample exactly at EOF: ffmpeg commonly exits with code 234 there.
    # Keep a one-second safety margin and skip isolated frames that cannot be decoded.
    last_sample = max(start, duration - 1.0)
    usable = max(1, last_sample - start)
    count = min(80, max(12, int(usable / 3)))
    times = [start + (usable * i / max(1, count - 1)) for i in range(count)]
    frames_dir = WORK / "analysis_frames"
    frames_dir.mkdir(exist_ok=True)
    frames = []
    for i, stamp in enumerate(times):
        path = frames_dir / f"frame_{i:03d}.jpg"
        if path.exists():
            path.unlink()
        result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{stamp:.2f}", "-i", str(source),
            "-frames:v", "1", "-vf", "scale=240:-1", "-q:v", "5", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and path.exists() and path.stat().st_size > 0:
            frames.append((stamp, Image.open(path).convert("RGB")))
        else:
            detail = (result.stderr or "").strip().splitlines()
            print(f"Skipping unreadable analysis frame at {stamp:.2f}s" +
                  (f": {detail[-1][:240]}" if detail else ""))
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
    # Full-source mode is opt-in; artist filtering alone still runs highlight analysis.
    if os.getenv("FANCAMVERSE_USE_FULL_SOURCE", "").strip().lower() in {"1", "true", "yes"}:
        print(f"Full-source mode enabled ({duration:.1f}s).")
        return [{"start": 0.0, "end": duration, "hook": "A Must-Watch K-Pop Moment"}]

    # For a short user-supplied clip under 15 seconds, preserve the full moment
    # as one Short instead of rejecting it or trying to invent five segments.
    if duration < MIN_SHORT_SECONDS:
        print(f"Short source detected ({duration:.1f}s); using the complete clip as one Short.")
        return [{"start": 0.0, "end": duration, "hook": "A Must-Watch K-Pop Moment"}]
    sheets, first_allowed = make_contact_sheets(source, duration)
    model = (os.getenv("GEMINI_MODEL") or "gemini-3.8-flash").strip()
    prompt = f"""You are selecting compelling moments from a user-recorded K-pop concert fancam for YouTube Shorts.
Source filename: {filename}
Full duration: {duration:.1f} seconds. The opening/intro before {first_allowed:.1f}s must be excluded.
The attached contact sheets show sampled frames with their exact source timestamps.
Choose exactly five DISTINCT, non-overlapping highlight windows. Prioritize visually engaging performance moments:
sharp choreography, expressive facial moments, memorable gestures, energetic stage presence, or strong audience-facing moments.
Avoid empty stage, waiting, walking to position, camera setup, long transitions, and repeated near-identical moments.
Each window must be 15 to 20 seconds long and fully inside the source video. Start at or after {first_allowed:.1f}s.
Use the timestamps on the images to estimate boundaries. Return JSON only:
{{"clips":[{{"start":12.0,"end":29.0,"hook":"A short natural English intro caption, 3-7 words"}}]}}
Exactly 5 clips. No markdown. Hooks must not invent names, song titles, event details, or claims not visible in the source."""
    parts = [{"text": prompt}]
    for path in sheets:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
            "data": base64.b64encode(path.read_bytes()).decode("ascii")}})
    response = gemini_generate_with_fallback(
        model, required("GEMINI_API_KEY"),
        {"contents": [{"parts": parts}],
         "generationConfig": {"temperature": 0.15, "responseMimeType": "application/json"}},
        timeout=180, task_name="highlight analysis"
    )
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
        raise RuntimeError(f"Highlight analysis returned {len(distinct)} valid distinct clips; exactly 5 are required. No upload was made.")
    return distinct

def render_clips(source, clips, source_stem, index_offset=0, display_name=""):
    outputs = []
    fontfile = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    # Fill a true 9:16 frame by center-cropping landscape footage horizontally.
    output_w, output_h = 1080, 1920
    print(f"Render aspect ratio for {source_stem}: 9:16 (center crop; 1080x1920)")

    import textwrap
    for position, clip in enumerate(clips, 1):
        index = position + index_offset
        output = WORK / f"{source_stem}_short_{index}.mp4"
        raw_hook = re.sub(r"\\s+", " ", str(clip.get("hook", "A Must-Watch Stage Moment"))).strip()
        wrapped_hook = textwrap.fill(raw_hook, width=25, max_lines=2, placeholder="…")

        # Intro hook: centered for 2.7 seconds, then disappears.
        intro_path = WORK / f"intro_overlay_{index}.png"
        intro = Image.new("RGBA", (output_w, output_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(intro)
        intro_font = ImageFont.truetype(fontfile, 46)
        lines = wrapped_hook.splitlines() or [wrapped_hook]
        line_boxes = [draw.textbbox((0, 0), line, font=intro_font, stroke_width=3) for line in lines]
        line_heights = [box[3] - box[1] for box in line_boxes]
        line_widths = [box[2] - box[0] for box in line_boxes]
        line_gap = 10
        text_w = max(line_widths)
        text_h = sum(line_heights) + line_gap * (len(lines) - 1)
        pad_x, pad_y = 28, 20
        box_w, box_h = text_w + pad_x * 2, text_h + pad_y * 2
        box_x = (output_w - box_w) // 2
        box_y = (output_h - box_h) // 2
        draw.rounded_rectangle((box_x, box_y, box_x + box_w, box_y + box_h),
                               radius=16, fill=(0, 0, 0, 145))
        current_y = box_y + pad_y
        for line, bbox, line_w, line_h in zip(lines, line_boxes, line_widths, line_heights):
            line_x = (output_w - line_w) // 2
            draw.text((line_x - bbox[0], current_y - bbox[1]), line,
                      font=intro_font, fill=(255, 255, 255, 255),
                      stroke_width=3, stroke_fill=(0, 0, 0, 255))
            current_y += line_h + line_gap
        intro.save(intro_path)

        # Persistent artist name at the top center.
        name_path = WORK / f"name_overlay_{index}.png"
        name_overlay = Image.new("RGBA", (output_w, output_h), (0, 0, 0, 0))
        if display_name:
            name_draw = ImageDraw.Draw(name_overlay)
            name_font = ImageFont.truetype(fontfile, 48)
            name_bbox = name_draw.textbbox((0, 0), display_name, font=name_font, stroke_width=2)
            name_w, name_h = name_bbox[2] - name_bbox[0], name_bbox[3] - name_bbox[1]
            px, py = 24, 14
            bx, by = (output_w - name_w - px * 2) // 2, 190
            name_draw.rounded_rectangle((bx, by, bx + name_w + px * 2, by + name_h + py * 2),
                                        radius=14, fill=(0, 0, 0, 125))
            name_draw.text((bx + px - name_bbox[0], by + py - name_bbox[1]), display_name,
                           font=name_font, fill=(255, 255, 255, 255),
                           stroke_width=2, stroke_fill=(0, 0, 0, 255))
        name_overlay.save(name_path)

        # Convert every source orientation to a true full-frame 9:16 Short.
        # Landscape footage is center-cropped horizontally; portrait footage is
        # scaled to fill. No blurred side/background canvas is added.
        filter_complex = (
            f"[0:v]scale={output_w}:{output_h}:force_original_aspect_ratio=increase,"
            f"crop={output_w}:{output_h},setsar=1[base];"
            f"[1:v]format=rgba[name];"
            f"[2:v]format=rgba[intro];"
            f"[base][name]overlay=0:0[namebase];"
            f"[namebase][intro]overlay=0:0:enable='between(t,0,2.7)'[v]"
        )
        run(["ffmpeg", "-y", "-ss", f"{clip['start']:.3f}", "-i", str(source),
            "-loop", "1", "-i", str(name_path),
            "-loop", "1", "-i", str(intro_path),
            "-t", f"{clip['end'] - clip['start']:.3f}",
            "-filter_complex", filter_complex, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "slow", "-crf", "16",
            "-profile:v", "high", "-level:v", "4.2",
            "-pix_fmt", "yuv420p", "-maxrate", "16M", "-bufsize", "24M",
            "-c:a", "aac", "-b:a", "256k",
            "-movflags", "+faststart", str(output)])
        verify_vertical_output(output)
        outputs.append((output, clip))
    return outputs

def identify_artist(filename):
    value = filename.casefold()
    if any(token in value for token in ("장원영", "wonyoung", "won young")):
        return "WONYOUNG", "IVE", [
            "Wonyoung", "IVE Wonyoung", "Wonyoung fancam", "IVE fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Wonyoung performance", CHANNEL_BRAND, "Shorts"
        ]
    if any(token in value for token in ("채령", "chaeryeong", "chae ryeong")):
        return "CHAERYEONG", "ITZY", [
            "Chaeryeong", "ITZY Chaeryeong", "Chaeryeong fancam", "ITZY fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Chaeryeong performance", "Shorts"
        ]
    if any(token in value for token in ("권은비", "eunbi", "kwon eunbi", "kwon eun-bi")):
        return "EUNBI", "SOLO", [
            "Kwon Eunbi", "Eunbi", "Kwon Eunbi fancam", "Eunbi fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Eunbi performance", CHANNEL_BRAND, "Shorts"
        ]
    if any(token in value for token in ("나띠", "natty")):
        return "NATTY", "KISS OF LIFE", [
            "Natty", "KISS OF LIFE", "KISS OF LIFE Natty", "Natty fancam",
            "KIOF", "K-pop", "K-pop fancam", "K-pop Shorts",
            "Korean idol", "female idol", CHANNEL_BRAND, "Shorts"
        ]
    if any(token in value for token in ("카리나", "karina")):
        return "KARINA", "aespa", [
            "Karina", "aespa Karina", "Karina fancam", "aespa fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Karina performance", "Shorts"
        ]
    if any(token in value for token in ("닝닝", "ningning")):
        return "NINGNING", "aespa", [
            "Ningning", "aespa Ningning", "Ningning fancam", "aespa fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Ningning performance", "Shorts"
        ]
    return "", "", []

def generate_metadata(filename, hook, index):
    artist, group, fixed_tags = identify_artist(filename)
    if artist == "WONYOUNG":
        title = "WONYOUNG (IVE) ✨ | Charismatic K-Pop Fancam #Shorts"
        description = ("WONYOUNG of IVE ✨ Enjoy this fan-captured performance and her "
                       f"stage presence.\n\nFan-captured footage. {CHANNEL_BRAND}.")
        tags = fixed_tags
    elif artist == "CHAERYEONG":
        title = "CHAERYEONG (ITZY) 🌙 | K-Pop Fancam #Shorts"
        description = ("CHAERYEONG of ITZY 🌙 Enjoy this fan-captured performance and "
                       f"her stage presence.\n\nFan-captured footage. {CHANNEL_BRAND}.")
        tags = fixed_tags
    elif artist == "EUNBI":
        title = "KWON EUNBI ✨ | K-Pop Fancam #Shorts"
        description = ("KWON EUNBI ✨ Enjoy this fan-captured performance and her "
                       f"stage presence.\n\nFan-captured footage. {CHANNEL_BRAND}.")
        tags = fixed_tags
    elif artist == "NATTY":
        title = "NATTY (KISS OF LIFE) 🔥 | K-Pop Fancam #Shorts"
        description = ("NATTY of KISS OF LIFE 🔥 Enjoy this fan-captured performance and "
                       f"her stage presence.\n\nFan-captured footage. {CHANNEL_BRAND}.")
        tags = fixed_tags
    else:
        model = (os.getenv("GEMINI_MODEL") or "gemini-3.8-flash").strip()
        prompt = f"""Create YouTube Shorts metadata for {CHANNEL_BRAND}, an English-language K-pop fan-cam channel targeting US viewers.
Source filename: {filename!r}
On-screen English hook: {hook!r}
Clip number: {index}
Use ONLY details clearly present in the filename and hook. Never guess an idol, group, song, event, or venue.
Return valid JSON only with keys title, description, tags.
title: concise natural English, maximum 90 characters, searchable and engaging.
description: one or two short English sentences; identify this as fan-captured footage. No official affiliation claim.
Do NOT include dates, years, resolution terms such as 4K, camera specs, or recording metadata in the title or description.
tags: 8 to 12 relevant English tags; no unrelated trending tags."""
        response = gemini_generate_with_fallback(
            model, required("GEMINI_API_KEY"),
            {"contents": [{"parts": [{"text": prompt}]}],
             "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}},
            timeout=90, task_name="metadata generation"
        )
        data = json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
        title = str(data.get("title", "")).strip()[:100]
        description = str(data.get("description", "")).strip()
        tags = data.get("tags", [])
        if not title or not isinstance(tags, list):
            raise RuntimeError("Gemini returned invalid upload metadata.")

    clean, seen = [], set()
    for tag in [CHANNEL_BRAND, "K-pop", "K-pop fancam", "Shorts"] + tags:
        tag = re.sub(r"\s+", " ", str(tag)).strip().lstrip("#")
        key = tag.casefold()
        if tag and key not in seen and len(tag) <= 60 and len(",".join(clean + [tag])) <= 480:
            clean.append(tag); seen.add(key)
    hashtags = " ".join("#" + re.sub(r"[^A-Za-z0-9]", "", tag) for tag in clean[:6])
    description = (description + "\n\n" if description else "") + hashtags
    return title, description[:5000], clean

def next_publish_at_kst(publish_time):
    try:
        hour, minute = [int(part) for part in publish_time.split(":", 1)]
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("FANCAMVERSE_PUBLISH_TIME_KST must use HH:MM format.")
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    publish_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # YouTube requires scheduled publication to be in the future.
    if publish_at <= now + timedelta(minutes=15):
        publish_at += timedelta(days=1)
    return publish_at.isoformat(timespec="seconds")


def upload(youtube, path, title, description, tags, privacy, publish_at=None):
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("Privacy must be private, unlisted, or public.")
    if publish_at and privacy != "private":
        raise ValueError("Scheduled YouTube publication requires private privacy status.")
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = publish_at
    body = {"snippet": {"title": title, "description": description, "tags": tags, "categoryId": "10"},
        "status": status}
    result = youtube.videos().insert(part="snippet,status", body=body,
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)).execute()
    video_id = result.get("id")
    if not video_id:
        raise RuntimeError("YouTube upload completed without returning a video ID.")
    if publish_at:
        print(f"Uploaded and scheduled: https://www.youtube.com/shorts/{video_id} (publishAt={publish_at})")
    else:
        print(f"Uploaded: https://www.youtube.com/shorts/{video_id} (privacy={privacy})")
    return video_id

def main():
    drive, youtube = make_services()
    folder_id = required("FANCAMVERSE_DRIVE_FOLDER_ID")
    history = load_history()
    source_state = history.setdefault("sources", {})
    for old_id in history.get("processed", []):
        source_state.setdefault(old_id, {"next_clip": SHORTS_PER_SOURCE + 1, "complete": True})
    # Migration: any source that has already consumed a clip in the old
    # sequential scheme is treated as used, so it will never be repeated.
    for state in source_state.values():
        if int(state.get("next_clip", 1)) > 1:
            state["complete"] = True

    files = list_source_videos(drive, folder_id)
    source_filter = os.getenv("FANCAMVERSE_SOURCE_FILTER", "").strip()
    if source_filter:
        raw_terms = [term.strip() for term in re.split(r"[,;]", source_filter) if term.strip()]
        terms = [term.casefold() for term in raw_terms]
        files = [item for item in files if any(term in item["name"].casefold() for term in terms)]
        if not files:
            print("Requested artists were not in the configured inbox; searching accessible Drive files by filename.")
            files = search_drive_target_videos(drive, raw_terms)
            files = [item for item in files if any(term in item["name"].casefold() for term in terms)]
        print(f"Manual source filter matched {len(files)} Drive video(s): " +
              ", ".join(item["name"] for item in files))
        required_order = [x.strip().upper() for x in os.getenv("FANCAMVERSE_REQUIRED_ARTISTS", "").split(",") if x.strip()]
        if required_order:
            found_artists = {identify_artist(item["name"])[0] for item in files}
            missing = set(required_order) - found_artists
            if missing:
                raise RuntimeError(f"Missing requested source artist(s): {', '.join(sorted(missing))}.")
            files = [next(item for item in files if identify_artist(item["name"])[0] == artist)
                     for artist in required_order]
    excluded_terms = [
        term.strip().casefold()
        for term in os.getenv("FANCAMVERSE_EXCLUDE_ARTISTS", "김채원,chaewon").split(",")
        if term.strip()
    ]
    if excluded_terms:
        before_exclusion = len(files)
        files = [
            item for item in files
            if not any(term in item.get("name", "").casefold() for term in excluded_terms)
        ]
        print(f"Excluded {before_exclusion - len(files)} file(s) matching blocked artist names.")

    if not files:
        print("No eligible videos found in the configured Google Drive inbox.")
        return

    # Randomly select from unused source videos. A source is consumed after
    # one successful Short, preventing any original video from being repeated.
    batch_size = int(os.getenv("FANCAMVERSE_UPLOADS_PER_RUN", "1"))
    output_only = os.getenv("FANCAMVERSE_OUTPUT_ONLY", "false").lower() in {"1", "true", "yes"}
    privacy = os.getenv("FANCAMVERSE_PRIVACY_STATUS", "private").lower()
    publish_time = os.getenv("FANCAMVERSE_PUBLISH_TIME_KST", "").strip()
    publish_at = next_publish_at_kst(publish_time) if publish_time and not output_only else None
    if publish_at:
        privacy = "private"
        print(f"Scheduled YouTube publication for {publish_at} (Asia/Seoul).")
    if not output_only:
        print("Upload destination: CurioLoop, using the existing YOUTUBE_* OAuth token.")

    uploaded_count = 0

    for batch_number in range(1, batch_size + 1):
        eligible = [
            item for item in files
            if not source_state.get(item["id"], {}).get("complete", False)
            and int(source_state.get(item["id"], {}).get("next_clip", 1)) <= SHORTS_PER_SOURCE
        ]
        if not eligible:
            print("No unused source videos remain in the current Drive inbox.")
            break

        item = random.choice(eligible)
        state = source_state.get(item["id"], {"next_clip": 1, "complete": False})
        print(
            f"Batch {batch_number}/{batch_size} | Random unused source: "
            f"{item['name']} ({item['id']})"
        )

        source = download_video(drive, item)
        duration = probe_duration(source, allow_short=bool(source_filter))
        saved_clips = state.get("clips")
        if saved_clips and len(saved_clips) == SHORTS_PER_SOURCE:
            clips = saved_clips
            print("Using the saved five non-overlapping highlight windows for this source.")
        else:
            clips = analyze_highlights(source, duration, item["name"])

        used_indices = set(int(x) for x in state.get("used_clip_indices", []))
        available_indices = [i for i in range(1, len(clips) + 1) if i not in used_indices]
        if not available_indices:
            print(f"All highlight windows already used for {item['name']}; skipping this source.")
            source_state[item["id"]] = {**state, "name": item["name"], "complete": True}
            continue
        selected_index = random.choice(available_indices)
        clip = clips[selected_index - 1]
        artist_name, _, _ = identify_artist(item["name"])
        # Use the artist name for unique artifact filenames. Korean Drive filenames
        # are sanitized to the same fallback stem ("mp4"), which would overwrite
        # the first artist's rendered file when both are processed in one run.
        output_stem = artist_name or source.stem
        rendered = render_clips(source, [clip], output_stem, index_offset=selected_index - 1,
                                display_name=artist_name)
        path, clip = rendered[0]
        title, description, tags = generate_metadata(item["name"], clip["hook"], selected_index)
        print(
            f"Selected Short {selected_index}: {clip['start']:.1f}-{clip['end']:.1f}s | {title}"
        )

        if output_only:
            print(f"OUTPUT_ONLY: preview ready at {path}; upload skipped.")
            # Preview mode does not consume a clip or advance rotation history.
            continue

        video_id = upload(youtube, path, title, description, tags, privacy, publish_at=publish_at)
        uploaded_count += 1

        source_state[item["id"]] = {
            "name": item["name"],
            "next_clip": SHORTS_PER_SOURCE + 1,
            "complete": True,
            "clips": clips,
            "used_clip_indices": sorted(used_indices | {selected_index})
        }
        history["last_source_id"] = item["id"]
        history["sources"] = source_state
        history["processed"] = [
            sid for sid, state in source_state.items() if state.get("complete")
        ][-2000:]
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(
            json.dumps(history, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )
        print(f"Recorded successful upload {video_id}; progress saved immediately.")

    if output_only:
        print("OUTPUT_ONLY run finished; no uploads or progress changes were made.")
    else:
        print(f"CurioLoop batch complete: {uploaded_count}/{batch_size} Shorts uploaded.")

if __name__ == "__main__":
    main()
