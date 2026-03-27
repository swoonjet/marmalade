# Marmalade

An autonomous generative collage engine creating art in the wild.

Marmalade continuously crawls digital archives for pre-1995 video and audio, downloads clips to local storage, grades and cuts them with ffmpeg, and auto-plays 20-second vignettes combining found video with independently sourced audio. A local LLM generates search queries and names each piece.

No buttons. No manual triggers. Just a stream.

## How it works

Two background agents hunt for material across multiple archives:

- **Video agent** — nature footage, scientific films, abstract animation, ambient texture from archive.org (Prelinger Archives, stock footage, educational films), NASA, Wikimedia Commons, Library of Congress, Europeana
- **Audio agent** — field recordings, ambient sound, 78rpm records, experimental music, world instruments, jazz, classical, electronic from archive.org music collections, Macaulay Library, Wikimedia, Library of Congress, Europeana

A local LLM (Ollama + mistral-small) generates search queries and describes found clips in two words each. Those four words become a three-word title via compound-word jamming.

Each vignette uses unique clips that are consumed and never reused. When the pool runs dry, archived vignettes replay until fresh material arrives.

## Requirements

- Python 3.8+
- ffmpeg + ffprobe
- Ollama with mistral-small (optional — works without LLM, just uses built-in queries)

## Quick start

```bash
# Install Ollama (optional, for LLM-driven queries)
# https://ollama.ai
ollama pull mistral-small

# Run
python3 server.py

# Open
open http://localhost:8888
```

Click anywhere on first load to unlock audio (browser autoplay policy).

## Flags

```bash
python3 server.py            # normal start, keeps existing pool
python3 server.py --fresh    # wipe pool and start fresh
```

## Content filtering

Edit `blocked.txt` to add terms you want filtered out (one per line). The file is hot-reloaded every 30 seconds — just save and matching content is purged from the pool and blocked from future downloads.

You can also delete clips directly from `pool/video/` or `pool/audio/` — the system cleans up references within 30 seconds.

## File structure

```
server.py        # Python server — crawlers, pool, vignette assembly, HTTP API
index.html       # Frontend — player, archive sidebar, status display
marmalade.gif    # Animated logo
toast.jpg        # Default background image
blocked.txt      # User-editable content blocklist (hot-reloaded)
requirements.txt # Dependencies (stdlib only)

pool/            # Active clip pool (gitignored)
  video/         #   Downloaded and cut video clips
  audio/         #   Downloaded and cut audio clips
raw_cache/       # Full-length originals for re-cutting (gitignored)
archive/         # Played vignettes preserved for replay (gitignored)
toasts/          # Crawled marmalade-on-toast background photos (gitignored)
```

## Architecture

- **5 parallel video crawlers** + **5 parallel audio crawlers** + **3 toast image crawlers**
- Clips are single-use (consumed on play, never repeated)
- Raw downloads cached for re-cutting from different positions
- Archive vignettes copied to persistent storage for replay
- Pool persists across restarts
- Source failure tracking with automatic cooldown
- All content filtered against built-in + user blocklist at download, pool entry, and periodic sweep

## License

All crawled content is sourced from public archives (archive.org, NASA, Library of Congress, Wikimedia Commons, Europeana, Macaulay Library, Openverse, Flickr public feeds). Respect the licensing terms of individual sources.
