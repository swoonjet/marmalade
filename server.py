#!/usr/bin/env python3
"""
Marmalade v3 — Continuous LLM-driven media crawler + auto-playing vignettes.

Two agents crawl everywhere they can for video and audio.
A local LLM generates search queries and describes found material.
Vignettes auto-assemble and auto-play. No button. Just a stream.

Agent B describes video in two words (at least one monosyllabic).
Agent A describes audio in two words (at least one monosyllabic).
Those four words become a three-word title via compound jamming.

Sources: archive.org, xeno-canto, Wikimedia Commons, NASA,
         Library of Congress, Smithsonian, Europeana, Freesound,
         university digital collections, government film archives

Requires: ffmpeg, ffprobe, python3
Optional: Ollama on localhost:11434 with mistral-small

Usage:  python3 server.py
        Open http://localhost:8888
"""

import http.server
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════

PORT = 8888
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral-small")

BASE_DIR = Path(__file__).parent
POOL_DIR = BASE_DIR / "pool"
ARCHIVE_DIR = BASE_DIR / "archive"
TOAST_DIR = BASE_DIR / "toasts"
CACHE_DIR = BASE_DIR / "raw_cache"    # persistent raw downloads for re-cutting
for d in [POOL_DIR, ARCHIVE_DIR, POOL_DIR / "video", POOL_DIR / "audio",
          ARCHIVE_DIR / "vignettes", TOAST_DIR,
          CACHE_DIR, CACHE_DIR / "video", CACHE_DIR / "audio"]:
    d.mkdir(exist_ok=True)

MAX_RAW_CACHE = 80    # max raw files per media type

MAX_VIDEO_POOL = 60
MAX_AUDIO_POOL = 60
CRAWL_INTERVAL = 12           # seconds between hunts per agent
LLM_QUERY_INTERVAL = 240      # seconds between LLM query refreshes
VIGNETTE_DURATION = 20
_BLOCKED_BUILTIN = [
    "periscope", "periscope film", "periscope films", "pf#",
    "periscope film library",
    # War / military
    "war", "warfare", "battle", "combat", "military", "soldier",
    "army", "navy", "marines", "troops", "invasion", "bombing",
    "air raid", "blitz", "infantry", "artillery", "tank battle",
    "missile", "warhead", "torpedo", "grenade", "ammunition",
    "nazi", "third reich", "hitler", "fascist", "fascism",
    "reich", "gestapo", "ss troops", "concentration camp",
    "luftwaffe", "wehrmacht", "axis powers", "allied forces",
    "d-day", "pearl harbor", "hiroshima", "nagasaki",
    "korean war", "vietnam", "cold war",
    "air force", "marine corps", "armed forces", "enlist",
    "draft", "conscription", "veteran", "pow",
    # News / political
    "newsreel", "news reel", "news report", "news bulletin",
    "headline", "broadcast news", "nightly news", "news anchor",
    "breaking news", "press conference", "press briefing",
    "president", "senator", "congressman", "election", "campaign",
    "political", "politics", "democrat", "republican", "congress",
    "parliament", "propaganda", "rally", "protest", "riot",
    "legislation", "governor", "mayor", "white house",
    "capitol", "senate", "bipartisan", "partisan",
    "inauguration", "state of the union", "debate",
    "diplomat", "embassy", "treaty", "sanction",
    "civil rights march", "assassination",
]

# ── Hot-reloadable blocklist ──
# Edit blocked.txt (one term per line) while running — reloads every 30s.
# Also reads rejected.txt which is auto-populated when you delete clips.
BLOCKLIST_FILE = BASE_DIR / "blocked.txt"
REJECTED_FILE = BASE_DIR / "rejected.txt"
_user_blocked = []
_user_blocked_mtime = 0
_rejected_terms = []
_rejected_mtime = 0

def _load_user_blocklist():
    """Hot-reload blocked.txt and rejected.txt if changed."""
    global _user_blocked, _user_blocked_mtime, _rejected_terms, _rejected_mtime
    # blocked.txt
    try:
        mt = BLOCKLIST_FILE.stat().st_mtime if BLOCKLIST_FILE.exists() else 0
        if mt != _user_blocked_mtime:
            _user_blocked_mtime = mt
            if BLOCKLIST_FILE.exists():
                lines = BLOCKLIST_FILE.read_text().strip().splitlines()
                _user_blocked = [l.strip().lower() for l in lines if l.strip() and not l.strip().startswith("#")]
                log("BLOCK", f"Reloaded blocked.txt: {len(_user_blocked)} terms")
            else:
                _user_blocked = []
    except:
        pass
    # rejected.txt
    try:
        mt = REJECTED_FILE.stat().st_mtime if REJECTED_FILE.exists() else 0
        if mt != _rejected_mtime:
            _rejected_mtime = mt
            if REJECTED_FILE.exists():
                lines = REJECTED_FILE.read_text().strip().splitlines()
                _rejected_terms = [l.strip().lower() for l in lines if l.strip() and not l.strip().startswith("#")]
            else:
                _rejected_terms = []
    except:
        pass

def _blocklist_refresh_loop():
    """Background thread: reload blocklist files every 30s."""
    while True:
        _load_user_blocklist()
        time.sleep(30)

# Start the blocklist refresh thread
_load_user_blocklist()
threading.Thread(target=_blocklist_refresh_loop, daemon=True, name="blocklist-refresh").start()

def get_blocked():
    """Return the combined block list (builtin + user file + rejected)."""
    return _BLOCKED_BUILTIN + _user_blocked + _rejected_terms

BLOCKED = _BLOCKED_BUILTIN  # initial value; is_blocked() uses get_blocked() dynamically

# ═══════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════

_log_lock = threading.Lock()
_log_buf = []

def log(tag, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{tag}] {msg}"
    with _log_lock:
        _log_buf.append(line)
        if len(_log_buf) > 150:
            _log_buf.pop(0)
    print(line)

def get_logs(n=50):
    with _log_lock:
        return list(_log_buf[-n:])

# ═══════════════════════════════════════════════════════════════════
# Syllable helpers
# ═══════════════════════════════════════════════════════════════════

def count_syllables(word):
    """Rough syllable count via vowel groups."""
    w = word.lower().strip()
    if not w:
        return 0
    # Remove trailing silent e
    if len(w) > 2 and w.endswith("e") and w[-2] not in "aeiou":
        w = w[:-1]
    count = len(re.findall(r'[aeiouy]+', w))
    return max(1, count)

def is_mono(word):
    return count_syllables(word) == 1

# One-syllable descriptor words (rich, evocative)
MONO_VIDEO = [
    "Bleak", "Bright", "Burned", "Coarse", "Cold", "Cracked", "Dark",
    "Dense", "Dim", "Drenched", "Dry", "Dull", "Faint", "Flat", "Frayed",
    "Glazed", "Grim", "Harsh", "Hushed", "Lost", "Pale", "Raw",
    "Scarred", "Sharp", "Slick", "Slow", "Smeared", "Still", "Stripped",
    "Thick", "Thin", "Torn", "Vast", "Warped", "Worn", "Bruised",
    "Charred", "Clenched", "Crushed", "Drained", "Dulled", "Etched",
    "Flawed", "Grayed", "Gripped", "Hewn", "Lapsed", "Marred",
    "Mute", "Numb", "Pinched", "Pressed", "Scorched", "Sealed",
    "Shrunk", "Spent", "Stained", "Starved", "Stretched", "Stunned",
    "Traced", "Veiled", "Bleached", "Bruised", "Choked", "Curled",
    "Locked", "Scrubbed", "Stiff", "Stung", "Wrecked",
]

MULTI_VIDEO = [
    "Coral", "Amber", "Silver", "Crystal", "Fossil", "Ember",
    "Crater", "Granite", "Obsidian", "Tidal", "Sunken", "Frozen",
    "Vanished", "Eroded", "Molten", "Fractured", "Pelagic", "Feral",
    "Buried", "Scattered", "Trembling", "Sulfur", "Cinder", "Basalt",
    "Phantom", "Oxide", "Residue", "Voltage", "Marrow", "Kelp",
    "Silo", "Pollen", "Plateau", "Basin", "Delta", "Tundra",
]

MONO_AUDIO = [
    "Buzz", "Chirp", "Click", "Crack", "Crash", "Drone", "Drip",
    "Gust", "Hiss", "Howl", "Hum", "Growl", "Groan", "Lull",
    "Ping", "Pulse", "Ring", "Roar", "Rush", "Screech", "Snap",
    "Snarl", "Spark", "Splash", "Squawk", "Static", "Surge",
    "Swarm", "Throb", "Thump", "Tone", "Wail", "Whine", "Whir",
    "Gasp", "Clang", "Creak", "Drift", "Flare", "Gleam", "Glint",
    "Glow", "Plunge", "Scrape", "Shriek", "Sigh", "Slam", "Sting",
    "Strain", "Sweep", "Thrum", "Whirl", "Blast", "Blip", "Chime",
    "Clap", "Fizz", "Haze", "Jolt", "Lurch", "Moan", "Pluck",
    "Rattle", "Shudder", "Squelch", "Tremor", "Twang", "Warp",
]

MULTI_AUDIO = [
    "Echo", "Murmur", "Silence", "Vibration", "Resonance", "Frequency",
    "Decay", "Signal", "Nocturne", "Cipher", "Meridian", "Passage",
    "Fugue", "Remnant", "Current", "Archive", "Memory", "Lament",
    "Harbour", "Estuary", "Chorus", "Voltage", "Ritual", "Relic",
    "Sequence", "Interval", "Undercurrent", "Threshold", "Sonar",
    "Antenna", "Filament", "Oscillation", "Transmission", "Artifact",
]


def pick_two_words(mono_list, multi_list):
    """Pick two words, at least one monosyllabic."""
    mono = random.choice(mono_list)
    other = random.choice(multi_list + mono_list)
    if random.random() < 0.5:
        return [mono, other]
    return [other, mono]


def build_title(video_words, audio_words):
    """
    4 words (at least 2 mono) → jam 2 together (one must be mono) →
    3-word title: compound + 2 leftovers.
    """
    all_four = list(video_words) + list(audio_words)  # [vw1, vw2, aw1, aw2]

    # Find indices of mono words
    monos = [i for i, w in enumerate(all_four) if is_mono(w)]
    multis = [i for i in range(4) if i not in monos]

    # Pick 2 to jam: at least one must be mono
    if monos:
        # Pick one mono
        m_idx = random.choice(monos)
        # Pick another word (any)
        others = [i for i in range(4) if i != m_idx]
        o_idx = random.choice(others)
        jam_indices = sorted([m_idx, o_idx])
    else:
        # Shouldn't happen with our lists, but fallback
        jam_indices = [0, 1]

    leftover_indices = [i for i in range(4) if i not in jam_indices]

    # Build compound
    w1, w2 = all_four[jam_indices[0]], all_four[jam_indices[1]]
    compound = w1 + w2

    # Remaining two words
    remaining = [all_four[i] for i in leftover_indices]

    # Verify at least one of the 3 final words is mono
    final_three = [compound] + remaining
    has_mono = any(is_mono(w) for w in remaining)

    if not has_mono and monos:
        # Swap to ensure a mono stays in remaining
        pass  # Our selection already biases toward this

    # Randomize placement of compound word — not always first
    final_three = [compound] + remaining
    random.shuffle(final_three)

    return f"{final_three[0]} {final_three[1]} {final_three[2]}"


# ═══════════════════════════════════════════════════════════════════
# Ollama LLM
# ═══════════════════════════════════════════════════════════════════

