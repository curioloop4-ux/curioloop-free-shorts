import asyncio
import json
import os
import re
import subprocess
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
VOICE = os.getenv("MONEY_TTS_VOICE", "ko-KR-SunHiNeural")
GENERIC = {"about", "with", "from", "into", "real", "video", "footage", "cinematic",
           "close", "wide", "shot", "background", "documentary", "concept", "financial"}


def required(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def generate_content():
    if IS_LONG:
        prompt = """한국어 유튜브 경제 다큐멘터리 한 편을 기획하라.
주제는 부자들의 습관, 소비심리, 저축, 복리, 물가, 금리, 가계경제, 경제사 중 하나.
투자 종목 추천·매수매도 지시·확정적 수익 약속·과장된 부자 성공담 금지.
반드시 검증 가능한 경제 원리와 현실적인 사례를 중심으로 설명하고, 사실과 해석을 구분하라.
JSON만 반환: title, description, sections, sources.
sections는 정확히 5개 객체이며 각 객체는 heading, narration, visual_queries를 포함한다.
각 narration은 한국어 230~280 어절이 아니라 공백 기준 230~280개의 단어(어절)로 작성한다.
전체 분량은 약 1,150~1,400 어절, 자연스러운 내레이션 기준 6~9분을 목표로 한다.
구조: 강한 질문과 사례, 배경과 핵심 개념, 작동 원리, 현실 사례와 숫자, 오해·한계 및 실천 가능한 결론.
숫자·통계·연구·인용은 출처가 명확한 경우에만 사용하고, 확인 불가한 수치는 만들지 마라.
sources에는 한국은행, 통계청, 금융감독원, 한국거래소, 국세청, OECD, World Bank 등
실제로 존재하는 기관의 주제 관련 URL을 3~5개 포함하라. URL을 추측해 만들지 말고 확실하지 않으면 기관의 공식 메인/자료 페이지를 사용하라.
각 section의 visual_queries는 실제 화면에 보일 수 있는 영어 Pixabay 영상 검색어 2개.
일반적인 돈·도시 풍경만 반복하지 말고 내용과 직접 관련된 사물·행동·상황을 지정하라.
반환은 JSON만."""
    else:
        prompt = """한국어 유튜브 쇼츠용 경제·돈 상식 영상 하나를 작성하라.
주제는 부자들의 습관, 소비심리, 월급관리, 저축, 복리, 물가, 금리, 가계경제 중 하나.
투자 종목 추천, 매수매도 지시, 확정 수익 약속, 근거 없는 부자 명언 금지.
사실과 의견을 구분하고 확인되지 않은 통계나 연구를 절대 만들지 마라.
JSON만 반환: title, hook_text, narration, description, visual_queries, sources.
title은 45자 이내, hook_text는 짧고 강렬한 한국어 문구.
narration은 자연스러운 한국어 110~150 공백 기준 어절, 약 35~55초 분량.
첫 2초에 시청자의 일상적 궁금증을 자극하고, 구체적인 사례와 핵심 원리를 설명한 뒤 기억에 남는 결론으로 끝내라.
과장된 공포·부자 혐오·가난 비하·무리한 절약 권유 금지.
visual_queries는 내용과 직접 관련된 영어 Pixabay 영상 검색어 3개.
sources는 근거로 참고할 만한 공식 기관 또는 신뢰할 수 있는 자료 URL 2~3개.
반환은 JSON만."""
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        params={"key": required("GEMINI_API_KEY")},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.55, "responseMimeType": "application/json"}},
        timeout=150,
    )
    response.raise_for_status()
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
        data["narration"] = "\n\n".join(s["narration"] for s in sections)
        data["visual_queries"] = [q for s in sections for q in s["visual_queries"]]
    else:
        if not isinstance(data.get("narration"), str) or len(data["narration"].split()) < 65:
            raise ValueError("Short narration is missing or too short.")
        if not isinstance(data.get("visual_queries"), list) or len(data["visual_queries"]) < 2:
            raise ValueError("At least two relevant video queries are required.")
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
    words = text.split()
    groups, current = [], []
    limit = 7 if IS_LONG else 5
    for word in words:
        current.append(word)
        if len(current) >= limit or re.search(r"[.!?。！？]$", word):
            groups.append(current)
            current = []
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
        caption = " ".join(group).replace("{", "").replace("}", "")
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
        if len(clips) >= (10 if IS_LONG else 3):
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
            if len(clips) >= (10 if IS_LONG else 3):
                break
    minimum = 6 if IS_LONG else 2
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
        client_id=required("YOUTUBE_CLIENT_ID"),
        client_secret=required("YOUTUBE_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    youtube = build("youtube", "v3", credentials=creds)
    sources = "\n".join(f"- {url}" for url in data["sources"])
    visual_credits = "\n".join(dict.fromkeys(credits))
    description = (
        data.get("description", "돈과 경제를 쉽고 재미있게 설명하는 돈좀아는척입니다.") +
        "\n\n참고 자료\n" + sources +
        "\n\n영상 자료 출처\n" + visual_credits +
        "\n\n본 영상은 경제·금융 정보 제공을 위한 일반적인 교육 콘텐츠이며 개인별 투자 권유가 아닙니다."
    )
    body = {
        "snippet": {
            "title": data["title"][:100],
            "description": description[:4900],
            "tags": ["돈좀아는척", "경제상식", "재테크기초", "부자습관", "돈관리", "경제다큐멘터리"],
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
    print(f"Creating 돈좀아는척 {'long-form documentary' if IS_LONG else 'Short'} with {MODEL}")
    data = generate_content()
    print("Selected:", data["title"])
    (WORK / "script.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    voice = asyncio.run(make_voice(data["narration"]))
    clips, credits = download_relevant_clips(data["visual_queries"])
    output = render_video(data, voice, clips)
    (WORK / "visual_credits.txt").write_text("\n".join(dict.fromkeys(credits)), encoding="utf-8")
    print("Rendered:", output, "| Duration:", round(duration_of(output), 1), "seconds")
    if os.getenv("OUTPUT_ONLY", "true").lower() in {"1", "true", "yes"}:
        print("OUTPUT_ONLY enabled; upload skipped.")
    else:
        upload_video(output, data, credits)


if __name__ == "__main__":
    main()
