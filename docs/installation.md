# Installation details

The quickest install downloads the current archive and runs the same installer:

```sh
curl -fsSL https://raw.githubusercontent.com/btsouth/agent-pulse/main/install.sh | bash -s -- --with-plugin
```

Set `OMARCHY_USAGE_REF=v1.12.1` on the Bash side of the pipe to pin a release, and `OMARCHY_USAGE_TARBALL=/path/file.tar.gz` installs a local archive. From a checkout, run `python3 install.py --with-plugin` directly; both paths produce the same installation.

The installer runs without sudo. It checks all tracked destinations before changing files, records original files and pending replacements, and writes each replacement atomically. If an install or upgrade is interrupted, rerun it to finish or use `--uninstall` to restore managed originals. This is per-file recovery, not an atomic transaction across all files. It does not modify `/usr/share/omarchy`.

The GitHub repository is `btsouth/agent-pulse`. Installed commands, service names, plugin IDs, and data paths keep their existing names so updates use the same installation and history.

| Item | Default location |
| --- | --- |
| Installed application | `~/.local/share/omarchy-usage-dashboard/app` |
| Launcher | `~/.local/bin/omarchy-usage-dashboard` |
| Refresh wrapper | `~/.local/bin/omarchy-usage-dashboard-refresh` |
| Reset notifier | `~/.local/bin/omarchy-usage-dashboard-notify-resets` |
| Desktop entry | `~/.local/share/applications/omarchy-usage-dashboard.desktop` |
| Optional plugin | `~/.config/omarchy/plugins/community.ai-usage-dashboard` |
| Refresh timer/service | `~/.config/systemd/user/omarchy-usage-dashboard.{timer,service}` |
| Installer registry | `~/.local/state/omarchy-usage-dashboard/installation.json` |
| Metric ledger and quota cache | `~/.local/state/omarchy/ai-usage/` |
| Preferences | `~/.config/omarchy/ai-usage/settings.json` |
| Optional CommandCode API key | `~/.config/omarchy/ai-usage/commandcode.key` |

XDG config, data, and state paths are respected. The launchers remain under `~/.local/bin`, which should be on PATH. Set custom source-home variables such as CODEX_HOME consistently for your shell and systemd user manager if you use them.

`--with-plugin` installs the Agent Pulse widget alongside the built-in Agents widget. To use it as your only AI widget, remove Agents from your bar layout and keep Agent Pulse. This hides the old widget without changing its packaged files. The application menu entry and the widget's Open analytics button open the same dashboard. On uninstall, the widget entry added by this installer is removed by its id, including any settings added to that entry. An entry that existed before installation is preserved. Other shell configuration is preserved.

`--no-systemd` writes files without enabling, stopping, or reloading services. It is intended for staged installs and automated tests. Enable the installed timer manually when staging is complete.

Omarchy's native collectors maintain Codex/Claude quota snapshots. If those are unavailable, local token history still works and the UI shows missing quota information. The dashboard uses existing OpenCode Go credentials only for quota requests to OpenCode's service: the active key in OpenCode 2.x's credential store (`opencode.db`), or `auth.json` from 1.x. CommandCode reads a key you supply, from `~/.config/omarchy/ai-usage/commandcode.key`, the `COMMANDCODE_API_KEY` environment variable, or the Settings field; it is never written back or refreshed, and it stays out of any report the dashboard renders.

Grok quota uses the existing CLI access token without refreshing credentials. Manual Refresh bypasses quota caches; a changed Grok login also invalidates its previous cache. Pi, Oh My Pi, and general OpenCode histories do not supply a shared account quota. Gemini can display an existing Omarchy quota snapshot, but this package does not exchange Gemini OAuth credentials.

## Update notices

Agent Pulse checks the latest published GitHub release once a day during the normal refresh. When a newer version is available, the dashboard and bar panel show a selectable command. Run it in a terminal to install that release. The command includes `--with-plugin` when the widget is installed, and the installer preserves history and preferences.

Turn checking off in **Settings → Updates**, then save preferences. The check sends no usage data to GitHub. If GitHub is unavailable, collection continues and the last successful result stays cached until the next daily attempt. Updates are installed only when you run the command.

Installed copies predating this feature need one manual update before they can show notices. Release maintainers must keep `VERSION` and `plugin/manifest.json` aligned with the release tag.
