#!/usr/bin/env python3
"""Render the five Google Flow mirror-kitten clips into one vertical Short.

The source ZIP is supplied as a URL by the workflow. This keeps video binaries
out of Git history and leaves the existing main.py pipeline untouched.
"""
import argparse
import asyncio
import os
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

import edge_tts

ORDER_HINTS = [
    ("staring_at_mirror", "The cat just found his biggest enemy."),
    ("facing_mirror", "He stared it down. It stared right back."),
    ("challenging_its_mirror", "So he tried to scare it."),
    ("investigates_mirror", "Then he moved in for a closer look."),
    ("grooming_after_mirror", "And suddenly, he knew the truth. It was him."),
]
VOICE = "en-US-GuyNeural"

def download(url: str, target: Path):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=90) as response, target.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

def locate_clips(root: Path):
    files = list(root.rglob("*.mp4"))
    chosen = []
    for hint, _ in ORDER_HINTS:
        matches = [p for p in files if hint in p.name.lower()]
        if not matches:
            raise RuntimeError(f"ZIP is missing an MP4 matching: {hint}")
        chosen.append(matches[0])
    return chosen

async def make_voice(text: str, output: Path):
    await edge_tts.Communicate(text, VOICE, rate="+3%", volume="+0%").save(str(output))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip-url", required=True)
    ap.add_argument("--output", default="mirror_cat_short.mp4")
    args = ap.parse_args()
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        archive = work / "source.zip"
        download(args.zip_url, archive)
        extracted = work / "clips"
        extracted.mkdir()
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extracted)
        clips = locate_clips(extracted)

        concat_file = work / "concat.txt"
        concat_file.write_text(
            "".join("file '" + str(p).replace("'", "'\\''") + "'\n" for p in clips),
            encoding="utf-8"
        )
        silent = work / "joined.mp4"
        os.system(
            "ffmpeg -y -f concat -safe 0 -i " + shlex_quote(str(concat_file)) +
            " -an -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p " +
            shlex_quote(str(silent)) + " >/dev/null 2>&1"
        )
        if not silent.exists() or silent.stat().st_size == 0:
            raise RuntimeError("FFmpeg could not concatenate the source clips.")

        narration = " ".join(line for _, line in ORDER_HINTS)
        voice = work / "voice.mp3"
        asyncio.run(make_voice(narration, voice))

        # Captions are distributed across the five source scenes.
        ass = work / "captions.ass"
        def ass_time(seconds):
            h = int(seconds // 3600); m = int((seconds % 3600) // 60)
            s = seconds % 60
            return f"{h}:{m:02d}:{s:05.2f}"
        header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H00101010,&H80000000,-1,0,1,3,1,2,48,48,150,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        for i, (_, line) in enumerate(ORDER_HINTS):
            start = i * 7.93
            end = min((i + 1) * 7.93, 39.65)
            events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{line}")
        ass.write_text(header + "\n".join(events) + "\n", encoding="utf-8")

        # Keep original scene audio muted; use clean narration over the montage.
        cmd = (
            "ffmpeg -y -i " + shlex_quote(str(silent)) +
            " -i " + shlex_quote(str(voice)) +
            " -vf " + shlex_quote("subtitles=" + str(ass).replace("\\", "/")) +
            " -map 0:v:0 -map 1:a:0 -c:v libx264 -preset veryfast -crf 21 "
            "-c:a aac -b:a 160k -shortest -movflags +faststart " + shlex_quote(str(out)) +
            " >/dev/null 2>&1"
        )
        os.system(cmd)
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("Final render failed.")
        print(f"Rendered {out} ({out.stat().st_size} bytes)")

def shlex_quote(value):
    import shlex
    return shlex.quote(value)

if __name__ == "__main__":
    main()
