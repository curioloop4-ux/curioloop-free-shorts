import asyncio
import base64
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path

import edge_tts
import requests
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

ROOT = Path(__file__).parent
WORK = ROOT / "hybrid_preview"
WORK.mkdir(exist_ok=True)
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required secret: {name}")
    return value

def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True)

def services():
    creds = Credentials(
        None, refresh_token=required("FANCAMVERSE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("FANCAMVERSE_CLIENT_ID"),
        client_secret=required("FANCAMVERSE_CLIENT_SECRET"),
        scopes=DRIVE_SCOPES
    )
    return build("drive", "v3", credentials=creds)

def list_videos(drive, folder_id):
    pending, visited, found = [folder_id], set(), []
    supported = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm", "video/x-matroska"}
    while pending:
        parent = pending.pop(0)
        if parent in visited:
            continue
        visited.add(parent)
        token = None
        while True:
            args = {"q": f"'{parent}' in parents and trashed=false", "pageSize":100,
                    "fields":"nextPageToken,files(id,name,mimeType,size)",
                    "supportsAllDrives":True, "includeItemsFromAllDrives":True}
            if token:
                args["pageToken"] = token
            result = drive.files().list(**args).execute()
            for item in result.get("files", []):
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    pending.append(item["id"])
                elif item.get("mimeType") in supported:
                    if not any(x in item["name"].casefold() for x in ("김채원", "chaewon")):
                        found.append(item)
            token = result.get("nextPageToken")
            if not token:
                break
    return found

def download(drive, item):
    path = WORK / re.sub(r"[^A-Za-z0-9._-]+", "_", item["name"])
    request = drive.files().get_media(fileId=item["id"], supportsAllDrives=True)
    with path.open("wb") as f:
        downloader = MediaIoBaseDownload(f, request, chunksize=8*1024*1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"Drive download: {int(status.progress()*100)}%")
    if path.stat().st_size == 0:
        raise RuntimeError("Downloaded source is empty.")
    return path

def duration(path):
    return float(run(["ffprobe","-v","error","-show_entries","format=duration",
                      "-of","default=noprint_wrappers=1:nokey=1",str(path)]).stdout.strip())

def contact_sheet(path, total):
    stamps = [max(1, min(total-2, total*i/12)) for i in range(1,12)]
    sheet = Image.new("RGB",(4*240,3*184),(18,18,18))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, stamp in enumerate(stamps):
        frame = WORK / f"frame_{i}.jpg"
        subprocess.run(["ffmpeg","-v","error","-y","-ss",str(stamp),"-i",str(path),
                        "-frames:v","1","-vf","scale=240:160","-q:v","5",str(frame)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        im = Image.open(frame).convert("RGB")
        x,y=(i%4)*240,(i//4)*184
        sheet.paste(im,(x,y))
        draw.text((x+5,y+162),f"{stamp:.1f}s",font=font,fill="white")
    out=WORK/"contact_sheet.jpg"
    sheet.save(out,quality=88)
    return out

def gemini_post_with_retry(url, api_key, payload, timeout, task_name):
    """Retry temporary Gemini rate-limit and service errors with backoff."""
    retryable = {408, 429, 500, 502, 503, 504}
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                url, params={"key": api_key}, json=payload, timeout=timeout
            )
            if response.status_code not in retryable:
                response.raise_for_status()
                return response
            if attempt == max_attempts:
                detail = (response.text or "")[:1000]
                raise RuntimeError(
                    f"Gemini {task_name} failed after {max_attempts} attempts "
                    f"(HTTP {response.status_code}): {detail}"
                )
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = max(float(retry_after), 8 * (2 ** (attempt - 1)))
            except (TypeError, ValueError):
                delay = 8 * (2 ** (attempt - 1))
            delay = min(delay, 90) + random.uniform(0, 2)
            print(
                f"Gemini {task_name}: HTTP {response.status_code}; "
                f"retry {attempt + 1}/{max_attempts} in {delay:.1f}s."
            )
            time.sleep(delay)
        except requests.HTTPError:
            raise
        except requests.RequestException as exc:
            if attempt == max_attempts:
                raise RuntimeError(
                    f"Gemini {task_name} request failed after {max_attempts} attempts: {exc}"
                ) from exc
            delay = 8 * (2 ** (attempt - 1)) + random.uniform(0, 2)
            print(f"Gemini {task_name}: network error; retry in {delay:.1f}s: {exc}")
            time.sleep(delay)

def gemini_generate_with_fallback(primary_model, api_key, payload, timeout, task_name):
    """Retry the primary Gemini model, then try the configured lighter fallback."""
    fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
    models = list(dict.fromkeys([primary_model, fallback_model]))
    last_error = None
    for index, model in enumerate(models):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            print(f"Gemini {task_name}: using model {model}.")
            return gemini_post_with_retry(url, api_key, payload, timeout, task_name)
        except RuntimeError as exc:
            last_error = exc
            message = str(exc)
            transient = any(
                f"HTTP {status}" in message for status in (408, 429, 500, 502, 503, 504)
            ) or "request failed after" in message
            if not transient or index == len(models) - 1:
                raise
            print(
                f"Gemini {task_name}: {model} unavailable after retries; "
                f"switching to fallback {fallback_model}."
            )
    raise last_error

def analyze(path, filename, total, sheet):
    prompt = f"""Create an original, fact-grounded 30-second English K-pop performance analysis Short.
Source filename: {filename!r}. Source duration: {total:.1f} seconds.
Inspect the attached contact sheet. Do not infer an artist, group, song, event, or venue unless the filename or frames make it unambiguous.
Select one visually clear, continuous clip 22 to 26 seconds long, fully within the source.
Write original commentary that analyzes only observable performance elements (movement, timing, expression, stage presence, camera framing); avoid unsupported biography or superlatives.
Return JSON only:
{{"start":10.0,"end":34.0,"title":"Why This Performance Moment Works","hook":"LOOK AT THE TIMING","analysis":"40 to 50 word original voiceover script in natural English","summary":"A concise 8 to 12 word visual takeaway"}}
No markdown. The analysis must be meaningful commentary, not a description of the footage alone."""
    payload={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":"image/jpeg",
             "data":base64.b64encode(sheet.read_bytes()).decode("ascii")}}]}],
             "generationConfig":{"temperature":0.25,"responseMimeType":"application/json"}}
    model=os.getenv("GEMINI_MODEL","gemini-3.8-flash")
    response=gemini_generate_with_fallback(
        model, required("GEMINI_API_KEY"), payload, 180, "hybrid performance analysis"
    )
    data=json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
    start,end=float(data["start"]),float(data["end"])
    if start < 0 or end > total or not 22 <= end-start <= 26:
        raise RuntimeError(f"Gemini selected invalid clip window: {start:.1f}-{end:.1f}s")
    words=len(str(data["analysis"]).split())
    if not 35 <= words <= 55:
        raise RuntimeError(f"Analysis narration length is invalid ({words} words).")
    return data

