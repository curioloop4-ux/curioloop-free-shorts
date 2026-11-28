import base64
import json
import os
import re
import subprocess
import time
import random
import difflib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

ROOT = Path(__file__).parent
WORK = ROOT / os.getenv("FANCAMVERSE_WORK_DIR", "work_fancamverse")
WORK.mkdir(exist_ok=True)
HISTORY = ROOT / os.getenv("FANCAMVERSE_HISTORY_FILE", "history/fancamverse_processed.json")
CHANNEL_BRAND = os.getenv("FANCAMVERSE_CHANNEL_BRAND", "FANCAMVERSE").strip() or "FANCAMVERSE"
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
    """Fail closed unless the rendered MP4 is exactly 1080x1800 (9:15)."""
    result = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(path)
    ])
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError(f"Rendered video has no video stream: {path.name}")
    width = int(streams[0].get("width", 0))
    height = int(streams[0].get("height", 0))
    if (width, height) != (1080, 1800):
        raise RuntimeError(
            f"Refusing upload: {path.name} is {width}x{height}, expected 1080x1800 (9:15)."
        )
    print(f"Verified vertical output: {path.name} | {width}x{height} | 9:15")


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

def validate_upload_destination_config():
    # Keep the configured destination ID as a required deployment setting,
    # but do not call channels.list(mine=True): FANCAMVERSE uses an existing
    # youtube.upload-only refresh token, and that endpoint requires read scope.
    expected_id = required("YOUTUBE_EXPECTED_CHANNEL_ID")
    print(
        "Upload destination configured: FANCAMVERSE "
        f"(expected channel ID {expected_id}); using its dedicated OAuth token."
    )
    return expected_id


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
    mime_counts = {}
    ignored_files = []
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
                mime = item.get("mimeType", "unknown")
                mime_counts[mime] = mime_counts.get(mime, 0) + 1
                if mime == "application/vnd.google-apps.folder":
                    pending.append(item["id"])
                elif mime in SUPPORTED:
                    candidates.append(item)
                else:
                    ignored_files.append(item)
            page_token = result.get("nextPageToken")
            if not page_token:
                break
    candidates.sort(key=lambda f: (f.get("createdTime", ""), f.get("id", "")))
    print(f"Drive inbox scan: {len(candidates)} supported video(s) found across {len(visited)} folder(s).")
    folder = drive.files().get(fileId=folder_id, fields="id,name,mimeType").execute()
    print(
        f"Configured Drive inbox: {folder.get('name')} "
        f"({folder.get('id')}) | mimeType={folder.get('mimeType')}"
    )
    print(f"Drive MIME summary: {json.dumps(mime_counts, ensure_ascii=False, sort_keys=True)}")
    for item in ignored_files:
        print(
            f"Drive ignored unsupported file: {item.get('name')} | "
            f"{item.get('mimeType')} | {item.get('size', 'unknown')} bytes"
        )
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

def target_clips_for_duration(duration):
    """Choose how many 25-second Shorts to extract from each source."""
    if duration < 60:
        return 1
    if duration < 90:
        return 2
    if duration < 150:
        return 3
    if duration < 240:
        return 4
    return 5


def fallback_highlights(duration, reason="Gemini unavailable"):
    """Create evenly distributed, non-overlapping 25-second clips when AI analysis fails."""
    target_count = target_clips_for_duration(duration)
    clip_length = MIN_SHORT_SECONDS
    first_allowed = min(INTRO_SKIP_SECONDS, max(0.0, duration - clip_length))
    last_start = max(0.0, duration - clip_length)
    if target_count == 1:
        starts = [first_allowed]
    else:
        span = max(0.0, last_start - first_allowed)
        starts = [first_allowed + span * i / (target_count - 1) for i in range(target_count)]
    clips = [
        {"start": round(start, 2), "end": round(start + clip_length, 2),
         "hook": "A Must-Watch Performance Moment"}
        for start in starts
    ]
    print(f"Using {len(clips)} evenly distributed fallback clip(s): {reason}.")
    return clips


def parse_gemini_json(response, task_name):
    """Extract JSON safely from Gemini responses, including fenced or wrapped output."""
    try:
        raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Gemini {task_name} response did not contain text.") from exc
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.IGNORECASE)
    # Gemini occasionally appends a stray suffix (e.g. 136.7k) to a numeric
    # timestamp. Strip only a trailing k immediately before JSON delimiters.
    cleaned = re.sub(r"(?<=\d)k(?=\s*[,}\]])", "", cleaned, flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    # Some model responses include a short preamble or trailing text. Extract the
    # first balanced JSON object without altering its contents.
    start = cleaned.find("{")
    if start >= 0:
        depth, quoted, escaped = 0, False, False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start:index + 1]
                    try:
                        value = json.loads(candidate)
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        break
    preview = raw[:350].replace("\n", " ")
    raise RuntimeError(f"Gemini {task_name} returned malformed JSON: {preview}")