class OllamaClient:
    def __init__(self):
        self.host = OLLAMA_HOST.rstrip("/")
        self.model = OLLAMA_MODEL
        self.available = False

    def check(self):
        try:
            req = urllib.request.Request(f"{self.host}/api/tags",
                                         headers={"User-Agent": "Marmalade/3.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            self.available = any(self.model in m for m in models)
            log("LLM", f"{'Connected' if self.available else 'Model not found'}: {models}")
            return self.available
        except Exception as e:
            self.available = False
            log("LLM", f"Not reachable: {e}")
            return False

    def generate(self, prompt, max_tokens=300):
        if not self.available:
            return None
        try:
            payload = json.dumps({
                "model": self.model, "prompt": prompt, "stream": False,
                "options": {"temperature": 1.1, "num_predict": max_tokens},
            }).encode()
            req = urllib.request.Request(
                f"{self.host}/api/generate", data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "Marmalade/3.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return data.get("response", "").strip()
        except Exception as e:
            log("LLM", f"Error: {e}")
            return None

    def describe_clip(self, media_type, title, source):
        """Ask LLM to describe a clip in exactly 2 words, one monosyllabic."""
        prompt = (
            f"You found a {media_type} clip: \"{title}\" from {source}. "
            f"Describe it in EXACTLY two evocative words. "
            f"At least one word MUST be one syllable (like: dark, cold, raw, hum, drone, crack, bright, still, torn, mute). "
            f"Return ONLY the two words, nothing else. No punctuation."
        )
        result = self.generate(prompt, max_tokens=15)
        if result:
            words = result.strip().split()
            words = [re.sub(r'[^a-zA-Z]', '', w).capitalize() for w in words if w.strip()]
            if len(words) >= 2:
                pair = words[:2]
                if any(is_mono(w) for w in pair):
                    return pair
        return None

    def generate_queries(self, media_type="video"):
        if media_type == "video":
            prompt = (
                "You are searching digital archives for rare, visually striking PRE-1995 footage. "
                "Only material from before 1995 — pre-internet era. Think: 16mm educational films, "
                "1960s surgical films, Cold War atomic tests, Kodachrome nature documentaries, "
                "electron microscopy from the 1970s, Soviet science films, early wildlife cinema, "
                "newsreel footage, magic lantern slides, early animation, Pathé newsreels, "
                "glacier expeditions from the 1950s, deep sea diving films from the 1960s, "
                "insect metamorphosis filmed in the 1940s, coral reef 16mm, Victorian zoetropes, "
                "8mm home movies of nature, 1930s factory process films, reel-to-reel. "
                "Avoid Periscope Films. Be specific and unusual. "
                "Return ONLY queries, one per line."
            )
        else:
            prompt = (
                "You are searching digital archives for rare, evocative PRE-1995 audio. "
                "Only material from before 1995 — pre-internet era. Think: hydrophone recordings, "
                "shortwave number stations from the Cold War, wax cylinder ethnographic music, "
                "1950s field recordings of dawn chorus, volcanic tremor on reel-to-reel tape, "
                "submarine sonar pings, Geiger counter near reactor, 78 rpm nature records, "
                "decommissioned lighthouse fog horn, 1960s Tibetan singing bowls recordings, "
                "early tape recordings of cicada swarms, mine shaft ambience, gramophone recordings, "
                "1940s radio broadcasts of nature, early BBC sound effects library. "
                "Be specific and unusual. Return ONLY queries, one per line."
            )
        result = self.generate(prompt)
        if result:
            lines = [l.strip().strip("-").strip("•").strip("*").strip('"').strip()
                     for l in result.split("\n") if l.strip() and len(l.strip()) > 5]
            return lines[:8] if lines else None
        return None


# ═══════════════════════════════════════════════════════════════════
# Utility
# ═══════════════════════════════════════════════════════════════════

def is_blocked(text):
    lower = (text or "").lower()
    return any(b in lower for b in get_blocked())

def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Marmalade/3.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def download_partial(url, output_path, max_bytes=30_000_000, timeout=90):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Marmalade/3.0",
            "Range": f"bytes=0-{max_bytes}",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(output_path, "wb") as f:
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
        return output_path.exists() and output_path.stat().st_size > 10000
    except Exception as e:
        log("DL", f"Error: {e}")
        return False

def probe_duration(filepath):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(filepath)],
            capture_output=True, text=True, timeout=10)
        d = r.stdout.strip()
        if d and d != "N/A":
            return float(d)
    except:
        pass
    return None

def detect_content_start(filepath, total_dur):
    """Skip black frames / title cards at the beginning."""
    if not total_dur or total_dur < 10:
        return 0
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(filepath),
             "-t", str(min(120, total_dur * 0.3)),
             "-vf", "blackdetect=d=0.5:pix_th=0.1",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=20)
        ends = re.findall(r"black_end:(\d+\.?\d*)", r.stderr)
        if ends:
            last = max(float(e) for e in ends)
            if 2 < last < total_dur * 0.5:
                return last + 0.5
    except:
        pass
    return max(3, total_dur * 0.15)


# ═══════════════════════════════════════════════════════════════════
# Video grading
# ═══════════════════════════════════════════════════════════════════

VIDEO_GRADES = [
    "colorbalance=rs=0.5:gs=-0.3:bs=0.4:rh=0.4:gh=-0.2:bh=0.5,noise=alls=35:allf=t,eq=contrast=1.5:brightness=-0.08:saturation=0.6",
    "colorbalance=rs=-0.4:gs=-0.1:bs=0.6:rh=-0.3:bh=0.5,noise=alls=20:allf=t,eq=contrast=1.3:brightness=-0.1:saturation=0.7",
    "colorbalance=rs=0.3:gs=0.1:bs=-0.1:rh=0.2:gh=0.2:bh=0.1,noise=alls=25:allf=t,eq=contrast=1.4:saturation=1.2",
    "hue=s=0,noise=alls=40:allf=t,eq=contrast=1.5:brightness=-0.15:gamma=0.7",
    "colorbalance=rs=0.6:gs=-0.4:bs=0.6:rh=0.5:gh=-0.3:bh=0.5,noise=alls=30:allf=t,eq=contrast=1.5:saturation=0.8",
    "negate,colorbalance=rs=0.3:gs=0.5:bs=-0.2,noise=alls=30:allf=t,eq=contrast=1.2:saturation=1.5",
    "noise=alls=45:allf=t,eq=contrast=1.7:brightness=-0.12:saturation=0,colorbalance=rs=0.1:bs=0.2",
    "colorbalance=rs=-0.2:gs=0.4:bs=0.5:rh=-0.1:gh=0.3:bh=0.4,noise=alls=20:allf=t,eq=contrast=1.3:brightness=-0.1:saturation=1.3",
    "colorbalance=rs=0.4:gs=-0.2:bs=0.3,noise=alls=30:allf=t,eq=contrast=1.4:saturation=0.7",
    "colorbalance=rs=0.2:gs=0.1:bs=-0.1,noise=alls=15:allf=t,eq=contrast=1.2:saturation=1.1",
    "colorbalance=rs=0.1:gs=-0.4:bs=0.6:rh=0.0:gh=-0.3:bh=0.5,noise=alls=25:allf=t,eq=contrast=1.6:saturation=0.4",
    "curves=vintage,noise=alls=30:allf=t,eq=contrast=1.3:brightness=-0.05:saturation=0.9",
    "colorbalance=rs=-0.3:gs=0.2:bs=0.1,noise=alls=20:allf=t,eq=contrast=1.1:saturation=1.4:gamma=1.1",
    "colorbalance=rs=0.7:gs=-0.1:bs=-0.3:rh=0.5:gh=0.0:bh=-0.2,noise=alls=35:allf=t,eq=contrast=1.5:saturation=0.5",
]


# ═══════════════════════════════════════════════════════════════════
# ffmpeg cutting
# ═══════════════════════════════════════════════════════════════════

def check_video_has_content(filepath, sample_dur=2):
    """Reject truly blank/static video (solid color frames with no motion).
    Film leaders, countdowns, and flicker are fine — they have visual change."""
    try:
        # Use scene detection: if there are zero scene changes in 2 seconds
        # the video is likely a static frame / blank
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(filepath),
             "-t", str(sample_dur),
             "-vf", "select='gt(scene,0.01)',showinfo",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=10)
        scene_hits = r.stderr.count("showinfo")
        # Also check file size — extremely tiny = blank
        sz = filepath.stat().st_size if filepath.exists() else 0
        if sz < 8000:
            return False
        # If absolutely zero scene changes and tiny file, reject
        if scene_hits == 0 and sz < 30000:
            return False
        return True
    except:
        return True  # on error, keep the clip


def cut_video_clip(input_path, output_path, duration=3):
    total = probe_duration(input_path)
    if total and total > 10:
        start = detect_content_start(input_path, total)
        mx = max(start + 1, total - duration - 2)
        start = random.uniform(start, mx)
    else:
        start = 0
    grade = random.choice(VIDEO_GRADES)
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-ss", str(start),
        "-i", str(input_path), "-t", str(duration),
        "-vf", (f"scale=640:360:force_original_aspect_ratio=decrease,"
                f"pad=640:360:(ow-iw)/2:(oh-ih)/2,fps=24,{grade}"),
        "-c:v", "libx264", "-preset", "fast", "-crf", "24",
        "-pix_fmt", "yuv420p", "-an", str(output_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not output_path.exists():
            return False
        # Validate the output is actually playable with real duration
        out_dur = probe_duration(output_path)
        if not out_dur or out_dur < 0.5:
            log("CUT", f"Video clip too short ({out_dur}s), rejecting")
            try:
                output_path.unlink()
            except:
                pass
            return False
        # Check for blank/static content (but allow film leaders etc)
        if not check_video_has_content(output_path):
            log("CUT", f"Video clip appears blank/static, rejecting")
            try:
                output_path.unlink()
            except:
                pass
            return False
        return output_path.stat().st_size > 10000
    except:
        return False

def find_loudest_section(input_path, clip_duration=8):
    """Use ffmpeg silencedetect to find a section with consistent audio signal.
    Falls back to volumedetect max_volume timestamp estimation."""
    total = probe_duration(input_path)
    if not total or total < 3:
        return 0

    # Strategy: scan for non-silent sections using silencedetect
    # Find silence gaps, then pick a start point that avoids them
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(input_path),
             "-af", "silencedetect=noise=-35dB:d=0.5",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=20)
        stderr = r.stderr

        # Parse silence_start and silence_end
        starts = [float(m) for m in re.findall(r"silence_start:\s*(\d+\.?\d*)", stderr)]
        ends = [float(m) for m in re.findall(r"silence_end:\s*(\d+\.?\d*)", stderr)]

        # Build list of "loud" windows
        loud_ranges = []
        prev_end = 0
        for i, ss in enumerate(starts):
            if ss - prev_end >= clip_duration:
                loud_ranges.append((prev_end, ss))
            if i < len(ends):
                prev_end = ends[i]
        # Check final segment after last silence
        if total - prev_end >= clip_duration:
            loud_ranges.append((prev_end, total))

        if loud_ranges:
            # Pick the longest loud range
            loud_ranges.sort(key=lambda r: r[1]-r[0], reverse=True)
            best = loud_ranges[0]
            # Start randomly within that range
            max_start = best[1] - clip_duration
            min_start = best[0]
            if max_start > min_start:
                return random.uniform(min_start, max_start)
            return min_start

    except:
        pass

    # Fallback: skip first 15%, pick from middle
    mn = max(1, total * 0.15)
    mx = max(mn + 1, total - clip_duration - 1)
    return random.uniform(mn, mx) if mx > mn else mn


