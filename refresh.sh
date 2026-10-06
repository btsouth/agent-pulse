#!/bin/bash
set -eu
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
force=()
limits_only=false
agent_ids=()
skip_next=false
for arg in "$@"; do
  if [[ "$skip_next" == true ]]; then
    skip_next=false
    continue
  fi
  case "$arg" in
    --force) force=(--force) ;;
    --limits-only) limits_only=true ;;
    --except) skip_next=true ;;
    -*) ;;
    *) agent_ids+=("$arg") ;;
  esac
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

# A targeted limits refresh (the panel's Claude timer, or a retry for one
# agent) skips the history scan and the helpers for agents it did not name.
targeted=false
if [[ "$limits_only" == true && ${#agent_ids[@]} -gt 0 ]]; then targeted=true; fi
wants() {
  [[ $targeted == false ]] && return 0
  local id
  for id in "${agent_ids[@]}"; do [[ $id == "$1" || $id == "$1"-* ]] && return 0; done
  return 1
}
# Omarchy's Claude collector just rewrote claude.json without banked limit
# resets. Add them back first: this usually answers from its cache, so the
# panel barely sees the record without them.
if wants claude; then python3 "$project_dir/claude_limits.py" "${force[@]}" || true; fi
# Repair the intermittent Codex app-server timeout in Omarchy's collector.
# Also collect purchased ChatGPT credit balances for each configured account.
if wants codex; then python3 "$project_dir/codex_limits.py" || true; fi
status=0
if [[ $targeted == false ]]; then python3 "$project_dir/collector.py" scan "${force[@]}" || status=$?; fi
# Reset notifications ride every refresh path, timer and panel alike.
if command -v omarchy-usage-dashboard-notify-resets >/dev/null 2>&1; then
  omarchy-usage-dashboard-notify-resets || true
fi
exit "$status"
