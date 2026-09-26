#!/usr/bin/env python3
"""Check account editing in an isolated offscreen Quickshell instance."""
import datetime as dt, json, os, pathlib, shutil, subprocess, tempfile, time
root=pathlib.Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='usage-ui-qa-') as tmp:
 b=pathlib.Path(tmp); env=dict(os.environ, HOME=tmp,XDG_CONFIG_HOME=tmp+'/config',XDG_DATA_HOME=tmp+'/data',XDG_STATE_HOME=tmp+'/state',CODEX_HOME=tmp+'/codex',CLAUDE_CONFIG_DIR=tmp+'/claude',GROK_HOME=tmp+'/grok',PI_CODING_AGENT_DIR=tmp+'/pi',MUSE_HOME=tmp+'/muse',CURSOR_HOME=tmp+'/cursor',AI_USAGE_ROOT=str(root),AI_USAGE_DEMO='1',QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software')
 clock_file=b/'config/omarchy/shell.json';clock_file.parent.mkdir(parents=True)
 def clock_config(pattern):
  return json.dumps({'version':1,'bar':{'position':'top','layout':{'center':[{'id':'omarchy.clock','format':pattern}]}}})
 clock_file.write_text(clock_config('ddd d MMM h:mm AP'))
 pin_file=b/'config/omarchy/ai-usage/pinned-limit.json';pin_file.parent.mkdir(parents=True)
 pin_file.write_text(json.dumps({'provider':'codex','label':'Weekly (7-day)','title':'Weekly'}))
 usage_file=b/'state/omarchy/agents/usage/codex.json';usage_file.parent.mkdir(parents=True)
 reset_at=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=2)).isoformat()
 usage={'id':'codex','name':'Codex limits','limits':[{'label':'Weekly (7-day)','percent':0.26,'resetsAt':reset_at}]}
 usage_file.write_text(json.dumps(usage))
 for provider,percent in [('claude',.51),('codex-second',.73)]:
  (usage_file.parent/(provider+'.json')).write_text(json.dumps({'id':provider,'name':provider,
      'limits':[{'label':'Weekly (7-day)','percent':percent,'resetsAt':reset_at}]}))
 shutil.copytree(root/'ui',b/'ui'); p=b/'ui/shell.qml'; q=p.read_text().replace('implicitWidth: 1200','implicitWidth: 1000').replace('implicitHeight: 900','implicitHeight: 640')
 q=q.replace('function quit(): void', '''function qaClock(): string {
            var start=Math.floor(new Date(2026,8,24,21,0,0).getTime()/1000)
            var hour={start:start,label:"21:00",title:"21:00 EDT to 22:00 EDT"}
            return JSON.stringify({label:root.localClock(start),title:root.hourTitle(hour),row:hourlyView.hourLabel(hour)})
        }
        function qaPulse(): string { return JSON.stringify({live:root.liveTodayView(),tokens:root.pulseTokens(),displayed:pulseCounter.displayedTokens,status:root.pulseStatus()}) }
        function qaPulseReload(): void { pulseFile.reload() }
        function qaScan(quiet: string): string {
            if (quiet !== "") root.refresh(quiet === "true")
            return JSON.stringify({running:scan.running,loading:root.viewLoading,enabled:scroll.enabled,opacity:scroll.opacity})
        }
        function qaPin(): string {
            return JSON.stringify({visible:pinnedColumn.parent.visible,pins:root.pinnedLimits.map(pin => {
                var limit=root.pinnedWindow(pin)
                return {name:root.pinnedName(pin),label:pin.label,percent:limit ? limit.percent : null,
                    reset:limit ? root.pinnedResetText(limit.resetsAt) : ""}
            })})
        }
        function qaPinReload(): void { pinFile.reload() }
        function qaUnpin(): void { root.unpinLimit(root.pinnedLimits[0]) }
        function qaAdd(): void { root.openSettings(); root.settingsTab="accounts"; root.draftAccounts=[{id:"qa",label:"QA",directories:[{provider:"codex",path:"/tmp/qa/.codex"}]}] }
        function qaSourcePicker(): string {
            sourcePicker.popup.open()
            sourcePicker.currentIndex=1
            sourcePicker.activated(1)
            var selected=root.provider
            root.selection=({excludeSource:["codex","claude"],account:"qa"})
            sourcePicker.currentIndex=0
            sourcePicker.activated(0)
            var all={provider:root.provider,index:sourcePicker.currentIndex,label:sourcePicker.displayText,
                excluded:root.selection.excludeSource || [],account:root.selection.account}
            root.selection=({excludeSource:["codex","claude"],account:"qa"})
            sourcePicker.currentIndex=1
            sourcePicker.activated(1)
            var focused={provider:root.provider,excluded:root.selection.excludeSource || [],account:root.selection.account}
            root.selection=({})
            root.chooseSource("all")
            sourcePicker.popup.close()
            return JSON.stringify({selected:selected,all:all,focused:focused})
        }
        function qaSave(): void { root.saveSettings() }
        function qaScroll(): void { settingsScroll.contentItem.contentY=0 }
        function qaClick(): void {
            function walk(item) {
                if (item.text === "Add folder" && item.clicked) { item.clicked(); return true }
                var kids=item.children || []
                for(var i=0;i<kids.length;i++) if(walk(kids[i])) return true
                return false
            }
            walk(captureRoot)
        }
        function qaFill(): void {
            function walk(item) {
                if (item.placeholderText === "Full agent home folder, e.g. /mnt/work/.codex" && item.text === "") { item.text="/tmp/qa-copy/.codex"; item.textEdited() }
                var kids=item.children || []
                for(var i=0;i<kids.length;i++) walk(kids[i])
            }
            walk(captureRoot)
        }
        function quit(): void''');p.write_text(q)
 proc=subprocess.Popen(['quickshell','-p',str(b/'ui'),'--no-color'],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
 def ipc(*args):
  r=subprocess.run(['quickshell','ipc','-p',str(b/'ui'),'--any-display','call','analytics',*args],env=env,capture_output=True,text=True)
  if r.returncode: raise RuntimeError(r.stderr+r.stdout)
  return r.stdout.strip()
 try:
  time.sleep(1.5)
  assert json.loads(ipc('qaClock'))=={'label':'9:00 PM','title':'9:00 PM EDT to 10:00 PM EDT','row':'9:00 PM'}
  replacement=clock_file.with_suffix('.next');replacement.write_text(clock_config('ddd d MMM HH:mm'));os.replace(replacement,clock_file)
  time.sleep(.5)
  assert json.loads(ipc('qaClock'))=={'label':'21:00','title':'21:00 EDT to 22:00 EDT','row':'21:00'}
  picker=json.loads(ipc('qaSourcePicker'))
  assert picker=={'selected':'codex',
                  'all':{'provider':'all','index':0,'label':'All sources','excluded':[],'account':'qa'},
                  'focused':{'provider':'codex','excluded':['claude'],'account':'qa'}},picker
  pinned=json.loads(ipc('qaPin'))
  assert pinned['visible'] and pinned['pins'][0]['name']=='ChatGPT Main' and pinned['pins'][0]['label']=='Weekly (7-day)' and pinned['pins'][0]['percent']==.26 and 'Resets in' in pinned['pins'][0]['reset'],pinned
  usage['limits'][0]['percent']=.42
  replacement=usage_file.with_suffix('.next');replacement.write_text(json.dumps(usage));os.replace(replacement,usage_file)
  time.sleep(.3)
  updated=json.loads(ipc('qaPin'))
  assert updated['pins'][0]['percent']==.42,updated
  def add_pin(provider):
   return subprocess.run(['python3',str(root/'collector.py'),'pin','--pin-provider',provider,
       '--pin-label','Weekly (7-day)','--pin-title','Weekly'],env=env,capture_output=True,text=True)
  assert add_pin('claude').returncode==0
  assert add_pin('codex-second').returncode==0
  assert add_pin('grok').returncode!=0
  ipc('qaPinReload');time.sleep(.4)
  three=json.loads(ipc('qaPin'))
  assert [p['percent'] for p in three['pins']]==[.42,.51,.73],three
  ipc('qaUnpin');time.sleep(.4)
  unpinned=json.loads(ipc('qaPin'))
  assert unpinned['visible'] and [p['percent'] for p in unpinned['pins']]==[.51,.73],unpinned
  assert len(json.loads(pin_file.read_text())['pins'])==2
  now=dt.datetime.now().astimezone()
  pulse_file=b/'state/omarchy/ai-usage/hourly-summary.json';pulse_file.parent.mkdir(parents=True,exist_ok=True)
  pulse={'schemaVersion':1,'date':str(now.date()),'generatedAt':now.timestamp(),
         'utcOffsetMinutes':int(now.utcoffset().total_seconds()//60),'providers':{'codex':{'tokens':120}},
         'hours':[],'unplacedTokens':0}
  pulse_file.write_text(json.dumps(pulse));ipc('qaPulseReload');time.sleep(.3)
  first=json.loads(ipc('qaPulse'))
  assert first['live'] and first['tokens']==120 and first['displayed']==120,first
  pulse['providers']['codex']['tokens']=240
  replacement=pulse_file.with_suffix('.next');replacement.write_text(json.dumps(pulse));os.replace(replacement,pulse_file)
  time.sleep(.3)
  moving=json.loads(ipc('qaPulse'))
  assert moving['tokens']==240 and 120 < moving['displayed'] < 240,moving
  time.sleep(1.2)
  settled=json.loads(ipc('qaPulse'))
  assert settled['displayed']==240,settled
  def scan_state(quiet=''):
   return json.loads(ipc('qaScan',quiet))
  for _ in range(50):
   if not scan_state()['running']:break
   time.sleep(.1)
  background=scan_state('true')
  assert background=={'running':True,'loading':False,'enabled':True,'opacity':1},background
  navigation=scan_state('false')
  assert navigation['running'] and navigation['loading'] and not navigation['enabled'],navigation
  for _ in range(50):
   done=scan_state()
   if not done['running']:break
   time.sleep(.1)
  time.sleep(.3)
  done=scan_state()
  assert done=={'running':False,'loading':False,'enabled':True,'opacity':1},done
  ipc('qaAdd');time.sleep(.4);ipc('qaClick');time.sleep(.3);ipc('qaFill');ipc('qaScroll');time.sleep(.3);ipc('capture',str(b/'account-editor.png'));time.sleep(.3);ipc('qaSave');time.sleep(1)
  settings=json.loads((b/'config/omarchy/ai-usage/settings.json').read_text())
  assert settings['accounts'][0]['directories']==[{'provider':'codex','path':'/tmp/qa/.codex'},{'provider':'codex','path':'/tmp/qa-copy/.codex'}],settings
  print('QML clock, source picker, three pinned limits and individual unpin, live pulse, quiet background refresh, and account editor passed at 1000x640')
 finally:
  proc.terminate();out=proc.communicate(timeout=5)[0]
  if any(e in out for e in ['ReferenceError','TypeError','Unable to assign','Failed to load']):raise RuntimeError(out)
