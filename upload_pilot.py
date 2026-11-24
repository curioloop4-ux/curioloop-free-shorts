from pathlib import Path
import main

video = Path("pilot_artifact/curioloop_short_20261124_142820.mp4")
credits_file = Path("pilot_artifact/visual_credits.txt")
if not video.is_file():
    raise FileNotFoundError(f"Approved pilot MP4 not found: {video}")
credits = [line.strip() for line in credits_file.read_text(encoding="utf-8").splitlines() if line.strip()] if credits_file.exists() else []
title = "Why Is Dating in Korea So Different From Dating in America?"
narration = (
    "Ever noticed how dating can feel like a different game in Korea and the U.S.? "
    "In Korea, some couples put more emphasis on becoming an official couple and marking relationship milestones. "
    "In the U.S., some people are comfortable dating casually before defining things. "
    "Texting expectations can differ too—but there's no single rule in either country. "
    "The real secret? Ask, don't assume."
)
tags = [
    "Korean dating culture", "American dating culture", "cross cultural dating",
    "Korean vs American dating", "dating differences", "relationship communication",
    "dating advice", "international relationships", "dating expectations",
    "relationship milestones", "modern dating", "healthy relationships"
]
main.upload(video, title, narration, credits, tags)
