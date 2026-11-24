import asyncio
import json
import os
import re
import subprocess
import textwrap
import time
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


def make_script(previous_titles=None, featured_gender="woman"):
    """Generate general-knowledge Shorts with relevant licensed visuals."""
    api_key = required("GEMINI_API_KEY")
    model = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
    previous_titles = previous_titles or []
    prompt = """Create one original English YouTube Short for adult male viewers interested in women, attraction, beauty, style and relationship psychology.
Choose accurate, engaging topics such as women's beauty and fashion trends, first impressions, attraction psychology, dating communication, grooming, fitness and social behavior. Keep the perspective informative and respectful; never objectify women or make manipulative dating claims. No Korean entertainment news.
Return ONLY JSON keys: title, hook_text, narration, visual_theme, featured_person, visual_queries.
Choose one specific verified fact; never invent statistics or studies. Avoid medical/financial advice.
20-35 seconds; narration 60-85 natural English words; title 35-45 characters maximum, concise and punchy;
hook_text 3-6 words; immediate curiosity hook that follows the casual opening.
AUDIENCE ANGLE: frame the hook around what adult men commonly notice, misunderstand or find interesting about women, while avoiding stereotypes and treating women as people rather than props.
NARRATION OPENING RULES:
Start with a relatable everyday-life observation, familiar habit, small frustration, or casual question.
Do NOT begin by naming the scientific concept, stating the fact, or sounding like a lecture.
Use a conversational bridge from the everyday situation into the surprising fact within the next sentence.
Example style only: "Ever notice how you feel more awake after a quick walk?" Then explain the real reason.
Keep the opening natural, specific to the topic, and accurate; do not use generic clickbait.
VISUAL RULES:
Use confident adult women as the primary visual focus when relevant, with tasteful fashion, beauty, wellness and fitness footage. The first shot must be a real licensed stock VIDEO of an adult woman wearing tasteful athletic/workout clothing,
doing a natural fitness-related action (walking into a gym, stretching, tying running shoes, warming up,
or exercising). Attractive, healthy, confident presentation is fine; fully clothed and non-sexualized.
Use a medium or full-body shot with the woman clearly visible. Do not use a still image for the opening.
Then transition to real footage of the exact topic/action: actual animal for animal facts,
actual exercise footage for fitness facts, actual food/preparation for food facts,
and relevant objects/experiments for science.
Do not replace the opening woman with a man, portrait-only image, unrelated scenery, or abstract graphic.
If suitable licensed opening footage or topic footage is unavailable, fail rather than use mismatched footage.
Return exactly 5 English stock-VIDEO search queries in this order:
1) "adult woman workout outfit gym stretching fitness full body video" for the fixed opening.
2) "adult woman stylish fashion elegant fitted outfit confident full body video" for a tasteful, eye-catching adult-woman fashion/glamour shot.
3) "adult woman fitness fashion stylish outfit confident movement video" for a second tasteful adult-woman shot.
4) and 5) must show the exact topic/action. All people must be clearly adults, fully clothed and non-explicit; no lingerie, nudity, sexual activity or minors. Keep the two glamour shots natural and relevant to the subject's mood. featured_person must be exactly "fit adult woman".
Return valid JSON only."""
    if os.getenv("CONTENT_MODE", "general").lower() == "beauty":
        prompt = """Create one original English YouTube Short for adult male viewers interested in women, focused on practical beauty, skincare, haircare, massage, fashion, personal presentation and attraction psychology. Use a respectful, informative angle about trends, first impressions or everyday self-care without objectifying women or promising results.
Choose one useful, evidence-aware everyday tip. Do not promise medical or cosmetic results; do not diagnose or prescribe treatments. Avoid unsupported claims and fabricated studies.
Return ONLY JSON keys: title, hook_text, narration, visual_theme, featured_person, visual_queries.
20-35 seconds; narration 60-85 natural English words; title concise and curiosity-driven; hook_text 3-6 words.
Open with a relatable beauty or self-care question, then give one practical takeaway and a memorable close.
VISUAL RULES: first shot is a licensed video of an attractive adult woman in tasteful, fully clothed fitness or self-care clothing, clearly visible in a medium/full-body shot. Follow with beauty-related adult footage: professional facial massage, gentle skincare routine, scalp massage or haircare, and a confident stylish adult woman.
Use exactly 5 English stock-video search queries in this order:
1) adult woman workout outfit gym stretching fitness full body video
2) professional facial massage adult woman spa skincare treatment video
3) adult woman scalp massage hair care salon video
4) adult woman skincare routine applying moisturizer beauty salon video
5) stylish adult woman confident fashion full body video
All people must be clearly adults and fully clothed. No nudity, lingerie, sexual activity, minors, medical claims or invasive procedures. Return valid JSON only."""
    # Alternate the featured adult subject while keeping the same topic-first format.
    if featured_gender == "man":
        prompt += "\nFEATURED SUBJECT: The featured person in this episode must be an attractive, confident adult man. The first three visuals must feature a man sweating after a workout, a professional sports massage involving an adult man, and an adult man exercising. Do not use women as the featured subject in these opening visuals. Keep him fully clothed and non-sexualized."
        data_featured_person = "fit adult man"
        opening_visual_queries = [
            "adult man sweating after cardio workout gym fitness full body video",
            "professional sports massage adult man athlete recovery video",
            "adult man fitness workout gym stretching full body exercise video",
        ]
    else:
        prompt += "\nFEATURED SUBJECT: The featured person in this episode must be an attractive, confident adult woman. The first three visuals must feature a woman sweating after a workout, a professional massage involving an adult woman, and an adult woman exercising. Keep her fully clothed and non-sexualized."
        data_featured_person = "fit adult woman"
        opening_visual_queries = [
            "adult woman sweating after cardio workout gym fitness full body video",
            "professional adult woman massage therapist giving shoulder massage spa wellness video",
            "adult woman fitness workout gym stretching full body exercise video",
        ]
    if previous_titles:
        prompt += (
            "\nSTRICT ORIGINALITY: Do not reuse the same central fact, topic angle, example, "
            "or narrative structure from any item below. Choose a clearly different subject "
            "and build a fresh script, not a paraphrase. Previously used scripts (title | theme | opening): "
            + " || ".join(previous_titles[-30:])
        )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    print(f"Generating general-knowledge CurioLoop Short with Gemini model: {model}")
    payload = {"contents":[{"parts":[{"text":prompt}]}],
               "generationConfig":{"temperature":0.85,"responseMimeType":"application/json"}}
    response = None
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(1, 6):
        try:
            response = requests.post(url, params={"key": api_key},
                json=payload, timeout=90)
            if response.status_code in retryable_statuses:
                if attempt == 5:
                    response.raise_for_status()
                wait_seconds = min(8 * (2 ** (attempt - 1)), 60)
                print(f"Gemini temporarily unavailable (HTTP {response.status_code}); "
                      f"retrying in {wait_seconds}s ({attempt}/5).")
                time.sleep(wait_seconds)
                continue
            response.raise_for_status()
            break
        except requests.exceptions.RequestException as exc:
            if attempt == 5:
                raise
            wait_seconds = min(8 * (2 ** (attempt - 1)), 60)
            print(f"Gemini request failed ({type(exc).__name__}); "
                  f"retrying in {wait_seconds}s ({attempt}/5).")
            time.sleep(wait_seconds)
    if response is None:
        raise RuntimeError("Gemini request failed without a response after retries.")
    data=json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
    for key in ("title","hook_text","narration","visual_theme"):
        if not isinstance(data.get(key),str) or not data[key].strip():
            raise ValueError(f"Gemini response missing {key}")
    # Alternate female- and male-focused episodes; opening footage follows the selected subject.
    data["featured_person"] = data_featured_person
    if not isinstance(data.get("visual_queries"),list) or len(data["visual_queries"])<3:
        raise ValueError("Gemini response must include 3 topic-specific stock-video queries")
    data["visual_queries"] = opening_visual_queries
    return data


