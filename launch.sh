#!/bin/bash
set -eu
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export AI_USAGE_ROOT="$project_dir"
account_record=""
if [[ "${1:-}" == --account-record && -n "${2:-}" ]]; then
  account_record="$2"
  export AI_USAGE_ACCOUNT_RECORD="$account_record"
fi
if app_pid=$(quickshell ipc -p "$project_dir/ui" call analytics showWindow 2>/dev/null); then
  if [[ -n "$account_record" ]]; then
    quickshell ipc -p "$project_dir/ui" call analytics agent "$account_record"
  fi
  if [[ "$app_pid" =~ ^[0-9]+$ ]] && command -v hyprctl >/dev/null 2>&1; then
    # Reused windows may live on another workspace. Focus this instance only.
    for attempt in {1..20}; do
      if hyprctl dispatch "hl.dsp.focus({ window = \"pid:$app_pid\" })" >/dev/null 2>&1 ||
         hyprctl dispatch focuswindow "pid:$app_pid" >/dev/null 2>&1; then
        break
      fi
      sleep 0.05
    done
  fi
  exit 0
fi
exec quickshell -d -n -p "$project_dir/ui"