def cut_audio_clip(input_path, output_path, duration=15):
    """Cut audio from the loudest section, normalize volume. Default 15s for timeline fill."""
    start = find_loudest_section(input_path, duration)
    fade_out = max(0, duration - 1)

    # Use loudnorm for consistent output level + fades
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-ss", str(start),
        "-i", str(input_path), "-t", str(duration),
        "-af", (f"loudnorm=I=-16:LRA=11:TP=-1.5,"
                f"afade=t=in:d=0.3,afade=t=out:st={fade_out}:d=1"),
        "-c:a", "libmp3lame", "-q:a", "2", str(output_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and output_path.exists() and output_path.stat().st_size > 5000:
            return True
        # Fallback without loudnorm (some files may not support 2-pass)
        cmd_simple = [
            "ffmpeg", "-v", "error", "-y", "-ss", str(start),
            "-i", str(input_path), "-t", str(duration),
            "-af", (f"volume=2.0,"
                    f"afade=t=in:d=0.3,afade=t=out:st={fade_out}:d=1"),
            "-c:a", "libmp3lame", "-q:a", "2", str(output_path)]
        r2 = subprocess.run(cmd_simple, capture_output=True, text=True, timeout=30)
        return r2.returncode == 0 and output_path.exists() and output_path.stat().st_size > 5000
    except:
        return False

def make_clip_id(media_type):
    return f"{media_type[0]}_{int(time.time())}_{random.randint(1000,9999)}"


def cache_raw(raw_path, media_type):
    """Move a raw download into persistent cache for re-cutting later."""
    dest = CACHE_DIR / media_type / raw_path.name
    try:
        import shutil
        shutil.move(str(raw_path), str(dest))
        # Cap the cache
        cached = sorted((CACHE_DIR / media_type).glob("*"), key=lambda f: f.stat().st_mtime)
        while len(cached) > MAX_RAW_CACHE:
            old = cached.pop(0)
            try:
                old.unlink()
            except:
                pass
        return dest
    except:
        return None


def recut_from_cache(media_type):
    """Pick a random cached raw file and cut a new clip from a fresh position."""
    cache_sub = CACHE_DIR / media_type
    raws = list(cache_sub.glob("*"))
    if not raws:
        return None, None
    random.shuffle(raws)
    for raw in raws[:3]:
        dur = probe_duration(raw)
        if not dur or dur < 5:
            continue
        cid = make_clip_id(media_type)
        ext = ".mp4" if media_type == "video" else ".mp3"
        clip = POOL_DIR / media_type / f"{cid}{ext}"
        if media_type == "video":
            ok = cut_video_clip(raw, clip, duration=random.choice([2, 3, 3, 4]))
        else:
            ok = cut_audio_clip(raw, clip, duration=random.choice([12, 14, 15, 18]))
        if ok:
            title = raw.stem.replace("raw_", "")[:40]
            log("RECUT", f"[{media_type}] New cut from cached: {title}")
            return clip, {"source": "recut", "title": f"recut:{title}",
                          "file": f"pool/{media_type}/{cid}{ext}"}
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Source: archive.org  (EXPANDED)
# ═══════════════════════════════════════════════════════════════════

# PRE-1995 ONLY — pre-internet era footage
_PRE95 = 'date:[1900-01-01 TO 1994-12-31]'

ARCHIVE_VIDEO_Q = [
    # Nature / wildlife — ambient, non-narrative
    f'collection:prelinger AND (wildlife OR animals OR nature OR birds OR fish OR ocean) AND {_PRE95}',
    f'collection:prelinger AND (underwater OR sea OR marine OR coral OR diving) AND {_PRE95}',
    f'collection:prelinger AND (forest OR jungle OR arctic OR desert OR mountain OR cave) AND {_PRE95}',
    f'collection:prelinger AND (insect OR butterfly OR spider OR ant OR beetle) AND {_PRE95}',
    f'collection:prelinger AND (volcano OR earthquake OR geology OR glacier OR erosion) AND {_PRE95}',
    f'collection:prelinger AND (astronomy OR telescope OR stars OR moon OR planets) AND {_PRE95}',
    f'collection:prelinger AND (microscope OR microorganism OR cell OR bacteria) AND {_PRE95}',
    f'collection:prelinger AND (weather OR tornado OR hurricane OR lightning) AND {_PRE95}',
    f'collection:prelinger AND (dam OR bridge OR tunnel OR construction) AND {_PRE95}',
    f'collection:prelinger AND (train OR railroad OR locomotive OR railway) AND {_PRE95}',
    f'collection:prelinger AND (flight OR aircraft OR aviation OR balloon) AND {_PRE95}',
    # Marine life
    f'mediatype:movies AND (whale OR dolphin OR shark OR octopus OR squid) AND {_PRE95}',
    f'mediatype:movies AND (coral reef OR anemone OR sea urchin OR starfish) AND {_PRE95}',
    f'mediatype:movies AND (deep sea OR abyssal OR hydrothermal vent) AND {_PRE95}',
    f'mediatype:movies AND (jellyfish OR medusa OR cnidaria OR plankton) AND {_PRE95}',
    f'mediatype:movies AND (cephalopod OR nautilus OR cuttlefish) AND {_PRE95}',
    # Wildlife
    f'mediatype:movies AND (eagle OR hawk OR owl OR falcon OR pelican) AND {_PRE95}',
    f'mediatype:movies AND (lion OR elephant OR gorilla OR tiger OR bear) AND {_PRE95}',
    f'mediatype:movies AND (snake OR reptile OR lizard OR crocodile OR turtle) AND {_PRE95}',
    f'mediatype:movies AND (chrysalis OR metamorphosis OR larvae OR pupae OR cocoon) AND {_PRE95}',
    f'mediatype:movies AND (bat OR cave OR spelunking OR stalactite OR cavern) AND {_PRE95}',
    f'mediatype:movies AND (fungus OR mushroom OR mycelium OR spore) AND {_PRE95}',
    # Science / process
    f'mediatype:movies AND (glacier calving OR iceberg OR polar OR permafrost) AND {_PRE95}',
    f'mediatype:movies AND (electron microscope OR scanning electron OR magnification) AND {_PRE95}',
    f'mediatype:movies AND (time lapse OR timelapse) AND (plant OR flower OR growth OR decay) AND {_PRE95}',
    f'mediatype:movies AND (erosion OR sedimentation OR geological) AND {_PRE95}',
    # Ambient / texture / non-narrative
    f'mediatype:movies AND (clouds OR sky OR sunset OR sunrise OR horizon) AND {_PRE95}',
    f'mediatype:movies AND (waves OR surf OR tide OR shoreline OR beach) AND {_PRE95}',
    f'mediatype:movies AND (garden OR greenhouse OR botanical OR terrarium) AND {_PRE95}',
    f'mediatype:movies AND (aquarium OR fish tank OR tropical fish) AND {_PRE95}',
    f'mediatype:movies AND (lava OR magma OR volcanic flow OR molten) AND {_PRE95}',
    f'mediatype:movies AND (snowfall OR blizzard OR frost OR ice crystal) AND {_PRE95}',
    f'mediatype:movies AND (candle OR flame OR fire OR embers OR campfire) AND {_PRE95}',
    f'mediatype:movies AND (fountain OR waterfall OR rapids OR river flow) AND {_PRE95}',
    f'mediatype:movies AND (fog OR mist OR haze OR dew OR morning) AND {_PRE95}',
    f'mediatype:movies AND (kaleidoscope OR abstract OR pattern OR experimental film) AND {_PRE95}',
    f'mediatype:movies AND (city lights OR neon OR night city OR rain window) AND {_PRE95}',
    f'mediatype:movies AND (pottery OR glassblowing OR weaving OR craftsman) AND {_PRE95}',
    f'mediatype:movies AND (pendulum OR clockwork OR gears OR mechanism) AND {_PRE95}',
    f'mediatype:movies AND (aurora OR northern lights OR borealis) AND {_PRE95}',
    # Collections
    f'collection:stock_footage AND (nature OR wildlife OR ocean OR forest) AND {_PRE95}',
    f'collection:classic_tv AND (nature OR wildlife OR documentary) AND {_PRE95}',
    f'collection:educationalfilms AND (science OR biology OR ecology) AND {_PRE95}',
    f'collection:ComputerChronicles AND (graphics OR visualization) AND {_PRE95}',
    f'mediatype:movies AND (16mm OR 8mm OR film reel) AND (nature OR science) AND {_PRE95}',
    f'mediatype:movies AND (Kodachrome OR Technicolor) AND (wildlife OR nature) AND {_PRE95}',
    f'mediatype:movies AND (educational film) AND (biology OR chemistry OR physics) AND {_PRE95}',
    # Abstract / experimental
    f'mediatype:movies AND (abstract animation OR visual music OR color organ) AND {_PRE95}',
    f'mediatype:movies AND (oscilloscope OR waveform OR signal OR cathode ray) AND {_PRE95}',
    f'collection:prelinger AND (scenery OR panorama OR travelogue OR landscape) AND {_PRE95}',
]

ARCHIVE_AUDIO_Q = [
    # ── Nature / field recordings ──
    f'mediatype:audio AND (field recording OR ambient sound OR nature sounds) AND {_PRE95}',
    f'mediatype:audio AND (whale song OR humpback OR cetacean) AND {_PRE95}',
    f'mediatype:audio AND (underwater OR hydrophone OR ocean recording) AND {_PRE95}',
    f'mediatype:audio AND (wind OR rain OR thunder OR storm) AND {_PRE95}',
    f'mediatype:audio AND (forest OR jungle OR rainforest) AND {_PRE95}',
    f'mediatype:audio AND (insect OR cicada OR cricket) AND {_PRE95}',
    f'mediatype:audio AND (cave OR underground OR dripping) AND {_PRE95}',
    f'mediatype:audio AND (ocean waves OR surf OR tide pool) AND {_PRE95}',
    f'mediatype:audio AND (river OR waterfall OR stream OR rapids) AND {_PRE95}',
    f'mediatype:audio AND (desert OR sand OR dune) AND {_PRE95}',
    f'mediatype:audio AND (VLF OR very low frequency OR natural radio) AND {_PRE95}',
    f'mediatype:audio AND (beehive OR bees OR apiary) AND {_PRE95}',
    # ── Music / instrumental ──
    f'mediatype:audio AND (ambient music OR drone OR minimalist) AND {_PRE95}',
    f'mediatype:audio AND (tape loop OR musique concrete OR electroacoustic) AND {_PRE95}',
    f'mediatype:audio AND (synthesizer OR moog OR analog synth) AND {_PRE95}',
    f'mediatype:audio AND (electronic music OR experimental music) AND {_PRE95}',
    f'mediatype:audio AND (cello OR violin OR viola OR chamber music) AND {_PRE95}',
    f'mediatype:audio AND (piano solo OR solo piano OR Satie OR Debussy) AND {_PRE95}',
    f'mediatype:audio AND (organ music OR pipe organ OR church organ) AND {_PRE95}',
    f'mediatype:audio AND (flute OR shakuhachi OR pan flute OR recorder) AND {_PRE95}',
    f'mediatype:audio AND (harp OR zither OR dulcimer OR autoharp) AND {_PRE95}',
    f'mediatype:audio AND (guitar OR acoustic guitar OR classical guitar) AND {_PRE95}',
    f'mediatype:audio AND (choral OR choir OR gregorian OR plainchant) AND {_PRE95}',
    f'mediatype:audio AND (jazz OR cool jazz OR modal jazz) AND {_PRE95}',
    f'mediatype:audio AND (bossa nova OR samba OR latin jazz) AND {_PRE95}',
    f'mediatype:audio AND (raga OR sitar OR tabla OR indian classical) AND {_PRE95}',
    f'mediatype:audio AND (koto OR shamisen OR gagaku OR japanese traditional) AND {_PRE95}',
    f'mediatype:audio AND (gamelan OR metallophone OR ethnic percussion) AND {_PRE95}',
    f'mediatype:audio AND (mbira OR kalimba OR thumb piano) AND {_PRE95}',
    f'mediatype:audio AND (didgeridoo OR aboriginal OR indigenous music) AND {_PRE95}',
    f'mediatype:audio AND (throat singing OR overtone OR harmonic singing) AND {_PRE95}',
    f'mediatype:audio AND (Tibetan OR singing bowl OR gong OR bell) AND {_PRE95}',
    # ── Texture / atmosphere / mechanical ──
    f'mediatype:audio AND (shortwave OR numbers station OR radio recording) AND {_PRE95}',
    f'mediatype:audio AND (morse code OR telegraph OR wireless) AND {_PRE95}',
    f'mediatype:audio AND (fog horn OR lighthouse OR maritime OR buoy) AND {_PRE95}',
    f'mediatype:audio AND (train OR locomotive OR steam OR railroad) AND {_PRE95}',
    f'mediatype:audio AND (factory OR machinery OR industrial noise) AND {_PRE95}',
    f'mediatype:audio AND (space OR pulsar OR radio telescope OR cosmos) AND {_PRE95}',
    f'mediatype:audio AND (church bell OR cathedral bells OR carillon) AND {_PRE95}',
    f'mediatype:audio AND (clock OR ticking OR pendulum OR chime) AND {_PRE95}',
    f'mediatype:audio AND (typewriter OR mechanical OR printing press) AND {_PRE95}',
    # ── Archive.org music collections ──
    f'collection:78rpm AND (orchestra OR instrumental OR classical)',
    f'collection:78rpm AND (hawaiian OR polynesian OR exotica)',
    f'collection:78rpm AND (jazz OR swing OR big band)',
    f'collection:78rpm AND (folk OR traditional OR acoustic)',
    f'collection:78rpm AND (blues OR delta OR slide guitar)',
    f'collection:78rpm AND (waltz OR tango OR bolero)',
    f'mediatype:audio AND (wax cylinder OR phonograph OR gramophone) AND {_PRE95}',
    f'mediatype:audio AND (test tone OR oscillator OR sine wave OR signal) AND {_PRE95}',
    f'mediatype:audio AND (lullaby OR music box OR carousel OR fairground) AND {_PRE95}',
    f'mediatype:audio AND (soundtrack OR film score OR incidental music) AND {_PRE95}',
    f'mediatype:audio AND (reel to reel OR tape recording) AND (music OR performance) AND {_PRE95}',
]


def search_archive(query, max_results=40):
    params = {
        "q": query, "output": "json", "rows": str(max_results),
        "page": "1", "fl[]": "identifier,title,description,mediatype",
    }
    url = f"https://archive.org/advancedsearch.php?{urllib.parse.urlencode(params, doseq=True)}"
    try:
        data = http_get_json(url)
        docs = data.get("response", {}).get("docs", [])
        return [d for d in docs
                if not is_blocked(d.get("title", ""))
                and not is_blocked(d.get("description", ""))
                and not is_blocked(d.get("identifier", ""))]
    except Exception as e:
        log("IA", f"Search error: {e}")
        return []


def find_ia_file(identifier, media_type):
    try:
        data = http_get_json(f"https://archive.org/metadata/{identifier}/files")
        files = data.get("result", [])
    except:
        return None
    exts = ([".mp4", ".ogv", ".mpeg", ".avi", ".mkv", ".webm"]
            if media_type == "video"
            else [".mp3", ".ogg", ".flac", ".wav", ".aac"])
    min_sz = 5_000_000 if media_type == "video" else 100_000
    max_sz = 500_000_000 if media_type == "video" else 100_000_000
    for ext in exts:
        cands = [f for f in files
                 if f.get("name", "").lower().endswith(ext)
                 and not is_blocked(f.get("name", ""))
                 and min_sz < int(f.get("size", 0) or 0) < max_sz]
        if cands:
            cands.sort(key=lambda f: int(f.get("size", 0) or 0))
            c = cands[0]
            return f"https://archive.org/download/{identifier}/{urllib.parse.quote(c['name'])}"
    return None


def hunt_archive(media_type, queries=None):
    qs = queries or (ARCHIVE_VIDEO_Q if media_type == "video" else ARCHIVE_AUDIO_Q)
    q = random.choice(qs)
    log("IA", f"[{media_type}] {q[:60]}...")
    results = search_archive(q)
    if not results:
        return None, None
    random.shuffle(results)
    for item in results[:6]:
        ident = item.get("identifier", "")
        title = item.get("title", "Unknown")
        url = find_ia_file(ident, media_type)
        if not url:
            continue
        ext = ".mp4" if media_type == "video" else ".mp3"
        raw = POOL_DIR / media_type / f"raw_{ident[:40]}{ext}"
        mb = 25_000_000 if media_type == "video" else 10_000_000
        log("IA", f"[{media_type}] Downloading: {title[:50]}")
        if not download_partial(url, raw, max_bytes=mb):
            continue
        cid = make_clip_id(media_type)
        clip = POOL_DIR / media_type / f"{cid}{ext}"
        if media_type == "video":
            ok = cut_video_clip(raw, clip, duration=random.choice([2, 3, 3, 4]))
        else:
            ok = cut_audio_clip(raw, clip, duration=random.choice([12, 14, 15, 18]))
        cache_raw(raw, media_type)
        if ok:
            log("IA", f"[{media_type}] Got: {cid} — {title[:40]}")
            return clip, {"source": "archive.org", "title": title,
                          "file": f"pool/{media_type}/{cid}{ext}"}
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Source: Macaulay Library (Cornell Lab of Ornithology)
# Free bird/nature audio — no API key needed
# ═══════════════════════════════════════════════════════════════════

MACAULAY_Q = [
    "norcar", "baleag", "amecro", "houspa", "eursta",
    "rethaw", "comloo", "gryowl", "brnowl", "snoowl",
    "comrav", "amegol", "bkcchi", "pilwoo", "westan",
    "grhowl", "barswa", "whcspa", "whbnut", "rewbla",
    "easmea", "comyel", "normoc", "grnher", "snober",
    "buffle", "wiltur", "sposan", "indpea", "comswi",
    "rufhum", "annhum", "belkin", "orcwar", "blujay",
    "carwre", "easwpw", "yerwar", "cangoo", "comgol",
    "osprey", "pefal", "merlin", "ameker", "barowl",
]

def hunt_macaulay():
    """Macaulay Library — Cornell's massive bird/nature audio archive."""
    q = random.choice(MACAULAY_Q)
    log("ML", f"Searching: {q}")
    try:
        url = (f"https://search.macaulaylibrary.org/api/v1/search?"
               f"taxonCode={urllib.parse.quote(q)}&count=20&mediaType=audio"
               f"&sort=rating_rank_desc")
        data = http_get_json(url, timeout=15)
        results = data.get("results", {}).get("content", data.get("results", data))
        if not isinstance(results, list):
            results = []
    except Exception as e:
        log("ML", f"Error: {e}")
        return None, None
    if not results:
        return None, None
    random.shuffle(results)
    for rec in results[:5]:
        murl = rec.get("mediaUrl", "")
        if not murl:
            continue
        species = rec.get("commonName", "Unknown bird")
        location = rec.get("location", "")
        title = f"{species} ({location[:30]})" if location else species
        log("ML", f"Downloading: {title[:50]}")
        raw = POOL_DIR / "audio" / f"raw_ml_{rec.get('catalogId','0')}.mp3"
        if not download_partial(murl, raw, max_bytes=8_000_000):
            continue
        cid = make_clip_id("audio")
        clip = POOL_DIR / "audio" / f"{cid}.mp3"
        if cut_audio_clip(raw, clip, duration=random.choice([12, 14, 15, 18])):
            cache_raw(raw, "audio")
            log("ML", f"Got: {cid} — {title[:40]}")
            return clip, {"source": "Macaulay", "title": title,
                          "file": f"pool/audio/{cid}.mp3"}
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Source: Archive.org Music Collections (dedicated music crawler)
# ═══════════════════════════════════════════════════════════════════

_MUSIC_QUERIES = [
    f'collection:78rpm AND (orchestra OR symphony OR concerto)',
    f'collection:78rpm AND (jazz OR swing OR bebop OR cool jazz)',
    f'collection:78rpm AND (hawaiian OR exotica OR lounge)',
    f'collection:78rpm AND (blues OR delta blues OR country blues)',
    f'collection:78rpm AND (folk OR traditional OR ballad)',
    f'collection:78rpm AND (tango OR waltz OR bolero OR rumba)',
    f'collection:78rpm AND (piano OR organ OR harpsichord)',
    f'collection:78rpm AND (violin OR cello OR string quartet)',
    f'collection:78rpm AND (flute OR clarinet OR oboe OR bassoon)',
    f'collection:opensourceaudio AND (ambient OR drone OR meditation)',
    f'collection:opensourceaudio AND (electronic OR synthesizer OR experimental)',
    f'collection:opensourceaudio AND (acoustic OR folk OR instrumental)',
    f'collection:opensourceaudio AND (classical OR chamber OR solo)',
    f'collection:audio_music AND (guitar OR acoustic OR fingerpicking) AND {_PRE95}',
    f'collection:audio_music AND (piano OR keyboard OR Rhodes) AND {_PRE95}',
    f'collection:audio_music AND (world music OR ethnic OR traditional) AND {_PRE95}',
    f'mediatype:audio AND (Erik Satie OR Claude Debussy OR Maurice Ravel) AND {_PRE95}',
    f'mediatype:audio AND (Brian Eno OR ambient OR generative music) AND {_PRE95}',
    f'mediatype:audio AND (Terry Riley OR Steve Reich OR Philip Glass OR minimalism) AND {_PRE95}',
    f'mediatype:audio AND (Javanese OR Balinese OR gamelan OR kecak) AND {_PRE95}',
    f'mediatype:audio AND (African drumming OR djembe OR balafon OR kora) AND {_PRE95}',
    f'mediatype:audio AND (Andean OR charango OR quena OR zampoña) AND {_PRE95}',
]

def hunt_archive_music():
    """Dedicated crawler for music from archive.org collections."""
    q = random.choice(_MUSIC_QUERIES)
    log("MUSIC", f"Searching: {q[:60]}")
    docs = search_archive(q, max_results=30)
    if not docs:
        return None, None
    random.shuffle(docs)
    for item in docs[:5]:
        ident = item.get("identifier", "")
        title = item.get("title", ident)
        if is_blocked(title) or is_blocked(item.get("description", "")):
            continue
        try:
            fdata = http_get_json(f"https://archive.org/metadata/{ident}/files", timeout=10)
            files = fdata.get("result", [])
        except:
            continue
        audio_exts = [".mp3", ".ogg", ".flac", ".wav"]
        candidates = [f for f in files
                      if any(f.get("name", "").lower().endswith(e) for e in audio_exts)
                      and not is_blocked(f.get("name", ""))
                      and 50_000 < int(f.get("size", 0) or 0) < 30_000_000]
        if not candidates:
            continue
        pick = random.choice(candidates)
        furl = f"https://archive.org/download/{ident}/{urllib.parse.quote(pick['name'])}"
        raw = POOL_DIR / "audio" / f"raw_music_{random.randint(10000,99999)}.mp3"
        if not download_partial(furl, raw, max_bytes=15_000_000):
            continue
        cid = make_clip_id("audio")
        clip = POOL_DIR / "audio" / f"{cid}.mp3"
        if cut_audio_clip(raw, clip, duration=random.choice([12, 14, 15, 18, 20])):
            cache_raw(raw, "audio")
            log("MUSIC", f"Got: {cid} — {title[:40]}")
            return clip, {"source": "archive-music", "title": title,
                          "file": f"pool/audio/{cid}.mp3"}
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Source: NASA
# ═══════════════════════════════════════════════════════════════════

# NASA — focus on pre-1995 programs (Apollo, Gemini, Mercury, Skylab, early Shuttle)
NASA_Q = [
    "Apollo mission", "Apollo 11", "Apollo 13", "Apollo 17",
    "Gemini mission", "Gemini spacewalk", "Mercury program",
    "Skylab", "SpaceLab", "shuttle launch 1981",
    "shuttle launch 1983", "shuttle launch 1986", "shuttle landing",
    "astronaut training 1960", "astronaut training 1970",
    "rocket engine test", "Saturn V", "lunar surface",
    "moonwalk", "earth from space 1960", "earth from space 1970",
    "Voyager", "Pioneer", "Viking Mars",
    "solar eclipse", "aurora borealis", "nebula Hubble",
    "Jupiter Pioneer", "Saturn rings Voyager",
    "launch pad", "mission control 1960", "mission control 1970",
    "spacewalk 1965", "spacewalk 1970", "capsule recovery",
    "Van Allen belt", "magnetosphere",
    "Mariner", "Ranger moon", "Surveyor moon",
    "X-15", "lifting body", "wind tunnel",
]

def hunt_nasa():
    q = random.choice(NASA_Q)
    log("NASA", f"Searching: {q}")
    try:
        url = (f"https://images-api.nasa.gov/search?"
               f"q={urllib.parse.quote(q)}&media_type=video&page_size=20"
               f"&year_start=1958&year_end=1994")
        data = http_get_json(url, timeout=15)
        items = data.get("collection", {}).get("items", [])
    except Exception as e:
        log("NASA", f"Error: {e}")
        return None, None
    if not items:
        return None, None
    random.shuffle(items)
    for item in items[:5]:
        d = item.get("data", [{}])[0]
        title = d.get("title", "Unknown")
        nid = d.get("nasa_id", "")
        if is_blocked(title):
            continue
        try:
            manifest = http_get_json(item.get("href", ""), timeout=10)
        except:
            continue
        mp4s = [u for u in manifest if u.endswith(".mp4")]
        med = [u for u in mp4s if "medium" in u.lower() or "small" in u.lower()]
        chosen = med[0] if med else (mp4s[0] if mp4s else None)
        if not chosen:
            continue
        log("NASA", f"Downloading: {title[:50]}")
        raw = POOL_DIR / "video" / f"raw_nasa_{nid[:30]}.mp4"
        if not download_partial(chosen, raw, max_bytes=25_000_000):
            continue
        cid = make_clip_id("video")
        clip = POOL_DIR / "video" / f"{cid}.mp4"
        if cut_video_clip(raw, clip, duration=random.choice([2, 3, 3, 4])):
            cache_raw(raw, "video")
            log("NASA", f"Got: {cid} — {title[:40]}")
            return clip, {"source": "NASA", "title": title,
                          "file": f"pool/video/{cid}.mp4"}
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Source: Wikimedia Commons
# ═══════════════════════════════════════════════════════════════════

WIKI_VIDEO_CATS = [
    "Category:Nature_videos", "Category:Wildlife_videos",
    "Category:Underwater_videos", "Category:Bird_videos",
    "Category:Videos_of_fish", "Category:Timelapse_videos",
    "Category:Insect_videos", "Category:Videos_of_marine_animals",
    "Category:Videos_of_waves", "Category:Storm_videos",
    "Category:Videos_of_volcanoes", "Category:Space_videos",
    "Category:Microscopy_videos", "Category:Videos_of_spiders",
    "Category:Videos_of_cephalopods", "Category:Videos_of_jellyfish",
    "Category:Videos_of_snakes", "Category:Videos_of_frogs",
    "Category:Videos_of_fungi", "Category:Medical_videos",
    "Category:Videos_of_chemical_reactions",
    "Category:Videos_of_physical_experiments",
    "Category:Historical_films", "Category:Newsreels",
    "Category:Educational_films",
]

WIKI_AUDIO_CATS = [
    "Category:Bird_sounds", "Category:Whale_sounds",
    "Category:Insect_sounds", "Category:Frog_sounds",
    "Category:Nature_sounds", "Category:Wind_sounds",
    "Category:Rain_sounds", "Category:Ocean_sounds",
    "Category:Animal_sounds", "Category:Thunder_sounds",
    "Category:Bat_sounds", "Category:Mammal_sounds",
    "Category:River_sounds", "Category:Forest_sounds",
    "Category:Sounds_of_bells", "Category:Sounds_of_trains",
    "Category:Sounds_of_sirens", "Category:Sounds_of_machines",
]

def hunt_wiki(media_type):
    cats = WIKI_VIDEO_CATS if media_type == "video" else WIKI_AUDIO_CATS
    cat = random.choice(cats)
    log("WM", f"[{media_type}] {cat}")
    try:
        url = (f"https://commons.wikimedia.org/w/api.php?"
               f"action=query&list=categorymembers&cmtitle={urllib.parse.quote(cat)}"
               f"&cmtype=file&cmlimit=30&format=json")
        data = http_get_json(url, timeout=15)
        members = data.get("query", {}).get("categorymembers", [])
    except Exception as e:
        log("WM", f"Error: {e}")
        return None, None
    v_ext = [".ogv", ".webm", ".mp4"]
    a_ext = [".ogg", ".mp3", ".wav", ".flac"]
    exts = v_ext if media_type == "video" else a_ext
    files = [m for m in members if any(m.get("title", "").lower().endswith(e) for e in exts)]
    if not files:
        return None, None
    random.shuffle(files)
    for mf in files[:5]:
        ft = mf.get("title", "")
        name = ft.replace("File:", "").rsplit(".", 1)[0]
        if is_blocked(ft):
            continue
        try:
            iu = (f"https://commons.wikimedia.org/w/api.php?"
                  f"action=query&titles={urllib.parse.quote(ft)}"
                  f"&prop=imageinfo&iiprop=url|size&format=json")
            id2 = http_get_json(iu, timeout=10)
            pg = list(id2.get("query", {}).get("pages", {}).values())[0]
            ii = pg.get("imageinfo", [{}])[0]
            furl = ii.get("url")
            fsz = ii.get("size", 0)
        except:
            continue
        if not furl or fsz > 200_000_000:
            continue
        log("WM", f"[{media_type}] Downloading: {name[:50]}")
        rext = ft.rsplit(".", 1)[-1].lower()
        raw = POOL_DIR / media_type / f"raw_wm_{random.randint(10000,99999)}.{rext}"
        mb = 30_000_000 if media_type == "video" else 10_000_000
        if not download_partial(furl, raw, max_bytes=mb):
            continue
        oext = ".mp4" if media_type == "video" else ".mp3"
        cid = make_clip_id(media_type)
        clip = POOL_DIR / media_type / f"{cid}{oext}"
        if media_type == "video":
            ok = cut_video_clip(raw, clip, duration=random.choice([2, 3, 3, 4]))
        else:
            ok = cut_audio_clip(raw, clip, duration=random.choice([12, 14, 15, 18]))
        cache_raw(raw, media_type)
        if ok:
            log("WM", f"[{media_type}] Got: {cid} — {name[:40]}")
            return clip, {"source": "Wikimedia", "title": name,
                          "file": f"pool/{media_type}/{cid}{oext}"}
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Source: Library of Congress
# ═══════════════════════════════════════════════════════════════════

# Library of Congress — pre-1995 focus
LOC_V_Q = [
    "wildlife film", "nature documentary", "underwater expedition",
    "birds migration", "ocean waves", "national park", "glacier",
    "volcanic eruption", "microscopy", "time lapse",
    "Yellowstone", "coral reef", "arctic expedition", "tropical fish",
    "dam construction", "rocket launch", "atomic test",
    "surgical procedure", "crystal growth", "weather phenomena",
    "cave exploration", "deep sea", "insects macro",
    "railroad", "aviation history", "factory process",
    "16mm film", "educational film", "newsreel",
    "Kodachrome", "home movie", "industrial film",
]

LOC_A_Q = [
    "field recording", "bird song", "nature sounds",
    "folk music", "oral history", "radio broadcast",
    "environmental sounds", "ethnographic recording",
    "appalachian music", "native american", "ambient recording",
    "maritime sounds", "industrial noise", "street sounds",
    "ceremony recording", "dialect recording",
    "reel to reel", "wax cylinder", "78 rpm",
]

def hunt_loc(media_type):
    qs = LOC_V_Q if media_type == "video" else LOC_A_Q
    q = random.choice(qs)
    fa = "film/video" if media_type == "video" else "audio"
    log("LOC", f"[{media_type}] {q}")
    try:
        url = (f"https://www.loc.gov/search/?q={urllib.parse.quote(q)}"
               f"&fa=original-format:{urllib.parse.quote(fa)}"
               f"&dates=1900/1994&fo=json&c=20")
        data = http_get_json(url, timeout=15)
        results = data.get("results", [])
    except Exception as e:
        log("LOC", f"Error: {e}")
        return None, None
    if not results:
        return None, None
    random.shuffle(results)
    v_exts = [".mp4", ".ogv", ".mpeg", ".webm"]
    a_exts = [".mp3", ".wav", ".ogg", ".flac"]
    exts = v_exts if media_type == "video" else a_exts
    for item in results[:5]:
        title = item.get("title", "Unknown")
        if is_blocked(title):
            continue
        resources = item.get("resources", [])
        if not resources:
            iu = item.get("url", "")
            if iu:
                try:
                    resources = http_get_json(iu + "?fo=json", timeout=10).get("resources", [])
                except:
                    continue
        murl = None
        for res in resources:
            fls = res.get("files", []) if isinstance(res, dict) else []
            for fg in fls:
                items_list = fg if isinstance(fg, list) else [fg]
                for f in items_list:
                    u = f.get("url", "") if isinstance(f, dict) else ""
                    if any(u.lower().endswith(e) for e in exts):
                        murl = u
                        break
                if murl:
                    break
            if murl:
                break
        if not murl:
            continue
        log("LOC", f"[{media_type}] Downloading: {title[:50]}")
        oext = ".mp4" if media_type == "video" else ".mp3"
        raw = POOL_DIR / media_type / f"raw_loc_{random.randint(10000,99999)}{oext}"
        mb = 25_000_000 if media_type == "video" else 10_000_000
        if not download_partial(murl, raw, max_bytes=mb):
            continue
        cid = make_clip_id(media_type)
        clip = POOL_DIR / media_type / f"{cid}{oext}"
        if media_type == "video":
            ok = cut_video_clip(raw, clip, duration=random.choice([2, 3, 3, 4]))
        else:
            ok = cut_audio_clip(raw, clip, duration=random.choice([12, 14, 15, 18]))
        cache_raw(raw, media_type)
        if ok:
            log("LOC", f"[{media_type}] Got: {cid} — {title[:40]}")
            return clip, {"source": "Library of Congress", "title": title,
                          "file": f"pool/{media_type}/{cid}{oext}"}
    return None, None


# (Smithsonian removed — requires API key since 2025)


# ═══════════════════════════════════════════════════════════════════
# Toast Image Crawler — collects marmalade-on-toast photos
# for rotating UI backgrounds
# ═══════════════════════════════════════════════════════════════════

TOAST_SEARCH_Q = [
    "marmalade on toast", "marmalade toast photograph",
    "orange marmalade on toast", "Seville orange marmalade toast",
    "marmalade spread on toast", "toast with marmalade",
    "thick cut marmalade toast", "marmalade toast breakfast plate",
    "orange marmalade bread slice", "jar of marmalade with toast",
    "marmalade toast butter knife", "homemade marmalade on toast",
    # Broader but still relevant — helps find more results
    "marmalade jar", "orange preserve", "citrus marmalade jar",
    "marmalade breakfast", "toast with orange jam",
    "bread with marmalade", "marmalade and butter",
]

TOAST_WIKI_CATS = [
    "Category:Marmalade", "Category:Toast_(food)",
    "Category:Citrus_fruits", "Category:Breakfast",
]

MAX_TOAST_IMAGES = 60
TOAST_CRAWL_INTERVAL = 20  # seconds between toast hunts
TOAST_WORKERS = 3           # parallel toast crawler threads


def hunt_toast_wikimedia():
    """Search Wikimedia Commons for marmalade/toast photos."""
    if random.random() < 0.5:
        # Category browsing
        cat = random.choice(TOAST_WIKI_CATS)
        try:
            offset = random.randint(0, 100)
            url = (f"https://commons.wikimedia.org/w/api.php?"
                   f"action=query&list=categorymembers&cmtitle={urllib.parse.quote(cat)}"
                   f"&cmtype=file&cmlimit=30&cmcontinue=&format=json"
                   f"&cmoffset={offset}")
            data = http_get_json(url, timeout=15)
            members = data.get("query", {}).get("categorymembers", [])
        except:
            return None
        files = [m for m in members
                 if any(m.get("title", "").lower().endswith(e) for e in [".jpg", ".jpeg", ".png"])]
    else:
        # Text search
        q = random.choice(TOAST_SEARCH_Q)
        try:
            url = (f"https://commons.wikimedia.org/w/api.php?"
                   f"action=query&list=search&srsearch={urllib.parse.quote(q)}"
                   f"&srnamespace=6&srlimit=20&format=json")
            data = http_get_json(url, timeout=15)
            results = data.get("query", {}).get("search", [])
            files = [{"title": r["title"]} for r in results
                     if any(r.get("title", "").lower().endswith(e) for e in [".jpg", ".jpeg", ".png"])]
        except:
            return None
    if not files:
        return None
    random.shuffle(files)
    for mf in files[:5]:
        ft = mf.get("title", "")
        try:
            iu = (f"https://commons.wikimedia.org/w/api.php?"
                  f"action=query&titles={urllib.parse.quote(ft)}"
                  f"&prop=imageinfo&iiprop=url|size|mime&iiurlwidth=1200&format=json")
            id2 = http_get_json(iu, timeout=10)
            pg = list(id2.get("query", {}).get("pages", {}).values())[0]
            ii = pg.get("imageinfo", [{}])[0]
            furl = ii.get("thumburl", ii.get("url"))
            mime = ii.get("mime", "")
            fsz = ii.get("size", 0)
        except:
            continue
        if not furl or "image" not in mime:
            continue
        if fsz > 15_000_000:
            continue
        # Download
        fname = f"toast_{int(time.time())}_{random.randint(1000,9999)}.jpg"
        dest = TOAST_DIR / fname
        try:
            req = urllib.request.Request(furl, headers={"User-Agent": "Marmalade/3.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                with open(dest, "wb") as f:
                    f.write(resp.read())
            if dest.exists() and dest.stat().st_size > 5000:
                log("TOAST", f"Got: {fname} from {ft[:40]}")
                return fname
            else:
                dest.unlink(missing_ok=True)
        except:
            dest.unlink(missing_ok=True)
    return None


def hunt_toast_archive():
    """Search archive.org for marmalade/toast images."""
    q = random.choice(TOAST_SEARCH_Q)
    try:
        page = random.randint(1, 5)
        params = {
            "q": f"({q}) AND mediatype:image",
            "output": "json", "rows": "15", "page": str(page),
            "fl[]": "identifier,title",
        }
        url = f"https://archive.org/advancedsearch.php?{urllib.parse.urlencode(params, doseq=True)}"
        data = http_get_json(url, timeout=15)
        docs = data.get("response", {}).get("docs", [])
    except:
        return None
    if not docs:
        return None
    random.shuffle(docs)
    for item in docs[:5]:
        ident = item.get("identifier", "")
        try:
            fdata = http_get_json(f"https://archive.org/metadata/{ident}/files", timeout=10)
            files = fdata.get("result", [])
        except:
            continue
        imgs = [f for f in files
                if any(f.get("name", "").lower().endswith(e) for e in [".jpg", ".jpeg", ".png"])
                and 5000 < int(f.get("size", 0) or 0) < 10_000_000]
        if not imgs:
            continue
        pick = random.choice(imgs)
        furl = f"https://archive.org/download/{ident}/{urllib.parse.quote(pick['name'])}"
        fname = f"toast_{int(time.time())}_{random.randint(1000,9999)}.jpg"
        dest = TOAST_DIR / fname
        try:
            req = urllib.request.Request(furl, headers={"User-Agent": "Marmalade/3.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                with open(dest, "wb") as f:
                    f.write(resp.read())
            if dest.exists() and dest.stat().st_size > 5000:
                log("TOAST", f"Got: {fname} from {ident[:40]}")
                return fname
            else:
                dest.unlink(missing_ok=True)
        except:
            dest.unlink(missing_ok=True)
    return None


def hunt_toast_openverse():
    """Search Openverse (Creative Commons) for marmalade/toast photos. No API key needed."""
    q = random.choice(TOAST_SEARCH_Q)
    page = random.randint(1, 3)
    try:
        url = (f"https://api.openverse.org/v1/images/"
               f"?q={urllib.parse.quote(q)}&page={page}&page_size=20"
               f"&license_type=commercial&extension=jpg")
        data = http_get_json(url, timeout=15)
        results = data.get("results", [])
    except Exception as e:
        log("TOAST", f"Openverse search error: {e}")
        return None
    if not results:
        return None
    random.shuffle(results)
    for item in results[:6]:
        thumb = item.get("thumbnail") or item.get("url")
        if not thumb:
            continue
        fname = f"toast_{int(time.time())}_{random.randint(1000,9999)}.jpg"
        dest = TOAST_DIR / fname
        try:
            req = urllib.request.Request(thumb, headers={"User-Agent": "Marmalade/3.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                with open(dest, "wb") as f:
                    f.write(resp.read())
            if dest.exists() and dest.stat().st_size > 5000:
                log("TOAST", f"Openverse: {fname} — {item.get('title', '')[:40]}")
                return fname
            else:
                dest.unlink(missing_ok=True)
        except:
            dest.unlink(missing_ok=True)
    return None


def hunt_toast_europeana():
    """Search Europeana for marmalade/toast/preserve images."""
    q = random.choice(TOAST_SEARCH_Q)
    page = random.randint(1, 3)
    try:
        params = {
            "query": q,
            "wskey": "api2demo",
            "media": "true",
            "qf": "TYPE:IMAGE",
            "rows": "20",
            "start": str((page - 1) * 20 + 1),
        }
        url = f"https://api.europeana.eu/record/v2/search.json?{urllib.parse.urlencode(params)}"
        data = http_get_json(url, timeout=15)
        items = data.get("items", [])
    except Exception as e:
        log("TOAST", f"Europeana search error: {e}")
        return None
    if not items:
        return None
    random.shuffle(items)
    for item in items[:6]:
        # Try to get a direct image URL
        img_url = None
        for agg in item.get("edmIsShownBy", []):
            if any(agg.lower().endswith(e) for e in [".jpg", ".jpeg", ".png"]):
                img_url = agg
                break
        if not img_url:
            # Try edmPreview (thumbnail)
            previews = item.get("edmPreview", [])
            if previews:
                img_url = previews[0]
        if not img_url:
            continue
        fname = f"toast_{int(time.time())}_{random.randint(1000,9999)}.jpg"
        dest = TOAST_DIR / fname
        try:
            req = urllib.request.Request(img_url, headers={"User-Agent": "Marmalade/3.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                with open(dest, "wb") as f:
                    f.write(resp.read())
            if dest.exists() and dest.stat().st_size > 5000:
                title = (item.get("title", [""])[0] if isinstance(item.get("title"), list)
                         else item.get("title", ""))
                log("TOAST", f"Europeana: {fname} — {str(title)[:40]}")
                return fname
            else:
                dest.unlink(missing_ok=True)
        except:
            dest.unlink(missing_ok=True)
    return None


def hunt_toast_flickr():
    """Search Flickr public feeds for marmalade/toast photos. No API key needed."""
    q = random.choice(TOAST_SEARCH_Q)
    try:
        url = (f"https://api.flickr.com/services/feeds/photos_public.gne?"
               f"tags={urllib.parse.quote(q)}&format=json&nojsoncallback=1")
        data = http_get_json(url, timeout=15)
        items = data.get("items", [])
    except Exception as e:
        log("TOAST", f"Flickr feed error: {e}")
        return None
    if not items:
        return None
    random.shuffle(items)
    for item in items[:6]:
        # Flickr feed gives media:m URLs — swap _m for _c (800px) or _b (1024px)
        media = item.get("media", {}).get("m", "")
        if not media:
            continue
        img_url = media.replace("_m.jpg", "_b.jpg")
        fname = f"toast_{int(time.time())}_{random.randint(1000,9999)}.jpg"
        dest = TOAST_DIR / fname
        try:
            req = urllib.request.Request(img_url, headers={"User-Agent": "Marmalade/3.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                with open(dest, "wb") as f:
                    f.write(resp.read())
            if dest.exists() and dest.stat().st_size > 5000:
                log("TOAST", f"Flickr: {fname} — {item.get('title', '')[:40]}")
                return fname
            else:
                dest.unlink(missing_ok=True)
        except:
            dest.unlink(missing_ok=True)
    return None


class ToastCollection:
    """Manages a rotating set of marmalade-on-toast background images."""
    def __init__(self):
        self._lock = threading.Lock()
        self.images = []
        self._scan()

    def _scan(self):
        for f in sorted(TOAST_DIR.glob("toast_*.jpg")):
            if f.stat().st_size > 5000:
                self.images.append(f.name)
        # Also include the original generated toast
        if (BASE_DIR / "toast.jpg").exists():
            self.images.insert(0, "__original__")
        log("TOAST", f"Loaded {len(self.images)} toast images")

    def add(self, fname):
        with self._lock:
            self.images.append(fname)
            # Cap collection
            while len(self.images) > MAX_TOAST_IMAGES:
                old = self.images.pop(1 if self.images[0] == "__original__" else 0)
                if old != "__original__":
                    try:
                        (TOAST_DIR / old).unlink()
                    except:
                        pass

    def random_url(self):
        with self._lock:
            if not self.images:
                return "toast.jpg"
            pick = random.choice(self.images)
            if pick == "__original__":
                return "toast.jpg"
            return f"toasts/{pick}"

    def list_all(self):
        with self._lock:
            urls = []
            for img in self.images:
                if img == "__original__":
                    urls.append("toast.jpg")
                else:
                    urls.append(f"toasts/{img}")
            return urls


# ═══════════════════════════════════════════════════════════════════
# Source: Europeana  (EU digital library collections)
# ═══════════════════════════════════════════════════════════════════

# Europeana — pre-1995 European archive collections
EUROPEANA_Q = [
    "whale recording", "bird song field", "underwater sound",
    "volcanic eruption film", "wildlife documentary", "microscopy film",
    "glacier footage", "insect macro", "ocean deep",
    "cave exploration film", "ethnographic film", "industrial film",
    "medical film", "astronomical observation", "newsreel",
    "16mm nature", "educational film science", "archive film wildlife",
    "Pathé nature", "colonial film", "expedition film",
]

def hunt_europeana(media_type):
    """Try Europeana — no key needed for basic search."""
    q = random.choice(EUROPEANA_Q)
    mt_filter = "VIDEO" if media_type == "video" else "SOUND"
    log("EU", f"[{media_type}] {q}")
    try:
        url = (f"https://api.europeana.eu/record/v2/search.json?"
               f"query={urllib.parse.quote(q)}&qf=TYPE:{mt_filter}"
               f"&reusability=open&rows=20&wskey=api2demo")
        data = http_get_json(url, timeout=15)
        items = data.get("items", [])
    except Exception as e:
        log("EU", f"Error: {e}")
        return None, None
    if not items:
        return None, None
    random.shuffle(items)
    for item in items[:5]:
        title = item.get("title", ["Unknown"])[0] if isinstance(item.get("title"), list) else item.get("title", "Unknown")
        if is_blocked(str(title)):
            continue
        # Try to find a direct media link
        edmIsShownBy = item.get("edmIsShownBy", [])
        edmHasView = item.get("edmHasView", [])
        urls = edmIsShownBy + edmHasView if isinstance(edmIsShownBy, list) else [edmIsShownBy] + (edmHasView if isinstance(edmHasView, list) else [edmHasView])
        urls = [u for u in urls if u]
        v_exts = [".mp4", ".ogv", ".webm", ".avi", ".mpeg"]
        a_exts = [".mp3", ".ogg", ".wav", ".flac"]
        exts = v_exts if media_type == "video" else a_exts
        murl = None
        for u in urls:
            if any(u.lower().endswith(e) for e in exts):
                murl = u
                break
        if not murl:
            continue
        log("EU", f"[{media_type}] Downloading: {str(title)[:50]}")
        oext = ".mp4" if media_type == "video" else ".mp3"
        raw = POOL_DIR / media_type / f"raw_eu_{random.randint(10000,99999)}{oext}"
        mb = 25_000_000 if media_type == "video" else 10_000_000
        if not download_partial(murl, raw, max_bytes=mb):
            continue
        cid = make_clip_id(media_type)
        clip = POOL_DIR / media_type / f"{cid}{oext}"
        if media_type == "video":
            ok = cut_video_clip(raw, clip, duration=random.choice([2, 3, 3, 4]))
        else:
            ok = cut_audio_clip(raw, clip, duration=random.choice([12, 14, 15, 18]))
        cache_raw(raw, media_type)
        if ok:
            log("EU", f"[{media_type}] Got: {cid} — {str(title)[:40]}")
            return clip, {"source": "Europeana", "title": str(title),
                          "file": f"pool/{media_type}/{cid}{oext}"}
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Clip Pool
# ═══════════════════════════════════════════════════════════════════

MIN_VIDEO_FOR_VIGNETTE = 6   # need this many UNUSED video clips
MIN_AUDIO_FOR_VIGNETTE = 4   # need this many UNUSED audio clips


class ClipPool:
    """Clips are single-use: once consumed by a vignette, they're gone.
    The pool is a queue — crawlers fill it, vignettes drain it.
    Each vignette is unique; no clip ever repeats."""

    def __init__(self):
        self._lock = threading.Lock()
        self.video = []       # available (unused) video clips
        self.audio = []       # available (unused) audio clips
        self._scan()

    def _scan(self):
        """Load existing clips on startup. Skip broken files."""
        skipped = 0
        for f in sorted((POOL_DIR / "video").glob("v_*.mp4")):
            if f.stat().st_size < 5000:
                skipped += 1
                continue
            dur = probe_duration(f)
            if not dur or dur < 0.5:
                skipped += 1
                continue
            self.video.append({"source": "cached", "title": f.stem,
                               "file": f"pool/video/{f.name}"})
        for f in sorted((POOL_DIR / "audio").glob("a_*.mp3")):
            if f.stat().st_size < 3000:
                skipped += 1
                continue
            dur = probe_duration(f)
            if not dur or dur < 1.0:
                skipped += 1
                continue
            self.audio.append({"source": "cached", "title": f.stem,
                               "file": f"pool/audio/{f.name}"})
        log("POOL", f"Loaded {len(self.video)}v {len(self.audio)}a (skipped {skipped} broken)")

    def cleanup(self):
        """Remove pool entries whose files were manually deleted or are now blocked.
        Called periodically by a background thread."""
        removed = 0
        with self._lock:
            for lst in (self.video, self.audio):
                to_remove = []
                for i, clip in enumerate(lst):
                    fp = BASE_DIR / clip.get("file", "")
                    # File was deleted by user
                    if not fp.exists():
                        to_remove.append(i)
                        removed += 1
                        continue
                    # Title now matches a blocked term (user updated blocked.txt)
                    if is_blocked(clip.get("title", "")):
                        to_remove.append(i)
                        removed += 1
                        try: fp.unlink()
                        except: pass
                for i in reversed(to_remove):
                    lst.pop(i)
        if removed > 0:
            log("POOL", f"Cleanup: removed {removed} clips (deleted or newly blocked)")

    def add(self, media_type, meta):
        # Final safety net — reject blocked content at pool level
        if is_blocked(meta.get("title", "")) or is_blocked(meta.get("source", "")):
            log("POOL", f"Blocked at pool level: {meta.get('title', '')[:40]}")
            try: (BASE_DIR / meta["file"]).unlink()
            except: pass
            return
        with self._lock:
            lst = self.video if media_type == "video" else self.audio
            mx = MAX_VIDEO_POOL if media_type == "video" else MAX_AUDIO_POOL
            lst.append(meta)
            # Cap the queue size — evict oldest if too long
            while len(lst) > mx:
                old = lst.pop(0)
                try:
                    (BASE_DIR / old["file"]).unlink()
                except:
                    pass

    def consume(self, media_type, n):
        """Take n clips from the pool — they're removed and won't be reused.
        Returns the clips, or empty list if not enough."""
        with self._lock:
            lst = self.video if media_type == "video" else self.audio
            if len(lst) < n:
                return []
            # Shuffle for variety, then take n
            random.shuffle(lst)
            taken = lst[:n]
            del lst[:n]
            return taken

    def count(self):
        with self._lock:
            return len(self.video), len(self.audio)

    def has_enough_for_vignette(self):
        v, a = self.count()
        return v >= MIN_VIDEO_FOR_VIGNETTE and a >= MIN_AUDIO_FOR_VIGNETTE


# ═══════════════════════════════════════════════════════════════════
# Vignette Archive
# ═══════════════════════════════════════════════════════════════════

class VignetteArchive:
    def __init__(self):
        self._lock = threading.Lock()
        self.items = []
        self._load()

    def _path(self):
        return ARCHIVE_DIR / "vignettes.json"

    def _load(self):
        try:
            if self._path().exists():
                self.items = json.loads(self._path().read_text())
                log("ARC", f"Loaded {len(self.items)} archived vignettes")
        except:
            self.items = []

    def _save(self):
        try:
            self._path().write_text(json.dumps(self.items, indent=2))
        except:
            pass

    def add(self, v):
        """Archive a vignette — copies clip files to archive/vignettes/ so they persist."""
        import shutil
        arc_clips = ARCHIVE_DIR / "vignettes"
        arc_clips.mkdir(exist_ok=True)
        # Copy all clip files into the archive folder and update paths
        for clip_list_key in ("video", "audio"):
            for clip in v.get(clip_list_key, []):
                src = BASE_DIR / clip.get("file", "")
                if src.exists():
                    dest = arc_clips / src.name
                    try:
                        if not dest.exists():
                            shutil.copy2(str(src), str(dest))
                        clip["file"] = f"archive/vignettes/{src.name}"
                    except:
                        pass
        with self._lock:
            self.items.append(v)
            self._save()

    def list_all(self):
        with self._lock:
            return list(self.items)


# ═══════════════════════════════════════════════════════════════════
# Crawler
# ═══════════════════════════════════════════════════════════════════

# Source weights: (name, function_or_tuple, weight)
VIDEO_SOURCES = [
    ("archive", lambda: hunt_archive("video"), 4),
    ("nasa", hunt_nasa, 3),
    ("wikimedia", lambda: hunt_wiki("video"), 2),
    ("loc", lambda: hunt_loc("video"), 1),
    ("europeana", lambda: hunt_europeana("video"), 1),
]

AUDIO_SOURCES = [
    ("archive", lambda: hunt_archive("audio"), 4),       # nature, ambient, texture
    ("archive-music", lambda: hunt_archive_music(), 5),   # music, instrumental, world
    ("macaulay", hunt_macaulay, 2),                       # birds/nature (reduced)
    ("wikimedia", lambda: hunt_wiki("audio"), 2),
    ("loc", lambda: hunt_loc("audio"), 1),
    ("europeana", lambda: hunt_europeana("audio"), 1),
]

def weighted_pick(sources):
    total = sum(w for _, _, w in sources)
    r = random.uniform(0, total)
    acc = 0
    for name, fn, w in sources:
        acc += w
        if r <= acc:
            return name, fn
    return sources[-1][0], sources[-1][1]


PARALLEL_WORKERS = 5  # concurrent downloads per media type

class Crawler:
    def __init__(self, pool, llm, toasts=None):
        self.pool = pool
        self.llm = llm
        self.toasts = toasts
        self.running = False
        self.status = {
            "video": "idle", "audio": "idle", "llm": "disconnected",
            "video_hunts": 0, "audio_hunts": 0,
            "video_found": 0, "audio_found": 0,
        }
        self._lock = threading.Lock()
        self.llm_video_q = []
        self.llm_audio_q = []
        self.last_llm = 0
        # Track sources that keep failing so we skip them temporarily
        self._fail_count = {}   # source_name -> consecutive failures
        self._fail_cooldown = {}  # source_name -> timestamp when eligible again

    def start(self):
        self.running = True
        # Launch multiple parallel workers per media type
        for i in range(PARALLEL_WORKERS):
            threading.Thread(target=self._video_loop, daemon=True,
                             name=f"video-{i}").start()
            threading.Thread(target=self._audio_loop, daemon=True,
                             name=f"audio-{i}").start()
        # Toast image crawlers (multiple parallel, independent)
        for i in range(TOAST_WORKERS):
            threading.Thread(target=self._toast_loop, daemon=True,
                             name=f"toast-{i}").start()
        # Pool cleanup thread — purges deleted/blocked clips every 30s
        threading.Thread(target=self._cleanup_loop, daemon=True,
                         name="pool-cleanup").start()
        log("CRAWL", f"Background crawlers started ({PARALLEL_WORKERS}x video + {PARALLEL_WORKERS}x audio + {TOAST_WORKERS}x toast + cleanup)")

    def _cleanup_loop(self):
        """Periodically remove pool clips that were deleted or newly blocked."""
        while self.running:
            time.sleep(30)
            try:
                self.pool.cleanup()
            except Exception as e:
                log("CLEANUP", f"Error: {e}")

    def _set(self, k, v):
        with self._lock:
            self.status[k] = v

    def _inc(self, k):
        with self._lock:
            self.status[k] = self.status.get(k, 0) + 1

    def get_status(self):
        with self._lock:
            v, a = self.pool.count()
            return {**self.status, "pool_video": v, "pool_audio": a,
                    "need_video": MIN_VIDEO_FOR_VIGNETTE,
                    "need_audio": MIN_AUDIO_FOR_VIGNETTE}

    def _mark_fail(self, source_name):
        """Track consecutive failures; after 5 in a row, cool down 2 minutes."""
        with self._lock:
            self._fail_count[source_name] = self._fail_count.get(source_name, 0) + 1
            if self._fail_count[source_name] >= 5:
                self._fail_cooldown[source_name] = time.time() + 120
                log("CRAWL", f"Source '{source_name}' cooled down (5 consecutive fails)")

    def _mark_success(self, source_name):
        with self._lock:
            self._fail_count[source_name] = 0

    def _is_cooled_down(self, source_name):
        with self._lock:
            cd = self._fail_cooldown.get(source_name, 0)
            if cd and time.time() < cd:
                return True
            if cd and time.time() >= cd:
                self._fail_cooldown.pop(source_name, None)
                self._fail_count[source_name] = 0
            return False

    def _pick_available_source(self, sources):
        """Weighted pick but skip cooled-down sources."""
        available = [(n, fn, w) for n, fn, w in sources if not self._is_cooled_down(n)]
        if not available:
            # All cooled down — just pick any
            available = sources
        return weighted_pick(available)

    def _refresh_llm(self):
        now = time.time()
        if now - self.last_llm < LLM_QUERY_INTERVAL:
            return
        self.last_llm = now
        if not self.llm.available:
            self.llm.check()
        if self.llm.available:
            self._set("llm", "thinking")
            vq = self.llm.generate_queries("video")
            if vq:
                self.llm_video_q = vq
                log("LLM", f"Video queries: {vq}")
            aq = self.llm.generate_queries("audio")
            if aq:
                self.llm_audio_q = aq
                log("LLM", f"Audio queries: {aq}")
            self._set("llm", "connected")
        else:
            self._set("llm", "disconnected")

    def _video_loop(self):
        while self.running:
            try:
                v, _ = self.pool.count()
                if v >= MAX_VIDEO_POOL:
                    self._set("video", "pool full")
                    time.sleep(10)
                    continue

                self._refresh_llm()
                self._set("video", "hunting")
                self._inc("video_hunts")

                source_name = None
                # Sometimes use LLM queries with archive.org
                if self.llm_video_q and random.random() < 0.35:
                    q = random.choice(self.llm_video_q)
                    source_name = "archive-llm"
                    _, meta = hunt_archive("video",
                                           queries=[f'mediatype:movies AND ({q})'])
                else:
                    source_name, fn = self._pick_available_source(VIDEO_SOURCES)
                    _, meta = fn()

                if meta:
                    self.pool.add("video", meta)
                    self._inc("video_found")
                    self._set("video", f"found: {meta['title'][:30]}")
                    self._mark_success(source_name)
                else:
                    self._set("video", "miss")
                    self._mark_fail(source_name)
                    # Fallback: try re-cutting from raw cache
                    _, recut_meta = recut_from_cache("video")
                    if recut_meta:
                        self.pool.add("video", recut_meta)
                        self._inc("video_found")
                        self._set("video", f"recut: {recut_meta['title'][:30]}")
            except Exception as e:
                log("CRAWL", f"Video error: {e}")
                traceback.print_exc()
                if source_name:
                    self._mark_fail(source_name)
            time.sleep(CRAWL_INTERVAL + random.uniform(-5, 5))

    def _audio_loop(self):
        while self.running:
            try:
                _, a = self.pool.count()
                if a >= MAX_AUDIO_POOL:
                    self._set("audio", "pool full")
                    time.sleep(10)
                    continue

                self._refresh_llm()
                self._set("audio", "hunting")
                self._inc("audio_hunts")

                source_name = None
                if self.llm_audio_q and random.random() < 0.35:
                    q = random.choice(self.llm_audio_q)
                    source_name = "archive-llm"
                    _, meta = hunt_archive("audio",
                                           queries=[f'mediatype:audio AND ({q})'])
                else:
                    source_name, fn = self._pick_available_source(AUDIO_SOURCES)
                    _, meta = fn()

                if meta:
                    self.pool.add("audio", meta)
                    self._inc("audio_found")
                    self._set("audio", f"found: {meta['title'][:30]}")
                    self._mark_success(source_name)
                else:
                    self._set("audio", "miss")
                    self._mark_fail(source_name)
                    # Fallback: try re-cutting from raw cache
                    _, recut_meta = recut_from_cache("audio")
                    if recut_meta:
                        self.pool.add("audio", recut_meta)
                        self._inc("audio_found")
                        self._set("audio", f"recut: {recut_meta['title'][:30]}")
            except Exception as e:
                log("CRAWL", f"Audio error: {e}")
                traceback.print_exc()
                if source_name:
                    self._mark_fail(source_name)
            time.sleep(CRAWL_INTERVAL + random.uniform(-5, 5))

    _TOAST_SOURCES = [
        ("wikimedia",  hunt_toast_wikimedia),
        ("archive.org", hunt_toast_archive),
        ("openverse",  hunt_toast_openverse),
        ("europeana",  hunt_toast_europeana),
        ("flickr",     hunt_toast_flickr),
    ]

    def _toast_loop(self):
        """Background crawler for marmalade-on-toast images.
        Tries multiple sources per cycle before sleeping."""
        if not self.toasts:
            log("TOAST", "No toast collection — toast crawler disabled")
            return
        wname = threading.current_thread().name
        log("TOAST", f"{wname} started")
        while self.running:
            try:
                n = len(self.toasts.images)
                if n >= MAX_TOAST_IMAGES:
                    time.sleep(TOAST_CRAWL_INTERVAL * 4)
                    continue
                # Try up to 3 sources per cycle to increase hit rate
                sources_this_round = random.sample(
                    self._TOAST_SOURCES, min(3, len(self._TOAST_SOURCES)))
                found = False
                for src, hunt_fn in sources_this_round:
                    try:
                        fname = hunt_fn()
                        if fname:
                            self.toasts.add(fname)
                            log("TOAST", f"[{wname}] +1 from {src}: {fname} ({len(self.toasts.images)}/{MAX_TOAST_IMAGES})")
                            found = True
                            break
                    except Exception as e:
                        log("TOAST", f"[{wname}] {src} error: {e}")
                if not found:
                    log("TOAST", f"[{wname}] miss (tried {len(sources_this_round)} sources)")
            except Exception as e:
                log("TOAST", f"[{wname}] Error: {e}")
                traceback.print_exc()
            time.sleep(TOAST_CRAWL_INTERVAL + random.uniform(-5, 10))


# ═══════════════════════════════════════════════════════════════════
# Vignette assembler  (naming logic lives here)
# ═══════════════════════════════════════════════════════════════════

def assemble_vignette(pool, llm, archive):
    """Consume clips from the pool — each clip is used once, never repeated.
    Waits until the pool has enough; caller should check has_enough_for_vignette() first."""
    video_clips = pool.consume("video", MIN_VIDEO_FOR_VIGNETTE)
    audio_clips = pool.consume("audio", MIN_AUDIO_FOR_VIGNETTE)

    # Agent B: describe the video selection in 2 words
    if video_clips:
        sample_v = random.choice(video_clips)
        v_words = None
        if llm.available:
            v_words = llm.describe_clip("video", sample_v.get("title", ""),
                                         sample_v.get("source", ""))
        if not v_words:
            v_words = pick_two_words(MONO_VIDEO, MULTI_VIDEO)
    else:
        v_words = pick_two_words(MONO_VIDEO, MULTI_VIDEO)

    # Agent A: describe the audio selection in 2 words
    if audio_clips:
        sample_a = random.choice(audio_clips)
        a_words = None
        if llm.available:
            a_words = llm.describe_clip("audio", sample_a.get("title", ""),
                                         sample_a.get("source", ""))
        if not a_words:
            a_words = pick_two_words(MONO_AUDIO, MULTI_AUDIO)
    else:
        a_words = pick_two_words(MONO_AUDIO, MULTI_AUDIO)

    # Build 3-word title
    title = build_title(v_words, a_words)

    vignette = {
        "name": title,
        "created": datetime.now().isoformat(),
        "video": video_clips,
        "audio": audio_clips,
        "duration": VIGNETTE_DURATION,
        "video_words": v_words,
        "audio_words": a_words,
    }

    archive.add(vignette)
    log("PLAY", f"Vignette: {title} ({len(video_clips)}v + {len(audio_clips)}a)")
    return vignette


# ═══════════════════════════════════════════════════════════════════
# HTTP Server
# ═══════════════════════════════════════════════════════════════════

_pool = None
_crawler = None
_archive = None
_llm = None
_toasts = None


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(BASE_DIR), **kw)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/next":
            self.handle_next()
        elif p == "/api/archive":
            self.handle_archive()
        elif p == "/api/status":
            self.handle_status()
        elif p == "/api/logs":
            self.handle_logs()
        elif p == "/api/toast":
            self.handle_toast()
        elif p == "/api/blocked":
            self.handle_blocked_list()
        else:
            super().do_GET()

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/reject":
            self.handle_reject()
        else:
            self.send_error(404)

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def handle_next(self):
        """Frontend polls this to get the next vignette when ready.
        Only serves a vignette when the pool has enough unique clips."""
        if not _pool.has_enough_for_vignette():
            v, a = _pool.count()
            self._json({"ready": False, "pool_video": v, "pool_audio": a,
                         "need_video": MIN_VIDEO_FOR_VIGNETTE,
                         "need_audio": MIN_AUDIO_FOR_VIGNETTE})
            return
        vignette = assemble_vignette(_pool, _llm, _archive)
        vignette["ready"] = True
        v, a = _pool.count()
        vignette["pool_video"] = v
        vignette["pool_audio"] = a
        self._json(vignette)

    def handle_archive(self):
        self._json({"items": _archive.list_all()})

    def handle_status(self):
        self._json(_crawler.get_status())

    def handle_logs(self):
        self._json({"logs": get_logs(50)})

    def handle_toast(self):
        """Return a random toast background URL, or all of them."""
        self._json({
            "current": _toasts.random_url() if _toasts else "toast.jpg",
            "all": _toasts.list_all() if _toasts else ["toast.jpg"],
        })

    def handle_reject(self):
        """Add a term to rejected.txt — content with this term will be purged and blocked."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            term = body.get("term", "").strip().lower()
            if not term:
                self._json({"ok": False, "error": "empty term"}, 400)
                return
            # Append to rejected.txt
            with open(REJECTED_FILE, "a") as f:
                f.write(term + "\n")
            # Force reload
            _load_user_blocklist()
            # Immediately purge matching clips from pool
            _pool.cleanup()
            log("REJECT", f"User rejected term: '{term}'")
            self._json({"ok": True, "term": term})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def handle_blocked_list(self):
        """Return the full active block list so UI can show it."""
        self._json({"terms": get_blocked(), "count": len(get_blocked())})

    def log_message(self, fmt, *args):
        if "404" in str(args) or "500" in str(args):
            super().log_message(fmt, *args)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def clear_pool():
    """Wipe old pool clips so crawlers start fresh."""
    count = 0
    for d in ["video", "audio"]:
        pool_sub = POOL_DIR / d
        for f in pool_sub.glob("*"):
            try:
                f.unlink()
                count += 1
            except:
                pass
    log("POOL", f"Cleared {count} old clips — starting fresh")


def main():
    global _pool, _crawler, _archive, _llm, _toasts

    # Pool persists across restarts — only clear with --fresh flag
    if "--fresh" in sys.argv:
        clear_pool()
    else:
        log("POOL", "Keeping existing pool (use --fresh to wipe)")

    # Clean raw downloads
    for d in ["video", "audio"]:
        for f in (POOL_DIR / d).glob("raw_*"):
            try:
                f.unlink()
            except:
                pass

    print(f"""
\033[38;5;208m
╔══════════════════════════════════════════════════════════╗
║               M A R M A L A D E   v 3                   ║
║                                                          ║
║   Continuous crawl. LLM-driven queries. Auto-play.       ║
║   No button. Just a stream of found footage.             ║
║   {PARALLEL_WORKERS}x parallel workers per media type.               ║
║                                                          ║
║   Sources: archive.org · Macaulay Library · NASA         ║
║            Library of Congress · Wikimedia · Europeana    ║
║                                                          ║
║   http://localhost:{PORT}                                  ║
╚══════════════════════════════════════════════════════════╝
\033[0m""")

    _llm = OllamaClient()
    _llm.check()
    _pool = ClipPool()
    _archive = VignetteArchive()
    _toasts = ToastCollection()
    _crawler = Crawler(_pool, _llm, _toasts)
    _crawler.start()

    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    log("SERVER", f"http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        _crawler.running = False
        server.shutdown()


if __name__ == "__main__":
    main()
