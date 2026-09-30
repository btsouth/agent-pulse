import json
import os
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import codex_limits

ROOT = Path(__file__).parents[1]


class CodexLimitRepairTests(unittest.TestCase):
    def setUp(self):
        credits = patch.object(codex_limits, 'fetch_credit_balance', return_value={'error': 'Credit balance unavailable'})
        credits.start()
        self.addCleanup(credits.stop)

    def test_banked_resets_use_each_accounts_auth_and_clear_spent_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_home = root / '.codex'
            second_home = root / 'second/.codex'
            for folder, account in ((main_home, 'main-account'), (second_home, 'second-account')):
                folder.mkdir(parents=True)
                (folder / 'auth.json').write_text(json.dumps({'tokens': {
                    'access_token': account + '-token', 'account_id': account}}))
            config = root / 'config/omarchy/ai-usage/settings.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'accounts': [{'id': 'codex-second', 'directories':
                [{'provider': 'codex', 'path': str(second_home)}]}]}))
            usage = root / 'state/omarchy/agents/usage'
            usage.mkdir(parents=True)
            for agent in ('codex', 'codex-second'):
                (usage / (agent + '.json')).write_text(json.dumps({
                    'id': agent, 'limits': [{'label': 'Weekly (7-day)', 'percent': 0.2}],
                    'resetCreditsAvailable': 1}))
            seen = []
            def response(req, timeout):
                token = req.get_header('Authorization')
                account = req.get_header('Chatgpt-account-id')
                seen.append((token, account))
                count = 1 if account == 'main-account' else 0
                return io.BytesIO(json.dumps({'available_count': count}).encode())
            env = {'HOME': str(root), 'XDG_CONFIG_HOME': str(root / 'config'),
                   'XDG_STATE_HOME': str(root / 'state'), 'CODEX_HOME': str(second_home)}
            with patch.dict(os.environ, env), patch.object(codex_limits.request, 'urlopen', side_effect=response):
                codex_limits.main()
            self.assertEqual(seen, [('Bearer main-account-token', 'main-account'),
                                    ('Bearer second-account-token', 'second-account')])
            self.assertEqual(json.loads((usage / 'codex.json').read_text())['resetCreditsAvailable'], 1)
            self.assertEqual(json.loads((usage / 'codex-second.json').read_text())['resetCreditsAvailable'], 0)

    def test_banked_resets_carry_the_earliest_live_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {
                'access_token': 'token', 'account_id': 'account'}}))
            record = root / 'codex.json'
            record.write_text(json.dumps({'id': 'codex', 'limits': [], 'resetCreditsAvailable': 0}))
            credits = [{'status': 'available', 'expires_at': '2099-11-01T00:00:00Z'},
                       {'status': 'available', 'expires_at': '2099-10-22T20:37:57Z'},
                       {'status': 'available', 'expires_at': '2000-01-01T00:00:00Z'},
                       {'status': 'redeemed', 'expires_at': '2099-01-01T00:00:00Z'}]
            body = json.dumps({'available_count': 2, 'credits': credits}).encode()
            with patch.object(codex_limits.request, 'urlopen', return_value=io.BytesIO(body)):
                self.assertTrue(codex_limits.repair(record, root))
            saved = json.loads(record.read_text())
            self.assertEqual(saved['resetCreditsAvailable'], 2)
            self.assertEqual(saved['resetCreditsExpiresAt'], '2099-10-22T20:37:57+00:00')

    def test_failed_credit_lookup_marks_count_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {
                'access_token': 'token', 'account_id': 'account'}}))
            record = root / 'codex.json'
            record.write_text(json.dumps({'id': 'codex', 'limits': [], 'resetCreditsAvailable': 1}))
            with patch.object(codex_limits.request, 'urlopen', side_effect=OSError('offline')):
                self.assertTrue(codex_limits.repair(record, root))
            saved = json.loads(record.read_text())
            self.assertIsNone(saved['resetCreditsAvailable'])
            self.assertEqual(saved['resetCreditsExpiresAt'], '')

    def test_repairs_failed_named_account_with_buffered_rpc_replies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            codex = bin_dir / 'codex'
            codex.write_text('''#!/usr/bin/python3
import sys
for line in sys.stdin:
    if '"id": 1' in line:
        sys.stdout.write('{"id":1,"result":{}}\\n')
    elif '"id": 2' in line:
        # A notification and reply arrive in one write. A buffered readline
        # can consume both but return only the first line to select's caller.
        sys.stdout.write('{"method":"notice"}\\n{"id":2,"result":{"account":{"planType":"pro"}}}\\n')
    elif '"id": 3' in line:
        sys.stdout.write('{"id":3,"result":{"rateLimits":{"primary":{"usedPercent":7,"windowDurationMins":10080,"resetsAt":1790000000}}}}\\n')
    sys.stdout.flush()
''')
            codex.chmod(0o755)
            config = root / 'config/omarchy/ai-usage/settings.json'
            config.parent.mkdir(parents=True)
            second = root / 'second/.codex'
            second.mkdir(parents=True)
            config.write_text(json.dumps({'accounts': [{'id': 'codex-second', 'directories':
                                [{'provider': 'codex', 'path': str(second)}]}]}))
            usage = root / 'state/omarchy/agents/usage'
            usage.mkdir(parents=True)
            record = usage / 'codex-second.json'
            record.write_text(json.dumps({'id': 'codex-second', 'usageStatusText':
                'Codex limits unavailable', 'authHelpText': 'account/read', 'limits': [],
                'todayPrompts': 4}))
            env = dict(os.environ, HOME=str(root), XDG_CONFIG_HOME=str(root / 'config'),
                       XDG_STATE_HOME=str(root / 'state'), PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
            result = subprocess.run(['python3', str(ROOT / 'codex_limits.py')], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            fixed = json.loads(record.read_text())
            self.assertEqual(fixed['usageStatusText'], '')
            self.assertEqual(fixed['tierLabel'], 'pro')
            self.assertEqual(fixed['limits'][0]['percent'], 0.07)
            self.assertEqual(fixed['todayPrompts'], 4)


class ChatGPTCreditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {'HOME': str(self.root), 'XDG_STATE_HOME': str(self.root / 'state'),
                                     'XDG_CONFIG_HOME': str(self.root / 'config')})
        env.start()
        self.addCleanup(env.stop)
        self.home = self.root / '.codex'
        self.home.mkdir()
        self.auth('account-one')

    def auth(self, account):
        (self.home / 'auth.json').write_text(json.dumps({'tokens': {
            'access_token': 'private-token', 'account_id': account}}))

    def balance(self, value, unlimited=False):
        body = json.dumps({'credits': {'balance': value, 'unlimited': unlimited}}).encode()
        with patch.object(codex_limits.request, 'urlopen', return_value=io.BytesIO(body)) as call:
            result = codex_limits.fetch_credit_balance(self.home)
        req = call.call_args.args[0]
        self.assertEqual(req.full_url, codex_limits.USAGE_URL)
        self.assertEqual(req.get_header('Authorization'), 'Bearer private-token')
        self.assertEqual(req.get_header('Chatgpt-account-id'), json.loads((self.home / 'auth.json').read_text())['tokens']['account_id'])
        return result

    def test_tracks_decreases_topups_and_repeated_refreshes(self):
        first = self.balance('62500')
        self.assertEqual((first['remaining'], first['spent']), (62500, 0))
        used = self.balance('62499.75')
        self.assertEqual(used['spent'], 0.25)
        self.assertEqual(self.balance('62499.75')['spent'], 0.25)
        self.assertEqual(self.balance('70000')['spent'], 0.25)
        final = self.balance('69900')
        self.assertEqual(final['spent'], 100.25)
        self.assertEqual(final['trackingSince'], first['trackingSince'])
        tracker = self.root / 'state/omarchy/ai-usage/chatgpt-credits.json'
        self.assertEqual(tracker.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('private-token', tracker.read_text())
        self.assertNotIn('account-one', tracker.read_text())

    def test_account_switch_and_zero_balance(self):
        self.balance('100')
        self.assertEqual(self.balance('90')['spent'], 10)
        self.auth('account-two')
        self.assertEqual(self.balance('0')['spent'], 0)
        self.auth('account-one')
        self.assertEqual(self.balance('80')['spent'], 20)

    def test_unlimited_and_invalid_balances_do_not_invent_usage(self):
        self.assertTrue(self.balance(None, unlimited=True)['unlimited'])
        for value in (None, True, 'bad', 'NaN', 'Infinity', '-1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.balance(value)

    def test_failure_hides_previous_balance_and_preserves_history(self):
        self.balance('100')
        record = self.root / 'codex.json'
        record.write_text(json.dumps({'limits': [], 'chatgptCredits': {'remaining': 100}}))
        with patch.object(codex_limits.request, 'urlopen', side_effect=OSError('private-token')):
            self.assertTrue(codex_limits.repair(record, self.home))
        self.assertEqual(json.loads(record.read_text())['chatgptCredits'], {'error': 'Credit balance unavailable'})
        self.assertEqual(self.balance('95')['spent'], 5)

    def test_owned_snapshot_survives_native_records_and_clears_failed_reads(self):
        usage = self.root / 'state/omarchy/agents/usage'
        usage.mkdir(parents=True)
        record = usage / 'codex.json'
        record.write_text(json.dumps({'id': 'codex', 'limits': []}))
        snapshot = self.root / 'state/omarchy/ai-usage/chatgpt-credit-snapshots.json'
        with patch.object(codex_limits, 'fetch_credit_balance', return_value={
                'remaining': 62500, 'updatedAt': '2026-09-30T00:00:00+00:00'}):
            codex_limits.main()
        record.write_text(json.dumps({'id': 'codex', 'limits': []}))
        self.assertEqual(json.loads(snapshot.read_text())['codex']['remaining'], 62500)
        with patch.object(codex_limits, 'fetch_credit_balance', side_effect=OSError('private-token')):
            codex_limits.main()
        failed = json.loads(snapshot.read_text())['codex']
        self.assertEqual(failed['error'], 'Credit balance unavailable')
        self.assertNotIn('remaining', failed)


if __name__ == '__main__':
    unittest.main()
