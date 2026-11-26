import json
import os
import re
import subprocess
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

ROOT = Path(__file__).parent
WORK = ROOT / "work_artist_intro"
WORK.mkdir(exist_ok=True)
HISTORY = ROOT / "history" / "artist_intro_processed.json"
ARTIST = os.getenv("INTRO_ARTIST", "KARINA").strip()
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
SUPPORTED = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm", "video/x-matroska"}
PROFILES = {
    "KARINA": {
        "display": "KARINA",
        "group": "aespa",
        "lines": [
            "Meet KARINA, the leader of aespa.",
            "She made her debut with aespa in November 2020.",
            "Known for her commanding stage presence and precise performance,",
            "she continues to stand out on global stages.",
            "From Black Mamba to Supernova, this is KARINA."
        ],
        "title": "MEET KARINA",
        "tag": "AESPA MEMBER SPOTLIGHT"
    },
    "WINTER": {
        "display": "WINTER",
        "group": "aespa",
        "lines": [
            "Meet WINTER, a member of aespa.",
            "She made her debut with aespa in November 2020.",
            "Known for her distinctive vocals and sharp performance,",
            "she brings a unique presence to the group's stages.",
            "This is WINTER."
        ],
        "title": "MEET WINTER",
        "tag": "AESPA MEMBER SPOTLIGHT"
    },
    "GISELLE": {
        "display": "GISELLE",
        "group": "aespa",
        "lines": [
            "Meet GISELLE, a member of aespa.",
            "She made her debut with aespa in November 2020.",
            "Known for her rap delivery and confident stage presence,",
            "she adds a distinctive edge to the group's sound.",
            "This is GISELLE."
        ],
        "title": "MEET GISELLE",
        "tag": "AESPA MEMBER SPOTLIGHT"
    },
    "NINGNING": {
        "display": "NINGNING",
        "group": "aespa",
        "lines": [
            "Meet NINGNING, a member of aespa.",
            "She made her debut with aespa in November 2020.",
            "Known for her powerful vocals and expressive performances,",
            "she brings energy to every stage.",
            "This is NINGNING."
        ],
        "title": "MEET NINGNING",
        "tag": "AESPA MEMBER SPOTLIGHT"
    }
}

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required secret: {name}")
    return value

def run(args):
    subprocess.run(args, check=True)

def services():
    drive_creds = Credentials(
        None, refresh_token=required("FANCAMVERSE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("FANCAMVERSE_CLIENT_ID"),
        client_secret=required("FANCAMVERSE_CLIENT_SECRET"),
        scopes=DRIVE_SCOPES
    )
    yt_creds = Credentials(
        None, refresh_token=required("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("YOUTUBE_CLIENT_ID"),
        client_secret=required("YOUTUBE_CLIENT_SECRET"),
        scopes=YT_SCOPES
    )
    return build("drive", "v3", credentials=drive_creds), build("youtube", "v3", credentials=yt_creds)

def list_videos(drive, folder_id):
    queue, visited, videos = [folder_id], set(), []
    while queue:
        parent = queue.pop(0)
        if parent in visited:
            continue
        visited.add(parent)
        token = None
        while True:
            args = {
                "q": f"'{parent}' in parents and trashed = false",
                "pageSize": 100,
                "orderBy": "createdTime",
                "fields": "nextPageToken,files(id,name,mimeType,createdTime,size)",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True
            }
            if token:
                args["pageToken"] = token
            result = drive.files().list(**args).execute()
            for item in result.get("files", []):
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    queue.append(item["id"])
                elif item.get("mimeType") in SUPPORTED:
                    name = item.get("name", "").casefold()
                    aliases = [ARTIST.casefold()]
                    if ARTIST == "KARINA":
                        aliases.append("카리나")
                    elif ARTIST == "NINGNING":
                        aliases.append("닝닝")
                    elif ARTIST == "WINTER":
                        aliases.append("윈터")
                    elif ARTIST == "GISELLE":
                        aliases.append("지젤")
                    if any(alias in name for alias in aliases):
                        videos.append(item)
            token = result.get("nextPageToken")
            if not token:
                break
    videos.sort(key=lambda x: (x.get("createdTime", ""), x["id"]), reverse=True)
    return videos

def load_history():
    if not HISTORY.exists():
        return {"processed": []}
    data = json.loads(HISTORY.read_text(encoding="utf-8"))
    if not isinstance(data.get("processed"), list):
        raise RuntimeError("Invalid intro history file.")
    return data

def download(drive, item):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", item["name"]).strip("._") or "source.mp4"
    path = WORK / name
    request = drive.files().get_media(fileId=item["id"], supportsAllDrives=True)
    with path.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"Drive download: {int(status.progress() * 100)}%")
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("Drive download produced an empty file.")
    return path

def duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True
    )
    return float(result.stdout.strip())

