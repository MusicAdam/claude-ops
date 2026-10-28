#!/usr/bin/env bash
# Make the README's screenshots and clips (docs/media/) from a made-up sample project, never your own queue.
# A throwaway claude-ops runs in a temp folder with a fake HOME and its own OPS_DATA and port; a stub claude
# (scripts/demo_seed.py) plays canned worker and chat turns, so no model is called. It never reads or writes
# ~/.claude-ops. Needs Playwright for Python (with Chromium) and ffmpeg. Run from anywhere:
#   scripts/demo.sh           # write docs/media/*.png, *.mp4, *.gif, then stop the daemon and remove the temp folder
#   scripts/demo.sh --keep    # the same, then leave the demo running and print its URL (Ctrl-C stops it)
# DEMO_PYTHON picks the Python that has Playwright; PYTHON the one (3.11+) that runs opsd.py.
set -u
here="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$([ -x /opt/homebrew/bin/python3 ] && echo /opt/homebrew/bin/python3 || command -v python3)}"
venv="${XDG_CACHE_HOME:-$HOME/.cache}/claude-ops-demo/venv"
cap=""
for c in ${DEMO_PYTHON:-} "$venv/bin/python" "$PY"; do
  if [ -x "$c" ] && "$c" -c "import playwright.sync_api" 2>/dev/null; then cap="$c"; break; fi
done
if [ -z "$cap" ]; then
  echo "scripts/demo.sh needs Playwright for Python. Install it in its own venv (opsd.py never uses it):" >&2
  echo "  $PY -m venv '$venv' && '$venv/bin/pip' install playwright && '$venv/bin/python' -m playwright install chromium" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "scripts/demo.sh needs ffmpeg (and ffprobe) to encode the clips: brew install ffmpeg" >&2
  exit 1
fi
if ! "$cap" -c "from playwright.sync_api import sync_playwright as s
with s() as p: p.chromium.launch().close()" 2>/dev/null; then
  echo "Playwright has no Chromium to run: $cap -m playwright install chromium" >&2
  exit 1
fi
exec "$cap" "$here/scripts/demo_capture.py" --daemon-python "$PY" "$@"
