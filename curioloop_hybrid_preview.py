import asyncio
import base64
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

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
    prompt = f"""Create a 30-second English K-pop ARTIST PROFILE Short, not a performance critique.
Source filename: {filename!r}; source duration: {total:.1f}s.
Inspect the contact sheet to identify the performer. Return JSON only with:
{{"start":10.0,"end":15.0,"artist":"Ningning","group":"aespa","title":"Meet Ningning","hook":"MEET NINGNING","facts":[{{"label":"GROUP","value":"aespa"}},{{"label":"DEBUT","value":"2020"}},{{"label":"ROLE","value":"Vocalist"}}],"narration":"A natural 45-55 word English introduction based only on reliable, verifiable public facts.","profile_url":"https://official-agency-profile-page"}}
Choose a compelling 4-6 second continuous highlight. Identify only when the filename or frames support it. Do not invent birth dates, roles, awards or biography. Facts must be independently verifiable from the artist's official agency profile or official group website. profile_url must be an official agency or artist website URL, not Wikipedia, fan sites, social aggregators or news sites. If identity or facts cannot be verified, return empty facts and empty profile_url and state only what is observable. No markdown."""
    payload={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":"image/jpeg",
             "data":base64.b64encode(sheet.read_bytes()).decode("ascii")}}]}],
             "generationConfig":{"temperature":0.15,"responseMimeType":"application/json"}}
    model=os.getenv("GEMINI_MODEL","gemini-3.8-flash")
    response=gemini_generate_with_fallback(model,required("GEMINI_API_KEY"),payload,180,"artist profile research")
    data=json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
    start,end=float(data["start"]),float(data["end"])
    if start < 0 or end > total or not 4 <= end-start <= 6:
        raise RuntimeError(f"Gemini selected invalid highlight: {start:.1f}-{end:.1f}s")
    data["facts"]=[x for x in data.get("facts",[]) if isinstance(x,dict) and x.get("label") and x.get("value")][:3]
    data["narration"]=str(data.get("narration","")).strip()
    if len(data["narration"].split()) < 35:
        raise RuntimeError("Artist introduction narration is too short or could not be verified.")
    return data

class ImageMetaParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.images=[]
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=="meta" and a.get("property") in ("og:image","og:image:secure_url") or tag=="meta" and a.get("name")=="twitter:image":
            if a.get("content"): self.images.append(a["content"])
        if tag=="img" and a.get("src"):
            alt=(a.get("alt") or "").lower()
            if any(k in alt for k in ("profile","artist","ningning","aespa","portrait")):
                self.images.append(a["src"])

def official_portrait(profile_url):
    if not profile_url: return None
    host=urlparse(profile_url).hostname or ""
    allowed=("smtown.com","aespa.com","jype.com","ygfamily.com","yg-entertainment.com","hybecorp.com","weverse.io","starship-ent.com","cubeent.co.kr","kqent.com","pledis.co.kr","beliftlab.com")
    if not any(host==d or host.endswith("."+d) for d in allowed):
        print(f"Official profile URL rejected (untrusted domain): {host}")
        return None
    try:
        page=requests.get(profile_url,timeout=25,headers={"User-Agent":"Mozilla/5.0"})
        page.raise_for_status()
        parser=ImageMetaParser(); parser.feed(page.text)
        for candidate in parser.images:
            image_url=urljoin(profile_url,candidate)
            if urlparse(image_url).scheme not in ("http","https"): continue
            img=requests.get(image_url,timeout=25,headers={"User-Agent":"Mozilla/5.0"})
            img.raise_for_status()
            if not img.headers.get("Content-Type","").startswith("image/"): continue
            target=WORK/"official_artist_photo.jpg"
            target.write_bytes(img.content)
            with Image.open(target) as im:
                im.verify()
            print(f"OFFICIAL_PHOTO_SOURCE={profile_url}")
            return target
    except Exception as exc:
        print(f"Official portrait unavailable; continuing without it: {exc}")
    return None

def make_overlay(data, out):
    w,h=1080,1920
    image=Image.new("RGBA",(w,h),(8,10,18,255))
    draw=ImageDraw.Draw(image)
    title_font=ImageFont.truetype(FONT,66)
    label_font=ImageFont.truetype(FONT,34)
    body_font=ImageFont.truetype(FONT,48)
    small=ImageFont.truetype(FONT,30)
    draw.rounded_rectangle((55,150,1025,340),radius=30,fill=(0,0,0,190))
    draw.text((90,205),str(data.get("hook","MEET THE ARTIST")).upper()[:32],font=title_font,fill="white")
    draw.text((85,1160),str(data.get("artist","ARTIST")),font=title_font,fill="white")
    draw.text((88,1250),str(data.get("group","")).upper(),font=label_font,fill=(255,220,120))
    y=1350
    for fact in data.get("facts",[])[:3]:
        draw.text((90,y),f"{str(fact['label']).upper()}: {fact['value']}"[:38],font=body_font,fill="white")
        y+=105
    draw.text((88,1780),"CURIOLOOP  /  ARTIST SPOTLIGHT",font=small,fill=(255,220,120))
    image.save(out)

