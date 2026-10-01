#!/usr/bin/env python3
"""Install a user-owned dashboard; never edit packaged Omarchy files."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
APP = 'omarchy-usage-dashboard'
PLUGIN = 'community.ai-usage-dashboard'

def digest(data): return hashlib.sha256(data).hexdigest()

def atomic(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out: out.write(data)
        os.chmod(temporary,mode); os.replace(temporary,path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def encode_exec(path, systemd=False):
    value=str(path).replace('\\','\\\\').replace('"','\\"')
    if systemd: return value.replace('%','%%').replace('$','$$')
    return value.replace('`','\\`').replace('$','\\$').replace('%','%%')

def clashing_widgets(config):
    """Installed plugins that claim the same bar aliases as this one."""
    names=[]
    for manifest in sorted(config.glob('omarchy/plugins/*/manifest.json')):
        if manifest.parent.name==PLUGIN: continue
        try: data=json.loads(manifest.read_text())
        except (OSError, ValueError): continue
        widget=data.get('barWidget') or {}
        if 'model-usage' in (widget.get('aliases') or []): names.append(manifest.parent.name)
    try: first=json.loads((Path(os.environ.get('OMARCHY_PATH', '/usr/share/omarchy'))/'shell/plugins/agents/manifest.json').read_text())
    except (OSError, ValueError): first={}
    if 'model-usage' in ((first.get('barWidget') or {}).get('aliases') or []):
        names.append('omarchy.agents (built-in)')
    return names

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-plugin', action='store_true', help='install and add a separate bar widget; preserve existing widgets')
    parser.add_argument('--uninstall', action='store_true', help='restore managed files; keep data, settings, and later edits')
    parser.add_argument('--no-systemd', action='store_true', help='stage files without controlling user services')
    args=parser.parse_args()
    home=Path.home(); config=Path(os.environ.get('XDG_CONFIG_HOME',home/'.config'))
    data=Path(os.environ.get('XDG_DATA_HOME',home/'.local/share'))
    state=Path(os.environ.get('XDG_STATE_HOME',home/'.local/state'))/APP
    registry=state/'installation.json';runtime=data/APP/'app'
    managed=json.loads(registry.read_text()) if registry.exists() else {'files':{},'barEntry':None,'timer':False}
    def save_registry(): atomic(registry,(json.dumps(managed,indent=2)+'\n').encode(),0o600)
    def ctl(*words):
        if not args.no_systemd: subprocess.run(['systemctl','--user',*words],check=True)
    shell_path=config/'omarchy/shell.json'
    if args.uninstall:
        if not registry.exists(): print('No managed installation found.'); return
        if managed['timer']: ctl('disable','--now',APP+'.timer')
        if managed['barEntry'] and shell_path.exists():
            shell=json.loads(shell_path.read_text())
            entry_id=managed['barEntry'].get('id') if isinstance(managed['barEntry'],dict) else None
            for entries in shell.get('bar',{}).get('layout',{}).values():
                if isinstance(entries,list): entries[:]=[e for e in entries if not (isinstance(e,dict) and e.get('id')==entry_id)]
            atomic(shell_path,(json.dumps(shell,indent=2)+'\n').encode())
        retained=[]
        for key,entry in list(managed['files'].items()):
            path=Path(key)
            if path.is_symlink() or (path.exists() and digest(path.read_bytes()) not in (entry['hash'],entry.get('pendingHash'))):
                retained.append(key); continue
            if entry['original'] is None: path.unlink(missing_ok=True)
            else: atomic(path,base64.b64decode(entry['original']),entry['mode'])
            del managed['files'][key]
        # Prune only empty directories created for this application.
        for folder in [runtime,config/'omarchy/plugins'/PLUGIN]:
            if folder.exists():
                for child in sorted(folder.rglob('*'),key=lambda x:len(x.parts),reverse=True):
                    if child.is_dir():
                        try: child.rmdir()
                        except OSError: pass
                try: folder.rmdir()
                except OSError: pass
        managed['timer']=False;managed['barEntry']=None
        if retained: save_registry()
        else: registry.unlink()
        ctl('daemon-reload')
        print('Uninstalled managed files. Usage history and preferences preserved.')
        for path in retained: print('Kept later edit:',path)
        return
    for command in ['python3','quickshell']:
        if not shutil.which(command): parser.error('Missing dependency: '+command)
    plan={}
    def add(path,content,mode=0o644): plan[path]=(content.encode() if isinstance(content,str) else content,mode)
    for name in ['collector.py','codex_limits.py','claude_limits.py','catalog.json','pricing.json','muse-pricing.json','codex-pricing.json','claude-pricing.json','ollama-pricing.json','commandcode-pricing.json','clinepass-pricing.json','launch.sh','refresh.sh','notify-resets.sh','LICENSE']:
        add(runtime/name,(ROOT/name).read_bytes(),0o755 if name.endswith('.sh') else 0o644)
    for folder in ['ui','licenses']:
        for source in (ROOT/folder).rglob('*'):
            if source.is_file(): add(runtime/source.relative_to(ROOT),source.read_bytes())
    for name,script in [(APP,'launch.sh'),(APP+'-refresh','refresh.sh'),(APP+'-notify-resets','notify-resets.sh')]:
        add(home/'.local/bin'/name,'#!/bin/bash\nexec '+shlex.quote(str(runtime/script))+' "$@"\n',0o755)
    add(data/'applications'/f'{APP}.desktop','[Desktop Entry]\nType=Application\nName=Agent Pulse\nComment=Local coding-agent token history and comparisons\nExec="'+encode_exec(home/'.local/bin'/APP)+'"\nIcon=utilities-system-monitor\nTerminal=false\nCategories=Utility;\n')
    add(config/'systemd/user'/f'{APP}.service','[Unit]\nDescription=Refresh Agent Pulse\n[Service]\nType=oneshot\nExecStartPre=-/usr/bin/python3 "'+encode_exec(runtime/'codex_limits.py',True)+'"\nExecStart=/usr/bin/python3 "'+encode_exec(runtime/'collector.py',True)+'" scan\nExecStartPost="'+encode_exec(runtime/'notify-resets.sh',True)+'"\n')
    add(config/'systemd/user'/f'{APP}.timer','[Unit]\nDescription=Refresh Agent Pulse every 15 minutes\n[Timer]\nOnStartupSec=2min\nOnUnitActiveSec=15min\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
    shell=None;bar_entry=None
    if args.with_plugin:
        for source in (ROOT/'plugin').rglob('*'):
            if source.is_file(): add(config/'omarchy/plugins'/PLUGIN/source.relative_to(ROOT/'plugin'),source.read_bytes())
        shell=json.loads(shell_path.read_text()) if shell_path.exists() else {'version':1,'bar':{'layout':{'right':[]}}}
        layout=shell.setdefault('bar',{}).setdefault('layout',{})
        if not any(e.get('id')==PLUGIN for entries in layout.values() if isinstance(entries,list) for e in entries):
            bar_entry={'id':PLUGIN};layout.setdefault('right',[]).append(bar_entry)
    # Preflight every destination before changing any managed file.
    for path,(content,mode) in plan.items():
        entry=managed['files'].get(str(path))
        if path.is_symlink(): parser.error('Refusing to replace symlink: '+str(path))
        if path.exists() and entry and digest(path.read_bytes()) not in (entry['hash'],entry.get('pendingHash')):
            parser.error('Preserving locally edited file: '+str(path))
        if path.exists() and not path.is_file(): parser.error('Destination is not a file: '+str(path))
    for path,(content,mode) in plan.items():
        key=str(path)
        if key not in managed['files']:
            managed['files'][key]={'original':base64.b64encode(path.read_bytes()).decode() if path.exists() else None,
                                  'mode':path.stat().st_mode & 0o777 if path.exists() else None}
        entry=managed['files'][key]
        entry.setdefault('hash',digest(path.read_bytes()) if path.exists() else digest(content))
        entry['pendingHash']=digest(content)
    # Persist rollback information before mutations, including partial-install recovery.
    managed['timer']=True
    if bar_entry: managed['barEntry']=bar_entry
    save_registry()
    for path,(content,mode) in plan.items():
        if not path.exists() or path.read_bytes()!=content: atomic(path,content,mode)
    if bar_entry: atomic(shell_path,(json.dumps(shell,indent=2)+'\n').encode())
    for entry in managed['files'].values():
        if 'pendingHash' in entry: entry['hash']=entry.pop('pendingHash')
    save_registry()
    ctl('daemon-reload');ctl('enable','--now',APP+'.timer')
    print('Installed Agent Pulse. Run:',home/'.local/bin'/APP)
    if args.with_plugin:
        print('Added a separate bar widget. Existing widgets and provider settings were preserved.')
        clashing=clashing_widgets(config)
        if clashing:
            print()
            print('These installed widgets also serve model usage and will show a second AI icon:')
            for name in clashing: print('  '+name)
            print('Remove them from your bar layout once this widget is in place. Settings you')
            print('customised there can be copied into the '+PLUGIN+' entry in shell.json.')

if __name__=='__main__': main()
