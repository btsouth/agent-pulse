#!/usr/bin/env python3
"""List recorded models with no applicable rate, and the table that owns each route.

A gap means the rate table has not caught up with a model, not that the usage is
free. Every route names the file that prices it and the step that refreshes it,
so a gap has an owner instead of being found one model at a time on a dashboard.

    python3 tools/pricing_gaps.py                 # last 90 days, human output
    python3 tools/pricing_gaps.py --days 30 --json
    python3 tools/pricing_gaps.py --check         # quiet; exits 1 when a gap exists
    python3 tools/pricing_gaps.py --online        # also ask the upstream snapshot

Reads the live ledger and rate files; writes nothing.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('collector', ROOT / 'collector.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

UPSTREAM = 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'
# The table that owns a route's rates, and the step that brings it current.
OWNERS = {
    'opencode-go': ('pricing.json', 'python3 tools/update_go_pricing.py --write'),
    'ollama-cloud': ('ollama-pricing.json', 'transcribe the published Ollama Cloud table'),
    'commandcode': ('commandcode-pricing.json', 'transcribe https://commandcode.ai/docs/resources/pricing-limits'),
    'commandcode-anthropic': ('commandcode-pricing.json', 'transcribe https://commandcode.ai/docs/resources/pricing-limits'),
    'clinepass': ('clinepass-pricing.json', 'transcribe the ClinePass reference table in the Cline docs'),
    'muse': ('muse-pricing.json', 'transcribe the Muse published rates'),
    'grok': ('recorded costUsdTicks', 'no table: the app records this cost per turn'),
    'cursor': ('recorded list price', 'no table: the app records this value'),
}
DEFAULT_OWNER = ('catalog.json', 'python3 tools/update_catalog.py <upstream-commit-sha>')
BUNDLED = ('catalog.json', 'pricing.json', 'ollama-pricing.json', 'commandcode-pricing.json',
           'clinepass-pricing.json', 'codex-pricing.json', 'claude-pricing.json', 'muse-pricing.json')

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--days', type=int, default=90, help='window of recorded usage to check')
parser.add_argument('--minimum-tokens', type=int, default=0, help='ignore gaps below this token count')
parser.add_argument('--json', action='store_true', help='machine-readable output')
parser.add_argument('--check', action='store_true', help='exit 1 when a gap exists, print nothing')
parser.add_argument('--online', action='store_true', help='ask the upstream catalog whether it carries the model')
parser.add_argument('--all-providers', action='store_true', help='include sources the dashboard does not count')
parser.add_argument('--brief', action='store_true', help='print only the route and model of each gap, for change detection')
args = parser.parse_args()
enabled = set(c.settings()['enabled'])

def recorded():
    """Unpriced records per route, model, and speed tier.

    Each record is priced on its own, as the dashboard does, so a long-context
    threshold or a tier rate applies to one request and never to a sum.
    """
    database = sqlite3.connect(f"file:{c.STATE / 'usage.sqlite'}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    since = int(time.time()) - args.days * 86400
    rates = c.load_rates()['document']
    groups = {}
    for row in database.execute("SELECT * FROM events WHERE ts>=? AND model<>''", (since,)):
        record = dict(row)
        if c.price(record, rates)[0] is not None: continue
        tier = record.get('serviceTier') or record.get('requestedServiceTier')
        key = (record['provider'], record.get('apiProvider') or '', record['model'], tier)
        tokens = sum(record[f] for f in c.FIELDS[:4])
        group = groups.get(key)
        if group is None:
            # The first unpriced record stands for the group in the hint.
            group = groups[key] = record | {'serviceTier': tier, 'requestedServiceTier': None,
                                            'tokens': 0, 'records': 0, 'lastSeen': 0}
        group['tokens'] += tokens; group['records'] += 1
        group['lastSeen'] = max(group['lastSeen'], record['ts'])
    return sorted(groups.values(), key=lambda group: group['tokens'], reverse=True)

def owns(provider):
    return OWNERS.get(provider, DEFAULT_OWNER)

def hint(record, rates, upstream):
    """Why a row is unpriced, in the terms of the table that would fix it."""
    provider, model = record['provider'], record['model']
    tier = record.get('serviceTier')
    if tier and c.price(record | {'serviceTier': None}, rates)[0] is not None:
        return f'no published {tier} tier rate for this model: add its _{tier} rates or leave it unpriced'
    if '/' not in model:
        # A resale table keys rows by the id the route returns, so a short id is
        # a different key rather than a missing rate.
        namespaced = [key for key in rates if key.endswith('/' + model)]
        if namespaced:
            return f'an id shape the table does not carry: compare with {namespaced[0]}'
    if upstream is not None:
        if model in upstream:
            return 'upstream carries this model: refresh the snapshot'
        if '/' in model and model.split('/', 1)[1] in upstream:
            return 'upstream carries this model under its bare name: check the id or add an alias'
    return 'no source carries this model yet: price it from the route table or leave it unpriced'

def main():
    rates = c.load_rates()['document']
    upstream = None
    if args.online:
        with urllib.request.urlopen(UPSTREAM, timeout=60) as response:
            upstream = json.load(response)
    found = []
    hidden = 0
    for record in recorded():
        if record['tokens'] < args.minimum_tokens:
            continue
        # A source the dashboard does not count cannot show an unpriced warning,
        # so it is not work unless the caller asks for the whole ledger.
        if not args.all_providers and record['provider'] not in enabled:
            hidden += 1
            continue
        table, step = owns(record['provider'])
        found.append({'provider': record['provider'], 'model': record['model'], 'tier': record['serviceTier'],
                      'tokens': record['tokens'], 'records': record['records'],
                      'lastSeen': record['lastSeen'], 'table': table, 'refresh': step,
                      'hint': hint(record, rates, upstream)})
    if args.check:
        raise SystemExit(1 if found else 0)
    if args.brief:
        # Stable output for a watcher: the set of gaps, without counts or dates,
        # which move every day and would read as a change every tick.
        for gap in sorted(found, key=lambda entry: (entry['provider'], entry['model'])):
            print(f"{gap['provider']}/{gap['model']}" + (f" ({gap['tier']})" if gap['tier'] else ''))
        return
    if args.json:
        print(json.dumps({'days': args.days, 'gaps': found, 'hiddenByDisabledSources': hidden}, indent=2))
        raise SystemExit(1 if found else 0)
    if not found:
        print(f'No unpriced models in the last {args.days} days.')
        if hidden:
            print(f'{hidden} gap(s) sit on sources this dashboard does not count; --all-providers lists them.')
        return
    print(f'{len(found)} unpriced model(s) in the last {args.days} days, worst first:\n')
    for gap in found:
        print(f"  {gap['model']} on {gap['provider']}" + (f" at the {gap['tier']} tier" if gap['tier'] else ''))
        print(f"    {gap['tokens']:,} tokens over {gap['records']} record(s), last seen {time.strftime('%Y-%m-%d', time.localtime(gap['lastSeen']))}")
        print(f"    table: {gap['table']}")
        print(f"    step:  {gap['refresh']}")
        print(f"    why:   {gap['hint']}")
    print('\nA route with no published rate for a model is a documentation gap upstream, not free usage.')

main()