def render(source, profile):
    import edge_tts
    import asyncio

    narration = " ".join(profile["lines"])
    audio = WORK / "narration.mp3"
    asyncio.run(edge_tts.Communicate(narration, "en-US-JennyNeural", rate="+3%").save(str(audio)))

    source_duration = duration(source)
    if source_duration < 8:
        raise RuntimeError(f"Source video is too short ({source_duration:.1f}s).")
    clip_len = min(22.0, source_duration - 1.0)
    clip_start = min(12.0, max(0.0, (source_duration - clip_len) / 2))
    output = WORK / f"{profile['display'].lower()}_artist_intro.mp4"
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # 10-second profile title card, followed by a 22-second center-cropped fancam segment.
    # Both sections are 1080x1800 (9:15); source footage is center-cropped, never left-aligned.
    title = profile["title"].replace(":", r"\:")
    tag = profile["tag"].replace(":", r"\:")
    filter_complex = (
        "[0:v]scale=1080:1800:force_original_aspect_ratio=increase,"
        "crop=1080:1800,boxblur=24:8,eq=brightness=-0.28,"
        "drawbox=x=70:y=650:w=940:h=500:color=black@0.48:t=fill,"
        f"drawtext=fontfile={font}:text='{tag}':fontcolor=white@0.85:fontsize=34:"
        "x=(w-text_w)/2:y=760,"
        f"drawtext=fontfile={font}:text='{title}':fontcolor=white:fontsize=82:"
        "borderw=3:bordercolor=black@0.8:x=(w-text_w)/2:y=870,"
        "drawtext=fontfile=" + font + ":text='K-POP ARTIST SPOTLIGHT':fontcolor=white@0.9:"
        "fontsize=30:x=(w-text_w)/2:y=1010,trim=duration=10,setpts=PTS-STARTPTS[v0];"
        "[0:v]trim=start=" + f"{clip_start:.3f}" + ":duration=" + f"{clip_len:.3f}" +
        ",setpts=PTS-STARTPTS,scale=1080:1800:force_original_aspect_ratio=increase,"
        "crop=1080:1800,setsar=1,"
        "drawbox=x=0:y=0:w=1080:h=190:color=black@0.35:t=fill,"
        f"drawtext=fontfile={font}:text='{profile['display']}':fontcolor=white:fontsize=58:"
        "borderw=2:bordercolor=black@0.7:x=55:y=65[v1];"
        "[v0][v1]concat=n=2:v=1:a=0,format=yuv420p[v];"
        "[1:a]apad,atrim=duration=" + f"{10 + clip_len:.3f}" + "[a]"
    )
    run([
        "ffmpeg", "-y", "-i", str(source), "-i", str(audio),
        "-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
        "-t", f"{10 + clip_len:.3f}", "-r", "30",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)
    ])
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(output)],
        check=True, capture_output=True, text=True
    )
    stream = json.loads(probe.stdout)["streams"][0]
    if (int(stream["width"]), int(stream["height"])) != (1080, 1800):
        raise RuntimeError("Rendered intro failed 1080x1800 (9:15) validation.")
    print(f"Rendered intro: {output} | 1080x1800 | {10 + clip_len:.1f}s")
    return output

