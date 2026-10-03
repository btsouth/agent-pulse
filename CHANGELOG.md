# Changelog

## 1.10.0 - 2026-10-03

- Show a daily GitHub release notice in the dashboard and bar panel with a command to update to that release, preserving history, preferences and the installed widget. Disable checks in Settings → Updates.

## 1.9.0 - 2026-09-30

- Price every model released since the last snapshot. The bundled LiteLLM catalog is refreshed to its current revision, which adds Claude Sonnet 5.5, Claude Opus 5.5, GPT-6 Sol, Luna, and 6.1 Sol, and the models the Gemini and image routes have gained. A refresh keeps an entry that upstream has since dropped, because a dropped rate would turn already recorded usage into unpriced tokens long after the fact; `tools/update_catalog.py` now prints what it added and which base rates changed, and takes `--drop-removed` to mirror upstream exactly.
- Refresh the OpenCode Go table from its own documentation: add MiMo V2.6 Flash and Pro, Grok 4.7, and GPT-6 Luna, and correct DeepSeek V4.1 Flash's monthly allowance, which the docs now put at $60 rather than $15.
- Price ClinePass rows whose model id carries the model vendor's prefix rather than Cline's (`deepseek/deepseek-v4.1-flash`). A row whose id lost the prefix entirely stays unpriced, as before.
- Add `tools/pricing_gaps.py`, which lists every model in the ledger with no applicable rate, names the table that owns its route and the step that refreshes it, and exits non-zero so a timer can report a new model instead of a person finding one on the dashboard. It reports only the sources the dashboard counts unless `--all-providers` asks for the whole ledger.
- Price DeepSeek V4.1 Flash Fast on the CommandCode route from the rates its own table publishes: $0.16 input, $0.58 output, and $0.016 cache read per million, doubling in the same 01:00-04:00 and 06:00-10:00 UTC weekday peak bands as the other DeepSeek entries. Records for that model were reported as unpriced, which understated the route's API value.
- Remove the unlabelled limit percentage from the Accounts rows on the overview. It did not say which window the figure belonged to, and it sat beside two other numbers with different meanings. Each account's own section and its limit cards still show every limit with its window name.
- Align the Breakdown column headers with the columns they label. The header row packed at the layout's default spacing while the rows packed at 12, so TOKENS, API VALUE, and CACHE READ drifted right of the figures under them, worst at the leftmost column.
- Fold any dashboard overview section to its header. Totals, Pinned limits, Tokens by hour, Accounts, the selected account's limits and models, Cache and output, Breakdown, and the yearly activity map each collapse to a single line that keeps its headline figure, and the choice survives reopening the window. Folding is a view preference: it is remembered in `view-state.json` beside the other analytics preferences, and the bar panel is unaffected. `demo.py --view collapsed` renders the folded view.

- Fix the bar panel saying "No indexed token history for this source" for labelled second logins such as Claude Second, Claude Third, and ChatGPT Second. Their history was indexed all along, but the hourly snapshot only knew providers. It now keeps totals for each source in the panel, so those sources show their own hours and tokens today, and Main no longer includes its second logins' usage. All sources still counts every login once.
- Rewrite the panel's message for a source with no indexed history. It now says why: the source keeps no local logs, or it has not been indexed yet and its history folder can be added in Settings. It shows the token count from the source's own usage record when there is one. A source with history but nothing today reads "No tokens processed today", and All sources names the sources without hourly history instead of counting them.

## 1.8.2 - 2026-09-29

- Replace the fast API-key fingerprints in Ollama Cloud, CommandCode, and ClinePass quota caches with salted PBKDF2 fingerprints. Existing caches refresh once after upgrading; key changes still refresh immediately.
## 1.8.1 - 2026-09-29

