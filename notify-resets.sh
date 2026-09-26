#!/bin/bash

# Notify on weekly/monthly limit resets in the agent usage records.
#
# Ships with the dashboard and rides every refresh path: the systemd service
# runs it after each successful scan, and refresh.sh runs it after a panel
# refresh. It compares the records in the usage dir against a snapshot from
# the previous run and sends a desktop notification when either:
#   - a limit's resetsAt jumps forward by more than an hour (scheduled weekly
#     reset, or a surprise reset that re-anchors the window), or
#   - a provider's banked reset credits (resetCreditsAvailable) increase,
#     which is how Codex/Grok deliver dropped resets.
# Short windows notify only when they were nearly spent: a label shaped in
# minutes or hours ("5h window", "30m window", "5 hours") or named a session
# ("Session (5-hour)") alerts on reset only if it stood at 90% or more.
# Those windows can drop out of a record, or lose their reset time, until
# the next use opens a new one, so either of those after the old reset time
# counts as a reset too.
#
# Alerts stay limited to the providers passed here -- codex and claude by
# default. Adding a provider to the usage dashboard must not silently opt it
# into notifications; pass --provider <id> to watch more.

set -uo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/agents"
USAGE_DIR="$STATE_DIR/usage"
# The timer's post-scan run and a panel-triggered refresh can overlap; the
# second one out skips instead of double-notifying. Same convention as the
# collector's lock, non-blocking so the loser exits clean.
LOCK="$STATE_DIR/reset-snapshot.lock"
mkdir -p "$STATE_DIR"
exec 9>"$LOCK"
flock -n 9 || exit 0
SNAP="$STATE_DIR/reset-snapshot.json"

PROVIDERS=(codex claude)
while [[ $# -gt 0 ]]; do
  case "$1" in
  --provider)
    [[ -n ${2:-} ]] || { echo "omarchy-usage-notify-resets: --provider needs an id" >&2; exit 2; }
    PROVIDERS+=("$2")
    shift 2
    ;;
  *)
    echo "omarchy-usage-notify-resets: unknown argument: $1" >&2
    exit 2
    ;;
  esac
done

shopt -s nullglob
files=()
for agent in "${PROVIDERS[@]}"; do
  [[ ! -f "$USAGE_DIR/$agent.json" ]] || files+=("$USAGE_DIR/$agent.json")
done
((${#files[@]})) || exit 0

# Timestamps are normalized here (fractional seconds stripped, +00:00 -> Z) so
# snapshot comparisons are jitter-free and fromdate can parse them.
current=$(jq -s '
  def stamp: if type == "string" then sub("[.][0-9]+"; "") | sub("\\+00:00$"; "Z") else . end;
  {
    ids: [ .[].id ],
    limits: ([ .[]
      | .id as $id | .name as $name
      | (.limits // [])[]
      | select(.label)
      | (.label | test("session|\\b[0-9]+\\s*-?\\s*h(our)?s?\\b|\\b[0-9]+\\s*-?\\s*m(in(ute)?s?)?\\b"; "i")) as $short
      | select($short or .resetsAt)
      | { key: ($id + "|" + .label),
          value: ({
            id: $id,
            name: $name,
            label: .label,
            percent: (.percent // 0),
            resetsAt: (.resetsAt | stamp)
          } + if $short then { short: true } else {} end) }
    ] | from_entries),
    credits: ([ .[]
      | select(.resetCreditsAvailable != null)
      | { key: .id, value: { id: .id, name: .name, credits: .resetCreditsAvailable } }
    ] | from_entries)
  }' "${files[@]}") || exit 0

# The popup carries the provider's own mark. The installed plugin ships
# assets for the dashboard's providers; the shell's first-party agents plugin
# covers the rest. A provider found in neither goes out without an image.
icon_for() {
  local id="$1" dir
  for dir in "$HOME/.config/omarchy/plugins/community.ai-usage-dashboard/assets" \
    "${OMARCHY_PATH:-/usr/share/omarchy}/shell/plugins/agents/assets"; do
    [[ -f "$dir/$id.svg" ]] && { printf '%s\n' "$dir/$id.svg"; return 0; }
  done
  return 1
}

if [[ -s $SNAP ]]; then
  events=$(jq -rn --slurpfile snapw "$SNAP" --argjson new "$current" '
    def epoch: try fromdate catch null;
    ($snapw[0] // { limits: {}, credits: {} }) as $old
    | ([ $new.limits | to_entries[]
        | . as $e
        | ($old.limits[$e.key] // null) as $prev
        | select($prev != null and $e.value.short != true)
        | (($e.value.resetsAt | epoch) // empty) as $newT
        | (($prev.resetsAt | epoch) // empty) as $oldT
        | select($newT - $oldT > 3600)
        | { id: $e.value.id, name: $e.value.name,
            line: "\($e.value.label): was \(($prev.percent * 100) | round)%, now \(($e.value.percent * 100) | round)%" }
      ]) as $limitEvents
    | ([ $old.limits | to_entries[]
        | select(.value.short == true and .value.percent >= 0.9)
        | . as $p
        | select($new.ids | index($p.value.id))
        | (($p.value.resetsAt | epoch) // empty) as $oldT
        | ($new.limits[$p.key] // null) as $cur
        | (if $cur == null then null else ($cur.resetsAt | epoch) end) as $newT
        | select(($newT != null and $newT - $oldT > 3600)
            or (now >= $oldT - 300 and ($newT == null or $cur.percent < $p.value.percent)))
        | { id: $p.value.id, name: ($cur.name // $p.value.name),
            line: "\($p.value.label): was \(($p.value.percent * 100) | round)%, now \((($cur.percent // 0) * 100) | round)%" }
      ]) as $shortEvents
    | ([ $new.credits | to_entries[]
        | . as $e
        | ($old.credits[$e.key] // null) as $prev
        | select($prev != null and $e.value.credits > $prev.credits)
        | { id: $e.value.id, name: $e.value.name,
            line: "Banked resets: \($prev.credits) -> \($e.value.credits)" }
      ]) as $creditEvents
    | ($limitEvents + $shortEvents + $creditEvents)
    | group_by(.id)
    | map({ id: .[0].id, name: .[0].name, body: (map(.line) | join("\n")) })
    | .[] | [.id, .name, .body] | @tsv
  ' 2>/dev/null)

  while IFS=$'\t' read -r id name body; do
    [[ -n $id ]] || continue
    args=(-a "AI Usage" -u normal)
    if icon=$(icon_for "$id"); then
      # file:// makes the shell's daemon ingest the svg as the popup image;
      # a bare path lands in appIcon and renders nothing.
      args+=(-i "file://$icon")
    fi
    notify-send "${args[@]}" "$name limit reset" "$(printf '%b' "$body")" || true
  done <<<"$events"
fi

tmp=$(mktemp "$STATE_DIR/.reset-snapshot.XXXXXX") || exit 0
printf '%s\n' "$current" >"$tmp"
chmod 600 "$tmp"
mv "$tmp" "$SNAP"
