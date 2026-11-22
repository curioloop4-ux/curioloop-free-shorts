# CurioLoop Free Shorts Automation

A low-cost starter pipeline that creates one English YouTube Short per scheduled run using Gemini (script), Edge TTS (voice), FFmpeg (locally generated visual card), and YouTube Data API (upload).

## Important
- This repository does not become operational until the required API keys and YouTube OAuth refresh token are added as GitHub Actions secrets.
- First runs are **private** by default. Change `YOUTUBE_PRIVACY_STATUS` to `public` only after a successful private test.
- Free-tier quotas and provider terms can change. This is not unlimited free hosting or guaranteed trend discovery.
- Review generated facts and Pexels asset licensing before enabling public uploads.

## GitHub Actions secrets
Add these under **Settings → Secrets and variables → Actions → New repository secret**:
- `GEMINI_API_KEY`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

Optional repository variable:
- `YOUTUBE_PRIVACY_STATUS`: `private` (default), `unlisted`, or `public`
- `GEMINI_MODEL`: defaults to `gemini-2.5-flash`

## Run
GitHub Actions runs daily at 23:00 UTC (08:00 Korea time). You can also run it manually from **Actions → CurioLoop Daily Shorts → Run workflow**.

## Pipeline
1. Gemini generates an original short script.
2. Edge TTS generates English narration.
3. FFmpeg creates a vertical abstract background, title, and captions locally.
4. YouTube Data API uploads the Short using the configured privacy status.

## Limitations
- Visuals are locally generated motion-card backgrounds, not stock footage or generative AI video.
- It does not yet perform reliable live trend research or automatic performance-based optimization.
- YouTube API quota, Gemini free limits, Edge TTS terms may limit use.
- YouTube decides whether a video is classified as a Short.
