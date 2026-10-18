"""Regression checks from the public-readiness review, using synthetic data only."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_collector', ROOT / 'collector.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = {key: str(self.root / key.lower()) for key in (
            'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'CODEX_HOME',
            'CLAUDE_CONFIG_DIR', 'GROK_HOME', 'PI_CODING_AGENT_DIR', 'MUSE_HOME', 'CURSOR_HOME')}
        env.update(HOME=tmp.name, AI_USAGE_DEMO='')
        for patcher in (patch.dict(os.environ, env), patch.object(c, 'HOME', self.root),
                        patch.object(c, 'STATE', self.root / 'state'),
                        patch.object(c, 'CONFIG', self.root / 'settings.json'),
                        patch.object(c.urllib.request, 'urlopen', side_effect=AssertionError('unexpected network'))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ledger(self):
        ledger = c.Ledger(c.STATE / 'usage.sqlite')
        self.addCleanup(ledger.db.close)
        return ledger

    def invoke(self, *args):
        with patch('sys.argv', ['collector.py', *args]), contextlib.redirect_stdout(io.StringIO()) as out:
            c.main()
        return json.loads(out.getvalue())

    def test_report_never_scans_or_creates_state(self):
        with patch.object(c.Ledger, 'scan', side_effect=AssertionError('report scanned')):
            result = self.invoke('report')
        self.assertEqual(result['summary']['tokens'], 0)
        self.assertFalse(c.STATE.exists())

    def test_report_keeps_database_and_scan_metadata_unchanged(self):
        ledger = self.ledger()
        ledger.put(c.record('one', 'codex', 's', dt.datetime.now().isoformat(), 'unknown', '', 'CLI', input=17))
        ledger.db.execute("INSERT INTO metadata VALUES ('scan', ?)", (json.dumps({'scannedAt': 1}),))
        ledger.db.commit()
        before = ledger.db.execute('SELECT * FROM metadata').fetchall()
        with patch.object(c.Ledger, 'scan', side_effect=AssertionError('report scanned')):
            result = self.invoke('report')
        self.assertEqual(result['summary']['tokens'], 17)
        self.assertEqual(ledger.db.execute('SELECT * FROM metadata').fetchall(), before)
        readonly = c.Ledger(c.STATE / 'usage.sqlite', readonly=True)
        try:
            with self.assertRaises(sqlite3.OperationalError): readonly.db.execute('DELETE FROM events')
        finally: readonly.db.close()

    def test_cursor_full_fallback_page_does_not_truncate_history(self):
        class BadRequest(Exception): code = 400
        first = 1788815371513
        seen = []
        def post(token, method, body):
            seen.append(dict(body))
            if body['pageSize'] == 1000: raise BadRequest()
            start = first if 'endDate' not in body else body['endDate']
            count = 100 if 'endDate' not in body else 1
            return {'usageEventsDisplay': [{'timestamp': start - n, 'model': 'm',
                     'tokenUsage': {'inputTokens': '300'}} for n in range(count)]}
        with patch.object(c, 'cursor_post', post):
            rows, oldest, complete = c.cursor_walk('synthetic')
        self.assertTrue(complete)
        self.assertEqual(len(rows), 101)
        self.assertEqual(sum(r['input'] for r in rows), 30300)
        self.assertEqual(oldest, first - 100)

    def test_cursor_resume_does_not_advance_past_unfetched_arrivals(self):
        ledger = self.ledger()
        c.atomic_json(c.STATE / 'cursor-usage.json', {'historyVersion': 1, 'newestTs': 1788815371000,
            'billingCycleStart': 1780000000000, 'fullPullPending': True, 'resumeFloorMs': 1788000000000})
        old = c.cursor_api_record({'timestamp': 1787000000000, 'tokenUsage': {'inputTokens': 3}})
        with patch.object(c, 'cursor_token', return_value='synthetic'), \
             patch.object(c, 'cursor_summary', return_value={'billingCycleStart': 1780000000000}), \
             patch.object(c, 'cursor_walk', return_value=([old], 1787000000000, True)) as walk:
            c.cursor_usage(ledger, force=True)
            walk.assert_called_once_with('synthetic', 1787999999999, 0, 1780000000000)
        saved = json.loads((c.STATE / 'cursor-usage.json').read_text())
        self.assertEqual(saved['newestTs'], 1788815371000)
        self.assertFalse(saved['fullPullPending'])
        with patch.object(c, 'cursor_token', return_value='synthetic'), \
             patch.object(c, 'cursor_summary', return_value={'billingCycleStart': 1780000000000}), \
             patch.object(c, 'cursor_walk', return_value=([], None, True)) as walk:
            c.cursor_usage(ledger, force=True)
            walk.assert_called_once_with('synthetic', None, 1788815371000, 1780000000000)

    def test_cursor_throttle_preserves_failure_status(self):
        import time
        c.atomic_json(c.STATE / 'cursor-usage.json', {'historyVersion': 1, 'attemptedAt': time.time(), 'error': 'offline'})
        source, warnings = c.cursor_usage(self.ledger())
        self.assertEqual(source['readErrors'], 1)
        self.assertTrue(warnings)

    def test_cursor_force_reaches_cloud_collector(self):
        with patch.object(c, 'cursor_usage', return_value=({'provider': 'cursor', 'exists': True}, [])) as usage:
            ledger = self.ledger()
            ledger.scan(c.DEFAULTS | {'enabled': ['cursor']}, force=True)
            usage.assert_called_once_with(ledger, force=True)

    def test_go_exception_text_cannot_leak_a_key(self):
        auth = Path(os.environ['XDG_DATA_HOME']) / 'opencode/auth.json'
        c.atomic_json(auth, {'opencode-go': {'type': 'api', 'key': 'synthetic-secret'}})
        c.atomic_json(c.STATE / 'go-quota.json', {'limits': [{'label': 'Monthly', 'percent': .7}], 'updatedAt': 'old'})
        with patch.object(c.urllib.request, 'urlopen', side_effect=ValueError('synthetic-secret')):
            result = c.go_quota(force=True)
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertNotIn('synthetic-secret', (c.STATE / 'go-quota.json').read_text())
        self.assertEqual(result['updatedAt'], 'old')
        self.assertEqual(result['limits'][0]['percent'], .7)

    def test_go_key_prefers_opencode_v2_credential(self):
        import sqlite3
        root = Path(os.environ['XDG_DATA_HOME']) / 'opencode'
        c.atomic_json(root / 'auth.json', {'opencode-go': {'type': 'api', 'key': 'expired-v1'}})
        self.assertEqual(c.go_api_key(), 'expired-v1')
        conn = sqlite3.connect(root / 'opencode.db')
        conn.execute('CREATE TABLE credential (id text PRIMARY KEY, integration_id text, label text NOT NULL, value text NOT NULL, '
                     'connector_id text, method_id text, active integer, time_created integer NOT NULL, time_updated integer NOT NULL)')
        conn.executemany('INSERT INTO credential VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)', [
            ('a', 'opencode-go', 'API key', json.dumps({'type': 'api', 'key': 'expired-v1'}), 0, 1, 3),
            ('b', 'opencode-go', 'OpenCode Go', json.dumps({'type': 'api', 'key': 'current'}), 1, 2, 2),
            ('c', 'openrouter', 'API key', json.dumps({'type': 'api', 'key': 'other'}), 1, 1, 4)])
        conn.commit(); conn.close()
        self.assertEqual(c.go_api_key(), 'current')

    def test_retired_providers_leave_settings_and_ledger(self):
        c.atomic_json(c.CONFIG, {'enabled': ['codex', 'ollama-cloud', 'clinepass'],
                                 'ollamaApiKey': 'old-key', 'clinepassApiKey': 'old-key'})
        cfg = c.settings()
        self.assertEqual(cfg['enabled'], ['codex'])
        self.assertNotIn('old-key', json.dumps(c.masked_settings(cfg)))
        ledger = self.ledger()
        for provider in ('codex', 'ollama-cloud', 'clinepass'):
            ledger.put(c.record(provider, provider, 's', 1789000000, 'm', '/p', 'Hermes', input=10), '/source/' + provider)
        c.purge_retired_providers(ledger)
        self.assertEqual(ledger.db.execute('SELECT provider FROM events').fetchall(), [('codex',)])
        self.assertEqual(ledger.db.execute('SELECT event_id FROM event_sources').fetchall(), [('codex',)])

    def test_go_rejected_key_says_reconnect(self):
        import urllib.error
        with patch.object(c, 'go_api_key', return_value='synthetic'), \
             patch.object(c.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError('u', 401, 'expired', {}, None)):
            result = c.go_quota(force=True)
        self.assertIn('Reconnect Go in OpenCode', result['error'])

    def test_resale_routes_do_not_inherit_another_providers_price(self):
        for provider in ('commandcode',):
            row = c.record('x', provider, 's', 1, 'unlisted', '', 'Hermes', input=10)
            self.assertEqual(c.price(row, {'unlisted': {'input_cost_per_token': 2}}), (None, None))
            self.assertEqual(c.price(row, {provider + '/unlisted': {'input_cost_per_token': 2}})[0], 20)

    def test_cursor_missing_cost_is_unpriced_not_free(self):
        for value in (None, 'broken', float('nan'), float('inf')):
            row = c.cursor_api_record({'timestamp': 1788815371513,
                'model': 'm', 'tokenUsage': {'inputTokens': '300', 'totalCents': value}})
            self.assertEqual(row['input'], 300)
            self.assertEqual(c.price(row, {'m': {'input_cost_per_token': 1}}), (None, None))
        row = c.cursor_api_record({'timestamp': 1788815371513,
            'tokenUsage': {'inputTokens': 300, 'totalCents': 0}})
        self.assertEqual(c.price(row, {}), (0, None))
        ledger = self.ledger()
        ledger.put(row)
        ledger.put(dict(row, reportedValue=None))
        self.assertIsNone(ledger.db.execute('SELECT reportedValue FROM events').fetchone()[0])

    def test_cursor_old_watermark_is_rebuilt(self):
        ledger = self.ledger()
        c.atomic_json(c.STATE / 'cursor-usage.json', {'billingCycleStart': 1780000000000,
            'newestTs': 1788815371000, 'fullPullPending': False})
        with patch.object(c, 'cursor_token', return_value='synthetic'), \
             patch.object(c, 'cursor_summary', return_value={'billingCycleStart': 1780000000000}), \
             patch.object(c, 'cursor_walk', return_value=([], None, True)) as walk:
            c.cursor_usage(ledger, force=True)
            walk.assert_called_once_with('synthetic', None, 0, 1780000000000)

    def test_cursor_events_are_durable_before_watermark(self):
        ledger = self.ledger()
        row = c.cursor_api_record({'timestamp': 1788815371513, 'tokenUsage': {'inputTokens': 3}})
        save = c.atomic_json
        def check_save(path, value):
            if path.name == 'cursor-usage.json' and value.get('newestTs'):
                other = sqlite3.connect(c.STATE / 'usage.sqlite')
                try: self.assertEqual(other.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
                finally: other.close()
            return save(path, value)
        with patch.object(c, 'cursor_token', return_value='synthetic'), \
             patch.object(c, 'cursor_summary', return_value={}), \
             patch.object(c, 'cursor_walk', return_value=([row], 1788815371513, True)), \
             patch.object(c, 'atomic_json', check_save):
            _, warnings = c.cursor_usage(ledger, force=True)
        self.assertEqual(warnings, [])

    def test_device_id_cannot_escape_sync_directory(self):
        for device in ('../escape', '/tmp/escape', '..', 'folder/name', 'folder\\name'):
            with self.assertRaises(ValueError): c.save_settings(c.DEFAULTS | {'ledgerDeviceId': device})
        self.assertEqual(c.save_settings(c.DEFAULTS | {'ledgerDeviceId': 'desktop-01'})['ledgerDeviceId'], 'desktop-01')

    def test_go_allowance_respects_account_and_project(self):
        ledger = self.ledger()
        now = dt.datetime.now().astimezone()
        for name, account, project, tokens in [('a', 'work', '/a', 10), ('b', 'local', '/a', 20), ('c', 'work', '/b', 30)]:
            ledger.put(c.record(name, 'opencode-go', name, now.timestamp() - 1, 'priced', project, 'CLI', input=tokens), self.root / account / 'history')
        cfg = c.DEFAULTS | {'enabled': ['opencode-go'], 'accounts': [
            {'id': 'work', 'label': 'Work', 'directories': [{'provider': 'opencode-go', 'path': str(self.root / 'work')}]}]}
        rates = c.load_rates()
        rates['document']['opencode-go/priced'] = {'input_cost_per_token': 1, 'monthly_limit_usd': 100}
        with patch.object(c, 'load_rates', return_value=rates):
            report = c.report(ledger, cfg, now=now, selection={'account': 'work', 'project': '/a'})
            other = c.report(ledger, cfg, now=now, provider='codex')
        self.assertEqual(other['goAllowance']['models'], [])
        self.assertEqual(report['goAllowance']['models'][0]['value'], 10)


if __name__ == '__main__': unittest.main()
