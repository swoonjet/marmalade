#!/bin/bash
# Sync latest vignettes to GitHub Pages (jontoews.com/marmalade)
# Uses orphan branch force-push — no history bloat
set -e

# moved 2026-07-30 from ~/Documents/Marmalade during the Documents cleanup
#
# 2026-09-14: the source of truth is now the DROPLET's live archive, not the
# local one. The local engine stopped running in March, so this job spent
# months re-pushing the same 276 March vignettes every hour. The droplet
# (marmalade.jontoews.com) is the instance that actually crawls, so we pull
# the latest vignettes down from it before staging. Bulk media lives on the
# 4TB drive rather than the MacBook's internal disk, which is ~93% full.
#
# The working mirror stays on the internal disk on purpose. It is only ~350MB,
# it is rewritten every run, and putting it on the SMB drive made this job both
# slow (~7MB/min) and dependent on the drive being mounted at 04:30. The 4TB
# drive holds the cold archive (Marmalade Archive/), which is what actually
# needed to come off a 93%-full internal disk.
DROPLET="root@192.241.144.238"
DROPLET_DIR="/opt/marmalade"
MARMALADE_DIR="/Users/jontoewsinterceptgroup.com/Creative-Projects/marmalade/live-mirror"
LOCAL_REPO="/Users/jontoewsinterceptgroup.com/Creative-Projects/marmalade/source"

# Don't let a stalled stream hang the nightly job forever. The droplet is a
# 1-vCPU box and marmalade's own ffmpeg crawlers can saturate it (observed
# load average 4.0), which starves the transfer.
SSH_OPTS="-o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=8"
STAGE_DIR="/tmp/marmalade-stage"
REPO="swoonjet/marmalade"
BRANCH="gh-pages"
MAX_VIGNETTES=25
LOG="$LOCAL_REPO/sync.log"
LOCK="$LOCAL_REPO/.syncing"

# Prevent overlapping runs
if [ -f "$LOCK" ]; then
  pid=$(cat "$LOCK")
  if kill -0 "$pid" 2>/dev/null; then
    echo "$(date): Sync already running (pid $pid)" >> "$LOG"
    exit 0
  fi
fi
echo $$ > "$LOCK"
trap "rm -f $LOCK" EXIT

echo "$(date): Starting sync" >> "$LOG"

mkdir -p "$MARMALADE_DIR/archive"

# --- Pull the latest vignettes from the droplet -----------------------------
# Only the newest MAX_VIGNETTES worth of media, not the droplet's whole 6.4GB
# archive. Nothing is written on the droplet: the file list is passed as argv
# to a remote tar, which streams back over the existing ssh connection.

if ! rsync -a -e "ssh $SSH_OPTS" "$DROPLET:$DROPLET_DIR/archive/vignettes.json" \
     "$MARMALADE_DIR/archive/vignettes.json" 2>>"$LOG"; then
  echo "$(date): Could not reach droplet — skipping" >> "$LOG"
  exit 0
fi

FILELIST=$(mktemp /tmp/marmalade-files.XXXXXX)
trap "rm -f $LOCK $FILELIST" EXIT

python3 - "$MARMALADE_DIR/archive/vignettes.json" "$MAX_VIGNETTES" > "$FILELIST" <<'PYEOF'
import json, sys
vigs = json.load(open(sys.argv[1]))
for vig in vigs[-int(sys.argv[2]):]:
    for clip in vig.get('video', []) + vig.get('audio', []):
        f = clip.get('file', '')
        if f:
            print(f)
PYEOF

if [ -s "$FILELIST" ]; then
  # shellcheck disable=SC2046
  ssh $SSH_OPTS "$DROPLET" "tar -C '$DROPLET_DIR' -cf - $(python3 -c "
import shlex,sys
print(' '.join(shlex.quote(l.strip()) for l in open(sys.argv[1]) if l.strip()))
" "$FILELIST") 2>/dev/null" | tar -C "$MARMALADE_DIR" -xf - 2>>"$LOG" || \
    echo "$(date): some vignette media failed to transfer" >> "$LOG"
fi

