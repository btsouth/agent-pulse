# Contributing

Keep parser changes covered by small synthetic fixtures. Do not attach real transcripts, credentials, project paths, or unreviewed usage dumps to issues or pull requests.

Run `python3 -m unittest discover -s tests -v` and the syntax checks in the CI workflow. For QML changes, run `python3 demo.py` and check a light and dark Omarchy theme, narrow windows, tooltips, filters, and keyboard navigation. Run `python3 tools/check_ui.py` for an isolated account-editor save/reload check. Use `omarchy plugin validate ./plugin` on a compatible Omarchy installation.

The bar plugin is derived from Omarchy's Agents plugin. Preserve attribution and review upstream API changes before updating copied components. Pricing updates must use an explicit source revision and retain source attribution. Missing prices must remain visibly unavailable. Refresh rates with `python3 tools/update_catalog.py <commit sha>` and `python3 tools/update_go_pricing.py --write`, then run `python3 tools/pricing_gaps.py`: it names every recorded model with no applicable rate, the table that owns its route, and the step that refreshes it.

Keep changes focused and describe what users will notice and how it was checked.

Collection tests must set HOME, XDG paths, CODEX_HOME, CLAUDE_CONFIG_DIR, GROK_HOME, PI_CODING_AGENT_DIR, MUSE_HOME, and CURSOR_HOME to temporary directories. Never run an unsandboxed scan for testing. Provider calls should be mocked. `report` is read-only; `scan` writes the ledger.

Use `python3 demo.py --theme light --capture /tmp/usage-light.png` and `python3 demo.py --capture /tmp/usage-dark.png` for synthetic visual checks. Quickshell checks run locally and are not included in the Ubuntu CI job.
