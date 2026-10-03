import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io

Scope {
    id: root
    readonly property var palette: themeData && themeData.palette ? themeData.palette : (data && data.theme ? data.theme.palette : ({}))
    readonly property color base: palette.background || "#111c18"
    readonly property color ink: palette.foreground || "#c1c497"
    readonly property color bright: palette.light_foreground || ink
    readonly property color accent: palette.accent || "#509475"
    readonly property color muted: Qt.alpha(ink, 0.84)
    readonly property color edge: Qt.alpha(ink, 0.14)
    readonly property color bg: Qt.alpha(base, settingsOpen ? opacitySlider.value : data ? Number(data.settings.windowOpacity || 0.985) : 0.985)
    readonly property color surface: Qt.alpha(palette.lighter_background || "#23372b", 0.28)
    readonly property string fontFamily: themeData && themeData.font ? themeData.font : (data && data.theme ? data.theme.font : "monospace")
    readonly property string helper: (Quickshell.env("AI_USAGE_ROOT") || decodeURIComponent(Qt.resolvedUrl("..").toString().replace(/^file:\/\//, ""))) + "/collector.py"
    property string clockPattern: Qt.locale().timeFormat(Locale.ShortFormat)
    readonly property bool clockUses24Hour: !/[Aa]/.test(clockPattern.replace(/'[^']*'/g, ""))
    readonly property string shortTimePattern: clockUses24Hour ? "HH:mm" : "h:mm AP"
    function loadClockPattern(raw) {
        try {
            var config = JSON.parse(raw)
            var bar = config.bar || {}, layout = bar.layout || {}
            var vertical = bar.position === "left" || bar.position === "right"
            for (var side of ["left", "center", "right"]) {
                var entries = layout[side] || []
                for (var i = 0; i < entries.length; i++) {
                    if (!entries[i] || entries[i].id !== "omarchy.clock") continue
                    clockPattern = String(vertical ? (entries[i].verticalFormat || "HH\nmm")
                                                   : (entries[i].format || "dddd HH:mm"))
                    return
                }
            }
        } catch (e) {}
        clockPattern = Qt.locale().timeFormat(Locale.ShortFormat)
    }
    function localClock(start) { return Qt.formatDateTime(new Date(Number(start) * 1000), shortTimePattern) }
    function hourTitle(hour) {
        var parts = String(hour.title || "").split(" to ")
        if (parts.length !== 2) return localClock(hour.start)
        var start = Number(hour.start)
        return parts[0].replace(/^\d{2}:\d{2}/, localClock(start)) + " to "
            + (parts[1] === "now" ? "now" : parts[1].replace(/^\d{2}:\d{2}/, localClock(start + 3600)))
    }
    property var data: null
    property string requestedAgent: Quickshell.env("AI_USAGE_ACCOUNT_RECORD") || ""
    function openAgent(id) {
        if (!data) { requestedAgent = id; return }
        var account = accountViews.find(a => a.recordId === id)
        if (id === "all" || account) chooseAccount(account ? account.id : "all")
    }
    property var pulseSummary: null
    property string pulseSignature: ""
    property bool pulseFailed: false
    property double nowMs: Date.now()
    readonly property string pinPath: (Quickshell.env("XDG_CONFIG_HOME") || Quickshell.env("HOME")+"/.config")+"/omarchy/ai-usage/pinned-limit.json"
    property var pinnedLimits: []
    property var pinnedUsages: ({})
    property bool pinSaveFailed: false
    // Overview sections fold down to their header, so a view can hold the two
    // sections a screenshot wants and skip whatever sits between them. The
    // choice is a view preference, not collected data: it has its own file, and
    // settings.json keeps its contract with the collector.
    readonly property var sectionIds: ["pinned","totals","account","hourly","accounts","cache","breakdown","year"]
    property var collapsedSections: []
    property bool viewStateDirty: false
    readonly property string viewStatePath: (Quickshell.env("XDG_CONFIG_HOME") || Quickshell.env("HOME")+"/.config")+"/omarchy/ai-usage/view-state.json"
    property int days: 1
    property string provider: "all"
    property string metric: "tokens"
    property var selection: ({})
    property var navigation: []
    readonly property var accountViews: data ? data.accountViews || [] : []
    readonly property string accountViewId: selection.account && provider !== "all" ? provider + ":" + selection.account : "all"
    readonly property var currentAccount: accountViews.find(a => a.id === accountViewId) || null
    readonly property var periodOptions: {
        var options = [1,7,30,90,365].map(n => ({id:String(n),name:n === 1 ? "Today" : n === 365 ? "Past year" : "Past " + n + " days"}))
        if (currentAccount) (currentAccount.quota.limits || []).forEach(w => {
            if (w.windowId && Date.parse(w.resetsAt) > nowMs)
                options.push({id:w.windowId,name:"Since reset · " + w.label})
        })
        if (selection.resetWindow && !options.some(o => o.id === selection.resetWindow))
            options.push({id:selection.resetWindow,name:"Reset window unavailable"})
        return options
    }
    function chooseAccount(id) {
        var account = accountViews.find(a => a.id === id)
        navigation = []
        provider = account ? account.provider : "all"
        selection = account ? {account:account.accountId} : ({})
        breakdown = "models"
        filtersOpen = false
        resetTableLimit()
        refresh()
    }
    function chooseResetWindow(id) {
        if (!currentAccount) return
        navigation = []
        var next = Object.assign({}, selection, {resetWindow:id})
        delete next.day; delete next.hourStart
        selection = next
        breakdown = "models"
        resetTableLimit(); refresh()
    }
    function choosePeriodOption(id) {
        if (/^\d+$/.test(id)) choosePeriod(Number(id))
        else chooseResetWindow(id)
    }
    function goBack() {
        if (!navigation.length) return
        var stack = navigation.slice(); var old = stack.pop(); navigation = stack
        selection = old.selection; provider = old.provider; breakdown = old.breakdown
        resetTableLimit(); refresh()
    }
    property int tableLimit: 8
    function breakdownPageSize() { return breakdown === "models" ? 8 : 20 }
    function resetTableLimit() { tableLimit = breakdownPageSize() }
    property bool filtersOpen: false
    property bool providerDetailsOpen: false
    property bool providerRowsExpanded: false
    property bool coverageOpen: false
    function choosePeriod(count) {
        days = count; navigation = []
        var keep = Object.assign({}, selection)
        delete keep.day; delete keep.hourStart; delete keep.resetWindow
        selection = keep
        resetTableLimit()
        refresh()
    }
    function filterBy(field, name, providerId) {
        // Narrowing the page leaves the reader where they are; an empty name
        // clears that field, which is what the All chips do.
        navigation = navigation.concat([{selection:selection,provider:provider,breakdown:breakdown}])
        var next = Object.assign({}, selection)
        if (name === "") delete next[field]; else next[field] = name
        if (field === "day") delete next.hourStart
        selection = next
        if (providerId) provider = providerId
        resetTableLimit(); refresh()
    }
    function drill(field, name, providerId) {
        // Opening a row also narrows the page, then shows the sessions behind it.
        filterBy(field, name, providerId)
        breakdown = "sessions"
    }
    function drillHour(start) {
        if (!data || Number(selection.hourStart) === Number(start) || !data.hourly.some(h => Number(h.start) === Number(start))) return
        navigation = navigation.concat([{selection:selection,provider:provider,breakdown:breakdown}])
        selection = Object.assign({}, selection, {day: Qt.formatDate(new Date(start*1000), "yyyy-MM-dd"), hourStart: Number(start)})
        breakdown = "sessions"
        resetTableLimit()
        refresh()
    }
    function modelChips(options, selectedName) {
        // The row stays switchable while a filter is on: the selected model keeps
        // its chip even when it falls outside the most-used ones.
        var top = options.slice(0, 8)
        if (selectedName && !top.some(o => o.id === selectedName)) {
            var extra = options.find(o => o.id === selectedName)
            if (extra) top = top.concat([extra])
        }
        return top
    }
    function clearSelection() {
        navigation = []
        var keep = {}
        if (selection.account) keep.account = selection.account
        if (selection.resetWindow) keep.resetWindow = selection.resetWindow
        selection = keep
        breakdown = "models"
        resetTableLimit(); refresh()
    }
    function chooseSource(id) {
        navigation = []
        var next = {}
        var excluded = (selection.excludeSource || []).filter(source => source !== id)
        if (id === "all" || !excluded.length) delete next.excludeSource
        else next.excludeSource = excluded
        selection = next
        provider = id
        resetTableLimit()
        refresh()
    }
    function isExcluded(id) { return (selection.excludeSource || []).indexOf(id) >= 0 }
    function toggleSource(id) {
        // A source switched off leaves the whole view without leaving the
        // collection: settings decides what this machine collects, this decides
        // what this view shows.
        navigation = navigation.concat([{selection:selection,provider:provider,breakdown:breakdown}])
        var list = (selection.excludeSource || []).slice()
        var at = list.indexOf(id)
        if (at >= 0) list.splice(at, 1); else list.push(id)
        var next = Object.assign({}, selection)
        if (list.length) next.excludeSource = list; else delete next.excludeSource
        selection = next
        if (at < 0 && provider === id) provider = "all"
        resetTableLimit(); refresh()
    }
    function filterText() {
        return Object.keys(selection).filter(k => k !== "resetWindow" && (k !== "account" || root.provider === "all")).map(function(k) {
            if (k === "account") return "account: "+((root.data.accountOptions.find(a=>a.id===root.selection[k]) || {}).label || root.selection[k])
            if (k === "excludeSource") return "excluded: "+root.selection[k].map(id=>root.providerName(id)).join(", ")
            if (k === "hourStart") return "hour: "+root.localClock(root.selection[k])
            return k+": "+root.selection[k]
        }).join(" · ")
    }
    function when(ts) { return ts ? Qt.formatDateTime(new Date(ts*1000), "MMM d, yyyy " + shortTimePattern) : "No recorded activity" }
    property string breakdown: "models"
    readonly property var breakdownItems: data ? (data[breakdown] || []) : []
    onBreakdownChanged: resetTableLimit()
    property bool settingsOpen: false
    property string settingsTab: "sources"
    property string error: ""
    property bool pending: false
    property bool pendingQuiet: true
    // A quiet scan refreshes the view already on screen, so the page stays
    // readable and scrollable while it runs. Only a scan for a view the reader
    // just asked for holds the page, since the rows shown are not theirs yet.
    property bool scanQuiet: true
    readonly property bool viewLoading: scan.running && !scanQuiet
    property bool viewDimmed: false
    onViewLoadingChanged: if (!viewLoading) viewDimmed = false
    property var themeData: null
    property bool themePending: false
    property var draftEnabled: ["codex", "claude", "opencode-go"]
    property string notice: ""
    property bool draftUpdateCheck: true
    readonly property var updateInfo: data && data.update ? data.update : ({available: false})
    property var providerOptions: data ? data.availableProviders || [] : []
    readonly property var machineAccounts: data && data.accountOptions ? data.accountOptions.filter(a => a.id.indexOf("machine:") === 0) : []
    property var draftPrices: ({})
    property var draftHomes: ({})
    property var draftAccounts: []
    property string draftLocalLabel: "Local"
    property string draftLedgerSyncDir: ""
    property string draftLedgerDeviceId: ""
    // The stored key never reaches this window; the report sends a mask, so
    // an untouched field means "keep what is stored" rather than an edit.
    property string draftOllamaKey: ""
    property string draftCommandCodeKey: ""
    property string draftClinePassKey: ""
    property string saveError: ""
    property var homeOptions: [
        {key:"codexHomes",name:"Codex homes",example:"/mnt/other-computer/.codex"},
        {key:"claudeHomes",name:"Claude homes",example:"/mnt/other-computer/.claude"},
        {key:"grokHomes",name:"Grok homes",example:"/mnt/other-computer/.grok"},
        {key:"geminiHomes",name:"Gemini homes",example:"/mnt/other-computer/.gemini"},
        {key:"opencodeHomes",name:"OpenCode data folders",example:"/mnt/other-computer/.local/share/opencode"},
        {key:"piHomes",name:"Pi agent folders",example:"/mnt/other-computer/.pi/agent"},
        {key:"ompHomes",name:"Oh My Pi agent folders",example:"/mnt/other-computer/.omp/agent"},
        {key:"museHomes",name:"Muse homes",example:"/mnt/other-computer/.local/share/muse"},
        {key:"hermesHomes",name:"Hermes homes",example:"/mnt/other-computer/.hermes"}]
    function providerName(id) { var p = providerOptions.find(p => p.id === id); return p ? p.name : id }
    function accountSources(account) {
        var names = []
        ;(account.directories || []).forEach(d => { var n = providerName(d.provider); if (names.indexOf(n) < 0) names.push(n) })
        return names.join(" + ")
    }
    function luminance(c) {
        function linear(v) { return v <= 0.04045 ? v/12.92 : Math.pow((v+0.055)/1.055,2.4) }
        return 0.2126*linear(c.r)+0.7152*linear(c.g)+0.0722*linear(c.b)
    }
    function colorFor(id) {
        var raw = ({codex: palette.bright_cyan || "#8cd3cb", claude: palette.bright_red || "#db9f9c",
            "opencode-go": palette.bright_yellow || "#e5c736", grok: palette.bright_blue || "#9cb8db",
            gemini: palette.bright_magenta || "#c6a0d5", opencode: palette.bright_green || "#a7c080",
            pi: palette.bright_white || palette.bright_foreground || "#d4d4d4", omp: palette.red || "#d88b68",
            muse: palette.blue || "#7aa2f7", "ollama-cloud": palette.orange || "#a2734b",
            "commandcode": palette.bright_green || "#a7c080", "clinepass": palette.cyan || "#2dd5b7",
            cursor: palette.magenta || "#c586c0"})[id] || root.ink
        var color=Qt.darker(raw,1), background=luminance(root.base)
        for (var i=0;i<16;i++) {
            var value=luminance(color)
            if ((Math.max(value,background)+0.05)/(Math.min(value,background)+0.05)>=4.5) break
            color=background>0.179 ? Qt.darker(color,1.15) : Qt.lighter(color,1.15)
        }
        return color
    }
    function accountColor(provider, shade) {
        var base = colorFor(provider)
        if (!shade) return base
        var factor = Math.min(1.6, 1 + 0.5 * shade)
        return luminance(root.base) > 0.179 ? Qt.darker(base, factor) : Qt.lighter(base, factor)
    }
    function compact(n) {
        n = Number(n || 0)
        return n >= 1e9 ? (n/1e9).toFixed(2)+"B" : n >= 1e6 ? (n/1e6).toFixed(1)+"M" : n >= 1e3 ? (n/1e3).toFixed(1)+"K" : String(Math.round(n))
    }
    function liveTodayView() {
        return !!pulseSummary && !!data && days === 1 && Object.keys(selection).length === 0
            && pulseSummary.date === Qt.formatDate(new Date(nowMs), "yyyy-MM-dd")
            && Number(pulseSummary.utcOffsetMinutes) === -new Date(nowMs).getTimezoneOffset()
    }
    function pulseTokens() {
        if (!liveTodayView()) return 0
        if (provider !== "all") return Number((pulseSummary.providers[provider] || {}).tokens || 0)
        return data.settings.enabled.reduce((sum, id) => sum + Number((pulseSummary.providers[id] || {}).tokens || 0), 0)
    }
    function pinnedName(pin) {
        if (!pin) return ""
        if (pin.provider === "codex") return "ChatGPT Main"
        if (pin.provider === "codex-second") return "ChatGPT Second"
        var usage = pinnedUsages[pin.provider]
        return usage && usage.name ? usage.name : pin.provider
    }
    function pinnedWindow(pin) {
        var usage = pin ? pinnedUsages[pin.provider] : null
        if (!pin || !usage || usage.id !== pin.provider) return null
        var matches = (usage.limits || []).filter(w => w.label === pin.label)
        if (matches.length === 1) return matches[0]
        return matches.find(w => String(w.title || "") === pin.title) || null
    }
    function pinnedResetText(value) {
        if (!value) return "Reset time not reported"
        var parsed = new Date(String(value).replace(/(\.\d{3})\d+/, "$1"))
        if (!isFinite(parsed.getTime())) return "Reset time unavailable"
        var relative = resetText(value)
        return (relative || "Awaiting reset update") + " · " + Qt.formatDateTime(parsed, "ddd " + shortTimePattern)
    }
    function unpinLimit(pin) {
        if (!pinSave.running) {
            pinSaveFailed = false
            pinSave.command = ["python3", helper, "pin", "--pin-provider", pin.provider,
                "--pin-label", pin.label, "--pin-title", pin.title, "--pin-remove"]
            pinSave.running = true
        }
    }
    function setPinnedUsage(id, usage) {
        var next = Object.assign({}, pinnedUsages)
        next[id] = usage
        pinnedUsages = next
    }
    function pulseStatus() {
        if (Quickshell.env("AI_USAGE_DEMO") === "1") return "Demo data · local updates disabled"
        if (pulseFailed) return "Local update unavailable"
        if (!pulseSummary) return "Checking local history"
        var age = Math.max(0, Math.floor((nowMs - Number(pulseSummary.generatedAt || 0) * 1000) / 1000))
        return age < 30 ? "Live local history · checked just now" : age < 60
            ? "Local history checked under 1m ago" : "Local history checked " + Math.floor(age / 60) + "m ago"
    }
    function acceptPulse(value) {
        if (!value || value.schemaVersion !== 1) return
        var signature = JSON.stringify([value.date, value.utcOffsetMinutes, value.providers, value.hours, value.unplacedTokens])
        var changed = pulseSignature !== "" && pulseSignature !== signature
        pulseSignature = signature
        pulseSummary = value
        pulseFailed = false
        nowMs = Date.now()
        if (changed && !live.running && !settingsOpen) refresh(true)
    }
    function money(n) { return "$" + Number(n || 0).toLocaleString(Qt.locale("en_US"), 'f', 2) }
    function creditAmount(n) { return Number(n).toLocaleString(Qt.locale("en_US"), 'f', Number(n) % 1 === 0 ? 0 : 2) }
    function shortDate(value) { return value ? Qt.formatDate(new Date(value+"T12:00:00"),"MMM d") : "" }
    function display(b) { return metric === "tokens" ? compact(b ? b.tokens : 0) : b && b.tokens > 0 && b.unpricedTokens === b.tokens ? "Unpriced" : money(b ? b.value : 0) }
    function amount(b) { return b ? (metric === "tokens" ? b.tokens : b.value) : 0 }
    function valueText(b) { return b.unpricedTokens === b.tokens && b.tokens > 0 ? "Unpriced" : money(b.value) + (b.unpricedTokens ? " + unpriced" : "") }
    function comparisonText() {
        if (!data || selection.hourStart || selection.resetWindow) return ""
        var current = Number(data.summary.tokens || 0), previous = Number(data.previous.tokens || 0)
        var span = selection.day ? "previous day" : days === 1 ? "yesterday so far" : "previous period"
        if (previous <= 0) return "No " + span + " baseline"
        var ratio = current / previous
        if (ratio >= 10) return "↑ " + Math.round(ratio) + "× vs " + span
        return (current >= previous ? "↑ " : "↓ ") + Math.abs((ratio - 1) * 100).toFixed(1) + "% vs " + span
    }
    function sectionFolded(id) { return collapsedSections.indexOf(id) >= 0 }
    function setSectionFolded(id, folded) {
        if (sectionIds.indexOf(id) < 0) return
        var at = collapsedSections.indexOf(id)
        if (folded ? at >= 0 : at < 0) return
        var next = collapsedSections.slice()
        if (folded) next.push(id); else next.splice(at, 1)
        collapsedSections = next
        viewStateDirty = true
        viewStateFile.setText(JSON.stringify({collapsed: next}))
    }
    function toggleSection(id) { setSectionFolded(id, !sectionFolded(id)) }
    function unfoldAllSections() {
        if (!collapsedSections.length) return
        collapsedSections = []
        viewStateDirty = true
        viewStateFile.setText(JSON.stringify({collapsed: []}))
    }
    function acceptViewState(raw) {
        // A fold this window already made outranks the file it read at startup,
        // which may predate the change.
        if (viewStateDirty) return
        var list = []
        try {
            var value = JSON.parse(raw)
            if (value && Array.isArray(value.collapsed)) list = value.collapsed
        } catch(e) {}
        collapsedSections = list.filter(id => sectionIds.indexOf(id) >= 0)
    }
    // What a folded section says about itself, so folding hides detail without
    // hiding the number that made it worth a look.
    function metricText(value) { return metric === "tokens" ? compact(value) : money(value) }
    function seriesTokens(series) { return series.reduce((sum, row) => sum + amount(row.total), 0) }
    function plural(noun, count) { return count === 1 ? noun.replace(/s$/, "") : noun }
    function pinnedSummary() {
        if (!pinnedLimits.length) return ""
        return pinnedLimits.map(pin => {
            var limit = pinnedWindow(pin)
            return pinnedName(pin) + (limit ? " " + Math.round(Number(limit.percent || 0) * 100) + "%" : "")
        }).join(" · ")
    }
    function totalsSummary() {
        return data ? compact(data.summary.tokens) + " processed · " + valueText(data.summary) : ""
    }
    function accountSummary() {
        if (!currentAccount) return ""
        var limits = currentAccount.quota.limits || []
        if (!limits.length) return currentAccount.quota.error || "No limits reported"
        return (limits[0].title || limits[0].label) + " " + Math.round(Number(limits[0].percent || 0) * 100) + "% used"
    }
    function hourlySummary() {
        if (!data) return ""
        var rows = data.hourly.length ? data.hourly : data.daily
        var unit = data.hourly.length ? "hour" : "day"
        return rows.length + " " + (rows.length === 1 ? unit : unit + "s") + " · "
            + metricText(seriesTokens(rows)) + (metric === "tokens" ? " tokens" : "")
    }
    function accountsSummary() {
        if (!data) return ""
        return data.cards.length + " " + plural("accounts", data.cards.length) + " · " + compact(data.summary.tokens) + " tokens"
    }
    function cacheSummary() {
        return data ? compact(data.summary.output) + " output · " + money(data.summary.cacheSavings) + " saved" : ""
    }
    function breakdownSummary() {
        if (!data) return ""
        var count = breakdownItems.length
        if (!count) return "Nothing recorded in this period"
        return count + " " + plural(breakdown, count) + " · " + compact(seriesTokens(breakdownItems)) + " tokens"
    }
    function yearSummary() {
        if (!data) return ""
        var total = 0
        for (var key in data.heatmap) total += Number(data.heatmap[key] || 0)
        return compact(total) + " tokens in the past year"
    }
    function routeCount(row) {
        // A Models row can stand for several routes. Say so, rather than letting
        // one row read as one route's traffic.
        return root.breakdown === "models" && row.routes ? row.routes.length : 0
    }
    function recordedAs(row) {
        // Each route records the model string its own API returns, so when a row
        // carries one spelling that is not the grouped name, keep it reachable.
        if (root.breakdown !== "models" || !row.routes || row.routes.length !== 1) return ""
        return row.routes[0].model !== row.name ? "Recorded as " + row.routes[0].model : ""
    }
    function exploreRow(row) {
        if (root.breakdown === "sessions") return
        var field = root.breakdown === "models" ? "model" : root.breakdown === "projects" ? "project" : root.breakdown === "routes" ? "apiProvider" : root.breakdown === "accounts" ? "account" : "client"
        // A row that spans routes has no single route to scope the drill to, and
        // the report matches the whole model family either way.
        root.drill(field, root.breakdown === "accounts" ? row.accountId : row.name, root.routeCount(row) > 1 ? "" : row.provider)
    }
    function refresh(quiet = false) {
        if (scan.running) {
            if (!quiet) scanQuiet = false
            pendingQuiet = pending ? pendingQuiet && quiet : quiet
            pending = true
            return
        }
        if (!quiet) error = ""
        scanQuiet = quiet
        scan.command = ["python3", helper, "report", "--days", String(days), "--provider", provider]
        for (var field in selection) {
            // A list-valued filter (excluded sources) repeats its flag once per
            // entry, which is how the collector's own argument takes them.
            var value = selection[field]
            if (Array.isArray(value)) {
                for (var i = 0; i < value.length; i++) scan.command = scan.command.concat(["--"+field, String(value[i])])
            } else scan.command = scan.command.concat(["--"+field, String(value)])
        }
        scan.running = true
    }
    function refreshLive(quiet = true) {
        if (!live.running) live.running = true
        root.refresh(quiet)
    }
    function refreshPulse() {
        if (Quickshell.env("AI_USAGE_DEMO") !== "1" && window.visible && !settingsOpen && !live.running && !pulse.running)
            pulse.running = true
    }
    function refreshTheme() {
        if (themeScan.running) { themePending = true; return }
        themeScan.running = true
    }
    function resetText(value) {
        var seconds = (Date.parse(value) - Date.now()) / 1000
        // No timestamp is not a state worth a permanent line: providers that
        // report a share without a reset time (Ollama Cloud) would otherwise
        // carry "Reset unavailable" under every meter forever, and the bar
        // panel already omits the line for the same case.
        if (!isFinite(seconds)) return ""
        if (seconds <= 0) return "Awaiting reset update"
        return "Resets in " + (seconds >= 86400 ? Math.floor(seconds/86400)+"d " : "") + (seconds >= 3600 ? Math.floor(seconds/3600)%24+"h" : Math.ceil(seconds/60)+"m")
    }
    function quotaAge(q) {
        var age = (Date.now() - Date.parse(q.updatedAt))/60000
        return !isFinite(age) ? "No quota update" : age > 30 ? "Stale quota · " + Math.floor(age) + "m ago" : "Quota updated " + Math.max(0, Math.floor(age)) + "m ago"
    }
    function openSettings() {
        notice = ""
        var s = data ? data.settings : {}
        draftUpdateCheck = s.updateCheck !== false
        draftEnabled = (s.enabled || ["codex", "claude", "opencode-go"]).slice()
        draftAccounts = JSON.parse(JSON.stringify(s.accounts || []))
        draftLocalLabel = s.localAccountLabel || "Local"
        draftLedgerSyncDir = s.ledgerSyncDir || ""
        draftLedgerDeviceId = s.ledgerDeviceId || ""
        draftOllamaKey = s.ollamaApiKey || ""
        draftCommandCodeKey = s.commandcodeApiKey || ""
        draftClinePassKey = s.clinepassApiKey || ""
        var prices = {}, homes = {}
        providerOptions.forEach(p => prices[p.id] = s.monthlyPrices && s.monthlyPrices[p.id] !== undefined ? String(s.monthlyPrices[p.id]) : "")
        draftAccounts.forEach(a => prices[a.id] = s.monthlyPrices && s.monthlyPrices[a.id] !== undefined ? String(s.monthlyPrices[a.id]) : "")
        homeOptions.forEach(h => homes[h.key] = (s[h.key] || []).join("\n"))
        draftPrices = prices
        draftHomes = homes
        opacitySlider.value = s.windowOpacity || 0.985
        settingsOpen = true
    }
    function saveSettings() {
        var prices = {}
        var fields = draftPrices
        for (var p in fields) {
            if (fields[p].trim() === "") continue
            var n = Number(fields[p])
            if (!isFinite(n) || n < 0 || n > 100000) { notice = "Enter a valid monthly price, or leave it blank."; return }
            prices[p] = n
        }
        saveError = ""
        var s = {updateCheck: draftUpdateCheck, accounts: draftAccounts, localAccountLabel: draftLocalLabel, enabled: draftEnabled, monthlyPrices: prices, windowOpacity: opacitySlider.value, clinepassApiKey: draftClinePassKey,
                 ledgerSyncDir: draftLedgerSyncDir, ledgerDeviceId: draftLedgerDeviceId, ollamaApiKey: draftOllamaKey, commandcodeApiKey: draftCommandCodeKey}
        homeOptions.forEach(h => s[h.key] = (draftHomes[h.key] || "").split("\n").filter(x => x.trim()).map(x => x.trim()))
        save.command = ["python3", helper, "settings", "--save"]
        save.payload = JSON.stringify(s)
        save.running = true
    }
    Process {
        id: scan
        stdout: StdioCollector { onStreamFinished: {
            try {
                root.data = JSON.parse(text); root.error = ""
                if (root.requestedAgent !== "") {
                    var id = root.requestedAgent; root.requestedAgent = ""
                    root.openAgent(id)
                }
            } catch(e) { root.error = "Could not load usage data. Try refreshing." }
        } }
        stderr: StdioCollector { onStreamFinished: { if (text.trim()) console.warn(text.trim()) } }
        onExited: function(code) {
            if (code !== 0) root.error = "The scan failed. Previously loaded data is still shown."
            if (root.pending) { root.pending = false; root.refresh(root.pendingQuiet) }
        }
    }
    Process {
        id: save
        // The payload rides stdin, never argv: it can carry an API key, and a
        // command line is world-readable in /proc while the process lives.
        property string payload: ""
        stdinEnabled: true
        onStarted: { write(payload + "\n"); payload = "" }
        stdout: StdioCollector { onStreamFinished: { try { root.saveError = JSON.parse(text).error || "" } catch(e) {} } }
        onExited: function(code) {
            if (code === 0) { root.settingsOpen = false; root.selection = ({}); root.navigation = []; root.provider = "all"; root.notice = "Settings saved"; root.refreshLive(false) }
            else root.notice = root.saveError || "Settings could not be saved."
        }
    }
    Timer {
        // A save that never returns would leave Save preferences disabled for
        // the life of the window, which reads as a button that does nothing.
        // Ask it to stop first, then insist: killing it exits nonzero, so the
        // window reports the failure and the button comes back. A local
        // settings write has nothing to wait on.
        property int attempts: 0
        running: save.running; interval: 15000; repeat: true
        onTriggered: { attempts += 1; if (attempts > 1) save.signal(9); else save.running = false }
        onRunningChanged: if (!running) attempts = 0
    }
    Process {
        id: live
        command: Quickshell.env("AI_USAGE_DEMO") === "1" ? ["python3", helper, "report"] : ["bash", helper.replace(/collector\.py$/, "refresh.sh"), "--force"]
        onExited: { pulseFile.reload(); root.refresh(true); root.refreshPulse() }
    }
    Process {
        id: pulse
        command: ["python3", helper, "pulse"]
        stdout: StdioCollector { onStreamFinished: {
            try { root.acceptPulse(JSON.parse(text)) } catch(e) { root.pulseFailed = true }
        } }
        onExited: function(code) { pulseFile.reload(); if (code !== 0) root.pulseFailed = true }
    }
    Process {
        id: pinSave
        onExited: function(code) {
            pinSaveFailed = code !== 0
            pinFile.reload()
        }
    }
    Process {
        id: themeScan
        command: ["python3", helper, "theme"]
        stdout: StdioCollector { onStreamFinished: {
            try { var t = JSON.parse(text); if (t.palette && Object.keys(t.palette).length) root.themeData = t } catch(e) {}
        } }
        onExited: function(code) {
            if (root.themePending) { root.themePending = false; root.refreshTheme() }
        }
    }
    Timer { interval: 300000; repeat: true; running: window.visible; onTriggered: root.refresh(true) }
    // A view scan that answers quickly swaps in without a flash of dimming.
    Timer { interval: 250; running: root.viewLoading; onTriggered: root.viewDimmed = true }
    Timer { interval: 15000; repeat: true; running: window.visible && !root.settingsOpen; onTriggered: root.refreshPulse() }
    Timer { interval: 5000; repeat: true; running: window.visible; onTriggered: root.nowMs = Date.now() }
    Timer { interval: 5000; repeat: true; running: window.visible; onTriggered: pinFile.reload() }
    FileView {
        id: pinFile
        path: root.pinPath
        watchChanges: true; atomicWrites: true; printErrors: false
        onFileChanged: reload()
        onLoaded: {
            try {
                var value = JSON.parse(text())
                var entries = value && Array.isArray(value.pins) ? value.pins : [value]
                var next = entries.filter(pin => pin && typeof pin.provider === "string"
                    && /^[a-z0-9][a-z0-9-]{0,79}$/.test(pin.provider)
                    && typeof pin.label === "string" && pin.label.length > 0
                    && (pin.title === undefined || typeof pin.title === "string"))
                    .map(pin => ({provider:pin.provider,label:pin.label,title:pin.title || ""})).slice(0, 3)
                if (JSON.stringify(root.pinnedLimits) !== JSON.stringify(next)) root.pinnedLimits = next
            } catch(e) { root.pinnedLimits = [] }
        }
        onLoadFailed: root.pinnedLimits = []
    }
    Instantiator {
        model: root.pinnedLimits
        delegate: FileView {
            required property var modelData
            path: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME")+"/.local/state")
                + "/omarchy/agents/usage/" + modelData.provider + ".json"
            watchChanges: true; atomicWrites: true; printErrors: false
            onPathChanged: if (path) reload()
            onFileChanged: reload()
            onLoaded: {
                try { root.setPinnedUsage(modelData.provider, JSON.parse(text())) }
                catch(e) { root.setPinnedUsage(modelData.provider, null) }
            }
            onLoadFailed: root.setPinnedUsage(modelData.provider, null)
        }
    }
    FileView {
        id: pulseFile
        path: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME")+"/.local/state")+"/omarchy/ai-usage/hourly-summary.json"
        watchChanges: true; atomicWrites: true; printErrors: false
        onFileChanged: reload()
        onLoaded: { try { root.acceptPulse(JSON.parse(text())) } catch(e) {} }
    }
    FileView {
        // Which overview sections this window remembers as folded. Its own file
        // beside the other config files: the collector never reads it, so the
        // settings and quota contracts stay as they are.
        id: viewStateFile
        path: root.viewStatePath
        watchChanges: true; atomicWrites: true; printErrors: false
        onFileChanged: reload()
        onLoaded: root.acceptViewState(text())
        onLoadFailed: root.acceptViewState("")
    }
    FileView {
        // current/ is stable while omarchy theme set replaces current/theme by
        // rename, so this watcher keeps firing after each swap. The file
        // watchers below only survive in-place edits. A theme change repaints
        // through the fast theme command, not a history scan.
        path: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME")+"/.local/state")+"/omarchy/current"
        watchChanges: true; printErrors: false
        onFileChanged: root.refreshTheme()
    }
    FileView {
        path: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME")+"/.local/state")+"/omarchy/current/theme/colors.toml"
        watchChanges: true; printErrors: false
        onFileChanged: root.refreshTheme()
    }
    FileView {
        path: Quickshell.env("HOME")+"/.config/omarchy/shell.toml"
        watchChanges: true; printErrors: false
        onFileChanged: root.refresh(true)
    }
    FileView {
        path: (Quickshell.env("XDG_CONFIG_HOME") || Quickshell.env("HOME")+"/.config")+"/omarchy/shell.json"
        watchChanges: true; atomicWrites: true; printErrors: false
        onFileChanged: reload()
        onLoaded: root.loadClockPattern(text())
        onLoadFailed: root.loadClockPattern("")
    }
    IpcHandler {
        target: "analytics"
        function quit(): void { Qt.quit() }
        function refresh(): void { root.refreshLive() }
        function showWindow(): int {
            // Hot reload or a compositor close can leave visible true without a mapped window.
            if (!window.backingWindowVisible) window.visible = false
            Qt.callLater(function() { window.visible = true; root.refresh(true) })
            return Quickshell.processId
        }
        function capture(path: string): void { captureRoot.grabToImage(result => result.saveToFile(path)) }
        function captureTooltip(path: string): void { chartTip.contentItem.grabToImage(result => result.saveToFile(path)) }
        function account(id: string): void { root.drill("account", id, "") }
        function accountView(id: string): void { root.chooseAccount(id) }
        function agent(id: string): void {
            root.openAgent(id)
        }
        function resetWindow(id: string): void { root.chooseResetWindow(id) }
        function model(name: string): void { root.filterBy("model", name, "") }
        function toggleSource(id: string): void { root.toggleSource(id) }
        function metric(name: string): void { root.metric = name }
        function fold(id: string): void { root.setSectionFolded(id, true) }
        function unfold(id: string): void { root.setSectionFolded(id, false) }
        function unfoldAll(): void { root.unfoldAllSections() }
        function preferences(): void { root.openSettings() }
        function overview(): void { root.settingsOpen = false }
        function period(days: int): void { root.choosePeriod(days) }
        function firstHour(): void {
            if (!root.data) return
            var hour = root.data.hourly.find(h => h.total.tokens > 0)
            if (hour) root.drillHour(hour.start)
        }
        function filters(): void { root.filtersOpen = true }
        function scrollTo(y: int): void { scroll.contentItem.contentY = y }
        function tooltip(): void { chart.hovered=2; chart.pointerX=chart.width/2; chartTip.open() }
        function clearTooltip(): void { chart.hovered=-1 }
    }

    component Label: Text {
        color: root.ink
        font.family: root.fontFamily
        font.pixelSize: 13
        renderType: Text.NativeRendering
    }
    component Sub: Label { color: root.muted; font.pixelSize: 12 }
    component Choice: Button {
        id: control
        property bool selected: false
        // A source switched off in a filter row: dimmed and struck through, so a
        // view that leaves something out looks like one at a glance.
        property bool excluded: false
        implicitHeight: 34
        leftPadding: 14; rightPadding: 14
        Accessible.name: text
        background: Rectangle {
            radius: 3
            color: control.excluded ? Qt.alpha(root.ink,0.03) : control.down ? Qt.alpha(root.ink,0.18) : control.selected ? Qt.alpha(root.ink,0.13) : control.hovered ? Qt.alpha(root.ink,0.07) : "transparent"
            Behavior on color { ColorAnimation { duration: 110 } }
            border.color: control.activeFocus ? root.accent : control.excluded ? Qt.alpha(root.ink,0.08) : control.selected ? Qt.alpha(root.ink,0.26) : "transparent"
        }
        contentItem: Label { text: control.text; color: control.excluded ? Qt.alpha(root.ink,0.42) : control.selected ? root.bright : root.muted; font.pixelSize: 12; font.strikeout: control.excluded; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
    }
    component ScopePicker: ComboBox {
                    id: picker
                    property string value: "all"
                    signal chosen(string id)
                    textRole: "name"; valueRole: "id"
                    currentIndex: model.findIndex(p => p.id === value)
                    onModelChanged: currentIndex = model.findIndex(p => p.id === value)
                    onValueChanged: currentIndex = model.findIndex(p => p.id === value)
                    font.family: root.fontFamily; font.pixelSize: 12; implicitHeight: 34
                    onActivated: chosen(currentValue)
                    background: Rectangle {
                        radius: 3
                        color: picker.down ? Qt.alpha(root.ink,0.18) : picker.hovered ? Qt.alpha(root.ink,0.08) : root.surface
                        border.color: picker.activeFocus ? root.accent : root.edge
                    }
                    contentItem: Label {
                        leftPadding: 14; rightPadding: 32
                        text: picker.displayText
                        color: root.ink
                        font.pixelSize: 12
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                    indicator: Label {
                        x: picker.width - width - 12
                        y: (picker.height-height)/2
                        text: "⌄"
                        color: root.muted
                        font.pixelSize: 16
                    }
                    delegate: ItemDelegate {
                        required property var modelData
                        required property int index
                        width: picker.width - 8; height: 34
                        highlighted: picker.highlightedIndex === index
                        contentItem: Label {
                            text: modelData.name
                            color: parent.highlighted ? root.bright : root.ink
                            font.pixelSize: 12
                            verticalAlignment: Text.AlignVCenter
                            leftPadding: 10
                            elide: Text.ElideRight
                        }
                        background: Rectangle { radius: 3; color: parent.highlighted ? Qt.alpha(root.ink,0.12) : "transparent" }
                    }
                    popup: Popup {
                        y: picker.height + 4
                        width: picker.width
                        implicitHeight: Math.min(contentItem.implicitHeight + 8, 8 * 34 + 8)
                        padding: 4
                        background: Rectangle { color: root.base; border.color: root.edge; radius: 3 }
                        contentItem: ListView {
                            clip: true
                            implicitHeight: contentHeight
                            model: picker.popup.visible ? picker.delegateModel : null
                            currentIndex: picker.highlightedIndex
                            ScrollIndicator.vertical: ScrollIndicator {}
                        }
                    }
    }
    component Card: Rectangle { color: root.surface; border.color: root.edge; radius: 4 }
    component Section: Rectangle {
        id: section
        // A card whose body folds away behind a header that stays. The header
        // names the section, keeps the section's own controls, and is the way
        // back; folded, it reports what it holds rather than leaving a blank
        // line where a chart used to be.
        property string sectionId: ""
        property string title: ""
        property string summary: ""
        property bool collapsible: true
        readonly property bool folded: collapsible && root.sectionFolded(sectionId)
        property bool folding: false
        default property alias body: bodyColumn.data
        property alias headerItems: headerActions.data
        color: root.surface; border.color: root.edge; radius: 4
        implicitHeight: stack.implicitHeight + 36
        // Clip only while the fold animates: a card that clipped always would
        // cut off the tooltips that open below their row.
        clip: folding
        Behavior on implicitHeight { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
        onFoldedChanged: { folding = true; foldTimer.restart() }
        Timer { id: foldTimer; interval: 170; onTriggered: section.folding = false }

        Column {
            id: stack
            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: 18
            spacing: 12
            Item {
                id: header
                width: parent.width
                implicitHeight: headerRow.implicitHeight + 8
                activeFocusOnTab: section.collapsible
                Accessible.role: Accessible.Button
                Accessible.name: (section.folded ? "Show " : "Hide ") + section.title
                Accessible.onPressAction: if (section.collapsible) root.toggleSection(section.sectionId)
                Keys.onReturnPressed: if (section.collapsible) root.toggleSection(section.sectionId)
                Keys.onSpacePressed: if (section.collapsible) root.toggleSection(section.sectionId)
                Rectangle {
                    anchors.fill: parent; anchors.margins: -5
                    color: "transparent"; radius: 4
                    border.color: header.activeFocus ? root.accent : "transparent"
                }
                // Behind the row, so a control in the header keeps its own click
                // and every other point on the row folds the section.
                MouseArea {
                    id: headerClick
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.toggleSection(section.sectionId)
                }
                RowLayout {
                    id: headerRow
                    anchors.fill: parent
                    spacing: 12
                    Label {
                        text: section.title; font.pixelSize: 16; font.weight: Font.DemiBold
                        elide: Text.ElideRight; Layout.fillWidth: true
                    }
                    RowLayout { id: headerActions; spacing: 8; visible: !section.folded }
                    Sub {
                        visible: section.folded
                        text: section.summary
                        elide: Text.ElideRight; horizontalAlignment: Text.AlignRight
                        Layout.fillWidth: true
                    }
                    Item {
                        id: foldGrip
                        visible: section.collapsible
                        implicitWidth: 20; implicitHeight: 20
                        // A crowded header (a segmented control, six breakdown
                        // chips) needs the grip to read as its own control
                        // rather than as part of the last one.
                        Layout.leftMargin: 8
                        Rectangle {
                            anchors.fill: parent; anchors.margins: -2; radius: 3
                            color: headerClick.containsMouse ? Qt.alpha(root.ink, 0.08) : "transparent"
                            Behavior on color { ColorAnimation { duration: 110 } }
                        }
                        Canvas {
                            anchors.centerIn: parent
                            width: 20; height: 20
                            // Right when folded, down when open: the header is the
                            // control, so the triangle only has to say which way.
                            rotation: section.folded ? 0 : 90
                            Behavior on rotation { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
                            property color tint: headerClick.containsMouse ? root.bright : root.muted
                            onTintChanged: requestPaint()
                            onPaint: {
                                var ctx = getContext("2d"); ctx.reset()
                                ctx.translate(width/2, height/2)
                                ctx.strokeStyle = tint; ctx.lineWidth = 1.7
                                ctx.lineCap = "round"; ctx.lineJoin = "round"
                                ctx.beginPath(); ctx.moveTo(-2.8, -4.8); ctx.lineTo(2.2, 0); ctx.lineTo(-2.8, 4.8); ctx.stroke()
                            }
                        }
                    }
                }
            }
            Column { id: bodyColumn; width: parent.width; spacing: 12; visible: !section.folded }
        }
    }
    component HoverTip: ToolTip {
        id: tip
        property string heading: ""
        property string detail: ""
        property var rows: []
        delay: 120
        timeout: -1
        margins: 12
        implicitWidth: 330
        closePolicy: Popup.NoAutoClose
        enter: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 100 } }
        exit: Transition { NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 80 } }
        background: Item {}
        padding: 0
        contentItem: Rectangle {
            implicitWidth: 330
            implicitHeight: tooltipColumn.implicitHeight+32
            color: root.base
            border.color: Qt.alpha(root.ink,0.28)
            radius: 4
            Rectangle { x: 1; y: 1; width: parent.width-2; height: 2; color: Qt.alpha(root.accent,0.65) }
            Column {
                id: tooltipColumn
                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 16
                spacing: 11
                Label { width: parent.width; text: tip.heading; wrapMode: Text.Wrap; font.pixelSize: 12; font.weight: Font.Medium; color: root.bright }
                Rectangle { width: parent.width; height: 1; color: root.edge; visible: tip.rows.length>0 }
                Repeater {
                    model: tip.rows
                    RowLayout {
                        required property var modelData
                        width: tooltipColumn.width; spacing: 12
                        Rectangle { implicitWidth: 5; implicitHeight: 5; color: modelData.color || root.accent; radius: 1 }
                        Sub { text: modelData.label; Layout.fillWidth: true; font.pixelSize: 11 }
                        Label { text: modelData.value; font.pixelSize: 12 }
                    }
                }
                Sub { width: parent.width; text: tip.detail; visible: text!==""; wrapMode: Text.Wrap; font.pixelSize: 10 }
            }
        }
    }
    component QuietScrollBar: ScrollBar {
        id: control
        policy: ScrollBar.AsNeeded
        padding: 1
        implicitWidth: 7
        background: Item {}
        contentItem: Rectangle {
            implicitWidth: 4; implicitHeight: 30; radius: 2
            color: control.pressed ? root.accent : Qt.alpha(root.ink,control.hovered?0.4:0.2)
            opacity: control.active || control.hovered ? 1 : 0.45
            Behavior on opacity { NumberAnimation { duration: 150 } }
        }
    }
    component Field: TextField {
        color: root.ink; font.family: root.fontFamily; font.pixelSize: 13; placeholderTextColor: root.muted
        selectionColor: Qt.alpha(root.accent,0.4); selectedTextColor: root.ink
        padding: 12; selectByMouse: true
        background: Rectangle { radius: 3; color: Qt.alpha(root.base, 0.5); border.color: parent.activeFocus ? root.accent : root.edge }
    }
    component Homes: TextArea {
        color: root.ink; font.family: root.fontFamily; font.pixelSize: 12; placeholderTextColor: root.muted
        selectionColor: Qt.alpha(root.accent,0.4); selectedTextColor: root.ink
        padding: 12; selectByMouse: true; wrapMode: TextEdit.NoWrap
        background: Rectangle { radius: 3; color: Qt.alpha(root.base, 0.5); border.color: parent.activeFocus ? root.accent : root.edge }
    }
    FloatingWindow {
        id: window
        title: "Agent Pulse"
        color: "transparent"
        implicitWidth: 1200
        implicitHeight: 900
        minimumSize: Qt.size(1000, 640)
        visible: true
        Component.onCompleted: root.refreshLive()
        Shortcut { sequence: "Alt+Left"; onActivated: root.goBack() }
        Shortcut { sequence: "Ctrl+Q"; onActivated: Qt.quit() }
        Shortcut { sequence: "Ctrl+R"; onActivated: root.refreshLive() }
        Shortcut { sequence: "Escape"; onActivated: { if(root.settingsOpen) root.settingsOpen = false; else if(root.navigation.length) root.goBack(); else window.visible = false } }
        Rectangle {
            id: captureRoot
            anchors.fill: parent
            color: root.bg
        ColumnLayout {
            id: dashboardBody
            anchors.fill: parent
            anchors.margins: 26
            spacing: 18
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Rectangle {
                    implicitWidth: 36; implicitHeight: 36; radius: 3; color: Qt.alpha(root.accent, 0.14)
                    Label { anchors.centerIn: parent; text: "󱚣"; color: root.ink; font.pixelSize: 24 }
                }
                Column {
                    Layout.fillWidth: true; Layout.minimumWidth: 150
                    spacing: 3
                    Label { text: root.settingsOpen ? "Preferences" : "Agent Pulse"; font.pixelSize: 21; font.weight: Font.Medium }
                    Sub { width: parent.width; elide: Text.ElideRight; text: Quickshell.env("AI_USAGE_DEMO") === "1" ? "Demo data · no local history" : (root.data ? root.data.settings.enabled.map(p => root.providerName(p)).join(" · ") : "Local agent history") }
                }
                Item { Layout.fillWidth: true }
                Sub { Layout.maximumWidth: 340; elide: Text.ElideRight; text: root.viewLoading || live.running ? "Updating usage…" : root.liveTodayView() && !root.pulseFailed && Quickshell.env("AI_USAGE_DEMO") !== "1" ? "Today · local history live" : root.data ? (root.selection.resetWindow ? "Since reset · " + root.data.period.resetLabel : root.days === 1 ? "Today" : root.data.period.start + "  to  " + root.data.period.end) : "Loading…" }
                Choice { text: root.settingsOpen ? "Cancel" : "Settings"; onClicked: root.settingsOpen ? root.settingsOpen = false : root.openSettings() }
                Choice { visible: root.settingsOpen; text: "Save preferences"; selected: true; enabled: !save.running; onClicked: root.saveSettings() }
                Choice { visible: !root.settingsOpen; text: "Refresh"; enabled: !root.viewLoading && !live.running; onClicked: root.refreshLive() }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: root.edge }
            ColumnLayout {
                Layout.fillWidth: true
                visible: !root.settingsOpen && root.updateInfo.available === true
                Label { text: "Agent Pulse " + (root.updateInfo.latest || "") + " is available"; color: root.accent }
                Sub { text: "Run this command in a terminal to update. History and preferences are preserved."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                TextArea {
                    Layout.fillWidth: true
                    text: root.updateInfo.command || ""
                    readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere
                    font.pixelSize: 12; color: root.ink
                    background: Rectangle { color: root.surface; radius: 6 }
                    Accessible.name: "Update command"
                }
            }

            Label { visible: root.notice !== ""; text: root.notice; Layout.fillWidth: true; wrapMode: Text.WordWrap; color: root.accent }
            Label { visible: root.error !== ""; text: root.error; color: root.colorFor("claude"); Layout.fillWidth: true; wrapMode: Text.WordWrap }
            Label { visible: !!root.data && !!root.data.period.error; text: root.data ? root.data.period.error : ""; color: root.colorFor("claude"); Layout.fillWidth: true; wrapMode: Text.WordWrap }
            RowLayout {
                visible: !root.settingsOpen
                Layout.fillWidth: true
                spacing: 10
                Sub { text: "Account" }
                ScopePicker {
                    id: accountPicker
                    Layout.preferredWidth: 300
                    model: [{id:"all",name:"All accounts"}].concat(root.accountViews)
                    value: root.accountViewId
                    Accessible.name: "Account"
                    onChosen: id => root.chooseAccount(id)
                }
                Sub { text: "Period" }
                ScopePicker {
                    id: periodPicker
                    Layout.preferredWidth: 275
                    model: root.periodOptions
                    value: root.selection.resetWindow || String(root.days)
                    Accessible.name: "Period"
                    onChosen: id => root.choosePeriodOption(id)
                }
                Choice { text: root.filtersOpen ? "Hide filters" : "Filters" + (root.filterText() !== "" ? " · active" : ""); onClicked: root.filtersOpen = !root.filtersOpen }
                Item { Layout.fillWidth: true }
            }
            RowLayout {
                visible: !root.settingsOpen && root.filtersOpen && !root.selection.account
                Sub { text: "Source" }
                ScopePicker {
                    id: sourcePicker
                    Layout.preferredWidth: 220
                    model: [{id:"all",name:"All sources"}].concat(root.data ? root.data.settings.enabled.map(p => ({id:p,name:root.providerName(p)})) : [])
                    value: root.provider
                    Accessible.name: "Source focus"
                    onChosen: id => root.chooseSource(id)
                }
            }
            Flow { Layout.fillWidth: true; spacing: 8; visible: !root.settingsOpen && root.filtersOpen && !!root.data && (root.data.modelOptions.length > 1 || !!root.selection.model)
                Choice { text: "All models"; selected: !root.selection.model; onClicked: root.filterBy("model","") }
                Repeater { model: root.data ? root.modelChips(root.data.modelOptions, root.selection.model) : []
                    Choice { required property var modelData; text: modelData.name; selected: root.selection.model===modelData.id; onClicked: root.filterBy("model",modelData.id,"") }
                }
            }
            Flow { Layout.fillWidth: true; spacing: 8; visible: !root.settingsOpen && root.filtersOpen && !!root.data && !root.selection.account
                Sub { text: "Include sources"; topPadding: 9; rightPadding: 6 }
                Repeater { model: root.data ? root.data.settings.enabled : []
                    Choice { required property string modelData; text: root.providerName(modelData); selected: !root.isExcluded(modelData); excluded: root.isExcluded(modelData); onClicked: root.toggleSource(modelData) }
                }
            }
            Sub { Layout.fillWidth: true; visible: !root.settingsOpen && !!root.data && !!root.data.accountWarning; text: root.data ? root.data.accountWarning : ""; wrapMode: Text.WordWrap }
            RowLayout {
                visible: !root.settingsOpen && root.filterText() !== ""
                Layout.fillWidth: true
                Label { Layout.fillWidth: true; elide: Text.ElideMiddle; text: root.filterText() }
                Choice { visible: root.navigation.length > 0; text: "Back"; onClicked: root.goBack() }
                Choice { text: "Clear filters"; onClicked: root.clearSelection() }
            }
            ScrollView {
                id: scroll
                ScrollBar.vertical: QuietScrollBar { parent: scroll; x: scroll.width-width; height: scroll.height }
                visible: !root.settingsOpen
                enabled: !root.viewLoading
                opacity: root.viewDimmed && root.data ? 0.6 : 1
                Behavior on opacity { NumberAnimation { duration: 120 } }
                Layout.fillWidth: true; Layout.fillHeight: true
                contentWidth: availableWidth
                clip: true
                Column {
                    width: scroll.availableWidth
                    spacing: 18
                    Section {
                        sectionId: "pinned"
                        title: root.pinnedLimits.length === 1 ? "Pinned limit" : "Pinned limits"
                        summary: root.pinnedSummary()
                        visible: root.pinnedLimits.length > 0 && !root.currentAccount
                        width: parent.width
                        headerItems: Sub { text: root.pinnedLimits.length + "/3" }
                        Repeater {
                            model: root.pinnedLimits
                            Column {
                                required property var modelData
                                required property int index
                                readonly property var limit: root.pinnedWindow(modelData)
                                readonly property var usage: root.pinnedUsages[modelData.provider]
                                width: parent.width
                                spacing: 7
                                Rectangle { visible: index > 0; width: parent.width; height: 1; color: root.edge }
                                RowLayout {
                                    width: parent.width
                                    Label {
                                        text: root.pinnedName(modelData) + " · " + (modelData.title || modelData.label)
                                        font.pixelSize: 16; font.weight: Font.DemiBold; elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                    Label {
                                        text: limit ? Math.round(Number(limit.percent || 0) * 100) + "% used" : "—"
                                        font.pixelSize: 16
                                        color: limit && Number(limit.percent) >= 0.9 ? root.colorFor("claude") : root.ink
                                    }
                                    Choice { text: "Unpin"; enabled: !pinSave.running; onClicked: root.unpinLimit(modelData) }
                                }
                                Rectangle {
                                    visible: !!limit
                                    width: parent.width; height: 5; radius: 3; color: root.edge
                                    Rectangle {
                                        width: parent.width * Math.min(1, Math.max(0, Number(limit ? limit.percent : 0)))
                                        height: parent.height; radius: parent.radius
                                        color: limit && Number(limit.percent) >= 0.9 ? root.colorFor("claude") : root.accent
                                    }
                                }
                                Sub {
                                    width: parent.width; wrapMode: Text.WordWrap
                                    text: limit ? (usage && usage.limitsStale ? "Last known · " : "")
                                        + root.pinnedResetText(limit.resetsAt) : "Waiting for this limit's next update"
                                }
                            }
                        }
                        Sub { visible: root.pinSaveFailed; text: "Could not update pinned limit" }
                    }
                    Card {
                        visible: !root.data || root.data.summary.unpricedTokens > 0 || (root.data.coverage.warnings || []).length > 0
                        width: parent.width; height: visible ? pricingNote.implicitHeight + 28 : 0
                        Column {
                            id: pricingNote
                            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 14; spacing: 6
                            Label { width: parent.width; wrapMode: Text.WordWrap; text: root.data ? (root.data.summary.tokens === 0 ? (Object.keys(root.selection).length ? "No activity for this filter in this period. Clear filters to see the rest of the history." : "No activity in this period. Add a history folder in Settings or use a supported coding agent.") : root.data.summary.unpricedTokens ? root.compact(root.data.summary.unpricedTokens)+" tokens have no complete price. API value is a partial estimate." : "All recorded tokens in this view have an API-value estimate.") : "Checking pricing coverage…"; color: root.data && root.data.summary.unpricedTokens ? root.colorFor("claude") : root.ink }
                            Sub { width: parent.width; wrapMode: Text.WordWrap; text: root.data ? "History on "+(root.data.coverage.machine || "this computer")+" · scanned "+root.when(root.data.coverage.scannedAt)+" · "+(root.data.pricing.coveragePercent===null ? "No activity" : (root.data.summary.unpricedTokens && root.data.pricing.coveragePercent>99.9 ? ">99.9" : root.data.pricing.coveragePercent.toFixed(1))+"% of tokens priced") : "" }
                        }
                    }
                    ColumnLayout {
                        width: parent.width; spacing: 18
                        Section {
                            sectionId: "totals"
                            title: "Totals"
                            summary: root.totalsSummary()
                            Layout.fillWidth: true
                            RowLayout { width: parent.width; spacing: 16
                                Column { Layout.fillWidth: true; spacing: 6
                                    Sub { text: root.liveTodayView() ? "PROCESSED TOKENS · TODAY" : "PROCESSED TOKENS"; font.letterSpacing: 1.1 }
                                    AnimatedTokenCount {
                                        id: pulseCounter
                                        visible: root.liveTodayView()
                                        dataReady: root.liveTodayView()
                                        targetTokens: root.pulseTokens()
                                        scopeKey: root.provider + "|" + (root.pulseSummary ? root.pulseSummary.date : "")
                                        animateChanges: window.visible && !root.settingsOpen
                                        color: root.ink
                                        font.family: root.fontFamily
                                        font.pixelSize: 27
                                        font.weight: Font.Medium
                                    }
                                    Label { visible: !root.liveTodayView(); text: root.currentAccount && !root.currentAccount.historyAvailable ? "—" : root.data ? root.compact(root.data.summary.tokens) : "…"; font.pixelSize: 31; font.weight: Font.Medium }
                                    Sub { text: root.currentAccount && !root.currentAccount.historyAvailable ? "Token history not linked" : root.liveTodayView() ? root.pulseStatus() : root.data ? root.data.summary.sessions + " sessions" : "Reading history" }
                                }
                                Column { Layout.fillWidth: true; spacing: 6
                                    Sub { text: "OUTPUT"; font.letterSpacing: 1.1 }
                                    Label { text: root.data ? root.compact(root.data.summary.output) : "…"; font.pixelSize: 24 }
                                    Sub { text: "Includes reasoning" }
                                }
                                Column { Layout.fillWidth: true; spacing: 6
                                    Sub { text: "CACHE READ"; font.letterSpacing: 1.1 }
                                    Label { text: root.data ? root.compact(root.data.summary.cacheRead) : "…"; font.pixelSize: 24 }
                                    Sub { text: root.data && root.data.summary.tokens ? (root.data.summary.cacheRead/root.data.summary.tokens*100).toFixed(1)+"% of processed tokens" : "No activity" }
                                }
                                Column { Layout.fillWidth: true; spacing: 6
                                    Sub { text: "API VALUE ESTIMATE"; font.letterSpacing: 1.1 }
                                    Label { text: root.data ? root.valueText(root.data.summary) : "…"; font.pixelSize: 24 }
                                    Sub { text: "Separate from plan charges" }
                                }
                            }
                            Rectangle { width: parent.width; height: 1; color: root.edge }
                            RowLayout { width: parent.width
                                Label { visible: root.comparisonText() !== ""; text: root.comparisonText(); color: root.accent; font.pixelSize: 12 }
                                Item { Layout.fillWidth: true }
                                Sub { text: "Processed tokens include reused context on each request" }
                            }
                        }
                        Section {
                            sectionId: "account"
                            title: root.currentAccount ? root.currentAccount.name : ""
                            summary: root.accountSummary()
                            visible: !!root.currentAccount
                            Layout.fillWidth: true
                            headerItems: Sub { text: root.currentAccount ? root.currentAccount.quota.plan || "" : "" }
                            Column {
                                width: parent.width; spacing: 12
                                Label {
                                    visible: !!root.currentAccount && !root.currentAccount.historyAvailable
                                    width: parent.width; wrapMode: Text.WordWrap
                                    text: "Limits are available. To see tokens and models, link this account's history folder in Settings → Accounts."
                                }
                                GridLayout {
                                    width: parent.width; columns: 2; columnSpacing: 24
                                    Column {
                                        Layout.fillWidth: true; Layout.preferredWidth: parent.width / 2; Layout.alignment: Qt.AlignTop
                                        spacing: 10
                                        Sub { text: "ACCOUNT LIMITS"; font.letterSpacing: 1.1 }
                                        Repeater {
                                            model: root.currentAccount ? root.currentAccount.quota.limits || [] : []
                                            Column {
                                                required property var modelData
                                                width: parent.width; spacing: 5
                                                RowLayout {
                                                    width: parent.width
                                                    Label { text: modelData.title || modelData.label; Layout.fillWidth: true; elide: Text.ElideRight }
                                                    Sub { text: Math.round(Number(modelData.percent || 0) * 100) + "% used" }
                                                }
                                                Rectangle {
                                                    width: parent.width; height: 4; radius: 2; color: root.edge
                                                    Rectangle { height: 4; radius: 2; width: parent.width * Math.min(1,Math.max(0,Number(modelData.percent || 0))); color: modelData.percent >= 0.9 ? root.colorFor("claude") : root.accent }
                                                }
                                                RowLayout {
                                                    width: parent.width
                                                    Sub {
                                                        Layout.fillWidth: true; wrapMode: Text.WordWrap
                                                        text: modelData.windowId && root.currentAccount && root.currentAccount.historyAvailable
                                                            ? root.compact(modelData.tokens) + " recorded tokens since reset · " + root.resetText(modelData.resetsAt)
                                                            : root.resetText(modelData.resetsAt) || "Reset time not reported"
                                                    }
                                                    Choice {
                                                        visible: !!modelData.windowId && !!root.currentAccount && root.currentAccount.historyAvailable
                                                        text: "View usage"; selected: root.selection.resetWindow === modelData.windowId
                                                        onClicked: root.chooseResetWindow(modelData.windowId)
                                                    }
                                                }
                                            }
                                        }
                                        Sub {
                                            visible: !!root.currentAccount && !(root.currentAccount.quota.limits || []).length
                                            width: parent.width; wrapMode: Text.WordWrap
                                            text: root.currentAccount ? root.currentAccount.quota.error || "No account limits reported" : ""
                                        }
                                        Sub {
                                            visible: !!root.currentAccount && !!root.currentAccount.quota.chatgptCredits
                                            width: parent.width; wrapMode: Text.WordWrap
                                            text: {
                                                var credits = root.currentAccount ? root.currentAccount.quota.chatgptCredits : null
                                                return !credits ? "" : credits.error ? "ChatGPT credits unavailable" : credits.unlimited ? "Unlimited ChatGPT credits"
                                                    : root.creditAmount(credits.remaining) + " ChatGPT credits remaining · account-wide"
                                            }
                                        }
                                        Sub {
                                            visible: !!root.currentAccount && !!root.currentAccount.quota.balance
                                            text: root.currentAccount && root.currentAccount.quota.balance ? root.money(root.currentAccount.quota.balance.remaining) + " prepaid credits remaining" : ""
                                        }
                                        Sub { text: root.currentAccount ? root.quotaAge(root.currentAccount.quota) : "" }
                                    }
                                    Column {
                                        Layout.fillWidth: true; Layout.preferredWidth: parent.width / 2; Layout.alignment: Qt.AlignTop
                                        spacing: 8
                                        Sub { text: root.selection.resetWindow ? "MODELS · SINCE RESET" : "MODELS · SELECTED PERIOD"; font.letterSpacing: 1.1 }
                                        Repeater {
                                            model: root.data ? root.data.models.slice(0,8) : []
                                            RowLayout {
                                                required property var modelData
                                                width: parent.width; spacing: 12
                                                Label { text: modelData.name; Layout.fillWidth: true; elide: Text.ElideRight }
                                                Label { text: root.compact(modelData.tokens); font.weight: Font.DemiBold }
                                                Choice { text: "Explore"; onClicked: root.drill("model",modelData.name,"") }
                                            }
                                        }
                                        Sub { visible: !!root.data && !root.data.models.length; text: "No recorded models in this period" }
                                        Choice {
                                            visible: !!root.data && root.data.models.length > 8
                                            text: "All " + (root.data ? root.data.models.length : 0) + " models"
                                            onClicked: { root.breakdown = "models"; root.tableLimit = root.data.models.length; scroll.contentItem.contentY = breakdownSection.y }
                                        }
                                    }
                                }
                            }
                        }
                        Section {
                            sectionId: "hourly"
                            title: root.selection.hourStart ? "Selected hour" : root.selection.day ? "Tokens by hour · "+root.selection.day : root.data && root.data.hourly.length ? "Tokens by hour" : "Daily activity"
                            summary: root.hourlySummary()
                            Layout.fillWidth: true
                            headerItems: RowLayout {
                                spacing: 8
                                Choice { text: "Tokens"; selected: root.metric === "tokens"; onClicked: root.metric = "tokens" }
                                Choice { text: "API value"; selected: root.metric === "value"; onClicked: root.metric = "value" }
                            }
                            Column {
                                width: parent.width; spacing: 10
                                HourlyActivity {
                                    id: hourlyView
                                    visible: root.data && root.data.hourly.length > 0
                                    width: parent.width
                                    hours: root.data ? root.data.hourly : []
                                    unplaced: root.data ? root.data.hourlyUnplaced : ({total:{tokens:0,value:0}})
                                    metric: root.metric
                                    selectedHour: Number(root.selection.hourStart || -1)
                                    fontFamily: root.fontFamily; ink: root.ink; muted: root.muted; edge: root.edge; accent: root.accent
                                    sourceColor: function(id) { return root.colorFor(id) }
                                    sourceName: function(id) { return root.providerName(id) }
                                    formatHour: function(start) { return root.localClock(start) }
                                    formatHourTitle: function(hour) { return root.hourTitle(hour) }
                                    onHourSelected: start => { if (Number(root.selection.hourStart || -1) !== start) root.drillHour(start) }
                                }
                                Canvas {
                                    id: chart
                                    visible: !root.data || root.data.hourly.length === 0
                                    width: parent.width; height: 150
                                    property int hovered: -1
                                    property real pointerX: width/2
                                    property bool hourly: root.data ? root.data.hourly.length > 0 : false
                                    property var series: root.data ? (hourly ? root.data.hourly || [] : root.data.daily) : []
                                    property string metric: root.metric
                                    onSeriesChanged: requestPaint()
                                    onMetricChanged: requestPaint()
                                    Connections { target: root; function onClockUses24HourChanged() { chart.requestPaint() } }
                                    onHoveredChanged: requestPaint()
                                    onWidthChanged: requestPaint()
                                    onHeightChanged: requestPaint()
                                    onPaint: {
                                        var ctx=getContext("2d"); ctx.reset()
                                        var w=width, h=height, left=48, top=10, bottom=h-26, plot=w-left-10
                                        if (!root.data || root.data.summary.tokens === 0) {
                                            ctx.font="13px \""+root.fontFamily+"\"";ctx.fillStyle=root.muted;ctx.textAlign="center"
                                            ctx.fillText(root.data ? "No activity in this period" : "Reading local history…",w/2,h/2)
                                            return
                                        }
                                        var cards=root.data ? root.data.cards : [], ids=cards.map(c=>c.id), max=1
                                        function total(d) { return root.amount(d.total) }
                                        for (var d of series) max=Math.max(max,total(d))
                                        ctx.font="10px \""+root.fontFamily+"\""; ctx.lineWidth=1
                                        for(var t=0;t<3;t++) {
                                            var y=top+(bottom-top)*t/2
                                            ctx.strokeStyle=root.edge;ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w,y);ctx.stroke()
                                            ctx.fillStyle=root.muted;ctx.fillText(root.metric==="tokens"?root.compact(max*(1-t/2)):"$"+root.compact(max*(1-t/2)),0,y+4)
                                        }
                                        if (hourly) {
                                            var group=plot/Math.max(1,series.length), bw=Math.min(22,group*0.7/Math.max(1,ids.length)), gap=4
                                            for(var i=0;i<series.length;i++) {
                                                var center=left+group*(i+0.5), totalWidth=ids.length*bw+(ids.length-1)*gap
                                                for(var j=0;j<ids.length;j++) {
                                                    var barHeight=root.amount(series[i].cards[ids[j]])/max*(bottom-top)
                                                    var barAlpha=cards[j].shade===0?0.8:cards[j].shade===1?0.55:0.38
                                                    ctx.fillStyle=Qt.alpha(root.accountColor(cards[j].provider,cards[j].shade),hovered===i?1:barAlpha)
                                                    ctx.fillRect(center-totalWidth/2+j*(bw+gap),bottom-barHeight,bw,barHeight)
                                                }
                                                if(series.length<=8 || i%3===0 || i===series.length-1) {
                                                    ctx.fillStyle=root.muted;ctx.textAlign="center";ctx.fillText(root.localClock(series[i].start),center,h-5);ctx.textAlign="left"
                                                }
                                            }
                                        } else {
                                            ctx.beginPath()
                                            for(var i=0;i<series.length;i++) {
                                                var x=left+(series.length===1?plot/2:i*plot/(series.length-1))
                                                var yy=bottom-total(series[i])/max*(bottom-top)
                                                if(i===0)ctx.moveTo(x,yy);else ctx.lineTo(x,yy)
                                            }
                                            ctx.strokeStyle=root.accent;ctx.lineWidth=2.5;ctx.stroke()
                                            if(series.length>1){ctx.lineTo(left+plot,bottom);ctx.lineTo(left,bottom);ctx.closePath();ctx.fillStyle=Qt.alpha(root.accent,0.10);ctx.fill()}
                                            ctx.fillStyle=root.muted
                                            if(series.length){ctx.fillText(series[0].date.slice(5),left,h-5);ctx.fillText(series[series.length-1].date.slice(5),w-42,h-5)}
                                        }
                                        if(hovered>=0 && hovered<series.length){var xx=hourly?left+(hovered+0.5)*plot/series.length:left+(series.length===1?plot/2:hovered*plot/(series.length-1));ctx.strokeStyle=Qt.alpha(root.ink,0.3);ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(xx,top);ctx.lineTo(xx,bottom);ctx.stroke()}

                                    }
                                    MouseArea {
                                        anchors.fill: parent; hoverEnabled: true
                                        onPositionChanged: mouse => { chart.pointerX=mouse.x; chart.hovered=Math.max(0,Math.min(chart.series.length-1,(chart.hourly ? Math.floor((mouse.x-48)/(width-58)*chart.series.length) : Math.round((mouse.x-48)/(width-58)*Math.max(0,chart.series.length-1))))) }
                                        onExited: chart.hovered=-1
                                        cursorShape: chart.hourly ? Qt.ArrowCursor : Qt.PointingHandCursor
                                        onClicked: { if (!chart.hourly && chart.hovered>=0 && chart.hovered<chart.series.length) root.drill("day",chart.series[chart.hovered].date,"") }
                                    }
                                    HoverTip {
                                        id: chartTip
                                        parent: chart
                                        visible: chart.hovered>=0 && chart.hovered<chart.series.length
                                        x: chart.pointerX > chart.width/2 ? 48 : Math.max(0,chart.width-width-12)
                                        y: 16
                                        heading: chart.hovered>=0 && chart.hovered<chart.series.length ? (chart.hourly ? root.hourTitle(chart.series[chart.hovered]) : Qt.formatDate(new Date(chart.series[chart.hovered].date+"T12:00:00"),"dddd, MMM d")) : ""
                                        rows: chart.hovered>=0 && chart.hovered<chart.series.length && root.data ? [{label:"Total",value:root.display(chart.series[chart.hovered].total),color:root.accent}].concat(root.data.cards.map(c=>({label:c.name,value:root.display(chart.series[chart.hovered].cards[c.id]),color:root.accountColor(c.provider,c.shade)}))) : []
                                        detail: (chart.hourly ? "This hour · " : "") + (root.metric === "tokens" ? "Processed tokens, including cached input" : "Estimated API value, not your bill")
                                    }
                                }
                            }
                        }
                    }
                    Section {
                        sectionId: "accounts"
                        title: "Accounts"
                        summary: root.accountsSummary()
                        visible: !root.currentAccount
                        width: parent.width
                        headerItems: Choice { text: root.providerDetailsOpen ? "Hide details" : "Show limits and models"; onClicked: root.providerDetailsOpen = !root.providerDetailsOpen }
                        Column {
                            width: parent.width; spacing: 6
                            RowLayout { width: parent.width; spacing: 12
                                Item { Layout.preferredWidth: 7 }
                                Item { Layout.preferredWidth: 190 }
                                Item { Layout.fillWidth: true }
                                Sub { text: "TOKENS"; Layout.preferredWidth: 78; horizontalAlignment: Text.AlignRight }
                                Sub { text: "API VALUE"; Layout.preferredWidth: 120; horizontalAlignment: Text.AlignRight }
                            }
                            Repeater {
                                model: root.data ? root.data.cards.slice(0, root.providerRowsExpanded ? root.data.cards.length : 6) : []
                                Item {
                                    required property var modelData
                                    width: parent.width; height: 40
                                    activeFocusOnTab: true
                                    Accessible.role: Accessible.Button
                                    Accessible.name: "Open " + modelData.name
                                    Keys.onReturnPressed: root.chooseAccount(modelData.id)
                                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                                    TapHandler { onTapped: root.chooseAccount(modelData.id) }
                                    RowLayout { anchors.fill: parent; spacing: 12
                                        Rectangle { implicitWidth: 7; implicitHeight: 7; radius: 4; color: root.accountColor(modelData.provider,modelData.shade) }
                                        Label { text: modelData.name; Layout.preferredWidth: 190; elide: Text.ElideRight; font.pixelSize: 12 }
                                        Rectangle { Layout.fillWidth: true; implicitHeight: 5; radius: 3; color: root.edge
                                            Rectangle { width: parent.width * (root.data && root.data.summary.tokens ? modelData.tokens/root.data.summary.tokens : 0); height: parent.height; radius: parent.radius; color: root.accountColor(modelData.provider,modelData.shade) }
                                        }
                                        Label { text: root.compact(modelData.tokens); Layout.preferredWidth: 78; horizontalAlignment: Text.AlignRight; font.pixelSize: 12 }
                                        Sub { text: root.valueText(modelData); Layout.preferredWidth: 120; horizontalAlignment: Text.AlignRight; elide: Text.ElideRight }
                                    }
                                }
                            }
                            Choice { visible: !!root.data && root.data.cards.length > 6; text: root.providerRowsExpanded ? "Show fewer accounts" : "Show all " + (root.data ? root.data.cards.length : 0) + " accounts"; onClicked: root.providerRowsExpanded = !root.providerRowsExpanded }
                        }
                    }
                    GridLayout {
                        // The detail cards belong to the Accounts section: folding
                        // that section hides them with its rows.
                        visible: root.providerDetailsOpen && !root.currentAccount && !root.sectionFolded("accounts")
                        height: visible ? implicitHeight : 0
                        width: parent.width; columns: root.data ? (root.data.cards.length > 3 ? 2 : Math.max(1,root.data.cards.length)) : 3; rowSpacing: 14; columnSpacing: 14
                        Repeater {
                            model: root.data ? root.data.cards : []
                            Card {
                                required property var modelData
                                Layout.fillWidth: true
                                implicitHeight: providerColumn.implicitHeight + 36
                                Layout.fillHeight: true
                                Column {
                                    id: providerColumn
                                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 18
                                    spacing: 10
                                    Row {
                                        spacing: 8
                                        Rectangle { width: 8; height: 8; radius: 4; color: root.accountColor(modelData.provider, modelData.shade); anchors.verticalCenter: parent.verticalCenter }
                                        Label { text: modelData.name; font.pixelSize: 16; font.weight: Font.DemiBold }
                                    }
                                    Row {
                                        spacing: 10
                                        Label { text: root.compact(modelData.tokens); font.pixelSize: 26; font.weight: Font.Medium }
                                        Sub { text: "tokens"; anchors.bottom: parent.bottom; anchors.bottomMargin: 3 }
                                    }
                                    Sub { width: parent.width; wrapMode: Text.WordWrap; text: root.valueText(modelData) + " API value · " + modelData.sessions + " sessions" }
                                    Sub { width: parent.width; wrapMode: Text.WordWrap; text: modelData.valueShare === null || (modelData.tokens > 0 && modelData.unpricedTokens === modelData.tokens) ? "No priced API value" : modelData.valueShare.toFixed(1)+"% of priced API value" }
                                    Sub { width: parent.width; wrapMode: Text.WordWrap; text: modelData.sessions ? root.compact(modelData.tokensPerSession)+" tokens · "+root.money(modelData.valuePerSession)+" priced value / recorded session" : "No recorded sessions" }
                                    Rectangle { width: parent.width; height: 3; radius: 2; color: root.edge
                                        Rectangle { width: parent.width*(root.data.summary.tokens ? modelData.tokens/root.data.summary.tokens : 0); height: 3; radius: 2; color: root.accountColor(modelData.provider, modelData.shade) }
                                    }
                                    Sub {
                                        width: parent.width; wrapMode: Text.WordWrap
                                        text: modelData.monthlyPrice !== null ? "Monthly plan: "+root.money(modelData.monthlyPrice) : "Monthly plan price not set"
                                    }
                                    Sub { visible: modelData.provider === "grok"; width: parent.width; wrapMode: Text.WordWrap; text: root.compact(modelData.modelCalls || 0)+" model calls · "+modelData.requests+" usage records" }
                                    Sub { visible: modelData.quotaScope !== ""; text: modelData.quotaScope }
                                    Repeater {
                                        model: modelData.quota.limits || []
                                        Column {
                                            required property var modelData
                                            width: providerColumn.width; spacing: 4
                                            RowLayout { width: parent.width
                                                Sub { text: modelData.label }
                                                Item { Layout.fillWidth: true }
                                                Label { text: (modelData.percent*100).toFixed(0)+"% used"; font.pixelSize: 11 }
                                            }
                                            Rectangle { width: parent.width; height: 4; radius: 2; color: root.edge
                                                Rectangle { height: 4; radius: 2; width: parent.width*Math.min(1,Math.max(0,modelData.percent)); color: modelData.percent>=0.9 ? root.colorFor("claude") : Qt.alpha(root.ink,0.55) }
                                            }
                                            Sub {
                                                // Local tokens in this window, when the collector could
                                                // tell where the window began.
                                                text: {
                                                    var reset = root.resetText(modelData.resetsAt)
                                                    return reset.indexOf("Resets in") === 0 && typeof modelData.tokens === "number"
                                                        ? reset + " · " + root.compact(modelData.tokens) + " tokens on this PC" : reset
                                                }
                                                width: parent.width; elide: Text.ElideRight; font.pixelSize: 10
                                            }
                                        }
                                    }
                                    Column {
                                        id: walletSection
                                        // A wallet the provider reports on top of its windows:
                                        // money left rather than a share of a cap. A balance
                                        // that would print as $0.00 stays off the card.
                                        visible: !!modelData.quota.balance && Math.round((modelData.quota.balance.remaining || 0) * 100) > 0
                                        width: providerColumn.width; spacing: 4
                                        readonly property var wallet: modelData.quota.balance || ({})
                                        readonly property real spent: Math.max(0, Number(wallet.funded || 0) - Number(wallet.remaining || 0))

                                        RowLayout { width: parent.width
                                            Sub { text: walletSection.wallet.label || "Prepaid credits"
                                                  textFormat: Text.PlainText; Layout.fillWidth: true; elide: Text.ElideRight }
                                            Label { text: root.money(walletSection.wallet.remaining)+" left"; font.pixelSize: 11 }
                                        }
                                        Rectangle { visible: walletSection.wallet.funded > 0; width: parent.width; height: 4; radius: 2; color: root.edge
                                            Rectangle { height: 4; radius: 2
                                                width: parent.width*Math.min(1,Math.max(0, walletSection.wallet.remaining/(walletSection.wallet.funded || 1)))
                                                color: walletSection.wallet.funded > 0 && walletSection.wallet.remaining/walletSection.wallet.funded <= 0.1 ? root.colorFor("claude") : Qt.alpha(root.ink,0.55) }
                                        }
                                        Sub { visible: walletSection.wallet.funded > 0
                                              text: root.money(walletSection.spent)+" spent of "+root.money(walletSection.wallet.funded)+" funded"
                                                    +(walletSection.wallet.estimated ? " · estimated" : "")
                                              font.pixelSize: 10 }
                                    }
                                    Column {
                                        visible: !credits.error && isFinite(Number(credits.remaining)) && Number(credits.remaining) > 0
                                        width: providerColumn.width; spacing: 4
                                        readonly property var credits: modelData.quota.chatgptCredits || ({})
                                        Sub { text: "ChatGPT credits · Work and Codex" }
                                        Label {
                                            width: parent.width; wrapMode: Text.WordWrap; font.pixelSize: 13
                                            text: parent.credits.error ? "Balance unavailable"
                                                : parent.credits.unlimited ? "Unlimited credits"
                                                : root.creditAmount(parent.credits.remaining) + " remaining"
                                        }
                                        Sub {
                                            visible: !!parent.credits.trackingSince && !parent.credits.error && !parent.credits.unlimited
                                            width: parent.width; wrapMode: Text.WordWrap; font.pixelSize: 10
                                            text: visible ? root.creditAmount(parent.credits.spent) + " used since "
                                                + Qt.formatDateTime(new Date(parent.credits.trackingSince), "MMM d, yyyy")
                                                + " · estimated" : ""
                                        }
                                        Sub { text: "Account-wide balance"; font.pixelSize: 10 }
                                    }
                                    Column {
                                        visible: !!modelData.models && modelData.models.length > 0
                                        width: providerColumn.width; spacing: 5
                                        Sub { text: "Models · this period" }
                                        Repeater {
                                            model: modelData.models || []
                                            RowLayout {
                                                required property var modelData
                                                width: providerColumn.width; spacing: 8
                                                Sub { text: modelData.model; Layout.fillWidth: true; elide: Text.ElideRight }
                                                Label { text: root.compact(modelData.tokens); font.pixelSize: 10 }
                                                Label { text: modelData.unpriced >= modelData.tokens ? "unpriced" : root.money(modelData.value); font.pixelSize: 10; color: root.muted }
                                            }
                                        }
                                    }
                                    Column {
                                        visible: modelData.provider === "opencode-go" && !!root.data && root.data.goAllowance.models.length > 0
                                        width: providerColumn.width; spacing: 5
                                        Sub { text: "Model allowance · this month" }
                                        Repeater {
                                            model: root.data ? root.data.goAllowance.models.slice(0,4) : []
                                            Column {
                                                required property var modelData
                                                width: providerColumn.width; spacing: 2
                                                RowLayout { width: parent.width; spacing: 8
                                                    Sub { text: modelData.model; Layout.fillWidth: true; elide: Text.ElideRight }
                                                    Label { text: root.money(modelData.value)+" / "+root.money(modelData.limit); font.pixelSize: 10 }
                                                }
                                                Rectangle { width: parent.width; height: 3; radius: 2; color: root.edge
                                                    Rectangle { height: 3; radius: 2
                                                        width: parent.width*Math.min(1, modelData.limit > 0 ? modelData.value/modelData.limit : 0)
                                                        color: modelData.value >= modelData.limit*0.9 ? root.colorFor("claude") : Qt.alpha(root.ink,0.55) }
                                                }
                                                Sub { visible: modelData.promo; text: "4x promo through "+root.shortDate(modelData.promoEnds); font.pixelSize: 9 }
                                            }
                                        }
                                        Sub { visible: !!root.data && root.data.goAllowance.models.length > 4; text: "+"+(root.data.goAllowance.models.length-4)+" more models used this month"; font.pixelSize: 9 }
                                    }
                                    Sub { width: parent.width; wrapMode: Text.WordWrap; text: modelData.quota.error || root.quotaAge(modelData.quota); font.pixelSize: 10 }
                                }
                            }
                        }
                    }
                    Section {
                        sectionId: "cache"
                        title: "Cache and output"
                        summary: root.cacheSummary()
                        width: parent.width
                        RowLayout {
                            width: parent.width; spacing: 15
                            Repeater {
                                model: [{name:"Uncached input",key:"input"},{name:"Cached input",key:"cacheRead"},{name:"Cache writes",key:"cacheWrite"},{name:"Output",key:"output"},{name:"Known cache savings",key:"cacheSavings"}]
                                Column {
                                    required property var modelData
                                    Layout.fillWidth: true; spacing: 8
                                    Sub { text: modelData.name }
                                    Label { text: root.data ? (modelData.key === "cacheSavings" ? root.money(root.data.summary[modelData.key]) : root.compact(root.data.summary[modelData.key])) : "…"; font.pixelSize: 21 }
                                }
                            }
                        }
                    }
                    Section {
                        id: breakdownSection
                        sectionId: "breakdown"
                        title: "Breakdown"
                        summary: root.breakdownSummary()
                        width: parent.width
                        headerItems: RowLayout {
                            spacing: 8
                            Repeater { model: ["models","projects","clients","routes","accounts","sessions"]
                                Choice { required property string modelData; text: modelData[0].toUpperCase()+modelData.slice(1); selected: root.breakdown===modelData; onClicked: root.breakdown=modelData }
                            }
                        }
                        Column {
                            width: parent.width; spacing: 10
                            RowLayout { width: parent.width; spacing: 12
                                Sub { text: root.breakdown === "models" ? "MODEL" : root.breakdown === "projects" ? "PROJECT" : root.breakdown === "sessions" ? "SESSION / PROJECT" : root.breakdown === "routes" ? "SOURCE ROUTE" : root.breakdown === "accounts" ? "ACCOUNT" : "CLIENT"; Layout.fillWidth: true }
                                Sub { text: "TOKENS"; Layout.preferredWidth: 95; horizontalAlignment: Text.AlignRight }
                                Sub { text: "API VALUE"; Layout.preferredWidth: 150; horizontalAlignment: Text.AlignRight }
                                Sub { text: "CACHE READ"; Layout.preferredWidth: 95; horizontalAlignment: Text.AlignRight }
                            }
                            Repeater {
                                model: root.breakdownItems.slice(0, root.tableLimit)
                                Rectangle {
                                    required property var modelData
                                    width: parent.width; height: root.breakdown === "sessions" ? 62 : 43; color: rowHover.hovered ? Qt.alpha(root.ink,0.04) : "transparent"
                                    Behavior on color { ColorAnimation { duration: 100 } }
                                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: root.edge; opacity: 0.55 }
                                    RowLayout {
                                        anchors.fill: parent; spacing: 12
                                        Rectangle { implicitWidth: 6; implicitHeight: 6; radius: 1; color: root.colorFor(modelData.provider) }
                                        Label {
                                            id: modelLabel
                                            Layout.fillWidth: true; elide: Text.ElideMiddle
                                            activeFocusOnTab: root.breakdown !== "sessions"
                                            color: activeFocus ? root.bright : root.ink
                                            Accessible.role: Accessible.Button
                                            Accessible.name: "Explore "+modelData.name
                                            Keys.onReturnPressed: root.exploreRow(modelData)
                                            text: root.breakdown === "sessions" ? (modelData.project.split("/").filter(x=>x).pop() || "/")+" · "+modelData.name.slice(0,12)+"\n"+modelData.client+" · "+root.when(modelData.lastAt) : root.breakdown === "projects" ? modelData.name.split('/').filter(x=>x).pop() || "/" : modelData.name
                                            HoverTip {
                                                parent: modelLabel
                                                visible: rowHover.hovered
                                                y: parent.height+10
                                                heading: root.breakdown === "sessions" ? modelData.project+"\n"+modelData.name : modelData.name
                                                detail: modelData.sessions+" sessions · "+root.compact(modelData.requests)+" usage records"+(root.recordedAs(modelData) ? " · "+root.recordedAs(modelData) : "")
                                                rows: (root.routeCount(modelData) > 1
                                                       ? modelData.routes.map(r => ({label: r.providerName+" · "+r.model, value: root.compact(r.tokens)+" · "+root.valueText(r), color: root.colorFor(r.provider)}))
                                                       : []).concat([
                                                       {label:"Uncached input",value:root.compact(modelData.input)},
                                                       {label:"Cached input",value:root.compact(modelData.cacheRead)},
                                                       {label:"Cache writes",value:root.compact(modelData.cacheWrite)},
                                                       {label:"Output",value:root.compact(modelData.output)}])
                                            }
                                            HoverHandler { id: rowHover; cursorShape: root.breakdown === "sessions" ? Qt.ArrowCursor : Qt.PointingHandCursor }
                                            TapHandler { onTapped: root.exploreRow(modelData) }
                                        }
                                        Sub { visible: root.routeCount(modelData) > 1; text: root.routeCount(modelData)+" routes"; font.pixelSize: 10 }
                                        Label { text: root.compact(modelData.tokens); Layout.preferredWidth: 95; horizontalAlignment: Text.AlignRight }
                                        Label { text: root.valueText(modelData); Layout.preferredWidth: 150; horizontalAlignment: Text.AlignRight; color: modelData.unpricedTokens ? root.colorFor("claude") : root.ink }
                                        Sub { text: (modelData.tokens ? modelData.cacheRead/modelData.tokens*100 : 0).toFixed(1)+"%"; Layout.preferredWidth: 95; horizontalAlignment: Text.AlignRight }
                                    }
                                }
                            }
                            RowLayout {
                                width: parent.width
                                visible: root.breakdownItems.length > root.breakdownPageSize()
                                Sub { text: "Showing " + Math.min(root.tableLimit, root.breakdownItems.length) + " of " + root.breakdownItems.length; Layout.fillWidth: true }
                                Choice { visible: root.tableLimit > root.breakdownPageSize(); text: "Show fewer"; onClicked: root.resetTableLimit() }
                                Choice { visible: root.breakdownItems.length > root.tableLimit; text: "Show " + Math.min(root.breakdownPageSize(), root.breakdownItems.length - root.tableLimit) + " more"; onClicked: root.tableLimit += root.breakdownPageSize() }
                            }
                            Sub { width: parent.width; wrapMode: Text.WordWrap; text: "A recorded session is not a completed task. Averages cover this period; priced value excludes unknown prices." }
                            Sub { visible: !!root.data && !root.breakdownItems.length; text: "No recorded activity in this period." }
                        }
                    }
                    Section {
                        sectionId: "year"
                        title: "Activity over the past year"
                        summary: root.yearSummary()
                        width: parent.width
                        headerItems: Sub { text: "Darker to brighter = more tokens" }
                        Column {
                            width: parent.width; spacing: 12
                            Canvas {
                                id: heatmap
                                width: parent.width; height: 80
                                property var activity: root.data ? root.data.heatmap : ({})
                                onActivityChanged: requestPaint()
                                onWidthChanged: requestPaint()
                                property string hoverText: ""
                                property real pointerX: 0
                                property string hoverDate: ""
                                onPaint: {
                                    var ctx=getContext('2d'); ctx.reset()
                                    var today=new Date();today.setHours(12,0,0,0)
                                    var max=1;for(var key in activity)max=Math.max(max,activity[key])
                                    var cell=Math.min(15,(width-10)/53), sy=11
                                    for(var i=0;i<371;i++){
                                        var day=new Date(today);day.setDate(day.getDate()-370+i)
                                        var k=Qt.formatDate(day,'yyyy-MM-dd'),n=activity[k]||0
                                        ctx.fillStyle=n?Qt.alpha(root.colorFor("codex"),0.18+0.82*Math.sqrt(n/max)):Qt.alpha(root.ink,0.065)
                                        ctx.fillRect(Math.floor(i/7)*cell,(i%7)*sy,cell-3,8)
                                    }
                                }
                                MouseArea {
                                    anchors.fill: parent; hoverEnabled: true
                                    onPositionChanged: mouse => {
                                        var cell=Math.min(15,(width-10)/53), i=Math.floor(mouse.x/cell)*7+Math.floor(mouse.y/11)
                                        var d=new Date();d.setDate(d.getDate()-370+i)
                                        var key=Qt.formatDate(d,'yyyy-MM-dd')
                                        heatmap.pointerX=mouse.x;heatmap.hoverDate=key
                                        heatmap.hoverText=i>=0&&i<371?(heatmap.activity[key]?root.compact(heatmap.activity[key])+" tokens":"No recorded activity"):""
                                    }
                                    onExited: heatmap.hoverText=""
                                }
                                HoverTip {
                                    parent: heatmap
                                    visible: heatmap.hoverText!==""
                                    x: Math.max(0,Math.min(heatmap.width-width,heatmap.pointerX+16))
                                    y: -implicitHeight-10
                                    heading: heatmap.hoverDate ? Qt.formatDate(new Date(heatmap.hoverDate+"T12:00:00"),"dddd, MMM d, yyyy") : ""
                                    detail: heatmap.hoverText
                                    implicitWidth: 280
                                }
                            }
                        }
                    }
                    Choice { text: root.coverageOpen ? "Hide data details" : "Data coverage and pricing details"; onClicked: root.coverageOpen = !root.coverageOpen }
                    Card {
                        visible: root.coverageOpen
                        width: parent.width; height: visible ? coverageColumn.implicitHeight+36 : 0
                        Column {
                            id: coverageColumn
                            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 18; spacing: 9
                            Label { text: "Data coverage"; font.pixelSize: 15; font.weight: Font.DemiBold }
                            Sub { width: parent.width; wrapMode: Text.WordWrap; text: root.data ? "Local history on "+root.data.coverage.machine+". Additional configured homes: "+(root.data.coverage.additionalHomes||0)+". A scan reads available folders"+(root.machineAccounts.length ? " and imports the synced machine ledgers below." : "; it does not sync another machine.")+" Normal ChatGPT chats are not included." : "Reading sources…" }
                            Sub { visible: root.machineAccounts.length > 0; width: parent.width; wrapMode: Text.WordWrap; text: "Synced machines: "+root.machineAccounts.map(a=>a.label).join(", ")+". Their recorded events are included in every total and breakdown." }
                            Repeater {
                                model: root.data ? root.data.coverage.sources || [] : []
                                Column {
                                    required property var modelData
                                    width: coverageColumn.width; spacing: 4
                                    Label { width: parent.width; elide: Text.ElideMiddle; font.pixelSize: 12; text: modelData.path; color: modelData.status === "available" ? root.ink : root.colorFor("claude") }
                                    Sub { width: parent.width; wrapMode: Text.WordWrap; text: (modelData.status || "Unknown")+" · "+modelData.files+(modelData.kind === "database" ? " database" : " files")+(modelData.latestFileAt ? " · latest file change "+root.when(modelData.latestFileAt) : "") }
                                }
                            }
                            Label { text: "Indexed clients · all retained history"; font.pixelSize: 14 }
                            Repeater {
                                model: root.data ? root.data.coverage.clients || [] : []
                                Sub { required property var modelData; width: coverageColumn.width; wrapMode: Text.WordWrap; text: modelData.provider+" / "+modelData.client+" · "+modelData.sessions+" sessions · latest event "+root.when(modelData.lastAt) }
                            }
                            Sub { width: parent.width; wrapMode: Text.WordWrap; text: root.data ? "Pricing: "+root.data.pricing.source+(root.data.pricing.fetchedAtMs?" · "+Qt.formatDateTime(new Date(root.data.pricing.fetchedAtMs),"MMM d, yyyy"):"")+". Estimates use this catalog's rates, not historical billing rates." : "" }
                            Sub { width: parent.width; wrapMode: Text.WordWrap; text: "Grok, OpenCode, Pi, and Oh My Pi use recorded API estimates when available. Their recorded totals do not provide cache savings. Usage records are message snapshots, aggregated rows, or cloud events, not equivalent request counts. Cursor also retains events with zero reported tokens." }
                            Repeater {
                                model: root.data ? root.data.pricing.unpriced || [] : []
                                Sub { required property var modelData; width: coverageColumn.width; wrapMode: Text.WordWrap; text: modelData.provider+" / "+modelData.name+": "+root.compact(modelData.unpricedTokens)+" unpriced tokens" }
                            }
                            Label { width: parent.width; wrapMode: Text.WordWrap; font.pixelSize: 12; color: root.colorFor("claude"); visible: !!root.data && root.data.unknownModels.length>0; text: root.data ? "Unpriced models: "+root.data.unknownModels.join(", ")+". Their tokens are included; their API value is not." : "" }
                            Label { width: parent.width; wrapMode: Text.WordWrap; font.pixelSize: 12; color: root.colorFor("claude"); visible: !!root.data && (root.data.coverage.warnings||[]).length>0; text: root.data ? (root.data.coverage.warnings||[]).join("\n") : "" }
                            Sub { width: parent.width; wrapMode: Text.WordWrap; text: "Today compares with yesterday up to the same local time. Longer ranges compare with the full preceding calendar period. Saved metrics remain when transcripts are archived or removed." }
                        }
                    }
                    Item { width: 1; height: 8 }
                }
            }
            ScrollView {
                id: settingsScroll
                ScrollBar.vertical: QuietScrollBar { parent: settingsScroll; x: settingsScroll.width-width; height: settingsScroll.height }
                visible: root.settingsOpen
                Layout.fillWidth: true; Layout.fillHeight: true; contentWidth: availableWidth; clip: true
                Column {
                    width: settingsScroll.availableWidth; spacing: 18
                    Label { text: "Make it yours"; font.pixelSize: 24; font.weight: Font.DemiBold }
                    Sub { text: "Analytics preferences stay on this machine. Existing app credentials are read, never changed." }
                    Flow { width: parent.width; spacing: 8
                        Repeater { model: [{id:"sources",name:"Sources"},{id:"accounts",name:"Accounts"},{id:"pricing",name:"Pricing"},{id:"sync",name:"Sync"},{id:"appearance",name:"Appearance"},{id:"updates",name:"Updates"}]
                            Choice { required property var modelData; text: modelData.name; selected: root.settingsTab === modelData.id; onClicked: root.settingsTab = modelData.id }
                        }
                    }
                    CheckBox {
                        visible: root.settingsTab === "updates"
                        text: "Check for new releases"
                        checked: root.draftUpdateCheck
                        onToggled: root.draftUpdateCheck = checked
                    }
                    Sub {
                        visible: root.settingsTab === "updates"; width: parent.width; wrapMode: Text.WordWrap
                        text: "Checks GitHub once a day during refresh. Sends no usage data. Updates are installed only when you run the command."
                    }
                    Label { visible: root.settingsTab === "sources"; text: "Visible providers"; font.pixelSize: 16 }
                    Flow { visible: root.settingsTab === "sources"; width: parent.width; spacing: 10
                        Repeater { model: root.providerOptions
                            Choice {
                                required property var modelData
                                text: modelData.name; selected: root.draftEnabled.indexOf(modelData.id)>=0
                                onClicked: { var a=root.draftEnabled.slice();var i=a.indexOf(modelData.id);if(i>=0)a.splice(i,1);else a.push(modelData.id);root.draftEnabled=a }
                            }
                        }
                    }
                    Label { visible: root.settingsTab === "appearance"; text: "Window transparency"; font.pixelSize: 16 }
                    RowLayout {
                        visible: root.settingsTab === "appearance"
                        width: 460; spacing: 18
                        Slider {
                            id: opacitySlider
                            from: 0.55; to: 1; stepSize: 0.005
                            implicitHeight: 36
                            Layout.minimumHeight: 36
                            hoverEnabled: true
                            snapMode: Slider.SnapAlways
                            Layout.fillWidth: true
                            Accessible.name: "Window opacity"
                            background: Rectangle { x: opacitySlider.leftPadding; y: opacitySlider.topPadding + opacitySlider.availableHeight/2-2; width: opacitySlider.availableWidth; height: 3; color: root.edge
                                Rectangle { width: parent.width*opacitySlider.visualPosition; height: 3; color: root.accent }
                            }
                            handle: Rectangle { implicitWidth: 16; implicitHeight: 16; x: opacitySlider.leftPadding + opacitySlider.visualPosition*(opacitySlider.availableWidth-width); y: opacitySlider.topPadding+opacitySlider.availableHeight/2-height/2; width: 16; height: 16; radius: 3; color: opacitySlider.pressed ? root.accent : root.ink; border.color: opacitySlider.activeFocus ? root.bright : root.edge }
                        }
                        Sub { text: (opacitySlider.value*100).toFixed(1)+"% opacity" }
                    }
                    Sub { visible: root.settingsTab === "appearance"; width: parent.width; wrapMode: Text.WordWrap; text: "Drag to preview. Save preferences to keep it. At 100%, the background is fully opaque." }
                    Label { visible: root.settingsTab === "pricing"; text: "Monthly subscription prices · USD"; font.pixelSize: 16 }
                    Sub { visible: root.settingsTab === "pricing"; text: "Optional. API value is an estimate, separate from subscription charges." }
                    Flow { visible: root.settingsTab === "pricing"; width: parent.width; spacing: 14
                        Repeater { model: root.providerOptions.filter(p => root.draftEnabled.indexOf(p.id)>=0)
                            Column {
                                required property var modelData
                                spacing: 6
                                Sub { text: modelData.name }
                                Field { width: 170; placeholderText: "Not set"; Accessible.name: modelData.name+" monthly price"
                                    text: root.draftPrices[modelData.id] || ""
                                    onTextEdited: root.draftPrices[modelData.id] = text
                                }
                            }
                        }
                        Repeater { model: root.draftAccounts
                            Column {
                                required property var modelData
                                spacing: 6
                                Sub { text: (modelData.label || "Untitled account") + (root.accountSources(modelData) ? " · " + root.accountSources(modelData) : "") }
                                Field { width: 170; placeholderText: "Not set"; Accessible.name: (modelData.label || "Account")+" monthly price"
                                    text: root.draftPrices[modelData.id] || ""
                                    onTextEdited: root.draftPrices[modelData.id] = text
                                }
                            }
                        }
                    }
                    Sub { visible: root.settingsTab === "pricing"; width: parent.width; wrapMode: Text.WordWrap; text: "Prices on a provider apply to the local history group; prices on a labelled account apply only to that account." }
                    Sub { visible: root.settingsTab === "sources"; width: parent.width; wrapMode: Text.WordWrap; text: "Quota sources: Grok and Muse read their existing logins; Ollama Cloud, CommandCode, and ClinePass read a key here, an environment variable, or a key file under ~/.config/omarchy/ai-usage." }
                    Label { visible: root.settingsTab === "sources"; text: "Ollama Cloud API key"; font.pixelSize: 16 }
                    Sub { visible: root.settingsTab === "sources"; width: parent.width; wrapMode: Text.WordWrap; text: "Optional. Only needed for Ollama Cloud limits; token history works without it." }
                    Field { visible: root.settingsTab === "sources"; width: 420; text: root.draftOllamaKey; placeholderText: "Not set"; echoMode: TextInput.Password; onTextEdited: root.draftOllamaKey = text; Accessible.name: "Ollama Cloud API key" }
                    Label { visible: root.settingsTab === "sources"; text: "CommandCode API key"; font.pixelSize: 16 }
                    Sub { visible: root.settingsTab === "sources"; width: parent.width; wrapMode: Text.WordWrap; text: "Optional. Only needed for CommandCode plan limits; token history works without it." }
                    Field { visible: root.settingsTab === "sources"; width: 420; text: root.draftCommandCodeKey; placeholderText: "Not set"; echoMode: TextInput.Password; onTextEdited: root.draftCommandCodeKey = text; Accessible.name: "CommandCode API key" }
                    Label { visible: root.settingsTab === "sources"; text: "ClinePass API key"; font.pixelSize: 16 }
                    Sub { visible: root.settingsTab === "sources"; width: parent.width; wrapMode: Text.WordWrap; text: "Optional. Only needed for ClinePass plan limits; token history works without it." }
                    Field { visible: root.settingsTab === "sources"; width: 420; text: root.draftClinePassKey; placeholderText: "Not set"; echoMode: TextInput.Password; onTextEdited: root.draftClinePassKey = text; Accessible.name: "ClinePass API key" }
                    Label { visible: root.settingsTab === "accounts"; text: "History accounts"; font.pixelSize: 16 }
                    Sub { visible: root.settingsTab === "accounts"; width: parent.width; wrapMode: Text.WordWrap; text: "Label agent home folders by account. Keep mirrored folders under the same account. Labels do not switch logins; quota is only for the current login on this PC." }
                    Field { visible: root.settingsTab === "accounts"; width: 260; text: root.draftLocalLabel; placeholderText: "Local account name"; onTextEdited: root.draftLocalLabel = text; Accessible.name: "Local account name" }
                    Repeater { model: root.draftAccounts
                        Column {
                            id: accountEditor
                            required property var modelData
                            required property int index
                            visible: root.settingsTab === "accounts"
                            width: parent.width; spacing: 10
                            RowLayout { width: parent.width
                                Field { Layout.fillWidth: true; text: accountEditor.modelData.label; placeholderText: "Account name, e.g. Work"; onTextEdited: root.draftAccounts[accountEditor.index].label = text }
                                Choice { text: "Remove account"; onClicked: { var a=root.draftAccounts.slice(); a.splice(accountEditor.index,1); root.draftAccounts=a } }
                            }
                            Repeater { model: accountEditor.modelData.directories
                                RowLayout {
                                    required property var modelData
                                    required property int index
                                    width: accountEditor.width
                                    ComboBox { Layout.preferredWidth: 170; model: root.providerOptions; textRole: "name"; valueRole: "id"; currentIndex: root.providerOptions.findIndex(p=>p.id===modelData.provider)
                                        font.family: root.fontFamily; font.pixelSize: 12; implicitHeight: 42; palette.button: root.base; palette.buttonText: root.ink; palette.window: root.base; palette.text: root.ink; palette.highlight: root.accent
                                        onActivated: root.draftAccounts[accountEditor.index].directories[index].provider = currentValue
                                    }
                                    Field { Layout.fillWidth: true; text: modelData.path; placeholderText: "Full agent home folder, e.g. /mnt/work/.codex"; onTextEdited: root.draftAccounts[accountEditor.index].directories[index].path = text }
                                    Choice { text: "Remove"; onClicked: { root.draftAccounts[accountEditor.index].directories.splice(index,1); root.draftAccounts=JSON.parse(JSON.stringify(root.draftAccounts)) } }
                                }
                            }
                            Choice { text: "Add folder"; onClicked: { root.draftAccounts[accountEditor.index].directories.push({provider:"codex",path:""}); root.draftAccounts=JSON.parse(JSON.stringify(root.draftAccounts)) } }
                            Rectangle { width: parent.width; height: 1; color: root.edge }
                        }
                    }
                    Choice { visible: root.settingsTab === "accounts"; text: "Add account"; onClicked: { root.draftAccounts=root.draftAccounts.concat([{id:"account-"+Date.now()+"-"+Math.random().toString(36).slice(2,8),label:"",directories:[{provider:"codex",path:""}]}]) } }
                    Label { visible: root.settingsTab === "accounts"; text: "Unlabelled additional history folders"; font.pixelSize: 16 }
                    Sub { visible: root.settingsTab === "accounts"; width: parent.width; wrapMode: Text.WordWrap; text: "One full home-folder path per line, such as /mnt/other-computer/.codex. Use folders you have already mounted or synced. No remote connection is made. Copies with stable session IDs are deduplicated." }
                    Repeater { model: root.homeOptions.filter(h => root.draftEnabled.indexOf(h.key.replace(/Homes$/, ""))>=0 || (h.key === "opencodeHomes" && root.draftEnabled.indexOf("opencode-go")>=0) || (h.key === "hermesHomes" && ["opencode-go", "ollama-cloud", "commandcode"].some(id => root.draftEnabled.indexOf(id) >= 0)))
                        Column {
                            required property var modelData
                            visible: root.settingsTab === "accounts"
                            width: parent.width; spacing: 6
                            Sub { text: modelData.name }
                            Homes { width: parent.width; height: 70; placeholderText: modelData.example; Accessible.name: modelData.name
                                text: root.draftHomes[modelData.key] || ""
                                onTextChanged: root.draftHomes[modelData.key] = text
                            }
                        }
                    }
                    Label { visible: root.settingsTab === "sync"; text: "Synced machines"; font.pixelSize: 16 }
                    Sub { visible: root.settingsTab === "sync"; width: parent.width; wrapMode: Text.WordWrap; text: "Point each machine at one synced folder. Each writes a ledger snapshot and imports the others. Token counters, timestamps, model names, and source paths are included." }
                    Field { visible: root.settingsTab === "sync"; width: parent.width; text: root.draftLedgerSyncDir; placeholderText: "Shared folder, e.g. ~/Sync/ai-usage"; onTextEdited: root.draftLedgerSyncDir = text; Accessible.name: "Synced ledger folder" }
                    Field { visible: root.settingsTab === "sync"; width: 260; text: root.draftLedgerDeviceId; placeholderText: "Device id, e.g. desktop"; onTextEdited: root.draftLedgerDeviceId = text; Accessible.name: "Synced ledger device id" }
                    Sub { visible: root.settingsTab === "sync"; width: parent.width; wrapMode: Text.WordWrap; text: "The device id names this machine's snapshot ("+(root.draftLedgerDeviceId||"hostname")+".sqlite) and must be unique per machine." }
                    Card {
                        visible: root.settingsTab === "sources"
                        width: parent.width; height: visible ? 88 : 0
                        Column { anchors.fill: parent; anchors.margins: 16; spacing: 8
                            Label { text: "OpenCode Go connection" }
                            Sub { width: parent.width; wrapMode: Text.WordWrap; text: "Uses the existing OpenCode Go API key in OpenCode. No cookie is needed. Quota is refreshed in the background; reconnect in OpenCode if authentication expires." }
                        }
                    }


                }
            }
        }
        }
    }
}
