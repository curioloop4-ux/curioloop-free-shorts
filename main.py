import asyncio
import json
import os
import re
import subprocess
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

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


def fetch_korean_entertainment_news(limit=20):
    """Fetch recent Korean entertainment headlines from Google News RSS."""
    url = "https://news.google.com/rss/search"
    params = {
        "q": '(K-pop OR 아이돌 OR 가수 OR 배우 OR 연예 OR 드라마 OR 영화 OR 예능 OR OTT OR 넷플릭스 OR 시상식 OR 콘서트) when:3d',
        "hl": "ko",
        "gl": "KR",
        "ceid": "KR:ko",
    }
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CurioLoop/1.0)"}
    response = requests.get(url, params=params, headers=headers, timeout=25)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    stories = []
    seen = set()
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        description = re.sub(r"\\s+", " ", description).strip()
        source_node = item.find("source")
        source = (source_node.text or "").strip() if source_node is not None else "Google News"
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        key = re.sub(r"\\W+", "", title.lower())
        if not title or key in seen:
            continue
        seen.add(key)
        stories.append({
            "title": title,
            "description": description[:700],
            "source": source,
            "published": published,
            "url": link,
        })
        if len(stories) >= limit:
            break
    if not stories:
        raise RuntimeError("Korean entertainment news feed returned no stories; refusing to generate an unrelated Short.")
    print("Korean entertainment headlines:")
    for story in stories[:10]:
        print(f"- [{story['source']}] {story['title']}")
    return stories


def fetch_trending_candidates(limit=20):
    """Run YouTube discovery and return metadata only; never download YouTube media."""
    if not os.getenv("YOUTUBE_API_KEY"):
        print("YOUTUBE_API_KEY not configured; using Korean entertainment news feed.")
        return []
    try:
        subprocess.run(["python", str(ROOT / "discover_trending.py")],
                       cwd=ROOT, check=True, timeout=180)
        reports = sorted((ROOT / "discovery").glob("trending_candidates_*.json"), reverse=True)
        if not reports:
            return []
        return json.loads(reports[0].read_text(encoding="utf-8"))[:limit]
    except Exception as exc:
        print(f"Trending discovery unavailable; continuing with news feed: {type(exc).__name__}: {exc}")
        return []


