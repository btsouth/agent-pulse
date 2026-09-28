#!/usr/bin/env python3
"""Exercise the real panel's model summary without opening a desktop window."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


repo = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--capture-source', type=Path, help='save an offscreen source picker image')
parser.add_argument('--capture-header', type=Path, help='save the offscreen panel header image')
parser.add_argument('--capture-hourly', type=Path, help='save the offscreen hourly section image for the selected source')
parser.add_argument('--hourly-source', default='cursor', help='source shown in the hourly section image')
parser.add_argument('--theme', choices=['dark', 'light'], default='dark')
parser.add_argument('--panel-width', type=int, default=460)
parser.add_argument('--panel-height', type=int, default=360)
args = parser.parse_args()
omarchy_ui = Path('/usr/share/omarchy/shell/Ui')
omarchy_commons = Path('/usr/share/omarchy/shell/Commons')
if not omarchy_ui.exists() or not omarchy_commons.exists():
    raise SystemExit('This check needs the installed Omarchy shell')

with tempfile.TemporaryDirectory(prefix='.panel-qa-', dir=repo) as temp:
    root = Path(temp)
    (root / 'Commons').symlink_to(omarchy_commons)
    ui = root / 'Ui'
    ui.mkdir()
    for source in omarchy_ui.iterdir():
        if source.name != 'KeyboardPanel.qml':
            (ui / source.name).symlink_to(source)
    # The compositor-backed popup cannot load on Qt's offscreen platform.
    # Keep the panel content and model calculation, with only that host stubbed.
    (ui / 'KeyboardPanel.qml').write_text('''import QtQuick
Item {
  property Item anchorItem
  property var owner
  property var bar
  property bool open: false
  property Item focusTarget
  property int contentWidth: 460
  property int contentHeight: 900
  function fittedContentWidth(value) { return Math.min(value, PANEL_WIDTH) }
  function fittedContentHeight(value, maximum) { return Math.min(value, maximum, PANEL_HEIGHT) }
  width: contentWidth
  height: contentHeight
}
'''.replace('PANEL_WIDTH', str(args.panel_width)).replace('PANEL_HEIGHT', str(args.panel_height)))
    shutil.copytree(repo / 'plugin', root / 'plugin')
    panel_file = root / 'plugin/Panel.qml'
    panel_file.write_text(panel_file.read_text().replace(
        '  function modelTooltip(row) {',
        '  function qaSourcePicker() { return {label: providerSwitch.label, value: providerSwitch.value, text: providerSwitch.currentLabel()} }\n'
        '  function qaHeader() { return {pickerX: providerSwitch.mapToItem(column, 0, 0).x, pickerY: providerSwitch.mapToItem(column, 0, 0).y, pickerWidth: providerSwitch.width, buttonX: analyticsButton.mapToItem(column, 0, 0).x, buttonY: analyticsButton.mapToItem(column, 0, 0).y, buttonWidth: analyticsButton.width, columnWidth: column.width, pinnedY: pinnedSection.y} }\n'
        '  function qaCaptureSource(path) { return providerSwitch.grabToImage(function(image) { image.saveToFile(path) }) }\n\n'
        '  function qaCaptureHeader(path) { return headerControls.grabToImage(function(image) { image.saveToFile(path) }) }\n\n'
        '  function qaHourly() { return {headline: hourlyHeadline(), gap: hourlyGap, missing: hourlyMissingText(), rows: hourRows().map(function(row) { return row.tokens }), total: hourlyTotal("tokens")} }\n'
        '  function qaCaptureHourly(path) { return hourlySection.grabToImage(function(image) { image.saveToFile(path) }) }\n\n'
        '  function qaScrollState() { return {contentY: panelFlick.contentY, contentHeight: panelFlick.contentHeight, height: panelFlick.height, size: panelScroll.size, position: panelScroll.position, width: panelScroll.width, visible: panelScroll.visible, outsideClip: panelScroll.parent === keyCatcher} }\n'
        '  function qaScrollDown() { panelScroll.increase(); return qaScrollState() }\n\n'
        '  function qaScrollBar() { return panelScroll }\n'
        '  function qaResetScroll() { panelFlick.contentY = 0 }\n\n'
        '  function modelTooltip(row) {'))
    (root / 'shell.qml').write_text('''import QtQuick
import QtQuick.Window
import QtTest
import Quickshell
import Quickshell.Io
import "plugin" as Plugin
ShellRoot {
  Window {
    width: 600
    height: 900
    visible: true
    color: "#151b18"
    Plugin.Panel { id: panel; width: 460; height: 20 }
  }
  // Keep the QtTest pointer helper available for IPC without auto-running a test suite.
  TestCase { id: dragDriver; name: "PanelScrollDrag"; when: false }
  IpcHandler {
    target: "panelqa"
    function inspect(): string {
      return JSON.stringify(panel.models.map(row => ({name: row.name, total: row.total,
        details: panel.modelTooltip(row)})))
    }
    function watchLimits(): string { return JSON.stringify(panel.allLimitRows()) }
    function source(): string { return JSON.stringify(panel.qaSourcePicker()) }
    function header(): string { return JSON.stringify(panel.qaHeader()) }
    function selectCodex(): void { panel.selectedProviderId = "codex" }
    function select(id: string): void { panel.selectedProviderId = id }
    function hourly(): string { return JSON.stringify(panel.qaHourly()) }
    function captureHourly(path: string): string { return String(panel.qaCaptureHourly(path)) }
    function captureSource(path: string): string { return String(panel.qaCaptureSource(path)) }
    function captureHeader(path: string): string { return String(panel.qaCaptureHeader(path)) }
    function scrollState(): string { return JSON.stringify(panel.qaScrollState()) }
    function scrollDown(): string { return JSON.stringify(panel.qaScrollDown()) }
    function dragScroll(): string {
      panel.qaResetScroll()
      var scroll = panel.qaScrollBar()
      dragDriver.mouseDrag(scroll, scroll.width / 2, Math.max(8, scroll.size * scroll.height / 2), 0, 120)
      return JSON.stringify(panel.qaScrollState())
    }
  }
}
''')
    usage = root / 'state/omarchy/agents/usage'
    usage.mkdir(parents=True)
    reset_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    (usage / 'codex.json').write_text(json.dumps({
        'id': 'codex', 'name': 'Codex', 'activeDays': 1,
        'limits': [{'label': 'Weekly (7-day)', 'percent': 0.26, 'resetsAt': reset_at}],
        'modelUsage': {'gpt-6-sol': {'inputTokens': 100, 'outputTokens': 50,
                                     'cacheReadInputTokens': 200, 'cacheCreationInputTokens': 25}},
    }))
    (usage / 'claude.json').write_text(json.dumps({
        'id': 'claude', 'name': 'Claude', 'activeDays': 1,
        'limits': [
            {'label': 'Session', 'title': 'Session', 'percent': 1, 'resetsAt': reset_at},
            {'label': 'Weekly (7-day)', 'title': 'Weekly', 'percent': 0.97, 'resetsAt': reset_at},
            {'label': 'Model weekly', 'title': 'Weekly', 'percent': 0.98, 'resetsAt': reset_at},
            {'label': 'Monthly', 'title': 'Monthly', 'percent': 0.59},
            {'label': 'Expired', 'percent': 1, 'resetsAt': '2020-01-01T00:00:00Z'},
        ],
        'modelUsage': {'claude-sonnet-4': {'inputTokens': 20}},
    }))
    # A source that keeps no local logs, and one whose own record counts
    # tokens although the ledger has never indexed it.
    (usage / 'cursor.json').write_text(json.dumps({
        'id': 'cursor', 'name': 'Cursor', 'hasLocalStats': False,
        'limits': [{'label': 'Monthly', 'percent': 0.1}]}))
    (usage / 'grok.json').write_text(json.dumps({
        'id': 'grok', 'name': 'Grok Build', 'todayTotalTokens': 5000,
        'limits': [{'label': 'Monthly', 'percent': 0.1}]}))
    now = dt.datetime.now().astimezone()
    hour = int(dt.datetime.combine(now.date(), dt.time(now.hour)).timestamp())
    hourly = root / 'state/omarchy/ai-usage/hourly-summary.json'
    hourly.parent.mkdir(parents=True)
    # Claude's provider total holds every login; its own record holds 900.
    hourly.write_text(json.dumps({
        'schemaVersion': 1, 'date': str(now.date()), 'generatedAt': now.timestamp(),
        'utcOffsetMinutes': int(now.utcoffset().total_seconds() // 60),
        'availableProviders': ['claude'], 'availableSources': ['claude'],
        'providers': {'claude': {'tokens': 2000, 'unplacedTokens': 0}},
        'sources': {'claude': {'tokens': 900, 'unplacedTokens': 0}, 'codex': {'tokens': 0, 'unplacedTokens': 0}},
        'hours': [{'start': hour - 3600, 'label': 'earlier', 'zone': 'X', 'providers': {'claude': 1500}, 'sources': {'claude': 400}},
                  {'start': hour, 'label': 'now', 'zone': 'X', 'providers': {'claude': 500}, 'sources': {'claude': 500}}]}))
    pins = root / 'config/omarchy/ai-usage/pinned-limit.json'
    pins.parent.mkdir(parents=True)
    pins.write_text(json.dumps({'pins': [{'provider': 'codex', 'label': 'Weekly (7-day)', 'title': 'Weekly'},
        {'provider': 'claude', 'label': 'Session', 'title': 'Session'}]}))
    theme = root / '.local/state/omarchy/current/theme/colors.toml'
    theme.parent.mkdir(parents=True)
    theme.write_text('background = "#151b18"\nforeground = "#e8e6da"\naccent = "#7aaf92"\n'
                     if args.theme == 'dark' else
                     'background = "#faf7f0"\nforeground = "#292d32"\naccent = "#28654a"\n')
    refresh = root / '.local/bin/omarchy-usage-dashboard-refresh'
    refresh.parent.mkdir(parents=True)
    refresh.write_text('#!/bin/sh\nexit 0\n')
    refresh.chmod(0o755)
    env = dict(os.environ, HOME=temp, XDG_CONFIG_HOME=temp + '/config',
               XDG_STATE_HOME=temp + '/state', XDG_DATA_HOME=temp + '/data',
               AI_USAGE_ROOT=str(repo), QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software')
    proc = subprocess.Popen(['quickshell', '-p', str(root), '--no-color'], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        rows = None
        for _ in range(40):
            if proc.poll() is not None:
                break
            call = subprocess.run(['quickshell', 'ipc', '-p', str(root), '--any-display',
                                   'call', 'panelqa', 'inspect'], env=env,
                                  capture_output=True, text=True)
            if call.returncode == 0:
                try:
                    rows = json.loads(call.stdout.strip())
                    if len(rows) == 2:
                        break
                except ValueError:
                    pass
            time.sleep(0.1)
        assert rows and [row['total'] for row in rows] == [375, 20], rows
        assert 'cache read 200' in rows[0]['details'], rows
        def ipc(method, *arguments):
            call = subprocess.run(['quickshell', 'ipc', '-p', str(root), '--any-display',
                                   'call', 'panelqa', method, *arguments], env=env,
                                  capture_output=True, text=True, check=True)
            return call.stdout.strip()
        watched = json.loads(ipc('watchLimits'))
        assert [row['percent'] for row in watched] == [0.98, 0.97, 0.59], watched
        assert [row['label'] for row in watched] == ['Model weekly', 'Weekly (7-day)', 'Monthly'], watched
        source = json.loads(ipc('source'))
        assert source == {'label': 'SOURCE', 'value': 'all', 'text': 'All sources'}, source
        header = json.loads(ipc('header'))
        assert header['pickerY'] == 0 and abs(header['buttonY'] - header['pickerY']) <= 3, header
        assert header['pickerX'] >= 3 and header['pickerWidth'] >= 120, header
        assert header['buttonX'] > header['pickerX'] + header['pickerWidth'], header
        assert header['buttonX'] + header['buttonWidth'] < header['columnWidth'], header
        assert header['pinnedY'] > header['pickerY'], header
        if args.capture_source:
            image_path = args.capture_source.resolve()
            image_path.unlink(missing_ok=True)
            assert ipc('captureSource', str(image_path)) == 'true'
            for _ in range(30):
                if image_path.exists(): break
                time.sleep(0.1)
            assert image_path.exists(), image_path
            print('Captured source picker:', image_path)
        if args.capture_header:
            image_path = args.capture_header.resolve()
            image_path.unlink(missing_ok=True)
            assert ipc('captureHeader', str(image_path)) == 'true'
            for _ in range(30):
                if image_path.exists(): break
                time.sleep(0.1)
            assert image_path.exists(), image_path
            print('Captured panel header:', image_path)
        before_scroll = json.loads(ipc('scrollState'))
        after_scroll = json.loads(ipc('scrollDown'))
        assert before_scroll['contentHeight'] > before_scroll['height'], before_scroll
        assert before_scroll['visible'] and before_scroll['outsideClip'] and before_scroll['width'] >= 12, before_scroll
        assert after_scroll['contentY'] > before_scroll['contentY'], (before_scroll, after_scroll)
        dragged = json.loads(ipc('dragScroll'))
        assert dragged['contentY'] > 0, dragged
        ipc('selectCodex')
        focused = json.loads(ipc('source'))
        assert focused == {'label': 'SOURCE', 'value': 'codex', 'text': 'Main'}, focused
        def hourly_for(source):
            ipc('select', source)
            time.sleep(0.2)
            return json.loads(ipc('hourly'))
        everyone = hourly_for('all')
        # All sources adds the providers, which hold every login.
        assert everyone['total'] == 2000 and everyone['rows'] == [500, 1500], everyone
        assert everyone['missing'].startswith('No hourly history for ') and all(
            name in everyone['missing'] for name in ('Codex', 'Cursor', 'Grok Build')), everyone
        # One source reads its own record, not its provider's total.
        own = hourly_for('claude')
        assert own['headline'] == '900 processed tokens today' and own['rows'] == [500, 400] and not own['gap'], own
        idle = hourly_for('codex')
        assert idle['gap'] is not None and idle['headline'] == 'Not indexed yet', idle
        assert 'Codex' in idle['gap']['detail'] and idle['rows'] == [], idle
        local = hourly_for('cursor')
        assert local['headline'] == 'No hourly token history' and 'only its limits' in local['gap']['detail'], local
        counted = hourly_for('grok')
        assert counted['headline'] == '5,000 tokens today' and 'No hourly breakdown' in counted['gap']['detail'], counted
        if args.capture_hourly:
            hourly_for(args.hourly_source)
            image_path = args.capture_hourly.resolve()
            image_path.unlink(missing_ok=True)
            assert ipc('captureHourly', str(image_path)) == 'true'
            for _ in range(30):
                if image_path.exists(): break
                time.sleep(0.1)
            assert image_path.exists(), image_path
            print('Captured hourly section:', image_path)
        print('Offscreen panel model rows, source picker, hourly section, and attached scrollbar passed')
    finally:
        proc.terminate()
        try:
            log = proc.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            log = proc.communicate()[0]
        if any(error in log for error in ('ReferenceError', 'TypeError', 'Unable to assign', 'Failed to load')):
            raise RuntimeError(log)
