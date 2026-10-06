# Accounts and history folders

Open Settings, name the default local history group if needed, then choose **Add account**. Give it a unique name and add one or more source folders. Save preferences to scan them.

| Source | Folder to select |
| --- | --- |
| Codex | Agent home containing `sessions` or `archived_sessions` |
| Claude Code | Agent home containing `projects` |
| Grok Build | Agent home containing `sessions` |
| Gemini CLI | Agent home containing `tmp` |
| OpenCode / OpenCode Go | Data folder containing `opencode.db` or `storage/message` |
| Pi / Oh My Pi | Agent folder containing `sessions` |
| Muse | Data home containing `sessions` |
| Hermes | Hermes home containing `state.db` (add one entry per route: OpenCode Go, Ollama Cloud, CommandCode, and ClinePass) |
| Cursor | Cloud usage only; sign in to the Cursor desktop app (no folders to add) |

An account can combine Codex, Claude, and other source folders. OpenCode and OpenCode Go need separate source entries to label both sets of routes in the same database. Folders must already be available locally or mounted. No remote sync or credentials are configured here. Missing folders appear in source coverage and can be connected later.

A Hermes home (`~/.hermes`) carries every route Hermes bills. Choose the route it belongs to (OpenCode Go, Ollama Cloud, CommandCode, or ClinePass) when you add the folder, and add further entries for the other routes if you want them labelled; the folder itself holds one database. Put it under the account whose history it belongs to, so the two clients' usage for that account compares on one card instead of splitting across two.

Choose **Account** at the top of the dashboard. Each entry identifies one source and one account, including the main login, named folders, separate T3 Code login homes, legacy additional homes, and linked agent records. Accounts stay selectable when they have no activity in the current period. Existing named folders take precedence over automatic discovery, so adding the same home through T3 does not create another account. T3 runtimes routed to API services remain clients of that service's main account unless you explicitly assign their folders to a named account.

Choose **Period** beside the account. Its totals, charts, and model breakdowns all use that scope. The account detail shows its own limits and tokens per model immediately. **View usage** beside a limit selects **Since reset** for that window. It starts at the reported reset timestamp minus the window duration, including a partial first day; monthly windows use the previous calendar month. Expired limits, unknown durations, and model-only limits do not offer a reset period. Missing reset information never turns into a guessed date range.

Switching accounts clears model, project, day, hour, and source exclusions and returns to the date range selected before a reset period. **Clear filters** clears drill-downs while keeping the account and period. The secondary source selector and source exclusions apply in the All accounts view. Analytics opened from the bar follows its selected account. Renaming a named account changes its label without reimporting tokens.

## Copies and retained history

Stable event IDs deduplicate copied sessions across folders. Multiple copies under the same account count once. A named copy takes precedence over an unlabelled copy. If copies of an event belong to different named accounts, the event counts once under **Needs review**, with a warning. Move the mirrored folders into the same account to resolve it. For nested configured folders, the most specific folder wins.

The ledger retains previously recorded usage after source files disappear. Older records whose source cannot be recovered during migration appear as **Unassigned history**. They remain in overall totals. Removing an account label keeps its history; its known source paths follow any remaining named or discovered home, or return to the local group. Legacy additional folders appear as separate accounts named after their folder. To combine mirrored folders or choose a clearer label, add them to one named account.

## Synced machines

Under **Settings → Synced machines**, point every machine at the same folder (Syncthing, Dropbox, a network share) and give each one a device id, for example `desktop` and `laptop`. Each machine writes a consistent snapshot of its ledger to `<device>.sqlite` in that folder and imports the other snapshots during its normal scan. Events keep their stable ids, so a history indexed on more than one machine counts once, and local provenance wins over an imported copy. Imported activity appears under a `machine:<device>` account that you can filter, price, or ignore like any other account.

Snapshots carry token counters, model and project names, session ids, and timestamps, the same fields the local ledger stores. They do not carry prompts, response bodies, credentials, or file contents. Quota stays tied to the current login on each machine; imported accounts do not show limits. Snapshots are written with SQLite's `VACUUM INTO`, so a sync client never copies a half-written database. Remove the folder setting to stop syncing; imported events remain in the ledger.

## Limits and comparisons

History labels are not verified login identities. The main account keeps its current-login quota when selected. A named account shows limits when an agent usage record under `~/.local/state/omarchy/agents/usage/` has the same record id as the account, a provider-qualified id such as `codex:work`, or an unambiguous name matching the account label. The account detail states when quota or token history is unavailable. A separate agent record without a linked folder shows its limits and asks you to link history in Settings, rather than showing another login's tokens. Accounts without their own quota record never inherit the main login's limits. This version does not switch authentication or fetch every account's limits. When a recent shared Claude reading exists in `~/.cache/claude-usage/`, for example from a T3 Code build that shares its usage poll, Agent Pulse uses it for banked resets instead of asking again.

Optional monthly prices can be set per provider (the local history group) or per labelled account. Imported, conflicting, and unassigned histories never inherit the local price. API estimates still use the same recorded costs and catalog rules in every account view.

Averages divide period activity by distinct recorded sessions. Sessions are not completed tasks, and differently sized tasks cannot establish relative model efficiency. API-value shares and averages include only priced usage; unknown rates remain visibly unpriced.
