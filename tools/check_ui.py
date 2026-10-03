#!/usr/bin/env python3
"""Check account editing in an isolated offscreen Quickshell instance."""
import datetime as dt, importlib.util, json, os, pathlib, shutil, subprocess, tempfile, time
root=pathlib.Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='usage-ui-qa-') as tmp:
 b=pathlib.Path(tmp); env=dict(os.environ, HOME=tmp,XDG_CONFIG_HOME=tmp+'/config',XDG_DATA_HOME=tmp+'/data',XDG_STATE_HOME=tmp+'/state',CODEX_HOME=tmp+'/codex',CLAUDE_CONFIG_DIR=tmp+'/claude',GROK_HOME=tmp+'/grok',PI_CODING_AGENT_DIR=tmp+'/pi',MUSE_HOME=tmp+'/muse',CURSOR_HOME=tmp+'/cursor',AI_USAGE_ROOT=str(root),AI_USAGE_DEMO='1',AI_USAGE_ACCOUNT_RECORD='codex',QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software')
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
 spec=importlib.util.spec_from_file_location('qa_collector',root/'collector.py');c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
 c.HOME=b;c.STATE=b/'state/omarchy/ai-usage';c.CONFIG=b/'config/omarchy/ai-usage/settings.json'
 c.atomic_json(c.CONFIG,c.DEFAULTS | {'enabled':['codex','claude'],'accounts':[{'id':'work','label':'Work',
     'directories':[{'provider':'codex','path':str(b/'work')}]}]})
 now=dt.datetime.now().astimezone()
 ledger=c.Ledger(c.STATE/'usage.sqlite')
 for key,folder,model,tokens,when in [('main-a','local','gpt-4.1',120,now),('main-b','local','gpt-5',200,now),
                                     ('main-old','local','gpt-4.1',900,now-dt.timedelta(days=6)),('work','work','gpt-4.1',500,now)]:
  ledger.put(c.record(key,'codex',key,when.isoformat(),model,'/qa','CLI',input=tokens),b/folder/'sessions/test.jsonl')
 ledger.db.commit();ledger.db.close()
 (usage_file.parent/'work.json').write_text(json.dumps({'id':'work','name':'Work','limits':[{'label':'Weekly (7-day)',
     'percent':.65,'resetsAt':reset_at}]}))
 shutil.copytree(root/'ui',b/'ui'); p=b/'ui/shell.qml'; q=p.read_text().replace('implicitWidth: 1200','implicitWidth: 1000').replace('implicitHeight: 900','implicitHeight: 640')
 q=q.replace('function quit(): void', '''function qaTierSummary(): string {
            return JSON.stringify([
                root.tierSummary({tokens:0}),
                root.tierSummary({tokens:100,fastTokens:0,tierAssumedTokens:0}),
                root.tierSummary({tokens:100,fastTokens:73,tierAssumedTokens:27}),
                root.tierSummary({tokens:1000,fastTokens:0,tierAssumedTokens:1})])
        }
        function qaClock(): string {
            var start=Math.floor(new Date(2026,8,24,21,0,0).getTime()/1000)
            var hour={start:start,label:"21:00",title:"21:00 EDT to 22:00 EDT"}
            return JSON.stringify({label:root.localClock(start),title:root.hourTitle(hour),row:hourlyView.hourLabel(hour)})
        }
        function qaPulse(): string { return JSON.stringify({live:root.liveTodayView(),tokens:root.pulseTokens(),displayed:pulseCounter.displayedTokens,status:root.pulseStatus()}) }
        function qaPulseReload(): void { pulseFile.reload() }
        function qaAccount(id: string): void {
            root.selection=({model:"missing-model",excludeSource:["codex"],day:"2000-01-01"})
            var index=accountPicker.model.findIndex(a => a.id === id)
            if (index < 0) throw new Error("Missing account " + id)
            accountPicker.currentIndex=index
            accountPicker.activated(index)
        }
        function qaPeriod(id: string): void {
            var index=periodPicker.model.findIndex(p => p.id === id)
            if (index < 0) throw new Error("Missing period " + id)
            periodPicker.currentIndex=index
            periodPicker.activated(index)
        }
        function qaClear(): void { root.clearSelection() }
        function qaData(): string {
            return JSON.stringify({loading:scan.running || root.pending,account:root.accountViewId,
                accountLabel:accountPicker.displayText,periodLabel:periodPicker.displayText,selection:root.selection,
                tokens:root.data ? root.data.summary.tokens : -1,models:root.data ? root.data.models.map(m => ({name:m.name,tokens:m.tokens})) : [],
                detail:accountSection.visible,options:root.accountViews.map(a => a.id),
                quota:root.currentAccount ? root.currentAccount.quota : null})
        }
        function qaScan(quiet: string): string {
            if (quiet !== "") root.refresh(quiet === "true")
            return JSON.stringify({running:scan.running,loading:root.viewLoading,enabled:scroll.enabled,opacity:scroll.opacity})
        }
        function qaPin(): string {
            return JSON.stringify({visible:pinnedSection.visible,pins:root.pinnedLimits.map(pin => {
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
  startup=json.loads(ipc('qaData'))
  assert startup['account']=='codex:local' and startup['tokens']==320,startup
  assert json.loads(ipc('qaTierSummary')) == ['', '', 'Fast rates · 27% speed unknown', '<1% speed unknown']
  assert json.loads(ipc('qaClock'))=={'label':'9:00 PM','title':'9:00 PM EDT to 10:00 PM EDT','row':'9:00 PM'}
  replacement=clock_file.with_suffix('.next');replacement.write_text(clock_config('ddd d MMM HH:mm'));os.replace(replacement,clock_file)
  time.sleep(.5)
  assert json.loads(ipc('qaClock'))=={'label':'21:00','title':'21:00 EDT to 22:00 EDT','row':'21:00'}
  picker=json.loads(ipc('qaSourcePicker'))
  assert picker=={'selected':'codex',
                  'all':{'provider':'all','index':0,'label':'All sources','excluded':[]},
                  'focused':{'provider':'codex','excluded':['claude']}},picker
  def view_data():
   for _ in range(100):
    data=json.loads(ipc('qaData'))
    if not data['loading']:return data
    time.sleep(.1)
   raise AssertionError(data)
  view_data()
  ipc('qaAccount','codex:local');main=view_data()
  assert main['account']=='codex:local' and main['selection']=={'account':'local'} and main['tokens']==320 and main['detail'],main
  assert len(main['models'])==2 and sum(m['tokens'] for m in main['models'])==320,main
  window=main['quota']['limits'][0]
  assert window['tokens']==320 and window['percent']==.26,main
  ipc('qaPeriod',window['windowId']);reset=view_data()
  assert reset['tokens']==320 and reset['periodLabel']=='Since reset · Weekly (7-day)',reset
  ipc('model','gpt-5');filtered=view_data()
  assert filtered['tokens']==200,filtered
  ipc('qaClear');cleared=view_data()
  assert cleared['selection']=={'account':'local','resetWindow':window['windowId']} and cleared['tokens']==320,cleared
  ipc('qaAccount','codex:work');work=view_data()
  assert work['tokens']==500 and work['selection']=={'account':'work'} and work['quota']['limits'][0]['percent']==.65,work
  ipc('qaAccount','claude:local');idle=view_data()
  assert idle['tokens']==0 and idle['detail'] and idle['quota']['limits'][0]['percent']==.51,idle
  ipc('agent','codex');handoff=view_data()
  assert handoff['account']=='codex:local' and handoff['tokens']==320,handoff
  ipc('qaAccount','all');all_accounts=view_data()
  assert all_accounts['tokens']==820 and not all_accounts['detail'] and all_accounts['selection']=={},all_accounts
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
  print('QML account and reset-period selection, model filtering, idle accounts, cold/reused handoff, clock, source picker, pins, pulse, background refresh, and account editor passed at 1000x640')
 finally:
  proc.terminate();out=proc.communicate(timeout=5)[0]
  if any(e in out for e in ['ReferenceError','TypeError','Unable to assign','Failed to load']):raise RuntimeError(out)
