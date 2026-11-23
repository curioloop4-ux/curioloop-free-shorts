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


def fetch_korean_entertainment_news(limit=20, topic=None):
    """Fetch recent Korean entertainment headlines, optionally focused on one trend topic."""
    url = "https://news.google.com/rss/search"
    params = {
        "q": (f'\"{topic}\" when:14d' if topic else '(K-pop OR 아이돌 OR 가수 OR 배우 OR 연예 OR 드라마 OR 영화 OR 예능 OR OTT OR 넷플릭스 OR 시상식 OR 콘서트) when:3d'),
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
    if not stories and topic:
        print(f"No matching news articles found for selected trend: {topic}. The script will be restricted to candidate metadata.")
        return []
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
    """Generate evergreen general-knowledge Shorts with adult women as recurring visuals."""
    api_key = required("GEMINI_API_KEY")
    model = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
    if not model or model == "gemini-2.5-flash":
        model = "gemini-3.5-flash-lite"
    previous_titles = previous_titles or []
    prompt = """Create one original English YouTube Short for a GLOBAL audience for CurioLoop.
Return to the channel's original format: surprising general knowledge, psychology, science,
history, nature, everyday mysteries, technology, and useful life knowledge.
Do NOT make Korean entertainment news or celebrity gossip the topic.

Return ONLY valid JSON with keys: title, hook_text, narration, visual_theme, featured_person, visual_queries.

Choose one specific, accurate, genuinely interesting fact that can be explained in 20-35 seconds.
Prefer well-established facts about human psychology, animals, space, science, history,
everyday objects, technology, geography, and misunderstood facts.
Never invent statistics, studies, quotations, experiments, dates, or scientific claims.
Avoid medical/financial advice, conspiracy theories, and sensational misinformation.
The first 2 seconds must create immediate curiosity with a concrete, accurate hook.
Title under 65 characters. hook_text must be 3-6 punchy words.
Narration must be 60-85 words in natural spoken English: hook, surprising fact,
short explanation, memorable payoff. No greetings or filler.

VISUAL RULE: Every video must begin with the same recognizable CurioLoop signature shot:
OPENING ONLY: a clearly visible adult woman in a FULL-BODY vertical shot, standing and facing the
camera directly with a warm, playful smile. Show her from head to toe; do not crop to only the face
or upper body. Keep the shot friendly, natural, and non-sexual.
The first visual query must always request: "adult woman full body standing facing camera smiling head to toe vertical video".
AFTER THE OPENING: the woman does NOT need to face the camera. Show natural side views, three-quarter
angles, candid movement, and action shots while she interacts with the subject of the fact. Do not use random unrelated scenery. Examples: for a cat fact,
show a woman cuddling or playing with a cat; for a dog fact, a woman petting a dog; for a food fact,
a woman tasting or preparing that food; for a science fact, a woman doing or observing a safe relevant
demonstration; for a space fact, a woman looking through a telescope or at a star display.
Keep her fully clothed and non-sexualized. She is an illustrative presenter, not someone claimed
to have discovered or experienced the fact.
Set featured_person exactly to "adult woman stock visual".
visual_queries must contain exactly 3 specific English stock-VIDEO searches. Query 1 must be exactly
the full-body, front-facing smiling opening shot described above. Queries 2 and 3 must show an adult
woman interacting with the concrete topic or object; these later shots may use natural side, three-quarter,
or candid angles and do NOT need to face the camera. Each query must include
"woman" and a specific action/topic. Cat examples: "woman cuddling a cat close up video", "woman playing
with pet cat at home video". For dogs, woman with dog; for food, woman tasting or cooking that food;
for nature, woman observing the relevant animal or plant. If the exact interaction is unavailable,
use a woman visibly reacting to a closely related topic visual, not unrelated scenery. Never use cityscapes, landscapes, buildings, cars, or abstract backgrounds
as the main visual.
No Korean entertainment topic. No markdown. Return valid JSON only.
"""
    if previous_titles:
        prompt += "\n\nDo not repeat these topics, facts, hooks, or angles already used: " + "; ".join(previous_titles[-30:])
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    print(f"Generating general-knowledge CurioLoop Short with Gemini model: {model}")
    response = requests.post(
        url, params={"key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.85, "responseMimeType": "application/json"}},
        timeout=90,
    )
    response.raise_for_status()
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(raw)
    for key in ("title", "hook_text", "narration", "visual_theme"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Gemini response missing {key}")
    data["featured_person"] = "adult woman stock visual"
    if not isinstance(data.get("visual_queries"), list) or len(data["visual_queries"]) < 3:
        raise ValueError("Gemini response must include 3 female stock-video visual queries")
    topic_queries = [str(q).strip() for q in data["visual_queries"] if str(q).strip()]
    signature_query = "adult woman full body standing facing camera smiling head to toe vertical video"
    data["visual_queries"] = [signature_query] + [q for q in topic_queries if q.lower() != signature_query.lower()][:2]
    if len(data["visual_queries"]) < 3:
        raise ValueError("Gemini returned fewer than 3 usable visual queries")
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



def fetch_video_clips(title, visual_theme, visual_queries, featured_person, count=5):
    """Use only tightly topic-matched licensed assets; never fill with unrelated stock footage."""
    featured_person = (featured_person or "").strip()
    stock_woman_mode = featured_person.lower() == "adult woman stock visual"
    subject = "adult woman" if stock_woman_mode else (featured_person if featured_person and featured_person.lower() != "none" else title.strip())
    visual_terms = [str(q).strip() for q in (visual_queries or []) if str(q).strip()][:3]
    query_terms = list(dict.fromkeys([
        *visual_terms, "adult woman close up portrait vertical video",
        "woman expressive face reaction video", subject, title.strip(),
    ]))
    query_terms = [q for q in query_terms if q and q.lower() != "none"]
    session = requests.Session()
    session.headers.update({"User-Agent": "CurioLoop/1.0"})
    assets_dir = WORK / "visual_assets"
    assets_dir.mkdir(exist_ok=True)
    clips, credits = [], []

    # For named people, generic stock footage is not a valid substitute.
    subject_words = [w.lower() for w in re.findall(r"[A-Za-z0-9가-힣]+", subject) if len(w) >= 2]
    generic_visual_terms = {
        "city", "cityscape", "street", "building", "buildings", "traffic", "road",
        "skyline", "architecture", "downtown", "travel", "landscape", "nature",
        "mountain", "beach", "sunset", "drone", "aerial", "urban", "night"
    }
    named_person = bool(featured_person and featured_person.lower() not in {"none", "adult woman stock visual"} and featured_person.lower() != title.strip().lower())
    def relevant_metadata(text, query):
        words = [w.lower() for w in re.findall(r"[A-Za-z0-9가-힣]+", text or "") if len(w) >= 2]
        if not words:
            return False
        normalized = " ".join(words)
        query_words = [w.lower() for w in re.findall(r"[A-Za-z0-9가-힣]+", query or "") if len(w) >= 2]
        meaningful = [w for w in query_words if w not in generic_visual_terms]
        if named_person:
            return bool(meaningful) and any(w in words for w in meaningful)
        if stock_woman_mode:
            # Require both an adult-woman indicator and a topic/action match from the query.
            woman_terms = {"woman", "women", "female", "girl", "lady"}
            if not any(w in words for w in woman_terms):
                return False
            query_lower = (query or "").lower()
            if "smiling playfully" in query_lower:
                # The signature opening is independent of the video's topic.
                # Require a woman, a smiling expression, and full-body/standing metadata.
                signature_terms = {"smile", "smiling", "laugh", "laughing", "happy", "cheerful", "joy", "grin"}
                body_terms = {"full", "body", "fullbody", "standing", "stand", "whole", "person"}
                return (any(w in words for w in signature_terms)
                        and any(w in words for w in body_terms))
            generic_terms = {
                "adult", "woman", "women", "female", "girl", "lady", "close", "up",
                "portrait", "vertical", "video", "footage", "stock", "reaction",
                "expressive", "face", "model", "beautiful", "young", "person",
                "facing", "camera", "playful", "silly", "tongue", "out", "sticking", "making"
            }
            topic_terms = [w.lower() for w in re.findall(r"[A-Za-z0-9가-힣]+", query or "")
                           if len(w) >= 3 and w.lower() not in generic_terms]
            # Topic-specific queries must match a concrete subject/action tag.
            return bool(topic_terms) and any(w in words for w in topic_terms)
        return bool(meaningful) and sum(1 for w in meaningful if w in words) >= min(2, len(meaningful))

    def render_still(url, index, source, license_name, credit_url, creator="Not provided"):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        image_path = assets_dir / f"licensed_image_{index}.jpg"
        image_path.write_bytes(response.content)
        clip_path = WORK / f"clip_{index}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(image_path),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                   "zoompan=z='min(zoom+0.0012,1.12)':x='iw/2-(iw/zoom/2)+sin(on/40)*18':"
                   "y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30,format=yuv420p",
            "-t", "8", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-movflags", "+faststart", str(clip_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
        clips.append(clip_path)
        credits.append(f"{source} — {creator} — {license_name} — {credit_url}")

    def render_video(url, index, source, license_name, credit_url, creator="Not provided"):
        response = session.get(url, timeout=90, stream=True)
        response.raise_for_status()
        video_path = assets_dir / f"licensed_video_{index}.mp4"
        with video_path.open("wb") as output_file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output_file.write(chunk)
        clip_path = WORK / f"clip_{index}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path), "-t", "8",
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-movflags", "+faststart", str(clip_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=240)
        clips.append(clip_path)
        credits.append(f"{source} — {creator} — {license_name} — {credit_url}")

    # The first clip must be a front-facing, clearly visible smiling woman's face, not merely any woman clip.
    signature_found = False

    # Pixabay stock video is accepted only when its tags explicitly match the subject/query.
    pixabay_key = os.getenv("PIXABAY_API_KEY")
    if pixabay_key and len(clips) < count:
        for query_index, query in enumerate(query_terms):
            if len(clips) >= count:
                break
            try:
                response = session.get("https://pixabay.com/api/videos/",
                    params={"key": pixabay_key, "q": query, "per_page": 30, "safesearch": "true"},
                    timeout=30)
                response.raise_for_status()
                for item in response.json().get("hits", []):
                    tags = item.get("tags", "")
                    if not relevant_metadata(tags, query):
                        print(f"Rejecting unrelated Pixabay result: {tags}")
                        continue
                    variants = item.get("videos", {})
                    chosen = variants.get("large") or variants.get("medium") or variants.get("small")
                    if not chosen or not chosen.get("url"):
                        continue
                    try:
                        render_video(chosen["url"], len(clips)+1, "Pixabay",
                                     "Pixabay Content License (check current terms)",
                                     item.get("pageURL", ""), str(item.get("user", "Pixabay contributor")))
                        if query_index == 0 and len(clips) == 1:
                            signature_found = True
                    except Exception as exc:
                        print(f"Skipping Pixabay video: {type(exc).__name__}: {exc}")
                    query_limit = 1 if query_index == 0 else count
                    if len(clips) >= query_limit:
                        break
            except Exception as exc:
                print(f"Pixabay search failed for '{query}': {type(exc).__name__}: {exc}")

    # The signature opening is mandatory and must be a real licensed video clip.
    # Do not substitute a still image, generic portrait, or unrelated footage.
    if not signature_found:
        raise RuntimeError(
            "Required CurioLoop signature opening not found: a licensed close-up video showing an "
            "adult woman's full face front-facing and smiling is mandatory. No substitute footage was used."
        )

    # Google Custom Search images: accept only explicit reuse-rights results and matching metadata.
    # Google image results are intentionally not allowed to satisfy the mandatory video opening.
    google_key, google_cx = os.getenv("GOOGLE_CSE_API_KEY"), os.getenv("GOOGLE_CSE_ID")
    if google_key and google_cx and len(clips) < count:
        for query in query_terms:
            if len(clips) >= count:
                break
            try:
                response = session.get("https://www.googleapis.com/customsearch/v1",
                    params={"key": google_key, "cx": google_cx, "q": query,
                            "searchType": "image", "rights": "cc_publicdomain,cc_attribute,cc_sharealike",
                            "num": min(10, count-len(clips))}, timeout=30)
                response.raise_for_status()
                for item in response.json().get("items", []):
                    image_url = item.get("link")
                    context = item.get("title", "") + " " + item.get("snippet", "")
                    if not image_url or not relevant_metadata(context, query):
                        print(f"Rejecting unrelated Google image result: {context[:120]}")
                        continue
                    page_url = item.get("image", {}).get("contextLink") or item.get("displayLink", "")
                    try:
                        render_still(image_url, len(clips)+1, "Google Custom Search result",
                                     "Reuse-rights filter applied; verify original page license",
                                     page_url, item.get("title", "Creator not supplied"))
                    except Exception as exc:
                        print(f"Skipping Google image result: {type(exc).__name__}: {exc}")
                    if len(clips) >= count:
                        break
            except Exception as exc:
                print(f"Google image search failed for '{query}': {type(exc).__name__}: {exc}")

    # Wikimedia Commons fallback, also requiring title metadata to match the topic.
    if len(clips) < count:
        api_url = "https://commons.wikimedia.org/w/api.php"
        for query in query_terms:
            if len(clips) >= count:
                break
            try:
                search_response = session.get(api_url, params={
                    "action": "query", "list": "search", "srsearch": query,
                    "srnamespace": 6, "srlimit": 40, "format": "json"
                }, timeout=30)
                search_response.raise_for_status()
                results = search_response.json().get("query", {}).get("search", [])
                titles = [x.get("title", "") for x in results if relevant_metadata(x.get("title", ""), query)]
                for offset in range(0, len(titles), 40):
                    info_response = session.get(api_url, params={
                        "action": "query", "titles": "|".join(titles[offset:offset+40]),
                        "prop": "imageinfo", "iiprop": "url|extmetadata",
                        "iiurlwidth": 1400, "format": "json"
                    }, timeout=30)
                    info_response.raise_for_status()
                    for page in info_response.json().get("query", {}).get("pages", {}).values():
                        info = (page.get("imageinfo") or [{}])[0]
                        meta = info.get("extmetadata") or {}
                        license_name = (meta.get("LicenseShortName", {}).get("value") or "").strip()
                        normalized = license_name.lower().replace("-", " ").strip()
                        allowed = normalized.startswith(("cc0", "cc by", "public domain", "pd ")) or "public domain" in normalized
                        image_url = info.get("thumburl") or info.get("url")
                        if not allowed or not image_url or info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
                            continue
                        page_title = page.get("title", "Wikimedia Commons image")
                        try:
                            render_still(image_url, len(clips)+1, "Wikimedia Commons", license_name,
                                         "https://commons.wikimedia.org/wiki/" + page_title.replace(" ", "_"),
                                         (meta.get("Artist", {}).get("value") or "Author not listed").strip())
                        except Exception as exc:
                            print(f"Skipping Commons image: {type(exc).__name__}: {exc}")
                        if len(clips) >= count:
                            break
                    if len(clips) >= count:
                        break
            except Exception as exc:
                print(f"Wikimedia Commons search failed for '{query}': {type(exc).__name__}: {exc}")

    if not clips:
        raise RuntimeError(
            f"No topic-matched licensed visuals found for '{subject}'. "
            "The workflow stopped rather than using unrelated stock footage. "
            "A licensed source containing the actual subject is required."
        )
    (WORK / "visual_credits.txt").write_text("\n".join(credits), encoding="utf-8")
    print(f"Collected {len(clips)} topic-matched licensed visual asset(s) for: {subject}")
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


def render(title, hook_text, voice, captions, duration, visual_theme, visual_queries, featured_person):
    output = WORK / f"curioloop_short_{__import__('time').strftime('%Y%m%d_%H%M%S')}.mp4"
    title_path = WORK / "title.txt"
    title_path.write_text(title.replace("\\n", " ").strip(), encoding="utf-8")
    hook_path = WORK / "hook.txt"
    hook_path.write_text(hook_text.replace("\\n", " ").strip(), encoding="utf-8")
    clips, credits = fetch_video_clips(title, visual_theme, visual_queries, featured_person, count=5)
    if not clips:
        raise RuntimeError("No usable topic-matched visual assets found; stopping video generation.")

    if len(clips) < 2:
        raise RuntimeError("The mandatory tongue-out opening was found, but no topic-related follow-up visual was found.")

    # Reserve the first 2.5 seconds for the signature shot; divide the remaining runtime
    # evenly across the topic-related clips.
    opening_duration = min(2.5, max(1.5, duration * 0.12))
    topic_segment = (duration - opening_duration) / (len(clips) - 1)
    segment_durations = [opening_duration] + [topic_segment] * (len(clips) - 1)
    cmd = ["ffmpeg", "-y"]
    for clip_path in clips:
        cmd += ["-stream_loop", "-1", "-i", str(clip_path)]
    cmd += ["-i", str(voice)]
    mascot = make_mascot()
    cmd += ["-loop", "1", "-i", str(mascot)]

    filters = []
    labels = []
    for i in range(len(clips)):
        clip_duration = segment_durations[i]
        filters.append(
            f"[{i}:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,trim=duration={clip_duration:.3f},"
            f"setpts=PTS-STARTPTS,setsar=1[v{i}]"
        )
        labels.append(f"[v{i}]")
    filters.append(
        "".join(labels) + f"concat=n={len(clips)}:v=1:a=0,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "textfile=title.txt:fontcolor=white:fontsize=54:"
        "x=(w-text_w)/2:y=150:box=1:boxcolor=0x000000@0.45:boxborderw=24,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "textfile=hook.txt:fontcolor=yellow:fontsize=82:borderw=5:bordercolor=black:"
        "x=(w-text_w)/2:y=590:box=1:boxcolor=0x000000@0.58:boxborderw=28:"
        "enable='between(t,0,2.5)',"
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
            "description": narration + ("\n\nVisual credits:\n" + "\n".join(credits) if credits else "") + "\n\nCurioLoop | Curious facts, surprising science, psychology, history and everyday mysteries.\n\n#Shorts #CurioLoop #Facts #DidYouKnow #Psychology #Science #Knowledge",
            "tags": ["Shorts", "CurioLoop", "Facts", "Did You Know", "General Knowledge", "Psychology", "Science", "History", "Interesting Facts"],
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
            print("Featured person:", script["featured_person"])
            print("Opening hook:", script["hook_text"])
            voice = asyncio.run(make_voice(script["narration"]))
            captions, duration = make_ass(script["narration"])
            rendered, credits = render(
                script["title"], script["hook_text"], voice, captions, duration,
                script["visual_theme"], script["visual_queries"], script["featured_person"]
            )
            if os.getenv("OUTPUT_ONLY", "false").lower() in {"1", "true", "yes"}:
                print(f"VIDEO_ONLY: Rendered MP4 saved at {rendered}; YouTube upload skipped.")
                uploaded += 1
            else:
                upload(rendered, script["title"], script["narration"], credits)
                uploaded += 1
        except Exception as exc:
            print(f"Short {index} failed: {type(exc).__name__}: {exc}")
            # Stop immediately when YouTube's channel-level upload limit is reached.
            # Continuing would waste generation requests on videos that cannot upload.
            if "uploadLimitExceeded" in str(exc) or "exceeded the number of videos they may upload" in str(exc):
                print("YouTube upload limit reached. Stopping this batch; retry after the channel limit resets.")
                break
        if index < count:
            time.sleep(8)

    print(f"\\nBatch finished: {uploaded}/{count} Short(s) created.")
    if uploaded == 0:
        raise RuntimeError("No Shorts were created successfully.")


if __name__ == "__main__":
    main()