def make_overlay(data, out):
    w,h=1080,1920
    image=Image.new("RGBA",(w,h),(0,0,0,0))
    draw=ImageDraw.Draw(image)
    title_font=ImageFont.truetype(FONT,52)
    label_font=ImageFont.truetype(FONT,30)
    body_font=ImageFont.truetype(FONT,38)
    draw.rounded_rectangle((54,170,1026,310),radius=28,fill=(0,0,0,175))
    title=str(data["hook"]).upper()[:42]
    draw.text((86,210),title,font=title_font,fill="white",stroke_width=2,stroke_fill="black")
    draw.rounded_rectangle((54,1430,1026,1755),radius=30,fill=(0,0,0,190))
    draw.text((90,1470),"CURIOLOOP  /  PERFORMANCE BREAKDOWN",font=label_font,fill=(255,220,120))
    summary=str(data["summary"])
    words=summary.split()
    lines=[]
    line=""
    for word in words:
        candidate=(line+" "+word).strip()
        if draw.textbbox((0,0),candidate,font=body_font)[2] > 850:
            lines.append(line); line=word
        else:
            line=candidate
    if line: lines.append(line)
    y=1540
    for line in lines[:3]:
        draw.text((90,y),line,font=body_font,fill="white",stroke_width=1,stroke_fill="black")
        y+=62
    image.save(out)