def make_script(previous_titles=None):
    api_key = required("GEMINI_API_KEY")
    model = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
    if not model or model == "gemini-2.5-flash":
        model = "gemini-3.5-flash-lite"

    stories = fetch_korean_entertainment_news()
    trending = fetch_trending_candidates()
    previous_titles = previous_titles or []
    trend_text = "\\n".join(
        f"- Candidate: {item.get('title', '')} | Channel: {item.get('channel', '')} | "
        f"Views: {item.get('views', 0)} | Views/hour: {item.get('views_per_hour', 0)} | "
        f"Categories: {', '.join(item.get('categories', []))} | Link: {item.get('url', '')}"
        for item in trending
    ) or "No YouTube trend candidates available."
    story_text = "\\n".join(
        f"- Headline: {s['title']}\\n  Publisher: {s['source']}\\n  Published: {s['published']}\\n  Available snippet: {s['description']}\\n  Source link: {s['url']}"
        for s in stories
    )
    prompt = """Create one original English YouTube Shorts highlight-style explainer for a GLOBAL audience, using ONLY one or more of the Korean entertainment stories below.
Return ONLY valid JSON with keys: title, narration, visual_theme, visual_queries.
CURRENT KOREAN ENTERTAINMENT NEWS (Google News RSS, recent feed):
""" + story_text + """

YOUTUBE TREND CANDIDATES (metadata only; use as topic signals, never as footage or verified reporting):
""" + trend_text + """

Channel identity: CurioLoop — Korean Entertainment Highlights for viewers worldwide. The channel covers the full Korean entertainment industry, not only K-pop.

Editorial scope:
- Cover the full Korean entertainment spectrum: K-pop artists and groups, music releases and charts, concerts, award shows, Korean actors, K-dramas, Korean films, streaming/OTT releases, variety shows, celebrity interviews, and the entertainment business.\n- Do not let K-pop dominate every video. Choose the strongest current story across categories; when suitable, rotate among music, drama, film, variety, and actors across a batch.
- Prioritize stories with international relevance: global chart results, major performances, comebacks, awards, streaming success, casting announcements, notable interviews, and cultural impact.
- Do not choose ordinary non-entertainment news.
- Use the publisher, headline, snippet, and link as leads. Never pretend to have read the full article if only a snippet is available.
- Use YouTube trend candidate titles and view velocity only to identify topics worth investigating. They are not evidence that claims are true and are not footage sources. Do not quote, translate, summarize, or imitate the candidate video itself.
- Do not invent facts, quotes, chart positions, view counts, ratings, dates, causes, or announcements. If a detail is not present in the supplied source material or reliably established background knowledge, omit it.
- Avoid unverified dating rumors, private-life speculation, gossip, defamatory allegations, and sensationalized controversy. Do not state allegations as facts.
- Do not copy or translate any publisher's article, Korean creator's video, script, footage, thumbnail, or distinctive presentation. Write an original concise explainer with context for international viewers.
- Explain unfamiliar Korean entertainment context briefly (for example, what MAMA is, how a comeback works, or why a chart result matters) only when relevant.
- Title under 65 characters; narration 75-105 words; first sentence must hook viewers and final sentence should deliver a clear takeaway.
- visual_theme describes the mood. visual_queries must be exactly 3 short, concrete English phrases for relevant illustrative stock footage matching the selected category (for example: Korean drama filming set, cinema audience reaction, Korean variety show studio, K-pop concert crowd). Do not imply generic stock footage depicts the named celebrity or a specific copyrighted production.
- No abstract backgrounds, generic color/style phrases, or unrelated stock footage. Do not include markdown.
"""
    if previous_titles:
        prompt += "\\n\\nDo not repeat these topics or angles already used in this batch: " + "; ".join(previous_titles[-20:])
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    print(f"Generating Korean entertainment Short with Gemini model: {model}")
    response = requests.post(
        url,
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.75, "responseMimeType": "application/json"}},
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

    # Only place source files here when editing and commercial-use permission is confirmed.
    approved_dir = ROOT / "licensed_clips"
    approved_files = sorted(
        p for p in approved_dir.glob("*")
        if p.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}
    ) if approved_dir.exists() else []
    for source in approved_files[:count]:
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                check=True, capture_output=True, text=True, timeout=20,
            )
            if float(probe.stdout.strip() or 0) < 3:
                continue
            target = WORK / f"clip_{len(clips)+1}.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(source), "-an", "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", str(target)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180,
            )
            clips.append(target)
            credits.append(f"Authorized source file supplied to CurioLoop: {source.name}")
            print(f"Using authorized source file: {source.name}")
        except Exception as exc:
            print(f"Skipping unusable authorized source {source.name}: {type(exc).__name__}: {exc}")

    if len(clips) >= count:
        print(f"Using {len(clips)} authorized source clip(s); skipping stock fallback.")
        return clips, credits

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