async def make_voice(text):
    path = WORK / "voice.mp3"
    communicate = edge_tts.Communicate(text, "en-US-AriaNeural", rate="+2%")
    await communicate.save(str(path))
    return path


def make_ass(narration):
    # Adaptive phrase captions: keep each beat compact and readable on a 9:16 phone screen.
    words = narration.split()
    groups = []
    current = []
    current_chars = 0
    for word in words:
        clean = re.sub(r"[^A-Za-z0-9']", "", word)
        word_chars = max(1, len(clean))
        proposed_chars = current_chars + (1 if current else 0) + len(word)
        # 2-3 words per beat; split earlier when a phrase would become too wide.
        if current and (len(current) >= 3 or proposed_chars > 19):
            groups.append(current)
            current = []
            current_chars = 0
        current.append(word)
        current_chars += len(word)
        # Natural punctuation creates a short visual pause.
        if re.search(r"[.!?,;:]$", word):
            groups.append(current)
            current = []
            current_chars = 0
    if current:
        if len(current) == 1 and groups and sum(len(x) for x in groups[-1]) < 3:
            groups[-1].extend(current)
        else:
            groups.append(current)

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

    # Slightly smaller type when a phrase is naturally longer; retain a fixed safe-zone position.
    longest = max((len(" ".join(group)) for group in groups), default=0)
    font_size = 68 if longest > 16 else 74
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2", "PlayResX: 1080", "PlayResY: 1920", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Montserrat ExtraBold,{font_size},&H0000D7FF,&H00FFFFFF,&H00101010,&H80000000,1,0,1,5,2,2,120,120,390,1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]

    # Allocate phrase timing by spoken word length and add a small pause for punctuation.
    weights = []
    for group in groups:
        weight = 0.0
        for word in group:
            weight += max(2, len(re.sub(r"[^A-Za-z0-9']", "", word)))
            if re.search(r"[.!?]$", word):
                weight += 3.0
            elif re.search(r"[,;:]$", word):
                weight += 1.5
        weights.append(weight)
    total_weight = sum(weights) or 1.0
    cursor = 0.0
    for group, weight in zip(groups, weights):
        group_duration = duration * weight / total_weight
        karaoke_weights = [
            max(2, len(re.sub(r"[^A-Za-z0-9']", "", word))) +
            (2 if re.search(r"[.!?]$", word) else 0)
            for word in group
        ]
        karaoke_total = sum(karaoke_weights) or 1
        karaoke = []
        for word, word_weight in zip(group, karaoke_weights):
            centiseconds = max(1, round(group_duration * 100 * word_weight / karaoke_total))
            karaoke.append(f"{{\\kf{centiseconds}}}{word}")
        text = " ".join(karaoke)
        end = min(cursor + group_duration, duration)
        lines.append(
            f"Dialogue: 0,{stamp(cursor)},{stamp(end)},Default,,0,0,0,,{{\\an2}}{text}"
        )
        cursor = end

    path = WORK / "captions.ass"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path, duration


