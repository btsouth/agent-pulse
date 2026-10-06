import os
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1]
REFRESH = ROOT / 'refresh.sh'


class RefreshTests(unittest.TestCase):
    def run_refresh(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin = tmp / 'bin'
            bin.mkdir()
            capture = tmp / 'args'
            python_capture = tmp / 'python'
            # The packaged updater tolerates a failing collector the same way
            # refresh.sh does; exit nonzero here to prove the failure cannot
            # leak into refresh.sh's own exit status.
            updater = bin / 'omarchy-agent-usage-update'
            updater.write_text('#!/bin/bash\necho "$@" >> "$CAPTURE"\nexit 3\n')
            updater.chmod(0o755)
            python = bin / 'python3'
            python.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$PYTHON_CAPTURE"\nexit 0\n')
            python.chmod(0o755)
            env = dict(os.environ, PATH=str(bin) + ':/usr/bin:/bin', CAPTURE=str(capture),
                       PYTHON_CAPTURE=str(python_capture),
                       HOME=str(tmp), XDG_CONFIG_HOME=str(tmp / 'config'),
                       XDG_STATE_HOME=str(tmp / 'state'), XDG_DATA_HOME=str(tmp / 'data'))
            result = subprocess.run(['bash', str(REFRESH), *args],
                                    env=env, capture_output=True, text=True)
            forwarded = capture.read_text().strip() if capture.exists() else ''
            calls = python_capture.read_text().splitlines() if python_capture.exists() else []
        return result, forwarded, [Path(call.split()[0]).name for call in calls]

    def test_forwards_panel_arguments_to_updater(self):
        result, forwarded, calls = self.run_refresh('--limits-only', '--except', 'grok', 'codex')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(forwarded, '--limits-only --except grok codex')
        self.assertEqual(calls, ['codex_limits.py'])

    def test_forwards_force_and_limit_flags_verbatim(self):
        result, forwarded, calls = self.run_refresh('--force', '--limits-only')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(forwarded, '--force --limits-only')
        self.assertEqual(calls, ['claude_limits.py', 'codex_limits.py', 'collector.py'])

    def test_targeted_claude_limits_only_skips_scan_and_codex(self):
        result, forwarded, calls = self.run_refresh('--limits-only', 'claude')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(forwarded, '--limits-only claude')
        self.assertEqual(calls, ['claude_limits.py'])

    def test_targeted_codex_limits_only_skips_scan_and_claude(self):
        result, forwarded, calls = self.run_refresh('--limits-only', 'codex-second')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(forwarded, '--limits-only codex-second')
        self.assertEqual(calls, ['codex_limits.py'])

    def test_untargeted_and_full_refreshes_still_run_everything(self):
        for args in (('--limits-only',), ('claude',)):
            with self.subTest(args=args):
                result, forwarded, calls = self.run_refresh(*args)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(forwarded, ' '.join(args))
                self.assertEqual(calls, ['claude_limits.py', 'codex_limits.py', 'collector.py'])

    def test_except_value_is_not_an_agent_id(self):
        result, forwarded, calls = self.run_refresh('--limits-only', '--except', 'codex', 'claude')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(forwarded, '--limits-only --except codex claude')
        self.assertEqual(calls, ['claude_limits.py'])


if __name__ == '__main__':
    unittest.main()
