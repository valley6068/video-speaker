# Video Speaker

A skill for Claude Code (or any AI agent that can run commands) that lets it **look inside a video**: see real frames, read the text on screen, get the transcript, and recover a prompt someone showed on screen but never shared.

It runs on your own computer, **silently in the background**: no windows, no browser, no panels pop up while it takes pictures or transcripts.

## What it can do
- `info`: title, channel, length, subtitles and the full description.
- `transcript`: what is said. It uses the site's own subtitles first, and otherwise listens with Whisper on your computer.
- `text`: the words shown on screen, read on your computer (OCR).
- `sheet`: one picture with many moments of the video in a grid.
- `frames`: single pictures at the moments you pick, with crop and zoom for small text.
- `watch` and `prompt`: a vision model describes the video, or rebuilds a prompt shown on screen. These two need an [Ollama](https://ollama.com) key. Everything else is free.

Pictures and videos only live in a temp folder and are deleted automatically.

## Where it works
**Tested on YouTube and TikTok.**
It uses [yt-dlp](https://github.com/yt-dlp/yt-dlp), so many other sites, like X/Twitter, Instagram, Vimeo, Twitch and Reddit, may work too. They are **not tested yet**. Reports are welcome.

Tested on Windows. macOS and Linux should work, but they are not tested. On those, reading on-screen text needs [Tesseract](https://github.com/tesseract-ocr/tesseract).

## Install
You need Python 3.10 or newer.

1. Copy the `video-speaker` folder into your Claude Code skills folder: `~/.claude/skills/video-speaker` (on Windows: `%USERPROFILE%\.claude\skills\video-speaker`).
2. Install its packages:
   ```bash
   pip install -r ~/.claude/skills/video-speaker/requirements.txt
   ```
3. Optional, for the most reliable YouTube downloads: install [Deno](https://deno.com). yt-dlp uses it for YouTube.

That's it. Ask Claude about any video link and it will use the skill. The first transcript of a video without subtitles downloads the Whisper speech model once (about 460 MB).

You can also run it yourself:
```bash
python ~/.claude/skills/video-speaker/vs.py info "https://www.youtube.com/watch?v=jNQXAC9IVRw"
```

## Safety
- The local server only listens on `127.0.0.1`, so other computers can't reach it.
- It refuses requests from websites in your browser, including hidden ones (checked with `Host`, `Origin` and `Sec-Fetch-Site`), so a web page can't read your history, change settings or make it open files.
- Links found inside a web page are only followed when they go to the public internet, never to `file://`, this computer or the home network (router, NAS).
- Anything that changes something on the server needs a POST, so the tool's own downloads (which only send GET) can't be turned against it.
- Downloads are capped at 2 GB, and oversized "picture bombs" are refused.
- Fake videos (playlists that point at local files) are refused by ffmpeg. Checked with ffmpeg 9, so keep ffmpeg up to date.
- The skill tells the AI that everything inside a video (title, transcript, on-screen text) is data, never instructions.

## Settings
Optional environment variables:
- `VIDEOSPEAKER_PORT`: the local port (default 8765).
- `VIDEOSPEAKER_HOME`: where settings and the cache live (default `%LOCALAPPDATA%\VideoSpeaker`, or `~/.cache/videospeaker`).
- `OLLAMA_API_KEY`: only for `watch` and `prompt`.

If something goes wrong, the server's errors are in `server.log` in that folder.

## License
MIT
