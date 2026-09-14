#!/usr/bin/env python3
"""
Build the static player for GitHub Pages.
Copies the original index.html and patches API calls to read
from the static manifest.json instead of the local server.
"""
import sys
from pathlib import Path

stage_dir = Path(sys.argv[1])
source_dir = Path(__file__).parent

# Read original index.html
html = (source_dir / 'index.html').read_text()

# Patch: Replace /api/next with manifest-based playback
# Replace /api/archive with manifest-based archive
# Replace /api/status, /api/logs, /api/toast with no-ops

patch_js = '''
<script>
// ═══════════════════════════════════════════════════
// STATIC MODE — patches for GitHub Pages deployment
// Loaded manifest replaces all /api/ calls
// ═══════════════════════════════════════════════════
(function() {
  let _manifest = [];
  let _manifestIdx = 0;
  let _manifestLoaded = false;

  // Load manifest on startup
  fetch('manifest.json?' + Date.now())
    .then(r => r.json())
    .then(data => {
      _manifest = data;
      _manifestLoaded = true;
      // Shuffle for variety
      for (let i = _manifest.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [_manifest[i], _manifest[j]] = [_manifest[j], _manifest[i]];
      }
    })
    .catch(() => {});

  // Override fetch to intercept /api/ calls
  const _origFetch = window.fetch;
  window.fetch = function(url, opts) {
    if (typeof url === 'string') {
      if (url.startsWith('/api/next')) {
        return Promise.resolve({
          json: () => {
            if (!_manifestLoaded || _manifest.length === 0) {
              return Promise.resolve({ ready: false, pool_video: 0, pool_audio: 0, need_video: 6, need_audio: 4 });
            }
            const vig = _manifest[_manifestIdx % _manifest.length];
            _manifestIdx++;
            // Reshuffle when we've cycled through all
            if (_manifestIdx >= _manifest.length) {
              _manifestIdx = 0;
              for (let i = _manifest.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                [_manifest[i], _manifest[j]] = [_manifest[j], _manifest[i]];
              }
            }
            return Promise.resolve({ ...vig, ready: true });
          }
        });
      }
      if (url.startsWith('/api/archive')) {
        return Promise.resolve({
          json: () => {
            if (!_manifestLoaded) return Promise.resolve({ items: [] });
            // Return all manifest entries as archive items (chronological)
            const sorted = [..._manifest].sort((a, b) =>
              new Date(a.created) - new Date(b.created)
            );
            return Promise.resolve({ items: sorted });
          }
        });
      }
      if (url.startsWith('/api/status')) {
        return Promise.resolve({
          json: () => Promise.resolve({
            video: 'streaming',
            audio: 'streaming',
            llm: 'connected',
            pool_video: _manifest.length * 6,
            pool_audio: _manifest.length * 4,
            need_video: 6,
            need_audio: 4
          })
        });
      }
      if (url.startsWith('/api/logs')) {
        return Promise.resolve({
          json: () => Promise.resolve({
            logs: ['[static mode] Playing from ' + _manifest.length + ' pre-generated vignettes']
          })
        });
      }
      if (url.startsWith('/api/toast')) {
        return Promise.resolve({
          json: () => Promise.resolve({ current: 'toast.jpg', all: ['toast.jpg'] })
        });
      }
    }
    return _origFetch.apply(this, arguments);
  };
})();
</script>
'''

# Inject the patch script right after <head> so it loads before the main script
html = html.replace('<head>', '<head>' + patch_js, 1)

(stage_dir / 'index.html').write_text(html)
print("Built static player from original index.html (patched API calls)")
