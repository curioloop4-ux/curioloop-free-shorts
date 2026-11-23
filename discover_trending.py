import csv
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API_URL = "https://www.googleapis.com/youtube/v3"
OUT = Path("discovery")
OUT.mkdir(exist_ok=True)

# Keep discovery restricted to videos marked Creative Commons by their uploader.
# This label is a lead for rights review, not a guarantee that every element is cleared.
CATEGORIES = [
    ("K-pop", "K-pop 아이돌 무대"),
    ("K-dramas", "한국 드라마 공식 영상"),
    ("Korean movies", "한국 영화 예고편"),
    ("Variety", "한국 예능 공식 영상"),
    ("Actors", "한국 배우 인터뷰"),
    ("Korean entertainment", "Korean entertainment highlights"),
]


def duration_seconds(value):
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return 0
    hours, minutes, seconds = (int(x or 0) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def main():
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing YOUTUBE_API_KEY. Add a YouTube Data API v3 key as a GitHub Actions secret."
        )

    published_after = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    candidates = {}
    for category, query in CATEGORIES:
        params = {
            "key": api_key,
            "part": "snippet",
            "type": "video",
            "q": query,
            "order": "viewCount",
            "publishedAfter": published_after,
            "videoLicense": "creativeCommon",
            "maxResults": 10,
            "regionCode": "KR",
            "relevanceLanguage": "ko",
        }
        response = session.get(f"{API_URL}/search", params=params, timeout=30)
        response.raise_for_status()
        for item in response.json().get("items", []):
            video_id = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {})
            if not video_id:
                continue
            record = candidates.setdefault(video_id, {
                "video_id": video_id,
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "categories": [],
            })
            if category not in record["categories"]:
                record["categories"].append(category)

    ids = list(candidates)
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        response = session.get(
            f"{API_URL}/videos",
            params={
                "key": api_key,
                "part": "statistics,contentDetails,status",
                "id": ",".join(batch),
            },
            timeout=30,
        )
        response.raise_for_status()
        for item in response.json().get("items", []):
            video_id = item["id"]
            record = candidates[video_id]
            stats = item.get("statistics", {})
            record["views"] = int(stats.get("viewCount", 0))
            record["likes"] = int(stats.get("likeCount", 0)) if "likeCount" in stats else None
            record["duration_seconds"] = duration_seconds(
                item.get("contentDetails", {}).get("duration")
            )
            record["license_label"] = item.get("status", {}).get("license", "unknown")
            published = record.get("published_at", "")
            try:
                age_hours = max(
                    (datetime.now(timezone.utc) - datetime.fromisoformat(
                        published.replace("Z", "+00:00")
                    )).total_seconds() / 3600,
                    1,
                )
            except ValueError:
                age_hours = 1
            record["views_per_hour"] = round(record["views"] / age_hours, 2)

    ranked = sorted(
        candidates.values(),
        key=lambda x: (x.get("views_per_hour", 0), x.get("views", 0)),
        reverse=True,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    json_path = OUT / f"trending_candidates_{stamp}.json"
    csv_path = OUT / f"trending_candidates_{stamp}.csv"
    json_path.write_text(
        json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fields = [
        "title", "categories", "channel", "views", "views_per_hour",
        "duration_seconds", "published_at", "license_label", "url", "video_id",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in ranked:
            item = dict(row)
            item["categories"] = ", ".join(item.get("categories", []))
            writer.writerow({field: item.get(field) for field in fields})

    print(f"Found {len(ranked)} unique Creative Commons-labelled candidate(s).")
    for index, item in enumerate(ranked[:15], 1):
        print(
            f"{index:02d}. {item['views']:,} views | "
            f"{item['views_per_hour']:,.1f} views/hour | "
            f"{', '.join(item['categories'])} | {item['title']} | {item['url']}"
        )
    print(f"Saved: {json_path} and {csv_path}")
    print(
        "RIGHTS CHECK: Creative Commons metadata is only a discovery signal. "
        "Before editing or monetizing, verify the uploader's authority, license "
        "terms, attribution requirements, and third-party music/footage rights."
    )


if __name__ == "__main__":
    main()
