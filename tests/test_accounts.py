import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('collector', Path(__file__).parents[1] / 'collector.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for patcher in (patch.object(c, 'STATE', self.root / 'state'),
                        patch.object(c, 'CONFIG', self.root / 'settings.json'),
                        patch.object(c, 'HOME', self.root),
                        patch.dict(os.environ, {key: str(self.root / key.lower()) for key in (
                            'HOME', 'XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME',
                            'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'GROK_HOME', 'PI_CODING_AGENT_DIR', 'MUSE_HOME', 'CURSOR_HOME')})):
            patcher.start(); self.addCleanup(patcher.stop)
        self.ledger = c.Ledger(self.root / 'usage.sqlite')
        self.now = dt.datetime(2026, 9, 5, 18).astimezone()
        self.cfg = c.DEFAULTS | {'enabled': ['codex'], 'accounts': [
            {'id': 'work', 'label': 'Work', 'directories': [{'provider': 'codex', 'path': str(self.root / 'work')}]},
            {'id': 'personal', 'label': 'Personal', 'directories': [{'provider': 'codex', 'path': str(self.root / 'personal')}]}]}

    def tearDown(self):
        self.ledger.db.close()
        self.tmp.cleanup()

    def add(self, key, folder, tokens=100, session=None):
        self.ledger.put(c.record(key, 'codex', session or key, self.now.isoformat(), 'gpt-4.1', '/project', 'CLI', input=tokens), self.root / folder / 'sessions/log.jsonl')

    def test_chatgpt_credit_balances_follow_their_account_records(self):
        usage = c.STATE.parent / 'agents/usage'
        usage.mkdir(parents=True)
        for agent, remaining in (('codex', 62500), ('work', 200)):
            (usage / (agent + '.json')).write_text(json.dumps({'id': agent,
                'chatgptCredits': {'remaining': remaining, 'spent': 10, 'trackingSince': '2026-09-30T00:00:00Z'}}))
        self.assertEqual(c.quota('codex')['chatgptCredits']['remaining'], 62500)
        self.assertEqual(c.account_quotas()[0]['work']['chatgptCredits']['remaining'], 200)
        c.STATE.mkdir(parents=True, exist_ok=True)
        (c.STATE / 'chatgpt-credit-snapshots.json').write_text(json.dumps({
            'codex': {'remaining': 61000}, 'work': {'remaining': 150}}))
        # Native refreshes can overwrite the credit field in either record.
        (usage / 'codex.json').write_text(json.dumps({'id': 'codex', 'limits': []}))
        (usage / 'work.json').write_text(json.dumps({'id': 'work', 'limits': []}))
        self.assertEqual(c.quota('codex')['chatgptCredits']['remaining'], 61000)
        self.assertEqual(c.account_quotas()[0]['work']['chatgptCredits']['remaining'], 150)

    def write_usage(self, key, **fields):
        path = c.STATE.parent / 'agents/usage' / (key + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'id': key, **fields}))

    def test_main_account_keeps_its_quota_when_selected_and_idle(self):
        reset = self.now + dt.timedelta(days=2)
        self.write_usage('codex', limits=[{'label': 'Weekly', 'percent': .4, 'resetsAt': reset.isoformat()}])
        data = c.report(self.ledger, self.cfg, now=self.now, provider='codex', selection={'account': 'local'})
        self.assertEqual(data['summary']['tokens'], 0)
        self.assertEqual(data['cards'][0]['quota']['limits'][0]['percent'], .4)
        views = {v['id']: v for v in data['accountViews']}
        self.assertEqual(set(views), {'codex:local', 'codex:work', 'codex:personal'})
        self.assertEqual(views['codex:local']['quota']['limits'][0]['tokens'], 0)

    def test_reset_period_reconciles_totals_models_charts_and_boundaries(self):
        reset = self.now + dt.timedelta(hours=2)
        self.write_usage('codex', limits=[{'label': 'Session (5-hour)', 'resetsAt': reset.isoformat()}])
        self.add_at('before', 'local', 3 + 1 / 3600, 900)
        self.add_at('boundary', 'local', 3, 20)
        self.add_at('recent', 'local', 1, 100)
        self.add_at('other-account', 'work', 1, 7000)
        self.ledger.put(c.record('other-model', 'codex', 'model', self.now.isoformat(), 'gpt-5', '', 'CLI',
                                 input=40, output=10, cacheRead=30), self.root / 'local/log.jsonl')
        view = next(v for v in c.report(self.ledger, self.cfg, now=self.now)['accountViews'] if v['id'] == 'codex:local')
        window = view['quota']['limits'][0]
        data = c.report(self.ledger, self.cfg, days=365, now=self.now, provider='codex',
                        selection={'account': 'local', 'resetWindow': window['windowId']})
        self.assertEqual(data['summary']['tokens'], 200)
        self.assertEqual(window['tokens'], 200)
        self.assertEqual(sum(m['tokens'] for m in data['models']), 200)
        self.assertEqual(sum(d['total']['tokens'] for d in data['daily']), 200)
        self.assertEqual(sum(h['total']['tokens'] for h in data['hourly']), 200)
        self.assertEqual(data['period']['startAt'], int((self.now - dt.timedelta(hours=3)).timestamp()))
        self.assertEqual(data['period']['resetLabel'], 'Session (5-hour)')
        filtered = c.report(self.ledger, self.cfg, now=self.now, provider='codex',
                            selection={'account': 'local', 'resetWindow': window['windowId'], 'model': 'gpt-5'})
        self.assertEqual(filtered['summary']['tokens'], 80)
        self.assertEqual(filtered['cards'][0]['quota']['limits'][0]['tokens'], 200)

    def test_expired_unknown_and_model_limits_cannot_be_reset_periods(self):
        for limit in [{'label': 'Weekly', 'resetsAt': (self.now - dt.timedelta(seconds=1)).isoformat()},
                      {'label': 'Billing cycle', 'resetsAt': (self.now + dt.timedelta(days=1)).isoformat()},
                      {'label': 'Weekly', 'title': 'Model A', 'resetsAt': (self.now + dt.timedelta(days=1)).isoformat()}]:
            with self.subTest(limit=limit):
                self.write_usage('codex', limits=[limit])
                data = c.report(self.ledger, self.cfg, now=self.now, provider='codex', selection={'account': 'local', 'resetWindow': 'missing'})
                self.assertTrue(data['period']['error'])
                self.assertNotIn('windowId', data['accountViews'][0]['quota']['limits'][0])
                self.assertEqual(data['summary']['tokens'], 0)

    def test_t3_and_legacy_homes_keep_separate_accounts_without_duplicate_options(self):
        t3 = self.root / '.t3/userdata/settings.json'
        t3.parent.mkdir(parents=True)
        t3.write_text(json.dumps({'providerInstances': {
            'second': {'driver': 'codex', 'displayName': 'ChatGPT Second', 'config': {'homePath': str(self.root / 'second')}},
            'work': {'driver': 'codex', 'displayName': 'Work in T3', 'config': {'homePath': str(self.root / 'work')}}}}))
        self.cfg['codexHomes'] = [str(self.root / 'second'), str(self.root / 'legacy')]
        self.add('main', '.codex', 10)
        self.add('second', 'second', 20)
        self.add('legacy', 'legacy', 30)
        self.add('work', 'work', 40)
        data = self.report()
        views = data['accountViews']
        self.assertEqual(len(views), 5)
        second = next(v for v in views if v['name'] == 'ChatGPT Second')
        self.assertEqual(self.report(second['accountId'])['summary']['tokens'], 20)
        self.assertEqual(self.report('work')['summary']['tokens'], 40)
        self.assertEqual(self.report('local')['summary']['tokens'], 10)
        self.assertEqual(data['settings']['accounts'], self.cfg['accounts'])
        self.assertNotIn('_historyResolved', data['settings'])

    def test_native_record_matches_account_name_and_unlinked_record_has_no_history(self):
        self.write_usage('codex-second', name='Work', limits=[{'label': 'Weekly', 'percent': .2}])
        self.write_usage('claude-third', name='Claude Third', limits=[{'label': 'Weekly', 'percent': .9}])
        self.cfg['enabled'] = ['codex', 'claude']
        self.add('work', 'work')
        data = c.report(self.ledger, self.cfg, now=self.now)
        views = {v['id']: v for v in data['accountViews']}
        self.assertEqual(views['codex:work']['recordId'], 'codex-second')
        self.assertEqual(views['codex:work']['quota']['limits'][0]['percent'], .2)
        self.assertFalse(views['claude:claude-third']['historyAvailable'])
        self.assertEqual(views['claude:claude-third']['tokens'], 0)

    def test_provider_qualified_quota_does_not_create_a_duplicate_account(self):
        self.write_usage('codex:work', name='Different record label', limits=[{'label': 'Weekly', 'percent': .7}])
        self.add('work', 'work')
        data = c.report(self.ledger, self.cfg, now=self.now)
        self.assertEqual(len(data['accountViews']), 3)
        self.assertEqual(next(v for v in data['accountViews'] if v['accountId'] == 'work')['quota']['limits'][0]['percent'], .7)

    def test_named_account_with_default_record_name_never_borrows_main_quota(self):
        self.cfg['accounts'][0]['label'] = 'Codex'
        self.write_usage('codex', name='Codex', limits=[{'label': 'Weekly', 'percent': .9}])
        self.add('work', 'work')
        data = c.report(self.ledger, self.cfg, now=self.now)
        work = next(v for v in data['accountViews'] if v['accountId'] == 'work')
        self.assertEqual(work['quota']['limits'], [])

    def report(self, account=None, days=7):
        with patch.object(c, 'quota', return_value={'limits': [{'label': 'Local quota'}]}), \
             patch.object(c, 'account_quotas', return_value=({}, {})), patch.object(c, 'theme', return_value={}):
            return c.report(self.ledger, self.cfg, days=days, now=self.now, selection={'account': account} if account else {})

    def test_account_filters_reconcile_and_relabel_without_rescan(self):
        self.add('a', 'work', 100)
        self.add('b', 'personal', 300)
        self.add('c', 'local', 50)
        data = self.report()
        self.assertEqual(data['summary']['tokens'], 450)
        self.assertEqual(sum(self.report(a)['summary']['tokens'] for a in ['work', 'personal', 'local']), 450)
        self.assertEqual(self.report('work')['summary']['tokens'], 100)
        self.cfg['accounts'][0]['label'] = 'Client work'
        self.assertEqual(next(a['name'] for a in self.report()['accounts'] if a['accountId'] == 'work'), 'Client work')
        self.assertEqual(self.report()['summary']['tokens'], 450)

    def test_mirrors_deduplicate_and_cross_account_conflict_is_visible(self):
        self.add('same', 'work')
        self.add('same', 'work/mirror')
        self.add('same', 'local')
        self.assertEqual(self.report('work')['summary']['tokens'], 100)
        self.add('same', 'personal')
        data = self.report()
        self.assertEqual(data['summary']['tokens'], 100)
        self.assertEqual(self.report('conflict')['summary']['tokens'], 100)
        self.assertTrue(data['accountWarning'])
        self.assertEqual(self.report('work')['summary']['tokens'], 0)

    def test_imported_account_never_inherits_local_quota_or_price(self):
        self.cfg['monthlyPrices'] = {'codex': 200}
        self.add('a', 'work')
        data = self.report('work')['providers'][0]
        self.assertEqual(data['quota']['limits'], [])
        self.assertIsNone(data['monthlyPrice'])
        self.assertIsNone(self.report()['providers'][0]['monthlyPrice'])
        self.assertEqual(self.report()['providers'][0]['quotaScope'], 'Current login on this PC')
        self.assertIsNone(self.report('work')['cards'][0]['monthlyPrice'])

    def test_overview_cards_split_accounts_and_use_their_own_prices(self):
        self.cfg['monthlyPrices'] = {'codex': 200, 'personal': 20}
        self.add('a', 'work', 100)
        self.add('b', 'personal', 300)
        self.add('c', 'local', 50)
        cards = self.report()['cards']
        self.assertEqual([card['accountId'] for card in cards], ['personal', 'work', 'local'])
        self.assertEqual([card['name'] for card in cards],
                         ['Codex · Personal', 'Codex · Work', 'Codex · Local'])
        self.assertEqual([card['monthlyPrice'] for card in cards], [20, None, 200])
        self.assertEqual(cards[2]['quota']['limits'], [{'label': 'Local quota'}])
        self.assertEqual(cards[0]['quota']['limits'], [])
        self.assertEqual([card['provider'] for card in cards], ['codex', 'codex', 'codex'])
        self.assertEqual(self.report('personal')['cards'][0]['accountId'], 'personal')
        self.assertEqual(len(self.report('personal')['cards']), 1)

    def test_daily_and_hourly_split_series_by_account(self):
        self.add('a', 'work', 100)
        self.add('b', 'personal', 300)
        self.add('c', 'local', 50)
        data = self.report()
        today = data['daily'][-1]
        self.assertEqual(today['providers']['codex']['tokens'], 450)
        self.assertEqual(today['cards']['codex:personal']['tokens'], 300)
        self.assertEqual(today['cards']['codex:work']['tokens'], 100)
        self.assertEqual(today['cards']['codex:local']['tokens'], 50)
        self.assertEqual([card['id'] for card in data['cards']], ['codex:personal', 'codex:work', 'codex:local'])
        self.assertEqual([card['shade'] for card in data['cards']], [0, 1, 2])
        self.assertEqual([card['shades'] for card in data['cards']], [3, 3, 3])
        hourly = self.report(days=1)['hourly']
        self.assertEqual(sum(h['providers']['codex']['tokens'] for h in hourly), 450)
        self.assertEqual(sum(h['cards'].get('codex:personal', {}).get('tokens', 0) for h in hourly), 300)

    def test_named_account_quota_comes_from_its_own_record(self):
        self.add('a', 'work', 100)
        self.add('b', 'personal', 100)
        self.add('c', 'local', 50)
        state = self.root / 'state/ai-usage'
        usage = state.parent / 'agents/usage'
        usage.mkdir(parents=True)
        (usage / 'work.json').write_text(json.dumps({'name': 'Work',
            'limits': [{'label': 'Weekly (7-day)', 'percent': .95}], 'updatedAt': '2026-09-15T20:00:00+00:00'}))
        (usage / 'other.json').write_text(json.dumps({'name': 'Personal',
            'limits': [{'label': 'Weekly (7-day)', 'percent': .5}]}))
        (usage / 'broken.json').write_text('not json')
        with patch.object(c, 'STATE', state), patch.object(c, 'quota', return_value={'limits': [{'label': 'Local quota'}]}), \
             patch.object(c, 'theme', return_value={}):
            cards = {card['accountId']: card for card in c.report(self.ledger, self.cfg, now=self.now)['cards']}
        self.assertEqual(cards['work']['quota']['limits'][0]['percent'], .95)
        self.assertEqual(cards['work']['quotaScope'], 'From its own usage record')
        self.assertEqual(cards['personal']['quota']['limits'][0]['percent'], .5)
        self.assertEqual(cards['local']['quota']['limits'], [{'label': 'Local quota'}])
        self.assertEqual(cards['local']['quotaScope'], 'Current login on this PC')
        self.assertIn('current login', self.report('work')['cards'][0]['quota']['error'])

    def test_window_start_reads_length_from_label(self):
        reset = dt.datetime(2026, 3, 31, 12, tzinfo=dt.timezone.utc)
        for label, start in [('Session (5-hour)', dt.datetime(2026, 3, 31, 7, tzinfo=dt.timezone.utc)),
                             ('5 hours', dt.datetime(2026, 3, 31, 7, tzinfo=dt.timezone.utc)),
                             ('5h window', dt.datetime(2026, 3, 31, 7, tzinfo=dt.timezone.utc)),
                             ('30m window', dt.datetime(2026, 3, 31, 11, 30, tzinfo=dt.timezone.utc)),
                             ('Weekly (7-day)', dt.datetime(2026, 3, 24, 12, tzinfo=dt.timezone.utc)),
                             ('Weekly', dt.datetime(2026, 3, 24, 12, tzinfo=dt.timezone.utc)),
                             # A calendar month back, clamped to February's last day.
                             ('Monthly', dt.datetime(2026, 2, 28, 12, tzinfo=dt.timezone.utc))]:
            self.assertEqual(c.window_start(label, reset), start, label)
        self.assertEqual(c.window_start('Monthly', dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)),
                         dt.datetime(2025, 12, 15, tzinfo=dt.timezone.utc))
        for label in ('Billing cycle', 'Auto', 'Session', ''):
            self.assertIsNone(c.window_start(label, reset), label)

    def add_at(self, key, folder, hours_ago, tokens):
        when = (self.now - dt.timedelta(hours=hours_ago)).isoformat()
        self.ledger.put(c.record(key, 'codex', key, when, 'gpt-4.1', '', 'CLI', input=tokens), self.root / folder / 'sessions/log.jsonl')

    def test_limit_windows_count_their_own_account_since_the_window_began(self):
        self.add_at('recent', 'local', 1, 100)
        self.add_at('earlier', 'local', 30, 20)
        self.add_at('old', 'local', 24 * 8, 5)
        self.add_at('work', 'work', 1, 7000)
        # Copied under the local login and a named account: it is the named one's.
        self.add_at('copy', 'local', 1, 300)
        self.add_at('copy', 'work', 1, 300)
        reset = (self.now + dt.timedelta(hours=2)).astimezone(dt.timezone.utc)
        limits = [{'label': 'Session (5-hour)', 'resetsAt': reset.isoformat(), 'percent': .2},
                  {'label': 'Weekly (7-day)', 'resetsAt': reset.isoformat().replace('+00:00', 'Z'), 'percent': .4},
                  {'label': 'Fable Weekly', 'title': 'Fable Weekly', 'resetsAt': reset.isoformat(), 'percent': 0},
                  {'label': 'Weekly (7-day)', 'resetsAt': (self.now - dt.timedelta(hours=1)).isoformat(), 'percent': 1},
                  {'label': 'Monthly', 'percent': .5},
                  {'label': 'Auto', 'resetsAt': reset.isoformat(), 'percent': .5}]
        counts = c.limit_window_tokens(self.ledger, self.cfg, {'mine': ({'codex'}, 'local', limits),
                                                               'work': ({'codex'}, 'work', limits[:2])}, self.now)
        self.assertEqual({index: tokens for index, (_, tokens) in counts['mine'].items()}, {0: 100, 1: 120})
        self.assertEqual(counts['mine'][0][0], int((reset - dt.timedelta(hours=5)).timestamp()))
        self.assertEqual({index: tokens for index, (_, tokens) in counts['work'].items()}, {0: 7300, 1: 7300})

    def test_panel_snapshot_and_cards_carry_window_tokens(self):
        self.add_at('local', 'local', 1, 100)
        self.add_at('work', 'work', 1, 40)
        state = self.root / 'state/ai-usage'
        usage = state.parent / 'agents/usage'
        usage.mkdir(parents=True)
        reset = (self.now + dt.timedelta(days=2)).isoformat()
        weekly = [{'label': 'Weekly (7-day)', 'percent': .3, 'resetsAt': reset}]
        (usage / 'codex.json').write_text(json.dumps({'id': 'codex', 'name': 'Codex', 'limits': weekly}))
        # Matched to its account by name, the way the dashboard finds its limits.
        (usage / 'codex-work.json').write_text(json.dumps({'id': 'codex-work', 'name': 'work', 'limits': weekly}))
        (usage / 'stranger.json').write_text(json.dumps({'id': 'stranger', 'name': 'Nobody', 'limits': weekly}))
        with patch.object(c, 'STATE', state):
            snapshot = c.hourly_snapshot(self.ledger, self.now, self.cfg)
            with patch.object(c, 'quota', return_value={'limits': weekly}), patch.object(c, 'theme', return_value={}):
                cards = {card['accountId']: card for card in c.report(self.ledger, self.cfg, now=self.now)['cards']}
        self.assertEqual(set(snapshot['limitTokens']), {'codex', 'codex-work'})
        self.assertEqual(snapshot['limitTokens']['codex'], [{'label': 'Weekly (7-day)', 'resetsAt': reset,
            'since': int((self.now - dt.timedelta(days=5)).timestamp()), 'tokens': 100}])
        self.assertEqual(snapshot['limitTokens']['codex-work'][0]['tokens'], 40)
        self.assertEqual(cards['local']['quota']['limits'][0]['tokens'], 100)
        self.assertNotIn('tokens', weekly[0])

    def test_hourly_snapshot_splits_each_record_by_account(self):
        self.add_at('local', 'local', 1, 50)
        self.add_at('work', 'work', 1, 100)
        self.add_at('personal', 'personal', 2, 300)
        self.add_at('copy', 'local', 1, 7)
        self.add_at('copy', 'work', 1, 7)
        # A second Personal login that has never produced history, and a
        # record for a login that is not configured here at all.
        self.cfg['accounts'].append({'id': 'idle', 'label': 'Idle', 'directories': [{'provider': 'codex', 'path': str(self.root / 'idle')}]})
        state = self.root / 'state/ai-usage'
        usage = state.parent / 'agents/usage'
        usage.mkdir(parents=True)
        for name in ('codex', 'work', 'idle'):
            (usage / f'{name}.json').write_text(json.dumps({'id': name, 'name': name.title(), 'limits': []}))
        (usage / 'personal.json').write_text(json.dumps({'id': 'personal', 'name': 'Personal'}))
        (usage / 'stranger.json').write_text(json.dumps({'id': 'stranger', 'name': 'Nobody'}))
        with patch.object(c, 'STATE', state):
            snapshot = c.hourly_snapshot(self.ledger, self.now, self.cfg)
        totals = {key: entry['tokens'] for key, entry in snapshot['sources'].items()}
        # The provider holds every login; each record holds only its own.
        self.assertEqual(snapshot['providers']['codex']['tokens'], 457)
        self.assertEqual(totals, {'codex': 50, 'work': 107, 'personal': 300, 'idle': 0})
        hours = {h['label']: h for h in snapshot['hours']}
        self.assertEqual(hours['16:00']['sources'], {'personal': 300})
        self.assertEqual(hours['17:00']['sources'], {'codex': 50, 'work': 107})
        # A login with no history reads as unknown to the ledger; one that has
        # only been idle today does not.
        self.assertEqual(snapshot['availableSources'], ['codex', 'personal', 'work'])
        self.assertIn('codex', snapshot['availableProviders'])

    def test_hourly_snapshot_keeps_plain_provider_ids_without_records(self):
        self.add('a', 'local', 40)
        with patch.object(c, 'STATE', self.root / 'state/ai-usage'):
            snapshot = c.hourly_snapshot(self.ledger, self.now, self.cfg)
        self.assertEqual(snapshot['sources'], {})
        self.assertEqual(snapshot['availableSources'], ['codex'])
        self.assertEqual(snapshot['providers']['codex']['tokens'], 40)

    def test_session_averages_and_priced_share(self):
        self.add('a', 'local', 100, 'one')
        self.add('b', 'local', 300, 'one')
        self.add('c', 'local', 200, 'two')
        data = self.report()
        self.assertEqual(data['summary']['sessions'], 2)
        self.assertEqual(data['summary']['tokensPerSession'], 300)
        self.assertAlmostEqual(data['summary']['valuePerSession'], data['summary']['value'] / 2)
        self.assertEqual(data['providers'][0]['valueShare'], 100)

    def test_missing_provenance_is_unassigned(self):
        self.ledger.put(c.record('old', 'codex', 'old', self.now.isoformat(), 'gpt-4.1', '', 'CLI', input=100))
        self.assertEqual(self.report('unassigned')['summary']['tokens'], 100)
        self.assertEqual(self.report('local')['summary']['tokens'], 0)

    def test_settings_validate_before_writing(self):
        with patch.object(c, 'CONFIG', self.root / 'settings.json'):
            saved = c.save_settings(self.cfg)
            self.assertEqual(saved['accounts'][0]['label'], 'Work')
            self.cfg['accounts'][1]['directories'] = self.cfg['accounts'][0]['directories']
            with self.assertRaisesRegex(ValueError, 'same source folder'):
                c.save_settings(self.cfg)
            self.assertEqual(c.settings()['accounts'], saved['accounts'])

    def test_named_directories_are_scanned_incrementally(self):
        import json
        home = self.root / 'work'
        path = home / 'sessions/test.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text('\n'.join(json.dumps(r) for r in [
            {'type': 'session_meta', 'payload': {'id': 'stable-account-session'}},
            {'type': 'event_msg', 'timestamp': self.now.isoformat(), 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'input_tokens': 100, 'output_tokens': 20},
                'total_token_usage': {'input_tokens': 100, 'output_tokens': 20}}}}]))
        with patch.object(c, 'HOME', self.root), patch.dict('os.environ', {
            'CODEX_HOME': str(self.root / 'local'), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'GROK_HOME': str(self.root / 'grok'), 'PI_CODING_AGENT_DIR': str(self.root / 'pi'),
            'XDG_DATA_HOME': str(self.root / 'data')}):
            self.ledger.scan(self.cfg)
            self.ledger.scan(self.cfg)
        self.assertEqual(self.report('work')['summary']['tokens'], 120)
        self.assertEqual(self.report()['summary']['tokens'], 120)

    def test_provenance_migration_preserves_events_and_reindexes_files(self):
        self.add('old', 'work')
        self.ledger.db.execute("DELETE FROM metadata WHERE key='provenanceVersion'")
        self.ledger.db.execute("INSERT INTO files VALUES ('old',1,1)")
        self.ledger.db.commit()
        other = c.Ledger(self.root / 'usage.sqlite')
        self.assertEqual(other.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        self.assertEqual(other.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
        other.db.close()
