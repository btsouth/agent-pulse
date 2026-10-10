import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

import claude_limits

GRANT = {'id': 'launch-grant', 'resets_total': 1, 'resets_left': 1,
         'starts_at': '2026-09-22T16:00:00+00:00', 'ends_at': '2026-10-22T16:00:00+00:00'}
NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


class BankedTests(unittest.TestCase):
    def test_counts_live_grants_and_reports_earliest_expiry(self):
        later = dict(GRANT, id='later', resets_left=2, ends_at='2026-11-01T00:00:00+00:00')
        expired = dict(GRANT, id='expired', ends_at='2026-09-01T00:00:00+00:00')
        spent = dict(GRANT, id='spent', resets_left=0, ends_at='2026-09-24T00:00:00+00:00')
        block = {'eligible': True, 'grants': [later, GRANT, expired, spent]}
        self.assertEqual(claude_limits.banked(block, NOW), (3, '2026-10-22T16:00:00+00:00'))

    def test_ineligible_account_has_none(self):
        self.assertEqual(claude_limits.banked({'eligible': False, 'ineligible_reason': 'tier'}, NOW), (0, ''))

    def test_refused_request_is_unknown_not_zero(self):
        for reason in ('surface', 'cli_version'):
            with self.assertRaises(ValueError):
                claude_limits.banked({'eligible': False, 'ineligible_reason': reason}, NOW)


class VersionTests(unittest.TestCase):
    def test_finds_the_cli_outside_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / '.local/bin/claude'
            cli.parent.mkdir(parents=True)
            cli.write_text('#!/bin/sh\necho "2.1.280 (Claude Code)"\n')
            cli.chmod(0o755)
            with patch.dict(os.environ, {'HOME': tmp, 'PATH': '/nonexistent'}):
                self.assertEqual(claude_limits.cli_version(), '2.1.280')


class RefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        claude = self.root / '.claude'
        claude.mkdir()
        (claude / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'claude-token', 'expiresAt': (time.time() + 3600) * 1000}}))
        usage = self.root / 'state/omarchy/agents/usage'
        usage.mkdir(parents=True)
        self.record = usage / 'claude.json'
        self.record.write_text(json.dumps({'id': 'claude', 'limits': []}))
        self.cache = self.root / 'cache/omarchy-usage-dashboard/claude-resets.json'
        self.claude = claude
        self.share_dir = self.root / 'cache/claude-usage'
        self.env = {'HOME': str(self.root), 'XDG_STATE_HOME': str(self.root / 'state'),
                    'XDG_CACHE_HOME': str(self.root / 'cache'), 'CLAUDE_CONFIG_DIR': str(claude)}
        self.requests = []

    def write_share(self, usage, fetched_at_ms=None, config_dir=None, raw=None):
        self.share_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r'[^A-Za-z0-9._-]+', '-', str(self.claude).lstrip('/'))
        payload = raw if raw is not None else json.dumps({
            'configDir': config_dir or str(self.claude),
            'fetchedAtMs': fetched_at_ms,
            'usage': usage,
        })
        (self.share_dir / (name + '.json')).write_text(payload)

    def write_poll(self, attempted_at_ms=None, next_attempt_at_ms=None, config_dir=None, raw=None):
        self.share_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r'[^A-Za-z0-9._-]+', '-', str(self.claude).lstrip('/'))
        payload = raw if raw is not None else json.dumps({
            'configDir': config_dir or str(self.claude),
            'attemptedAtMs': attempted_at_ms,
            'status': 'rate_limited',
            'nextAttemptAtMs': next_attempt_at_ms,
        })
        (self.share_dir / (name + '.poll.json')).write_text(payload)

    def run_main(self, argv=(), body=None, error=None):
        def response(req, timeout):
            self.requests.append(req)
            if error:
                raise error
            return io.BytesIO(json.dumps(body).encode())
        with patch.dict(os.environ, self.env), \
                patch.object(claude_limits, 'cli_version', return_value='2.1.280'), \
                patch.object(claude_limits.request, 'urlopen', side_effect=response):
            claude_limits.main(list(argv))
        return json.loads(self.record.read_text())

    def test_writes_count_and_expiry_as_the_installed_cli(self):
        record = self.run_main(body={'cedar_ember': {'eligible': True, 'grants': [GRANT]}})
        self.assertEqual(record['resetCreditsAvailable'], 1)
        self.assertEqual(record['resetCreditsExpiresAt'], '2026-10-22T16:00:00+00:00')
        req = self.requests[0]
        self.assertIn('cedar_ember=1', req.full_url)
        self.assertEqual(req.get_header('Authorization'), 'Bearer claude-token')
        self.assertEqual(req.get_header('User-agent'), 'claude-cli/2.1.280 (external, cli)')

    def test_fresh_answer_is_reused_until_forced(self):
        body = {'cedar_ember': {'eligible': True, 'grants': [GRANT]}}
        self.run_main(body=body)
        self.run_main(body=body)
        self.assertEqual(len(self.requests), 1)
        self.run_main(['--force'], body=body)
        self.assertEqual(len(self.requests), 2)

    def test_fresh_shared_reading_is_used_without_a_request_even_when_forced(self):
        body = {'cedar_ember': {'eligible': True, 'grants': [GRANT]}}
        self.write_share(body, int(time.time() * 1000))
        self.assertEqual(self.run_main(body=body)['resetCreditsAvailable'], 1)
        self.assertEqual(self.run_main(['--force'], body=body)['resetCreditsAvailable'], 1)
        self.assertEqual(self.requests, [])

    def test_old_shared_reading_falls_back_to_fetch(self):
        body = {'cedar_ember': {'eligible': True, 'grants': [GRANT]}}
        self.write_share(body, int((time.time() - 181) * 1000))
        self.assertEqual(self.run_main(body=body)['resetCreditsAvailable'], 1)
        self.assertEqual(len(self.requests), 1)

    def test_shared_reading_for_another_config_is_ignored(self):
        body = {'cedar_ember': {'eligible': True, 'grants': [GRANT]}}
        self.write_share(body, int(time.time() * 1000), config_dir=str(self.root / '.other'))
        self.assertEqual(self.run_main(body=body)['resetCreditsAvailable'], 1)
        self.assertEqual(len(self.requests), 1)

    def test_t3_polling_uses_stale_shared_reading_without_request(self):
        body = {'cedar_ember': {'eligible': True, 'grants': [GRANT]}}
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text(json.dumps({'fetchedAt': time.time() - 600, 'count': 4, 'expiresAt': ''}))
        self.write_share(body, int((time.time() - 181) * 1000))
        self.write_poll(int(time.time() * 1000), int(time.time() * 1000))
        record = self.run_main(error=OSError('must not fetch'))
        self.assertEqual(record['resetCreditsAvailable'], 1)
        self.assertEqual(self.requests, [])

    def test_t3_polling_keeps_cached_answer_without_share(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text(json.dumps({'fetchedAt': time.time() - 600, 'count': 2, 'expiresAt': ''}))
        self.write_poll(int(time.time() * 1000), int(time.time() * 1000))
        record = self.run_main(error=OSError('must not fetch'))
        self.assertEqual(record['resetCreditsAvailable'], 2)
        self.assertEqual(self.requests, [])

    def test_expired_t3_poll_file_falls_back_to_fetch(self):
        body = {'cedar_ember': {'eligible': True, 'grants': [GRANT]}}
        self.write_poll(int((time.time() - 1000) * 1000), int((time.time() - 1000) * 1000))
        self.assertEqual(self.run_main(body=body)['resetCreditsAvailable'], 1)
        self.assertEqual(len(self.requests), 1)

    def test_t3_poll_file_for_another_config_is_ignored(self):
        body = {'cedar_ember': {'eligible': True, 'grants': [GRANT]}}
        self.write_poll(int(time.time() * 1000), int(time.time() * 1000),
                        config_dir=str(self.root / '.other'))
        self.assertEqual(self.run_main(body=body)['resetCreditsAvailable'], 1)
        self.assertEqual(len(self.requests), 1)

    def test_t3_poll_backoff_keeps_ownership_after_attempt_window(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text(json.dumps({'fetchedAt': time.time() - 600, 'count': 3, 'expiresAt': ''}))
        self.write_poll(int((time.time() - 1000) * 1000), int((time.time() + 3600) * 1000))
        record = self.run_main(error=OSError('must not fetch'))
        self.assertEqual(record['resetCreditsAvailable'], 3)
        self.assertEqual(self.requests, [])

    def test_malformed_shared_reading_is_ignored(self):
        body = {'cedar_ember': {'eligible': True, 'grants': [GRANT]}}
        self.write_share(None, raw='not json')
        self.assertEqual(self.run_main(body=body)['resetCreditsAvailable'], 1)
        self.assertEqual(len(self.requests), 1)

    def test_failed_read_keeps_a_recent_answer_then_goes_unknown(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text(json.dumps({'fetchedAt': time.time() - 600, 'count': 1, 'expiresAt': ''}))
        self.assertEqual(self.run_main(error=OSError('429'))['resetCreditsAvailable'], 1)
        self.cache.write_text(json.dumps({'fetchedAt': time.time() - 3600, 'count': 1, 'expiresAt': ''}))
        self.assertIsNone(self.run_main(error=OSError('429'))['resetCreditsAvailable'])

    def test_rate_limit_waits_out_retry_after_even_when_forced(self):
        limited = claude_limits.error.HTTPError(claude_limits.USAGE_URL, 429, 'Too Many Requests',
                                                {'retry-after': '120'}, None)
        self.run_main(error=limited)
        self.run_main(['--force'], body={'cedar_ember': {'eligible': True, 'grants': [GRANT]}})
        self.assertEqual(len(self.requests), 1)
        self.assertGreater(json.loads(self.cache.read_text())['retryAt'], time.time() + 100)

    def test_second_account_gets_its_own_resets(self):
        second = self.root / '.claude2'
        second.mkdir()
        (second / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'second-token', 'expiresAt': (time.time() + 3600) * 1000}}))
        settings = self.root / 'config/omarchy/ai-usage/settings.json'
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({'accounts': [{'id': 'account-1', 'label': 'Claude Personal',
                                                      'directories': [{'provider': 'claude', 'path': str(second)}]}]}))
        usage = self.record.parent
        (usage / 'claude-second.json').write_text(json.dumps({'id': 'claude-second', 'name': 'Claude Personal'}))
        (usage / 'claude-third.json').write_text(json.dumps({'id': 'claude-third', 'name': 'Claude Trial'}))
        self.env['XDG_CONFIG_HOME'] = str(self.root / 'config')

        def response(req, timeout):
            self.requests.append(req)
            spent = req.get_header('Authorization') == 'Bearer claude-token'
            body = {'cedar_ember': {'eligible': True, 'grants': [dict(GRANT, resets_left=0 if spent else 1)]}}
            return io.BytesIO(json.dumps(body).encode())
        with patch.dict(os.environ, self.env), \
                patch.object(claude_limits, 'cli_version', return_value='2.1.280'), \
                patch.object(claude_limits.request, 'urlopen', side_effect=response):
            claude_limits.main([])
        self.assertEqual(json.loads(self.record.read_text())['resetCreditsAvailable'], 0)
        personal = json.loads((usage / 'claude-second.json').read_text())
        self.assertEqual(personal['resetCreditsAvailable'], 1)
        self.assertEqual(personal['resetCreditsExpiresAt'], '2026-10-22T16:00:00+00:00')
        # A record linked to no account folder is never given another login's count.
        self.assertNotIn('resetCreditsAvailable', json.loads((usage / 'claude-third.json').read_text()))
        self.assertEqual(len(self.requests), 2)

    def test_expired_sign_in_sends_nothing(self):
        (self.root / '.claude/.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'claude-token', 'expiresAt': 1000}}))
        before = self.record.read_bytes()
        self.run_main(body={})
        self.assertEqual(self.requests, [])
        # No count was ever known, so the record is left as its collector wrote it.
        self.assertEqual(self.record.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