- Price Codex GPT-6.1 Sol using OpenAI's published rates, including cached input, cache writes, and the long-context tier above 272K input tokens.
- Show the top eight models in Breakdown by default, with a row count and controls to show more or collapse the list. Switching back to Models restores the compact view.
- Show the tokens recorded on this PC since each limit window began, next to its reset countdown in the bar panel and on the dashboard's source cards (`Resets in 1d 21h · 6.6B tokens on this PC`). Session, weekly, and monthly windows are counted from the reset time back by the window's length, and monthly windows go back one calendar month. A window with no reset time, a label that does not give its length, or a scope limited to one model shows no count. The count covers the account the limit belongs to, so a labelled second login counts only its own history. Usage on other machines or on the web is not included, and cache reads count in full even though providers weight them lower, so the count tracks volume, not the percentage.
- Notify when a session limit, such as Claude's 5-hour window, resets after reaching 90% or more. Sessions below 90% still reset quietly, and a session that leaves the usage record or loses its reset time after resetting still counts.

## 1.8.0 - 2026-09-26

- Agent Pulse adds a live Today counter from recorded local usage, hourly token history, a clearer source picker, reset-aware limits to watch, and up to three pinned limits. The dashboard brings the key totals and controls forward and keeps the full model, route, session, account, pricing, and coverage views.
- Update the dashboard in place when live history, the five-minute timer, or Refresh brings new usage. The page no longer dims and locks on every background scan. Only a view change (period, filter, drill-in, source) holds the page, and one that answers within a quarter second shows no dimming.
- Restore the bar panel's top-model breakdown across all routes. Returning to All sources in analytics now includes every source again; focusing an excluded source includes that source while leaving other exclusions in place.
- Show SOURCE inside the bar panel's picker and mark the selected menu row without increasing the trigger height. Keep the compact Main and Second labels.
- Put the panel source picker first, beside Analytics, so source focus stays visible above the totals and pinned limits.
- Make the panel scrollbar drag the content, and widen its invisible drag target while keeping the narrow thumb.
- Preserve existing settings, saved history, installed bar entries, and rollback records when upgrading from 1.7.0.
- Price direct Claude Opus 5.5 and Codex GPT-6 Sol and Luna at their published rates, including cache and long-context pricing. Add CommandCode's published rates for those models and MiMo V2.6.
- Price ClinePass rows whose wire provider says `cline-pass` through the ClinePass table. Previously those rows appeared unpriced despite a matching published rate.

## 1.7.0 - 2026-09-22

- Show Claude's banked usage-limit resets on the Claude card, with the date an unspent reset expires. Anthropic's usage endpoint only returns them to a current Claude Code CLI, so the refresh identifies as the installed version, found on PATH or where Claude Code's installers put it. Answers are cached for five minutes, a rate-limited read waits out the endpoint's retry-after, and a failed read keeps the last answer for up to 30 minutes before the count becomes unknown. The panel holds the count while Omarchy's collector rewrites the record mid-refresh, so the line no longer blinks out when the panel opens. The Claude chip is labelled "Claude".
- Show when Codex banked resets expire, from the earliest live credit's `expires_at` on the same credit endpoint the count comes from.
- Restore the provider marks for Codex Second, CommandCode, ClinePass, Ollama Cloud, OpenCode Go, Grok, and Cursor. They were left behind when the private `bts.agents` clone was folded into this widget, so those tabs showed the generic glyph.
- Refresh opt-in Codex banked-reset counts with the auth file belonging to each card's configured home. A spent credit on a second account no longer inherits Main's count; if the credit endpoint cannot be read, the count becomes unknown instead of displaying a stale positive number.
- Recover Codex limits when Omarchy's app-server reader intermittently times out on `account/read` or `account/rateLimits/read`. The dashboard refresh reads the RPC stream without text buffering and repairs affected records, including named Codex accounts with one configured home folder. If another collector writes a timed-out record between refreshes, the bar keeps the last known limits with a stale label and requests an early refresh.

## 1.6.0 - 2026-09-22

