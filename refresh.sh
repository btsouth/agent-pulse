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

if [[ "$limits_only" == true && "${#agent_ids[@]}" -gt 0 ]]; then
  run_claude=false
  run_codex=false
  for id in "${agent_ids[@]}"; do
    if [[ "$id" == claude ]]; then run_claude=true; fi
    case "$id" in codex|codex-*) run_codex=true ;; esac
  done
  # A targeted limits refresh skips the history scan. The panel uses this
  # path to keep Claude limits current between full refreshes.
  if [[ "$run_claude" == true ]]; then
    if [[ "${#force[@]}" -gt 0 ]]; then
      python3 "$project_dir/claude_limits.py" "${force[@]}" || true
    else
      python3 "$project_dir/claude_limits.py" || true
    fi
  fi
  if [[ "$run_codex" == true ]]; then
    # Repair the intermittent Codex app-server timeout in Omarchy's collector.
    python3 "$project_dir/codex_limits.py" || true
  fi
  status=0
else
  # Omarchy's Claude collector just rewrote claude.json without banked limit
  # resets. Add them back first: this usually answers from its cache, so the
  # panel barely sees the record without them.
  if [[ "${#force[@]}" -gt 0 ]]; then
    python3 "$project_dir/claude_limits.py" "${force[@]}" || true
  else
    python3 "$project_dir/claude_limits.py" || true
  fi
  # Repair the intermittent Codex app-server timeout in Omarchy's collector.
  # Also collect purchased ChatGPT credit balances for each configured account.
  python3 "$project_dir/codex_limits.py" || true
  status=0
  if [[ "${#force[@]}" -gt 0 ]]; then
    python3 "$project_dir/collector.py" scan "${force[@]}" || status=$?
  else
    python3 "$project_dir/collector.py" scan || status=$?
  fi
fi
# Reset notifications ride every refresh path, timer and panel alike.
if command -v omarchy-usage-dashboard-notify-resets >/dev/null 2>&1; then
  omarchy-usage-dashboard-notify-resets || true
fi
exit "$status"