async def make_voice(text, path):
    communicate=edge_tts.Communicate(text,"en-US-AriaNeural",rate="+4%",volume="+0%")
    await communicate.save(str(path))

def render(source, data, output):
    overlay=WORK/"editorial_overlay.png"
    voice=WORK/"analysis_voice.mp3"
    make_overlay(data,overlay)
    asyncio.run(make_voice(data["analysis"],voice))
    voice_duration=duration(voice)
    clip_duration=float(data["end"])-float(data["start"])
    if voice_duration > clip_duration - 0.5:
        raise RuntimeError(
            f"Voiceover ({voice_duration:.1f}s) does not fit selected clip ({clip_duration:.1f}s); "
            "regenerate with a shorter narration."
        )
    final_duration=clip_duration
    filt=("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
          "crop=1080:1920,setsar=1[base];[1:v]format=rgba[ov];"
          "[base][ov]overlay=0:0[v];"
          "[0:a]volume=0.16[orig];[2:a]volume=1.0[voice];"
          "[orig][voice]amix=inputs=2:duration=longest:dropout_transition=2[a]")
    run(["ffmpeg","-y","-ss",f"{data['start']:.3f}","-i",str(source),
         "-loop","1","-i",str(overlay),"-i",str(voice),"-t",f"{final_duration:.2f}",
         "-filter_complex",filt,"-map","[v]","-map","[a]",
         "-c:v","libx264","-preset","slow","-crf","17","-pix_fmt","yuv420p",
         "-c:a","aac","-b:a","192k","-movflags","+faststart",str(output)])
    probe=json.loads(run(["ffprobe","-v","error","-select_streams","v:0",
                          "-show_entries","stream=width,height","-of","json",str(output)]).stdout)
    stream=probe["streams"][0]
    if (stream["width"],stream["height"]) != (1080,1920):
        raise RuntimeError("Rendered preview is not 1080x1920.")
    print(f"PREVIEW_READY={output}")
    print(f"TITLE={data['title']}")
    print(f"ANALYSIS={data['analysis']}")
    print(f"CLIP={data['start']:.1f}-{data['end']:.1f}s")
    print(f"VOICE_DURATION={voice_duration:.1f}s")

def main():
    drive=services()
    folder=required("FANCAMVERSE_DRIVE_FOLDER_ID")
    videos=list_videos(drive,folder)
    if not videos:
        raise RuntimeError("No eligible Drive source videos found (Chaewon is excluded).")
    item=random.choice(videos)
    print(f"Selected source: {item['name']}")
    source=download(drive,item)
    total=duration(source)
    if total < 30:
        raise RuntimeError("Selected source is too short for the hybrid analysis preview.")
    sheet=contact_sheet(source,total)
    data=analyze(source,item["name"],total,sheet)
    output=WORK/"curioloop_hybrid_analysis_preview.mp4"
    render(source,data,output)
    (WORK/"metadata.txt").write_text(
        f"Title: {data['title']}\n\nDescription: Original performance commentary and analysis.\n"
        f"Subscribe for more K-pop performance breakdowns.\n\n"
        f"Pinned comment: Which performance detail stood out to you?\n",
        encoding="utf-8")
    print(f"SOURCE={item['name']}")

if __name__=="__main__":
    main()