- Show CommandCode's extra credits, the top-up wallet spent once the plan's own credits run out, as a money balance on the provider card and in the bar panel. The wallet comes from the billing response the plan windows are already read from (`purchasedCredits`); what is left is the endpoint's own figure, and the amount it is measured against is that plus what the period has drawn from the wallet, so the derived figure is labelled an estimate. A wallet with nothing in it, or below the cent the card prints, stays off the card, and one left over from a plan with no windows still shows. A record's balance now carries the vendor's own name for the wallet ("Extra credits"), which the panel shows in place of the generic label, and the dashboard card gained the same balance line and meter.
- Count vendor-prefixed `muse-spark-*` records with the same bare Muse model in model totals and filters, including the bar panel's all-route model list, while retaining their OpenCode Go, CommandCode, ClinePass, or Ollama Cloud route attribution.
- Discover T3 Code's isolated provider instances and attribute CommandCode Codex/Claude runtimes to the CommandCode card. Count FlashX with its published rates, and route T3 OpenCode instances for Ollama Cloud and ClinePass to their own cards.
- Make reports read-only. Collect on open, Refresh, settings save, and the timer instead of scanning whenever a filter changes. Keep demo and test source paths isolated.
- Fix Cursor fallback pagination, capped-history resume, forced refresh, and stale error status. Commit events before advancing the history watermark, and rebuild old watermarks once to recover truncated pulls. Missing event prices stay unpriced.
- Keep Go allowance estimates within the selected account and drilldown. Prevent unlisted resale models from falling back to another provider's prices.
- Keep raw OpenCode Go errors out of quota caches, and reject path separators in ledger device ids.
- Make interrupted upgrades recoverable, quote the notifier's systemd path, and remove temporary archive files after installation.
- Correct Cursor support, uninstall, release-pinning, and privacy documentation. Add regression and installed-runtime checks.

## 1.5.0 - 2026-09-19

- Cursor usage: cloud usage events, per model, carrying the input, output, and cache-read tokens each one spent and its list-price cost, plus a billing-cycle quota meter with its reset date. Cursor is the first source here that cannot be rebuilt from local files, so it is read from Cursor's own API using the session the desktop app already stores locally; that token is never copied, logged, or sent anywhere but Cursor. Event costs are list prices and plan discounts are not applied per event, so the quota card's billed total is the figure to trust where the two disagree.
- Check for a clashing built-in widget off the machine's own Omarchy path. The installer read `/usr/share/omarchy` directly — a path that exists on the machine this was developed on and nowhere else — so the check passed there and went red the moment it ran anywhere else. It now follows the `OMARCHY_PATH` convention `refresh.sh` already uses, and its test stages a fixture manifest instead of reaching for the host's.
- Say in the README what the project counts, and that Fireworks starts off, so the opening paragraph matches what the widget actually does.

## 1.4.0 - 2026-09-19

- Count Command Code's own sessions. The CommandCode route used to know only what the Hermes ledger recorded, so work driven through the Command Code CLI — whatever model it ran — never reached the dashboard, and the model list showed the tail of last night's Hermes sessions long after the day had moved on. The collector now reads Command Code's session transcripts the way it reads Claude Code's, per message with its own usage, skips the checkpoints mirrors, and prices the models through the same CommandCode rate table, so the provider card carries the whole account: Hermes routes, the CLI, and the agents that run through it.
- Make the packaged widget the single, complete one. It carries the customizations the private `bts.agents` clone used to hold in code, moved into settings any install can set from its `bar.layout` entry: **providerOrder** walks the bar and panel in the order you list, **launchCommands** maps a provider to the CLI its right-click launches, **extraProviders** admits a record your own collector writes into the usage directory, and a provider set to `alwaysShow` stays on the bar before it has numbers. Fireworks keeps its place from the built-in widget, off by default. The installer names any other installed model-usage widget it finds, including the built-in Agents widget, so a second AI icon never appears by surprise. The CommandCode and ClinePass chips label themselves by name.
- Notify when a weekly or monthly limit resets. The notifier compares each refresh against a snapshot of the last one and speaks up when a long window's reset jumps forward or banked reset credits arrive — the way Codex delivers dropped resets — with the provider's own mark on the popup and short windows (minutes- or hours-shaped labels, session names) never mentioned. It rides every refresh path, runs after the timer's scan and after a panel refresh, and watches only the providers passed to it: codex and claude by default, more by `--provider`, so a provider added to the dashboard never silently opts itself into alerts. Overlapping runs serialize on a lock file so a reset notifies once.
- Prefer a machine's own refresh orchestrator over the packaged updater when one is installed, falling back to the updater everywhere else. A machine whose orchestrator routes records its own collectors also write keeps whichever writer ran last for those records.

