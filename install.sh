#!/usr/bin/env bash
# Install or update the AI Usage Dashboard from the repository tarball.
#
#   curl -fsSL https://raw.githubusercontent.com/btsouth/agent-pulse/main/install.sh | bash -s -- --with-plugin
#
# Environment overrides for testing or pinning a version:
#   OMARCHY_USAGE_REF=v1.0.0   archive ref (branch or tag), default main
#   OMARCHY_USAGE_TARBALL=...  use a local tarball instead of downloading
set -euo pipefail

ref="${OMARCHY_USAGE_REF:-main}"
url="${OMARCHY_USAGE_TARBALL:-https://github.com/btsouth/agent-pulse/archive/${ref}.tar.gz}"

for command in curl tar python3; do
  command -v "$command" >/dev/null || { echo "install.sh: $command is required" >&2; exit 1; }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [[ -n ${OMARCHY_USAGE_TARBALL:-} ]]; then
  tar -xzf "$url" -C "$work" --strip-components=1
else
  curl -fsSL "$url" | tar -xz -C "$work" --strip-components=1
fi

python3 "$work/install.py" "$@"