def render(source, data, portrait, output):
    overlay=WORK/"artist_overlay.png"
    voice=WORK/"artist_voice.mp3"
    make_overlay(data,overlay)
    asyncio.run(make_voice(data["narration"],voice))
    voice_duration=duration(voice)
    if voice_duration > 28:
        raise RuntimeError(f"Artist narration too long ({voice_duration:.1f}s); keep it under 28 seconds.")
    highlight=WORK/"highlight.mp4"
    photo_video=WORK/"profile_segment.mp4"
    photo=WORK/"portrait_background.jpg"
    if portrait:
        with Image.open(portrait) as im:
            im=im.convert("RGB")
            iw,ih=im.size
            scale=max(1080/iw,1920/ih)
            im=im.resize((int(iw*scale),int(ih*scale)))
            left=(im.width-1080)//2; top=(im.height-1920)//2
            im=im.crop((left,top,left+1080,top+1920))
            im.save(photo,quality=92)
    else:
        # Branded fallback: gradient-like dark background, no unlicensed substitute image.
        Image.new("RGB",(1080,1920),(15,20,35)).save(photo)
    start=float(data["start"]); end=float(data["end"])
    run(["ffmpeg","-y","-ss",f"{start:.3f}","-i",str(source),"-t",f"{end-start:.2f}",
         "-vf","scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
         "-an","-c:v","libx264","-preset","veryfast","-crf","18","-pix_fmt","yuv420p",str(highlight)])
    run(["ffmpeg","-y","-loop","1","-i",str(photo),"-loop","1","-i",str(overlay),
         "-t","24","-filter_complex","[0:v]scale=1080:1920,setsar=1[bg];[1:v]format=rgba[ov];[bg][ov]overlay=0:0[v]",
         "-map","[v]","-an","-c:v","libx264","-preset","veryfast","-crf","18","-pix_fmt","yuv420p",str(photo_video)])
    concat=WORK/"segments.txt"
    concat.write_text(f"file '{highlight.resolve()}'\nfile '{photo_video.resolve()}'\n",encoding="utf-8")
    silent=WORK/"silent.mp4"
    run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),"-c","copy",str(silent)])
    run(["ffmpeg","-y","-i",str(silent),"-i",str(voice),"-filter_complex",
         "[1:a]adelay=1200|1200,apad=pad_dur=30,volume=1.0[a]",
         "-map","0:v","-map","[a]","-t","30","-c:v","copy","-c:a","aac","-b:a","192k",
         "-movflags","+faststart",str(output)])
    probe=json.loads(run(["ffprobe","-v","error","-show_entries","stream=width,height","-of","json",str(output)]).stdout)
    stream=probe["streams"][0]
    if (stream["width"],stream["height"])!=(1080,1920): raise RuntimeError("Output is not 1080x1920.")
    print(f"PREVIEW_READY={output}")
    print(f"TITLE={data.get('title','Artist Spotlight')}")
    print(f"ARTIST={data.get('artist','Unknown')}")
    print(f"OFFICIAL_PHOTO={'yes' if portrait else 'no - branded fallback used'}")
    print(f"NARRATION={data['narration']}")

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
    if not videos: raise RuntimeError("No eligible Drive source videos found (Chaewon is excluded).")
    item=random.choice(videos)
    print(f"Selected source: {item['name']}")
    source=download(drive,item)
    total=duration(source)
    if total<30: raise RuntimeError("Selected source is too short.")
    sheet=contact_sheet(source,total)
    data=analyze(source,item["name"],total,sheet)
    portrait=official_portrait(data.get("profile_url",""))
    output=WORK/"curioloop_artist_spotlight_preview.mp4"
    render(source,data,portrait,output)
    description=(f"Meet {data.get('artist','this artist')} from {data.get('group','the group')}. "
                 "Artist information is presented as an editorial spotlight. Follow CurioLoop for more.")
    (WORK/"metadata.txt").write_text(
        f"Title: {data.get('title','Artist Spotlight')}\n\nDescription: {description}\n\n"
        "Pinned comment: Which artist should we feature next?\n"
        f"Official profile source: {data.get('profile_url','Not verified')}\n",encoding="utf-8")
    print(f"SOURCE={item['name']}")


if __name__=="__main__":
    main()