def fetch_video_clips(title, visual_theme, visual_queries, featured_person, count=5):
    """Use only tightly topic-matched licensed assets; never fill with unrelated stock footage."""
    featured_person = (featured_person or "").strip()
    stock_person_mode = featured_person.lower() in {"fit adult man", "fit adult woman"}
    subject = featured_person if stock_person_mode else (featured_person if featured_person and featured_person.lower() != "none" else title.strip())
    generated_terms = [str(q).strip() for q in (visual_queries or []) if str(q).strip()]
    if featured_person.lower() == "fit adult man":
        opening_queries = ["adult man sweating after cardio workout gym fitness full body video"]
        glamour_queries = [
            "professional sports massage adult man athlete recovery video",
            "adult man fitness workout gym stretching full body exercise video",
        ]
    else:
        opening_queries = ["adult woman sweating after cardio workout gym fitness full body video"]
        glamour_queries = [
            "professional adult woman massage therapist giving shoulder massage spa wellness video",
            "adult woman fitness workout gym stretching full body exercise video",
        ]
    topic_queries = generated_terms[1:5] if len(generated_terms) > 1 else [title.strip(), visual_theme]    query_terms = list(dict.fromkeys([*opening_queries, *glamour_queries, *topic_queries, title.strip(), visual_theme]))
    query_terms = [q for q in query_terms if q and q.lower() != "none"]
    session = requests.Session()
    session.headers.update({"User-Agent": "CurioLoop/1.0"})
    assets_dir = WORK / "visual_assets"
    assets_dir.mkdir(exist_ok=True)
    clips, credits = [], []

    # Persistent visual-asset deduplication across separate GitHub Actions runs.
    history_path = ROOT / "history" / "used_visuals.json"
    history_path.parent.mkdir(exist_ok=True)
    try:
        history_data = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        history_data = {}
    used_assets = set(history_data.get("asset_keys", []))

    def asset_keys(asset_id=None, page_url=None, media_url=None):
        keys = []
        for value in (asset_id, page_url, media_url):
            value = str(value or "").strip()
            if value:
                keys.append(re.sub(r"/$", "", value.split("?")[0]).lower())
        return keys

    def already_used(asset_id=None, page_url=None, media_url=None):
        keys = asset_keys(asset_id, page_url, media_url)
        return any(key in used_assets for key in keys)

    def remember_asset(asset_id=None, page_url=None, media_url=None):
        used_assets.update(asset_keys(asset_id, page_url, media_url))
        # Keep a rolling history to prevent unbounded repository growth.
        history_data["asset_keys"] = sorted(used_assets)[-1500:]
        history_data["updated_at"] = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat()
        history_path.write_text(
            json.dumps(history_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )

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
        if stock_person_mode:
            if query == (visual_terms[0] if visual_terms else ""):
                person_terms = {"man", "men", "male", "woman", "women", "female"}
                fitness_terms = {"fitness", "workout", "gym", "athlete", "athletic", "muscle", "muscular", "sport", "training", "bodybuilding", "exercise"}
                return any(w in words for w in person_terms) and any(w in words for w in fitness_terms)
            generic_terms = {"adult","woman","women","female","man","men","male","close","up",
                "portrait","vertical","video","footage","stock","reaction","full","body",
                "head","toe","fit","fitness","athletic","person","people"}
            topic_terms = [w.lower() for w in re.findall(r"[A-Za-z0-9가-힣]+", query or "")
                           if len(w)>=3 and w.lower() not in generic_terms]
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

    # Require an athletic adult man/woman opening; following clips must match the topic.
    opening_found = False

    # Pixabay stock video is accepted only when its tags explicitly match the subject/query.
    pixabay_key = os.getenv("PIXABAY_API_KEY")
    if pixabay_key and len(clips) < count:
        for query_index, query in enumerate(query_terms):
            if len(clips) >= count:
                break
            if opening_found and query_index == 0:
                continue
            try:
                response = session.get("https://pixabay.com/api/videos/",
                    params={"key": pixabay_key, "q": query, "per_page": 30, "safesearch": "true"},
                    timeout=30)
                response.raise_for_status()
                for item in response.json().get("hits", []):
                    tags = item.get("tags", "")
                    asset_id = f"pixabay:{item.get('id', '')}" if item.get("id") else None
                    page_url = item.get("pageURL", "")
                    video_url = ((item.get("videos", {}).get("large") or
                                  item.get("videos", {}).get("medium") or
                                  item.get("videos", {}).get("small") or {}).get("url", ""))
                    if already_used(asset_id, page_url, video_url):
                        print(f"Skipping previously used Pixabay clip: {page_url or asset_id}")
                        continue
                    if not relevant_metadata(tags, query):
                        print(f"Rejecting unrelated Pixabay result: {tags}")
                        continue
                    if query_index in (1, 2):
                        words = set(re.findall(r"[a-z]+", str(tags).lower()))
                        adult_woman_terms = {"woman", "women", "female"}
                        glamour_terms = {"fashion", "style", "model", "elegant", "beauty", "dress", "fitness", "confident", "portrait", "outfit"}
                        if not (words & adult_woman_terms) or not (words & glamour_terms):
                            print(f"Skipping non-matching glamour footage: {tags}")
                            continue
                    variants = item.get("videos", {})
                    chosen = variants.get("large") or variants.get("medium") or variants.get("small")
                    if not chosen or not chosen.get("url"):
                        continue
                    try:
                        render_video(chosen["url"], len(clips)+1, "Pixabay",
                                     "Pixabay Content License (check current terms)",
                                     item.get("pageURL", ""), str(item.get("user", "Pixabay contributor")))
                        remember_asset(asset_id, item.get("pageURL", ""), chosen.get("url", ""))
                        if query_index == 0 and len(clips) == 1:
                            opening_found = True
                            print(f"Accepted CurioLoop athletic opening from Pixabay query: {query}")
                    except Exception as exc:
                        print(f"Skipping Pixabay video: {type(exc).__name__}: {exc}")
                    query_limit = 1 if query_index in (0, 1, 2) else count
                    if len(clips) >= query_limit:
                        break
            except Exception as exc:
                print(f"Pixabay search failed for '{query}': {type(exc).__name__}: {exc}")

    # The opening must be a real licensed video clip, not a still image.
    if not opening_found:
        raise RuntimeError(
            "Required CurioLoop athletic opening not found: a licensed video tagged with an adult "
            "man/woman and fitness/workout action is mandatory. No substitute footage was used."
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
                    page_url = item.get("image", {}).get("contextLink") or item.get("displayLink", "")
                    if already_used(page_url=page_url, media_url=image_url):
                        print(f"Skipping previously used Google image: {page_url or image_url}")
                        continue
                    context = item.get("title", "") + " " + item.get("snippet", "")
                    if not image_url or not relevant_metadata(context, query):
                        print(f"Rejecting unrelated Google image result: {context[:120]}")
                        continue
                    page_url = item.get("image", {}).get("contextLink") or item.get("displayLink", "")
                    try:
                        render_still(image_url, len(clips)+1, "Google Custom Search result",
                                     "Reuse-rights filter applied; verify original page license",
                                     page_url, item.get("title", "Creator not supplied"))
                        remember_asset(page_url=page_url, media_url=image_url)
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
                        page_title = page.get("title", "Wikimedia Commons image")
                        commons_page_url = "https://commons.wikimedia.org/wiki/" + page_title.replace(" ", "_")
                        if already_used(page_url=commons_page_url, media_url=info.get("url", "")):
                            print(f"Skipping previously used Commons image: {commons_page_url}")
                            continue
                        meta = info.get("extmetadata") or {}
                        license_name = (meta.get("LicenseShortName", {}).get("value") or "").strip()
                        normalized = license_name.lower().replace("-", " ").strip()
                        allowed = normalized.startswith(("cc0", "cc by", "public domain", "pd ")) or "public domain" in normalized
                        image_url = info.get("thumburl") or info.get("url")
                        if not allowed or not image_url or info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
                            continue
                        try:
                            render_still(image_url, len(clips)+1, "Wikimedia Commons", license_name,
                                         commons_page_url,
                                         (meta.get("Artist", {}).get("value") or "Author not listed").strip())
                            remember_asset(page_url=commons_page_url, media_url=info.get("url", ""))
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
    """Draw a larger, rounder and extra-cute CurioLoop squirrel mascot."""
    path = WORK / "loopy_mascot.png"
    scale = 3
    image = Image.new("RGBA", (420 * scale, 360 * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def p(coords):
        return tuple(int(v * scale) for v in coords)

    outline = (111, 53, 29, 255)
    orange = (211, 105, 42, 255)
    light_orange = (239, 145, 72, 255)
    cream = (255, 229, 190, 255)
    pink = (255, 169, 166, 255)

    # Large fluffy curled tail makes the squirrel silhouette broad and recognizable.
    draw.ellipse(p((222, 58, 410, 292)), fill=orange, outline=outline, width=7 * scale)
    draw.ellipse(p((247, 91, 384, 260)), fill=(255, 213, 157, 255))
    draw.ellipse(p((271, 116, 365, 235)), fill=light_orange)
    draw.ellipse(p((286, 134, 348, 213)), fill=cream)
    # Small fluffy tufts on the outside of the tail.
    draw.ellipse(p((354, 73, 405, 137)), fill=orange, outline=outline, width=5 * scale)
    draw.ellipse(p((360, 215, 410, 274)), fill=orange, outline=outline, width=5 * scale)

    # Ears peek out above the oversized head.
    draw.ellipse(p((73, 20, 164, 126)), fill=orange, outline=outline, width=6 * scale)
    draw.ellipse(p((91, 43, 147, 111)), fill=pink)
    draw.ellipse(p((207, 20, 298, 126)), fill=orange, outline=outline, width=6 * scale)
    draw.ellipse(p((224, 43, 280, 111)), fill=pink)

    # Wide, chubby body and belly.
    draw.ellipse(p((101, 170, 314, 348)), fill=orange, outline=outline, width=7 * scale)
    draw.ellipse(p((139, 211, 278, 337)), fill=cream)

    # Big rounded head with soft cheeks.    draw.ellipse(p((58, 57, 316, 267)), fill=orange, outline=outline, width=7 * scale)
    draw.ellipse(p((77, 77, 297, 247)), fill=light_orange)
    draw.ellipse(p((99, 165, 274, 252)), fill=cream)

    # Large sparkling eyes, raised brows and rosy cheeks.
    draw.arc(p((112, 105, 166, 132)), start=190, end=345, fill=outline, width=4 * scale)
    draw.arc(p((213, 105, 267, 132)), start=195, end=350, fill=outline, width=4 * scale)
    draw.ellipse(p((121, 126, 157, 174)), fill=(48, 35, 31, 255))
    draw.ellipse(p((218, 126, 254, 174)), fill=(48, 35, 31, 255))
    draw.ellipse(p((130, 133, 141, 146)), fill=(255, 255, 255, 255))
    draw.ellipse(p((227, 133, 238, 146)), fill=(255, 255, 255, 255))
    draw.ellipse(p((105, 178, 151, 202)), fill=(255, 116, 133, 105))
    draw.ellipse(p((226, 178, 272, 202)), fill=(255, 116, 133, 105))

    # Tiny nose, happy smile and two little front teeth.
    draw.ellipse(p((181, 177, 197, 190)), fill=(92, 48, 39, 255))
    draw.arc(p((157, 180, 220, 224)), start=15, end=165, fill=outline, width=5 * scale)
    draw.rounded_rectangle(p((174, 207, 185, 222)), radius=3 * scale,
                           fill=(255, 255, 250, 255), outline=outline, width=2 * scale)
    draw.rounded_rectangle(p((187, 207, 198, 222)), radius=3 * scale,
                           fill=(255, 255, 250, 255), outline=outline, width=2 * scale)

    # Little paws hugging a large acorn at the front.
    draw.ellipse(p((116, 247, 173, 291)), fill=light_orange, outline=outline, width=4 * scale)
    draw.ellipse(p((235, 247, 292, 291)), fill=light_orange, outline=outline, width=4 * scale)
    draw.ellipse(p((174, 249, 231, 319)), fill=(151, 87, 45, 255),
                 outline=outline, width=4 * scale)
    draw.pieslice(p((169, 243, 236, 283)), start=180, end=360,
                  fill=(112, 72, 39, 255), outline=outline, width=3 * scale)
    draw.line([p((203, 248)), p((210, 233))], fill=(91, 125, 58, 255),
              width=5 * scale)

    # A soft ground shadow anchors the character.
    draw.ellipse(p((85, 333, 345, 355)), fill=(0, 0, 0, 45))
    image = image.resize((420, 360), Image.Resampling.LANCZOS)
    image.save(path)
    return path

def render(title, hook_text, voice, captions, duration, visual_theme, visual_queries, featured_person):
    output = WORK / f"curioloop_short_{__import__('time').strftime('%Y%m%d_%H%M%S')}.mp4"
    title_path = WORK / "title.txt"
    # Keep the on-screen title inside the Shorts safe area: wrap to two lines.
    clean_title = re.sub(r"\s+", " ", title.replace("\\n", " ").replace("\n", " ")).strip()
    title_lines = textwrap.wrap(clean_title, width=22, break_long_words=True, break_on_hyphens=False)
    if len(title_lines) > 2:
        title_lines = [title_lines[0], textwrap.shorten(" ".join(title_lines[1:]), width=22, placeholder="…")]
    title_path.write_text("\n".join(title_lines), encoding="utf-8")
    hook_path = WORK / "hook.txt"
    hook_path.write_text(hook_text.replace("\\n", " ").strip(), encoding="utf-8")
    clips, credits = fetch_video_clips(title, visual_theme, visual_queries, featured_person, count=5)
    if not clips:
        raise RuntimeError("No usable topic-matched visual assets found; stopping video generation.")

    if len(clips) < 2:
        raise RuntimeError("The athletic opening was found, but no topic-related follow-up visual was found.")

    # Three-part person-led opening: post-workout sweat, professional massage, then exercise.
    # Keep this opening visually prominent, then devote the rest of the runtime to topic-matched footage.
    intro_clip_count = min(3, len(clips))
    intro_duration = min(8.0, max(6.0, duration * 0.28))
    intro_segment = intro_duration / intro_clip_count
    topic_clip_count = len(clips) - intro_clip_count
    topic_segment = (duration - intro_duration) / topic_clip_count if topic_clip_count else 0
    segment_durations = (
        [intro_segment] * intro_clip_count
        + ([topic_segment] * topic_clip_count if topic_clip_count else [])
    )
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
        "textfile=title.txt:fontcolor=white:fontsize=46:line_spacing=12:"
        "x=(w-text_w)/2:y=150:box=1:boxcolor=0x000000@0.45:boxborderw=24,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "textfile=hook.txt:fontcolor=yellow:fontsize=82:borderw=5:bordercolor=black:"
        "x=(w-text_w)/2:y=590:box=1:boxcolor=0x000000@0.58:boxborderw=28:"
        "enable='between(t,0,2.5)',"
        "ass=captions.ass[base];"
        f"[{len(clips)+1}:v]scale=190:163[mascot];"
        "[base][mascot]overlay=W-w-28:250:format=auto[vout]"
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

    # Persistent script history prevents repeated topics across separate workflow runs.
    script_history_path = ROOT / "history" / "used_scripts.json"
    script_history_path.parent.mkdir(exist_ok=True)
    try:
        script_history = json.loads(script_history_path.read_text(encoding="utf-8")) if script_history_path.exists() else {"scripts": []}
    except (json.JSONDecodeError, OSError):
        script_history = {"scripts": []}
    if not isinstance(script_history, dict) or not isinstance(script_history.get("scripts"), list):
        script_history = {"scripts": []}

    prior_scripts = script_history["scripts"]
    used_titles = [item.get("title", "") for item in prior_scripts if isinstance(item, dict) and item.get("title")]
    recent_prompts = [
        " | ".join([
            str(item.get("title", "")),
            str(item.get("visual_theme", "")),
            str(item.get("narration", ""))[:110],
        ]).strip(" |")
        for item in prior_scripts[-30:] if isinstance(item, dict)
    ]

    for index in range(1, count + 1):
        print(f"\\n========== SHORT {index}/{count} ==========")
        try:
            duplicate_title_keys = {
                re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
                for title in used_titles
            }
            # Alternate female-focused and male-focused videos across batches using saved script history.
            featured_gender = "woman" if len(prior_scripts) % 2 == 0 else "man"
            print(f"Featured subject rotation: adult {featured_gender}")
            script = None
            for attempt in range(3):
                candidate = make_script(recent_prompts, featured_gender=featured_gender)
                candidate_key = re.sub(r"[^a-z0-9 ]", "", candidate["title"].lower()).strip()
                if candidate_key not in duplicate_title_keys:
                    script = candidate
                    break
                print(f"Duplicate title returned; requesting a fresh script ({attempt + 1}/3): {candidate['title']}")
                recent_prompts.append(
                    "REJECTED DUPLICATE: " + candidate["title"] + " | Do not reuse this topic or angle."
                )
            if script is None:
                raise RuntimeError("Gemini returned duplicate titles three times; refusing to reuse a script.")
            used_titles.append(script["title"])
            recent_prompts.append(" | ".join([
                str(script.get("title", "")),
                str(script.get("visual_theme", "")),
                str(script.get("narration", ""))[:110],
            ]).strip(" |"))
            prior_scripts.append({
                "title": script["title"],
                "visual_theme": script.get("visual_theme", ""),
                "narration": script.get("narration", ""),
                "featured_person": script.get("featured_person", ""),
                "created_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
            })
            # Keep a rolling record of the 300 most recent generated scripts.
            script_history["scripts"] = prior_scripts[-300:]
            script_history_path.write_text(
                json.dumps(script_history, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8"
            )
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