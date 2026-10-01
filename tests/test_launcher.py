import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class LauncherTests(unittest.TestCase):
    def run_launcher(self, ipc=True, legacy=False, account=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['quickshell', 'hyprctl']:
                script = root / name
                script.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys
with open(os.environ['LAUNCH_LOG'], 'a') as out:
    out.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]])+'\\n')
if pathlib.Path(sys.argv[0]).name == 'quickshell' and sys.argv[1] == 'ipc':
    if os.environ['IPC_OK'] == '0': sys.exit(1)
    print('54321')
elif pathlib.Path(sys.argv[0]).name == 'quickshell' and os.environ.get('AI_USAGE_ACCOUNT_RECORD'):
    with open(os.environ['LAUNCH_LOG'], 'a') as out:
        out.write(json.dumps(['account', os.environ['AI_USAGE_ACCOUNT_RECORD']])+'\\n')
if pathlib.Path(sys.argv[0]).name == 'hyprctl' and os.environ['LEGACY'] == '1' and 'hl.dsp.focus' in sys.argv[2]:
    sys.exit(1)
''')
                script.chmod(0o755)
            env = dict(os.environ, PATH=str(root)+':'+os.environ['PATH'], LAUNCH_LOG=str(root/'calls'),
                       IPC_OK=str(int(ipc)), LEGACY=str(int(legacy)))
            command = ['bash', str(Path(__file__).parents[1]/'launch.sh')]
            if account: command += ['--account-record', account]
            subprocess.run(command, env=env, check=True)
            return [json.loads(line) for line in (root/'calls').read_text().splitlines()]

    def test_existing_instance_is_focused_by_pid(self):
        calls = self.run_launcher()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], ['hyprctl', 'dispatch', 'hl.dsp.focus({ window = "pid:54321" })'])

    def test_legacy_hyprland_focus_fallback(self):
        calls = self.run_launcher(legacy=True)
        self.assertEqual(calls[-1], ['hyprctl', 'dispatch', 'focuswindow', 'pid:54321'])

    def test_missing_instance_launches_new_window(self):
        calls = self.run_launcher(ipc=False)
        self.assertEqual(calls[-1][:4], ['quickshell', '-d', '-n', '-p'])
        self.assertFalse(any(c[0] == 'hyprctl' for c in calls))

    def test_selected_account_is_forwarded_to_existing_window(self):
        calls = self.run_launcher(account='codex-second')
        self.assertEqual(calls[1][-4:], ['call', 'analytics', 'agent', 'codex-second'])

    def test_selected_account_is_forwarded_to_new_window(self):
        calls = self.run_launcher(ipc=False, account='codex-second')
        self.assertEqual(calls[-1], ['account', 'codex-second'])