def make_mascot():
    """Draw the recurring CurioLoop mascot as a transparent PNG using Pillow."""
    path = WORK / "loopy_mascot.png"
    scale = 3
    image = Image.new("RGBA", (260 * scale, 320 * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def p(coords):
        return tuple(int(v * scale) for v in coords)

    # Soft drop shadow, antennae and round alien head.
    draw.ellipse(p((40, 285, 226, 310)), fill=(0, 0, 0, 65))
    draw.line([p((83, 86)), p((67, 34))], fill=(116, 190, 235, 255), width=7 * scale)
    draw.line([p((177, 86)), p((195, 34))], fill=(116, 190, 235, 255), width=7 * scale)
    draw.ellipse(p((51, 12, 82, 43)), fill=(145, 220, 255, 255), outline=(255, 255, 255, 255), width=3 * scale)
    draw.ellipse(p((180, 12, 211, 43)), fill=(145, 220, 255, 255), outline=(255, 255, 255, 255), width=3 * scale)
    draw.ellipse(p((25, 50, 235, 252)), fill=(120, 204, 250, 255), outline=(255, 255, 255, 255), width=7 * scale)
    draw.ellipse(p((39, 62, 221, 239)), fill=(159, 224, 255, 255))

    # Large friendly eyes, highlights and rosy cheeks.
    draw.ellipse(p((66, 112, 119, 180)), fill=(18, 35, 66, 255))
    draw.ellipse(p((143, 112, 196, 180)), fill=(18, 35, 66, 255))
    draw.ellipse(p((78, 121, 94, 139)), fill=(255, 255, 255, 255))
    draw.ellipse(p((155, 121, 171, 139)), fill=(255, 255, 255, 255))
    draw.ellipse(p((47, 177, 91, 198)), fill=(255, 145, 165, 110))
    draw.ellipse(p((171, 177, 215, 198)), fill=(255, 145, 165, 110))
    draw.arc(p((105, 166, 157, 210)), start=10, end=170, fill=(25, 55, 85, 255), width=6 * scale)

    # Small white hoodie and blue infinity-style chest mark.
    draw.rounded_rectangle(p((45, 225, 215, 307)), radius=25 * scale,
                           fill=(250, 252, 255, 255), outline=(90, 160, 210, 255), width=5 * scale)
    draw.arc(p((87, 218, 173, 275)), start=180, end=360, fill=(120, 170, 205, 255), width=5 * scale)
    draw.arc(p((105, 260, 137, 282)), start=190, end=530, fill=(25, 145, 225, 255), width=6 * scale)
    draw.arc(p((125, 260, 157, 282)), start=10, end=350, fill=(25, 145, 225, 255), width=6 * scale)

    image = image.resize((260, 320), Image.Resampling.LANCZOS)
    image.save(path)
    return path


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
    mascot = make_mascot()
    cmd += ["-loop", "1", "-i", str(mascot)]

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
        "ass=captions.ass[base];"
        f"[{len(clips)+1}:v]scale=150:185[mascot];"
        "[base][mascot]overlay=W-w-34:340:format=auto[vout]"
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
            "description": narration + ("\n\nVisual credits:\n" + "\n".join(credits) if credits else "") + "\n\nCurioLoop | Korean Entertainment Highlights\nK-pop • K-dramas • Korean movies • Variety shows • Korean stars\n\n#Shorts #CurioLoop #KoreanEntertainment #Kpop #Kdrama #KoreanMovies",
            "tags": ["Shorts", "CurioLoop", "Korean entertainment", "Kpop", "Kdrama", "Korean movies", "Korean variety shows", "Korean actors", "Korean celebrities"],
            "categoryId": "24",
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
    used_titles = []

    for index in range(1, count + 1):
        print(f"\\n========== SHORT {index}/{count} ==========")
        try:
            script = make_script(used_titles)
            title_key = re.sub(r"[^a-z0-9 ]", "", script["title"].lower()).strip()
            if title_key in {re.sub(r"[^a-z0-9 ]", "", t.lower()).strip() for t in used_titles}:
                raise RuntimeError(f"Duplicate title detected; refusing duplicate upload: {script['title']}")
            used_titles.append(script["title"])
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
            # Stop immediately when YouTube's channel-level upload limit is reached.
            # Continuing would waste Gemini and Pixabay requests on videos that cannot upload.
            if "uploadLimitExceeded" in str(exc) or "exceeded the number of videos they may upload" in str(exc):
                print("YouTube upload limit reached. Stopping this batch; retry after the channel limit resets.")
                break
        if index < count:
            time.sleep(8)

    print(f"\\nBatch finished: {uploaded}/{count} Short(s) uploaded.")
    if uploaded == 0:
        raise RuntimeError("No Shorts were uploaded successfully.")


if __name__ == "__main__":
    main()
