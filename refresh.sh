#!/bin/bash
set -eu
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
force=()
for arg in "$@"; do
  if [[ "$arg" == --force ]]; then force=(--force); fi
done
# Native quota records are maintained by Omarchy; Go is collected below.
# A machine with its own refresh orchestrator (one that routes custom
# collectors and keeps each record written by exactly one writer) takes
# precedence; the packaged updater covers everyone else. The panel passes
# --limits-only, --except, and agent ids through here, so forward the whole
# list: the updater understands all of them, and unknown agent ids simply
# match no collector. The dashboard collector below only knows --force.
if command -v omarchy-agent-usage-refresh >/dev/null 2>&1; then
  timeout 60 omarchy-agent-usage-refresh "$@" || true
elif command -v omarchy-agent-usage-update >/dev/null 2>&1; then
  timeout 60 omarchy-agent-usage-update "$@" || true
fi
# Omarchy's Claude collector just rewrote claude.json without banked limit
# resets. Add them back first: this usually answers from its cache, so the
# panel barely sees the record without them.
python3 "$project_dir/claude_limits.py" "${force[@]}" || true
# Repair the intermittent Codex app-server timeout in Omarchy's collector.
# Also collect purchased ChatGPT credit balances for each configured account.
python3 "$project_dir/codex_limits.py" || true
status=0
python3 "$project_dir/collector.py" scan "${force[@]}" || status=$?
# Reset notifications ride every refresh path, timer and panel alike.
if command -v omarchy-usage-dashboard-notify-resets >/dev/null 2>&1; then
  omarchy-usage-dashboard-notify-resets || true
fi
exit "$status"
