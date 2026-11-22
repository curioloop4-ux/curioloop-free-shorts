# CurioLoop Free Shorts Automation

A low-cost starter pipeline that creates one English YouTube Short per scheduled run using Gemini (script), Pexels (stock footage), Edge TTS (voice), FFmpeg (render), and YouTube Data API (upload).

## Important
- This repository does not become operational until the required API keys and YouTube OAuth refresh token are added as GitHub Actions secrets.
- First runs are **private** by default. Change `YOUTUBE_PRIVACY_STATUS` to `public` only after a successful private test.
- Free-tier quotas and provider terms can change. This is not unlimited free hosting or guaranteed trend discovery.
- Review generated facts and Pexels asset licensing before enabling public uploads.

## GitHub Actions secrets
Add these under **Settings → Secrets and variables → Actions → New repository secret**:
- `GEMINI_API_KEY`
- `PEXELS_API_KEY`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

Optional repository variable:
- `YOUTUBE_PRIVACY_STATUS`: `private` (default), `unlisted`, or `public`
- `GEMINI_MODEL`: defaults to `gemini-2.5-flash`

## Run
GitHub Actions runs daily at 23:00 UTC (08:00 Korea time). You can also run it manually from **Actions → CurioLoop Daily Shorts → Run workflow**.

## Pipeline
1. Gemini generates a short script and a stock-footage search phrase.
2. Pexels supplies a vertical video clip.
3. Edge TTS generates English narration.
4. FFmpeg renders a 9:16 MP4 with captions.
5. YouTube Data API uploads the Short using the configured privacy status.

## Limitations
- This starter uses stock footage, not generative AI video.
- It does not yet perform reliable live trend research or automatic performance-based optimization.
- YouTube API quota, Gemini free limits, Pexels availability, and Edge TTS terms may limit use.
- YouTube decides whether a video is classified as a Short.
