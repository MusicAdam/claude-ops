#!/usr/bin/env bash
# Merge gate: the daemon answers on its port within 5 s of starting, however slow its projects' setup is.
# A scratch OPS_DATA holds one throwaway project whose [worktree] setup and [app] build each take 30 s; the
# daemon must answer /api/state within 5 s, and be done loading (no "starting") within 15 s, as no setup or
# build belongs in startup. Run from the repo root: scripts/startup-smoke.sh
set -u
PY="${PYTHON:-/opt/homebrew/bin/python3}"
here="$(cd "$(dirname "$0")/.." && pwd)"
d="$(mktemp -d)"
port=$((47000 + RANDOM % 800))
repo="$d/smoke-repo"
mkdir -p "$d/data/projects" "$repo"
git -C "$repo" init -q -b main
git -C "$repo" -c user.name=smoke -c user.email=smoke@example.com commit -q --allow-empty -m "Initial commit"
cat > "$d/data/projects/smoke.toml" <<TOML
title = "smoke"
repo = "$repo"
main_branch = "main"
dev_branch = "dev"

[worktree]
setup = ["sleep 30"]

[app]
build = "sleep 30"
launch = "true"
TOML

OPS_DATA="$d/data" OPS_PORT=$port "$PY" "$here/opsd.py" serve > "$d/log" 2>&1 &
pid=$!
fail=""
start=$(date +%s)
answered=""
for _ in $(seq 50); do  # 5 s
  if curl -sf -m 1 -o "$d/state" "http://127.0.0.1:$port/api/state"; then answered=1; break; fi
  sleep 0.1
done
if [ -z "$answered" ]; then
  fail="the daemon did not answer /api/state within 5 s"
else
  ready=""
  while [ $(( $(date +%s) - start )) -lt 15 ]; do
    curl -sf -m 1 -o "$d/state" "http://127.0.0.1:$port/api/state" && ! grep -q '"starting": true' "$d/state" \
      && grep -q '"projects"' "$d/state" && { ready=1; break; }
    sleep 0.2
  done
  [ -n "$ready" ] || fail="the daemon answered but was still starting after 15 s: slow work in startup?"
fi
kill "$pid" 2>/dev/null
wait "$pid" 2>/dev/null
if [ -n "$fail" ]; then
  echo "startup smoke failed: $fail. Daemon log:"
  cat "$d/log"
  rm -rf "$d"
  exit 1
fi
echo "startup smoke ok: answered, and ready in $(( $(date +%s) - start )) s"
rm -rf "$d"