- Leave a source out of the view by switching its chip off in the source row. Its history leaves the summary, the chart, the cards, the Go allowance card, and every breakdown at once, and the chip goes struck through with the filter line naming it, so a view never quietly hides something. **Overview** brings every source back. It is a view filter: Settings still decides what this machine collects, so nothing stops being counted. The source chips are a filter now rather than a focus control, and a source that a row had drilled into keeps its highlight until the filter is cleared.

- Keep the summary card's session line inside its border. It was one unwrapped line, so a history long enough to add "· partial value" drew past the card's right edge and into the gap before the chart card. It wraps now, and the card takes its height from the column inside it, so the extra line cannot push the rest of the text below the bottom edge either.

- Keep the bar panel's provider row on one line as providers are added. The row shares the width evenly, so a fixed panel width squeezes it once a sixth provider appears and the labels clip, because those chips do not elide. The panel now asks for the width the chip row actually needs (measured against the real Button and Style tokens: 425 for five chips, 511 for six) and never exceeds the screen, so a new provider needs no width change by hand.

- Filter the whole page by model, so the summary cards, the chart, the provider cards, and every breakdown describe one model across all the routes that served it: its cached and uncached input, its output, its cache savings, and its API value, all its own. Pick one from the row under the accounts, or open a Models-table row, which also jumps to that model's sessions. Changing the period keeps the filter, so one model over 30 days is a single extra click.
- Show the period comparison under a filter instead of the words "Filtered activity", since both periods are narrowed the same way and the figure is valid, and say plainly when a filter matches nothing rather than suggesting a history folder is missing.

- Count a model once in the Models breakdown, however many routes served it. The four routes that carry DeepSeek V4.1 Flash each record a different model string, so the table used to show one entry per route with two of them reading identically. A row now answers one question, with the split by route and the string each route recorded in its detail, and prices are unchanged: each route's tokens are priced at that route's own rates before they are added up.
- Follow that grouping when a model is selected, so choosing the row, or asking a report for any of the spellings it covers, shows the whole model rather than one route's share. Pricing coverage still names the route and the exact recorded string, which is what identifies a rate table that has not caught up.
- ClinePass usage: the plan's three windows, a rolling five hours, the calendar week, and the calendar month, read from an API key you supply, each with the reset time the endpoint publishes, so these meters carry a countdown. Add the key under **Settings**, or export `CLINE_API_KEY`, or drop it in `~/.config/omarchy/ai-usage/clinepass.key`.
- Count ClinePass token history through the same agent ledger as the other routes Hermes bills, since the subscription keeps no local history of its own.
- ClinePass reference rates, transcribed from the rates its own documentation publishes for a flat-rate subscription, with the published peak window for the DeepSeek models and DeepSeek's Flash rate for `cline-pass/deepseek-v4.1-flash`, which Cline's own model table omits.

- Fix saving preferences: the save waited for its input stream to close, which never happens when the dashboard hands the payload over, so the first click left the button disabled for the rest of the window's life and wrote nothing. The save now finishes as soon as the payload arrives, it still reads a payload a scripted caller writes a moment later, a save that receives no payload reports the failure instead of closing the window as if it had worked, and a save that never returns is asked to stop after fifteen seconds and then killed, so the button cannot stay dead.
- Refuse a price or an opacity that is not a number with a fixed sentence instead of repeating the value, so a key pasted into the wrong field cannot come back in the message the window shows, and answer a payload that is not a JSON object with that same channel instead of a traceback.

## 1.3.0 - 2026-09-17

- CommandCode usage: plan windows read from an API key you supply, with the resets they publish. Its allowance is measured in credit value rather than tokens (GOAT allows $14 in any 5 hours, $35 in any 7 days, and $70 a month), and each window reports its own spend, cap, and reset time, so these meters carry a countdown where the other providers cannot. Add the key under **Settings**, or export `COMMANDCODE_API_KEY`, or drop it in `~/.config/omarchy/ai-usage/commandcode.key`.
- Count CommandCode token history through the same agent ledger as OpenCode Go and Ollama Cloud. Its two profiles for one account, one OpenAI-shaped and one Anthropic-shaped, collapse onto one card; the raw route is kept in the Routes breakdown.
- CommandCode model rates, transcribed from its own resale table rather than the labs' list prices, with its published peak window for the DeepSeek models. Free-while-capacity-lasts models price at zero instead of staying unpriced.
- One row of provider tabs: the chips share the width evenly and keep their own text width, so four or five providers sit on one line instead of wrapping, and the panel has more room on both sides.