def is_standard_sakura_source(filename):
    """Target only the regular Sakura source, not a separately labeled B version."""
    name = Path(filename).stem.casefold()
    is_sakura = "사쿠라" in name or "sakura" in name
    is_b_version = bool(re.search(r"(?:^|[\s_\-\[(])(?:ver(?:sion)?[\s_\-]*)?b(?:$|[\s_\-\])])", name))
    return is_sakura and not is_b_version


def analyze_highlights(source, duration, filename):
    target_count = target_clips_for_duration(duration)
    # Full-source mode is opt-in; artist filtering alone still runs highlight analysis.
    if os.getenv("FANCAMVERSE_USE_FULL_SOURCE", "").strip().lower() in {"1", "true", "yes"}:
        print(f"Full-source mode enabled ({duration:.1f}s).")
        return [{"start": 0.0, "end": duration, "hook": "A Must-Watch K-Pop Moment"}]

    # For a source shorter than one Short, preserve the complete source.
    if duration < MIN_SHORT_SECONDS:
        print(f"Short source detected ({duration:.1f}s); using the complete clip as one Short.")
        return [{"start": 0.0, "end": duration, "hook": "A Must-Watch K-Pop Moment"}]
    sheets, first_allowed = make_contact_sheets(source, duration)
    model = (os.getenv("GEMINI_MODEL") or "gemini-3.8-flash").strip()
    prompt = f"""You are selecting compelling moments from a user-recorded K-pop concert fancam for YouTube Shorts.
Source filename: {filename}
Full duration: {duration:.1f} seconds. The opening/intro before {first_allowed:.1f}s must be excluded.
The attached contact sheets show sampled frames with their exact source timestamps.
If and only if this filename is the regular Sakura (SAKURA/사쿠라) source and is not a B-version, prioritize the source segment from 67 to 72 seconds (1:07-1:12): at least one selected 25-26 second clip must contain that entire five-second segment, provided the source is long enough and the segment is valid. Do not apply this timing preference to any other artist or to Sakura B-version.
Choose exactly {target_count} DISTINCT, non-overlapping highlight windows. Prioritize visually engaging performance moments:
sharp choreography, expressive facial moments, memorable gestures, energetic stage presence, or strong audience-facing moments.
Avoid empty stage, waiting, walking to position, camera setup, long transitions, and repeated near-identical moments.
Each window must be {MIN_SHORT_SECONDS} to {MAX_SHORT_SECONDS} seconds long and fully inside the source video. Start at or after {first_allowed:.1f}s.
Use the timestamps on the images to estimate boundaries. Return JSON only:
{{"clips":[{{"start":12.0,"end":29.0,"hook":"A short natural English intro caption, 3-7 words"}}]}}
Exactly {target_count} clips. No markdown. Hooks must not invent names, song titles, event details, or claims not visible in the source."""
    parts = [{"text": prompt}]
    for path in sheets:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
            "data": base64.b64encode(path.read_bytes()).decode("ascii")}})
    try:
        response = gemini_generate_with_fallback(
            model, required("GEMINI_API_KEY"),
            {"contents": [{"parts": parts}],
             "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}},
            timeout=180, task_name="highlight analysis"
        )
        data = parse_gemini_json(response, "highlight analysis")
    except Exception as exc:
        fallback = fallback_highlights(duration, f"AI analysis error: {exc}")
        if is_standard_sakura_source(filename) and duration >= 72:
            start = min(60.0, max(0.0, duration - MIN_SHORT_SECONDS))
            fallback[0] = {
                "start": round(start, 2),
                "end": round(start + MIN_SHORT_SECONDS, 2),
                "hook": "Sakura Performance Highlight"
            }
        return fallback
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
    if is_standard_sakura_source(filename) and duration >= 72:
        target_window_clips = [
            clip for clip in valid
            if clip["start"] <= 67.0 and clip["end"] >= 72.0
        ]
        if not target_window_clips:
            print("Regular Sakura source: AI did not return a clip containing 1:07-1:12; using a 25-second window anchored on that segment.")
            start = min(60.0, max(0.0, duration - MIN_SHORT_SECONDS))
            valid.append({
                "start": start,
                "end": start + MIN_SHORT_SECONDS,
                "hook": "Sakura Performance Highlight"
            })
    valid.sort(key=lambda x: x["start"])
    distinct = []
    for clip in valid:
        if all(clip["start"] >= old["end"] or clip["end"] <= old["start"] for old in distinct):
            distinct.append(clip)
    if not distinct:
        return fallback_highlights(duration, "AI returned no valid highlight windows")
    if len(distinct) < target_count:
        print(
            f"Highlight analysis found {len(distinct)} valid non-overlapping clips "
            f"(requested {target_count}); continuing with the available clips."
        )
    return distinct

def render_clips(source, clips, source_stem, index_offset=0, display_name=""):
    outputs = []
    fontfile = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    # Render at 9:15. Anchor the crop at the top so excess footage is removed from the bottom.
    output_w, output_h = 1080, 1800
    print(f"Render aspect ratio for {source_stem}: 9:15 (top-aligned crop; 1080x1800)")

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

        # Transparent typographic CTA overlay: top prompt + bottom subscription line.
        cta_path = WORK / f"cta_overlay_{index}.png"
        cta = Image.new("RGBA", (output_w, output_h), (0, 0, 0, 0))
        cta_draw = ImageDraw.Draw(cta)
        cta_font = ImageFont.truetype(fontfile, 42)
        cta_bold = ImageFont.truetype(fontfile, 36)

        def draw_cta_line(text, y, font, fill):
            box = cta_draw.textbbox((0, 0), text, font=font, stroke_width=3)
            text_w = box[2] - box[0]
            pad_x, pad_y = 28, 18
            left = (output_w - text_w) // 2 - pad_x
            top = y - pad_y
            right = (output_w + text_w) // 2 + pad_x
            bottom = y + (box[3] - box[1]) + pad_y
            cta_draw.rounded_rectangle((left, top, right, bottom), radius=24,
                                       fill=(5, 8, 20, 155))
            cta_draw.text(((output_w - text_w) / 2, y - box[1]), text,
                          font=font, fill=fill, stroke_width=3,
                          stroke_fill=(5, 5, 12, 255))

        # Keep all three CTA lines together in the visual center for the final 3 seconds.
        # The first two lines are white; the subscription line is cyan.
        draw_cta_line("WHO SHOULD WE FEATURE NEXT?", 760, cta_font,
                      (255, 255, 255, 255))
        draw_cta_line("COMMENT BELOW!", 830, cta_bold,
                      (255, 255, 255, 255))
        draw_cta_line("SUBSCRIBE FOR MORE K-POP!", 900, cta_bold,
                      (75, 224, 255, 255))
        cta.save(cta_path)

        # Convert every source orientation to a full-frame 9:15 Short.
        # Landscape sources are cropped symmetrically from the exact horizontal
        # center; portrait sources naturally have no horizontal excess after scaling.
        # Keep the vertical crop top-aligned to trim the bottom edge.
        # No blurred side/background canvas is added.
        filter_complex = (
            f"[0:v]scale={output_w}:{output_h}:force_original_aspect_ratio=increase,"
            f"crop={output_w}:{output_h}:(in_w-{output_w})/2:0,setsar=1[base];"
            f"[1:v]format=rgba[name];"
            f"[2:v]format=rgba[intro];"
            f"[base][name]overlay=0:0[namebase];"
            f"[namebase][intro]overlay=0:0:enable='between(t,0,2.7)'[withintro];"
            f"[3:v]format=rgba,fade=t=in:st={max(0.0, clip['end'] - clip['start'] - 3.0):.3f}:d=0.4:alpha=1[cta];"
            f"[withintro][cta]overlay=0:0:enable='between(t,{max(0.0, clip['end'] - clip['start'] - 3.0):.3f},{clip['end'] - clip['start']:.3f})'[v]"
        )
        run(["ffmpeg", "-y", "-ss", f"{clip['start']:.3f}", "-i", str(source),
            "-loop", "1", "-i", str(name_path),
            "-loop", "1", "-i", str(intro_path),
            "-loop", "1", "-i", str(cta_path),
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
    value = Path(filename).stem.casefold()
    # Explicit high-priority aliases for common Latin/Korean filename variants.
    if any(token in value for token in ("blackpink jennie", "jennie", "제니")):
        return "JENNIE", "BLACKPINK", [
            "Jennie", "BLACKPINK Jennie", "Jennie fancam", "BLACKPINK fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Jennie performance", "Shorts"
        ]
    if any(token in value for token in ("사쿠라", "sakura", "miyawaki")):
        return "SAKURA", "LE SSERAFIM", [
            "Sakura", "LE SSERAFIM Sakura", "Sakura fancam", "LE SSERAFIM fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Shorts"
        ]
    if any(token in value for token in ("키야", "kya", "kiiiiikiii")):
        return "KYA", "KiiiKiii", [
            "KYA", "KiiiKiii KYA", "KiiiKiii fancam", "K-pop",
            "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Shorts"
        ]
    if any(token in value for token in ("장원영", "wonyoung", "won young")):
        return "WONYOUNG", "IVE", [
            "Wonyoung", "IVE Wonyoung", "Wonyoung fancam", "IVE fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Wonyoung performance", "Shorts"
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
            "female idol", "Eunbi performance", "Shorts"
        ]
    if any(token in value for token in ("은하", "eunha")):
        return "EUNHA", "VIVIZ", [
            "Eunha", "VIVIZ Eunha", "Eunha fancam", "VIVIZ fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Eunha performance", "Shorts"
        ]
    if any(token in value for token in ("나띠", "natty")):
        return "NATTY", "KISS OF LIFE", [
            "Natty", "KISS OF LIFE", "KISS OF LIFE Natty", "Natty fancam",
            "KIOF", "K-pop", "K-pop fancam", "K-pop Shorts",
            "Korean idol", "female idol", "Shorts"
        ]
    if any(token in value for token in ("제니", "jennie")):
        return "JENNIE", "BLACKPINK", [
            "Jennie", "BLACKPINK Jennie", "Jennie fancam", "BLACKPINK fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol",
            "female idol", "Jennie performance", "Shorts"
        ]
    if any(token in value for token in ("우기", "yuqi")):
        return "YUQI", "(G)I-DLE", [
            "Yuqi", "(G)I-DLE Yuqi", "Yuqi fancam", "(G)I-DLE fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol", "female idol", "Shorts"
        ]
    if any(token in value for token in ("엘라", "ella")):
        return "ELLA", "MEOVV", [
            "Ella", "MEOVV Ella", "Ella fancam", "MEOVV fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol", "female idol", "Shorts"
        ]
    if any(token in value for token in ("카즈하", "kazuha")):
        return "KAZUHA", "LE SSERAFIM", [
            "Kazuha", "LE SSERAFIM Kazuha", "Kazuha fancam", "LE SSERAFIM fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol", "female idol", "Shorts"
        ]
    if any(token in value for token in ("해린", "haerin")):
        return "HAERIN", "NewJeans", [
            "Haerin", "NewJeans Haerin", "Haerin fancam", "NewJeans fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol", "female idol", "Shorts"
        ]
    if any(token in value for token in ("리사", "lisa")):
        return "LISA", "BLACKPINK", [
            "Lisa", "BLACKPINK Lisa", "Lisa fancam", "BLACKPINK fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol", "female idol", "Shorts"
        ]
    if any(token in value for token in ("낸시", "nancy")):
        return "NANCY", "MOMOLAND", [
            "Nancy", "MOMOLAND Nancy", "Nancy fancam", "MOMOLAND fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol", "female idol", "Shorts"
        ]
    if any(token in value for token in ("이지", "e.ji", "eji")):
        return "EJI", "ICHILLIN", [
            "E.JI", "ICHILLIN E.JI", "E.JI fancam", "ICHILLIN fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol", "female idol", "Shorts"
        ]
    if any(token in value for token in ("쯔위", "tzuyu")):
        return "TZUYU", "TWICE", [
            "Tzuyu", "TWICE Tzuyu", "Tzuyu fancam", "TWICE fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol", "female idol", "Shorts"
        ]
    if any(token in value for token in ("제니", "jennie")):
        return "JENNIE", "BLACKPINK", [
            "Jennie", "BLACKPINK Jennie", "Jennie fancam", "BLACKPINK fancam",
            "K-pop", "K-pop fancam", "K-pop Shorts", "Korean idol", "female idol", "Shorts"
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
    # Fuzzy recovery for minor misspellings in Drive filenames. Only accept a
    # single clear winner; never guess when two artists are similarly close.
    aliases = {
        "SAKURA": ("LE SSERAFIM", ["sakura", "miyawaki", "사쿠라"]),
        "KYA": ("KiiiKiii", ["kya", "kiya", "키야"]),
        "WONYOUNG": ("IVE", ["wonyoung", "장원영"]),
        "CHAERYEONG": ("ITZY", ["chaeryeong", "채령"]),
        "EUNBI": ("SOLO", ["eunbi", "kwoneunbi", "권은비"]),
        "EUNHA": ("VIVIZ", ["eunha", "은하"]),
        "NATTY": ("KISS OF LIFE", ["natty", "나띠"]),
        "JENNIE": ("BLACKPINK", ["jennie", "제니"]),
        "YUQI": ("(G)I-DLE", ["yuqi", "yuki", "우기"]),
        "ELLA": ("MEOVV", ["ella", "엘라"]),
        "KAZUHA": ("LE SSERAFIM", ["kazuha", "카즈하"]),
        "HAERIN": ("NewJeans", ["haerin", "해린"]),
        "LISA": ("BLACKPINK", ["lisa", "리사"]),
        "NANCY": ("MOMOLAND", ["nancy", "낸시"]),
        "EJI": ("ICHILLIN", ["eji", "이지"]),
        "TZUYU": ("TWICE", ["tzuyu", "쯔위"]),
        "KARINA": ("aespa", ["karina", "카리나"]),
        "NINGNING": ("aespa", ["ningning", "닝닝"]),
    }
    normalized = re.sub(r"[^a-z0-9가-힣]+", " ", value)
    words = normalized.split()
    candidates = []
    compact_value = re.sub(r"[^a-z0-9가-힣]+", "", value)
    for canonical, (team, variants) in aliases.items():
        for alias in variants:
            alias_norm = re.sub(r"[^a-z0-9가-힣]+", "", alias.casefold())
            if len(alias_norm) < 3:
                continue
            if alias_norm in compact_value:
                candidates.append((1.0, canonical, team, alias))
                break
            for word in words:
                if len(word) >= 4 and len(alias_norm) >= 4:
                    score = difflib.SequenceMatcher(None, word, alias_norm).ratio()
                    if score >= 0.90:
                        candidates.append((score, canonical, team, alias))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        best = candidates[0]
        runner_up = next((item for item in candidates[1:] if item[1] != best[1]), None)
        if runner_up is None or best[0] - runner_up[0] >= 0.06:
            canonical, team = best[1], best[2]
            print(f"Artist name auto-corrected from filename using alias '{best[3]}': {canonical} / {team}")
            tags = [canonical.title(), f"{team} {canonical.title()}", f"{canonical.title()} fancam",
                    f"{team} fancam", "K-pop", "K-pop Shorts", "Korean idol",
                    "female idol", "Shorts"]
            return canonical, team, tags
        print(f"Artist name recovery ambiguous for '{filename}'; refusing to guess.")
    return "", "", []

def generate_metadata(filename, hook, index):
    artist, group, fixed_tags = identify_artist(filename)
    if artist == "WONYOUNG":
        title = "WONYOUNG (IVE) ✨ | Charismatic K-Pop Fancam #Shorts"
        description = ("WONYOUNG of IVE ✨ Enjoy this fan-captured performance and her "
                       f"stage presence.\n\nFan-captured footage.")
        tags = fixed_tags
    elif artist == "CHAERYEONG":
        title = "CHAERYEONG (ITZY) 🌙 | K-Pop Fancam #Shorts"
        description = ("CHAERYEONG of ITZY 🌙 Enjoy this fan-captured performance and "
                       f"her stage presence.\n\nFan-captured footage.")
        tags = fixed_tags
    elif artist == "EUNBI":
        title = "KWON EUNBI ✨ | K-Pop Fancam #Shorts"
        description = ("KWON EUNBI ✨ Enjoy this fan-captured performance and her "
                       f"stage presence.\n\nFan-captured footage.")
        tags = fixed_tags
    elif artist == "EUNHA":
        title = "VIVIZ / EUNHA ✨ | K-Pop Fancam #Shorts"
        description = ("EUNHA of VIVIZ ✨ Enjoy this fan-captured performance and "
                       f"her stage presence.\\n\\nFan-captured footage.")
        tags = fixed_tags
    elif artist == "NATTY":
        title = "KISS OF LIFE / NATTY 🔥 | K-Pop Fancam #Shorts"
        description = ("NATTY of KISS OF LIFE 🔥 Enjoy this fan-captured performance and "
                       f"her stage presence.\n\nFan-captured footage.")
        tags = fixed_tags
    elif artist == "JENNIE":
        title = "BLACKPINK / JENNIE 🖤 | K-Pop Fancam #Shorts"
        description = ("JENNIE of BLACKPINK 🖤 Enjoy this fan-captured performance and "
                       f"her stage presence.\\n\\nFan-captured footage.")
        tags = fixed_tags
    elif artist in {"YUQI", "ELLA", "KAZUHA", "HAERIN", "LISA", "NANCY", "TZUYU", "JENNIE"}:
        title = f"{group} / {artist} ✨ | K-Pop Fancam #Shorts"
        description = (f"{artist} of {group} ✨ Enjoy this fan-captured performance and "
                       f"her stage presence.\\n\\nFan-captured footage.")
        tags = fixed_tags
    elif artist == "KARINA":
        title = "aespa / KARINA ✨ | K-Pop Fancam #Shorts"
        description = ("KARINA of aespa ✨ Enjoy this fan-captured performance and "
                       f"her stage presence.\n\nFan-captured footage.")
        tags = fixed_tags
    elif artist == "SAKURA":
        title = "LE SSERAFIM / SAKURA 🌸 | K-Pop Fancam #Shorts"
        description = ("SAKURA of LE SSERAFIM 🌸 Enjoy this fan-captured performance and "
                       f"her stage presence.\n\nFan-captured footage.")
        tags = fixed_tags
    elif artist == "KYA":
        title = "KiiiKiii / KYA ✨ | K-Pop Fancam #Shorts"
        description = ("KYA of KiiiKiii ✨ Enjoy this fan-captured performance and "
                       f"her stage presence.\n\nFan-captured footage.")
        tags = fixed_tags
    else:
        # Metadata must never block an otherwise valid Short when Gemini is rate-limited.
        title = "Performance Highlight #Shorts"
        description = ("A short highlight from the source video. Fan-edited content; "
                       "all footage belongs to its respective rights holders.")
        tags = ["performance highlight", "fan edit", "music", "entertainment",
                "Shorts", "video highlight"]

    # Add evidence-led discovery tags based on successful K-pop Shorts/fancam channels.
    # Keep the metadata specific to this clip: artist + group + performance format,
    # rather than stuffing unrelated group names into every upload.
    artist_tag = artist.title() if artist else ""
    group_tag = group.strip() if group else ""
    discovery_tags = [
        "K-pop", "Kpop", "K-pop fancam", "Kpop fancam", "K-pop Shorts",
        "Korean idol", "K-pop performance", "아이돌 직캠", "걸그룹 직캠",
        "여자아이돌", "Korean girl group", "Shorts"
    ]
    if artist_tag:
        discovery_tags.extend([artist_tag, f"{artist_tag} fancam"])
    if group_tag:
        discovery_tags.extend([group_tag, f"{group_tag} fancam"])
    tags = discovery_tags + tags

    # Strip channel branding from generated tags and descriptions.
    tags = [tag for tag in tags if "curioloop" not in str(tag).casefold()]
    clean, seen = [], set()
    for tag in ["K-pop", "K-pop fancam", "Shorts"] + tags:
        tag = re.sub(r"\s+", " ", str(tag)).strip().lstrip("#")
        key = tag.casefold()
        if tag and key not in seen and len(tag) <= 60 and len(",".join(clean + [tag])) <= 480:
            clean.append(tag); seen.add(key)
    # Leave descriptions empty; do not add source notes or hashtags.
    description = ""
    upload_stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%m%d-%H%M%S")
    title = f"{title.replace(' #Shorts', '')} | Clip {index:02d} · {upload_stamp} #Shorts"
    return title[:100], description[:5000], clean

def next_publish_at_kst(publish_time):
    """Return the fixed KST slot plus 30 minutes; never publish a missed slot immediately."""
    try:
        hour, minute = [int(part) for part in publish_time.split(":", 1)]
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("FANCAMVERSE_PUBLISH_TIME_KST must use HH:MM format.")
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    publish_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=30)
    # GitHub scheduled workflows can start late. If today's fixed slot has
    # passed, move to the next day's same slot instead of failing or publishing now.
    if publish_at <= now + timedelta(minutes=2):
        publish_at += timedelta(days=1)
        print(
            f"Today's fixed slot {publish_time} + 30 minutes has passed; "
            f"moving safely to the next day at {publish_at:%Y-%m-%d %H:%M} KST."
        )
    print(f"Fixed publication slot: {publish_time} + 30 minutes = {publish_at:%H:%M} KST.")
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

    # Treat the insert response as an API acknowledgement, not proof that the
    # requested visibility was applied. Inspect the returned resource fields.
    returned_status = result.get("status") or {}
    returned_privacy = returned_status.get("privacyStatus")
    returned_publish_at = returned_status.get("publishAt")
    returned_channel = (result.get("snippet") or {}).get("channelTitle", "")
    returned_channel_id = (result.get("snippet") or {}).get("channelId", "")
    expected_channel_id = required("YOUTUBE_EXPECTED_CHANNEL_ID")
    if returned_channel_id and returned_channel_id != expected_channel_id:
        print(
            f"::warning::Upload {video_id} was accepted on channel {returned_channel_id}, "
            f"but configured FANCAMVERSE channel is {expected_channel_id}. "
            "The upload will be recorded to prevent an automatic duplicate; stop for manual review."
        )
    else:
        print(
            "Upload channel response: "
            f"{returned_channel_id or 'channel ID not returned; dedicated OAuth token used'}"
        )
    verified = returned_privacy == privacy
    if publish_at and (not returned_publish_at or datetime.fromisoformat(returned_publish_at.replace('Z', '+00:00')) != datetime.fromisoformat(publish_at.replace('Z', '+00:00'))):
        verified = False
    print(
        "YouTube API response: "
        f"video_id={video_id}, channel={returned_channel or 'not returned'}, "
        f"requested_privacy={privacy}, returned_privacy={returned_privacy or 'not returned'}, "
        f"requested_publishAt={publish_at or 'none'}, "
        f"returned_publishAt={returned_publish_at or 'none'}, "
        f"visibility_verified={verified}"
    )
    if not verified:
        print(
            f"::warning::Upload {video_id} was accepted by videos.insert, but the API response "
            "did not confirm the requested privacy/schedule. Do not treat this as a verified publish."
        )
    else:
        print(f"Upload API response verified: https://www.youtube.com/shorts/{video_id}")
    return {"video_id": video_id, "verified": verified, "privacy": returned_privacy,
            "channel_title": returned_channel}

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
    coordinated_source_id = os.getenv("FANCAMVERSE_COORDINATED_SOURCE_ID", "").strip()
    if coordinated_source_id:
        files = [item for item in files if item["id"] == coordinated_source_id]
        if not files:
            raise RuntimeError(
                "The coordinated artist-intro source is no longer available in the Drive inbox; "
                "refusing to publish a fancam from a different original."
            )
        print("Using the exact Drive original selected for the preceding artist intro:", files[0]["name"])
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
    trigger_event = os.getenv("FANCAMVERSE_TRIGGER_EVENT", "unknown").strip()
    trigger_schedule = os.getenv("FANCAMVERSE_TRIGGER_SCHEDULE", "").strip()
    schedule_slot_key = ""
    if trigger_event == "schedule" and publish_time:
        schedule_slot_key = (
            datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
            + "|" + publish_time
        )
        completed_slots = history.setdefault("completed_schedule_slots", [])
        if schedule_slot_key in completed_slots:
            print(
                f"Scheduled slot {schedule_slot_key} was already processed; "
                "skipping this duplicate/fallback trigger."
            )
            return
    print(
        f"Upload trigger: event={trigger_event}, schedule={trigger_schedule or 'manual/unspecified'}, "
        f"privacy={privacy}, publish_slot_KST={publish_time or 'none'}"
    )
    if trigger_event == "schedule":
        if privacy != "public":
            raise RuntimeError("Scheduled upload must be configured as public; refusing private scheduled upload.")
        if not publish_time:
            raise RuntimeError("Scheduled upload has no recognized KST publish slot; refusing ambiguous publication.")
    publish_at = next_publish_at_kst(publish_time) if publish_time and not output_only else None
    if publish_at:
        privacy = "private"
        print(f"Scheduled YouTube publication for {publish_at} (Asia/Seoul).")
    if not output_only:
        validate_upload_destination_config()
        print(f"Upload destination: {CHANNEL_BRAND}, using its dedicated OAuth token.")

    uploaded_count = 0
    verified_count = 0

    for batch_number in range(1, batch_size + 1):
        eligible = [
            item for item in files
            if not source_state.get(item["id"], {}).get("complete", False)
        ]
        if not eligible:
            print("No unused source videos remain in the current Drive inbox.")
            break

        # Skip files whose artist/group cannot be safely identified, rather than
        # aborting the entire run and blocking other valid originals.
        identifiable = []
        for candidate in eligible:
            candidate_artist, candidate_group, _ = identify_artist(candidate["name"])
            if (not candidate_artist or not candidate_group
                    or not re.fullmatch(r"[A-Z][A-Z0-9 ]*", candidate_artist)
                    or not re.fullmatch(r"[A-Za-z0-9&!(). '-]+", candidate_group)):
                print(f"Skipping source with unresolved artist/group: {candidate['name']}")
                continue
            identifiable.append(candidate)
        if not identifiable:
            print("No unused source videos with a recognized artist/group remain.")
            break

        # Rotate by ARTIST, not merely by Drive file ID. One artist's multiple
        # source files must not crowd out other artists in the inbox.
        used_source_ids = set(source_state) | set(history.get("processed", []))
        artist_by_source = {
            candidate["id"]: identify_artist(candidate["name"])[0].casefold()
            for candidate in identifiable
        }
        artist_cycle = list(history.get("artist_cycle", []))
        # One-time migration only. Rebuilding this list on every run would
        # prevent the rotation cycle from ever resetting after all artists had a turn.
        if "artist_cycle" not in history:
            for prior_state in source_state.values():
                if prior_state.get("used_clip_indices"):
                    prior_name = prior_state.get("name", "")
                    prior_artist, prior_group, _ = identify_artist(prior_name)
                    if prior_artist and prior_artist.casefold() not in artist_cycle:
                        artist_cycle.append(prior_artist.casefold())
        available_artist_keys = set(artist_by_source.values())
        unfeatured_artist_keys = available_artist_keys - set(artist_cycle)
        if unfeatured_artist_keys:
            selection_pool = [
                candidate for candidate in identifiable
                if artist_by_source[candidate["id"]] in unfeatured_artist_keys
            ]
            print(
                "Artist rotation active: prioritizing artists not yet featured in this cycle:",
                ", ".join(sorted(unfeatured_artist_keys))
            )
        else:
            # Every currently available artist has had a turn; start a new cycle.
            artist_cycle = []
            selection_pool = identifiable
            print("Artist rotation cycle complete; starting a new cycle across all available artists.")
        fresh_sources = [
            candidate for candidate in selection_pool
            if candidate["id"] not in used_source_ids
        ]
        previously_used_sources = [
            candidate for candidate in selection_pool
            if candidate["id"] in used_source_ids
        ]
        random.shuffle(fresh_sources)
        random.shuffle(previously_used_sources)
        selection_pool = fresh_sources if fresh_sources else previously_used_sources
        print(
            f"Source rotation within artist pool: {len(fresh_sources)} never-used source(s); "
            f"{len(previously_used_sources)} previously-used source(s) with remaining unused clips."
        )
        last_artist_key = history.get("last_artist_key", "")
        alternatives = [
            candidate for candidate in selection_pool
            if artist_by_source[candidate["id"]] != last_artist_key
        ]
        item = random.choice(alternatives or selection_pool)
        state = source_state.get(item["id"], {"next_clip": 1, "complete": False})
        print(
            f"Batch {batch_number}/{batch_size} | Random "
            f"{'never-used' if item['id'] not in used_source_ids else 'previously-used fallback'} source: "
            f"{item['name']} ({item['id']})"
        )

        source = download_video(drive, item)
        duration = probe_duration(source, allow_short=bool(source_filter))
        saved_clips = state.get("clips")
        if saved_clips:
            clips = saved_clips
            print(f"Using {len(clips)} saved non-overlapping highlight window(s) for this source.")
        else:
            clips = analyze_highlights(source, duration, item["name"])

        # Also cover previously saved highlight lists: the regular Sakura source
        # must have a selectable 25-second clip containing the 1:07-1:12 segment.
        if is_standard_sakura_source(item["name"]) and duration >= 72:
            has_sakura_window = any(
                clip["start"] <= 67.0 and clip["end"] >= 72.0
                for clip in clips
            )
            if not has_sakura_window:
                start = min(60.0, max(0.0, duration - MIN_SHORT_SECONDS))
                clips.append({
                    "start": start,
                    "end": start + MIN_SHORT_SECONDS,
                    "hook": "Sakura Performance Highlight"
                })
                print("Added dedicated regular-Sakura clip containing 1:07-1:12.")

        used_indices = set(int(x) for x in state.get("used_clip_indices", []))
        available_indices = [i for i in range(1, len(clips) + 1) if i not in used_indices]
        intro_start = os.getenv("FANCAMVERSE_INTRO_SOURCE_START", "").strip()
        intro_end = os.getenv("FANCAMVERSE_INTRO_SOURCE_END", "").strip()
        if intro_start and intro_end:
            intro_start_s, intro_end_s = float(intro_start), float(intro_end)
            if intro_end_s <= intro_start_s:
                raise RuntimeError("Invalid artist-intro source segment boundaries.")
            before = len(available_indices)
            available_indices = [
                i for i in available_indices
                if clips[i - 1]["end"] <= intro_start_s
                or clips[i - 1]["start"] >= intro_end_s
            ]
            print(
                f"Artist-intro footage exclusion: {intro_start_s:.1f}-{intro_end_s:.1f}s; "
                f"removed {before - len(available_indices)} overlapping fancam highlight(s)."
            )
            if not available_indices:
                raise RuntimeError(
                    "Every unused fancam highlight overlaps the artist-intro footage. "
                    "Refusing to publish a duplicate segment."
                )
        if is_standard_sakura_source(item["name"]):
            sakura_priority = [
                i for i in available_indices
                if clips[i - 1]["start"] <= 67.0 and clips[i - 1]["end"] >= 72.0
            ]
            if sakura_priority:
                available_indices = sakura_priority
                print("Regular Sakura source: prioritizing the highlight containing 1:07-1:12.")
        if not available_indices:
            print(f"All highlight windows already used for {item['name']}; skipping this source.")
            source_state[item["id"]] = {**state, "name": item["name"], "complete": True}
            continue
        selected_index = random.choice(available_indices)
        clip = clips[selected_index - 1]
        artist_name, group_name, _ = identify_artist(item["name"])
        if (not artist_name or not group_name
                or not re.fullmatch(r"[A-Z][A-Z0-9 ]*", artist_name)
                or not re.fullmatch(r"[A-Za-z0-9&!(). '-]+", group_name)):
            print(f"Skipping source with unresolved artist/group after selection: {item['name']}")
            source_state[item["id"]] = {**state, "name": item["name"], "complete": True,
                                        "skip_reason": "unresolved_artist_group"}
            continue
        # Keep the artist token for unique filenames; show GROUP / NAME on-screen.
        output_stem = artist_name
        display_label = f"{group_name} / {artist_name}"
        rendered = render_clips(source, [clip], output_stem, index_offset=selected_index - 1,
                                display_name=display_label)
        path, clip = rendered[0]
        title, description, tags = generate_metadata(item["name"], clip["hook"], selected_index)
        print(
            f"Selected Short {selected_index}: {clip['start']:.1f}-{clip['end']:.1f}s | {title}"
        )

        if output_only:
            print(f"OUTPUT_ONLY: preview ready at {path}; upload skipped.")
            # Preview mode does not consume a clip or advance rotation history.
            continue

        upload_result = upload(youtube, path, title, description, tags, privacy, publish_at=publish_at)
        video_id = upload_result["video_id"]
        uploaded_count += 1
        if upload_result["verified"]:
            verified_count += 1

        updated_used_indices = sorted(used_indices | {selected_index})
        source_complete = len(updated_used_indices) >= len(clips)
        source_state[item["id"]] = {
            "name": item["name"],
            "next_clip": SHORTS_PER_SOURCE + 1 if source_complete else 1,
            "complete": source_complete,
            "clips": clips,
            "used_clip_indices": updated_used_indices
        }
        print(
            f"Source progress: {len(updated_used_indices)}/{len(clips)} Shorts used; "
            f"{'completed' if source_complete else 'will rotate back later'}."
        )
        history["last_source_id"] = item["id"]
        selected_artist_key = artist_by_source[item["id"]]
        history["last_artist_key"] = selected_artist_key
        artist_cycle = list(history.get("artist_cycle", []))
        if selected_artist_key not in artist_cycle:
            artist_cycle.append(selected_artist_key)
        history["artist_cycle"] = artist_cycle
        history["sources"] = source_state
        if schedule_slot_key:
            completed_slots = history.setdefault("completed_schedule_slots", [])
            if schedule_slot_key not in completed_slots:
                completed_slots.append(schedule_slot_key)
                history["completed_schedule_slots"] = completed_slots[-90:]
        history["processed"] = [
            sid for sid, state in source_state.items() if state.get("complete")
        ][-2000:]
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(
            json.dumps(history, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )
        print(f"Recorded YouTube upload acknowledgement {video_id}; progress saved to prevent duplicate upload.")

    if output_only:
        print("OUTPUT_ONLY run finished; no uploads or progress changes were made.")
    else:
        print(
            f"CurioLoop batch finished: YouTube API accepted {uploaded_count}/{batch_size} upload(s); "
            f"requested visibility verified for {verified_count}/{uploaded_count}."
        )
        if verified_count != uploaded_count:
            raise RuntimeError(
                "One or more uploads were accepted but their requested visibility was not verified. "
                "The uploaded video IDs and source progress were saved to prevent duplicate uploads."
            )

if __name__ == "__main__":
    main()
