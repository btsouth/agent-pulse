import datetime as dt
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import sqlite3
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('collector', Path(__file__).parents[1] / 'collector.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class _HTTPError(Exception):
    def __init__(self, code): super().__init__('HTTP ' + str(code)); self.code = code


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for patcher in (patch.object(c, 'HOME', self.root),
                        patch.dict(os.environ, self.sandbox_env()),
                        patch.object(c.urllib.request, 'urlopen', side_effect=AssertionError('Unexpected network in test')),
                        # No version means no update check, so a scan under
                        # test never asks GitHub for the latest release.
                        patch.object(c, 'installed_version', return_value='')):
            patcher.start(); self.addCleanup(patcher.stop)
        # Every path the collector writes must resolve inside the fixture. A
        # test that reaches the real home would overwrite the user's settings.
        self.conf = patch.object(c, 'CONFIG', self.root / 'config/omarchy/ai-usage/settings.json')
        self.conf.start(); self.addCleanup(self.conf.stop)
        self.pin_conf = patch.object(c, 'PINNED_LIMIT', self.root / 'config/omarchy/ai-usage/pinned-limit.json')
        self.pin_conf.start(); self.addCleanup(self.pin_conf.stop)
        self.state = patch.object(c, 'STATE', self.root / 'state')
        self.state.start(); self.addCleanup(self.state.stop)
        # Fail loudly rather than overwrite a real preference file.
        self.assertTrue(str(c.CONFIG).startswith(str(self.root)))
        self.assertTrue(str(c.PINNED_LIMIT).startswith(str(self.root)))
        self.assertTrue(str(c.STATE).startswith(str(self.root)))

    def tearDown(self):
        self.tmp.cleanup()

    def sandbox_env(self):
        """Every path a scan can walk out of the fixture. c.HOME is not enough:
        XDG_DATA_HOME and the per-provider homes are read from the environment,
        so patching the module constant alone still reads the real machine."""
        root = self.root
        return {'HOME': str(root), 'XDG_DATA_HOME': str(root / 'data'),
                'XDG_STATE_HOME': str(root / 'state'), 'XDG_CONFIG_HOME': str(root / 'config'),
                'CODEX_HOME': str(root / 'codex'), 'CLAUDE_CONFIG_DIR': str(root / 'claude'),
                'GROK_HOME': str(root / 'grok'), 'PI_CODING_AGENT_DIR': str(root / 'pi'),
                'MUSE_HOME': str(root / 'muse'), 'CURSOR_HOME': str(root / 'cursor')}

    def transcript(self, name, entries):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(e) + '\n' for e in entries))
        return path

    def codex_event(self, ts='2026-09-04T12:00:00Z', total=100):
        return {'type': 'event_msg', 'timestamp': ts, 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': 100, 'cached_input_tokens': 70,
                                'output_tokens': 20, 'reasoning_output_tokens': 10},
            'total_token_usage': {'input_tokens': total, 'output_tokens': 20}}}}

    def run_pulse(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['collector.py', 'pulse']), contextlib.redirect_stdout(output), \
             patch.object(c, 'cursor_usage', side_effect=AssertionError('Pulse fetched Cursor usage')), \
             patch.object(c, 'go_quota', side_effect=AssertionError('Pulse fetched a quota')), \
             patch.object(c.Ledger, 'sync_ledgers', side_effect=AssertionError('Pulse synced the ledger')):
            c.main()
        return json.loads(output.getvalue())

    def release_response(self, tag):
        return contextlib.closing(io.BytesIO(json.dumps({'tag_name': tag}).encode()))

    def test_update_check_reports_a_newer_release_once_a_day(self):
        cfg = c.settings()
        with patch.object(c, 'installed_version', return_value='1.9.0'):
            self.assertFalse(c.update_status(cfg)['available'])
            with patch.object(c.urllib.request, 'urlopen', return_value=self.release_response('v1.10.0')) as request:
                c.check_update(cfg)
                c.check_update(cfg)
            self.assertEqual(request.call_count, 1)
            status = c.update_status(cfg)
            self.assertEqual((status['available'], status['current'], status['latest']), (True, '1.9.0', '1.10.0'))
            self.assertNotIn('--with-plugin', status['command'])
            self.assertIn('OMARCHY_USAGE_REF=v1.10.0 bash', status['command'])
            plugin = c.CONFIG.parents[1] / 'plugins' / c.PLUGIN_ID / 'manifest.json'
            plugin.parent.mkdir(parents=True); plugin.write_text('{}')
            self.assertTrue(c.update_status(cfg)['command'].endswith('| OMARCHY_USAGE_REF=v1.10.0 bash -s -- --with-plugin'))
        with patch.object(c, 'installed_version', return_value='1.10.0'):
            self.assertFalse(c.update_status(cfg)['available'])

    def test_update_check_can_be_switched_off_and_survives_failures(self):
        with patch.object(c, 'installed_version', return_value='1.9.0'):
            # The fixture's urlopen raises on any call, so reaching it fails the test.
            c.check_update(c.settings() | {'updateCheck': False})
            self.assertFalse((c.STATE / 'update.json').exists())
            with patch.object(c.urllib.request, 'urlopen', side_effect=OSError('offline')):
                c.check_update(c.settings())
            self.assertFalse(c.update_status(c.settings())['available'])
            with patch.object(c.urllib.request, 'urlopen', return_value=self.release_response('nightly')):
                c.check_update(c.settings(), force=True)
            self.assertFalse(c.update_status(c.settings())['available'])
            with patch.object(c.urllib.request, 'urlopen', return_value=self.release_response('v2.0.0')):
                c.check_update(c.settings(), force=True)
            self.assertTrue(c.update_status(c.settings())['available'])
            self.assertFalse(c.update_status(c.settings() | {'updateCheck': False})['available'])
        self.assertLess(c.version_key('1.9.2'), c.version_key('1.10.0'))

    def test_update_check_recovers_from_malformed_cache(self):
        with patch.object(c, 'installed_version', return_value='1.9.0'):
            for cached in ([], {'attemptedAt': 'bad'}, {'attemptedAt': time.time() + 90000}):
                c.atomic_json(c.STATE / 'update.json', cached)
                with patch.object(c.urllib.request, 'urlopen', return_value=self.release_response('v1.10.0')):
                    c.check_update(c.settings())
                self.assertTrue(c.update_status(c.settings())['available'])

    def test_update_check_setting_is_saved_and_kept_when_not_sent(self):
        self.assertTrue(c.settings()['updateCheck'])
        self.assertFalse(c.save_settings({'updateCheck': False})['updateCheck'])
        self.assertFalse(c.save_settings({})['updateCheck'])
        self.assertTrue(c.save_settings({'updateCheck': True})['updateCheck'])

    def test_pinned_limits_migrate_and_enforce_three_without_touching_ledger(self):
        c.CONFIG.parent.mkdir(parents=True)
        c.CONFIG.write_text('{"keep":"settings"}')
        legacy = {'provider': 'codex', 'label': 'Weekly (7-day)', 'title': 'Weekly'}
        c.PINNED_LIMIT.write_text(json.dumps(legacy))
        self.assertEqual(c.pinned_limits(), [legacy])
        self.assertEqual(c.save_pinned_limit('claude', 'Weekly (7-day)', 'Weekly'),
                         [legacy, {'provider': 'claude', 'label': 'Weekly (7-day)', 'title': 'Weekly'}])
        c.save_pinned_limit('codex-second', 'Weekly (7-day)', 'Weekly')
        self.assertEqual(len(c.pinned_limits()), 3)
        self.assertEqual(len(c.save_pinned_limit('codex', 'Weekly (7-day)', 'Weekly')), 3)
        with self.assertRaisesRegex(ValueError, 'three limits'):
            c.save_pinned_limit('grok', 'Weekly', 'Weekly')
        self.assertEqual(c.pinned_limits()[0], legacy)
        self.assertEqual(c.save_pinned_limit('claude', 'Weekly (7-day)', 'Weekly', remove=True),
                         [legacy, {'provider': 'codex-second', 'label': 'Weekly (7-day)', 'title': 'Weekly'}])
        self.assertEqual(json.loads(c.CONFIG.read_text()), {'keep': 'settings'})
        self.assertFalse((c.STATE / 'usage.sqlite').exists())
        self.assertEqual(c.save_pinned_limit('', ''), [])
        self.assertEqual(c.pinned_limits(), [])
        with self.assertRaises(ValueError): c.save_pinned_limit('../codex', 'Weekly (7-day)')
        # The panel and dashboard loaders drop these ids, so a saved pin would
        # hold a slot nobody can see or unpin.
        for provider in ('Codex-Work', '-codex', 'a' * 81):
            with self.assertRaises(ValueError): c.save_pinned_limit(provider, 'Weekly (7-day)')
        self.assertEqual(c.pinned_limits(), [])

    def test_pulse_counts_new_codex_usage_without_network_or_duplicate_rows(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        path = self.transcript('codex/sessions/2026/09/pulse.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'pulse-session', 'timestamp': now}},
            self.codex_event(now, total=100)])
        first = self.run_pulse()
        self.assertEqual(first['tokens'], 120)
        self.assertEqual(self.run_pulse()['tokens'], 120)
        with path.open('a') as stream:
            stream.write(json.dumps(self.codex_event(now, total=200)) + '\n')
        second = self.run_pulse()
        self.assertEqual(second['tokens'], 240)
        self.assertEqual(json.loads((c.STATE / 'hourly-summary.json').read_text())['tokens'], 240)
        ledger = c.Ledger(c.STATE / 'usage.sqlite', readonly=True)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
        self.assertIsNone(ledger.db.execute("SELECT value FROM metadata WHERE key='scan'").fetchone())
        ledger.db.close()

    def test_pulse_reads_opencode_wal_changes(self):
        path = self.root / 'data/opencode/opencode.db'
        path.parent.mkdir(parents=True)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE session (id TEXT, directory TEXT)')
        db.execute('CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)')
        db.execute('INSERT INTO session VALUES (?,?)', ('session', '/project'))
        ts = int(time.time() * 1000)
        usage = {'role': 'assistant', 'modelID': 'test-model', 'providerID': 'opencode',
                 'tokens': {'input': 100, 'output': 20}}
        db.execute('INSERT INTO message VALUES (?,?,?,?)', ('message', 'session', ts, json.dumps(usage)))
        db.commit()
        self.assertEqual(self.run_pulse()['tokens'], 120)
        self.assertEqual(self.run_pulse()['tokens'], 120)
        usage['tokens']['output'] = 50
        db.execute('UPDATE message SET data=? WHERE id=?', (json.dumps(usage), 'message'))
        db.commit()
        self.assertEqual(self.run_pulse()['tokens'], 150)

    def test_codex_repeated_snapshot_cache_and_reasoning(self):
        event = self.codex_event()
        path = self.transcript('session.jsonl', [event, self.codex_event('2026-09-04T12:01:00Z')])
        records = list(c.codex_records(path))
        self.assertEqual(len(records), 1)
        self.assertEqual(sum(records[0][f] for f in c.FIELDS[:4]), 120)
        self.assertEqual(records[0]['input'], 30)

    def test_archive_move_does_not_duplicate(self):
        path = self.transcript('active.jsonl', [{'type': 'session_meta', 'payload': {'id': 'stable'}}, self.codex_event()])
        ledger = c.Ledger(self.root / 'test.sqlite')
        for r in c.codex_records(path): ledger.put(r)
        moved = path.with_name('archived.jsonl'); path.rename(moved)
        for r in c.codex_records(moved): ledger.put(r)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        moved.unlink(); ledger.db.commit()
        self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        ledger.db.close()

    def test_fork_inherited_history_is_excluded(self):
        path = self.transcript('fork.jsonl', [{'type': 'session_meta', 'payload': {
            'id': 'child', 'timestamp': '2026-09-04T12:00:00Z', 'forked_from_id': 'parent'}},
            self.codex_event('2026-09-03T12:00:00Z'), self.codex_event(total=200)])
        self.assertEqual(len(list(c.codex_records(path))), 1)

    def test_embedded_parent_metadata_cannot_replace_child(self):
        path = self.transcript('fork.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'child', 'timestamp': '2026-09-04T12:00:00Z'}},
            {'type': 'session_meta', 'payload': {'id': 'parent', 'timestamp': '2026-09-03T12:00:00Z'}},
            self.codex_event('2026-09-03T12:00:00Z'), self.codex_event(total=200)])
        records = list(c.codex_records(path))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['session'], 'child')

    def test_claude_stream_chunks_upsert_final_usage(self):
        def event(out): return {'type': 'assistant', 'timestamp': '2026-09-04T12:00:00Z',
             'sessionId': 's', 'requestId': 'req', 'message': {'id': 'msg', 'model': 'claude-test',
             'usage': {'input_tokens': 10, 'output_tokens': out, 'cache_creation_input_tokens': 20}}}
        path = self.transcript('claude.jsonl', [event(1), event(30), event(10)])
        ledger = c.Ledger(self.root / 'test.sqlite')
        for r in c.claude_records(path): ledger.put(r)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*),SUM(output) FROM events').fetchone(), (1, 30))
        ledger.db.close()

    def commandcode_event(self, mid='m1', model='glm-5.3-flash', ts='2026-09-19T12:00:00Z'):
        return {'type': 'message', 'id': mid, 'timestamp': ts, 'model': model,
                'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hi'}]},
                'usage': {'inputTokens': 100, 'outputTokens': 20, 'cacheReadTokens': 70, 'cacheWriteTokens': 5}}

    def test_commandcode_session_usage_and_checkpoint_skip(self):
        header = {'type': 'session', 'version': 3, 'id': 'sess-1', 'timestamp': '2026-09-19T11:59:00Z',
                  'cwd': '/home/user/Projects/demo'}
        path = self.transcript('commandcode/home-user-projects-demo/abc.jsonl',
                               [header, self.commandcode_event(), self.commandcode_event(mid='m2', model='deepseek/deepseek-v4.1-flash')])
        self.transcript('commandcode/home-user-projects-demo/abc.checkpoints.jsonl', [self.commandcode_event(mid='ghost')])
        ledger = c.Ledger(self.root / 'commandcode.sqlite')
        for r in c.commandcode_records(path): ledger.put(r)
        rows = ledger.db.execute('SELECT session, model, project, client, input, output, cacheRead, cacheWrite FROM events ORDER BY model').fetchall()
        self.assertEqual(rows, [
            ('sess-1', 'deepseek/deepseek-v4.1-flash', '/home/user/Projects/demo', 'Command Code', 100, 20, 70, 5),
            ('sess-1', 'glm-5.3-flash', '/home/user/Projects/demo', 'Command Code', 100, 20, 70, 5),
        ])
        ledger.db.close()

    def test_commandcode_stream_chunk_upserts_final_usage(self):
        path = self.transcript('commandcode/x/abc.jsonl', [self.commandcode_event(mid='m1')])
        ledger = c.Ledger(self.root / 'cc.sqlite')
        for r in c.commandcode_records(path): ledger.put(r)
        grown = self.transcript('commandcode/x/abc.jsonl', [self.commandcode_event(mid='m1'),
                                                            self.commandcode_event(mid='m2')])
        for r in c.commandcode_records(grown): ledger.put(r)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM events').fetchone(), (2,))
        ledger.db.close()

    def test_t3_provider_instances_are_discovered_and_routed(self):
        command_home = self.root / 't3/commandcode/codex'
        cline_data = self.root / 't3/clinepass/data'
        settings = {'providerInstances': {
            'commandcode': {'driver': 'codex', 'displayName': 'CommandCode', 'enabled': True,
                            'environment': [{'name': 'COMMANDCODE_API_KEY', 'value': 't3-secret', 'sensitive': True}],
                            'config': {'homePath': str(command_home)}},
            'clinepass': {'driver': 'opencode', 'displayName': 'ClinePass', 'enabled': True,
                          'environment': [{'name': 'XDG_DATA_HOME', 'value': str(cline_data), 'sensitive': False},
                                          {'name': 'CLINE_API_KEY', 'value': 't3-secret', 'sensitive': True}]}}}
        settings_path = self.root / '.t3/userdata/settings.json'
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps(settings))

        rollout = self.transcript('t3/commandcode/codex/sessions/2026/09/19/rollout.jsonl', [
            {'type': 'session_meta', 'timestamp': '2026-09-19T20:00:00Z', 'payload': {
                'id': 't3-command', 'timestamp': '2026-09-19T20:00:00Z', 'cwd': '/project',
                'originator': 't3code_desktop', 'model_provider': 'commandcode'}},
            {'type': 'turn_context', 'timestamp': '2026-09-19T20:00:01Z',
             'payload': {'model': 'z-ai/glm-5.3-flashx', 'cwd': '/project'}},
            self.codex_event(ts='2026-09-19T20:00:02Z')])
        self.assertTrue(rollout.exists())

        opencode = cline_data / 'opencode/opencode.db'
        opencode.parent.mkdir(parents=True)
        db = sqlite3.connect(opencode)
        db.executescript('CREATE TABLE message(id,session_id,time_created,data); CREATE TABLE session(id,directory);')
        db.execute('INSERT INTO session VALUES (?,?)', ('t3-cline-session', '/project'))
        db.execute('INSERT INTO message VALUES (?,?,?,?)', (
            't3-cline-message', 't3-cline-session', 1789866000000,
            json.dumps({'role': 'assistant', 'providerID': 'cline-pass', 'modelID': 'cline-pass/glm-5.3-flash',
                        'tokens': {'input': 100, 'output': 20, 'reasoning': 5, 'cache': {'read': 30, 'write': 0}}})))
        db.commit(); db.close()

        instances = c.t3_provider_instances()
        self.assertEqual({(item['driver'], item['provider'], item['root']) for item in instances}, {
            ('codex', 'commandcode', str(command_home)),
            ('opencode', 'clinepass', str(cline_data / 'opencode'))})
        self.assertNotIn('t3-secret', json.dumps(instances))

        ledger = c.Ledger(self.root / 't3.sqlite')
        meta = ledger.scan(c.DEFAULTS)
        rows = ledger.db.execute('SELECT provider,model,client,input,output,cacheRead,reasoning FROM events ORDER BY provider').fetchall()
        self.assertEqual(rows, [
            ('clinepass', 'cline-pass/glm-5.3-flash', 'OpenCode', 100, 25, 30, 5),
            ('commandcode', 'z-ai/glm-5.3-flashx', 'T3 Code', 30, 20, 70, 10)])
        self.assertNotIn('t3-secret', json.dumps(meta))
        ledger.db.close()

    def test_unknown_price_is_not_zero(self):
        r = c.record('1', 'codex', 's', 1, 'missing', '', 'CLI', input=100)
        self.assertEqual(c.price(r, {}), (None, None))
        self.assertEqual(c.price(r, {'missing': {'input_cost_per_token': 0, 'output_cost_per_token': 0}}), (0, 0))

    def grok_event(self, session='s', event='event', usage=None):
        return {'timestamp': 1788580000, 'params': {'sessionId': session, '_meta': {'eventId': event},
                'update': {'sessionUpdate': 'turn_completed', 'prompt_id': 'prompt', 'usage': usage or {
                    'inputTokens': 1000, 'cachedReadTokens': 800, 'outputTokens': 100,
                    'reasoningTokens': 40, 'modelCalls': 17, 'costUsdTicks': 5912850000}}}}

    def test_grok_cache_reasoning_cost_and_copied_events(self):
        path = self.transcript('project/session/updates.jsonl', [self.grok_event(), self.grok_event('fork')])
        ledger = c.Ledger(self.root / 'grok.sqlite')
        for r in c.grok_records(path): ledger.put(r)
        ledger.db.row_factory = sqlite3.Row
        rows = ledger.db.execute('SELECT * FROM events').fetchall()
        self.assertEqual(len(rows), 1)
        r = dict(rows[0])
        self.assertEqual((r['input'], r['cacheRead'], r['output'], r['reasoning'], r['modelCalls']), (200, 800, 100, 40, 17))
        self.assertEqual(sum(r[f] for f in c.FIELDS[:4]), 1100)
        self.assertAlmostEqual(c.price(r, {})[0], .591285)
        ledger.db.close()

    def test_grok_per_model_usage_does_not_duplicate_aggregate(self):
        usage = {'inputTokens': 300, 'outputTokens': 30, 'costUsdTicks': 3000000000,
                 'modelUsage': {'one': {'inputTokens': 100, 'outputTokens': 10, 'costUsdTicks': 1000000000},
                                'two': {'inputTokens': 200, 'outputTokens': 20}}}
        path = self.transcript('updates.jsonl', [self.grok_event(usage=usage)])
        rows = list(c.grok_records(path))
        self.assertEqual(sum(r['input'] + r['output'] for r in rows), 330)
        self.assertEqual(c.price(rows[0], {})[0], .1)
        self.assertEqual(c.price(rows[1], {}), (None, None))
        usage['modelUsage'].pop('one')
        path = self.transcript('updates.jsonl', [self.grok_event(usage=usage)])
        self.assertEqual(c.price(next(c.grok_records(path)), {})[0], .3)

    def test_grok_missing_usage_is_not_invented(self):
        event = self.grok_event(); event['params']['update'].pop('usage')
        path = self.transcript('updates.jsonl', [event, self.grok_event(usage={'inputTokens': 5})])
        rows = list(c.grok_records(path))
        self.assertEqual(len(rows), 1)
        self.assertEqual(c.price(rows[0], {'unknown': {'input_cost_per_token': 1}}), (None, None))

    def test_grok_billing_requires_valid_message_and_success(self):
        def frame(data, flag=0): return bytes([flag]) + len(data).to_bytes(4, 'big') + data
        body = b'\x0a\x05\x0d' + c.struct.pack('<f', 42.5)
        self.assertEqual(c.grok_billing(frame(body))[0]['percent'], .425)
        self.assertEqual(c.grok_billing(frame(b'\x0a\x00'))[0]['percent'], 0)
        for raw in [b'', frame(b''), frame(body)[:-1], frame(body) + frame(b'grpc-status: 16\r\n', 128)]:
            with self.assertRaises(ValueError): c.grok_billing(raw)

    def test_grok_expired_auth_stays_untouched_and_retains_stale_quota(self):
        home = self.root / 'grok'; home.mkdir()
        auth = home / 'auth.json'; auth.write_text(json.dumps({'account': {'key': 'test', 'expires_at': '2000-01-01T00:00:00Z'}}))
        before = auth.read_bytes()
        with patch.object(c, 'STATE', self.root / 'state'), patch.dict('os.environ', {'GROK_HOME': str(home)}), patch.object(c.urllib.request, 'urlopen') as request:
            c.atomic_json(c.STATE / 'grok-quota.json', {'limits': [{'percent': .3}], 'updatedAt': '2000-01-01T00:00:00Z'})
            quota = c.grok_quota(True)
            self.assertIn('expired', quota['error'])
            self.assertEqual(quota['limits'][0]['percent'], .3)
            request.assert_not_called()
        self.assertEqual(auth.read_bytes(), before)

    def test_grok_request_errors_do_not_expose_credentials(self):
        home = self.root / 'grok'; home.mkdir()
        (home / 'auth.json').write_text(json.dumps({'account': {'key': 'private-test-key'}}))
        with patch.object(c, 'STATE', self.root / 'state'), patch.dict('os.environ', {'GROK_HOME': str(home)}), patch.object(c.urllib.request, 'urlopen', side_effect=ValueError('Invalid header: private-test-key')):
            quota = c.grok_quota(True)
            self.assertNotIn('private-test-key', json.dumps(quota))

    def test_grok_login_invalidates_cached_signout(self):
        import io
        home = self.root / 'grok'; home.mkdir()
        auth = home / 'auth.json'
        auth.write_text(json.dumps({'account': {'key': 'old', 'expires_at': '2000-01-01T00:00:00Z'}}))
        with patch.object(c, 'STATE', self.root / 'state'), patch.dict('os.environ', {'GROK_HOME': str(home)}):
            self.assertIn('expired', c.grok_quota()['error'])
            auth.write_text(json.dumps({'account': {'key': 'new-valid-login'}}))
            with patch.object(c.urllib.request, 'urlopen', return_value=io.BytesIO(b'\x00\x00\x00\x00\x02\x0a\x00')) as request:
                self.assertEqual(c.grok_quota()['error'], '')
                request.assert_called_once()

    def test_existing_ledger_migrates_without_losing_events(self):
        path = self.root / 'old.sqlite'; db = sqlite3.connect(path)
        db.execute('CREATE TABLE events (id TEXT PRIMARY KEY, provider, session, ts, model, project, client, input, output, cacheRead, cacheWrite, cacheWrite1h, reasoning)')
        db.execute("INSERT INTO events VALUES ('old','codex','s',1,'m','','CLI',10,20,0,0,0,0)")
        db.commit(); db.close()
        ledger = c.Ledger(path)
        self.assertEqual(ledger.db.execute('SELECT input,output,reportedCostTicks FROM events').fetchone(), (10, 20, None))
        ledger.db.close()

    def test_grok_bar_keeps_history_visible_during_quiet_week(self):
        ledger = c.Ledger(self.root / 'bar.sqlite')
        ledger.put(c.record('old', 'grok', 's', '2020-01-01T12:00:00Z', 'grok', '', 'Grok Build', input=10))
        with patch.object(c, 'STATE', self.root / 'state'), patch.object(c, 'quota', return_value={'limits': []}):
            c.write_agent_record(ledger, 'grok')
            bar = json.loads((c.STATE.parent / 'agents/usage/grok.json').read_text())
            self.assertTrue(bar['ready'])
            self.assertEqual((bar['totalSessions'], bar['totalPrompts'], bar['activeDays']), (1, 1, 1))
            self.assertEqual(bar['todayTotalTokens'], 0)
        ledger.db.close()

    def test_cache_price_and_long_context(self):
        r = c.record('1', 'codex', 's', 1, 'm', '', 'CLI', input=200000, cacheRead=100000, output=10)
        catalog = {'m': {'input_cost_per_token': 1e-6, 'input_cost_per_token_above_272k_tokens': 2e-6,
                 'output_cost_per_token': 3e-6, 'cache_read_input_token_cost': 0.1e-6}}
        self.assertAlmostEqual(c.price(r, catalog)[0], 0.41003)

    def test_periods_and_provider_filters(self):
        ledger = c.Ledger(self.root / 'test.sqlite')
        now = dt.datetime(2026, 9, 5, 12).astimezone()
        for key, provider, date in [('a', 'codex', '2026-09-05T10:00:00'), ('b', 'claude', '2026-09-04T10:00:00'),
                                    ('c', 'codex', '2026-08-29T10:00:00')]:
            ledger.put(c.record(key, provider, key, date, 'unknown', '', 'CLI', input=100))
        with patch.object(c, 'load_rates', return_value={'document': {}, 'source': 'test'}):
            report = c.report(ledger, c.DEFAULTS, 7, now=now)
            self.assertEqual(report['summary']['tokens'], 200)
            self.assertEqual(report['previous']['tokens'], 100)
            self.assertEqual(report['summary']['unpricedTokens'], 200)
            self.assertEqual(c.report(ledger, c.DEFAULTS, 7, 'codex', now)['summary']['tokens'], 100)
        ledger.db.close()

    def test_go_scanner_excludes_other_providers_and_counts_reasoning(self):
        data_dir = self.root / 'data'; path = data_dir / 'opencode/opencode.db'
        path.parent.mkdir(parents=True)
        db = sqlite3.connect(path)
        db.executescript('CREATE TABLE message(id,session_id,time_created,data); CREATE TABLE session(id,directory);')
        db.execute('INSERT INTO session VALUES (?,?)', ('s', '/project'))
        for provider in ['opencode-go', 'openai', 'anthropic', 'openrouter']:
            data = {'role': 'assistant', 'providerID': provider, 'modelID': 'm',
                    'tokens': {'input': 10, 'output': 20, 'reasoning': 5, 'cache': {'read': 30, 'write': 0}}}
            db.execute('INSERT INTO message VALUES (?,?,?,?)', (provider, 's', 1788580000000, json.dumps(data)))
        db.commit(); db.close()
        ledger = c.Ledger(self.root / 'ledger.sqlite')
        with patch.object(c, 'HOME', self.root), patch.dict('os.environ', {'XDG_DATA_HOME': str(data_dir),
              'CODEX_HOME': str(self.root / 'codex'), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude')}):
            ledger.scan(c.DEFAULTS)
            ledger.scan(c.DEFAULTS)
        self.assertEqual(ledger.db.execute("SELECT COUNT(*),SUM(output),SUM(cacheRead) FROM events WHERE provider='opencode-go'").fetchone(), (1, 25, 30))
        self.assertEqual(ledger.db.execute("SELECT COUNT(*),SUM(output) FROM events WHERE provider='opencode'").fetchone(), (3, 75))
        ledger.db.close()

    def test_gemini_migration_rewind_and_stream_updates(self):
        message = {'id': 'gemini-msg', 'type': 'gemini', 'timestamp': '2026-09-04T12:00:00Z',
                   'model': 'gemini-3.8-flash', 'tokens': {'input': 100, 'cached': 70, 'output': 20, 'thoughts': 10, 'total': 130}}
        legacy = self.root / 'session-old.json'
        legacy.write_text(json.dumps({'sessionId': 's', 'projectHash': 'project', 'messages': [message]}))
        modern = self.transcript('session-new.jsonl', [{'sessionId': 's', 'projectHash': 'project'},
            message | {'tokens': None}, message, {'$rewindTo': 'gemini-msg'}, {'$set': {'messages': [message]}}])
        ledger = c.Ledger(self.root / 'gemini.sqlite')
        for path in [legacy, modern]:
            for record in c.gemini_records(path): ledger.put(record)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*),SUM(input),SUM(output),SUM(cacheRead),SUM(reasoning) FROM events').fetchone(), (1, 30, 30, 70, 10))
        ledger.db.close()

    def test_gemini_scans_nested_subagents_and_additional_home(self):
        root = self.root / 'gemini'
        message = {'id': 'subagent-message', 'type': 'gemini', 'timestamp': 1788580000,
                   'tokens': {'input': 100, 'output': 20, 'thoughts': 10, 'tool': 5, 'total': 135}}
        self.transcript('gemini/tmp/project/chats/parent/agent.jsonl', [{'sessionId': 'subagent', 'projectHash': 'p'}, message])
        ledger = c.Ledger(self.root / 'scan.sqlite')
        with patch.object(c, 'HOME', self.root), patch.dict('os.environ', {
            'CODEX_HOME': str(self.root / 'codex'), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'GROK_HOME': str(self.root / 'grok'), 'PI_CODING_AGENT_DIR': str(self.root / 'pi'), 'XDG_DATA_HOME': str(self.root / 'data')}):
            ledger.scan(c.DEFAULTS | {'geminiHomes': [str(root)]})
            ledger.scan(c.DEFAULTS | {'geminiHomes': [str(root)]})
        self.assertEqual(ledger.db.execute("SELECT COUNT(*),SUM(input+output) FROM events WHERE provider='gemini'").fetchone(), (1, 135))
        ledger.db.close()

    def muse_event(self, usage=None, response='resp_test1', recorded_us=1788791137057784,
                     model='muse-spark-1.3-contributor', session='test-session'):
        event = {'kind': 'model_completed', 'model': model, 'duration_ms': 1000,
                 'usage': usage if usage is not None else {
                     'input_tokens': 42733, 'output_tokens': 184, 'reasoning_tokens': 100,
                     'cache_read_tokens': 29425, 'cache_write_tokens': 0, 'cached_tokens': 29425}}
        if response is not None: event['response_id'] = response
        return {'schema_version': 1, 'id': 'event-id', 'stream': {'kind': 'session', 'id': session},
                'sequence': 56, 'recorded_at': recorded_us, 'record_type': 'event',
                'payload_type': 'runtime.session', 'payload_schema_version': 1,
                'payload': {'kind': 'run', 'run_id': 'run-1', 'event': event}}

    def muse_session(self, name, entries):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for entry in entries:
            if isinstance(entry, list):
                lines.append(json.dumps({'retained_frame': 'session_permission_transaction',
                    'frame_schema_version': 1, 'outer_log_ordinal': 1, 'transaction_id': 't',
                    'children': [{'child_index': i, 'record_json': json.dumps(e)}
                                 for i, e in enumerate(entry)]}))
            else:
                lines.append(json.dumps(entry))
        path.write_text('\n'.join(lines) + '\n')
        return path

    def test_muse_envelope_unwrap_mapping_and_microsecond_ts(self):
        meta = {'schema_version': 1, 'id': 'meta', 'stream': {'kind': 'session', 'id': 's'},
                'sequence': 3, 'recorded_at': 1788791037153195, 'record_type': 'event',
                'payload_type': 'runtime.session.metadata', 'payload_schema_version': 1,
                'payload': {'kind': 'metadata', 'record': {'workspace_root': '/project'}}}
        path = self.muse_session('2026/09/07/s/session.jsonl', [[meta], self.muse_event(session='s')])
        records = list(c.muse_records(path))
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual((r['provider'], r['session'], r['model'], r['project'], r['client']),
                         ('muse', 's', 'muse-spark-1.3-contributor', '/project', 'Muse'))
        self.assertEqual(r['ts'], 1788791137)
        self.assertEqual(r['input'], 42733 - 29425)
        self.assertEqual((r['output'], r['cacheRead'], r['cacheWrite'], r['reasoning']), (184, 29425, 0, 100))
        # Reasoning stays separate from output; the cache spellings are one count.
        self.assertEqual(sum(r[f] for f in c.FIELDS[:4]), 42733 + 184)

    def test_muse_legacy_cached_tokens_spelling(self):
        usage = {'input_tokens': 100, 'output_tokens': 20, 'cached_tokens': 70}
        path = self.muse_session('s/session.jsonl', [self.muse_event(usage=usage)])
        r = next(c.muse_records(path))
        self.assertEqual((r['input'], r['cacheRead']), (30, 70))

    def test_muse_malformed_shapes_do_not_abort_file(self):
        bad_stream = self.muse_event()
        bad_stream['stream'] = 'not-a-dict'
        bad_record = self.muse_event(response='resp_second')
        bad_record['payload'] = {'kind': 'metadata', 'record': ['not', 'a', 'dict']}
        good = self.muse_event(response='resp_third')
        path = self.muse_session('s/session.jsonl', [bad_stream, bad_record, good,
            {'children': [{'record_json': 'not json'}, 'not-a-dict'], 'payload': {'kind': 'run'}}])
        records = list(c.muse_records(path))
        self.assertEqual(len(records), 2)
        self.assertEqual({r['session'] for r in records}, {'s', 'test-session'})

    def test_muse_ignores_attribution_and_unrelated_rows(self):
        def attribution(family, reported):
            return {'schema_version': 1, 'id': family, 'stream': {'kind': 'session', 'id': 's'},
                    'sequence': 57, 'recorded_at': 1788791137052738, 'record_type': 'event',
                    'payload_type': 'runtime.session', 'payload_schema_version': 1,
                    'payload': {'kind': 'run', 'run_id': 'run-1', 'event': {
                        'kind': 'goal_usage_attribution',
                        'record': {'quantity': {'input_tokens': 42733, 'output_tokens': 184,
                                               'reasoning_tokens': 100, 'cached_tokens': 29425,
                                               'reported': reported, 'unit': 'tokens'},
                                   'usage_family': family}}}}
        path = self.muse_session('s/session.jsonl',
            [attribution('provider', True), attribution('tool', False), {'heartbeat': True}])
        self.assertEqual(list(c.muse_records(path)), [])

    def test_muse_copied_response_ids_merge(self):
        entries = [self.muse_event()]
        first = self.muse_session('one/session.jsonl', entries)
        second = self.muse_session('two/session.jsonl', entries)
        ledger = c.Ledger(self.root / 'muse.sqlite')
        for path in [first, second]:
            for r in c.muse_records(path): ledger.put(r)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        ledger.db.close()

    def test_muse_retry_without_response_id_collapses(self):
        small = self.muse_event(response=None, usage={'input_tokens': 10, 'output_tokens': 1})
        grown = self.muse_event(response=None, usage={'input_tokens': 30, 'output_tokens': 3})
        path = self.muse_session('s/session.jsonl', [small, grown])
        ledger = c.Ledger(self.root / 'retry.sqlite')
        for r in c.muse_records(path): ledger.put(r)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*),SUM(input),SUM(output) FROM events').fetchone(), (1, 30, 3))
        ledger.db.close()

    def test_muse_scan_covers_dated_and_subagent_files(self):
        home = self.root / 'muse'
        self.muse_session('muse/sessions/2026/09/07/aaa/session.jsonl', [self.muse_event(session='aaa')])
        self.muse_session('muse/sessions/2026/09/07/aaa/subagent/bbb/session.jsonl',
                          [self.muse_event(session='bbb', response='resp_sub')])
        ledger = c.Ledger(self.root / 'scan.sqlite')
        with patch.object(c, 'HOME', self.root), patch.dict('os.environ', {
                'MUSE_HOME': str(home), 'CODEX_HOME': str(self.root / 'codex'),
                'CLAUDE_CONFIG_DIR': str(self.root / 'claude'), 'GROK_HOME': str(self.root / 'grok'),
                'PI_CODING_AGENT_DIR': str(self.root / 'pi'), 'XDG_DATA_HOME': str(self.root / 'data')}):
            ledger.scan(c.DEFAULTS)
            ledger.scan(c.DEFAULTS)
        rows = ledger.db.execute("SELECT session,input FROM events WHERE provider='muse' ORDER BY session").fetchall()
        self.assertEqual([r[0] for r in rows], ['aaa', 'bbb'])
        ledger.db.close()

    def muse_auth(self, config_home, token='dca:test-access-token-secret'):
        home = self.root / config_home
        home.mkdir(parents=True, exist_ok=True)
        (home / 'muse/auth.json').parent.mkdir(parents=True, exist_ok=True)
        (home / 'muse/auth.json').write_text(json.dumps(
            {'schema_version': 1, 'providers': {'meta': {
                'access_token': token, 'api_base_url': 'https://api.meta.ai/v1/',
                'api_key': 'LLM|test-minted-key-secret', 'mechanism': 'oauth',
                'obtained_via': 'device_code'}}}))
        return home

    def minted_key(self, usage='default'):
        if usage == 'default':
            usage = {'window': {'used_percent': 16, 'window_duration_mins': 300, 'resets_at': 1788791137},
                     'weekly': {'used_percent': 14, 'resets_at': 1789344000}, 'tier': '1'}
        return {'api_key': 'LLM|test-minted-key-secret', 'subs_tier_name': 'Muse Code Power Usage',
                'subs_usage': usage}

    def muse_urlopen(self, minted):
        import io
        def fake(request, timeout=12):
            assert request.full_url == 'https://api.meta.ai/muse-code/key', request.full_url
            assert request.get_method() == 'POST'
            assert 'dca:test-access-token-secret' in request.get_header('Authorization')
            return io.BytesIO(json.dumps(minted).encode())
        return fake

    def test_muse_quota_maps_windows_tier_and_hides_key_material(self):
        config = self.muse_auth('config')
        with patch.object(c, 'STATE', self.root / 'state'), patch.dict('os.environ', {'XDG_CONFIG_HOME': str(config)}):
            with patch.object(c.urllib.request, 'urlopen', side_effect=self.muse_urlopen(self.minted_key())) as request:
                quota = c.muse_quota(True)
                self.assertEqual(request.call_count, 1)
            self.assertEqual(quota['error'], '')
            self.assertEqual(quota['plan'], 'Muse Code Power Usage')
            by_label = {w['label']: w for w in quota['limits']}
            self.assertAlmostEqual(by_label['Session (5-hour)']['percent'], .16)
            self.assertAlmostEqual(by_label['Weekly (7-day)']['percent'], .14)
            self.assertEqual(by_label['Weekly (7-day)']['resetsAt'],
                             dt.datetime.fromtimestamp(1789344000, dt.timezone.utc).isoformat())
            raw = (c.STATE / 'muse-quota.json').read_text()
            self.assertNotIn('dca:test-access-token-secret', raw)
            self.assertNotIn('LLM|test-minted-key-secret', raw)
            self.assertNotIn('dca:test-access-token-secret', json.dumps(quota))
            self.assertEqual(c.quota('muse')['plan'], 'Muse Code Power Usage')

    def test_muse_quota_missing_auth_reports_login_and_keeps_stale(self):
        with patch.object(c, 'STATE', self.root / 'state'), patch.dict('os.environ', {'XDG_CONFIG_HOME': str(self.root / 'empty')}), patch.object(c.urllib.request, 'urlopen') as request:
            c.atomic_json(c.STATE / 'muse-quota.json', {'limits': [{'label': 'Weekly (7-day)', 'percent': .1}], 'updatedAt': 'old'})
            quota = c.muse_quota(True)
            self.assertIn('login', quota['error'])
            self.assertEqual(quota['limits'][0]['percent'], .1)
            request.assert_not_called()

    def test_muse_quota_errors_do_not_expose_credentials(self):
        import io
        config = self.muse_auth('config')
        def failing(request, timeout=12):
            raise c.urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'bad dca:test-access-token-secret'))
        with patch.object(c, 'STATE', self.root / 'state'), patch.dict('os.environ', {'XDG_CONFIG_HOME': str(config)}), patch.object(c.urllib.request, 'urlopen', side_effect=failing):
            quota = c.muse_quota(True)
            self.assertNotIn('dca:test-access-token-secret', json.dumps(quota))
            self.assertNotIn('LLM|test-minted-key-secret', json.dumps(quota))
            with patch.object(c.urllib.request, 'urlopen', side_effect=self.muse_urlopen({'subs_usage': {}})):
                quota = c.muse_quota(True)
                self.assertIn('quota', quota['error'])
                self.assertNotIn('dca:test-access-token-secret', json.dumps(quota))

    def test_muse_login_invalidates_cached_error(self):
        config = self.muse_auth('config')
        auth = config / 'muse/auth.json'
        with patch.object(c, 'STATE', self.root / 'state'), patch.dict('os.environ', {'XDG_CONFIG_HOME': str(config)}):
            c.atomic_json(c.STATE / 'muse-quota.json', {'error': 'stale', 'attemptedAt': 0, 'authVersion': None})
            with patch.object(c.urllib.request, 'urlopen', side_effect=self.muse_urlopen(self.minted_key())) as request:
                self.assertEqual(c.muse_quota()['error'], '')
                self.assertEqual(request.call_count, 1)
                # A second call inside the throttle window reuses the cache.
                self.assertEqual(c.muse_quota()['error'], '')
                self.assertEqual(request.call_count, 1)
                # Re-login (changed auth file) bypasses the throttle.
                auth.write_text(auth.read_text() + ' ')
                self.assertEqual(c.muse_quota()['error'], '')
                self.assertEqual(request.call_count, 2)

    def test_muse_scan_refreshes_quota_before_record(self):
        import contextlib
        import io as stdlib_io
        import sys
        home = self.root / 'musehome'
        (home / 'sessions').mkdir(parents=True)
        config = self.muse_auth('config')
        with patch.object(c, 'STATE', self.root / 'state'), patch.object(c, 'CONFIG', self.root / 'settings.json'), patch.object(c, 'HOME', self.root), patch.dict('os.environ', {
                'MUSE_HOME': str(home), 'XDG_CONFIG_HOME': str(config), 'XDG_DATA_HOME': str(self.root / 'data'),
                'CODEX_HOME': str(self.root / 'codex'), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
                'GROK_HOME': str(self.root / 'grok'), 'PI_CODING_AGENT_DIR': str(self.root / 'pi')}), patch.object(
                c.urllib.request, 'urlopen', side_effect=self.muse_urlopen(self.minted_key())), patch.object(
                sys, 'argv', ['collector.py', 'scan']):
            c.save_settings(c.DEFAULTS | {'enabled': ['muse']})
            with contextlib.redirect_stdout(stdlib_io.StringIO()):
                c.main()
            record = json.loads((c.STATE.parent / 'agents/usage/muse.json').read_text())
            self.assertAlmostEqual(record['limits'][1]['percent'], .14)
            self.assertEqual(record['tierLabel'], 'Muse Code Power Usage')
            quota_file = json.loads((c.STATE / 'muse-quota.json').read_text())
            self.assertEqual(quota_file['limits'], record['limits'])

    def test_muse_settings_round_trip_keeps_home(self):
        with patch.object(c, 'CONFIG', self.root / 'settings.json'):
            config = c.save_settings(c.DEFAULTS | {'museHomes': ['/mounted/.local/share/muse']})
            self.assertEqual(config['museHomes'], ['/mounted/.local/share/muse'])
            again = c.save_settings(config)
            self.assertEqual(again['museHomes'], ['/mounted/.local/share/muse'])

    def cursor_fixture(self):
        path = Path(__file__).parent / 'fixtures/cursor-api.sample.json'
        return json.loads(path.read_text())['usageEventsDisplay']

    def test_cursor_fixture_contract_and_total_parser(self):
        events = self.cursor_fixture()
        self.assertEqual(len(events), 3)
        for ev in events:
            extra = dict(ev, futureUnknownKey={'nested': [1]})
            r = c.cursor_api_record(extra)
            self.assertEqual(r['provider'], 'cursor')
            self.assertTrue(r['ts'] > 0)
        # Degenerate inputs never raise; unparsable timestamps yield ts 0,
        # which put() drops.
        for bad in ({}, {'tokenUsage': None}, {'timestamp': 'not-a-time'}, {'tokenUsage': {'totalCents': 'nan-x'}}):
            self.assertEqual(c.cursor_api_record(bad)['ts'], 0)

    def test_cursor_record_mapping_tokens_cost_and_client(self):
        first, free, twin = self.cursor_fixture()
        r = c.cursor_api_record(first)
        self.assertEqual((r['input'], r['output'], r['cacheRead']), (300, 2, 205056))
        self.assertEqual(r['ts'], 1788815371)
        self.assertAlmostEqual(r['reportedValue'], 0.206292)
        self.assertEqual((r['turns'], r['model'], r['client'], r['session']), (1, 'synth-model-a', 'Cloud', 'synth-conv-1'))
        f = c.cursor_api_record(free)
        self.assertEqual(f['reportedValue'], 0.0)
        self.assertIsNotNone(f['reportedValue'])
        self.assertEqual((f['client'], f['session']), ('Cursor', 'cloud'))
        # Same millisecond, different tokens: distinct records.
        self.assertNotEqual(r['id'], c.cursor_api_record(twin)['id'])
        # Refetch stability across int/str/float ms forms.
        alt = dict(twin, timestamp=1788815371513.0)
        self.assertEqual(c.cursor_api_record(twin)['id'], c.cursor_api_record(alt)['id'])
        # None and '' conversation ids canonicalize to the same record.
        blank = c.cursor_api_record(dict(twin, conversationId=''))
        none = c.cursor_api_record(dict(twin, conversationId=None))
        self.assertEqual(blank['id'], none['id'])
        self.assertEqual((blank['session'], none['session']), ('cloud', 'cloud'))

    def test_cursor_event_ms_coercion(self):
        for form in ('1788815371513', 1788815371513, '1788815371513.0', 1788815371513.0):
            self.assertEqual(c.event_ms(form), 1788815371513)
        self.assertEqual(c.event_ms('2026-08-24T22:25:30Z'), 1787610330000)
        self.assertEqual(c.event_ms('garbage'), 0)

    def test_cursor_price_uses_reported_value_including_zero(self):
        ledger = c.Ledger(self.root / 'price.sqlite')
        for ev in self.cursor_fixture(): ledger.put(c.cursor_api_record(ev))
        ledger.db.row_factory = sqlite3.Row
        values = sorted(round(c.price(dict(row), {})[0], 6) for row in ledger.db.execute('SELECT * FROM events'))
        self.assertEqual(values, [0.0, 0.035, 0.206292])
        ledger.db.close()

    def test_cursor_walk_stops_on_short_page_and_boundary_repeat(self):
        first, _, _ = self.cursor_fixture()
        calls = []
        full = {'usageEventsDisplay': [first] * 1000}
        pages = [full, {'usageEventsDisplay': [first]}]
        def fake_post(token, method, body):
            calls.append((method, dict(body)))
            return pages[min(len(calls) - 1, 1)]
        with patch.object(c, 'cursor_post', fake_post):
            records, oldest, complete = c.cursor_walk('tok')
        self.assertTrue(complete)
        self.assertEqual(len(records), 1)
        self.assertEqual(oldest, 1788815371513)
        # The full first page keeps walking with an exclusive bound; the
        # repeated boundary row then yields zero new ids and halts the walk.
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]['endDate'], 1788815371512)

    def test_cursor_walk_short_first_page_commits(self):
        first, _, _ = self.cursor_fixture()
        with patch.object(c, 'cursor_post', return_value={'usageEventsDisplay': [first]}):
            records, oldest, complete = c.cursor_walk('tok')
        self.assertTrue(complete)
        self.assertEqual(len(records), 1)
        self.assertEqual(oldest, 1788815371513)

    def test_cursor_walk_retries_smaller_page_on_400(self):
        seen = []
        def fake_post(token, method, body):
            seen.append(body.get('pageSize'))
            if body.get('pageSize') == 1000:
                raise _HTTPError(400)
            return {'usageEventsDisplay': []}
        with patch.object(c, 'cursor_post', fake_post):
            records, _, complete = c.cursor_walk('tok')
        self.assertTrue(complete)
        self.assertEqual(records, [])
        self.assertEqual(seen, [1000, 100])

    def test_cursor_walk_failure_writes_nothing_and_keeps_cache(self):
        ledger = c.Ledger(self.root / 'fail.sqlite')
        ledger.put(c.record('old', 'cursor', 's', 1788810000, 'm', '', 'Cursor', input=1), 'cursor-api')
        with patch.object(c, 'STATE', self.root / 'state'), \
             patch.object(c, 'cursor_token', return_value='tok'), \
             patch.object(c, 'cursor_summary', return_value={'billingCycleStart': '1786479485000'}), \
             patch.object(c, 'cursor_walk', side_effect=c.CursorApiUnavailable('down')):
            source, warnings = c.cursor_usage(ledger, force=True)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        self.assertEqual(source.get('readErrors'), 1)
        self.assertTrue(warnings)
        cached = json.loads((self.root / 'state/cursor-usage.json').read_text())
        self.assertNotIn('newestTs', cached)
        ledger.db.close()

    def test_cursor_purge_removes_legacy_rows_on_success(self):
        ledger = c.Ledger(self.root / 'purge.sqlite')
        legacy = c.record('legacy-1', 'cursor', 's', 1788810000, 'unknown', '', 'Cursor')
        legacy['turns'] = 1
        ledger.put(legacy, '/home/u/.config/Cursor/User/globalStorage/state.vscdb')
        first, _, _ = self.cursor_fixture()
        with patch.object(c, 'STATE', self.root / 'state'), \
             patch.object(c, 'cursor_token', return_value='tok'), \
             patch.object(c, 'cursor_summary', return_value={'billingCycleStart': '1786479485000'}), \
             patch.object(c, 'cursor_walk', return_value=([c.cursor_api_record(first)], 1788815371513, True)):
            source, warnings = c.cursor_usage(ledger, force=True)
        self.assertEqual(warnings, [])
        rows = ledger.db.execute("SELECT id FROM events WHERE provider='cursor'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], 'legacy-1')
        self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM event_sources WHERE path LIKE '%state.vscdb'").fetchone()[0], 0)
        ledger.db.close()

    def test_cursor_empty_success_keeps_legacy_rows(self):
        ledger = c.Ledger(self.root / 'empty.sqlite')
        legacy = c.record('legacy-1', 'cursor', 's', 1788810000, 'unknown', '', 'Cursor')
        legacy['turns'] = 1
        ledger.put(legacy, '/home/u/.config/Cursor/User/globalStorage/state.vscdb')
        with patch.object(c, 'STATE', self.root / 'state'), \
             patch.object(c, 'cursor_token', return_value='tok'), \
             patch.object(c, 'cursor_summary', return_value={'billingCycleStart': '1786479485000'}), \
             patch.object(c, 'cursor_walk', return_value=([], 1788815371513, True)):
            source, warnings = c.cursor_usage(ledger, force=True)
        self.assertEqual(warnings, [])
        self.assertNotIn('readErrors', source)
        self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM events WHERE provider='cursor'").fetchone()[0], 1)
        ledger.db.close()

    def test_cursor_resume_finishes_history_then_tops_up(self):
        first, _, twin = self.cursor_fixture()
        new = dict(first, timestamp='1788829289000', conversationId='new-conv')
        ledger = c.Ledger(self.root / 'resume.sqlite')
        calls = []
        def fake_walk(token, end=None, stop_ms=0, cycle_ms=0):
            calls.append((end, stop_ms))
            if end is None:
                return ([c.cursor_api_record(new)], None, True)
            # History pages all older than the cached newest marker: the
            # resume walk must not stop on the incremental bound.
            self.assertEqual(stop_ms, 0)
            return ([c.cursor_api_record(first)], 1788815371513, True)
        with patch.object(c, 'STATE', self.root / 'state'), \
             patch.object(c, 'cursor_token', return_value='tok'), \
             patch.object(c, 'cursor_summary', return_value={'billingCycleStart': '1786479485000'}), \
             patch.object(c, 'cursor_walk', fake_walk):
            (self.root / 'state').mkdir(parents=True, exist_ok=True)
            (self.root / 'state/cursor-usage.json').write_text(json.dumps(
                {'historyVersion': 1, 'attemptedAt': 0, 'billingCycleStart': 1786479485000, 'newestTs': 1788815371513,
                 'fullPullPending': True, 'resumeFloorMs': 1788815371514, 'error': ''}))
            source, warnings = c.cursor_usage(ledger, force=True)
            self.assertEqual(warnings, [])
            source, warnings = c.cursor_usage(ledger, force=True)
        self.assertEqual(warnings, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
        cached = json.loads((self.root / 'state/cursor-usage.json').read_text())
        self.assertFalse(cached['fullPullPending'])
        self.assertEqual(cached['newestTs'], 1788829289 * 1000)
        ledger.db.close()

    def test_main_scan_auto_enable_collects_found_providers(self):
        seed = c.Ledger(self.root / 'state/usage.sqlite')
        legacy = c.record('legacy-1', 'cursor', 's', 1788810000, 'unknown', '', 'Cursor')
        legacy['turns'] = 1
        seed.put(legacy, '/home/u/.config/Cursor/User/globalStorage/state.vscdb')
        seed.db.commit()
        seed.db.close()
        used = []
        def fake_usage(ld, force=False):
            used.append(True)
            return ({'provider': 'cursor', 'path': 'cursor-api', 'files': 0, 'exists': True, 'kind': 'cloud'}, [])
        with patch.object(c, 'STATE', self.root / 'state'), \
             patch.object(c, 'CONFIG', self.root / 'missing-settings.json'), \
             patch.object(c, 'HOME', self.root), \
             patch.dict('os.environ', self.sandbox_env()), \
             patch.object(c, 'cursor_usage', fake_usage), \
             patch.object(c, 'cursor_quota', return_value={}), \
             patch.object(c, 'go_quota', return_value={}), \
             patch.object(c, 'grok_quota', return_value={}), \
             patch('sys.argv', ['collector.py', 'scan']):
            c.main()
        self.assertTrue(used)

    def test_scan_skips_go_quota_when_disabled(self):
        with patch.object(c, 'STATE', self.root / 'state'), \
             patch.object(c, 'CONFIG', self.root / 'settings.json'), \
             patch.object(c, 'HOME', self.root), \
             patch.dict('os.environ', self.sandbox_env()), \
             patch.object(c, 'go_quota', side_effect=AssertionError('disabled source polled')), \
             patch('sys.argv', ['collector.py', 'scan']):
            c.save_settings(c.DEFAULTS | {'enabled': ['codex']})
            c.main()
        self.assertFalse((self.root / 'agents/usage/opencode-go.json').exists())

    def test_cursor_token_missing_warns_and_skips_network(self):
        ledger = c.Ledger(self.root / 'notoken.sqlite')
        with patch.object(c, 'STATE', self.root / 'state'), \
             patch.object(c, 'cursor_token', return_value=None), \
             patch.object(c, 'cursor_post', side_effect=AssertionError('no network')):
            source, warnings = c.cursor_usage(ledger, force=True)
        self.assertEqual(source.get('readErrors'), 1)
        self.assertTrue(any('sign in' in w.lower() for w in warnings))
        ledger.db.close()

    def test_cursor_scan_runs_usage_when_enabled(self):
        ledger = c.Ledger(self.root / 'scan.sqlite')
        seen = []
        def fake_usage(ld, force=False):
            seen.append(ld is ledger)
            return ({'provider': 'cursor', 'path': 'cursor-api', 'files': 0, 'exists': True, 'kind': 'cloud'}, [])
        with patch.object(c, 'HOME', self.root), patch.dict('os.environ', self.sandbox_env()), \
             patch.object(c, 'cursor_usage', fake_usage):
            meta = ledger.scan(c.DEFAULTS | {'enabled': ['cursor']})
        self.assertTrue(seen)
        kinds = [s for s in meta['sources'] if s['provider'] == 'cursor']
        self.assertEqual(kinds[0]['status'], 'available')
        ledger.db.close()

    def test_cursor_quota_maps_summary_and_errors(self):
        summary = {'billingCycleStart': '1786479485000', 'billingCycleEnd': '1789157885000',
                   'planUsage': {'totalPercentUsed': 100}, 'displayMessage': "You've hit your usage limit"}
        with patch.object(c, 'STATE', self.root / 'state'), \
             patch.object(c, 'cursor_token', return_value='tok'), \
             patch.object(c, 'cursor_summary', return_value=summary):
            got = c.cursor_quota(force=True)
            self.assertEqual(c.quota('cursor')['limits'], got['limits'])
        self.assertEqual(got['limits'][0]['label'], 'Billing cycle')
        self.assertEqual(got['limits'][0]['percent'], 1.0)
        self.assertTrue(got['limits'][0]['resetsAt'].startswith('2026-'))
        self.assertIn('usage limit', got['error'])
        with patch.object(c, 'STATE', self.root / 'state'), \
             patch.object(c, 'cursor_token', return_value=None):
            denied = c.cursor_quota(force=True)
        self.assertTrue(denied['error'])
        self.assertIn('expired', c.cursor_api_error(_HTTPError(401)))
        self.assertIn('unavailable', c.cursor_api_error(ValueError('x')))

    def test_migration_adds_turns_and_put_keeps_only_turns(self):
        ledger = c.Ledger(self.root / 'mig.sqlite')
        ledger.db.execute('ALTER TABLE events DROP COLUMN turns')
        ledger.db.commit()
        ledger.db.close()
        fresh = c.Ledger(self.root / 'mig.sqlite')
        columns = {r[1] for r in fresh.db.execute('PRAGMA table_info(events)')}
        self.assertIn('turns', columns)
        empty = c.record('x', 'codex', 's', 1, 'm', '', 'CLI')
        fresh.put(empty)
        turn = dict(empty, id='y', provider='cursor', turns=1)
        fresh.put(turn)
        self.assertEqual(fresh.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        fresh.db.close()

    def test_pi_and_omp_copied_branches_preserve_spend(self):
        entry = {'type': 'message', 'id': 'short-id', 'timestamp': '2026-09-04T12:00:00Z',
                 'message': {'role': 'assistant', 'model': 'custom', 'provider': 'openrouter',
                   'usage': {'input': 10, 'output': 20, 'reasoning': 5, 'cacheRead': 30,
                             'cacheWrite': 5, 'cacheWrite1h': 2, 'cost': {'total': .25}}}}
        original = self.transcript('pi-original.jsonl', [{'type': 'session', 'id': 's', 'cwd': '/project'}, entry])
        fork = self.transcript('pi-fork.jsonl', [{'type': 'session', 'id': 'fork', 'cwd': '/project'}, entry])
        ledger = c.Ledger(self.root / 'pi.sqlite')
        for source in ['pi', 'omp']:
            for path in [original, fork]:
                for r in c.pi_records(path, source): ledger.put(r)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*),SUM(input+output+cacheRead+cacheWrite) FROM events').fetchone(), (2, 130))
        ledger.db.row_factory = sqlite3.Row
        for row in ledger.db.execute('SELECT * FROM events'):
            self.assertEqual(c.price(dict(row), {}), (.25, None))
        ledger.db.close()

    def test_opencode_routes_and_logged_cost(self):
        ledger = c.Ledger(self.root / 'routes.sqlite')
        for route in ['opencode-go', 'openrouter', 'anthropic']:
            ledger.put(c.opencode_record(route, 's', '2026-09-04T12:00:00Z', '/project', 'custom', route,
                                        {'input': 10, 'output': 20, 'reasoning': 5}, .2))
        cfg = c.DEFAULTS | {'enabled': ['opencode-go', 'opencode']}
        report = c.report(ledger, cfg, 7, now=dt.datetime(2026, 9, 5).astimezone())
        self.assertEqual(report['summary']['tokens'], 105)
        # Go keeps the app's recorded estimate as a fallback for unlisted models.
        self.assertEqual(report['summary']['unpricedTokens'], 0)
        self.assertAlmostEqual(report['summary']['value'], .6)
        filtered = c.report(ledger, cfg, 7, now=dt.datetime(2026, 9, 5).astimezone(), selection={'apiProvider': 'openrouter'})
        self.assertEqual(filtered['summary']['tokens'], 35)
        self.assertEqual(len(report['routes']), 3)
        ledger.db.close()

    def test_expanded_settings_keep_prices_and_home_lists(self):
        with patch.object(c, 'CONFIG', self.root / 'settings.json'):
            config = c.save_settings(c.DEFAULTS | {'enabled': list(c.PROVIDERS),
                'monthlyPrices': {'codex': 200, 'gemini': 20, 'pi': None}, 'ompHomes': ['/mounted/.omp/agent']})
            self.assertEqual(config['monthlyPrices'], {'codex': 200, 'gemini': 20})
            self.assertEqual(config['ompHomes'], ['/mounted/.omp/agent'])
            self.assertEqual(config['enabled'], list(c.PROVIDERS))

    def test_ledger_sync_settings_validate(self):
        with patch.object(c, 'CONFIG', self.root / 'settings.json'):
            config = c.save_settings(c.DEFAULTS | {'ledgerSyncDir': '~/Sync/ai-usage', 'ledgerDeviceId': 'desk'})
            self.assertEqual(config['ledgerSyncDir'], str(Path('~/Sync/ai-usage').expanduser().absolute()))
            self.assertEqual(config['ledgerDeviceId'], 'desk')
            with self.assertRaisesRegex(ValueError, 'device id'):
                c.save_settings(c.DEFAULTS | {'ledgerDeviceId': 'x' * 81})

    def test_synced_ledgers_export_import_and_dedupe(self):
        sync = self.root / 'sync'
        local = c.Ledger(self.root / 'state/usage.sqlite')
        remote = c.Ledger(sync / 'laptop.sqlite')
        remote.put(c.record('shared', 'codex', 's1', '2026-09-04T12:00:00Z', 'gpt-4.1', '/project', 'CLI', input=100), '/laptop/shared.jsonl')
        remote.put(c.record('remote-only', 'claude', 's2', '2026-09-04T13:00:00Z', 'claude-x', '/project', 'Claude Code', input=50), '/laptop/only.jsonl')
        remote.db.commit()
        local_path = str(self.root / 'local.jsonl')
        local.put(c.record('shared', 'codex', 's1', '2026-09-04T12:00:00Z', 'gpt-4.1', '/project', 'CLI', input=100), local_path)
        cfg = c.DEFAULTS | {'enabled': ['codex', 'claude'], 'ledgerSyncDir': str(sync), 'ledgerDeviceId': 'desk'}
        self.assertEqual(local.sync_ledgers(cfg), [])
        self.assertTrue((sync / 'desk.sqlite').exists())
        self.assertEqual(local.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
        labels, resolved = c.account_assignments(local, cfg)
        self.assertEqual(labels['machine:laptop'], 'laptop')
        self.assertEqual(resolved['remote-only'], 'machine:laptop')
        self.assertEqual(resolved['shared'], 'local')
        self.assertEqual([row[0] for row in local.db.execute('SELECT path FROM event_sources WHERE event_id=?', ('shared',))], [local_path])
        # Unchanged snapshots are not imported twice.
        self.assertEqual(local.sync_ledgers(cfg), [])
        self.assertEqual(local.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
        # A corrupt snapshot warns once without aborting the scan.
        (sync / 'broken.sqlite').write_bytes(b'not a database')
        self.assertEqual(local.sync_ledgers(cfg), ['Could not read synced ledger broken.sqlite'])
        self.assertEqual(local.sync_ledgers(cfg), [])
        local.db.close(); remote.db.close()

    def test_account_prices_save_and_unknown_price_keys_drop(self):
        with patch.object(c, 'CONFIG', self.root / 'settings.json'):
            config = c.save_settings(c.DEFAULTS | {'enabled': ['codex'], 'monthlyPrices': {'codex': 200, 'work': 20, 'ghost': 5},
                'accounts': [{'id': 'work', 'label': 'Work', 'directories': [{'provider': 'codex', 'path': '/work'}]}]})
            self.assertEqual(config['monthlyPrices'], {'codex': 200, 'work': 20})

    def test_supplemental_go_rates_price_popular_models(self):
        with patch.object(c, 'STATE', self.root):
            rates = c.load_rates()['document']
        for model, expected in [('glm-5.3', 1.4 + 4.4 + 0.26),
                                ('deepseek-v4.1-flash', 0.15 + 0.6 + 0.003)]:
            with self.subTest(model=model):
                rec = c.record('x', 'opencode-go', 's', '2026-09-04T12:00:00Z', model, '/p', 'OpenCode',
                               input=1_000_000, output=1_000_000, cacheRead=1_000_000)
                self.assertAlmostEqual(c.price(rec, rates)[0], expected, places=9)
        free = c.record('x', 'opencode-go', 's', '2026-09-04T12:00:00Z', 'ox-alpha-free', '/p', 'OpenCode', input=1000)
        self.assertEqual(c.price(free, rates)[0], 0)

    def test_catalog_rates_still_win_over_recorded_go_cost(self):
        with patch.object(c, 'STATE', self.root):
            rates = c.load_rates()['document']
        rec = c.record('x', 'opencode-go', 's', '2026-09-04T12:00:00Z', 'glm-5.3-flash', '/p', 'OpenCode', input=1_000_000)
        rec['reportedValue'] = 99.0
        self.assertAlmostEqual(c.price(rec, rates)[0], 0.15, places=9)

    def test_go_peak_hours_double_deepseek_rates(self):
        with patch.object(c, 'STATE', self.root):
            rates = c.load_rates()['document']
        def value(ts):
            rec = c.record('x', 'opencode-go', 's', ts, 'deepseek-v4.1-flash', '/p', 'OpenCode',
                           input=1_000_000, output=1_000_000, cacheRead=1_000_000)
            return c.price(rec, rates)[0]
        off_peak = value('2026-09-08T15:00:00Z')
        peak = value('2026-09-08T02:00:00Z')
        weekend = value('2026-09-12T02:00:00Z')
        self.assertAlmostEqual(off_peak, 0.15 + 0.6 + 0.003, places=9)
        self.assertAlmostEqual(peak, off_peak * 2, places=9)
        self.assertAlmostEqual(weekend, off_peak, places=9)

    def test_commandcode_deepseek_v4_1_flash_fast_is_priced(self):
        # The route's own table prices it below the v4 Flash Fast rates, and its
        # cache reads bill far above the non-fast V4.1 Flash ones.
        with patch.object(c, 'STATE', self.root):
            rates = c.load_rates()['document']
        def value(ts):
            rec = c.record('x', 'commandcode', 's', ts, 'deepseek/deepseek-v4.1-flash-fast', '/p', 'Hermes',
                           input=1_000_000, output=1_000_000, cacheRead=1_000_000)
            return c.price(rec, rates)
        off_peak, saving = value('2026-09-08T15:00:00Z')
        self.assertIsNotNone(off_peak, 'the route prices this model instead of leaving it unpriced')
        self.assertAlmostEqual(off_peak, 0.16 + 0.58 + 0.016, places=9)
        self.assertAlmostEqual(saving, 0.16 - 0.016, places=9)
        self.assertAlmostEqual(value('2026-09-08T02:00:00Z')[0], off_peak * 2, places=9)
        self.assertAlmostEqual(value('2026-09-12T02:00:00Z')[0], off_peak, places=9)

    def test_internal_codex_model_is_zero_and_not_unpriced(self):
        with patch.object(c, 'STATE', self.root):
            rates = c.load_rates()['document']
        auto_review = c.record('x', 'codex', 's', '2026-09-04T12:00:00Z', 'codex-auto-review', '/p', 'CLI', input=1000)
        self.assertEqual(c.price(auto_review, rates), (0.0, None))
        spark = c.record('x', 'codex', 's', '2026-09-04T12:00:00Z', 'gpt-5.3-codex-spark', '/p', 'CLI', input=1_000_000)
        self.assertAlmostEqual(c.price(spark, rates)[0], 1.75, places=9)

    def test_new_direct_model_rates_include_cache_and_long_context(self):
        rates = c.load_rates()['document']
        opus = c.record('o', 'claude', 's', 1, 'claude-opus-5-5', '/p', 'CLI',
                        input=1_000_000, output=1_000_000, cacheRead=1_000_000,
                        cacheWrite=2_000_000, cacheWrite1h=1_000_000)
        self.assertAlmostEqual(c.price(opus, rates)[0], 4 + 20 + .2 + 5 + 8)
        for model, output, long in [('gpt-6-sol', 10, 4 + 15 + .4 + 5),
                                    ('gpt-6-luna', .5, .2 + .75 + .02 + .25)]:
            with self.subTest(model=model):
                row = c.record('g', 'codex', 's', 1, model, '/p', 'CLI',
                               input=1_000_000, output=1_000_000,
                               cacheRead=1_000_000, cacheWrite=1_000_000)
                self.assertAlmostEqual(c.price(row, rates)[0], long)
                row.update(input=50_000, cacheRead=50_000, cacheWrite=50_000)
                rate = rates['codex/' + model]
                expected = output + 50_000 * sum(rate[key] for key in (
                    'input_cost_per_token', 'cache_read_input_token_cost',
                    'cache_creation_input_token_cost'))
                self.assertAlmostEqual(c.price(row, rates)[0], expected)

    def test_gpt_6_1_sol_prices_cache_and_full_request_long_context(self):
        rates = c.load_rates()['document']
        for cached, expected in [(22_000, .25 + .5 + .0022 + .0625),
                                 (122_000, .25 + .5 + .0122 + .0625),
                                 (122_001, .5 + .75 + .0244002 + .125)]:
            with self.subTest(cached=cached):
                row = c.record('g', 'codex', 's', 1, 'gpt-6.1-sol', '/p', 'CLI',
                               input=125_000, output=50_000,
                               cacheRead=cached, cacheWrite=25_000)
                value, savings = c.price(row, rates)
                self.assertIsNotNone(value)
                self.assertAlmostEqual(value, expected, places=9)
                self.assertAlmostEqual(savings, cached * (1.9e-6 if cached <= 122_000 else 3.8e-6))

    def tier_rollout(self, entries, provider='openai'):
        """A Codex rollout with turns, thread settings, and distinct usage events."""
        clock = iter(range(10, 60))
        def at(): return f'2026-09-04T12:00:{next(clock):02d}Z'
        lines = [{'type': 'session_meta', 'timestamp': '2026-09-04T12:00:05Z', 'payload': {
            'id': 'tiers', 'timestamp': '2026-09-04T12:00:05Z', 'model_provider': provider}},
            {'type': 'turn_context', 'timestamp': '2026-09-04T12:00:06Z', 'payload': {'model': 'gpt-6.1-sol'}}]
        total = 0
        for entry in entries:
            if entry == 'turn':
                lines.append({'type': 'event_msg', 'timestamp': at(), 'payload': {'type': 'task_started'}})
            elif entry == 'usage':
                total += 100
                event = self.codex_event(at(), total)
                lines.append(event)
            else:
                lines.append({'type': 'event_msg', 'timestamp': entry.get('ts') or at(), 'payload': {
                    'type': 'thread_settings_applied', 'thread_settings': entry['settings']}})
        return self.transcript('codex/sessions/2026/09/04/rollout-2026-09-04T12-00-05-tiers.jsonl', lines)

    def test_codex_tier_follows_recorded_thread_settings(self):
        fast = {'settings': {'model_provider_id': 'openai', 'service_tier': 'priority'}}
        standard = {'settings': {'model_provider_id': 'openai', 'service_tier': 'default'}}
        path = self.tier_rollout([
            # The first turn names no tier: Codex logs settings from the second.
            'turn', 'usage', fast, 'turn', 'usage', 'usage',
            # A later turn without new settings keeps the thread's tier.
            'turn', 'usage',
            standard, 'turn', 'usage',
            # A change after the turn produced usage may only apply from the
            # next turn, so the rest of this one has no known tier.
            fast, 'usage', 'turn', 'usage',
            {'settings': {'model_provider_id': 'openai', 'service_tier': 'auto'}}, 'turn', 'usage',
            # A pending change that is then unset mid-turn leaves the next turn unknown.
            'usage', fast, {'settings': {'model_provider_id': 'openai'}}, 'turn', 'usage',
            {'settings': {'model_provider_id': 'openai', 'service_tier': 'fast'}}, 'turn', 'usage',
            # Tiers belong to OpenAI's API, not to another model provider.
            {'settings': {'model_provider_id': 'commandcode', 'service_tier': 'priority'}}, 'turn', 'usage',
            # Inherited fork history predates the session and is ignored.
            {'ts': '2026-09-03T12:00:00Z', 'settings': {'model_provider_id': 'openai', 'service_tier': 'flex'}},
            'turn', 'usage'])
        tiers = [r['requestedServiceTier'] for r in c.codex_records(path)]
        self.assertEqual(tiers, [None, 'priority', 'priority', 'priority', 'default', None, 'priority',
                                 None, None, None, 'priority', None, None])

    def test_codex_tier_pricing_standard_fast_flex_and_unknown(self):
        rates = c.load_rates()['document']
        def row(cached, **tiers):
            return c.record('g', 'codex', 's', 1, 'gpt-6.1-sol', '/p', 'CLI', input=125_000, output=50_000,
                            cacheRead=cached, cacheWrite=25_000) | tiers
        # Standard short and full-request long context, from the published table.
        for cached, standard in [(22_000, .25 + .5 + .0022 + .0625), (122_001, .5 + .75 + .0244002 + .125)]:
            with self.subTest(cached=cached):
                read_saving = cached * (1.9e-6 if cached <= 122_000 else 3.8e-6)
                for tiers, factor, basis in [({'requestedServiceTier': 'default'}, 1, 'requested'),
                                             ({'requestedServiceTier': 'priority'}, 2, 'requested'),
                                             ({'requestedServiceTier': 'flex'}, .5, 'requested'),
                                             ({}, 1, 'assumed'),
                                             # A downgraded Fast request is billed at Standard.
                                             ({'requestedServiceTier': 'priority', 'serviceTier': 'default'}, 1, 'served'),
                                             ({'serviceTier': 'priority'}, 2, 'served')]:
                    value, savings, (tier, evidence) = c.price_detail(row(cached, **tiers), rates)
                    self.assertAlmostEqual(value, standard * factor, places=9)
                    self.assertAlmostEqual(savings, read_saving * factor, places=9)
                    self.assertEqual(evidence, basis)
        # A tier with no published rate stays unpriced instead of borrowing Standard.
        spark = c.record('x', 'codex', 's', 1, 'gpt-5.3-codex-spark', '/p', 'CLI', input=1000) | {'requestedServiceTier': 'priority'}
        self.assertEqual(c.price(spark, rates), (None, None))
        self.assertEqual(c.price(row(0, requestedServiceTier='ultrafast'), rates), (None, None))
        # Models without tier rates are not reported as a Standard assumption.
        opus = c.record('o', 'claude', 's', 1, 'claude-opus-5-5', '/p', 'CLI', input=1000)
        self.assertEqual(c.price_detail(opus, rates)[2], (None, None))
        gemini = c.record('m', 'gemini', 's', 1, 'gemini-2.5-pro', '/p', 'Gemini', input=1000)
        self.assertEqual(c.price_detail(gemini, rates)[2], (None, None))
        # Fast is 2x every applicable Standard rate; the separate subscription
        # allowance multiplier is not an API price.
        sol = rates['codex/gpt-6.1-sol']
        for key in [k for k in sol if k.endswith('_priority')]:
            self.assertAlmostEqual(sol[key], 2 * sol[key[:-len('_priority')]], places=15)

    def test_mixed_tier_session_reconciles_with_request_prices(self):
        ledger = c.Ledger(self.root / 'tiers.sqlite')
        now = dt.datetime(2026, 9, 5, 12).astimezone()
        rows = [c.record(key, 'codex', 'one-session', '2026-09-05T10:00:00', 'gpt-6.1-sol', '/p', 'T3 Code',
                         input=1000 * n, output=200 * n, cacheRead=5000 * n, reasoning=50) | ({'requestedServiceTier': tier} if tier else {})
                for n, (key, tier) in enumerate([('fast', 'priority'), ('standard', 'default'), ('unknown', None),
                                                 ('fast-long', 'priority')], 1)]
        rows[-1].update(input=300_000)
        for r in rows: ledger.put(r)
        rates = c.load_rates()
        stored = [dict(zip(('id', 'tier'), r)) for r in ledger.db.execute('SELECT id,requestedServiceTier FROM events ORDER BY id')]
        self.assertEqual({r['id']: r['tier'] for r in stored}, {'fast': 'priority', 'standard': 'default', 'unknown': None, 'fast-long': 'priority'})
        with patch.object(c, 'load_rates', return_value=rates):
            summary = c.report(ledger, c.DEFAULTS, 1, now=now)['summary']
        priced = [c.price(r, rates['document']) for r in rows]
        tokens = [sum(r[f] for f in c.FIELDS[:4]) for r in rows]
        self.assertEqual(summary['tokens'], sum(tokens))
        self.assertEqual(summary['reasoning'], 200)
        self.assertEqual(summary['sessions'], 1)
        self.assertAlmostEqual(summary['value'], sum(v for v, _ in priced), places=9)
        self.assertAlmostEqual(summary['cacheSavings'], sum(s for _, s in priced), places=9)
        self.assertEqual(summary['fastTokens'], tokens[0] + tokens[3])
        self.assertEqual(summary['tierRequestedTokens'], tokens[0] + tokens[1] + tokens[3])
        self.assertEqual(summary['tierAssumedTokens'], tokens[2])
        self.assertEqual(summary['tierServedTokens'], 0)
        # The Fast long-context request is twice its Standard price.
        standard = c.price(rows[3] | {'requestedServiceTier': 'default'}, rates['document'])[0]
        self.assertAlmostEqual(priced[3][0], 2 * standard, places=9)
        ledger.db.close()

    def test_ledger_migration_backfills_recorded_tiers_only(self):
        fast = {'settings': {'model_provider_id': 'openai', 'service_tier': 'priority'}}
        rollout = self.tier_rollout(['turn', 'usage', fast, 'turn', 'usage'])
        path = self.root / 'old.sqlite'
        # A ledger from before tiers: the previous schema, already indexed.
        db = sqlite3.connect(path)
        db.executescript('''
          CREATE TABLE events (id TEXT PRIMARY KEY, provider TEXT, session TEXT, ts INTEGER,
            model TEXT, project TEXT, client TEXT, input INTEGER, output INTEGER, cacheRead INTEGER,
            cacheWrite INTEGER, cacheWrite1h INTEGER, reasoning INTEGER, reportedCostTicks INTEGER,
            modelCalls INTEGER, turns INTEGER, reportedValue REAL, apiProvider TEXT, timePrecision TEXT);
          CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER);
          CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE event_sources(event_id TEXT, path TEXT, PRIMARY KEY(event_id,path));
          INSERT INTO metadata VALUES ('provenanceVersion','2');
        ''')
        for r in c.codex_records(rollout):
            r.pop('requestedServiceTier')
            db.execute(f'INSERT INTO events ({",".join(r)}) VALUES ({",".join("?" for _ in r)})', list(r.values()))
            db.execute('INSERT INTO event_sources VALUES (?,?)', (r['id'], str(rollout.resolve())))
        # Usage whose transcript no longer exists keeps an unknown tier.
        gone = c.record('gone', 'codex', 'old', '2026-09-01T10:00:00Z', 'gpt-6.1-sol', '/p', 'CLI', input=500)
        db.execute(f'INSERT INTO events ({",".join(gone)}) VALUES ({",".join("?" for _ in gone)})', list(gone.values()))
        stat = rollout.stat()
        db.execute('INSERT INTO files VALUES (?,?,?)', (str(rollout), stat.st_size, stat.st_mtime_ns))
        db.commit()
        before = db.execute('SELECT COUNT(*),SUM(input),SUM(output),SUM(cacheRead),SUM(cacheWrite),SUM(reasoning) FROM events').fetchone()
        db.close()
        ledger = c.Ledger(path)
        ledger.scan(c.DEFAULTS)
        rows = ledger.db.execute('SELECT id,requestedServiceTier,serviceTier FROM events ORDER BY ts').fetchall()
        self.assertEqual([(t, s) for _, t, s in rows], [(None, None), (None, None), ('priority', None)])
        self.assertEqual(rows[0][0], 'gone')
        self.assertEqual(ledger.db.execute('SELECT COUNT(*),SUM(input),SUM(output),SUM(cacheRead),SUM(cacheWrite),SUM(reasoning) FROM events').fetchone(), before)
        ledger.db.close()
        # The one-time re-read is not repeated on the next open.
        ledger = c.Ledger(path)
        self.assertIsNotNone(ledger.db.execute('SELECT 1 FROM files WHERE path=?', (str(rollout),)).fetchone())
        ledger.db.close()

    def test_tier_corrections_sync_without_changing_token_totals(self):
        sync = self.root / 'tier-sync'
        owner = c.Ledger(self.root / 'owner.sqlite')
        peer = c.Ledger(self.root / 'peer.sqlite')
        row = c.record('tier-correction', 'codex', 'session', '2026-09-04T12:00:00Z',
                       'gpt-6.1-sol', '/p', 'CLI', input=100, cacheRead=1000)
        source = str(self.root / 'rollout-local.jsonl')
        cfg = c.DEFAULTS | {'ledgerSyncDir': str(sync), 'ledgerDeviceId': 'owner'}
        for tier in ['priority', 'default', None]:
            owner.put(row | {'requestedServiceTier': tier}, source)
            owner.db.commit()
            self.assertEqual(owner.sync_ledgers(cfg), [])
            peer.import_ledger(sync / 'owner.sqlite', 'owner')
            self.assertEqual(peer.db.execute('SELECT requestedServiceTier FROM events').fetchone(), (tier,))
            self.assertEqual(peer.db.execute('SELECT COUNT(*),SUM(input+cacheRead+output) FROM events').fetchone(), (1, 1100))
        # An older ledger omits the field, rather than explicitly clearing it.
        owner.put(row | {'requestedServiceTier': 'priority'}, source)
        owner.put(row, source)
        self.assertEqual(owner.db.execute('SELECT requestedServiceTier FROM events').fetchone(), ('priority',))
        # Remote copies cannot overwrite the local source's explicit unknown.
        owner.put(row | {'requestedServiceTier': None}, source)
        peer.put(row | {'requestedServiceTier': 'priority'})
        peer.db.commit()
        owner.import_ledger(self.root / 'peer.sqlite', 'peer')
        self.assertEqual(owner.db.execute('SELECT requestedServiceTier FROM events').fetchone(), (None,))
        owner.db.close(); peer.db.close()

    def test_stale_peer_cannot_undo_owner_tier_correction(self):
        sync = self.root / 'three-machine-sync'
        owner = c.Ledger(self.root / 'owner.sqlite')
        peer = c.Ledger(self.root / 'peer.sqlite')
        third = c.Ledger(self.root / 'third.sqlite')
        row = c.record('shared-tier', 'codex', 'session', '2026-09-04T12:00:00Z',
                       'gpt-6.1-sol', '/p', 'CLI', input=100, cacheRead=1000)
        def cfg(device): return c.DEFAULTS | {'ledgerSyncDir': str(sync), 'ledgerDeviceId': device}
        owner.put(row | {'requestedServiceTier': 'priority'}, str(self.root / 'owner-rollout.jsonl'))
        owner.db.commit(); owner.sync_ledgers(cfg('a-owner'))
        peer.sync_ledgers(cfg('z-peer'))
        peer.sync_ledgers(cfg('z-peer'))  # Re-export the owner's original Fast evidence.
        for tier in ['default', None]:
            owner.put(row | {'requestedServiceTier': tier}, str(self.root / 'owner-rollout.jsonl'))
            owner.db.commit(); owner.sync_ledgers(cfg('a-owner'))
            # Sorted import sees the owner's correction before the stale peer.
            third.sync_ledgers(cfg('third'))
            third.sync_ledgers(cfg('third'))
            self.assertEqual(third.db.execute('SELECT requestedServiceTier FROM events').fetchone(), (tier,))
            self.assertEqual(third.db.execute('SELECT COUNT(*),SUM(input+cacheRead+output) FROM events').fetchone(), (1, 1100))
        owner.db.close(); peer.db.close(); third.db.close()

    def test_tier_migration_reindexes_preview_and_invalidates_snapshot(self):
        path = self.root / 'preview.sqlite'
        ledger = c.Ledger(path)
        ledger.db.execute("UPDATE metadata SET value='1' WHERE key='serviceTierVersion'")
        ledger.db.execute("INSERT INTO metadata VALUES ('ledgerSignature','old')")
        ledger.db.execute("INSERT INTO files VALUES ('/p/rollout-preview.jsonl',1,1)")
        ledger.db.commit(); ledger.db.close()
        ledger = c.Ledger(path)
        self.assertEqual(ledger.db.execute("SELECT value FROM metadata WHERE key='serviceTierVersion'").fetchone(), ('2',))
        self.assertIsNone(ledger.db.execute("SELECT value FROM metadata WHERE key='ledgerSignature'").fetchone())
        self.assertIsNone(ledger.db.execute("SELECT 1 FROM files WHERE path LIKE '%rollout-%.jsonl'").fetchone())
        ledger.db.close()

    def test_new_commandcode_rates_keep_route_pricing(self):
        rates = c.load_rates()['document']
        for model, expected in [('xiaomi/mimo-v2.6-flash', .14 + .28 + .0028),
                                ('xiaomi/mimo-v2.6-pro', .435 + .87 + .0036),
                                ('xiaomi/mimo-v2.6-pro-ultraspeed', 4.35 + 8.7 + .036),
                                ('gpt-6-sol', 2 + 10 + .2),
                                ('gpt-6-luna', .1 + .5 + .01),
                                ('claude-opus-5-5', 4 + 20 + .2)]:
            with self.subTest(model=model):
                row = c.record('x', 'commandcode', 's', 1, model, '/p', 'Hermes',
                               input=1_000_000, output=1_000_000, cacheRead=1_000_000)
                self.assertAlmostEqual(c.price(row, rates)[0], expected)

    def test_go_allowance_uses_monthly_window_and_promo(self):
        ledger = c.Ledger(self.root / 'allowance.sqlite')
        ledger.put(c.opencode_record('m1', 's', '2026-09-14T12:00:00Z', '/p', 'deepseek-v4.1-flash', 'opencode-go', {'input': 1_000_000}, 0))
        ledger.put(c.opencode_record('m2', 's', '2026-09-02T12:00:00Z', '/p', 'glm-5.3-flash', 'opencode-go', {'input': 1_000_000}, 0))
        ledger.put(c.opencode_record('m3', 's', '2026-08-01T12:00:00Z', '/p', 'glm-5.3-flash', 'opencode-go', {'input': 9_000_000}, 0))
        rates = {'source': 'test', 'document': {
            'opencode-go/deepseek-v4.1-flash': {'input_cost_per_token': .000001, 'output_cost_per_token': 0, 'cache_read_input_token_cost': 0,
                                                'monthly_limit_usd': 15, 'monthly_limit_promo_usd': 60, 'promo_ends': '2026-09-20'},
            'opencode-go/glm-5.3-flash': {'input_cost_per_token': .000002, 'output_cost_per_token': 0, 'cache_read_input_token_cost': 0,
                                          'monthly_limit_usd': 60}}}
        cfg = c.DEFAULTS | {'enabled': ['opencode-go']}
        with patch.object(c, 'load_rates', return_value=rates), patch.object(c, 'theme', return_value={}), \
             patch.object(c, 'quota', return_value={'limits': [{'label': 'Monthly', 'resetsAt': '2026-10-01T00:00:00+00:00'}]}):
            data = c.report(ledger, cfg, 7, now=dt.datetime(2026, 9, 15).astimezone())
        allowance = {row['model']: row for row in data['goAllowance']['models']}
        self.assertEqual(sorted(allowance), ['deepseek-v4.1-flash', 'glm-5.3-flash'])
        self.assertAlmostEqual(allowance['deepseek-v4.1-flash']['value'], 1.0)
        self.assertEqual((allowance['deepseek-v4.1-flash']['limit'], allowance['deepseek-v4.1-flash']['promo']), (60, True))
        self.assertAlmostEqual(allowance['glm-5.3-flash']['value'], 2.0)
        self.assertEqual((allowance['glm-5.3-flash']['limit'], allowance['glm-5.3-flash']['promo']), (60, False))
        ledger.db.close()

    def test_opencode_legacy_and_database_copies_merge(self):
        root = self.root / 'data/opencode'; root.mkdir(parents=True)
        item = {'id': 'm1', 'sessionID': 's', 'role': 'assistant', 'providerID': 'openrouter',
                'modelID': 'custom', 'time': {'created': 1788580000000}, 'path': {'cwd': '/project'},
                'tokens': {'input': 10, 'output': 20}, 'cost': .25}
        path = root / 'storage/message/s/m1.json'; path.parent.mkdir(parents=True); path.write_text(json.dumps(item))
        db = sqlite3.connect(root / 'opencode.db')
        db.executescript('CREATE TABLE message(id,session_id,time_created,data); CREATE TABLE session(id,directory);')
        db.execute('INSERT INTO session VALUES (?,?)', ('s', '/project'))
        db.execute('INSERT INTO message VALUES (?,?,?,?)', ('m1', 's', 1788580000000, json.dumps(item)))
        db.commit(); db.close()
        ledger = c.Ledger(self.root / 'legacy.sqlite')
        with patch.object(c, 'HOME', self.root), patch.dict('os.environ', {
            'CODEX_HOME': str(self.root / 'codex'), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'GROK_HOME': str(self.root / 'grok'), 'PI_CODING_AGENT_DIR': str(self.root / 'pi'), 'XDG_DATA_HOME': str(self.root / 'data')}):
            ledger.scan(c.DEFAULTS)
            ledger.scan(c.DEFAULTS)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*),SUM(input+output),SUM(reportedValue) FROM events').fetchone(), (1, 30, .25))
        ledger.db.close()

    def test_muse_contributor_and_standard_rates(self):
        with patch.object(c, 'STATE', self.root / 'state'), patch.object(c, 'HOME', self.root):
            catalog = c.load_rates()['document']
            contributor = c.record('m', 'muse', 's', 1, 'muse-spark-1.3-contributor', '', 'Muse',
                                   input=1000000, output=1000000, cacheRead=1000000)
            self.assertAlmostEqual(c.price(contributor, catalog)[0], 0.1 + 0.2 + 0.002)
            standard = c.record('m', 'muse', 's', 1, 'muse-spark-1.3', '', 'Muse',
                                input=1000000, output=1000000, cacheRead=1000000)
            self.assertAlmostEqual(c.price(standard, catalog)[0], 1.25 + 4.25 + 0.15)
            self.assertIn('Muse official rates', c.load_rates()['source'])

    def test_muse_unknown_model_and_cache_write_stay_unpriced(self):
        with patch.object(c, 'STATE', self.root / 'state'), patch.object(c, 'HOME', self.root):
            catalog = c.load_rates()['document']
            unknown = c.record('m', 'muse', 's', 1, 'muse-spark-9', '', 'Muse', input=100)
            self.assertEqual(c.price(unknown, catalog), (None, None))
            # No published cache-write rate exists for Muse models.
            written = c.record('m', 'muse', 's', 1, 'muse-spark-1.3-contributor', '', 'Muse',
                               input=100, cacheWrite=5)
            self.assertEqual(c.price(written, catalog), (None, None))

    def test_user_catalog_overrides_official_rates(self):
        with patch.object(c, 'STATE', self.root / 'state'):
            c.atomic_json(self.root / 'state/rates.json', {'source': 'custom', 'document': {
                'muse/muse-spark-1.3-contributor': {'input_cost_per_token': 1},
                'opencode-go/glm-5.3-flash': {'input_cost_per_token': 2}}})
            catalog = c.load_rates()['document']
            self.assertEqual(catalog['muse/muse-spark-1.3-contributor']['input_cost_per_token'], 1)
            self.assertEqual(catalog['opencode-go/glm-5.3-flash']['input_cost_per_token'], 2)
            self.assertIn('custom', c.load_rates()['source'])

    def test_gemini_catalog_fills_user_catalog_gaps_without_repricing(self):
        with patch.object(c, 'STATE', self.root):
            c.atomic_json(self.root / 'rates.json', {'source': 'custom', 'document': {'custom-model': {'input_cost_per_token': 1}}})
            catalog = c.load_rates()['document']
            self.assertEqual(catalog['custom-model']['input_cost_per_token'], 1)
            record = c.record('g', 'gemini', 's', 1, 'gemini-3.8-flash', '', 'Gemini CLI', input=1000000, output=1000000, cacheRead=1000000)
            self.assertAlmostEqual(c.price(record, catalog)[0], 4.575)

    def test_today_uses_hourly_buckets_without_future_zero_hours(self):
        ledger = c.Ledger(self.root / 'today.sqlite')
        now = dt.datetime(2026, 9, 5, 1, 30).astimezone()
        for key, when, tokens in [('a', '2026-09-05T00:15:00', 100),
                                  ('b', '2026-09-05T01:10:00', 200),
                                  ('prior', '2026-09-04T01:10:00', 50),
                                  ('later-prior', '2026-09-04T02:10:00', 999),
                                  ('future', '2026-09-05T02:10:00', 999)]:
            ledger.put(c.record(key, 'codex', key, when, 'm', '', 'CLI', input=tokens))
        with patch.object(c, 'load_rates', return_value={'document': {}, 'source': 'test'}):
            r = c.report(ledger, c.DEFAULTS, 1, now=now)
        self.assertEqual([h['label'] for h in r['hourly']], ['00:00', '01:00'])
        self.assertEqual([h['providers']['codex']['tokens'] for h in r['hourly']], [100, 200])
        self.assertEqual(r['summary']['tokens'], 300)
        self.assertEqual(r['previous']['tokens'], 50)
        self.assertTrue(r['hourly'][-1]['title'].endswith('to now'))
        ledger.db.close()

    def test_hour_drill_keeps_session_summaries_unplaced_and_previous_day_comparable(self):
        ledger = c.Ledger(self.root / 'hours.sqlite')
        now = dt.datetime(2026, 9, 5, 12).astimezone()
        for key, when, tokens, client in [
                ('today', '2026-09-05T10:15:00', 120, 'CLI'),
                ('later', '2026-09-05T11:15:00', 30, 'CLI'),
                ('session', '2026-09-05T10:20:00', 400, 'Hermes'),
                ('prior', '2026-09-04T10:15:00', 70, 'CLI')]:
            item = c.record(key, 'codex', key, when, 'm', '', client, input=tokens)
            if client == 'Hermes': item['timePrecision'] = 'session'
            ledger.put(item)
        with patch.object(c, 'load_rates', return_value={'document': {}, 'source': 'test'}):
            day = c.report(ledger, c.DEFAULTS, 7, now=now, selection={'day': '2026-09-05'})
            hour = c.report(ledger, c.DEFAULTS, 7, now=now,
                            selection={'day': '2026-09-05', 'hourStart': int(dt.datetime(2026, 9, 5, 10).timestamp())})
        self.assertEqual(day['summary']['tokens'], 550)
        self.assertEqual(day['previous']['tokens'], 70)
        self.assertEqual(day['hourlyUnplaced']['total']['tokens'], 400)
        self.assertEqual(sum(h['total']['tokens'] for h in day['hourly']), 150)
        self.assertEqual(hour['summary']['tokens'], 120)
        self.assertEqual(hour['hourly'][0]['total']['tokens'], 120)
        self.assertEqual([row['name'] for row in hour['sessions']], ['today'])
        snapshot = c.hourly_snapshot(ledger, now)
        self.assertEqual((snapshot['tokens'], snapshot['timedTokens'], snapshot['unplacedTokens']), (550, 150, 400))
        self.assertEqual(sum(h['tokens'] for h in snapshot['hours']), 150)
        ledger.db.close()

    def test_hour_labels_distinguish_daylight_saving_repeated_hour(self):
        if not hasattr(time, 'tzset'): self.skipTest('tzset unavailable')
        ledger = c.Ledger(self.root / 'dst.sqlite')
        old = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'America/New_York'; time.tzset()
            now = dt.datetime.fromisoformat('2026-11-01T02:30:00-05:00')
            for key, when in [('first', '2026-11-01T01:15:00-04:00'),
                              ('second', '2026-11-01T01:15:00-05:00')]:
                ledger.put(c.record(key, 'codex', key, when, 'm', '', 'CLI', input=100))
            with patch.object(c, 'load_rates', return_value={'document': {}, 'source': 'test'}):
                result = c.report(ledger, c.DEFAULTS, 1, now=now)
            repeated = [h for h in result['hourly'] if h['label'] == '01:00']
            self.assertEqual(len(repeated), 2)
            self.assertNotEqual(repeated[0]['title'], repeated[1]['title'])
            self.assertEqual([h['total']['tokens'] for h in repeated], [100, 100])
            self.assertEqual(sum(h['total']['tokens'] for h in result['hourly']), result['summary']['tokens'])
            snapshot = c.hourly_snapshot(ledger, now)
            repeated_panel = [h for h in snapshot['hours'] if h['label'] == '01:00']
            self.assertEqual([h['zone'] for h in repeated_panel], ['EDT', 'EST'])
        finally:
            if old is None: os.environ.pop('TZ', None)
            else: os.environ['TZ'] = old
            time.tzset()
            ledger.db.close()

    def test_drilldown_reconciles_sessions_and_partial_pricing(self):
        ledger = c.Ledger(self.root / 'detail.sqlite')
        now = dt.datetime(2026, 9, 5, 12).astimezone()
        for key, model, project, inp in [('a', 'known', '/one', 100), ('b', 'unknown', '/one', 200), ('c', 'known', '/two', 300)]:
            ledger.put(c.record(key, 'codex', key, '2026-09-05T10:00:00', model, project, 'CLI', input=inp))
        rates = {'source':'test', 'document':{'known':{'input_cost_per_token':0.01}}}
        with patch.object(c, 'load_rates', return_value=rates):
            whole = c.report(ledger, c.DEFAULTS, 7, now=now)
            detail = c.report(ledger, c.DEFAULTS, 7, now=now, selection={'project':'/one', 'day':'2026-09-05'})
            empty = c.report(ledger, c.DEFAULTS, 7, now=now, selection={'model':'absent'})
        self.assertEqual(whole['summary']['tokens'], sum(s['tokens'] for s in whole['sessions']))
        self.assertEqual(detail['summary']['tokens'], 300)
        self.assertEqual(detail['summary']['value'], 1)
        self.assertEqual(detail['summary']['unpricedTokens'], 200)
        self.assertEqual(detail['pricing']['unpriced'][0]['name'], 'unknown')
        self.assertAlmostEqual(detail['pricing']['coveragePercent'], 100/3)
        self.assertEqual(len(detail['sessions']), 2)
        self.assertEqual(len(detail['hourly']), 13)
        self.assertEqual(sum(h['providers']['codex']['tokens'] for h in detail['hourly']), detail['summary']['tokens'])
        self.assertEqual(empty['sessions'], [])
        self.assertIsNone(empty['pricing']['coveragePercent'])
        ledger.db.close()

    def test_missing_source_does_not_claim_fresh_history(self):
        ledger = c.Ledger(self.root / 'coverage.sqlite')
        with patch.object(c, 'HOME', self.root), patch.dict('os.environ', {
                'XDG_DATA_HOME':str(self.root/'data'), 'CODEX_HOME':str(self.root/'codex'),
                'CLAUDE_CONFIG_DIR':str(self.root/'claude')}):
            meta = ledger.scan(c.DEFAULTS)
        self.assertTrue(meta['scannedAt'])
        self.assertTrue(all(s['status'] == 'missing' for s in meta['sources']))
        self.assertTrue(all(s.get('latestFileAt') is None for s in meta['sources']))
        ledger.db.close()

    def test_bundled_catalog_works_without_t3_or_user_state(self):
        with patch.object(c, 'STATE', self.root / 'state'), patch.object(c, 'HOME', self.root):
            rates=c.load_rates()
        self.assertGreater(len(rates['document']), 100)
        self.assertNotIn('T3', rates['source'])
        record=c.record('x','codex','s',1,'gpt-4.1','','CLI',input=100,output=10)
        self.assertIsNotNone(c.price(record,rates['document'])[0])

    def hermes_ledger(self, rows, name='hermes/state.db', sessions=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.execute('''CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)''')
        db.execute('''CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT,
            task TEXT, api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
            estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,
            first_seen REAL, last_seen REAL)''')
        for sid, cwd in (sessions or []): db.execute('INSERT INTO sessions VALUES (?,?)', (sid, cwd))
        for row in rows:
            values = {'session_id': 's1', 'model': 'deepseek-v4.1-flash', 'billing_provider': 'opencode-go',
                      'billing_base_url': 'https://opencode.ai/zen/go/v1', 'billing_mode': '', 'task': '',
                      'api_call_count': 4, 'input_tokens': 1000, 'output_tokens': 200,
                      'cache_read_tokens': 5000, 'cache_write_tokens': 0, 'reasoning_tokens': 300,
                      'estimated_cost_usd': 0.0, 'actual_cost_usd': 0.0, 'cost_status': None, 'cost_source': None,
                      'first_seen': 1789000000.0, 'last_seen': 1789000100.0}
            values.update(row)
            db.execute('INSERT INTO session_model_usage VALUES (%s)' % ','.join('?' * len(values)), list(values.values()))
        db.commit(); db.close()
        return path

    def test_hermes_rows_map_to_their_route_totals(self):
        path = self.hermes_ledger([
            {'session_id': 's1', 'model': 'deepseek-v4.1-flash'},
            {'session_id': 's2', 'model': 'glm-5.2', 'billing_provider': 'ollama-cloud',
             'billing_base_url': 'https://ollama.com/v1', 'task': 'background_review',
             'input_tokens': 10, 'output_tokens': 2, 'cache_read_tokens': 0, 'reasoning_tokens': 0,
             'last_seen': 1789000300.0}], sessions=[('s1', '/home/user/Projects/toolport'), ('s2', '/home/user')])
        records = list(c.hermes_records(path))
        self.assertEqual(len(records), 2)
        go = records[0]['row']
        self.assertEqual((go['provider'], go['session'], go['model'], go['project'], go['client']),
                         ('opencode-go', 's1', 'deepseek-v4.1-flash', '/home/user/Projects/toolport', 'Hermes'))
        # Reasoning stays a separate column and is never added to output:
        # Hermes stores the provider's completion_tokens, which already
        # include it, unlike OpenCode's disjoint counter.
        self.assertEqual((go['input'], go['output'], go['reasoning'], go['cacheRead']), (1000, 200, 300, 5000))
        self.assertEqual(sum(go[f] for f in c.FIELDS[:4]), 6200)
        # Anchored at first_seen, not last_seen: the source row accumulates in
        # place and its whole total must not migrate to a later day.
        self.assertEqual(go['ts'], 1789000000)
        self.assertEqual(go['modelCalls'], 4)
        self.assertEqual(records[1]['row']['provider'], 'ollama-cloud')
        self.assertEqual(records[1]['task'], 'background review')

    def test_hermes_accumulating_row_updates_instead_of_double_counting(self):
        path = self.hermes_ledger([{'input_tokens': 1000, 'output_tokens': 200}])
        ledger = c.Ledger(self.root / 'hermes.sqlite')
        for item in c.hermes_records(path): ledger.put(item['row'], path, growing=True)
        # The agent accumulates in place, so the same key now carries more.
        db = sqlite3.connect(path)
        db.execute('UPDATE session_model_usage SET input_tokens=4000, output_tokens=500, api_call_count=9, last_seen=1789000900.0')
        db.commit(); db.close()
        for item in c.hermes_records(path): ledger.put(item['row'], path, growing=True)
        # Counters grow; the timestamp stays at the first sighting so a row's
        # whole total cannot migrate to a later day and rewrite daily history.
        self.assertEqual(ledger.db.execute('SELECT COUNT(*),SUM(input),SUM(output),MAX(ts),MAX(modelCalls) FROM events').fetchone(),
                         (1, 4000, 500, 1789000000, 9))
        ledger.db.close()

    def test_hermes_and_opencode_go_both_count_without_overlap(self):
        # No OpenCode transcript exists for the Hermes session, and no Hermes
        # row exists for the OpenCode session, so the two add up.
        path = self.hermes_ledger([{'session_id': '20260901_120000_a1b2c3', 'input_tokens': 100,
                                    'output_tokens': 10, 'reasoning_tokens': 0, 'cache_read_tokens': 0}],
                                  sessions=[('20260901_120000_a1b2c3', '/home/user')])
        ledger = c.Ledger(self.root / 'both.sqlite')
        ledger.put({**c.record('opencode-session-event', 'opencode-go', 'ses_abc', 1789000000, 'deepseek-v4.1-flash',
                               '/home/user', 'OpenCode', input=700, output=70), 'apiProvider': 'opencode-go'})
        for item in c.hermes_records(path): ledger.put(item['row'], path)
        self.assertEqual(ledger.db.execute("SELECT COUNT(*),SUM(input),SUM(output) FROM events WHERE provider='opencode-go'").fetchone(),
                         (2, 800, 80))
        ledger.db.close()

    def test_hermes_source_without_the_usage_table_degrades(self):
        path = self.root / '.hermes/state.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)')
        db.commit(); db.close()
        ledger = c.Ledger(self.root / 'degrade.sqlite')
        with patch.object(c, 'HOME', self.root), patch.dict('os.environ', {
                'CODEX_HOME': str(self.root / 'codex'), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
                'GROK_HOME': str(self.root / 'grok'), 'PI_CODING_AGENT_DIR': str(self.root / 'pi'),
                'XDG_DATA_HOME': str(self.root / 'data')}):
            meta = ledger.scan(c.DEFAULTS)
        hermes = next(s for s in meta['sources'] if s['provider'] == 'hermes')
        self.assertEqual((hermes['status'], hermes['readErrors']), ('partial', 1))
        ledger.db.close()

    def test_hermes_source_is_scanned_and_reported_once(self):
        self.hermes_ledger([{'session_id': 's1', 'input_tokens': 500, 'output_tokens': 50}],
                           name='.hermes/state.db', sessions=[('s1', '/home/user')])
        ledger = c.Ledger(self.root / 'scan.sqlite')
        with patch.object(c, 'HOME', self.root), patch.dict('os.environ', {
                'CODEX_HOME': str(self.root / 'codex'), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
                'GROK_HOME': str(self.root / 'grok'), 'PI_CODING_AGENT_DIR': str(self.root / 'pi'),
                'XDG_DATA_HOME': str(self.root / 'data')}):
            meta = ledger.scan(c.DEFAULTS)
            meta = ledger.scan(c.DEFAULTS)
        self.assertEqual(meta['sources'][[s['provider'] for s in meta['sources']].index('hermes')]['status'], 'available')
        self.assertEqual(ledger.db.execute("SELECT COUNT(*),SUM(input) FROM events WHERE provider='opencode-go'").fetchone(), (1, 500))
        ledger.db.close()

    def ollama_urlopen(self, payload):
        import io
        def fake(request, timeout=12):
            assert request.full_url == 'https://ollama.com/api/usage', request.full_url
            assert request.get_header('Authorization') == 'Bearer test-ollama-key', request.get_header('Authorization')
            return io.BytesIO(json.dumps(payload).encode())
        return fake

    def credential_quota_response(self, request, timeout=12):
        payloads = {
            'https://ollama.com/api/usage': {'limits': {'weekly': {'usage': .2}}},
            'https://api.commandcode.ai/alpha/billing/credits': {
                'windowLimits': {'weekly': {'used': 2, 'cap': 10}}},
            'https://api.commandcode.ai/alpha/billing/subscriptions': {'data': {}},
            'https://api.commandcode.ai/alpha/usage/summary': {},
            'https://api.cline.bot/api/v1/users/me/plan/usage-limits': {
                'limits': [{'type': 'weekly', 'percentUsed': 20}]},
            'https://api.cline.bot/api/v1/users/me/plan': {'plan': {}}}
        return io.BytesIO(json.dumps(payloads[request.full_url]).encode())

    def test_quota_fingerprints_keep_cache_hits_and_detect_changed_or_removed_keys(self):
        for provider in ('ollama', 'commandcode', 'clinepass'):
            with self.subTest(provider=provider), \
                 patch.object(c, provider + '_key', return_value='fixture-secret-one') as key, \
                 patch.object(c.urllib.request, 'urlopen', side_effect=self.credential_quota_response) as network:
                quota = getattr(c, provider + '_quota')
                first = quota()
                self.assertEqual(first['error'], '')
                self.assertNotIn('fixture-secret-one', json.dumps(first))
                fingerprint = first['keyVersion']['key']
                self.assertEqual(len(bytes.fromhex(fingerprint['salt'])), 16)
                self.assertEqual(len(bytes.fromhex(fingerprint['digest'])), 32)
                calls = network.call_count
                self.assertEqual(quota(), first)
                self.assertEqual(network.call_count, calls)
                key.return_value = 'fixture-secret-two'
                changed = quota()
                self.assertNotIn('fixture-secret-two', json.dumps(changed))
                self.assertGreater(network.call_count, calls)
                self.assertNotEqual(changed['keyVersion'], first['keyVersion'])
                calls = network.call_count
                quota(force=True)
                self.assertGreater(network.call_count, calls)
                key.return_value = ''
                calls = network.call_count
                removed = quota()
                self.assertIsNone(removed['keyVersion']['key'])
                self.assertTrue(removed['error'])
                self.assertEqual(network.call_count, calls)
                raw = (c.STATE / (provider + '-quota.json')).read_text()
                self.assertNotIn('fixture-secret-one', raw)
                self.assertNotIn('fixture-secret-two', raw)

    def test_quota_legacy_fingerprints_migrate_once_despite_recent_attempt(self):
        for provider in ('ollama', 'commandcode', 'clinepass'):
            with self.subTest(provider=provider), \
                 patch.object(c, provider + '_key', return_value='fixture-secret'), \
                 patch.object(c.urllib.request, 'urlopen', side_effect=self.credential_quota_response) as network:
                path = c.STATE / (provider + '-quota.json')
                c.atomic_json(path, {'attemptedAt': time.time(),
                                     'keyVersion': {'file': None, 'key': '0123456789abcdef'}})
                quota = getattr(c, provider + '_quota')
                migrated = quota()
                self.assertEqual(migrated['error'], '')
                self.assertGreater(network.call_count, 0)
                calls = network.call_count
                self.assertEqual(quota(), migrated)
                self.assertEqual(network.call_count, calls)
                self.assertEqual(json.loads(path.read_text())['keyVersion'], migrated['keyVersion'])

    def test_quota_fingerprints_use_independent_salts_and_fixed_work_factor(self):
        with patch.object(c.hashlib, 'pbkdf2_hmac', wraps=c.hashlib.pbkdf2_hmac) as derive:
            one = c.quota_key_version('fixture-secret', self.root / 'one.key', {})
            two = c.quota_key_version('fixture-secret', self.root / 'two.key', {})
        self.assertNotEqual(one['key']['salt'], two['key']['salt'])
        self.assertNotEqual(one['key']['digest'], two['key']['digest'])
        self.assertTrue(all(call.args[3] >= 600_000 for call in derive.call_args_list))

    def test_quota_invalid_salts_refresh_without_accepting_cache_work_factor(self):
        for salt in (None, 'z' * 32, '00', ['00' * 16]):
            with self.subTest(salt=salt), \
                 patch.object(c, 'ollama_key', return_value='fixture-secret'), \
                 patch.object(c.urllib.request, 'urlopen', side_effect=self.credential_quota_response) as network:
                c.atomic_json(c.STATE / 'ollama-quota.json', {
                    'attemptedAt': time.time(), 'keyVersion': {'file': None, 'key': {
                        'scheme': 'pbkdf2-sha256-v1', 'salt': salt, 'digest': 'old', 'iterations': 1}}})
                result = c.ollama_quota()
                self.assertEqual(result['error'], '')
                self.assertEqual(network.call_count, 1)
                self.assertEqual(len(bytes.fromhex(result['keyVersion']['key']['salt'])), 16)

    def test_quota_key_file_change_invalidates_the_cache(self):
        path = self.root / 'fixture.key'
        path.write_text('fixture-secret')
        one = c.quota_key_version('fixture-secret', path, {})
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
        two = c.quota_key_version('fixture-secret', path, {'keyVersion': one})
        self.assertNotEqual(one, two)
        self.assertEqual(one['key'], two['key'])

    def test_ollama_quota_maps_whatever_windows_the_plan_reports(self):
        payload = {'limits': {'session': {'usage': 0.046, 'models': [{'name': 'glm-5.2', 'request_count': 34}]},
                              'weekly': {'usage': 0.051, 'models': [{'name': 'glm-5.2', 'request_count': 254}]}}}
        with patch.dict('os.environ', {'OLLAMA_API_KEY': 'test-ollama-key', 'XDG_CONFIG_HOME': str(self.root / 'config'),
                'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c.urllib.request, 'urlopen', side_effect=self.ollama_urlopen(payload)):
            quota = c.ollama_quota(True)
            self.assertEqual(c.quota('ollama-cloud')['limits'][0]['label'], 'Session (5-hour)')
        self.assertEqual(quota['error'], '')
        by_label = {w['label']: w for w in quota['limits']}
        self.assertAlmostEqual(by_label['Session (5-hour)']['percent'], .046)
        self.assertAlmostEqual(by_label['Weekly (7-day)']['percent'], .051)
        # The endpoint carries no reset time, so no window invents one.
        self.assertNotIn('resetsAt', by_label['Weekly (7-day)'])

    def test_ollama_quota_accepts_a_monthly_credit_plan(self):
        payload = {'limits': {'monthly': {'usage': 0.0, 'models': [{'name': 'deepseek-v4.1-flash', 'request_count': 18}]}}}
        with patch.dict('os.environ', {'OLLAMA_API_KEY': 'test-ollama-key', 'XDG_CONFIG_HOME': str(self.root / 'config'),
                'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c.urllib.request, 'urlopen', side_effect=self.ollama_urlopen(payload)):
            quota = c.ollama_quota(True)
        self.assertEqual(quota['error'], '')
        self.assertEqual([w['label'] for w in quota['limits']], ['Monthly'])
        self.assertAlmostEqual(quota['limits'][0]['percent'], 0.0)
        # The endpoint's per-model request counts are not a share of the plan,
        # so nothing is fabricated into the window; per-model detail comes from
        # the local ledger instead.
        self.assertNotIn('note', quota['limits'][0])

    def test_cards_carry_per_model_rows_from_the_local_ledger(self):
        ledger = c.Ledger(self.root / 'models.sqlite')
        ledger.put(c.record('a', 'ollama-cloud', 's', 1789000000, 'glm-5.2', '/p', 'Hermes',
                            input=1000, output=100))
        ledger.put(c.record('b', 'ollama-cloud', 's', 1789000000, 'kimi-k3', '/p', 'Hermes',
                            input=10, output=1))
        with patch.object(c, 'quota', return_value={'limits': []}), patch.object(c, 'account_quotas', return_value=({}, {})), \
             patch.object(c, 'theme', return_value={}):
            data = c.report(ledger, c.DEFAULTS | {'enabled': ['ollama-cloud']}, days=365,
                            now=dt.datetime(2026, 9, 16, 12).astimezone())
        card = next(x for x in data['cards'] if x['provider'] == 'ollama-cloud')
        self.assertEqual([m['model'] for m in card['models']], ['glm-5.2', 'kimi-k3'])
        self.assertEqual(card['models'][0]['tokens'], 1100)
        self.assertGreater(card['models'][0]['value'], 0)
        ledger.db.close()

    def test_cards_are_ordered_by_tokens_across_providers(self):
        ledger = c.Ledger(self.root / 'order.sqlite')
        self.addCleanup(ledger.db.close)
        for provider, tokens in (('ollama-cloud', 10), ('opencode-go', 500), ('codex', 2000)):
            ledger.put(c.record(provider, provider, 's', 1789000000, 'glm-5.2', '/p', 'Hermes', input=tokens))
        with patch.object(c, 'quota', return_value={'limits': []}), patch.object(c, 'account_quotas', return_value=({}, {})), \
             patch.object(c, 'theme', return_value={}):
            data = c.report(ledger, c.DEFAULTS | {'enabled': ['ollama-cloud', 'opencode-go', 'codex']}, days=365,
                            now=dt.datetime(2026, 9, 16, 12).astimezone())
        self.assertEqual([card['provider'] for card in data['cards']], ['codex', 'opencode-go', 'ollama-cloud'])

    def model_family_ledger(self):
        """One model recorded the way four routes each name it, plus a second
        model that must stay its own row."""
        ledger = c.Ledger(self.root / 'families.sqlite')
        self.addCleanup(ledger.db.close)
        when = 1789000000
        families = {'opencode-go': 'deepseek-v4.1-flash', 'ollama-cloud': 'deepseek-v4.1-flash',
                    'commandcode': 'deepseek/deepseek-v4.1-flash', 'clinepass': 'cline-pass/deepseek-v4.1-flash'}
        for provider, model in families.items():
            ledger.put(c.record(provider, provider, provider + '-session', when, model, '/p', 'Hermes',
                                input=1000, output=100, cacheRead=5000))
        ledger.put(c.record('glm', 'ollama-cloud', 'ollama-session', when, 'glm-5.2', '/p', 'Hermes', input=1000, output=100))
        return ledger, families

    def test_models_breakdown_counts_one_model_across_routes(self):
        # Four routes record the same model under the string their own API
        # returns. "How much went through this model" is one answer, so the
        # Models table is one row that still carries the split by route.
        ledger, families = self.model_family_ledger()
        cfg = c.DEFAULTS | {'enabled': list(families) + ['ollama-cloud']}
        now = dt.datetime(2026, 9, 16, 12).astimezone()
        with patch.object(c, 'quota', return_value={'limits': []}), \
             patch.object(c, 'account_quotas', return_value=({}, {})), patch.object(c, 'theme', return_value={}):
            data = c.report(ledger, cfg, days=365, now=now)
            scoped = [c.report(ledger, cfg, days=365, provider=p, now=now) for p in families]
        merged = next(row for row in data['models'] if row['name'] == 'deepseek-v4.1-flash')
        self.assertEqual([row['name'] for row in data['models']], ['deepseek-v4.1-flash', 'glm-5.2'])
        self.assertEqual(merged['tokens'], 4 * 6100)
        # Each route reports the string it recorded, with its own figure.
        self.assertEqual({route['provider']: route['model'] for route in merged['routes']}, families)
        self.assertEqual([route['tokens'] for route in merged['routes']], [6100] * 4)
        self.assertEqual([route['sessions'] for route in merged['routes']], [1] * 4)
        # Grouping must not re-price anything. The grouped value is what each
        # route charged its own tokens at, so it equals the sum of the four
        # route-scoped reports, and it is not one route's rates applied to all
        # of the traffic (ClinePass bills this model at its own table).
        per_route = [next(row for row in scoped_report['models'] if row['name'] == 'deepseek-v4.1-flash')['value']
                     for scoped_report in scoped]
        self.assertAlmostEqual(merged['value'], sum(per_route), places=9)
        self.assertNotAlmostEqual(merged['value'], 4 * per_route[0], places=6)
        self.assertEqual(data['models'][0]['provider'], 'opencode-go')

    def test_muse_model_groups_opencode_hermes_and_commandcode_spellings(self):
        ledger = c.Ledger(self.root / 'muse-family.sqlite')
        self.addCleanup(ledger.db.close)
        ledger.put(c.opencode_record('open-message', 'open-session', 1789000000, '/p',
                                     'muse-spark-1.3-contributor', 'opencode-go',
                                     {'input': 100, 'output': 20, 'reasoning': 5,
                                      'cache': {'read': 70, 'write': 0}}, 0))

        hermes = self.hermes_ledger([{
            'session_id': 'hermes-session', 'model': 'muse-spark-1.3-contributor',
            'billing_provider': 'opencode-go', 'input_tokens': 1000,
            'output_tokens': 200, 'cache_read_tokens': 5000,
        }], sessions=[('hermes-session', '/p')])
        for item in c.hermes_records(hermes):
            ledger.put(item['row'], hermes)

        commandcode = self.transcript('commandcode/muse.jsonl', [
            self.commandcode_event(model='meta/muse-spark-1.3-contributor',
                                   ts='2026-09-15T12:00:00Z')])
        for item in c.commandcode_records(commandcode):
            ledger.put(item, commandcode)

        cfg = c.DEFAULTS | {'enabled': ['opencode-go', 'commandcode']}
        now = dt.datetime(2026, 9, 16, 12).astimezone()
        with patch.object(c, 'quota', return_value={'limits': []}), \
             patch.object(c, 'account_quotas', return_value=({}, {})), \
             patch.object(c, 'theme', return_value={}):
            data = c.report(ledger, cfg, days=365, now=now)
            prefixed = c.report(ledger, cfg, days=365, now=now,
                                selection={'model': 'meta/muse-spark-1.3-contributor'})

        self.assertEqual(c.model_family('meta/muse-spark-1.3-contributor'),
                         'muse-spark-1.3-contributor')
        self.assertEqual(c.model_family('muse-spark-1.2-contributor'),
                         'muse-spark-1.2-contributor')
        self.assertEqual([row['name'] for row in data['models']],
                         ['muse-spark-1.3-contributor'])
        muse = data['models'][0]
        self.assertEqual(muse['tokens'], 6590)
        self.assertEqual({row['provider']: (row['model'], row['tokens'])
                          for row in muse['routes']}, {
            'opencode-go': ('muse-spark-1.3-contributor', 6395),
            'commandcode': ('meta/muse-spark-1.3-contributor', 195),
        })
        self.assertEqual(prefixed['summary']['tokens'], 6590)

    def test_a_model_selection_matches_every_route_spelling(self):
        ledger, families = self.model_family_ledger()
        cfg = c.DEFAULTS | {'enabled': list(families)}
        now = dt.datetime(2026, 9, 16, 12).astimezone()
        with patch.object(c, 'quota', return_value={'limits': []}), \
             patch.object(c, 'account_quotas', return_value=({}, {})), patch.object(c, 'theme', return_value={}):
            by_name = c.report(ledger, cfg, days=365, now=now, selection={'model': 'deepseek-v4.1-flash'})
            by_spelling = c.report(ledger, cfg, days=365, now=now, selection={'model': 'cline-pass/deepseek-v4.1-flash'})
            sibling = c.report(ledger, cfg, days=365, now=now, selection={'model': 'deepseek-v4-flash'})
        self.assertEqual(by_name['summary']['tokens'], 4 * 6100)
        self.assertEqual(by_spelling['summary']['tokens'], by_name['summary']['tokens'])
        # A family is not a prefix match: the neighbouring model is a different
        # model, and a name nothing recorded selects nothing.
        self.assertEqual(sibling['summary']['tokens'], 0)
        # The per-card model rows read the same selection, so a filtered view
        # cannot show a populated table beside a card with no models.
        card = next(card for card in by_name['cards'] if card['provider'] == 'clinepass')
        self.assertEqual([row['model'] for row in card['models']], ['cline-pass/deepseek-v4.1-flash'])

    def test_unpriced_coverage_stays_per_route_and_spelling(self):
        # The table groups one model's spellings, but pricing coverage names the
        # route and the exact string, because that pair identifies the rate table
        # that has not caught up.
        ledger = c.Ledger(self.root / 'unpriced.sqlite')
        self.addCleanup(ledger.db.close)
        for provider in ('opencode-go', 'ollama-cloud'):
            ledger.put(c.record(provider, provider, provider + '-session', 1789000000, 'mystery-model', '/p', 'Hermes',
                                input=1000, output=100))
        cfg = c.DEFAULTS | {'enabled': ['opencode-go', 'ollama-cloud']}
        with patch.object(c, 'quota', return_value={'limits': []}), \
             patch.object(c, 'account_quotas', return_value=({}, {})), patch.object(c, 'theme', return_value={}):
            data = c.report(ledger, cfg, days=365, now=dt.datetime(2026, 9, 16, 12).astimezone())
        self.assertEqual([row['provider'] for row in data['pricing']['unpriced']], ['opencode-go', 'ollama-cloud'])
        self.assertEqual({row['name'] for row in data['pricing']['unpriced']}, {'mystery-model'})
        self.assertEqual([row['unpricedTokens'] for row in data['pricing']['unpriced']], [1100, 1100])
        # The table still answers the grouped question.
        self.assertEqual([(row['name'], row['tokens']) for row in data['models']], [('mystery-model', 2200)])
        self.assertEqual(data['unknownModels'], ['mystery-model'])

    def test_model_options_list_every_model_in_the_period(self):
        # The filter's own list must not narrow to the current selection, or a
        # reader could not switch models without clearing the filter first.
        ledger, families = self.model_family_ledger()
        cfg = c.DEFAULTS | {'enabled': list(families)}
        now = dt.datetime(2026, 9, 16, 12).astimezone()
        with patch.object(c, 'quota', return_value={'limits': []}), \
             patch.object(c, 'account_quotas', return_value=({}, {})), patch.object(c, 'theme', return_value={}):
            plain = c.report(ledger, cfg, days=365, now=now)
            filtered = c.report(ledger, cfg, days=365, now=now, selection={'model': 'deepseek-v4.1-flash'})
            route = c.report(ledger, cfg, days=365, provider='ollama-cloud', now=now)
            short = c.report(ledger, cfg, days=1, now=now)
        self.assertEqual([(o['name'], o['tokens']) for o in plain['modelOptions']],
                         [('deepseek-v4.1-flash', 4 * 6100), ('glm-5.2', 1100)])
        # Four route spellings of one model are a single option, like the table.
        self.assertEqual([o['name'] for o in filtered['modelOptions']], [o['name'] for o in plain['modelOptions']])
        # Scoped to the route tab, and to the period on screen.
        self.assertEqual([o['name'] for o in route['modelOptions']], ['deepseek-v4.1-flash', 'glm-5.2'])
        self.assertEqual(short['modelOptions'], [])

    def test_go_allowance_rows_follow_the_model_filter(self):
        # The allowance card must not list models the rest of the page excludes.
        ledger = c.Ledger(self.root / 'allowance-filter.sqlite')
        self.addCleanup(ledger.db.close)
        ledger.put(c.opencode_record('m1', 's', '2026-09-14T12:00:00Z', '/p', 'deepseek-v4.1-flash', 'opencode-go', {'input': 1_000_000}, 0))
        ledger.put(c.opencode_record('m2', 's', '2026-09-14T12:00:00Z', '/p', 'glm-5.3-flash', 'opencode-go', {'input': 1_000_000}, 0))
        rates = {'source': 'test', 'document': {
            'opencode-go/deepseek-v4.1-flash': {'input_cost_per_token': .000001, 'output_cost_per_token': 0,
                                                'cache_read_input_token_cost': 0, 'monthly_limit_usd': 15},
            'opencode-go/glm-5.3-flash': {'input_cost_per_token': .000002, 'output_cost_per_token': 0,
                                          'cache_read_input_token_cost': 0, 'monthly_limit_usd': 60}}}
        cfg = c.DEFAULTS | {'enabled': ['opencode-go']}
        with patch.object(c, 'load_rates', return_value=rates), patch.object(c, 'theme', return_value={}), \
             patch.object(c, 'quota', return_value={'limits': [{'label': 'Monthly', 'resetsAt': '2026-10-01T00:00:00+00:00'}]}):
            plain = c.report(ledger, cfg, 7, now=dt.datetime(2026, 9, 15).astimezone())
            filtered = c.report(ledger, cfg, 7, now=dt.datetime(2026, 9, 15).astimezone(),
                                selection={'model': 'deepseek-v4.1-flash'})
        self.assertEqual(sorted(row['model'] for row in plain['goAllowance']['models']),
                         ['deepseek-v4.1-flash', 'glm-5.3-flash'])
        self.assertEqual([row['model'] for row in filtered['goAllowance']['models']], ['deepseek-v4.1-flash'])

    def test_excluded_sources_leave_every_surface(self):
        # Leaving a source out of a view must remove it everywhere at once: the
        # summary, the cards, the breakdowns, and the model filter's own list.
        # Anything left behind contradicts the page it sits on.
        ledger, families = self.model_family_ledger()
        ledger.put(c.record('gpt', 'codex', 'codex-session', 1789000000, 'gpt-6-astra', '/p', 'CLI', input=5000, output=500))
        cfg = c.DEFAULTS | {'enabled': list(families) + ['codex']}
        now = dt.datetime(2026, 9, 16, 12).astimezone()
        with patch.object(c, 'quota', return_value={'limits': []}), \
             patch.object(c, 'account_quotas', return_value=({}, {})), patch.object(c, 'theme', return_value={}):
            plain = c.report(ledger, cfg, days=365, now=now)
            without = c.report(ledger, cfg, days=365, now=now, selection={'excludeSource': ['codex']})
            both_ways = c.report(ledger, cfg, days=365, now=now, selection={'excludeSource': ['codex', 'codex']})
        self.assertEqual([row['name'] for row in plain['models']], ['deepseek-v4.1-flash', 'gpt-6-astra', 'glm-5.2'])
        self.assertEqual([row['name'] for row in without['models']], ['deepseek-v4.1-flash', 'glm-5.2'])
        self.assertEqual(without['summary']['tokens'], plain['summary']['tokens'] - 5500)
        self.assertNotIn('codex', {route['provider'] for row in without['models'] for route in row['routes']})
        self.assertNotIn('codex', {row['provider'] for row in without['accounts']})
        self.assertNotIn('codex', {card['provider'] for card in without['cards']})
        self.assertNotIn('gpt-6-astra', [option['name'] for option in without['modelOptions']])
        self.assertEqual(sum(row['tokens'] for row in without['models']), without['summary']['tokens'])
        # Naming the same source twice is the same view, not a different one.
        self.assertEqual(both_ways['summary']['tokens'], without['summary']['tokens'])

    def test_ollama_key_precedence_and_no_key_message(self):
        config = self.root / 'config/omarchy/ai-usage'
        config.mkdir(parents=True, exist_ok=True)
        # Every credential source must be inside the fixture: a real
        # OLLAMA_API_KEY or an Ollama CLI key file on the machine would
        # otherwise decide this test's outcome.
        with patch.dict('os.environ', {'XDG_CONFIG_HOME': str(self.root / 'config'),
                                       'XDG_DATA_HOME': str(self.root / 'data'), 'OLLAMA_API_KEY': ''}), \
             patch.object(c.urllib.request, 'urlopen') as request:
            c.atomic_json(c.CONFIG, c.DEFAULTS)
            quota = c.ollama_quota(True)
            self.assertIn('Settings', quota['error'])
            request.assert_not_called()
            (config / 'ollama.key').write_text('file-key\n')
            self.assertEqual(c.ollama_key(), 'file-key')
            with patch.dict('os.environ', {'OLLAMA_API_KEY': 'env-key'}): self.assertEqual(c.ollama_key(), 'env-key')
            self.assertEqual(c.ollama_key(c.DEFAULTS | {'ollamaApiKey': 'typed-key'}), 'typed-key')

    def test_ollama_quota_errors_do_not_expose_the_key(self):
        import io
        def failing(request, timeout=12):
            raise c.urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'bad test-ollama-key'))
        with patch.dict('os.environ', {'OLLAMA_API_KEY': 'test-ollama-key', 'XDG_CONFIG_HOME': str(self.root / 'config'),
                'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c.urllib.request, 'urlopen', side_effect=failing):
            quota = c.ollama_quota(True)
        self.assertNotIn('test-ollama-key', json.dumps(quota))
        self.assertNotIn('test-ollama-key', (c.STATE / 'ollama-quota.json').read_text())

    def test_ollama_rates_price_cloud_models_and_double_at_peak(self):
        rates = c.load_rates()
        # Off-peak Tuesday 09:00 UTC.
        off = c.record('off', 'ollama-cloud', 's', dt.datetime(2026, 9, 15, 9, tzinfo=dt.timezone.utc).timestamp(),
                       'deepseek-v4.1-flash', '', 'Hermes', input=1_000_000, output=1_000_000, cacheRead=1_000_000)
        # Peak Tuesday 13:00 UTC.
        peak = c.record('peak', 'ollama-cloud', 's', dt.datetime(2026, 9, 15, 13, tzinfo=dt.timezone.utc).timestamp(),
                        'deepseek-v4.1-flash', '', 'Hermes', input=1_000_000, output=1_000_000, cacheRead=1_000_000)
        self.assertAlmostEqual(c.price(off, rates['document'])[0], 0.15 + 0.60 + 0.003, places=6)
        self.assertAlmostEqual(c.price(peak, rates['document'])[0], 0.30 + 1.20 + 0.006, places=6)
        # A model the pricing table lists without a cached-input rate stays
        # unpriced rather than assuming free caching.
        partial = c.record('partial', 'ollama-cloud', 's', off['ts'], 'qwen3.5:397b', '', 'Hermes',
                           input=100, output=10, cacheRead=50)
        self.assertIsNone(c.price(partial, rates['document'])[0])
        self.assertIn('Ollama Cloud model rates', rates['source'])

    def test_ollama_rates_never_price_another_route(self):
        rates = c.load_rates()
        # The same model name on the OpenCode Go route must not pick up the
        # cloud table, and vice versa.
        go = c.record('go', 'opencode-go', 's', 1789000000, 'glm-5.3-flash', '', 'OpenCode',
                      input=1_000_000, output=1_000_000)
        self.assertAlmostEqual(c.price(go, rates['document'])[0], 0.15 + 0.50, places=6)
        self.assertIsNone(rates['document'].get('glm-5.3-flash'))

    def test_hermes_rows_distinguish_source_primary_keys(self):
        # The source table's primary key includes the billing route spelling, so
        # two rows differing only there are distinct and must not collapse into
        # one ledger event (which MAX would silently under-count).
        path = self.hermes_ledger([
            {'billing_base_url': 'https://opencode.ai/zen/go/v1', 'input_tokens': 500},
            {'billing_base_url': 'https://opencode.ai/zen/go/v1/', 'input_tokens': 500}])
        ledger = c.Ledger(self.root / 'pk.sqlite')
        for item in c.hermes_records(path): ledger.put(item['row'], path, growing=True)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*),SUM(input) FROM events').fetchone(), (2, 1000))
        ledger.db.close()

    def test_commandcode_wire_variants_collapse_onto_one_provider(self):
        # Hermes has two profiles for the same product (OpenAI-shaped and
        # Anthropic-shaped) with the same key and account. One account must not
        # become two providers.
        path = self.hermes_ledger([
            {'billing_provider': 'commandcode', 'model': 'deepseek/deepseek-v4-pro',
             'billing_base_url': 'https://api.commandcode.ai/provider/v1', 'input_tokens': 1000, 'output_tokens': 100},
            {'billing_provider': 'commandcode-anthropic', 'model': 'claude-sonnet-4-6',
             'billing_base_url': 'https://api.commandcode.ai/provider/v1', 'input_tokens': 500, 'output_tokens': 50}])
        rows = [item['row'] for item in c.hermes_records(path)]
        self.assertEqual({r['provider'] for r in rows}, {'commandcode'})
        # The raw route survives on apiProvider for the Routes breakdown.
        self.assertEqual({r['apiProvider'] for r in rows}, {'commandcode', 'commandcode-anthropic'})
        # Distinct models stay distinct rows.
        self.assertEqual(len(rows), 2)

    def test_commandcode_flashx_uses_its_published_rates(self):
        rates = c.load_rates()
        r = c.record('flashx', 'commandcode', 's', 1789000000, 'z-ai/glm-5.3-flashx', '', 'T3 Code',
                     input=1_000_000, output=1_000_000, cacheRead=1_000_000)
        self.assertAlmostEqual(c.price(r, rates['document'])[0], 0.37 + 0.59 + 0.1635, places=6)

    def test_commandcode_quota_reads_its_windows_and_resets(self):
        # The endpoint reports money-valued windows with a reset each; the
        # monthly cap is only knowable as remaining credits plus what the
        # period spent, so the two responses are combined.
        payloads = {
            '/alpha/billing/credits': {'credits': {'monthlyCredits': 69.5},
                                       'windowLimits': {'fiveHour': {'used': 3.5, 'cap': 14, 'resetAt': 1789666587497},
                                                        'weekly': {'used': 3.5, 'cap': 35, 'resetAt': 1790253387497}}},
            '/alpha/billing/subscriptions': {'data': {'planId': 'individual-goat',
                                                      'currentPeriodEnd': '2026-10-17T12:08:37.000Z'}},
            '/alpha/usage/summary': {'totalCredits': 0.5}}
        with patch.dict('os.environ', {'COMMANDCODE_API_KEY': 'test-cc-key',
                                       'XDG_CONFIG_HOME': str(self.root / 'config'),
                                       'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c, 'commandcode_call', side_effect=lambda key, path: payloads[path]):
            result = c.commandcode_quota(force=True)
        self.assertEqual(result['error'], '')
        self.assertEqual(result['plan'], 'GOAT')
        by = {limit['label']: limit for limit in result['limits']}
        self.assertAlmostEqual(by['5 hours']['percent'], 0.25, places=6)
        self.assertAlmostEqual(by['Weekly']['percent'], 0.1, places=6)
        # 0.5 spent of a 70 allowance (69.5 remaining + 0.5 spent).
        self.assertAlmostEqual(by['Monthly']['percent'], 0.5 / 70, places=6)
        # A reset time is rendered as ISO, which is what the panel reads.
        self.assertTrue(by['5 hours']['resetsAt'].startswith('2026-09-'))
        self.assertTrue(by['Monthly']['resetsAt'].startswith('2026-10-17'))

    def test_commandcode_quota_survives_a_missing_subscription(self):
        credits = {'credits': {'monthlyCredits': 70.0},
                   'windowLimits': {'fiveHour': {'used': 0.0, 'cap': 14, 'resetAt': 1789666587497}}}
        def call(key, path):
            if path == '/alpha/billing/credits': return credits
            if path == '/alpha/usage/summary': return {'totalCredits': 0.0}
            raise ValueError('unavailable')
        with patch.dict('os.environ', {'COMMANDCODE_API_KEY': 'test-cc-key',
                                       'XDG_CONFIG_HOME': str(self.root / 'config'),
                                       'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c, 'commandcode_call', side_effect=call):
            result = c.commandcode_quota(force=True)
        # The windows still render even when the plan lookup fails.
        self.assertEqual(result['error'], '')
        self.assertEqual([limit['label'] for limit in result['limits']], ['5 hours', 'Monthly'])
        self.assertEqual(result['plan'], '')

    def test_commandcode_extra_credits_ride_along_as_a_balance(self):
        # The plan's credits run out before the top-up wallet does, so the
        # wallet is a balance rather than a window: what is left comes from the
        # endpoint, what it was funded with is that plus what this period drew
        # from it, and the card is told the funded figure is derived.
        payloads = {
            '/alpha/billing/credits': {'credits': {'monthlyCredits': 12.0, 'purchasedCredits': 8.4634954014},
                                       'windowLimits': {'weekly': {'used': 40.0, 'cap': 35, 'resetAt': 1790253387497}}},
            '/alpha/billing/subscriptions': {'data': {'planId': 'individual-goat',
                                                      'currentPeriodEnd': '2026-10-17T12:08:37.000Z'}},
            '/alpha/usage/summary': {'totalCredits': 23.0, 'totalPurchasedCredits': 1.533636676}}
        with patch.dict('os.environ', {'COMMANDCODE_API_KEY': 'test-cc-key',
                                       'XDG_CONFIG_HOME': str(self.root / 'config'),
                                       'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c, 'commandcode_call', side_effect=lambda key, path: payloads[path]):
            result = c.commandcode_quota(force=True)
        self.assertEqual(result['error'], '')
        balance = result['balance']
        self.assertEqual(balance['label'], 'Extra credits')
        self.assertAlmostEqual(balance['remaining'], 8.4634954014, places=6)
        self.assertAlmostEqual(balance['spent'], 1.533636676, places=6)
        # Money left plus the period's draw on the wallet is the only funded
        # figure either endpoint offers.
        self.assertAlmostEqual(balance['funded'], 9.9971320774, places=6)
        self.assertEqual(balance['currency'], 'USD')
        self.assertTrue(balance['estimated'])
        # The card reads the same file back through the quota dispatcher.
        self.assertAlmostEqual(c.quota('commandcode')['balance']['remaining'], 8.4634954014, places=6)

    def test_commandcode_empty_wallet_stays_off_the_card(self):
        # A plan nobody has topped up reports purchasedCredits: 0, and a $0.00
        # wallet on every refresh would be noise rather than information. A
        # residue below the cent the card prints is that same $0.00, so it is
        # treated the same way.
        for leftover in (0.0, 0.004):
            payloads = {
                '/alpha/billing/credits': {'credits': {'monthlyCredits': 70.0, 'purchasedCredits': leftover},
                                           'windowLimits': {'fiveHour': {'used': 1.0, 'cap': 14, 'resetAt': 1789666587497}}},
                '/alpha/billing/subscriptions': {'data': {'planId': 'individual-goat',
                                                          'currentPeriodEnd': '2026-10-17T12:08:37.000Z'}},
                '/alpha/usage/summary': {'totalCredits': 1.0, 'totalPurchasedCredits': 0.0}}
            with patch.dict('os.environ', {'COMMANDCODE_API_KEY': 'test-cc-key',
                                           'XDG_CONFIG_HOME': str(self.root / 'config'),
                                           'XDG_DATA_HOME': str(self.root / 'data')}), \
                 patch.object(c, 'commandcode_call', side_effect=lambda key, path: payloads[path]):
                result = c.commandcode_quota(force=True)
            self.assertNotIn('balance', result, leftover)
            self.assertEqual([limit['label'] for limit in result['limits']], ['5 hours', 'Monthly'])
        self.assertNotIn('balance', c.quota('commandcode'))

    def test_commandcode_wallet_outlives_a_plan_with_no_windows(self):
        # Extra credits are bought outright, so they survive the plan that
        # funded the windows: an account whose windows are gone still shows the
        # money it has left instead of an error and no card.
        payloads = {
            '/alpha/billing/credits': {'credits': {'purchasedCredits': 8.44}},
            '/alpha/billing/subscriptions': {'data': {'planId': 'individual-goat',
                                                      'currentPeriodEnd': '2026-10-17T12:08:37.000Z'}},
            '/alpha/usage/summary': {'totalCredits': 23.0, 'totalPurchasedCredits': 1.55}}
        with patch.dict('os.environ', {'COMMANDCODE_API_KEY': 'test-cc-key',
                                       'XDG_CONFIG_HOME': str(self.root / 'config'),
                                       'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c, 'commandcode_call', side_effect=lambda key, path: payloads[path]):
            result = c.commandcode_quota(force=True)
        self.assertEqual(result['error'], '')
        self.assertEqual(result['limits'], [])
        self.assertAlmostEqual(result['balance']['remaining'], 8.44, places=6)
        self.assertAlmostEqual(result['balance']['funded'], 9.99, places=6)
        # No windows and no wallet is still a broken read, not an empty card.
        self.assertIn('balance', c.quota('commandcode'))

    def test_commandcode_drained_wallet_replaces_a_stale_balance(self):
        # A drained wallet on a plan that reports no windows: the read itself
        # succeeds with nothing to show, so the snapshot is replaced and the
        # previous balance is gone. Leaving it behind an error would put money
        # on the card that is not there.
        good = {
            '/alpha/billing/credits': {'credits': {'purchasedCredits': 8.44}},
            '/alpha/billing/subscriptions': {'data': {'planId': 'individual-goat',
                                                      'currentPeriodEnd': '2026-10-17T12:08:37.000Z'}},
            '/alpha/usage/summary': {'totalCredits': 23.0, 'totalPurchasedCredits': 1.55}}
        drained = {
            '/alpha/billing/credits': {'credits': {'purchasedCredits': 0.0}},
            '/alpha/billing/subscriptions': {'data': {'planId': 'individual-goat',
                                                      'currentPeriodEnd': '2026-10-17T12:08:37.000Z'}},
            '/alpha/usage/summary': {'totalCredits': 0.0, 'totalPurchasedCredits': 0.0}}
        with patch.dict('os.environ', {'COMMANDCODE_API_KEY': 'test-cc-key',
                                       'XDG_CONFIG_HOME': str(self.root / 'config'),
                                       'XDG_DATA_HOME': str(self.root / 'data')}):
            with patch.object(c, 'commandcode_call', side_effect=lambda key, path: good[path]):
                c.commandcode_quota(force=True)
            self.assertIn('balance', c.quota('commandcode'))
            with patch.object(c, 'commandcode_call', side_effect=lambda key, path: drained[path]):
                result = c.commandcode_quota(force=True)
        self.assertEqual(result['error'], '')
        self.assertEqual(result['limits'], [])
        self.assertNotIn('balance', result)
        self.assertNotIn('balance', c.quota('commandcode'))

    def test_agent_record_carries_a_provider_balance(self):
        # The panel reads the agent record rather than the quota snapshot, so a
        # wallet has to land there too, and a provider without one keeps the
        # key out instead of carrying a null.
        ledger = c.Ledger(self.root / 'balance.sqlite')
        ledger.put(c.record('a', 'commandcode', 's', '2026-09-19T12:00:00Z', 'glm-5.3-flash', '', 'T3 Code', input=10))
        wallet = {'label': 'Extra credits', 'remaining': 8.47, 'spent': 1.53, 'funded': 10.0,
                  'currency': 'USD', 'estimated': True}
        with patch.object(c, 'quota', return_value={'limits': [], 'balance': wallet}):
            c.write_agent_record(ledger, 'commandcode')
        record = json.loads((c.STATE.parent / 'agents/usage/commandcode.json').read_text())
        self.assertEqual(record['balance'], wallet)
        with patch.object(c, 'quota', return_value={'limits': []}):
            c.write_agent_record(ledger, 'grok')
        self.assertNotIn('balance', json.loads((c.STATE.parent / 'agents/usage/grok.json').read_text()))
        ledger.db.close()

    def test_commandcode_quota_errors_do_not_expose_the_key(self):
        with patch.dict('os.environ', {'COMMANDCODE_API_KEY': 'sk-secret-cc-value',
                                       'XDG_CONFIG_HOME': str(self.root / 'config'),
                                       'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c.urllib.request, 'urlopen', side_effect=OSError('boom')):
            result = c.commandcode_quota(force=True)
        self.assertNotIn('sk-secret-cc-value', json.dumps(result))
        self.assertIn('unavailable', result['error'])

    def clinepass_urlopen(self, payloads):
        import io
        def fake(request, timeout=12):
            assert request.full_url.startswith('https://api.cline.bot/api/v1/'), request.full_url
            assert request.get_header('Authorization') == 'Bearer test-clinepass-key', request.get_header('Authorization')
            path = request.full_url.split('api.cline.bot', 1)[1]
            return io.BytesIO(json.dumps(payloads[path]).encode())
        return fake

    def clinepass_env(self):
        # A real CLINE_API_KEY, settings file, or key file on this machine would
        # otherwise decide the test's outcome, so every source points inside the
        # fixture.
        return {'CLINE_API_KEY': 'test-clinepass-key', 'XDG_CONFIG_HOME': str(self.root / 'config'),
                'XDG_DATA_HOME': str(self.root / 'data')}

    def test_clinepass_quota_reads_percent_windows_and_nanosecond_resets(self):
        # The endpoint wraps everything under "data", names its windows in
        # snake_case, reports a 0-100 percentage per window, and gives reset
        # times precise to the nanosecond.
        payloads = {
            '/api/v1/users/me/plan/usage-limits': {'data': {'limits': [
                {'type': 'five_hour', 'percentUsed': 12.5, 'resetsAt': '2026-09-18T09:06:34.503758991Z'},
                {'type': 'weekly', 'percentUsed': 0, 'resetsAt': '2026-09-25T04:06:34.506428852Z'},
                {'type': 'monthly', 'percentUsed': 3, 'resetsAt': '2026-10-18T04:06:34.509368183Z'}]},
                'success': True},
            '/api/v1/users/me/plan': {'data': {'plan': {'displayName': 'Cline Pass (Monthly)'}}, 'success': True}}
        with patch.dict('os.environ', self.clinepass_env()), \
             patch.object(c.urllib.request, 'urlopen', side_effect=self.clinepass_urlopen(payloads)):
            quota = c.clinepass_quota(True)
            # The card reads the same file back through the quota dispatcher,
            # plan name included.
            self.assertEqual(c.quota('clinepass')['plan'], 'Cline Pass (Monthly)')
        self.assertEqual(quota['error'], '')
        self.assertEqual([w['label'] for w in quota['limits']], ['5 hours', 'Weekly', 'Monthly'])
        by_label = {w['label']: w for w in quota['limits']}
        # percentUsed is a 0-100 percentage; the cache holds the 0-1 fraction
        # every other provider writes and other local tools read.
        self.assertAlmostEqual(by_label['5 hours']['percent'], 0.125, places=6)
        self.assertAlmostEqual(by_label['Monthly']['percent'], 0.03, places=6)
        # The reset keeps its instant, in the ISO shape the panel's countdown
        # reads, and the nanosecond precision survives the round trip.
        self.assertTrue(by_label['5 hours']['resetsAt'].startswith('2026-09-18T09:06:34'), by_label['5 hours']['resetsAt'])
        self.assertTrue(by_label['Monthly']['resetsAt'].startswith('2026-10-18T04:06:34'), by_label['Monthly']['resetsAt'])

    def test_clinepass_keeps_a_window_it_has_not_seen_before(self):
        # A plan tier that reports a fourth window must not lose a meter, and a
        # row with no usable percentage is dropped rather than shown as zero.
        payloads = {'/api/v1/users/me/plan/usage-limits': {'data': {'limits': [
            {'type': 'daily', 'percentUsed': 1, 'resetsAt': ''},
            {'type': 'five_hour', 'percentUsed': None, 'resetsAt': ''},
            {'type': 'monthly', 'percentUsed': 240, 'resetsAt': ''}]}, 'success': True},
            '/api/v1/users/me/plan': {'data': {'plan': {'displayName': 'Cline Pass (Monthly)'}}}}
        with patch.dict('os.environ', self.clinepass_env()), \
             patch.object(c.urllib.request, 'urlopen', side_effect=self.clinepass_urlopen(payloads)):
            quota = c.clinepass_quota(True)
        self.assertEqual([w['label'] for w in quota['limits']], ['Daily', 'Monthly'])
        self.assertEqual(quota['limits'][0]['percent'], 0.01)
        # An over-quota window clamps for the meter but keeps the real figure,
        # so a saturated bar is not read as exactly full.
        self.assertEqual(quota['limits'][1]['percent'], 1.0)
        self.assertAlmostEqual(quota['limits'][1]['raw'], 2.4, places=6)

    def test_clinepass_key_precedence_and_no_key_message(self):
        config = self.root / 'config/omarchy/ai-usage'
        config.mkdir(parents=True, exist_ok=True)
        with patch.dict('os.environ', {'CLINE_API_KEY': '', 'XDG_CONFIG_HOME': str(self.root / 'config'),
                                       'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c.urllib.request, 'urlopen') as request:
            c.atomic_json(c.CONFIG, c.DEFAULTS)
            quota = c.clinepass_quota(True)
            self.assertIn('Settings', quota['error'])
            request.assert_not_called()
            (config / 'clinepass.key').write_text('file-key\n')
            self.assertEqual(c.clinepass_key(), 'file-key')
            with patch.dict('os.environ', {'CLINE_API_KEY': 'env-key'}): self.assertEqual(c.clinepass_key(), 'env-key')
            self.assertEqual(c.clinepass_key(c.DEFAULTS | {'clinepassApiKey': 'typed-key'}), 'typed-key')

    def test_clinepass_quota_errors_do_not_expose_the_key(self):
        import io
        def failing(request, timeout=12):
            raise c.urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {},
                                           io.BytesIO(b'bad test-clinepass-key'))
        with patch.dict('os.environ', self.clinepass_env()), \
             patch.object(c.urllib.request, 'urlopen', side_effect=failing):
            quota = c.clinepass_quota(True)
        self.assertNotIn('test-clinepass-key', json.dumps(quota))
        self.assertIn('unavailable', quota['error'])
        # The response body never reaches the cache either.
        self.assertNotIn('test-clinepass-key', (c.STATE / 'clinepass-quota.json').read_text())

    def test_clinepass_refuses_an_unsuccessful_envelope(self):
        # Errors arrive as {"error": ..., "success": false}; the body must not
        # be quoted back at the user.
        payloads = {'/api/v1/users/me/plan/usage-limits': {'error': 'invalid api key test-clinepass-key', 'success': False}}
        with patch.dict('os.environ', self.clinepass_env()), \
             patch.object(c.urllib.request, 'urlopen', side_effect=self.clinepass_urlopen(payloads)):
            quota = c.clinepass_quota(True)
        self.assertIn('unavailable', quota['error'])
        self.assertNotIn('test-clinepass-key', json.dumps(quota))

    def test_clinepass_prices_only_its_own_route(self):
        rates = c.load_rates()['document']
        self.assertIn('clinepass/cline-pass/deepseek-v4.1-flash', rates)
        # The vendor prefix is part of the model id, so no bare name is stored.
        self.assertNotIn('cline-pass/deepseek-v4.1-flash', rates)
        # 01:30 UTC on a Saturday is outside the published peak window.
        off_peak = dt.datetime(2026, 9, 19, 1, 30, tzinfo=dt.timezone.utc).timestamp()
        row = c.record('a', 'clinepass', 's', off_peak, 'cline-pass/deepseek-v4.1-flash', '/p', 'Hermes', input=1_000_000)
        row['apiProvider'] = 'clinepass'
        value, _ = c.price(row, rates)
        self.assertAlmostEqual(value, 0.22, places=6)
        # Hermes may record the API provider as cline-pass, while the account
        # route remains clinepass. It uses the same resale rate.
        self.assertAlmostEqual(c.price(dict(row, apiProvider='cline-pass'), rates)[0], 0.22)
        # The same model name on another route keeps that route's rate.
        other = c.record('b', 'ollama-cloud', 's', off_peak, 'deepseek-v4.1-flash', '/p', 'Hermes', input=1_000_000)
        other['apiProvider'] = 'ollama-cloud'
        other_value, _ = c.price(other, rates)
        self.assertAlmostEqual(other_value, 0.15, places=6)
        # A ClinePass row that lost the vendor prefix is unpriced rather than
        # silently priced at another route's rate or at zero.
        prefixless, _ = c.price(dict(row, model='deepseek-v4.1-flash'), rates)
        self.assertIsNone(prefixless)
        # Some clients write the id with the model vendor's own prefix instead.
        # That shape is still a namespaced id, so the route's own rate applies.
        self.assertAlmostEqual(c.price(dict(row, model='deepseek/deepseek-v4.1-flash'), rates)[0], 0.22, places=6)

    def test_clinepass_pricing_charges_the_peak_window(self):
        rates = c.load_rates()['document']
        row = c.record('a', 'clinepass', 's', 0, 'cline-pass/deepseek-v4.1-flash', '/p', 'Hermes', input=1_000_000)
        row['apiProvider'] = 'clinepass'
        for moment, expected in ((dt.datetime(2026, 9, 21, 0, 59, tzinfo=dt.timezone.utc), 0.22),
                                 (dt.datetime(2026, 9, 21, 1, 0, tzinfo=dt.timezone.utc), 0.44),
                                 (dt.datetime(2026, 9, 21, 4, 0, tzinfo=dt.timezone.utc), 0.22),
                                 (dt.datetime(2026, 9, 21, 9, 59, tzinfo=dt.timezone.utc), 0.44),
                                 (dt.datetime(2026, 9, 21, 10, 0, tzinfo=dt.timezone.utc), 0.22)):
            value, _ = c.price(dict(row, ts=moment.timestamp()), rates)
            self.assertAlmostEqual(value, expected, places=6, msg=moment.isoformat())

    def test_settings_accepts_a_stdin_payload_and_masks_the_reply(self):
        # The UI sends the payload on stdin so a key never reaches argv.
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'))
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parents[1] / 'collector.py'), 'settings', '--save'],
            input='{"ollamaApiKey":"stdin-key","enabled":["ollama-cloud"]}',
            capture_output=True, text=True, env=env, cwd=str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('stdin-key', result.stdout)
        self.assertEqual(json.loads(result.stdout)['ollamaApiKey'], c.API_KEY_MASK)
        stored = json.loads((self.root / 'config/omarchy/ai-usage/settings.json').read_text())
        self.assertEqual(stored['ollamaApiKey'], 'stdin-key')

    def settings_save(self, env):
        """A settings save with a live, still-open stdin pipe, the way the
        window starts one. Returns the process and its three pipes."""
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).parents[1] / 'collector.py'), 'settings', '--save'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(self.root))
        stdin, stdout, stderr = child.stdin, child.stdout, child.stderr
        assert stdin and stdout and stderr
        self.addCleanup(child.kill)
        for pipe in (stdin, stdout, stderr): self.addCleanup(pipe.close)
        return child, stdin, stdout, stderr

    def test_settings_save_reads_a_payload_that_arrives_late(self):
        # A scripted caller can open the pipe and write a moment later. Only a
        # quiet gap between chunks is short; the first byte gets the full wait.
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'))
        child, stdin, stdout, stderr = self.settings_save(env)
        time.sleep(0.6)
        stdin.write('{"enabled": ["codex"], "monthlyPrices": {"codex": 200}}\n'); stdin.flush()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail('the save gave up on a payload that arrived after 0.6s')
        self.assertEqual(child.returncode, 0, stderr.read())
        stored = json.loads((self.root / 'config/omarchy/ai-usage/settings.json').read_text())
        self.assertEqual(stored['monthlyPrices'], {'codex': 200.0})

    def test_settings_save_keeps_a_stray_value_out_of_the_error_text(self):
        # A key pasted into a price or opacity field must not come back in the
        # error: the window renders whatever this channel prints.
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'))
        for payload in ({'enabled': ['codex'], 'monthlyPrices': {'codex': 'CANARY-typed-in-the-wrong-field'}},
                        {'enabled': ['codex'], 'windowOpacity': 'CANARY-typed-in-the-wrong-field'}):
            with self.subTest(payload=payload):
                child, stdin, stdout, stderr = self.settings_save(env)
                stdin.write(json.dumps(payload) + '\n'); stdin.flush()
                child.wait(timeout=15)
                out, err = stdout.read(), stderr.read()
                self.assertEqual(child.returncode, 1, out)
                self.assertIn('error', json.loads(out))
                self.assertNotIn('CANARY', out + err)
                self.assertFalse((self.root / 'config/omarchy/ai-usage/settings.json').exists())

    def test_settings_save_rejects_a_payload_that_is_not_an_object(self):
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'))
        child, stdin, stdout, stderr = self.settings_save(env)
        stdin.write('[1, 2]\n'); stdin.flush()
        child.wait(timeout=15)
        out, err = stdout.read(), stderr.read()
        self.assertEqual(child.returncode, 1, err)
        self.assertIn('error', json.loads(out))
        self.assertNotIn('Traceback', err)

    def test_settings_save_finishes_while_the_client_holds_stdin_open(self):
        # A GUI client keeps the write end of the pipe open for as long as the
        # collector runs, so a save that reads to the end of input never comes
        # back: no settings written, no exit, and a Save button that stays
        # disabled for the life of the window.
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'))
        child, stdin, stdout, stderr = self.settings_save(env)
        stdin.write('{"monthlyPrices": {"commandcode": 20}, "enabled": ["codex"]}\n'); stdin.flush()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail('the save waited for end of input instead of writing the settings')
        self.assertEqual(child.returncode, 0, stderr.read())
        stored = json.loads((self.root / 'config/omarchy/ai-usage/settings.json').read_text())
        self.assertEqual(stored['monthlyPrices'], {'commandcode': 20.0})

    def test_settings_save_without_a_payload_fails_instead_of_reporting_success(self):
        # An empty stdin used to fall through to reading the settings, which
        # exits 0: the window closed with "Settings saved" and wrote nothing.
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'))
        child, stdin, stdout, stderr = self.settings_save(env)
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail('the save waited for end of input instead of reporting the missing payload')
        self.assertEqual(child.returncode, 1)
        self.assertIn('error', json.loads(stdout.read()))
        self.assertFalse((self.root / 'config/omarchy/ai-usage/settings.json').exists())

    def test_settings_channel_never_echoes_the_key(self):
        with patch.dict('os.environ', {'XDG_CONFIG_HOME': str(self.root / 'config')}):
            saved = c.save_settings(c.DEFAULTS | {'ollamaApiKey': 'typed-key'})
            self.assertEqual(saved['ollamaApiKey'], 'typed-key')
            # A payload that omits the key entirely must leave it alone rather
            # than clear it (the form sends the mask when the field is
            # untouched, but a client may simply not send the field).
            kept = c.save_settings({k: v for k, v in saved.items() if k != 'ollamaApiKey'})
            self.assertEqual(kept['ollamaApiKey'], 'typed-key')
            # An explicit empty string is a deliberate clear.
            cleared = c.save_settings(saved | {'ollamaApiKey': ''})
            self.assertEqual(cleared['ollamaApiKey'], '')

    def test_over_quota_window_is_clamped_not_dropped(self):
        payload = {'limits': {'monthly': {'usage': 1.5}}}
        with patch.dict('os.environ', {'OLLAMA_API_KEY': 'test-ollama-key', 'XDG_CONFIG_HOME': str(self.root / 'config'),
                                       'XDG_DATA_HOME': str(self.root / 'data')}), \
             patch.object(c.urllib.request, 'urlopen', side_effect=self.ollama_urlopen(payload)):
            quota = c.ollama_quota(True)
        # An over-limit plan shows as full, not as a credential failure.
        self.assertEqual(quota['error'], '')
        self.assertEqual(quota['limits'][0]['percent'], 1.0)
        self.assertEqual(quota['limits'][0]['raw'], 1.5)

    def test_settings_keep_a_stored_ollama_key_and_report_only_masks_it(self):
        with patch.dict('os.environ', {'XDG_CONFIG_HOME': str(self.root / 'config')}):
            saved = c.save_settings(c.DEFAULTS | {'enabled': ['ollama-cloud'], 'ollamaApiKey': 'typed-key'})
            self.assertEqual(saved['ollamaApiKey'], 'typed-key')
            # The report round trip carries the mask; saving it back keeps the key.
            again = c.save_settings(c.DEFAULTS | saved | {'ollamaApiKey': c.API_KEY_MASK})
            self.assertEqual(again['ollamaApiKey'], 'typed-key')
            ledger = c.Ledger(self.root / 'mask.sqlite')
            with patch.object(c, 'quota', return_value={'limits': []}), patch.object(c, 'account_quotas', return_value=({}, {})), patch.object(c, 'theme', return_value={}):
                data = c.report(ledger, c.settings() | {'enabled': ['ollama-cloud']}, days=7)
            self.assertEqual(data['settings']['ollamaApiKey'], c.API_KEY_MASK)
            ledger.db.close()


if __name__ == '__main__': unittest.main()
