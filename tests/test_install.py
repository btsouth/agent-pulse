import json
import os
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).parents[1]
APP='omarchy-usage-dashboard'
PLUGIN='community.ai-usage-dashboard'

def with_stub(env,home):
    folder=home/'test-bin';folder.mkdir()
    stub=folder/'quickshell';stub.write_text('#!/bin/sh\nexit 0\n');stub.chmod(0o755)
    env['PATH']=str(folder)+os.pathsep+env['PATH']
    return env
class InstallationTests(unittest.TestCase):
    def test_clean_install_repeat_upgrade_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)/"home with $literal 'quotes'";home.mkdir()
            env=dict(os.environ,HOME=str(home),XDG_CONFIG_HOME=str(home/'config'),XDG_STATE_HOME=str(home/'state'),XDG_DATA_HOME=str(home/'data'))
            env=with_stub(env,home)
            shell=home/'config/omarchy/shell.json';shell.parent.mkdir(parents=True)
            original={'version':1,'bar':{'layout':{'right':[{'id':'omarchy.agents','custom':'keep'}]}}}
            shell.write_text(json.dumps(original))
            history=home/'state/omarchy/ai-usage/usage.sqlite';history.parent.mkdir(parents=True);history.write_bytes(b'keep metrics')
            prefs=home/'config/omarchy/ai-usage/settings.json';prefs.parent.mkdir(parents=True);prefs.write_text('{"monthlyPrices":{"codex":200}}')
            def run(*args,check=True): return subprocess.run(['python3',str(ROOT/'install.py'),*args,'--no-systemd'],env=env,capture_output=True,text=True,check=check)
            run('--with-plugin')
            registry=home/'state'/APP/'installation.json';before=registry.read_bytes()
            runtime=home/'data'/APP/'app';self.assertTrue((runtime/'catalog.json').exists())
            self.assertTrue((runtime/'muse-pricing.json').exists())
            self.assertTrue((runtime/'codex-pricing.json').exists())
            self.assertTrue((runtime/'claude-pricing.json').exists())
            self.assertTrue((runtime/'ollama-pricing.json').exists())
            self.assertTrue((runtime/'clinepass-pricing.json').exists())
            run('--with-plugin');self.assertEqual(before,registry.read_bytes())
            launcher=home/'.local/bin'/APP
            self.assertEqual(shlex.split(launcher.read_text().splitlines()[1])[1],str(runtime/"launch.sh"))
            desktop=home/'data/applications'/f'{APP}.desktop'
            subprocess.run(['desktop-file-validate',str(desktop)],check=True,capture_output=True)
            # A later edit must stop upgrade before any earlier planned file changes.
            (runtime/'collector.py').write_text('# keep my edit\n')
            result=run('--with-plugin',check=False);self.assertNotEqual(result.returncode,0)
            self.assertIn('Preserving locally edited file',result.stderr)
            self.assertEqual(before,registry.read_bytes())
            result=run('--uninstall');self.assertIn('Kept later edit',result.stdout)
            self.assertEqual((runtime/'collector.py').read_text(),'# keep my edit\n')
            self.assertFalse(launcher.exists())
            self.assertEqual(history.read_bytes(),b'keep metrics')
            self.assertEqual(json.loads(prefs.read_text())['monthlyPrices']['codex'],200)
            self.assertEqual(json.loads(shell.read_text()),original)

    def test_clash_warning_names_builtin_and_uninstall_removes_customized_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)/'home';home.mkdir()
            env=dict(os.environ,HOME=str(home),XDG_CONFIG_HOME=str(home/'config'),XDG_STATE_HOME=str(home/'state'),XDG_DATA_HOME=str(home/'data'))
            env=with_stub(env,home)
            # The built-in widget's manifest lives under OMARCHY_PATH on a real
            # install; stage a fixture copy so the check runs off this machine.
            agents=home/'omarchy/shell/plugins/agents';agents.mkdir(parents=True)
            (agents/'manifest.json').write_text(json.dumps(
                {'id':'omarchy.agents','barWidget':{'aliases':['agents','model-usage']}}))
            env['OMARCHY_PATH']=str(home/'omarchy')
            shell=home/'config/omarchy/shell.json';shell.parent.mkdir(parents=True)
            shell.write_text(json.dumps({'version':1,'bar':{'layout':{'right':[]}}}))
            def run(*args,check=True): return subprocess.run(['python3',str(ROOT/'install.py'),*args,'--no-systemd'],env=env,capture_output=True,text=True,check=check)
            result=run('--with-plugin');self.assertEqual(result.returncode,0)
            self.assertIn('omarchy.agents (built-in)',result.stdout)
            # A user who followed the settings advice customized the entry;
            # uninstall must still remove it by id.
            layout=json.loads(shell.read_text())
            layout['bar']['layout']['right'][0]['providerOrder']=['codex']
            shell.write_text(json.dumps(layout))
            run('--uninstall')
            self.assertEqual(json.loads(shell.read_text())['bar']['layout']['right'],[])

    def test_dashboard_only_does_not_touch_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            env=dict(os.environ,HOME=tmp,XDG_CONFIG_HOME=tmp+'/config',XDG_STATE_HOME=tmp+'/state',XDG_DATA_HOME=tmp+'/data')
            env=with_stub(env,Path(tmp))
            subprocess.run(['python3',str(ROOT/'install.py'),'--no-systemd'],env=env,capture_output=True,check=True)
            self.assertFalse(Path(tmp,'config/omarchy/shell.json').exists())
            subprocess.run(['python3',str(ROOT/'install.py'),'--uninstall','--no-systemd'],env=env,capture_output=True,check=True)
            self.assertFalse(Path(tmp,'data',APP,'app').exists())