def upload(youtube, path, profile, source_name):
    channels = youtube.channels().list(part="id,snippet", mine=True).execute().get("items", [])
    if len(channels) != 1:
        raise RuntimeError(f"Cannot verify authorized YouTube channel (found {len(channels)}).")
    channel = channels[0]
    channel_title = channel.get("snippet", {}).get("title", "")
    expected_id = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()
    if expected_id and channel["id"] != expected_id:
        raise RuntimeError(f"Wrong YouTube channel: {channel_title} ({channel['id']}); upload blocked.")
    if not expected_id and not any(word in channel_title.casefold() for word in ("curioloop", "dailyneedle")):
        raise RuntimeError(f"Authorized channel is '{channel_title}', not recognized as CurioLoop/DailyNeedle. Upload blocked.")
    print(f"Verified upload channel: {channel_title} ({channel['id']})")
    title = f"{profile['display']} of {profile['group']} | K-pop Artist Spotlight #Shorts"
    description = (
        f"Meet {profile['display']} from {profile['group']} in this quick artist spotlight.\n\n"
        "Artist profile and performance highlight. Subscribe for more K-pop member introductions.\n\n"
        f"Source video: {source_name}\n"
        "#aespa #Kpop #Shorts"
    )
    body = {
        "snippet": {"title": title[:100], "description": description,
                    "tags": [profile["display"], profile["group"], "K-pop", "artist spotlight", "Shorts"],
                    "categoryId": "10"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    }
    response = youtube.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)
    ).execute()
    print(f"Uploaded public intro: https://youtube.com/shorts/{response['id']}")
    return response["id"]

def main():
    key = ARTIST.upper()
    rotate = key in {"ROTATE", "AUTO", "RANDOM"}
    if not rotate and key not in PROFILES:
        raise RuntimeError(f"Unsupported artist '{ARTIST}'. Supported: {', '.join(PROFILES)}")
    drive, youtube = services()
    folder_id = required("FANCAMVERSE_DRIVE_FOLDER_ID")
    history = load_history()
    processed = {x.get("drive_file_id") for x in history["processed"]}
    selected = None
    keys = list(PROFILES)
    if rotate:
        # Prefer the member least recently published, while requiring a matching new Drive source.
        last_published = {}
        for entry in history["processed"]:
            last_published[entry.get("artist", "")] = max(
                last_published.get(entry.get("artist", ""), ""),
                entry.get("published_at", "")
            )
        keys.sort(key=lambda name: last_published.get(name, ""))
    else:
        keys = [key]
    for candidate_key in keys:
        found = [x for x in list_videos(drive, folder_id)
                 if x["id"] not in processed and any(alias in x.get("name", "").casefold()
                 for alias in ([candidate_key.casefold()] + ({
                     "KARINA": ["카리나"], "WINTER": ["윈터"],
                     "GISELLE": ["지젤"], "NINGNING": ["닝닝"]
                 }.get(candidate_key, []))))]
        print(f"Artist intro source scan: {len(found)} unprocessed {candidate_key} video(s).")
        if found:
            key, selected = candidate_key, found[0]
            break
    if selected is None:
        raise RuntimeError("No new Drive source found for any supported aespa member; no video was uploaded.")
    profile = PROFILES[key]
    item = selected
    source = download(drive, item)
    output = render(source, profile)
    if os.getenv("INTRO_OUTPUT_ONLY", "true").lower() not in {"0", "false", "no"}:
        print(f"PREVIEW_ONLY: {output}")
        return
    video_id = upload(youtube, output, profile, item["name"])
    history["processed"].append({
        "drive_file_id": item["id"], "source_name": item["name"],
        "artist": key, "youtube_video_id": video_id,
        "published_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    })
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
