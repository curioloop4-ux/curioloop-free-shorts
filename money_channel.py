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
        prompt = """请制作一部面向简体中文观众的经济类YouTube纪录片。主题围绕财富习惯、消费心理、储蓄、复利、通胀或家庭经济。禁止投资标的推荐、买卖指令、保证收益、虚构故事和伪造数据。只返回JSON：title, description, sections, sources。sections恰好5段，每段包含heading, narration, visual_queries，使用自然简体中文。每段提供2个英文Pixabay视频搜索词，画面与旁白直接相关，并可适当使用漂亮女性、健身男性、商务人士和高品质生活方式镜头。数据必须有可靠来源，不确定就不写。sources提供2至5个真实官方来源URL，不得编造。"""
    else:
        topic = topic or "为什么有些人越来越有钱？"
        prompt = f"""请创作一条面向简体中文观众的YouTube Shorts经济知识短视频。主题：{topic}。解释一个具体、可信的经济或金钱心理观点，不要把财富归因于外貌或单一习惯。只返回JSON：title, hook_text, narration, description, visual_queries, sources。全部文案使用自然口语化简体中文。title不超过45个汉字；hook_text是前2秒的短钩子；narration约90至130个汉字，适合20至30秒配音，结构为强钩子、一个具体观点或例子、简短结论。不得虚构统计、研究、名言或收益承诺。visual_queries必须提供5个英文Pixabay视频搜索词：至少3个与脚本内容直接相关，另外2个分别用于自然融入身材健美的成年男性、形象时尚有魅力的成年女性镜头（例如健身、商务、品质生活、自我管理场景）；人物镜头必须与旁白主题有联系，不得使用性暗示、无关摆拍或未成年人。sources提供2至3个真实官方或可信机构URL；不确定具体页面时使用机构主页，绝不编造。只返回JSON。"""
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
            if len(clips) >= (10 if IS_LONG else 5):
                break
    minimum = 6 if IS_LONG else 3
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

    topics = [
        "为什么有些人越来越有钱？解释资产积累、消费习惯和复利思维",
        "为什么越会花钱的人，反而可能越有钱？解释消费决策、预算和长期价值",
        "普通人怎样建立自己的第一份资产？解释储蓄率、持续投入和时间的作用",
    ]
    output_only = os.getenv("OUTPUT_ONLY", "false").lower() in {"1", "true", "yes"}
    for index, topic in enumerate(topics, start=1):
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
