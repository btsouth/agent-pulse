import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "community.ai-usage-dashboard"
  ipcTarget: "community.ai-usage-dashboard"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color surface: Color.popups.background
  readonly property color track: Style.selectedFillFor(foreground, Color.accent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  // Read the live shell configuration through the bar, so cycling the clock
  // format updates this panel without restarting it.
  readonly property string clockPattern: barClockPattern(bar && bar.shell ? bar.shell.shellConfig : null)
  readonly property bool clockUses24Hour: !/[Aa]/.test(clockPattern.replace(/'[^']*'/g, ""))
  readonly property string shortTimePattern: clockUses24Hour ? "HH:mm" : "h:mm AP"
  readonly property string pinPath: (Quickshell.env("XDG_CONFIG_HOME") || Quickshell.env("HOME") + "/.config")
    + "/omarchy/ai-usage/pinned-limit.json"
  property var pinnedLimits: []
  property var pendingPins: []
  property string pinError: ""

  FileView {
    id: pinFile
    path: root.pinPath
    watchChanges: true
    atomicWrites: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        var value = JSON.parse(text())
        root.pinnedLimits = root.readPins(value)
      } catch (e) { root.pinnedLimits = [] }
    }
    onLoadFailed: root.pinnedLimits = []
  }

  Process {
    id: pinProcess
    running: false
    onExited: function(code) {
      if (code !== 0) root.pinError = "Could not save pinned limit"
      pinFile.reload()
      if (root.pendingPins.length > 0) {
        var next = root.pendingPins.shift()
        root.savePin(next.pin, next.remove)
      }
    }
  }

  function readPins(value) {
    var entries = value && Array.isArray(value.pins) ? value.pins : [value]
    return entries.filter(function(pin) {
      return pin && typeof pin.provider === "string" && /^[a-z0-9][a-z0-9-]{0,79}$/.test(pin.provider)
        && typeof pin.label === "string" && pin.label.length > 0
        && (pin.title === undefined || typeof pin.title === "string")
    }).map(function(pin) {
      return {provider: pin.provider, label: pin.label, title: pin.title || ""}
    }).slice(0, 3)
  }

  function savePin(pin, remove) {
    if (pinProcess.running) { pendingPins.push({pin: pin, remove: remove}); return }
    pinError = ""
    var command = ["python3", (Quickshell.env("AI_USAGE_ROOT") || Quickshell.env("HOME")
      + "/.local/share/omarchy-usage-dashboard/app") + "/collector.py", "pin",
      "--pin-provider", pin.provider, "--pin-label", pin.label, "--pin-title", pin.title]
    if (remove) command.push("--pin-remove")
    pinProcess.command = command
    pinProcess.running = true
  }

  function isPinned(providerId, label, title) {
    return pinnedLimits.some(function(pin) {
      return pin.provider === providerId && pin.label === label && pin.title === title
    })
  }

  function togglePin(providerId, window) {
    if (!window) return
    var remove = isPinned(providerId, window.label, window.title)
    if (!remove && pinnedLimits.length >= 3) { pinError = "Three pins maximum · unpin one first"; return }
    savePin({provider: providerId, label: window.label, title: window.title}, remove)
  }

  function barClockPattern(config) {
    var barConfig = config && config.bar ? config.bar : {}
    var layout = barConfig.layout || {}
    var vertical = barConfig.position === "left" || barConfig.position === "right"
    for (var side of ["left", "center", "right"]) {
      var entries = layout[side] || []
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i] || entries[i].id !== "omarchy.clock") continue
        return String(vertical ? (entries[i].verticalFormat || "HH\nmm")
                               : (entries[i].format || "dddd HH:mm"))
      }
    }
    return Qt.locale().timeFormat(Locale.ShortFormat)
  }

  // A scrollbar that leaves no groove behind: only a short thumb, no track.
  // The default control paints its groove over the text it scrolls, which is
  // what this replaces. It is placed in its own column by the caller, so it
  // must be parented to a NON-clipping item: a child of the clipping Flickable
  // is clipped away and paints nothing (verified: 0 px clipped vs 212 px not).
  component QuietScrollBar: ScrollBar {
    id: quiet
    policy: ScrollBar.AsNeeded
    leftPadding: Style.space(4)
    rightPadding: Style.space(4)
    topPadding: 0
    bottomPadding: 0
    implicitWidth: Style.space(12)
    background: Item {}
    contentItem: Rectangle {
      implicitWidth: Style.space(4)
      implicitHeight: Style.space(28)
      width: implicitWidth
      height: Math.max(implicitHeight, quiet.size * quiet.availableHeight)
      radius: width / 2
      color: quiet.pressed ? Color.accent : Qt.alpha(root.foreground, quiet.hovered ? 0.45 : 0.25)
      opacity: quiet.active || quiet.hovered ? 1 : 0.5
      Behavior on opacity { NumberAnimation { duration: 150 } }
    }
  }

  readonly property var providers: usage.enabledProviders
  // The selection follows the provider, not the slot it happens to sit in: a
  // provider whose first scan lands while the panel is open would otherwise
  // shift the list underneath you and swap out what you were reading.
  property string selectedProviderId: "all"
  onProvidersChanged: {
    if (selectedProviderId !== "all" && providers.length > 0
        && !providers.some(function(p) { return p.providerId === selectedProviderId }))
      selectedProviderId = "all"
  }
  readonly property bool allSelected: selectedProviderId === "all"
  readonly property int providerIndex: {
    if (allSelected) return -1
    for (var i = 0; i < providers.length; i++)
      if (providers[i].providerId === selectedProviderId) return i
    return -1
  }
  readonly property var provider: providerIndex >= 0 ? providers[providerIndex] : null

  property bool cursorActive: false

  // Countdowns and "updated" read this instead of Date.now() so the
  // panel keeps telling the truth while it sits open.
  property double nowMs: Date.now()

  readonly property var limits: limitWindows(provider)
  readonly property var models: modelRows(providers)
  readonly property var headline: bindingWindow(provider)
  readonly property var balance: provider ? (provider.balance || null) : null
  // Banked resets a provider grants for clearing a rate limit window early.
  // -1 is a collector that never read them, held apart from a read that
  // reports none -- though both stay off the panel, since a standing
  // "0 resets banked" line is noise on an account that never has any.
  readonly property int bankedResets: provider ? Number(provider.resetCreditsAvailable) : -1
  // Promotional resets expire unspent, so the date rides along when known.
  readonly property string bankedResetsExpiry: {
    var expires = new Date(provider ? String(provider.resetCreditsExpiresAt || "") : "")
    if (isNaN(expires.getTime())) return ""
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][expires.getMonth()]
      + " " + expires.getDate()
  }
  // A prepaid account runs low the way a subscription window fills up: the
  // last 10% of the funded credits lights the same alarm.
  readonly property bool balanceAlarming: !!balance && balance.funded > 0
    && balance.remaining / balance.funded <= 0.1
  readonly property bool alarming: allSelected
    ? providers.some(function(p) { var w = bindingWindow(p); return !!w && w.percent >= 0.9 })
    : (!!headline && headline.percent >= 0.9) || balanceAlarming

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }
  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  function selectProvider(index) {
    if (providers.length === 0) return
    var length = providers.length + 1
    var wrapped = (((index + 1) % length) + length) % length - 1
    selectedProviderId = wrapped < 0 ? "all" : providers[wrapped].providerId
  }

  function hourlyData() {
    var data = usage.hourlySummary
    return data && data.date === todayDate() && data.schemaVersion === 1
      && Number(data.utcOffsetMinutes) === -new Date(root.nowMs).getTimezoneOffset() ? data : null
  }

  function hourlyIds() {
    if (!allSelected) return provider ? [provider.providerId] : []
    return providers.map(function(p) { return p.providerId })
  }

  // A record with indexed history at any time. The snapshot lists the agent
  // records with history, since a second login has no provider id of its own;
  // one written before that carries only the providers.
  function hourlyHasHistory(data, id) {
    return (data.availableSources || data.availableProviders || []).indexOf(id) >= 0
  }

  function hourlyMissingIds() {
    var data = hourlyData()
    if (!data) return []
    return hourlyIds().filter(function(id) { return !hourlyHasHistory(data, id) })
  }

  function hourlyAvailable() {
    var data = hourlyData()
    return !!data && hourlyIds().some(function(id) { return hourlyHasHistory(data, id) })
  }

  // One source reads its own record's totals, so a second login is not shown
  // its provider's history. All sources adds up the providers, which already
  // hold every login.
  function hourlyOwnRecord(data, id) {
    return !allSelected && !!data.sources && !!data.sources[id]
  }

  function hourlyTotal(field) {
    var data = hourlyData()
    if (!data) return 0
    var ids = hourlyIds(), sum = 0
    for (var i = 0; i < ids.length; i++) {
      var entry = hourlyOwnRecord(data, ids[i]) ? data.sources[ids[i]] : (data.providers || {})[ids[i]]
      sum += Number((entry || {})[field] || 0)
    }
    return sum
  }

  function hourRows() {
    var data = hourlyData()
    if (!data || !hourlyAvailable()) return []
    var ids = hourlyIds()
    return data.hours.slice(-6).reverse().map(function(hour) {
      var value = 0
      for (var i = 0; i < ids.length; i++)
        value += Number(((hourlyOwnRecord(data, ids[i]) ? hour.sources : hour.providers) || {})[ids[i]] || 0)
      var repeated = data.hours.some(function(other) { return other.start !== hour.start && other.label === hour.label })
      var label = Qt.formatDateTime(new Date(Number(hour.start) * 1000), root.shortTimePattern)
      return { label: repeated ? label + " " + hour.zone : label, start: hour.start, tokens: value }
    })
  }

  // What the hourly section says when the source has no indexed history. The
  // count comes from the source's own usage record when it has one, so the
  // section still gives a number and says what is missing rather than only
  // that something is.
  readonly property var hourlyGap: {
    if (allSelected || !provider || !hourlyData() || hourlyAvailable()) return null
    var name = provider.providerName
    if (provider.todayTotalTokens > 0)
      return { title: Number(provider.todayTotalTokens).toLocaleString(Qt.locale("en_US"), "f", 0) + " tokens today",
               detail: "No hourly breakdown yet. " + name + " has not been indexed on this PC. Refresh, or add its history folder under Settings in Analytics." }
    if (provider.hasLocalStats === false)
      return { title: "No hourly token history",
               detail: name + " has no local usage logs, so only its limits are tracked here." }
    return { title: "Not indexed yet",
             detail: "No token history has been read for " + name + ". Refresh, or add its history folder under Settings in Analytics." }
  }

  function hourlyHeadline() {
    if (hourlyAvailable()) {
      var tokens = hourlyTotal("tokens")
      return tokens > 0 ? Number(tokens).toLocaleString(Qt.locale("en_US"), "f", 0) + " processed tokens today"
                        : "No tokens processed today"
    }
    return hourlyGap ? hourlyGap.title : "Hourly history is loading"
  }

  // Sources with no history, by name, for the All sources view.
  function hourlyMissingText() {
    var names = hourlyMissingIds().map(function(id) {
      var match = providers.find(function(p) { return p.providerId === id })
      return match ? match.providerName : id
    })
    if (names.length === 0) return ""
    var shown = names.length > 3 ? names.slice(0, 2).concat([(names.length - 2) + " more"]) : names
    var list = shown.length > 1 ? shown.slice(0, -1).join(", ") + " and " + shown[shown.length - 1] : shown[0]
    return "No hourly history for " + list
  }

  function hourPeak() {
    var rows = hourRows(), peak = 1
    for (var i = 0; i < rows.length; i++) peak = Math.max(peak, rows[i].tokens)
    return peak
  }

  function allLimitRows() {
    var rows = []
    for (var i = 0; i < providers.length; i++) {
      var windows = limitWindows(providers[i])
      for (var j = 0; j < windows.length; j++) {
        var window = windows[j]
        if (resetDate(window) && resetMsFor(window) <= 0) continue
        if (isPinned(providers[i].providerId, window.label, window.title)) continue
        rows.push({id: providers[i].providerId, name: providers[i].providerName,
                   label: window.label, title: window.title, percent: window.percent,
                   resetAt: window.resetAt, stale: providers[i].limitsStale === true})
      }
    }
    rows.sort(function(a, b) {
      if (a.stale !== b.stale) return a.stale ? 1 : -1
      return b.percent - a.percent
    })
    return rows.slice(0, 3)
  }

  function pinnedProvider(pin) {
    if (!pin) return null
    return providers.find(function(p) { return p.providerId === pin.provider }) || null
  }

  function pinnedWindow(pin) {
    var p = pinnedProvider(pin)
    if (!p) return null
    var windows = limitWindows(p)
    var exact = windows.find(function(w) { return w.label === pin.label && w.title === pin.title })
    if (exact) return exact
    var sameLabel = windows.filter(function(w) { return w.label === pin.label })
    return sameLabel.length === 1 ? sameLabel[0] : null
  }

  function pinnedProviderName(pin) {
    if (!pin) return ""
    if (pin.provider === "codex") return "ChatGPT Main"
    if (pin.provider === "codex-second") return "ChatGPT Second"
    var p = pinnedProvider(pin)
    return p ? p.providerName : pin.provider
  }

  function limitSourceName(id, fallback) {
    if (id === "codex") return "ChatGPT Main"
    if (id === "codex-second") return "ChatGPT Second"
    return fallback || id
  }

  // Provider timestamps can carry microseconds or nanoseconds. QML's Date
  // parser accepts milliseconds reliably, so trim only the excess precision.
  function resetDate(window) {
    if (!window || !window.resetAt) return null
    var date = new Date(String(window.resetAt).replace(/(\.\d{3})\d+/, "$1"))
    return isFinite(date.getTime()) ? date : null
  }

  // Local tokens recorded in a limit's current window, from the collector's
  // snapshot. -1 when it has no count for this window: the label does not say
  // how long the window runs, or the snapshot predates its latest reset.
  function windowTokens(providerId, window) {
    var data = usage.hourlySummary
    var list = data && data.limitTokens ? data.limitTokens[providerId] : null
    var reset = resetDate(window)
    if (!list || !reset) return -1
    for (var i = 0; i < list.length; i++) {
      var at = resetDate({resetAt: list[i].resetsAt})
      // Claude's reset time drifts by fractions of a second between reads.
      if (list[i].label === window.label && at && Math.abs(at.getTime() - reset.getTime()) < 300000)
        return Number(list[i].tokens)
    }
    return -1
  }

  function resetDescription(window) {
    if (!window.resetAt) return "Reset time not reported"
    var date = resetDate(window)
    if (!date) return "Reset time unavailable"
    var remaining = resetMsFor(window)
    if (remaining > 0) return "Resets in " + formatDuration(remaining)
      + " · " + Qt.formatDateTime(date, "ddd " + shortTimePattern)
    return "Reset time passed · awaiting update"
  }

  function refreshNow() {
    usage.refreshAll(true)
  }

  // Right-click launches the selected agent's own CLI when a launch command
  // is mapped for it in settings:
  //   "launchCommands": { "codex": "codex", "commandcode": "command-code" }
  // Anything else (an API-only agent, or one installed after the map was
  // written) falls back to omarchy-agent's own picker rather than being sent
  // to whichever CLI happened to be the default.
  function launchCommandFor(providerId) {
    var map = usage.setting("launchCommands", {})
    return map && map[providerId] ? String(map[providerId]) : ""
  }

  function launchAgent() {
    if (root.bar && root.provider) {
      var command = launchCommandFor(root.provider.providerId)
      if (command !== "") {
        root.bar.run("omarchy launch terminal " + command)
        root.close()
        return
      }
    }
    if (root.bar) root.bar.run("omarchy-agent --pick")
    root.close()
  }

  // ---------------------------------------------------------------- limits
  //
  // Both providers report the same two shapes: a short rolling session window
  // and a long weekly one. Everything below normalizes them into one record so
  // the meters and the hero speak a single language.

  // Claude spells its windows out ("Session (5-hour)"), Codex abbreviates
  // them ("5h window", "30m window"). Both have to land on the same record.
  function windowIsLong(text) {
    return text.indexOf("week") >= 0 || text.indexOf("7-day") >= 0 || text.indexOf("seven") >= 0
      || text.indexOf("month") >= 0 || text.indexOf("30-day") >= 0
  }

  function windowSpanMs(label) {
    var text = String(label || "").toLowerCase()
    if (text.indexOf("month") >= 0 || text.indexOf("30-day") >= 0) return 30 * 24 * 3600 * 1000
    if (windowIsLong(text)) return 7 * 24 * 3600 * 1000
    var hours = text.match(/(\d+)\s*-?\s*h(?:our)?\b/)
    if (hours) return Number(hours[1]) * 3600 * 1000
    var minutes = text.match(/(\d+)\s*-?\s*m(?:in(?:ute)?s?)?\b/)
    if (minutes) return Number(minutes[1]) * 60 * 1000
    return 0
  }

  function windowTitle(label) {
    var text = String(label || "").toLowerCase()
    if (text.indexOf("month") >= 0) return "Monthly"
    if (windowIsLong(text)) return "Weekly"
    if (text.indexOf("session") >= 0 || windowSpanMs(label) > 0) return "Session"
    var plain = String(label || "").replace(/\s*\(.*\)\s*/, "").trim()
    return plain === "" ? "Limit" : plain
  }

  // A collector that already knows which window a limit belongs to says so,
  // and that beats reading it back out of the label: a model-scoped limit is
  // titled after its model, and a name like "Opus 5 (1M context)" would parse
  // as a one-minute window.
  function limitWindow(label, percent, resetAt, title) {
    return {
      label: String(label || ""),
      title: String(title || "") !== "" ? String(title) : windowTitle(label),
      percent: Number(percent),
      resetAt: String(resetAt || "")
    }
  }

  function limitWindows(p) {
    if (!p) return []
    var out = []
    var list = p.limits || []
    for (var i = 0; i < list.length; i++) {
      var entry = list[i] || {}
      var percent = Number(entry.percent)
      if (percent >= 0) out.push(limitWindow(entry.label, percent, entry.resetsAt, entry.title))
    }
    return out
  }

  // The window that decides how much room is left — the fullest one, since
  // that is what stops the next prompt.
  function bindingWindow(p) {
    var windows = limitWindows(p)
    var best = null
    for (var i = 0; i < windows.length; i++) {
      // A provider can leave a full window in its last saved record after
      // the advertised reset. That is no longer a live quota warning.
      if (resetDate(windows[i]) && resetMsFor(windows[i]) <= 0) continue
      if (!best || windows[i].percent > best.percent) best = windows[i]
    }
    return best
  }

  function resetMsFor(w) {
    var date = resetDate(w)
    return date ? date.getTime() - root.nowMs : -1
  }

  function formatDuration(ms) {
    if (!(ms > 0)) return "now"
    var minutes = Math.floor(ms / 60000)
    var hours = Math.floor(minutes / 60)
    var days = Math.floor(hours / 24)
    if (days > 0) return days + "d " + (hours % 24) + "h"
    if (hours > 0) return hours + "h " + (minutes % 60) + "m"
    return Math.max(1, minutes) + "m"
  }

  // ---------------------------------------------------------------- balance
  //
  // Prepaid agents report a credit ledger instead of rate-limit windows: the
  // record's balance object carries remaining, funded, and spent amounts.

  function currencyPrefix(currency) {
    var code = String(currency || "USD").toUpperCase()
    if (code === "USD") return "$"
    if (code === "EUR") return "€"
    if (code === "GBP") return "£"
    return code + " "
  }

  function formatMoney(value, currency) {
    var amount = Number(value)
    if (!isFinite(amount)) amount = 0
    return currencyPrefix(currency) + amount.toFixed(2)
  }

  function balanceDetailText(b) {
    if (!b || !(b.funded > 0)) return ""
    // Spent is printed as the difference, so the three figures on the card add
    // up to the cent instead of rounding apart.
    var spent = Number(b.funded) - Number(b.remaining)
    if (!isFinite(spent) || spent < 0) spent = Number(b.spent) || 0
    var text = formatMoney(spent, b.currency) + " spent of " + formatMoney(b.funded, b.currency) + " funded"
    if (b.estimated) text += " · estimated"
    return text
  }

  // A wallet the vendor names itself keeps that name; the generic label is
  // for a collector that reports a balance without saying what it is called.
  function balanceLabelText(b) {
    var label = b ? String(b.label || "").trim() : ""
    return label !== "" ? label : "Prepaid credits"
  }

  // ---------------------------------------------------------------- content

  // The plan you pay for, under the name of the tool it pays for. Limits live
  // in their own section; the hero just says what this is.
  function heroMeta(p) {
    if (!p) return ""
    if (String(p.usageStatusText || "") !== "") return p.usageStatusText
    var tier = String(p.tierLabel || "")
    if (tier === "") return "Subscription"
    return tier.charAt(0).toUpperCase() + tier.slice(1)
  }

  // Local calendar date, recomputed from nowMs so a panel left open across
  // midnight moves the "Today" row with the clock.
  function todayDate() {
    var now = new Date(root.nowMs)
    return now.getFullYear()
      + "-" + String(now.getMonth() + 1).padStart(2, "0")
      + "-" + String(now.getDate()).padStart(2, "0")
  }

  function dayName(date) {
    var parsed = new Date(String(date || "") + "T00:00:00")
    if (isNaN(parsed.getTime())) return String(date || "")
    return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][parsed.getDay()]
  }

  function dayLabel(date, today) {
    if (today) return "Today"
    return dayName(date)
  }

  function dayTooltip(day, today) {
    if (!day) return ""
    var parsed = new Date(String(day.date) + "T00:00:00")
    var label = isNaN(parsed.getTime())
      ? String(day.date)
      : dayName(day.date) + " " + (parsed.getMonth() + 1) + "/" + parsed.getDate()
    var text = label + " · " + usage.formatTokenCount(Number(day.messageCount || 0)) + " tokens"
    // Request and session counts only exist for today, so they ride along here
    // instead of taking a section of their own. Billing-API agents never
    // count requests, and "0 requests" would read as a quiet day, not a gap.
    if (today && provider && provider.hasPromptStats !== false)
      text += " · " + Number(provider.todayPrompts || 0) + " requests · "
        + Number(provider.todaySessions || 0) + " sessions"
    return text
  }

  function weekPeak(p) {
    var days = p ? (p.recentDays || []) : []
    var peak = 0
    for (var i = 0; i < days.length; i++) peak = Math.max(peak, Number(days[i].messageCount || 0))
    return peak
  }

  function modelRows(providerRows) {
    var totals = {}
    for (var p = 0; p < providerRows.length; p++) {
      var usageByModel = providerRows[p] ? (providerRows[p].modelUsage || {}) : {}
      for (var id in usageByModel) {
        var family = usage.modelFamily(id)
        var bucket = usageByModel[id] || {}
        var row = totals[family]
        if (!row) row = totals[family] = {
          id: family,
          name: usage.friendlyModelName(family),
          total: 0,
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0
        }
        row.input += Number(bucket.inputTokens || 0)
        row.output += Number(bucket.outputTokens || 0)
        row.cacheRead += Number(bucket.cacheReadInputTokens || 0)
        row.cacheWrite += Number(bucket.cacheCreationInputTokens || 0)
      }
    }
    var rows = []
    for (var family in totals) {
      var item = totals[family]
      item.total = item.input + item.output + item.cacheRead + item.cacheWrite
      rows.push(item)
    }
    rows.sort(function(a, b) { return b.total - a.total })
    return rows.slice(0, 4)
  }

  function modelTooltip(row) {
    if (!row) return ""
    return "In " + usage.formatTokenCount(row.input)
      + " · out " + usage.formatTokenCount(row.output)
      + " · cache read " + usage.formatTokenCount(row.cacheRead)
      + " · cache write " + usage.formatTokenCount(row.cacheWrite)
  }

  // Only speaks up when the numbers cover more than this machine.
  function footerText() {
    if (usage.syncStatusText !== "") return usage.syncStatusText
    if (provider && provider.syncEnabled && provider.syncDeviceCount > 0)
      return "Merged from " + provider.syncDeviceCount + " device" + (provider.syncDeviceCount === 1 ? "" : "s")
    return ""
  }

  // Agents that ship a white mark carry an `assets/<id>-light.svg` twin for
  // light surfaces; marks that work on both (Claude's brand-orange) ship one
  // file. The luminance check decides which candidate to try first.
  function colorChannelLuminance(value) {
    var channel = Number(value)
    if (!isFinite(channel)) return 0
    return channel <= 0.03928 ? channel / 12.92 : Math.pow((channel + 0.055) / 1.055, 2.4)
  }

  function colorLuminance(color) {
    return 0.2126 * colorChannelLuminance(color.r)
      + 0.7152 * colorChannelLuminance(color.g)
      + 0.0722 * colorChannelLuminance(color.b)
  }

  // Marks resolve by convention, so a new agent's data file needs nothing
  // from this panel: assets/<id>.svg if it ships one, the module's bar glyph
  // if it doesn't.
  function iconCandidatesForProvider(p, surfaceColor) {
    if (!p) return []
    var candidates = []
    if (colorLuminance(surfaceColor || Color.background) >= 0.5)
      candidates.push(Qt.resolvedUrl("assets/" + p.providerId + "-light.svg"))
    candidates.push(Qt.resolvedUrl("assets/" + p.providerId + ".svg"))
    return candidates
  }

  // Nothing to report, nothing in the bar: Bar.qml collapses a slot whose item
  // is invisible, so the icon appears the moment the first scan finds usage and
  // stays away entirely on a machine that has never run either CLI.
  visible: providers.length > 0
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onProviderIndexChanged: if (panelFlick) panelFlick.contentY = 0
  onOpenedChanged: if (opened) {
    cursorActive = false
    nowMs = Date.now()
    if (panelFlick) panelFlick.contentY = 0
    usage.refreshLimits()
    usage.refreshPulse()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Main {
    id: usage
    settings: root.settings
  }

  // Cheap enough to keep running: it only re-evaluates text bindings, and a
  // stale "resets in 2h" on a panel that is open is worse than a timer.
  Timer {
    interval: 30000
    running: root.opened
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  Timer {
    interval: 15000
    running: root.opened
    repeat: true
    onTriggered: usage.refreshPulse()
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refreshNow(); return "ok" }
    function next(): string { root.selectProvider(root.providerIndex + 1); return "ok" }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󱚣"
    active: root.alarming
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.launchAgent()
      else if (buttonCode === Qt.MiddleButton) root.selectProvider(root.providerIndex + 1)
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(460))
    // Use the available screen height for the limits; fittedContentHeight
    // still keeps the card clear of the bar and screen edges.
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(900))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: providerSwitch.popupOpen

      onMoveRequested: function(dx, dy) {
        if (dx !== 0) {
          root.cursorActive = true
          root.selectProvider(root.providerIndex + dx)
        }
        if (dy !== 0)
          panelFlick.contentY = root.clamp(panelFlick.contentY + dy * Style.space(56), 0,
                                           Math.max(0, panelFlick.contentHeight - panelFlick.height))
      }
      onActivateRequested: root.refreshNow()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { if (t === "r" || t === "R") root.refreshNow() }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        // Leave a column free on the right for the scrollbar's own track.
        anchors.rightMargin: Style.space(14)
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height

        // The attached control moves the Flickable when dragged. Keep it
        // outside the clipped content while retaining that connection.
        ScrollBar.vertical: QuietScrollBar {
          id: panelScroll
          parent: keyCatcher
          x: panelFlick.x + panelFlick.width + Style.space(1)
          y: panelFlick.y
          height: panelFlick.height
          interactive: true
        }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          RowLayout {
            id: headerControls
            width: parent.width
            spacing: Style.space(8)

            SourceDropdown {
              id: providerSwitch
              visible: root.providers.length > 1
              // Inset the picker so its full outline stays inside the clip.
              Layout.leftMargin: Style.space(3)
              Layout.fillWidth: true
              Layout.minimumWidth: Style.space(120)
              label: "SOURCE"
              showLabel: false
              rowHeight: Style.space(42)
              value: root.selectedProviderId
              options: [{value: "all", label: "All sources"}].concat(root.providers.map(function(p) {
                return {value: p.providerId, label: ({"codex": "Main", "codex-second": "Second", "claude-second": "Claude 2", "claude-third": "Claude 3", "opencode-go": "OpenCode Go"})[p.providerId] || p.providerName}
              }))
              foreground: root.foreground
              background: root.surface
              fontFamily: root.fontFamily
              onChanged: function(value) { root.selectedProviderId = value }
              Connections {
                target: root
                function onSelectedProviderIdChanged() { providerSwitch.value = root.selectedProviderId }
              }
            }

            Button {
              id: analyticsButton
              Layout.preferredWidth: implicitWidth
              Layout.fillWidth: !providerSwitch.visible
              Layout.rightMargin: Style.space(3)
              text: "Analytics ↗"
              Accessible.name: "Open Agent Pulse analytics"
              selected: true
              focusable: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              verticalPadding: Style.space(10)
              onClicked: {
                Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/omarchy-usage-dashboard"])
                root.close()
              }
            }
          }

          // ---------- Hero: provider mark · name · plan ----------
          PanelHero {
            id: hero
            visible: !!root.provider
            width: parent.width
            title: root.provider ? root.provider.providerName : ""
            meta: root.heroMeta(root.provider)
            foreground: root.foreground
            fontFamily: root.fontFamily

            iconComponent: Component {
              Item {
                id: heroMark
                property var candidates: root.iconCandidatesForProvider(root.provider, root.surface)
                // Provider objects are rebuilt on every refresh, which churns the
                // array's identity without changing its content. Restart the fallback
                // walk only when the URLs change: re-pointing source at a URL whose
                // load already failed emits no statusChanged, so an identity-only
                // reset would strand the walker on a missing -light twin.
                property string candidatesKey: candidates.join("\n")
                property int candidateIndex: 0
                onCandidatesKeyChanged: candidateIndex = 0

                width: Style.font.display
                height: Style.font.display

                Image {
                  id: heroMarkImage
                  anchors.fill: parent
                  source: heroMark.candidateIndex < heroMark.candidates.length ? heroMark.candidates[heroMark.candidateIndex] : ""
                  sourceSize.width: Style.font.display * 2
                  sourceSize.height: Style.font.display * 2
                  fillMode: Image.PreserveAspectFit
                  // Advancing source from inside its own status change trips the
                  // binding-loop detector; defer the step one tick.
                  onStatusChanged: if (status === Image.Error && heroMark.candidateIndex < heroMark.candidates.length)
                    Qt.callLater(function() { heroMark.candidateIndex++ })
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.centerIn: parent
                  visible: heroMarkImage.status !== Image.Ready
                  text: button.text
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }
            }
          }

          Column {
            visible: root.allSelected && root.providers.length > 0
            width: parent.width
            spacing: Style.spacing.sm
            Text {
              width: parent.width
              text: "Agent Pulse"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              font.bold: true
            }
            AnimatedTokenCount {
              width: parent.width
              dataReady: root.hourlyAvailable()
              visible: dataReady
              targetTokens: root.hourlyTotal("tokens")
              scopeKey: (root.allSelected ? "all" : "other") + "|" + root.todayDate()
              animateChanges: root.opened && root.allSelected
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display + 8
              font.bold: true
            }
            Text {
              width: parent.width
              text: root.hourlyAvailable() ? "Processed tokens today · updates as agents record usage"
                                           : "Reading local token history"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Column {
            id: pinnedSection
            visible: root.allSelected && root.pinnedLimits.length > 0
            width: parent.width
            spacing: Style.space(8)

            RowLayout {
              width: parent.width
              PanelSectionHeader {
                text: root.pinnedLimits.length === 1 ? "PINNED LIMIT" : "PINNED LIMITS"
                foreground: root.foreground
                fontFamily: root.fontFamily
                Layout.fillWidth: true
              }
              Text {
                text: root.pinnedLimits.length + "/3"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
            Repeater {
              model: root.pinnedLimits
              Column {
                required property var modelData
                readonly property var limit: root.pinnedWindow(modelData)
                readonly property var source: root.pinnedProvider(modelData)
                width: pinnedSection.width
                spacing: Style.space(3)
                RowLayout {
                  width: parent.width
                  Text {
                    text: root.pinnedProviderName(modelData) + " · " + (limit ? limit.title : modelData.title || root.windowTitle(modelData.label))
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                  }
                  Text {
                    text: limit ? Math.round(limit.percent * 100) + "%" : "—"
                    color: limit && limit.percent >= 0.9 ? root.urgent : root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                  Button {
                    text: "Unpin"
                    foreground: root.dim
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    focusable: true
                    Accessible.name: "Unpin " + root.pinnedProviderName(modelData) + " " + modelData.title + " limit"
                    onClicked: root.savePin(modelData, true)
                  }
                }
                Meter {
                  visible: !!limit
                  width: parent.width
                  value: limit ? limit.percent : -1
                  alarming: !!limit && limit.percent >= 0.9
                }
                Text {
                  width: parent.width
                  text: limit ? (source && source.limitsStale ? "Last known · " : "")
                    + root.resetDescription(limit) : "Waiting for this limit's next update"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }
              }
            }
          }

          Text {
            visible: root.pinError !== ""
            width: parent.width
            text: root.pinError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            visible: root.providers.length === 0
            width: parent.width
            topPadding: Style.space(24)
            text: "No AI coding subscriptions found.\nAgents show up here once you've used them."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          PanelSeparator { visible: root.allSelected && root.allLimitRows().length > 0; foreground: root.foreground }

          Column {
            id: allLimitsSection
            visible: root.allSelected && root.allLimitRows().length > 0
            width: parent.width
            spacing: Style.spacing.sm

            PanelSectionHeader {
              width: parent.width
              text: "LIMITS TO WATCH"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }
            Repeater {
              model: root.allLimitRows()
              Button {
                required property var modelData
                width: allLimitsSection.width
                height: Style.space(54)
                text: ""
                Accessible.name: root.limitSourceName(modelData.id, modelData.name) + ", " + modelData.title + ", "
                  + Math.round(modelData.percent * 100) + "% used, " + root.resetDescription(modelData)
                  + ", show source details"
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                leftAlign: true
                focusable: true
                horizontalPadding: Style.space(6)
                verticalPadding: Style.space(3)
                onClicked: root.selectedProviderId = modelData.id
                Column {
                  anchors.fill: parent
                  anchors.margins: Style.space(6)
                  anchors.rightMargin: Style.space(56)
                  spacing: Style.space(4)
                  RowLayout {
                    width: parent.width
                    spacing: Style.space(8)
                    Text {
                      text: root.limitSourceName(modelData.id, modelData.name) + " · " + modelData.title
                      color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.caption
                      elide: Text.ElideRight; Layout.fillWidth: true
                    }
                    Text {
                      text: Math.round(modelData.percent * 100) + "% used"
                      color: modelData.percent >= 0.9 ? root.urgent : root.foreground
                      font.family: root.fontFamily; font.pixelSize: Style.font.caption
                    }
                  }
                  Meter { width: parent.width; value: modelData.percent; alarming: modelData.percent >= 0.9 }
                  Text {
                    text: (modelData.stale ? "Last known · " : "") + root.resetDescription(modelData)
                    color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption
                  }
                }
                Button {
                  width: Style.space(46)
                  height: Style.space(28)
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Pin"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  focusable: true
                  Accessible.name: "Pin " + root.limitSourceName(modelData.id, modelData.name) + " " + modelData.title
                  onClicked: root.togglePin(modelData.id, modelData)
                }
              }
            }
          }

          // ---------- Status ----------
          BorderSurface {
            visible: !!root.provider && String(root.provider.usageStatusText || "") !== ""
            width: parent.width
            implicitHeight: statusText.implicitHeight + Style.spacing.xl * 2
            color: root.alpha(root.urgent, 0.10)
            borderSpec: Border.flat(root.alpha(root.urgent, 0.35), 1)
            radius: Style.cornerRadius

            Text {
              id: statusText
              textFormat: Text.PlainText
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              text: root.provider ? String(root.provider.authHelpText || root.provider.usageStatusText || "") : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // ---------- Balance / limits ----------
          PanelSeparator {
            visible: balanceSection.visible || limitsSection.visible
            foreground: root.foreground
          }

          Column {
            id: balanceSection
            visible: !!root.balance
            width: parent.width
            spacing: Style.space(10)

            // The meter shows what is left, not what is used: a prepaid
            // account drains toward empty rather than filling toward a cap.
            readonly property real ratio: root.balance && root.balance.funded > 0
              ? root.clamp(root.balance.remaining / root.balance.funded, 0, 1)
              : -1

            PanelSectionHeader {
              width: parent.width
              text: "BALANCE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Item {
              width: parent.width
              implicitHeight: Math.max(balanceLabel.implicitHeight, balanceValue.implicitHeight)

              Text {
                id: balanceLabel
                // The vendor names its own wallet, so the text is data now.
                textFormat: Text.PlainText
                text: root.balanceLabelText(root.balance)
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: balanceValue
                textFormat: Text.PlainText
                text: root.balance ? root.formatMoney(root.balance.remaining, root.balance.currency) : ""
                color: root.balanceAlarming ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            Meter {
              visible: balanceSection.ratio >= 0
              width: parent.width
              value: balanceSection.ratio
              alarming: root.balanceAlarming
            }

            Text {
              textFormat: Text.PlainText
              visible: text !== ""
              width: parent.width
              text: root.balanceDetailText(root.balance)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Column {
            id: limitsSection
            visible: root.limits.length > 0
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "LIMITS"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              visible: !!root.provider && root.provider.limitsStale === true
              width: parent.width
              text: "Last known limits · refreshing"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Repeater {
              model: root.limits

              LimitRow {
                required property var modelData
                width: limitsSection.width
                window: modelData
              }
            }

            Text {
              visible: root.bankedResets > 0
              width: parent.width
              text: (root.bankedResets === 1 ? "1 reset banked" : root.bankedResets + " resets banked")
                + (root.bankedResetsExpiry ? " · expires " + root.bankedResetsExpiry : "")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          PanelSeparator {
            visible: hourlySection.visible && (allLimitsSection.visible || limitsSection.visible || balanceSection.visible)
            foreground: root.foreground
          }

          Column {
            id: hourlySection
            visible: root.providers.length > 0
            width: parent.width
            spacing: Style.spacing.md
            readonly property var rows: root.hourRows()
            readonly property real peak: root.hourPeak()

            PanelSectionHeader {
              width: parent.width
              text: "LATEST 6 HOURS · TODAY"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }
            Text {
              visible: !root.allSelected
              width: parent.width
              text: root.hourlyHeadline()
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: true
              wrapMode: Text.WordWrap
            }
            Text {
              visible: !!root.hourlyGap
              width: parent.width
              text: root.hourlyGap ? root.hourlyGap.detail : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
            Repeater {
              model: hourlySection.rows
              HourRow {
                required property var modelData
                required property int index
                width: hourlySection.width
                hour: modelData
                ratio: Number(modelData.tokens || 0) / hourlySection.peak
                current: index === 0
              }
            }
            Text {
              visible: root.allSelected && root.hourlyMissingIds().length > 0
              width: parent.width
              text: root.hourlyMissingText()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
            Text {
              visible: root.hourlyTotal("unplacedTokens") > 0
              width: parent.width
              text: Number(root.hourlyTotal("unplacedTokens")).toLocaleString(Qt.locale("en_US"), "f", 0)
                    + " session-summary tokens have no exact hour"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
            Text {
              visible: !!root.hourlyData()
              width: parent.width
              text: root.hourlyData() ? (root.nowMs - Number(root.hourlyData().generatedAt || 0) * 1000 < 60000
                    ? "Updated just now · local time"
                    : "Updated " + root.formatDuration(root.nowMs - Number(root.hourlyData().generatedAt || 0) * 1000) + " ago · local time") : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          // ---------- Usage ----------
          PanelSeparator {
            visible: usageSection.visible
            foreground: root.foreground
          }

          Column {
            id: usageSection
            visible: !!root.provider && root.provider.recentDays && root.provider.recentDays.length > 0
            width: parent.width
            spacing: Style.spacing.md

            readonly property var days: root.provider ? (root.provider.recentDays || []) : []
            readonly property real peak: Math.max(1, root.weekPeak(root.provider))

            PanelSectionHeader {
              width: parent.width
              text: "TOKENS BY DAY"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: usageSection.days

              DayRow {
                required property var modelData
                required property int index

                width: usageSection.width
                day: modelData
                ratio: Number(modelData.messageCount || 0) / usageSection.peak
                // By date, not by position: the Claude stats-cache fallback can
                // hand us a window that stops short of today.
                today: String(modelData.date || "") === root.todayDate()
              }
            }
          }

          // ---------- Models ----------
          PanelSeparator {
            visible: modelSection.visible
            foreground: root.foreground
          }

          Column {
            id: modelSection
            visible: root.models.length > 0
            width: parent.width
            spacing: Style.spacing.md

            PanelSectionHeader {
              width: parent.width
              text: "TOKENS BY MODEL · ALL ROUTES"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.models

              ModelRow {
                required property var modelData
                width: modelSection.width
                row: modelData
                share: modelData.total / Math.max(1, root.models[0].total)
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: text !== ""
            width: parent.width
            topPadding: Style.space(2)
            text: root.footerText()
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
          }
        }
      }
    }
  }

  // A limit window: label and percentage, meter, and reset countdown.
  component LimitRow: Column {
    id: limitRow
    property var window: null

    readonly property bool alarming: window && window.percent >= 0.9
      && (!root.resetDate(window) || root.resetMsFor(window) > 0)

    spacing: Style.space(6)

    Item {
      width: parent.width
      implicitHeight: Math.max(limitLabel.implicitHeight, limitValue.implicitHeight)

      Text {
        id: limitLabel
        textFormat: Text.PlainText
        // A model-scoped window is titled after its model, and those names run
        // long enough to reach the percentage, so the title gives way first.
        text: limitRow.window ? limitRow.window.title : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        elide: Text.ElideRight
        anchors.left: parent.left
        anchors.right: limitValue.left
        anchors.rightMargin: Style.spacing.sm
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: limitValue
        textFormat: Text.PlainText
        text: limitRow.window && limitRow.window.percent >= 0
          ? Math.round(limitRow.window.percent * 100) + "%"
          : "—"
        color: limitRow.alarming ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        anchors.right: pinButton.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
      }
      Button {
        id: pinButton
        width: Style.space(50)
        height: Style.space(24)
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        text: root.provider && limitRow.window && root.isPinned(root.provider.providerId, limitRow.window.label, limitRow.window.title)
          ? "Pinned" : root.pinnedLimits.length >= 3 ? "Full" : "Pin"
        enabled: text !== "Full"
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        focusable: true
        Accessible.name: text + " " + (limitRow.window ? limitRow.window.title : "") + " limit"
        onClicked: if (root.provider) root.togglePin(root.provider.providerId, limitRow.window)
      }
    }

    Meter {
      width: parent.width
      value: limitRow.window ? limitRow.window.percent : -1
      alarming: limitRow.alarming
    }

    Text {
      id: resetText
      textFormat: Text.PlainText
      width: parent.width
      text: {
        var remainingMs = root.resetMsFor(limitRow.window)
        if (!(remainingMs > 0)) return ""
        var tokens = root.provider ? root.windowTokens(root.provider.providerId, limitRow.window) : -1
        return "Resets in " + root.formatDuration(remainingMs)
          + (tokens >= 0 ? " · " + usage.formatTokenCount(tokens) + " tokens on this PC" : "")
      }
      elide: Text.ElideRight
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  // Rounded track showing the percentage of the allowance used.
  component Meter: Item {
    id: meter
    property real value: -1
    property bool alarming: false
    property real thickness: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))

    implicitHeight: thickness

    Rectangle {
      id: meterTrack
      anchors.fill: parent
      radius: height / 2
      color: root.track
    }

    Rectangle {
      anchors.left: meterTrack.left
      anchors.verticalCenter: meterTrack.verticalCenter
      height: meterTrack.height
      radius: meterTrack.radius
      width: meterTrack.width * root.clamp(meter.value, 0, 1)
      color: meter.alarming ? root.urgent : root.foreground

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }

  }

  // One row per day: label, bar, tokens. Today is picked out in full
  // foreground so the week reads as a run-up to right now.
  component HourRow: Item {
    id: hourRow
    property var hour: null
    property real ratio: 0
    property bool current: false
    implicitHeight: Math.max(hourLabel.implicitHeight, hourValue.implicitHeight) + Style.spacing.sm
    Accessible.name: (hour ? hour.label : "") + ", " + (hour ? Number(hour.tokens || 0).toLocaleString(Qt.locale("en_US"), "f", 0) : "0") + " tokens"

    Text {
      id: hourLabel
      text: hourRow.hour ? hourRow.hour.label : ""
      color: hourRow.current ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: hourRow.current
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(hourRow.hour && hourRow.hour.label.length > 5 ? 92 : 52)
    }
    Rectangle {
      anchors.left: hourLabel.right
      anchors.right: hourValue.left
      anchors.leftMargin: Style.space(8)
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      height: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))
      radius: height / 2
      color: root.track
      Rectangle {
        height: parent.height
        width: parent.width * root.clamp(hourRow.ratio, 0, 1)
        radius: parent.radius
        color: hourRow.current ? root.foreground : root.alpha(root.foreground, 0.58)
      }
    }
    Text {
      id: hourValue
      text: hourRow.hour ? Number(hourRow.hour.tokens || 0).toLocaleString(Qt.locale("en_US"), "f", 0) : "0"
      color: hourRow.current ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: hourRow.current
      horizontalAlignment: Text.AlignRight
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(90)
    }
  }

  component DayRow: Item {
    id: dayRow
    property var day: null
    property real ratio: 0
    property bool today: false

    implicitHeight: Math.max(dayLabel.implicitHeight, dayValue.implicitHeight) + Style.spacing.sm

    Text {
      id: dayLabel
      textFormat: Text.PlainText
      text: root.dayLabel(dayRow.day ? dayRow.day.date : "", dayRow.today)
      color: dayRow.today ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: dayRow.today
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(52)
    }

    Rectangle {
      id: dayTrack
      anchors.left: dayLabel.right
      anchors.right: dayValue.left
      anchors.leftMargin: Style.space(8)
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      height: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))
      radius: height / 2
      color: root.track

      Rectangle {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height
        radius: parent.radius
        width: parent.width * root.clamp(dayRow.ratio, 0, 1)
        color: dayRow.today ? root.foreground : root.alpha(root.foreground, 0.55)

        Behavior on width {
          NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
        }
      }
    }

    Text {
      id: dayValue
      textFormat: Text.PlainText
      text: usage.formatTokenCount(dayRow.day ? Number(dayRow.day.messageCount || 0) : 0)
      color: dayRow.today ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      horizontalAlignment: Text.AlignRight
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(52)
    }

    MouseArea {
      id: dayHover
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.NoButton
    }

    PanelToolTip {
      visible: dayHover.containsMouse
      text: root.dayTooltip(dayRow.day, dayRow.today)
      fontFamily: root.fontFamily
    }
  }

  component ModelRow: Item {
    id: modelRow
    property var row: null
    property real share: 0

    implicitHeight: modelName.implicitHeight + Style.spacing.lg

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.05)
    }

    Rectangle {
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: parent.width * root.clamp(modelRow.share, 0, 1)
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.14)

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }

    Text {
      id: modelName
      textFormat: Text.PlainText
      text: modelRow.row ? modelRow.row.name : ""
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
      anchors.left: parent.left
      anchors.leftMargin: Style.space(8)
      anchors.right: modelTokens.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: modelTokens
      textFormat: Text.PlainText
      text: modelRow.row ? usage.formatTokenCount(modelRow.row.total) : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
      anchors.right: parent.right
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    MouseArea {
      id: modelHover
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.NoButton
    }

    PanelToolTip {
      visible: modelHover.containsMouse
      text: root.modelTooltip(modelRow.row)
      fontFamily: root.fontFamily
    }
  }

}
