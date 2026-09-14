#!/bin/bash
# Sync latest vignettes to GitHub Pages (jontoews.com/marmalade)
# Uses orphan branch force-push — no history bloat
set -e

# moved 2026-07-30 from ~/Documents/Marmalade during the Documents cleanup
MARMALADE_DIR="/Users/jontoewsinterceptgroup.com/Creative-Projects/marmalade/source"
STAGE_DIR="/tmp/marmalade-stage"
REPO="swoonjet/marmalade"
BRANCH="gh-pages"
MAX_VIGNETTES=25
LOG="$MARMALADE_DIR/sync.log"
LOCK="$MARMALADE_DIR/.syncing"

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

# Check vignettes exist
VIGNETTES_JSON="$MARMALADE_DIR/archive/vignettes.json"
if [ ! -f "$VIGNETTES_JSON" ]; then
  echo "$(date): No vignettes.json found" >> "$LOG"
  exit 1
fi

# Clean stage
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
python3 "$MARMALADE_DIR/build-player.py" "$STAGE_DIR"

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
