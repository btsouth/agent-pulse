#!/usr/bin/env python3
"""Run the dashboard with generated data in a temporary, isolated home."""
import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import time

ROOT=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--view', choices=['overview', 'settings', 'accounts', 'account', 'reset', 'today', 'hour', 'filters', 'sources', 'breakdown'], default='overview')
parser.add_argument('--capture',type=Path,help='render a PNG offscreen and exit')
parser.add_argument('--theme', choices=['dark', 'light'], default='dark', help='synthetic preview palette')
parser.add_argument('--clock-format', choices=['12', '24'], default='12', help='synthetic Omarchy bar clock format')
parser.add_argument('--agents',help='comma-separated providers to enable, default all')
parser.add_argument('--width',type=int,default=1200,help='dashboard width, at least 1000')
args=parser.parse_args()
with tempfile.TemporaryDirectory(prefix='usage-dashboard-demo-') as tmp:
    base=Path(tmp);env=dict(os.environ)
    env.update(HOME=tmp,XDG_STATE_HOME=str(base/'state'),XDG_CONFIG_HOME=str(base/'config'),
               XDG_DATA_HOME=str(base/'data'),CODEX_HOME=str(base/'codex'),CLAUDE_CONFIG_DIR=str(base/'claude'),GROK_HOME=str(base/'grok'),PI_CODING_AGENT_DIR=str(base/'pi'),
               MUSE_HOME=str(base/'muse'),CURSOR_HOME=str(base/'cursor'),AI_USAGE_ROOT=str(ROOT),AI_USAGE_DEMO='1')
    if args.capture: env.update(QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software')
    shell_config=base/'config/omarchy/shell.json'
    shell_config.parent.mkdir(parents=True,exist_ok=True)
    shell_config.write_text(json.dumps({'version':1,'bar':{'position':'top','layout':{'center':[
        {'id':'omarchy.clock','format':'ddd d MMM h:mm AP' if args.clock_format=='12' else 'ddd d MMM HH:mm'}]}}}))
    os.environ.update(env)
    spec=importlib.util.spec_from_file_location('demo_collector',ROOT/'collector.py');c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
    if args.theme == 'light':
        theme = c.STATE.parent / 'current/theme/colors.toml'
        theme.parent.mkdir(parents=True, exist_ok=True)
        theme.write_text('background = "#faf7f0"\nforeground = "#292d32"\naccent = "#28654a"\nlighter_background = "#dce5df"\n')
    providers=[p for p in (args.agents.split(',') if args.agents else c.PROVIDERS) if p in c.PROVIDERS] or list(c.PROVIDERS)
    c.atomic_json(c.CONFIG, c.DEFAULTS | {'enabled': providers, 'accounts': [{'id':'work','label':'Work','directories':[{'provider':p,'path':str(base/'work'/p)} for p in providers]}]})
    for provider, name, label, percent, days in [
        ('codex', 'Codex limits', 'Weekly (7-day)', 0.26, 2),
        ('claude', 'Claude', 'Weekly (7-day)', 0.58, 3),
        ('opencode-go', 'OpenCode Go', 'Weekly', 0.71, 5),
    ]:
        c.save_pinned_limit(provider, label, 'Weekly')
        c.atomic_json(c.STATE.parent / ('agents/usage/' + provider + '.json'), {
            'id': provider, 'name': name,
            **({'chatgptCredits': {'remaining': 62500, 'spent': 1500, 'estimated': True,
                  'trackingSince': '2026-09-30T00:00:00Z'}} if provider == 'codex' else {}),
            'limits': [{'label': label, 'percent': percent,
                        'resetsAt': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat()}]})
    ledger=c.Ledger(c.STATE/'usage.sqlite');rng=random.Random(7);now=dt.datetime.now().astimezone()
    for offset in range(7):
        date=now.date()-dt.timedelta(days=offset)
        for provider,model in [('codex','gpt-4.1'),('claude','claude-sonnet-4-20250514'),('opencode-go','glm-5.3-flash'),('grok','grok-demo'),('gemini','gemini-3.8-flash'),('opencode','example-model'),('pi','example-model'),('omp','example-model'),('muse','muse-spark-1.3-contributor'),('ollama-cloud','deepseek-v4.1-flash'),('commandcode','deepseek/deepseek-v4.1-flash'),('clinepass','cline-pass/deepseek-v4.1-flash'),('cursor','composer-2.5')]:
            count=rng.randint(15,50)
            for index in range(count):
                # Exercise the entire day, including empty hours, in UI captures.
                when=dt.datetime.combine(date,dt.time()).astimezone()+dt.timedelta(minutes=index*1439//max(1,count-1))
                if when>now:continue
                entry = c.record(f'{provider}-{offset}-{index}',provider,f'{provider}-{offset}-{index//8}',when.isoformat(),model,
                    '/demo/'+rng.choice(['website','notes','weather']),'CLI' if provider=='codex' else 'Claude Code' if provider=='claude' else 'Muse' if provider=='muse' else 'Hermes' if provider=='ollama-cloud' else 'Hermes' if provider in ('commandcode','clinepass') else 'Cursor' if provider=='cursor' else 'OpenCode',
                    input=rng.randint(500,2000),output=rng.randint(100,1000),cacheRead=rng.randint(10000,100000))
                if provider == 'cursor':
                    # Cloud-API shape: token-bearing rows with a list-price
                    # estimate, one turn each. Values are synthetic.
                    entry['turns'] = 1
                    entry['reportedValue'] = round((entry['input'] + entry['output']) * 0.00001, 6)
                if provider in ('ollama-cloud', 'commandcode', 'clinepass'):
                    entry['timePrecision'] = 'session'
                ledger.put(entry, base/('work' if index % 2 else 'local')/provider/'sessions/demo.jsonl')
    ledger.db.execute("UPDATE events SET reportedCostTicks=120000000,modelCalls=3 WHERE provider='grok'")
    ledger.db.execute("UPDATE events SET reportedValue=0.012,apiProvider='example-provider' WHERE provider IN ('opencode','pi','omp')")
    ledger.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('scan', json.dumps(
        {'sources': [], 'warnings': [], 'scannedAt': time.time(), 'machine': 'demo-computer'})))
    # A topped-up wallet so the capture smoke renders the balance line on a
    # provider card. Every figure here is synthetic.
    c.atomic_json(c.STATE / 'commandcode-quota.json', {
        'limits': [{'label': '5 hours', 'percent': 0.25, 'raw': 0.25, 'resetsAt': ''},
                   {'label': 'Weekly', 'percent': 0.1, 'raw': 0.1, 'resetsAt': ''},
                   {'label': 'Monthly', 'percent': 0.5, 'raw': 0.5, 'resetsAt': ''}],
        'balance': {'label': 'Extra credits', 'remaining': 8.47, 'spent': 1.53, 'funded': 10.0,
                    'currency': 'USD', 'estimated': True},
        'updatedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'error': '', 'plan': 'GOAT'})
    ledger.db.commit()
    c.atomic_json(c.STATE / 'hourly-summary.json', c.hourly_snapshot(ledger))
    ledger.db.close()
    # Copy QML into an isolated path so IPC cannot target the real dashboard.
    import shutil
    ui=base/'ui';shutil.copytree(ROOT/'ui',ui)
    shell = ui / 'shell.qml'
    shell.write_text(shell.read_text().replace('implicitWidth: 1200', 'implicitWidth: ' + str(max(1000, args.width))))
    proc=subprocess.Popen(['quickshell','-p',str(ui),'--no-color'],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    try:
        if args.capture:
            output=args.capture.resolve();output.parent.mkdir(parents=True,exist_ok=True)
            output.unlink(missing_ok=True)
            for _ in range(100):
                if proc.poll() is not None:raise RuntimeError(proc.stdout.read())
                result=subprocess.run(['quickshell','ipc','-p',str(ui),'--any-display','call','analytics','capture',str(output)],capture_output=True,text=True,env=env)
                if output.exists():break
                time.sleep(0.1)
            if not output.exists():raise RuntimeError('Timed out rendering demo')
            # Allow the initial report to finish, then capture the populated view.
            time.sleep(1)
            if args.view != 'overview':
                command = (['accountView', 'codex:local'] if args.view in ('account', 'reset') else
                           ['preferences'] if args.view == 'settings' else
                           ['period', '1'] if args.view == 'today' else
                           ['firstHour'] if args.view == 'hour' else
                           ['filters'] if args.view == 'filters' else ['account', 'work'])
                if args.view == 'sources': command = ['scrollTo', '750']
                if args.view == 'breakdown': command = ['scrollTo', '1550']
                if args.view == 'reset':
                    subprocess.run(['quickshell','ipc','-p',str(ui),'--any-display','call','analytics',*command],capture_output=True,text=True,env=env,check=True)
                    time.sleep(.5)
                    command = ['resetWindow', c.digest('Weekly (7-day)', '')]
                subprocess.run(['quickshell','ipc','-p',str(ui),'--any-display','call','analytics',*command],check=True,env=env)
                time.sleep(0.8)
            subprocess.run(['quickshell','ipc','-p',str(ui),'--any-display','call','analytics','capture',str(output)],check=True,env=env)
            time.sleep(0.3);print(output)
        else:
            print('Demo data only. Press Ctrl+C here to exit.')
            proc.wait()
    except KeyboardInterrupt: pass
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        log = proc.stdout.read()
        if 'ReferenceError' in log or 'TypeError' in log or 'Unable to assign' in log or 'Failed to load' in log:
            raise RuntimeError(log)
