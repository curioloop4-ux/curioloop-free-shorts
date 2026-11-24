import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

import edge_tts
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).parent
WORK = ROOT / "work_money"
WORK.mkdir(exist_ok=True)
MODEL = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
MODE = os.getenv("MONEY_CONTENT_TYPE", "shorts").lower()
IS_LONG = MODE == "longform"
WIDTH, HEIGHT = (1920, 1080) if IS_LONG else (1080, 1920)
VOICE = os.getenv("MONEY_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
GENERIC = {"about", "with", "from", "into", "real", "video", "footage", "cinematic",
           "close", "wide", "shot", "background", "documentary", "concept", "financial"}


def required(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def generate_content(topic=None, topic_category='经济'):
    if IS_LONG:
        prompt = """请制作一部面向简体中文成年男性观众的YouTube纪录片，受众对女性魅力、时尚美容、第一印象、两性沟通和关系心理感兴趣。主题可结合女性时尚与美容趋势、吸引力心理、约会沟通、社交行为、个人形象及相关消费文化；保持尊重、客观，不物化女性、不宣扬操控技巧、不制造性别对立。若选择经济主题，应从男性观众关心的消费、形象产业和社会趋势切入。禁止投资标的推荐、买卖指令、保证收益、虚构故事和伪造数据。只返回JSON：title, description, sections, sources。sections恰好5段，每段包含heading, narration, visual_queries，使用自然简体中文。每段提供2个英文Coverr或Pixabay视频搜索词，必须是与旁白直接对应的真实人物场景。画面优先出现外形出众、气质自信、穿着得体时尚的成年女性；需要时搭配成年男性互动场景。优先全身镜头或腰部以上中景，人物完整清晰、脸部可见、主体居中。明确避免手部特写、被裁掉的头部或半截身体、远处小人物、背影遮挡和模糊人物。禁止用无关风景或抽象图形充当主体。数据必须有可靠来源，不确定就不写。sources提供2至5个真实官方来源URL，不得编造。"""
    else:
        topic = topic or "为什么有些人越来越有钱？"
        if topic_category not in {"时尚穿搭", "美容与形象", "健身与自我管理", "魅力与心理", "按摩与放松"}:
            prompt = f"""请创作一条面向简体中文成年男性观众的Shorts。受众对女性魅力、时尚美容、第一印象和关系心理感兴趣。主题：{topic}。优先从男性观众关注的女性消费趋势、时尚美容产业、社交心理或关系中的金钱心理切入，保持尊重客观，不物化女性。解释具体可信的观点。只返回JSON：title, hook_text, narration, description, visual_queries, sources。全部使用自然简体中文。标题不超过45字，开头2秒有强钩子，旁白约90至130字。不得虚构统计或收益承诺。visual_queries提供5个英文视频搜索词。第1个必须为“adult woman workout outfit gym stretching fitness full body video”，作为固定开场：真实授权素材中的成年女性穿着得体运动服，进行自然的健身热身、拉伸或运动，全身或中景，脸部清楚。第2至5个必须对应本条经济主题，优先出现成年男女在工作、购物、创业、消费或讨论预算的真实场景；全身或腰部以上中景，人物清楚并占画面主体。排除手部特写、无人物风景、钞票硬币特写及未成年人。sources提供2至3个真实可信来源URL，不得编造。"""
        else:
            prompt = f"""请创作一条面向简体中文成年男性观众的高吸引力Shorts。受众对女性外形、时尚美容、魅力心理、第一印象和两性沟通感兴趣。类别：{topic_category}。主题：{topic}。从男性观众好奇的问题切入，内容围绕女性时尚穿搭、发型、护肤、健身塑形、个人形象、第一印象、吸引力心理、约会沟通或自我管理，必须与主题一致。尊重女性，不物化、不刻板化、不提供操控或骚扰建议。开头2秒用反差问题或强钩子吸引观看。只返回JSON：title, hook_text, narration, description, visual_queries, sources。全部使用自然简体中文。标题不超过45字，旁白约90至130字。不得虚构研究、数据或效果承诺。visual_queries提供5个英文视频搜索词。第1个必须为“adult woman workout outfit gym stretching fitness full body video”，作为固定开场。若主题类别为美容与形象或按摩与放松，后续搜索词优先安排professional facial massage、adult woman scalp massage、spa skincare treatment、shoulder massage等真实按摩与护肤场景，至少3个搜索词明确包含massage或spa，并确保画面中成年顾客和/或专业理疗师清楚可见。其他主题则使用与主题直接相关的成年人物活动。禁止未成年人、裸露色情、手部特写、无人物风景。sources提供2至3个真实可信来源URL；健康知识优先专业机构来源，不得编造。"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.55, "responseMimeType": "application/json"},
    }
    response = None
    for attempt in range(5):
        try:
            response = requests.post(
                url, params={"key": required("GEMINI_API_KEY")},
                json=payload, timeout=150,
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                break
            if attempt == 4:
                response.raise_for_status()
            wait = min(30, 5 * (2 ** attempt))
            print(f"Gemini temporarily unavailable (HTTP {response.status_code}); retrying in {wait}s ({attempt + 1}/5).")
            time.sleep(wait)
        except requests.exceptions.RequestException as exc:
            if attempt == 4:
                raise
            wait = min(30, 5 * (2 ** attempt))
            print(f"Gemini request failed: {exc}; retrying in {wait}s ({attempt + 1}/5).")
            time.sleep(wait)
    if response is None:
        raise RuntimeError("Gemini request failed without a response.")
    data = json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError("Gemini did not return a title.")
    if IS_LONG:
        sections = data.get("sections")
        if not isinstance(sections, list) or len(sections) != 5:
            raise ValueError("Long-form content must contain exactly five sections.")
        for section in sections:
            if not section.get("heading") or not section.get("narration"):
                raise ValueError("A documentary section is missing its heading or narration.")
            if not isinstance(section.get("visual_queries"), list) or len(section["visual_queries"]) != 2:
                raise ValueError("Each documentary section needs two video queries.")
        data["narration"] = "\n\n".join(section["narration"] for section in sections)
        data["visual_queries"] = [query for section in sections for query in section["visual_queries"]]
    else:
        if not isinstance(data.get("narration"), str) or not (60 <= len(data["narration"]) <= 220):
            raise ValueError("Chinese narration should be short enough for a 20-30 second Short.")
        if not isinstance(data.get("visual_queries"), list) or len(data["visual_queries"]) < 5:
            raise ValueError("Five visual search queries are required, including the fixed adult-woman fitness opening.")
        data["visual_queries"] = [str(query).strip() for query in data["visual_queries"][:5]]
        data["visual_queries"][0] = "adult woman workout outfit gym stretching fitness full body video"
    if not isinstance(data.get("sources"), list) or len(data["sources"]) < 2:
        raise ValueError("At least two reference sources are required.")
    return data

async def make_voice(text):
    path = WORK / "narration.mp3"
    await edge_tts.Communicate(text, VOICE, rate="-3%", volume="+0%").save(str(path))
    return path


def duration_of(path):
    return float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ], text=True).strip())


def ass_time(seconds):
    cs = int(seconds * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def make_subtitles(text, duration):
    """Chinese phrase-by-phrase karaoke captions matching CurioLoop's bold yellow/white style."""
    clean_text = re.sub(r"\s+", "", text)
    segments = re.split(r"(?<=[，。！？；：])", clean_text)
    groups = []
    for segment in segments:
        segment = segment.strip()
        while len(segment) > 8:
            groups.append(segment[:8])
            segment = segment[8:]
        if segment:
            groups.append(segment)
    if not groups:
        groups = [clean_text or "有钱研究所"]

    def stamp(seconds):
        cs = int(seconds * 100)
        h, rem = divmod(cs, 360000)
        m, rem = divmod(rem, 6000)
        sec, centisec = divmod(rem, 100)
        return f"{h}:{m:02d}:{sec:02d}.{centisec:02d}"

    weights = []
    for group in groups:
        weights.append(sum(2.0 if char in "，。！？；：、" else 1.0 for char in group))
    total_weight = sum(weights) or 1.0
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2",
        f"PlayResX: {WIDTH}", f"PlayResY: {HEIGHT}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Noto Sans CJK SC,{72 if IS_LONG else 74},&H0000D7FF,&H00FFFFFF,&H00101010,&H80000000,1,0,1,5,2,2,120,120,{390 if not IS_LONG else 180},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    cursor = 0.0
    for group, weight in zip(groups, weights):
        group_duration = duration * weight / total_weight
        char_weights = [2 if char in "，。！？；：、" else 1 for char in group]
        char_total = sum(char_weights) or 1
        karaoke = []
        for char, char_weight in zip(group, char_weights):
            centiseconds = max(1, round(group_duration * 100 * char_weight / char_total))
            karaoke.append(f"{{\\kf{centiseconds}}}{char}")
        end_time = min(cursor + group_duration, duration)
        lines.append(
            f"Dialogue: 0,{stamp(cursor)},{stamp(end_time)},Default,,0,0,0,,{{\\an2}}{''.join(karaoke)}"
        )
        cursor = end_time

    path = WORK / "captions.ass"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

def download_relevant_clips(queries):
    """Prefer Coverr's official API; fall back to Pixabay when Coverr has no suitable results."""
    target = 10 if IS_LONG else 5
    minimum = 6 if IS_LONG else 3
    session = requests.Session()
    session.headers.update({"User-Agent": "MoneyKnowHow/1.0"})
    coverr_key = os.getenv("COVERR_API_KEY", "").strip()
    pixabay_key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not coverr_key and not pixabay_key:
        raise RuntimeError("Set COVERR_API_KEY or PIXABAY_API_KEY in GitHub Actions secrets.")

    # Use topic-specific search phrases from the generated script.
    queries = [str(query).strip() for query in queries if str(query).strip()]
    clips, credits, seen = [], [], set()
    opening_found = IS_LONG

    def normalize_download(url, number):
        raw = WORK / f"raw_{number}.mp4"
        normalized = WORK / f"clip_{number}.mp4"
        with session.get(url, stream=True, timeout=120) as download:
            download.raise_for_status()
            with raw.open("wb") as out:
                for chunk in download.iter_content(1024 * 1024):
                    if chunk:
                        out.write(chunk)
        if IS_LONG:
            video_filter = (
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
                "fps=30,format=yuv420p"
            )
            ffmpeg_args = ["-vf", video_filter]
        else:
            # Shorts: fill the vertical 9:16 frame and center-crop landscape footage.
            # Do not shrink the full horizontal frame into a small inset or blurred layout.
            video_filter = (
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={WIDTH}:{HEIGHT}:(in_w-out_w)/2:(in_h-out_h)/2,"
                "fps=30,format=yuv420p"
            )
            ffmpeg_args = ["-vf", video_filter]
        # Keep opening exercise/massage shots on screen longer before cutting away.
        clip_seconds = 10 if not IS_LONG else 8
        subprocess.run([
            "ffmpeg", "-y", "-i", str(raw), "-t", str(clip_seconds),
            *ffmpeg_args,
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-movflags", "+faststart", str(normalized)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
        raw.unlink(missing_ok=True)
        return normalized

    def search_coverr(query):
        response = session.get(
            "https://api.coverr.co/videos",
            headers={"x-api-key": coverr_key},
            params={"query": query, "page_size": 30, "urls": "true", "sort": "popular"},
            timeout=40,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("hits", payload.get("videos", []))

    def search_pixabay(query):
        response = session.get(
            "https://pixabay.com/api/videos/",
            params={"key": pixabay_key, "q": query, "per_page": 30, "safesearch": "true"},
            timeout=35,
        )
        response.raise_for_status()
        return response.json().get("hits", [])

    # Search order is intentional: the first footage is always a woman exercising,
    # followed by massage/spa footage when the topic is beauty or relaxation.
    for source in ("Coverr", "Pixabay"):
        if len(clips) >= target:
            break
        if source == "Coverr" and not coverr_key:
            print("COVERR_API_KEY is not set; skipping Coverr.")
            continue
        if source == "Pixabay" and not pixabay_key:
            print("PIXABAY_API_KEY is not set; skipping Pixabay fallback.")
            continue
        print(f"Searching {source} for topic-matched people footage.")
        for query_index, query in enumerate(queries):
            if len(clips) >= target:
                break
            if not IS_LONG and query_index == 0 and opening_found:
                continue
            if not IS_LONG and query_index > 0 and not opening_found:
                continue
            try:
                hits = search_coverr(query) if source == "Coverr" else search_pixabay(query)
            except requests.exceptions.RequestException as exc:
                print(f"{source} search failed for '{query}': {exc}")
                continue
            hits = sorted(hits, key=lambda item: item.get("duration", 0), reverse=True)
            for item in hits:
                item_id = str(item.get("id", ""))
                if not item_id or item_id in seen:
                    continue
                tags = item.get("tags", [])
                if isinstance(tags, list):
                    tag_text = " ".join(str(tag.get("name", tag) if isinstance(tag, dict) else tag) for tag in tags).lower()
                else:
                    tag_text = str(tags).lower()
                title_desc = " ".join(str(item.get(k, "")) for k in ("title", "description")).lower()
                searchable = f"{tag_text} {title_desc}"
                # Keep human-centered footage only. Topic matching is intentionally not required:
                # the user prefers attractive, fit adult men/women as visual hooks across topics.
                people_terms = (
                    "person", "people", "woman", "women", "man", "men", "female", "male",
                    "businesswoman", "businessman", "employee", "customer", "couple",
                    "entrepreneur", "worker", "model", "portrait", "fashion", "fitness", "massage", "spa", "therapist", "skincare", "facial", "beauty", "salon", "scalp"
                )
                if not any(term in searchable for term in people_terms):
                    continue
                if not IS_LONG and query_index == 0:
                    woman_terms = ("woman", "women", "female", "girl")
                    fitness_terms = ("fitness", "workout", "gym", "stretch", "yoga", "exercise", "training", "athletic")
                    if not any(term in searchable for term in woman_terms) or not any(term in searchable for term in fitness_terms):
                        continue
                bad_terms = (
                    "hand close up", "hands close up", "finger close up", "fingers close up",
                    "hand closeup", "finger closeup", "isolated hand", "desert", "sand dune",
                    "sand dunes", "arid", "wilderness", "mountain landscape", "empty landscape",
                    "scenic landscape", "nature landscape", "ocean landscape", "no people",
                    "without people"
                )
                if any(term in searchable for term in bad_terms):
                    continue

                if source == "Coverr":
                    urls = item.get("urls") or {}
                    download_url = urls.get("mp4_download") or urls.get("mp4") or urls.get("mp4_preview")
                    page_url = item.get("url") or f"https://coverr.co/videos/{item_id}"
                    creator = item.get("creator") or item.get("author") or "Coverr"
                else:
                    variants = item.get("videos", {})
                    variant = variants.get("large") or variants.get("medium") or variants.get("small")
                    download_url = variant.get("url") if variant else None
                    page_url = item.get("pageURL", "")
                    creator = item.get("user", "creator")
                if not download_url:
                    continue
                try:
                    normalized = normalize_download(download_url, len(clips) + 1)
                except (requests.exceptions.RequestException, subprocess.SubprocessError, OSError) as exc:
                    print(f"Could not process {source} clip {item_id}: {exc}")
                    continue
                clips.append(normalized)
                if not IS_LONG and query_index == 0 and not opening_found:
                    opening_found = True
                    print("Accepted fixed adult-woman fitness opening.")
                seen.add(item_id)
                if source == "Coverr":
                    credits.append(f"Coverr video: {page_url} | Coverr attribution")
                else:
                    credits.append(f"Pixabay: {creator} | {page_url} | Check current Pixabay Content License")
                print(f"Selected {source} clip {item_id}: {item.get('title', query)}")
                break  # at most one clip per query
    if not IS_LONG and not opening_found:
        raise RuntimeError("Required adult-woman fitness opening was not found in licensed stock video; refusing to use unrelated footage.")
    if len(clips) < minimum:
        raise RuntimeError(
            f"Only {len(clips)} relevant licensed clips found; need at least {minimum}. "
            "Check COVERR_API_KEY and PIXABAY_API_KEY; no unrelated filler will be used."
        )
    return clips, credits

def render_video(data, voice, clips):
    montage_list = WORK / "montage.txt"
    montage_list.write_text("\n".join(f"file '{p.resolve()}'" for p in clips), encoding="utf-8")
    montage = WORK / "montage.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(montage_list),
                    "-c", "copy", str(montage)], cwd=WORK, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
    duration = duration_of(voice)
    subtitles = make_subtitles(data["narration"], duration)
    output = WORK / ("money_documentary.mp4" if IS_LONG else "money_short.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(montage), "-i", str(voice),
        "-vf", f"subtitles={subtitles}:fontsdir=/usr/share/fonts/opentype/noto",
        "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output)
    ], cwd=WORK, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
    return output


def upload_video(path, data, credits):
    token_name = "MONEY_YOUTUBE_REFRESH_TOKEN"
    creds = Credentials(
        None, refresh_token=required(token_name),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("MONEY_YOUTUBE_CLIENT_ID"),
        client_secret=required("MONEY_YOUTUBE_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    youtube = build("youtube", "v3", credentials=creds)
    sources = "\n".join(f"- {url}" for url in data["sources"])
    visual_credits = "\n".join(dict.fromkeys(credits))
    description = (
        data.get("description", "欢迎来到有钱研究所，一起了解财富、金钱心理与经济知识。") +
        "\n\n参考资料\n" + sources +
        "\n\n视频素材来源\n" + visual_credits +
        "\n\n本视频仅用于经济与金融知识分享，不构成个人投资建议。"
    )
    body = {
        "snippet": {
            "title": data["title"][:100],
            "description": description[:4900],
            "tags": ["有钱研究所", "财富知识", "金钱心理学", "富人习惯", "理财常识", "经济科普"],
            "categoryId": "27",
        },
        "status": {"privacyStatus": os.getenv("MONEY_YOUTUBE_PRIVACY_STATUS", "unlisted"),
                   "selfDeclaredMadeForKids": False},
    }
    result = youtube.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True,
                                   chunksize=8 * 1024 * 1024),
    ).execute()
    print("Uploaded to the channel authorized by MONEY_YOUTUBE_REFRESH_TOKEN. Video ID:", result.get("id"))



TOPIC_BANK = [
    ("money_psychology_01", "돈의 심리", "为什么人们总会冲动购买并后悔？"),
    ("money_psychology_02", "돈의 심리", "为什么越焦虑的人越容易乱花钱？"),
    ("money_psychology_03", "돈의 심리", "为什么打折会让人买下原本不需要的东西？"),
    ("money_psychology_04", "돈의 심리", "为什么人们更容易记住损失而不是收益？"),
    ("money_psychology_05", "돈의 심리", "为什么分期付款会让消费感觉更轻松？"),
    ("business_01", "기업과 비즈니스", "星巴克真正卖给顾客的只是咖啡吗？"),
    ("business_02", "기업과 비즈니스", "宜家为什么让顾客走过那么长的购物路线？"),
    ("business_03", "기업과 비즈니스", "Costco低利润模式为什么能够成功？"),
    ("business_04", "기업과 비즈니스", "麦当劳如何把标准化变成竞争优势？"),
    ("business_05", "기업과 비즈니스", "奢侈品牌为什么会主动控制商品供应？"),
    ("business_06", "기업과 비즈니스", "订阅制商业模式为什么越来越常见？"),
    ("business_07", "기업과 비즈니스", "免费应用通常通过什么方式赚钱？"),
    ("business_08", "기업과 비즈니스", "品牌联名为什么能提高消费者的购买欲？"),
    ("economy_01", "경제 상식", "通货膨胀为什么会悄悄降低现金的购买力？"),
    ("economy_02", "경제 상식", "为什么央行调整利率会影响普通人的生活？"),
    ("economy_03", "경제 상식", "汇率上涨为什么会影响进口商品价格？"),
    ("economy_04", "경제 상식", "供需关系如何决定商品价格？"),
    ("economy_05", "경제 상식", "经济衰退通常会怎样影响就业和消费？"),
    ("economy_06", "경제 상식", "为什么有些国家会出现住房负担过重的问题？"),
    ("economy_07", "경제 상식", "GDP增长为什么不代表每个人都更富有？"),
    ("economy_08", "경제 상식", "为什么同样的钱在不同国家购买力不同？"),
    ("wealth_01", "부의 격차와 자산", "为什么收入相同的人多年后资产差距会很大？"),
    ("wealth_02", "부의 격차와 자산", "资产和收入有什么关键区别？"),
    ("wealth_03", "부의 격차와 자산", "复利为什么需要时间才能发挥作用？"),
    ("wealth_04", "부의 격차와 자산", "紧急备用金为什么是家庭财务的第一道防线？"),
    ("wealth_05", "부의 격차와 자산", "储蓄率为什么有时比工资增长更重要？"),
    ("wealth_06", "부의 격차와 자산", "负债什么时候会成为家庭财务的压力？"),
    ("consumer_01", "소비와 트렌드", "为什么奢侈品涨价后仍然有人愿意购买？"),
    ("consumer_02", "소비와 트렌드", "网红推荐为什么会影响消费者的选择？"),
    ("consumer_03", "소비와 트렌드", "便利店如何通过商品摆放影响购买决定？"),
    ("consumer_04", "소비와 트렌드", "为什么限时优惠会让人产生紧迫感？"),
    ("consumer_05", "소비와 트렌드", "二手商品市场为什么越来越受关注？"),
    ("consumer_06", "소비와 트렌드", "消费者为什么愿意为便利支付更高价格？"),
    ("consumer_07", "소비와 트렌드", "外卖平台的低价优惠背后有哪些商业逻辑？"),
    ("technology_01", "기술과 미래산업", "人工智能会改变哪些行业的工作方式？"),
    ("technology_02", "기술과 미래산업", "为什么芯片会影响全球科技产业竞争？"),
    ("technology_03", "기술과 미래산업", "电动汽车产业链包含哪些关键环节？"),
    ("technology_04", "기술과 미래산업", "数据为什么会成为企业的重要资产？"),
    ("technology_05", "기술과 미래산업", "机器人普及可能怎样改变服务业？"),
    ("technology_06", "기술과 미래산업", "云计算如何帮助小企业降低技术门槛？"),
    ("history_01", "경제와 역사", "历史上的郁金香狂热为何成为泡沫案例？"),
    ("history_02", "경제와 역사", "工业革命如何改变普通人的工作和生活？"),
    ("history_03", "경제와 역사", "信用货币是如何逐渐成为现代经济基础的？"),
    ("history_04", "경제와 역사", "集装箱运输如何改变全球贸易？"),
    ("history_05", "경제와 역사", "为什么一些曾经繁荣的商业城市会衰落？"),
    ("work_01", "일과 생산성", "为什么提高工作效率不一定意味着工作更久？"),
    ("work_02", "일과 생산성", "技能积累如何影响一个人的长期收入能力？"),
    ("work_03", "일과 생산성", "远程办公给企业管理带来了哪些变化？"),
    ("work_04", "일과 생산성", "为什么沟通成本会影响团队生产力？"),
    ("world_01", "세계와 산업", "全球供应链中断为什么会影响日常商品？"),
    ("world_02", "세계와 산업", "为什么港口是全球贸易的重要节点？"),
    ("world_03", "세계와 산업", "旅游业如何带动一个城市的其他产业？"),
    ("world_04", "세계와 산업", "人口老龄化会给消费市场带来哪些变化？"),
    ("environment_01", "환경과 산업전환", "能源转型为什么需要巨额基础设施投资？"),
    ("environment_02", "환경과 산업전환", "电池回收为什么会成为重要的新产业？"),
    ("environment_03", "환경과 산업전환", "循环经济如何减少资源浪费并创造商业机会？"),
    ("science_01", "과학과 일상", "为什么时间感会随着年龄和经历发生变化？"),
    ("science_02", "과학과 일상", "睡眠不足会怎样影响判断和决策？"),
    ("science_03", "과학과 일상", "为什么人类大脑容易受到锚定效应影响？"),
]


APPEARANCE_TOPIC_BANK = [
    ("style_01", "时尚穿搭", "为什么有些穿搭看起来更显贵？"),
    ("style_02", "时尚穿搭", "普通衣服怎样穿出干净高级感？"),
    ("style_03", "时尚穿搭", "为什么合身比昂贵更重要？"),
    ("style_04", "时尚穿搭", "男生哪些穿衣细节最容易提升形象？"),
    ("style_05", "时尚穿搭", "女生怎样用简单搭配突出个人风格？"),
    ("beauty_01", "美容与形象", "为什么整洁感会影响第一印象？"),
    ("beauty_02", "美容与形象", "发型真的会改变一个人的整体气质吗？"),
    ("beauty_03", "美容与形象", "护肤时最容易被忽略的基础步骤是什么？"),
    ("beauty_04", "美容与形象", "为什么自然妆容越来越受欢迎？"),
    ("beauty_05", "美容与形象", "哪些日常习惯会让形象看起来更精神？"),
    ("fitness_01", "健身与自我管理", "为什么力量训练会改变整体体态？"),
    ("fitness_02", "健身与自我管理", "怎样改善久坐带来的不良体态？"),
    ("fitness_03", "健身与自我管理", "为什么规律运动能提升精神状态？"),
    ("fitness_04", "健身与自我管理", "健身新手最应该先建立什么习惯？"),
    ("fitness_05", "健身与自我管理", "为什么睡眠也是身材管理的一部分？"),
    ("attraction_01", "魅力与心理", "第一印象通常会受到哪些因素影响？"),
    ("attraction_02", "魅力与心理", "为什么自信的人更容易吸引注意？"),
    ("attraction_03", "魅力与心理", "一个人的肢体语言会传递什么信息？"),
    ("attraction_04", "魅力与心理", "为什么微笑会让人显得更有亲和力？"),
    ("attraction_05", "魅力与心理", "真正有吸引力的细节不只是外表吗？"),
]


MASSAGE_TOPIC_BANK = [
    ("massage_01", "按摩与放松", "面部按摩时有哪些温和且安全的注意事项？"),
    ("massage_02", "按摩与放松", "头皮按摩为什么会让人感觉放松？"),
    ("massage_03", "按摩与放松", "肩颈按摩前后应该注意哪些基本事项？"),
    ("massage_04", "按摩与放松", "为什么专业SPA护理通常强调轻柔手法？"),
    ("massage_05", "按摩与放松", "日常护肤中如何正确搭配简单的面部按摩？"),
    ("massage_06", "按摩与放松", "身体按摩和单纯放松休息有什么不同？"),
]


def reserve_topics(count=3):
    """Choose distinct economy, business and money topics without recent repeats."""
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    branch = os.getenv("GITHUB_REF_NAME", "main")
    if not token or not repo:
        raise RuntimeError("Persistent topic history requires the workflow GITHUB_TOKEN and repository context.")
    api = f"https://api.github.com/repos/{repo}/contents/topic_history.json"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    response = requests.get(api, headers=headers, params={"ref": branch}, timeout=30)
    if response.status_code == 200:
        saved = response.json()
        history = json.loads(__import__("base64").b64decode(saved["content"]).decode("utf-8"))
        file_sha = saved["sha"]
    elif response.status_code == 404:
        history, file_sha = {"recent_ids": [], "recent_topics": []}, None
    else:
        response.raise_for_status()

    recent_ids = history.get("recent_ids", [])
    topic_mode = os.getenv("MONEY_TOPIC_MODE", "economy").lower()
    topic_pool = MASSAGE_TOPIC_BANK if topic_mode == "beauty" else TOPIC_BANK
    available = [item for item in topic_pool if item[0] not in recent_ids]
    if len(available) < count:
        available = topic_pool[:]
    import random
    random.shuffle(available)
    selected = available[:count]

    history["recent_ids"] = (recent_ids + [item[0] for item in selected])[-len(topic_pool):]
    history["recent_topics"] = (history.get("recent_topics", []) +
        [{"id": item[0], "category": item[1], "topic": item[2]} for item in selected])[-len(TOPIC_BANK):]
    encoded = __import__("base64").b64encode(
        json.dumps(history, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")
    payload = {"message": "Track recent Chinese content topics",
               "content": encoded, "branch": branch}
    if file_sha:
        payload["sha"] = file_sha
    saved_response = requests.put(api, headers=headers, json=payload, timeout=30)
    saved_response.raise_for_status()
    print(f"Reserved Chinese {topic_mode} topics:")
    for item in selected:
        print(f" - [{item[1]}] {item[2]}")
    return [(item[1], item[2]) for item in selected]

def main():
    if MODE not in {"shorts", "longform"}:
        raise ValueError("MONEY_CONTENT_TYPE must be shorts or longform.")
    if IS_LONG:
        print(f"Creating 有钱研究所 long-form documentary with {MODEL}")
        data = generate_content()
        print("Selected:", data["title"])
        (WORK / "script.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        voice = asyncio.run(make_voice(data["narration"]))
        clips, credits = download_relevant_clips(data["visual_queries"])
        output = render_video(data, voice, clips)
        (WORK / "visual_credits.txt").write_text("\n".join(dict.fromkeys(credits)), encoding="utf-8")
        print("Rendered:", output, "| Duration:", round(duration_of(output), 1), "seconds")
        if os.getenv("OUTPUT_ONLY", "false").lower() in {"1", "true", "yes"}:
            print("OUTPUT_ONLY enabled; upload skipped.")
        else:
            upload_video(output, data, credits)
        return

    topics = reserve_topics(3)
    output_only = os.getenv("OUTPUT_ONLY", "false").lower() in {"1", "true", "yes"}
    start_short = max(1, min(3, int(os.getenv("MONEY_START_SHORT", "1"))))
    shorts_count = max(1, min(3, int(os.getenv("MONEY_SHORTS_COUNT", "1"))))
    selected_topics = topics[start_short - 1:start_short - 1 + shorts_count]
    print(f"Starting {len(selected_topics)} Short(s) at position {start_short}/3 ({os.getenv('MONEY_TOPIC_MODE', 'economy')} mode).")
    successful_uploads = 0
    for index, (topic_category, topic) in enumerate(selected_topics, start=start_short):
        print(f"\n========== SHORT {index}/3 ==========")
        try:
            print("Content category:", topic_category)
            data = generate_content(topic, topic_category)
            print("Selected:", data["title"])
            (WORK / f"script_{index}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            voice = asyncio.run(make_voice(data["narration"]))
            clips, credits = download_relevant_clips(data["visual_queries"])
            rendered = render_video(data, voice, clips)
            output = WORK / f"money_short_{index}.mp4"
            rendered.replace(output)
            (WORK / f"visual_credits_{index}.txt").write_text(
                "\n".join(dict.fromkeys(credits)), encoding="utf-8")
            print("Rendered:", output, "| Duration:", round(duration_of(output), 1), "seconds")
            if output_only:
                print("OUTPUT_ONLY enabled; upload skipped.")
            else:
                upload_video(output, data, credits)
                successful_uploads += 1
        except Exception as exc:
            error_text = str(exc)
            print(f"SHORT {index}/3 FAILED. Error: {error_text}")
            if "uploadLimitExceeded" in error_text or "exceeded the number of videos" in error_text:
                raise RuntimeError("YouTube daily upload quota reached. Video was rendered and saved as an artifact, but not uploaded. Retry after the quota resets.") from exc
            continue
    if not output_only and successful_uploads == 0:
        raise RuntimeError("No Chinese Shorts were uploaded successfully. Check the errors above; generated files are available as workflow artifacts.")


if __name__ == "__main__":
    main()