## 1.2.0 - 2026-09-17

- Ollama Cloud usage: local token history alongside the plan's usage limits, read from an API key you supply. Add it under **Settings**, or export `OLLAMA_API_KEY`, or drop a key in `~/.config/omarchy/ai-usage/ollama.key`. The key is never written back, never leaves the request it authenticates, and never appears in anything the dashboard renders.
- Ollama Cloud model rates, including the published peak window that doubles the DeepSeek rates on weekday afternoons.
- Count the agent's own OpenCode Go and Ollama Cloud spending. Running those routes through Hermes used to hide that usage; both are now included, and they reconcile to the agent's own ledger exactly.
- Split agent-sourced usage by what it was for: typed prompts, title generation, context compression, vision, approvals, and background review.
- Treat a Hermes home as a source folder, so it can be labelled per account or imported from another machine.
- Show up to four model rows on each overview card, from local token totals. Providers whose usage endpoint reports one aggregate number cannot supply per-model shares, so the local figures are the honest view there.
- Fixes to the above before release: reasoning was counted twice for agent rows (it is a subset of output there, not a separate counter), and an agent row's accumulated total could move to a later day on each scan and rewrite the daily history. Existing installs correct themselves on the next scan.

## 1.1.1 - 2026-09-15

- Follow Omarchy theme swaps live again: the dashboard watches the stable theme directory, which survives the swap that replaces it by rename.
- Repaint theme changes from a fast palette read instead of a full history scan, removing the two to three second lag after `omarchy theme set`.
- Derive the Pi provider color from the theme's bright foreground, since no shipped theme defines `bright_white`.
- Let the demo enable a subset of agents with `--agents` for quieter screenshots.

## 1.1.0 - 2026-09-15

- Synced machine ledgers: point every machine at one shared folder and each imports the others' snapshots, so totals, charts, and model breakdowns cover all of them.
- Show synced machines in the data coverage card, and warn once per broken snapshot instead of every scan.
- Fix release pinning in the one-line installer: tags download correctly, not only branches.

## 1.0.0 - 2026-09-15

- Named history accounts with multiple folders, account comparisons, and filters.
- Deduplicate mirrored history and flag conflicting account assignments.
- API-value shares and tokens/value per recorded session.
- Grok Build history, recorded API value, model calls, and weekly quota.
- Gemini CLI, general OpenCode, Pi, and Oh My Pi history, with source-route breakdowns.
- Detect active sources on first use and adapt layouts to the enabled providers.
- Refresh Grok quota after login and request fresh limits on manual Refresh.
- Reopen unmapped dashboard windows and focus the correct instance across workspaces.
- Keep provider colors readable on light and dark backgrounds.
- Wrap bar-widget provider chips when many sources are enabled.
- One overview card per account, with optional monthly prices per provider or labelled account.
- Daily and hourly charts split into one series per account when a provider has more than one, each in its own shade of the provider color, largest solid and the others dashed.
- Labelled accounts read their own agent usage record by matching id or name, so a second Codex account shows its quota beside the current login.
- OpenCode Go prices the full documented model table, doubles DeepSeek rates during peak hours, and falls back to OpenCode's recorded cost for unlisted models.
- The Go card shows value used against each model's monthly allowance, including the DeepSeek V4.1 Flash 4x promo through Sep 20.
- Price Codex `gpt-5.3-codex-spark` and mark `codex-auto-review` internal, clearing the missing-price warning.

## Initial package

Local metric ledger, provider comparisons, period and session drilldowns, API-value estimates, and optional Omarchy bar integration. Includes an offline pricing snapshot, synthetic demo, and user-owned install/rollback workflow.
