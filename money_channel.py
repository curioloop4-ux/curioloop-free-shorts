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


def generate_content(topic=None):
    if IS_LONG:
        prompt = """请制作一部面向简体中文观众的经济类YouTube纪录片。主题围绕财富习惯、消费心理、储蓄、复利、通胀或家庭经济。禁止投资标的推荐、买卖指令、保证收益、虚构故事和伪造数据。只返回JSON：title, description, sections, sources。sections恰好5段，每段包含heading, narration, visual_queries，使用自然简体中文。每段提供2个英文Pixabay视频搜索词，必须是与旁白直接对应的真实人物场景，突出成年人的表情、动作、互动和生活情境。人物优先选择穿着合身、剪裁利落服装的成年女性或成年男性，例如修身西装、合体衬衫、运动服或简洁时尚的日常服装；服装可以凸显身形，但整体保持高级、自然、非色情。禁止用硬币、钞票、金币、钞票堆、钱包特写、抽象金融图形或无关风景充当主体。数据必须有可靠来源，不确定就不写。sources提供2至5个真实官方来源URL，不得编造。"""
    else:
        topic = topic or "为什么有些人越来越有钱？"
        prompt = f"""请创作一条面向简体中文观众的YouTube Shorts经济知识短视频。主题：{topic}。解释一个具体、可信的经济或金钱心理观点，不要把财富归因于外貌或单一习惯。只返回JSON：title, hook_text, narration, description, visual_queries, sources。全部文案使用自然口语化简体中文。title不超过45个汉字；hook_text是前2秒的短钩子；narration约90至130个汉字，适合20至30秒配音，结构为强钩子、一个具体观点或例子、简短结论。不得虚构统计、研究、名言或收益承诺。visual_queries必须提供5个英文Pixabay视频搜索词，全部必须是与旁白内容直接对应的人物场景，明确描述成年人的身份、动作和环境（例如上班族加班、情侣讨论家庭预算、创业者经营店铺、消费者在商店比较商品、成年人学习技能或规划生活）。人物优先选择穿着合身、剪裁利落服装的成年女性或成年男性，例如修身西装、合体衬衫、运动服或简洁时尚的日常服装；服装可以凸显身形，但整体保持高级、自然、非色情。每个搜索词都要能找到以人物为主体、看得见动作或表情的实拍视频。严禁硬币、钞票、金币、钞票堆、钱包特写、抽象金融图形、纯文字动画、无关风景及无关人物摆拍；不得使用未成年人或性暗示画面。sources提供2至3个真实官方或可信机构URL；不确定具体页面时使用机构主页，绝不编造。只返回JSON。"""
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
            raise ValueError("Five visual search queries are required, including topic-relevant adult people scenes.")
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
    groups, current = [], ""
    if re.search(r"[\\u4e00-\\u9fff]", text):
        chunks = re.split(r"(?<=[，。！？；])", text.replace("\\n", " "))
        for chunk in chunks:
            chunk = chunk.strip()
            while len(chunk) > 16:
                groups.append(chunk[:16])
                chunk = chunk[16:]
            if chunk:
                groups.append(chunk)
    else:
        words = text.split()
        limit = 7 if IS_LONG else 5
        for word in words:
            current += (" " if current else "") + word
            if len(current.split()) >= limit or re.search(r"[.!?。！？]$", word):
                groups.append(current)
                current = ""
        if current:
            groups.append(current)
    weights = [sum(max(1, len(w)) for w in group) for group in groups]
    total = sum(weights) or 1
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2",
        f"PlayResX: {WIDTH}", f"PlayResY: {HEIGHT}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Noto Sans CJK KR,{54 if IS_LONG else 72},&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,1,0,1,4,2,2,100,100,{90 if IS_LONG else 300},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    cursor = 0.0
    for group, weight in zip(groups, weights):
        span = duration * weight / total
        caption = str(group).replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{ass_time(cursor)},{ass_time(min(duration, cursor + span))},Default,,0,0,0,,{caption}")
        cursor += span
    path = WORK / "captions.ass"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def download_relevant_clips(queries):
    key = required("PIXABAY_API_KEY")
    session = requests.Session()
    session.headers.update({"User-Agent": "MoneyKnowHow/1.0"})
    clips, credits, seen = [], [], set()
    for query in queries:
        if len(clips) >= (10 if IS_LONG else 5):
            break
        clips_before_query = len(clips)
        response = session.get("https://pixabay.com/api/videos/",
            params={"key": key, "q": query, "per_page": 30, "safesearch": "true"},
            timeout=35)
        response.raise_for_status()
        query_terms = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", query)
                       if len(w) >= 4 and w.lower() not in GENERIC]
        hits = sorted(response.json().get("hits", []),
                      key=lambda item: item.get("duration", 0), reverse=True)
        for item in hits:
            tags = (item.get("tags") or "").lower()
            if item.get("id") in seen or not any(term in tags for term in query_terms):
                continue
            variants = item.get("videos", {})
            variant = variants.get("large") or variants.get("medium") or variants.get("small")
            if not variant or not variant.get("url"):
                continue
            raw = WORK / f"raw_{len(clips)+1}.mp4"
            normalized = WORK / f"clip_{len(clips)+1}.mp4"
            with session.get(variant["url"], stream=True, timeout=90) as download:
                download.raise_for_status()
                with raw.open("wb") as out:
                    for chunk in download.iter_content(1024 * 1024):
                        if chunk:
                            out.write(chunk)
            subprocess.run([
                "ffmpeg", "-y", "-i", str(raw), "-t", "10", "-vf",
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},fps=30,format=yuv420p",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-movflags", "+faststart", str(normalized)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
            raw.unlink(missing_ok=True)
            clips.append(normalized)
            seen.add(item.get("id"))
            credits.append(f"Pixabay: {item.get('user','creator')} | {item.get('pageURL','')} | Check current Pixabay Content License")
            # Keep visual variety: at most one selected clip per query.
            if len(clips) > clips_before_query:
                break
    minimum = 6 if IS_LONG else 4
    if len(clips) < minimum:
        raise RuntimeError(f"Only {len(clips)} relevant licensed clips found; need at least {minimum}. No unrelated filler will be used.")
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


def reserve_topics(count=3):
    """Select distinct topics and persist recent IDs in the repository between workflow runs."""
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
    available = [item for item in TOPIC_BANK if item[0] not in recent_ids]
    if len(available) < count:
        # Only rotate after the full topic bank has been used.
        available = [item for item in TOPIC_BANK if item[0] not in recent_ids[-max(0, len(TOPIC_BANK)-count):]]
    import random
    random.shuffle(available)
    selected = []
    used_categories = set()
    # Prefer a different content category for every Short in the same batch.
    for item in available:
        if item[1] not in used_categories:
            selected.append(item)
            used_categories.add(item[1])
            if len(selected) == count:
                break
    if len(selected) < count:
        for item in available:
            if item not in selected:
                selected.append(item)
                if len(selected) == count:
                    break
    if len(selected) < count:
        raise RuntimeError("Not enough unused topics in the topic bank.")
    history["recent_ids"] = (recent_ids + [item[0] for item in selected])[-len(TOPIC_BANK):]
    history["recent_topics"] = (history.get("recent_topics", []) +
                                [{"id": item[0], "category": item[1], "topic": item[2]} for item in selected])[-len(TOPIC_BANK):]
    encoded = __import__("base64").b64encode(
        json.dumps(history, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")
    payload = {"message": "Track recently used Chinese channel topics",
               "content": encoded, "branch": branch}
    if file_sha:
        payload["sha"] = file_sha
    saved_response = requests.put(api, headers=headers, json=payload, timeout=30)
    saved_response.raise_for_status()
    print("Reserved distinct topics:")
    for item in selected:
        print(f" - [{item[1]}] {item[2]}")
    return [item[2] for item in selected]

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
    print(f"Starting Shorts batch at {start_short}/3.")
    for index, topic in enumerate(topics[start_short - 1:], start=start_short):
        print(f"\n========== SHORT {index}/3 ==========")
        try:
            data = generate_content(topic)
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
        except Exception as exc:
            print(f"SHORT {index}/3 FAILED; continuing with the next topic. Error: {exc}")
            continue


if __name__ == "__main__":
    main()
