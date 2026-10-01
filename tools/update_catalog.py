#!/usr/bin/env python3
"""Rebuild the attributed pricing subset from an explicit LiteLLM revision.

A model upstream has dropped keeps the rate it last carried here: local history
still holds its tokens, and a dropped entry silently turns already recorded usage
into unpriced tokens long after the fact. Pass --drop-removed to mirror upstream.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import urllib.request

BASE_FIELDS=('input_cost_per_token','output_cost_per_token','cache_read_input_token_cost','cache_creation_input_token_cost')
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('revision',help='full 40-character upstream Git commit SHA')
parser.add_argument('--drop-removed',action='store_true',help='also drop entries the revision no longer lists')
args=parser.parse_args()
if not re.fullmatch('[0-9a-f]{40}',args.revision):parser.error('Use a full lowercase commit SHA')
url=f'https://raw.githubusercontent.com/BerriAI/litellm/{args.revision}/model_prices_and_context_window.json'
with urllib.request.urlopen(url,timeout=60) as response:raw=json.load(response)
models={}
for name,rate in raw.items():
    if isinstance(rate,dict) and rate.get('litellm_provider') in ('openai','anthropic','gemini'):
        fields={k:v for k,v in rate.items() if ('cost_per_token' in k or k.startswith('cache_')) and isinstance(v,(int,float))}
        if fields:models[name]=fields
if not models:raise SystemExit('No eligible models found; catalog not changed')
path=Path(__file__).resolve().parents[1]/'catalog.json'
try:previous=json.loads(path.read_text())['document']
except (OSError,ValueError,KeyError,AttributeError):previous={}
kept={} if args.drop_removed else {name:fields for name,fields in previous.items() if name not in models}
added=sorted(set(models)-set(previous))
changed=sorted(name for name in set(models)&set(previous)
               if any(previous[name].get(field)!=models[name].get(field) for field in BASE_FIELDS))
value={'source':'LiteLLM pricing snapshot','revision':args.revision,'url':url,
       'fetchedAtMs':int(dt.datetime.now().timestamp()*1000),'document':{**kept,**models}}
path.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n')
print(f'{len(models)} eligible entries upstream: {len(added)} added, {len(changed)} with changed base rates, '
      f'{len(kept)} kept after an upstream removal.')
for name in added[:20]:print('  added  ',name)
for name in changed[:20]:
    print('  changed',name,{field:(previous[name].get(field),models[name].get(field)) for field in BASE_FIELDS
                            if previous[name].get(field)!=models[name].get(field)})
if len(added)>20 or len(changed)>20:print('  ...rest in the catalog diff')
print('Review rates, licensing, and THIRD_PARTY_NOTICES.md before release.')
