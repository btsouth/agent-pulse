import json
import os
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1]
NOTIFIER = ROOT / 'notify-resets.sh'

INCLUDED = ['Weekly', 'Weekly (7-day)', 'Monthly']
SHORT = ['Session (5-hour)', '5 hours', '5h window', '30m window']


class NotifyResetsTests(unittest.TestCase):
    def run_notifier(self, state, *args, before=0.9):
        stub = state / 'bin'
        stub.mkdir(exist_ok=True)
        calls = state / 'calls'
        sender = stub / 'notify-send'
        sender.write_text('#!/bin/bash\necho "$@" >> "$FAKE_LOG"\n')
        sender.chmod(0o755)
        env = dict(os.environ, XDG_STATE_HOME=str(state), FAKE_LOG=str(calls),
                   PATH=str(stub) + os.pathsep + os.environ['PATH'])
        for run in ('2026-09-19T10:00:00+00:00', '2026-09-19T15:00:01+00:00'):
            percent = before if '10:00' in run else 0.1
            (state / 'omarchy/agents/usage/p.json').write_text(json.dumps(
                {'id': 'p', 'name': 'P', 'resetCreditsAvailable': 0,
                 'limits': [{'label': self.label, 'percent': percent, 'resetsAt': run}]}))
            result = subprocess.run(['bash', str(NOTIFIER), '--provider', 'p', *args],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
        return calls.read_text() if calls.exists() else ''

    def check(self, label, notified, before=0.9):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / 'omarchy/agents/usage').mkdir(parents=True)
            self.label = label
            out = self.run_notifier(state, before=before)
        self.assertEqual(bool(out.strip()), notified, (label, before))
        return out

    def test_long_windows_notify(self):
        for label in INCLUDED:
            self.check(label, True)
            self.check(label, True, before=0.2)

    def test_short_windows_notify_only_when_nearly_spent(self):
        for label in SHORT:
            self.check(label, False, before=0.89)
            self.check(label, True, before=0.9)
        out = self.check('Session (5-hour)', True, before=1.0)
        self.assertIn('Session (5-hour): was 100%, now 10%', out)

    def run_session(self, records):
        # Each record is (limits, ...) for one run of the Claude notifier; the
        # first reset time is already past, as it is once a window has reset.
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            usage = state / 'omarchy/agents/usage'; usage.mkdir(parents=True)
            stub = state / 'bin'; stub.mkdir()
            sender = stub / 'notify-send'
            sender.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$FAKE_LOG"\n')
            sender.chmod(0o755)
            calls = state / 'calls'
            env = dict(os.environ, HOME=tmp, XDG_STATE_HOME=tmp,
                       FAKE_LOG=str(calls), PATH=str(stub) + os.pathsep + os.environ['PATH'])
            for limits in records:
                (usage / 'claude.json').write_text(json.dumps(
                    {'id': 'claude', 'name': 'Claude Code', 'limits': limits}))
                subprocess.run(['bash', str(NOTIFIER)], env=env, check=True, capture_output=True)
            return calls.read_text().splitlines() if calls.exists() else []

    def test_session_reset_notifies_once_when_the_window_leaves_the_record(self):
        full = [{'label': 'Session (5-hour)', 'percent': 1.0, 'resetsAt': '2026-09-19T10:00:00+00:00'}]
        self.assertEqual(len(self.run_session([full, [], []])), 1)
        cleared = [{'label': 'Session (5-hour)', 'percent': 0, 'resetsAt': None}]
        lines = self.run_session([full, cleared, cleared])
        self.assertEqual(len(lines), 1)
        self.assertIn('Session (5-hour): was 100%, now 0%', lines[0])

    def test_stale_session_record_waits_for_the_new_window(self):
        full = [{'label': 'Session (5-hour)', 'percent': 1.0, 'resetsAt': '2026-09-19T10:00:00+00:00'}]
        fresh = [{'label': 'Session (5-hour)', 'percent': 0.03, 'resetsAt': '2026-09-19T15:40:00+00:00'}]
        self.assertEqual(self.run_session([full, full]), [])
        self.assertEqual(len(self.run_session([full, full, fresh, fresh])), 1)

    def test_session_before_its_reset_time_stays_silent(self):
        full = [{'label': 'Session (5-hour)', 'percent': 1.0, 'resetsAt': '2099-01-01T10:00:00+00:00'}]
        self.assertEqual(self.run_session([full, []]), [])

    def test_banked_credits_notify_once_and_do_not_opt_in_other_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            usage = state / 'omarchy/agents/usage'; usage.mkdir(parents=True)
            stub = state / 'bin'; stub.mkdir()
            sender = stub / 'notify-send'
            sender.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$FAKE_LOG"\n')
            sender.chmod(0o755)
            calls = state / 'calls'
            env = dict(os.environ, HOME=tmp, XDG_STATE_HOME=tmp,
                       FAKE_LOG=str(calls), PATH=str(stub) + os.pathsep + os.environ['PATH'])
            def run(credits):
                for provider in ('codex', 'cursor'):
                    (usage / (provider + '.json')).write_text(json.dumps(
                        {'id': provider, 'name': provider, 'resetCreditsAvailable': credits, 'limits': []}))
                subprocess.run(['bash', str(NOTIFIER)], env=env, check=True, capture_output=True)
            run(1); self.assertFalse(calls.exists())
            run(2); run(2); run(1)
            lines = calls.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn('Banked resets: 1 -> 2', lines[0])
            self.assertNotIn('cursor', lines[0])

    def test_lock_holder_skips_second_run(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            usage = state / 'omarchy/agents/usage'
            usage.mkdir(parents=True)
            (usage / 'codex.json').write_text(json.dumps(
                {'id': 'codex', 'name': 'Codex',
                 'limits': [{'label': 'Weekly', 'percent': 0.9,
                             'resetsAt': '2026-09-19T10:00:00+00:00'}]}))
            lock = open(state / 'omarchy/agents/reset-snapshot.lock', 'w')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            env = dict(os.environ, XDG_STATE_HOME=str(state))
            result = subprocess.run(['bash', str(NOTIFIER)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertFalse((state / 'omarchy/agents/reset-snapshot.json').exists())
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


if __name__ == '__main__':
    unittest.main()