# Static assets live on the droplet too
rsync -a -e "ssh $SSH_OPTS" "$DROPLET:$DROPLET_DIR/marmalade.gif" "$DROPLET:$DROPLET_DIR/toast.jpg" \
  "$MARMALADE_DIR/" 2>>"$LOG" || true

# Check vignettes exist
VIGNETTES_JSON="$MARMALADE_DIR/archive/vignettes.json"
if [ ! -f "$VIGNETTES_JSON" ]; then
  echo "$(date): No vignettes.json found" >> "$LOG"
  exit 1
fi

# Clean stage — kept on the internal disk: it is transient, and git is much
# happier on a local filesystem than over SMB.
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/vignettes"

# Extract latest N vignettes and copy their assets
cd "$MARMALADE_DIR"
python3 -c "
import json, shutil, os, sys

src = '$MARMALADE_DIR'
dst = '$STAGE_DIR'
max_v = $MAX_VIGNETTES

vigs = json.load(open('$VIGNETTES_JSON'))

# Take latest N that have all files present
selected = []
for vig in reversed(vigs):
    if len(selected) >= max_v:
        break
    # Check all files exist
    all_ok = True
    for clip in vig.get('video', []) + vig.get('audio', []):
        fpath = os.path.join(src, clip.get('file', ''))
        if not os.path.exists(fpath):
            all_ok = False
            break
    if all_ok:
        selected.append(vig)

selected.reverse()  # chronological order
print(f'Selected {len(selected)} vignettes')

# Copy assets and rewrite paths
manifest = []
for vig in selected:
    entry = {
        'name': vig['name'],
        'created': vig['created'],
        'duration': vig.get('duration', 20),
        'video_words': vig.get('video_words', []),
        'audio_words': vig.get('audio_words', []),
        'video': [],
        'audio': [],
    }
    for clip in vig.get('video', []):
        fname = os.path.basename(clip['file'])
        src_path = os.path.join(src, clip['file'])
        dst_path = os.path.join(dst, 'vignettes', fname)
        shutil.copy2(src_path, dst_path)
        entry['video'].append({
            'source': clip.get('source', ''),
            'title': clip.get('title', ''),
            'file': 'vignettes/' + fname,
        })
    for clip in vig.get('audio', []):
        fname = os.path.basename(clip['file'])
        src_path = os.path.join(src, clip['file'])
        dst_path = os.path.join(dst, 'vignettes', fname)
        shutil.copy2(src_path, dst_path)
        entry['audio'].append({
            'source': clip.get('source', ''),
            'title': clip.get('title', ''),
            'file': 'vignettes/' + fname,
        })
    manifest.append(entry)

json.dump(manifest, open(os.path.join(dst, 'manifest.json'), 'w'), indent=2)
print(f'Manifest written with {len(manifest)} vignettes')

# Count total size
total = sum(os.path.getsize(os.path.join(dst, 'vignettes', f))
            for f in os.listdir(os.path.join(dst, 'vignettes')))
print(f'Total assets: {total/1024/1024:.0f} MB')
" 2>&1 | tee -a "$LOG"

# Copy marmalade.gif and toast.jpg if they exist
[ -f "$MARMALADE_DIR/marmalade.gif" ] && cp "$MARMALADE_DIR/marmalade.gif" "$STAGE_DIR/"
[ -f "$MARMALADE_DIR/toast.jpg" ] && cp "$MARMALADE_DIR/toast.jpg" "$STAGE_DIR/"

# Build the static player (written by the heredoc below)
# We generate it separately so it's always fresh
python3 "$LOCAL_REPO/build-player.py" "$STAGE_DIR"

# Git: orphan branch force-push
cd "$STAGE_DIR"
git init -b "$BRANCH"
git add -A
git commit -m "Live update $(date +%Y-%m-%d\ %H:%M) — $(python3 -c "import json; print(len(json.load(open('manifest.json'))))"  ) vignettes"
git remote add origin "https://github.com/$REPO.git"
git push --force origin "$BRANCH" 2>&1 | tee -a "$LOG"

echo "$(date): Sync complete" >> "$LOG"
echo "---" >> "$LOG"

# Cleanup
rm -rf "$STAGE_DIR"