class RecoveryTests(unittest.TestCase):
    def test_interrupted_upgrade_can_retry_without_losing_originals(self):
        import importlib.util
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('review_install',ROOT/'install.py')
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            env=with_stub(dict(os.environ,HOME=tmp,XDG_CONFIG_HOME=tmp+'/config',
                              XDG_STATE_HOME=tmp+'/state',XDG_DATA_HOME=tmp+'/data'),home)
            with patch.dict(os.environ,env), patch('sys.argv',['install.py','--no-systemd']):
                installer.main()
                runtime=home/'data'/APP/'app'
                registry=home/'state'/APP/'installation.json'
                managed=json.loads(registry.read_text())
                # Model a previous release whose first two files both changed.
                for name in ('collector.py','catalog.json'):
                    path=runtime/name;path.write_bytes(b'old release')
                    managed['files'][str(path)]['hash']=installer.digest(path.read_bytes())
                registry.write_text(json.dumps(managed))
                real_atomic=installer.atomic
                def interrupted(path,*args,**kwargs):
                    if path==runtime/'catalog.json': raise OSError('simulated disk failure')
                    return real_atomic(path,*args,**kwargs)
                with patch.object(installer,'atomic',interrupted):
                    with self.assertRaises(OSError): installer.main()
                self.assertEqual((runtime/'catalog.json').read_bytes(),b'old release')
                self.assertEqual((runtime/'collector.py').read_bytes(),(ROOT/'collector.py').read_bytes())
                installer.main()
                self.assertEqual((runtime/'catalog.json').read_bytes(),(ROOT/'catalog.json').read_bytes())
                self.assertTrue(all('pendingHash' not in e for e in json.loads(registry.read_text())['files'].values()))
            with patch.dict(os.environ,env), patch('sys.argv',['install.py','--no-systemd','--uninstall']):
                installer.main()
            self.assertFalse(runtime.exists())

    def test_archive_installer_cleans_extracted_files(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)/'home';home.mkdir()
            work=Path(tmp)/'work';work.mkdir()
            env=with_stub(dict(os.environ,HOME=str(home),XDG_CONFIG_HOME=str(home/'config'),
                              XDG_STATE_HOME=str(home/'state'),XDG_DATA_HOME=str(home/'data'),TMPDIR=str(work)),home)
            archive=Path(tmp)/'source.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                for source in ROOT.iterdir():
                    if source.suffix in ('.py','.json','.sh') or source.name in ('LICENSE','VERSION','ui','plugin','licenses'):
                        tar.add(source,arcname='source/'+source.name)
            env['OMARCHY_USAGE_TARBALL']=str(archive)
            subprocess.run(['bash',str(ROOT/'install.sh'),'--no-systemd'],env=env,check=True,capture_output=True)
            self.assertEqual(list(work.iterdir()),[])
            self.assertTrue((home/'.local/bin'/APP).exists())
            self.assertEqual((home/'data'/APP/'app/VERSION').read_text(), (ROOT/'VERSION').read_text())

    def test_installed_refresh_report_repeat_and_uninstall(self):
        import datetime as dt
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            env=dict(os.environ,HOME=tmp,AI_USAGE_DEMO='',OLLAMA_API_KEY='',COMMANDCODE_API_KEY='',CLINE_API_KEY='')
            env.update({key:str(home/key.lower()) for key in (
                'XDG_CONFIG_HOME','XDG_STATE_HOME','XDG_DATA_HOME','CODEX_HOME','CLAUDE_CONFIG_DIR',
                'GROK_HOME','PI_CODING_AGENT_DIR','MUSE_HOME','CURSOR_HOME')})
            env=with_stub(env,home)
            env['PATH']=str(home/'.local/bin')+os.pathsep+env['PATH']
            for name in ('omarchy-agent-usage-refresh','omarchy-agent-usage-update','notify-send'):
                stub=home/'test-bin'/name;stub.write_text('#!/bin/sh\nexit 0\n');stub.chmod(0o755)
            source=Path(env['CODEX_HOME'])/'sessions/fixture.jsonl';source.parent.mkdir(parents=True)
            source.write_text(json.dumps({'type':'event_msg','timestamp':dt.datetime.now(dt.timezone.utc).isoformat(),
                'payload':{'type':'token_count','info':{'last_token_usage':{'input_tokens':100,'cached_input_tokens':70,'output_tokens':20},
                    'total_token_usage':{'input_tokens':100,'output_tokens':20}}}})+'\n')
            subprocess.run(['python3',str(ROOT/'install.py'),'--with-plugin','--no-systemd'],env=env,check=True,capture_output=True)
            runtime=Path(env['XDG_DATA_HOME'])/APP/'app'
            # This installed-process fixture must not ask the real GitHub API.
            prefs=Path(env['XDG_CONFIG_HOME'])/'omarchy/ai-usage/settings.json'
            prefs.parent.mkdir(parents=True,exist_ok=True)
            prefs.write_text(json.dumps({'enabled':['codex'],'updateCheck':False}))
            for _ in range(2):
                subprocess.run([str(home/'.local/bin'/f'{APP}-refresh'),'--force'],env=env,check=True,capture_output=True)
                result=subprocess.run(['python3',str(runtime/'collector.py'),'report'],env=env,check=True,capture_output=True,text=True)
                report=json.loads(result.stdout)
                self.assertEqual(report['summary']['tokens'],120)
                self.assertEqual(report['summary']['requests'],1)
            subprocess.run(['python3',str(ROOT/'install.py'),'--uninstall','--no-systemd'],env=env,check=True,capture_output=True)
            self.assertTrue((Path(env['XDG_STATE_HOME'])/'omarchy/ai-usage/usage.sqlite').exists())
            self.assertFalse(runtime.exists())
