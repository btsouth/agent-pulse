#!/usr/bin/env python3
"""Local metrics only. Source transcripts are read, never modified or copied."""
import argparse
import calendar
import collections
import datetime as dt
import fcntl
import hashlib
import math
import json
import os
from pathlib import Path
import re
import select
import socket
import struct
import sqlite3
import sys
import tempfile
import time
import tomllib
import subprocess
import urllib.request
import urllib.parse
import uuid

HOME = Path.home()
STATE = Path(os.getenv('XDG_STATE_HOME', HOME / '.local/state')) / 'omarchy/ai-usage'
CONFIG = Path(os.getenv('XDG_CONFIG_HOME', HOME / '.config')) / 'omarchy/ai-usage/settings.json'
PINNED_LIMIT = CONFIG.with_name('pinned-limit.json')
MAX_PINNED_LIMITS = 3
PROVIDERS = {'codex': 'Codex', 'claude': 'Claude', 'opencode-go': 'OpenCode Go', 'grok': 'Grok Build',
             'gemini': 'Gemini CLI', 'opencode': 'OpenCode', 'pi': 'Pi', 'omp': 'Oh My Pi', 'muse': 'Muse',
             'ollama-cloud': 'Ollama Cloud', 'commandcode': 'CommandCode',
             'clinepass': 'ClinePass', 'cursor': 'Cursor'}
HOME_KEYS = ('codexHomes', 'claudeHomes', 'grokHomes', 'geminiHomes', 'opencodeHomes', 'piHomes', 'ompHomes',
             'museHomes', 'commandcodeHomes', 'hermesHomes')
# Provider ids used by the Hermes agent's own per-model usage table. Hermes
# bills the same routes this dashboard reads elsewhere, so its ledger is a
# source, not a separate provider. Two of these are the same product reached
# over different wire formats, so they map onto one dashboard provider: an
# account is not two accounts because one call used the Anthropic shape.
HERMES_ROUTES = ('opencode-go', 'ollama-cloud', 'commandcode', 'commandcode-anthropic', 'clinepass')
HERMES_ROUTE_NAMES = {'commandcode-anthropic': 'commandcode'}
HERMES_TASKS = {'': 'conversation', 'title_generation': 'title generation', 'background_review': 'background review',
                'approval': 'approval', 'compression': 'context compression', 'vision': 'vision',
                'embedding': 'embedding'}
# Ollama Cloud reports quota as a fraction of each plan window. Legacy plans
# have session and weekly windows; credit plans have a monthly one.
OLLAMA_WINDOWS = {'session': 'Session (5-hour)', 'weekly': 'Weekly (7-day)', 'monthly': 'Monthly'}
# One model can be reached over several routes, and each route writes the model
# string its own API returns: OpenCode Go and Ollama Cloud record a bare name,
# while CommandCode and ClinePass prefix it with the vendor's id. Those are the
# same model, so the Models breakdown groups them and shows the split by route.
# Identical strings need no entry below. Most vendor-prefixed spellings are
# listed explicitly because a general prefix rule would merge model families
# that only look alike. Muse Spark is the narrow exception: its routes add
# vendor prefixes to the same `muse-spark-*` model ids.
MODEL_FAMILIES = {'deepseek/deepseek-v4.1-flash': 'deepseek-v4.1-flash',
                  'cline-pass/deepseek-v4.1-flash': 'deepseek-v4.1-flash'}
# T3 Code keeps each configured provider instance in settings.json. The
# runtime homes are ordinary Codex, Claude, or OpenCode stores, but the app's
# provider name is what identifies the account and pricing route.
T3_DRIVERS = {'codex': 'codex', 'claudeagent': 'claude', 'claude': 'claude', 'opencode': 'opencode'}
CODEX_ROUTE_PROVIDERS = {'commandcode': 'commandcode'}
OPENCODE_ROUTE_PROVIDERS = {'opencodego': 'opencode-go', 'clinepass': 'clinepass',
                            'ollamacloud': 'ollama-cloud'}


def model_family(name):
    """The model a recorded string belongs to. Anything unlisted is its own
    family, so a new model is never folded into a family by accident."""
    bare = name.rsplit('/', 1)[-1]
    if bare.startswith('muse-spark-'):
        return bare
    return MODEL_FAMILIES.get(name, name)
# Stands in for a stored key in reports so the settings form can show that one
# exists without sending it back over the report channel. Never a valid key.
# One mask covers every provider's key field, since the form only needs to know
# that something is stored.
API_KEY_MASK = 'stored'
# Every stored key field, in one place: the report mask and the settings save
# path both iterate this list, so a key added here cannot be echoed by omission.
API_KEY_FIELDS = ('ollamaApiKey', 'commandcodeApiKey', 'clinepassApiKey')
DEFAULTS = {'enabled': ['codex', 'claude', 'opencode-go'], 'monthlyPrices': {},
            **{key: [] for key in HOME_KEYS}, 'accounts': [], 'localAccountLabel': 'Local', 'windowOpacity': 0.985,
            'ledgerSyncDir': '', 'ledgerDeviceId': '', 'ollamaApiKey': '', 'commandcodeApiKey': '',
            'clinepassApiKey': '', 'updateCheck': True}
# The update notice asks GitHub for the latest release once a day. It sends
# no usage data and can be switched off in Settings.
RELEASE_URL = 'https://api.github.com/repos/btsouth/agent-pulse/releases/latest'
INSTALL_URL = 'https://raw.githubusercontent.com/btsouth/agent-pulse/main/install.sh'
PLUGIN_ID = 'community.ai-usage-dashboard'
FIELDS = ('input', 'output', 'cacheRead', 'cacheWrite', 'cacheWrite1h', 'reasoning')
# OpenAI service tiers by the names its responses and the pricing catalog use.
# Fast mode is the renamed priority tier and either request spelling selects it.
SERVICE_TIERS = {'priority': 'priority', 'fast': 'priority', 'default': 'default', 'standard': 'default',
                 'flex': 'flex', 'ultrafast': 'ultrafast', 'scale': 'scale'}
# Bundled official rate tables merged over the catalog, in order. User
# rates.json entries still win over every file listed here.
OVERRIDES = (('pricing.json', 'OpenCode Go official rates'),
             ('muse-pricing.json', 'Muse official rates'),
             ('codex-pricing.json', 'Codex model rates'),
             ('claude-pricing.json', 'Claude model rates'),
             ('ollama-pricing.json', 'Ollama Cloud model rates'),
             ('commandcode-pricing.json', 'CommandCode model rates'),
             ('clinepass-pricing.json', 'ClinePass reference rates'))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, separators=(',', ':'))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def settings():
    try: return DEFAULTS | json.loads(CONFIG.read_text())
    except (OSError, ValueError): return dict(DEFAULTS)


def valid_pin(value):
    if not isinstance(value, dict): return None
    provider, label = value.get('provider'), value.get('label')
    if not isinstance(provider, str) or not isinstance(label, str): return None
    # Same rule the panel and dashboard loaders apply, so a saved pin is never one they drop.
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', provider): return None
    if not label or len(label) > 120: return None
    title = value.get('title', '')
    if not isinstance(title, str) or len(title) > 120: return None
    return {'provider': provider, 'label': label, 'title': title}


def pinned_limits():
    try: value = json.loads(PINNED_LIMIT.read_text())
    except (OSError, ValueError): return []
    if not isinstance(value, dict): return []
    entries = value.get('pins') if isinstance(value.get('pins'), list) else [value]
    pins = []
    for entry in entries:
        pin = valid_pin(entry)
        if pin and pin not in pins: pins.append(pin)
    return pins[:MAX_PINNED_LIMITS]


def pinned_limit():
    """Compatibility for callers expecting the first pinned window."""
    return next(iter(pinned_limits()), None)


def save_pinned_limit(provider, label, title='', remove=False):
    pins = pinned_limits()
    if provider == label == '' and not remove:
        pins = []
    else:
        pin = valid_pin({'provider': provider, 'label': label, 'title': title})
        if not pin: raise ValueError('Choose a source and limit window to pin.')
        if remove:
            pins = [entry for entry in pins if entry != pin]
        elif pin not in pins:
            if len(pins) >= MAX_PINNED_LIMITS:
                raise ValueError('You can pin up to three limits. Unpin one first.')
            pins.append(pin)
    atomic_json(PINNED_LIMIT, {'pins': pins})
    return pins


def provider_key(value):
    return ''.join(c for c in str(value or '').casefold() if c.isalnum())


def t3_provider_instances():
    """Enabled T3 Code runtime roots and the dashboard route each one uses.

    T3 stores custom provider homes in its own settings file. Reading the
    selected paths keeps those isolated runtimes visible without copying or
    exposing the credentials stored beside them.
    """
    path = HOME / '.t3/userdata/settings.json'
    try: data = json.loads(path.read_text())
    except (OSError, ValueError): return []
    instances = data.get('providerInstances') if isinstance(data, dict) else None
    if not isinstance(instances, dict): return []
    found, seen = [], set()
    for instance_id, instance in instances.items():
        if not isinstance(instance, dict) or instance.get('enabled') is False: continue
        driver = T3_DRIVERS.get(provider_key(instance.get('driver')))
        if not driver: continue
        config = instance.get('config') if isinstance(instance.get('config'), dict) else {}
        identity = provider_key(str(instance_id) + ' ' + str(instance.get('displayName') or ''))
        provider = driver
        if 'commandcode' in identity: provider = 'commandcode'
        elif 'clinepass' in identity: provider = 'clinepass'
        elif 'ollamacloud' in identity: provider = 'ollama-cloud'
        elif 'opencodego' in identity: provider = 'opencode-go'

        root = config.get('homePath')
        if driver == 'opencode':
            environment = instance.get('environment')
            for item in environment if isinstance(environment, list) else []:
                if not isinstance(item, dict) or item.get('sensitive'): continue
                if item.get('name') == 'XDG_DATA_HOME' and item.get('value'):
                    root = Path(str(item['value'])).expanduser() / 'opencode'
                    break
        if not root: continue
        root = Path(str(root)).expanduser()
        if not root.is_absolute(): continue
        key = (driver, provider, str(root.absolute()))
        if key in seen: continue
        seen.add(key)
        found.append({'driver': driver, 'provider': provider, 'root': key[2],
                      'id': str(instance_id), 'label': str(instance.get('displayName') or instance_id)})
    return found


def agent_records():
    records = {}
    for path in sorted((STATE.parent / 'agents/usage').glob('*.json')):
        try: record = json.loads(path.read_text())
        except (OSError, ValueError): continue
        if isinstance(record, dict): records[path.stem] = record
    return records


def record_provider(key, record):
    explicit = record.get('provider')
    if explicit in PROVIDERS: return explicit
    return next((p for p in sorted(PROVIDERS, key=len, reverse=True)
                 if key == p or key.startswith(p + '-') or key.startswith(p + ':')), None)


def history_config(cfg):
    """Resolve configured homes once into account identities, without saving
    discovered accounts or touching authentication. Explicit folder ownership
    wins over T3 and legacy extra homes. Unlinked agent records remain visible
    as quota-only accounts rather than inheriting somebody else's history.
    """
    if cfg.get('_historyResolved'): return cfg
    accounts = [dict(a, directories=list(a.get('directories', []))) for a in cfg.get('accounts', [])]
    defaults = {'codex': HOME / '.codex', 'claude': HOME / '.claude', 'grok': HOME / '.grok',
                'gemini': HOME / '.gemini', 'pi': HOME / '.pi/agent', 'omp': HOME / '.omp/agent',
                'muse': Path(os.getenv('XDG_DATA_HOME', HOME / '.local/share')) / 'muse',
                'opencode': Path(os.getenv('XDG_DATA_HOME', HOME / '.local/share')) / 'opencode',
                'commandcode': HOME / '.commandcode', 'hermes': HOME / '.hermes'}
    def add_home(provider, root, aid, label):
        root = Path(root).expanduser().resolve()
        if provider in defaults and root == defaults[provider].resolve(): return
        if any(d['provider'] == provider and root.is_relative_to(Path(d['path']).expanduser().resolve())
               for a in accounts for d in a['directories']): return
        accounts.append({'id': aid, 'label': label, 'directories': [{'provider': provider, 'path': str(root)}],
                         'discovered': True})
    for instance in t3_provider_instances():
        # A runtime routed to an API service is another client of the local
        # service account. Its private tool home is not a separate login.
        if instance['provider'] != instance['driver']: continue
        add_home(instance['provider'], instance['root'], 't3-' + digest(instance['id'])[:16], instance['label'])
    for key in HOME_KEYS:
        provider = key.removesuffix('Homes')
        routes = HERMES_ROUTES if provider == 'hermes' else ('opencode', 'opencode-go') if provider == 'opencode' else (provider,)
        for path in cfg.get(key, []):
            for route in dict.fromkeys(HERMES_ROUTE_NAMES.get(p, p) for p in routes):
                if route not in PROVIDERS: continue
                root = Path(path).expanduser().resolve()
                add_home(route, root, 'home-' + digest(route, str(root))[:16], root.name or PROVIDERS[route])
    records = agent_records()
    for key, record in records.items():
        if key in PROVIDERS: continue
        matches = [a for a in accounts if a['id'] == key or any(key == d['provider'] + ':' + a['id'] for d in a['directories'])]
        if not matches:
            matches = [a for a in accounts if record.get('name') and a['label'].casefold() == str(record['name']).casefold()]
        if len(matches) == 1:
            matches[0]['recordId'] = key
        elif not matches:
            provider = record_provider(key, record)
            if provider:
                accounts.append({'id': key, 'label': str(record.get('name') or key), 'directories': [],
                                 'providers': [provider], 'recordId': key, 'historyAvailable': False,
                                 'discovered': True})
    return cfg | {'accounts': accounts, '_historyResolved': True}


def theme():
    current = STATE.parent / 'current/theme'
    palette, shell = {}, {}
    for path, target in [(current / 'colors.toml', palette), (current / 'shell.toml', shell),
                         (CONFIG.parent.parent / 'shell.toml', shell)]:
        try:
            data = tomllib.loads(path.read_text())
            for key, value in data.items():
                if isinstance(value, dict): target[key] = target.get(key, {}) | value
                else: target[key] = value
        except (OSError, ValueError): pass
    try: font = subprocess.check_output(['fc-match', 'monospace', '-f', '%{family}'], text=True, timeout=2).split(',')[0]
    except (OSError, subprocess.SubprocessError): font = 'monospace'
    return {'palette': palette, 'shell': shell, 'font': font}


def installed_version():
    try: return (Path(__file__).resolve().parent / 'VERSION').read_text().strip()
    except OSError: return ''


def version_key(value):
    """1.10.0 sorts after 1.9.2. Anything that is not dotted numbers has no
    key, so an unexpected tag never claims to be an update."""
    match = re.fullmatch(r'v?(\d+(?:\.\d+)*)', str(value or '').strip())
    return tuple(int(part) for part in match.group(1).split('.')) if match else ()


def check_update(cfg, force=False):
    """Ask GitHub for the latest release, at most once a day. A failed
    request keeps the last answer and is not retried until the next day."""
    path = STATE / 'update.json'
    if not cfg.get('updateCheck', True) or not installed_version(): return
    try: cached = json.loads(path.read_text())
    except (OSError, ValueError): cached = {}
    if not isinstance(cached, dict): cached = {}
    attempted = cached.get('attemptedAt', 0)
    if not isinstance(attempted, (int, float)): attempted = 0
    if not force and 0 <= time.time() - attempted < 86400: return
    cached['attemptedAt'] = time.time()
    try:
        request = urllib.request.Request(RELEASE_URL, headers={'Accept': 'application/vnd.github+json',
                                                               'User-Agent': 'Agent-Pulse/' + installed_version()})
        with urllib.request.urlopen(request, timeout=8) as response: data = json.load(response)
        tag = str(data.get('tag_name') or '') if isinstance(data, dict) else ''
        if version_key(tag): cached['latest'] = tag.lstrip('v')
    except (OSError, ValueError): pass
    atomic_json(path, cached)


def update_status(cfg):
    """What the UI shows. Reads the last check only; never touches the network."""
    current = installed_version()
    try: latest = str(json.loads((STATE / 'update.json').read_text()).get('latest') or '')
    except (OSError, ValueError, AttributeError): latest = ''
    available = bool(cfg.get('updateCheck', True) and version_key(current) and version_key(latest) > version_key(current))
    plugin = (CONFIG.parents[1] / 'plugins' / PLUGIN_ID / 'manifest.json').exists()
    return {'current': current, 'latest': latest if available else '', 'available': available,
            'command': 'curl -fsSL %s | OMARCHY_USAGE_REF=v%s bash%s' %
                (INSTALL_URL, latest if available else current, ' -s -- --with-plugin' if plugin else '')}


def masked_settings(cfg):
    """A copy of the settings safe to hand to the UI: any stored key is
    replaced by a mask. Derived from the key fields rather than written out at
    each call site, so a key added later cannot be echoed by omission."""
    return cfg | {field: (API_KEY_MASK if cfg.get(field) else '')
                  for field in API_KEY_FIELDS}


def stdin_payload(stream=None, timeout=5.0, idle=1.0):
    """The settings payload a client wrote to stdin, read without waiting for
    the end of input.

    A GUI client hands this process a live pipe and holds its write end open
    for as long as the process runs, so reading to the end of file blocks the
    save forever: no settings written, no exit, and a Save button that never
    comes back. Stop as soon as the bytes parse as JSON. The first byte gets
    `timeout` seconds, since a client may open the pipe before it writes, and
    each later gap gets `idle`, so a writer that goes quiet mid-payload cannot
    hold the save open. `timeout` also caps the whole read.
    """
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or stream.isatty(): return None
    except (AttributeError, ValueError): return None
    deadline, chunks, wait = time.monotonic() + timeout, [], timeout
    while True:
        wait = min(wait, deadline - time.monotonic())
        if wait <= 0: break
        try: ready = select.select([stream], [], [], wait)[0]
        except (OSError, ValueError): break
        if not ready: break
        try: chunk = os.read(stream.fileno(), 65536)
        except (AttributeError, OSError): break
        if not chunk: break  # The writer closed: that is the end of the payload.
        chunks.append(chunk)
        try: json.loads(b''.join(chunks))
        except (UnicodeDecodeError, ValueError): wait = idle; continue
        break
    return b''.join(chunks).decode('utf-8', 'replace').strip() or None


def save_settings(value):
    if not isinstance(value, dict): raise ValueError('Send the settings as a JSON object.')
    # Refuse a value that is not a number with a fixed sentence. A message that
    # repeated the value would print whatever was typed or pasted into that
    # field, and a key pasted into the wrong field is exactly that.
    try: opacity = float(value.get('windowOpacity', 0.985))
    except (TypeError, ValueError): raise ValueError('Give the window opacity as a number.')
    clean = {'enabled': [p for p in value.get('enabled', []) if p in PROVIDERS],
             'monthlyPrices': {}, **{key: [] for key in HOME_KEYS},
             'accounts': [], 'localAccountLabel': str(value.get('localAccountLabel') or 'Local').strip(),
             'ledgerSyncDir': '', 'ledgerDeviceId': str(value.get('ledgerDeviceId') or '').strip(),
             **{field: str(value.get(field) or '').strip() for field in API_KEY_FIELDS},
             'windowOpacity': max(0.55, min(1.0, opacity)),
             # A client that does not send the field leaves the choice alone.
             'updateCheck': bool(value['updateCheck']) if 'updateCheck' in value else bool(settings().get('updateCheck', True))}
    # The user's own key beats the environment and the key file, since typing
    # one in is deliberate. Settings stay nonsecret by default; these values
    # are the exception and are never echoed back over the settings channel.
    for field in API_KEY_FIELDS:
        if field not in value:
            # A client that does not send the field at all means "leave it
            # alone", not "clear it". The form sends the mask when untouched.
            clean[field] = str(settings().get(field) or '')
        elif clean[field] == API_KEY_MASK:
            # A report round trip carries the mask, not the key, so an
            # unchanged field means "keep whatever is stored".
            clean[field] = str(settings().get(field) or '')
        if len(clean[field]) > 200: raise ValueError('Give the API key 200 characters or fewer.')
    if str(value.get('ledgerSyncDir') or '').strip():
        clean['ledgerSyncDir'] = str(Path(str(value['ledgerSyncDir'])).expanduser().absolute())
    if len(clean['ledgerDeviceId']) > 80: raise ValueError('Give the ledger device id 80 characters or fewer.')
    if clean['ledgerDeviceId'] in ('.', '..') or any(c in clean['ledgerDeviceId'] for c in '/\\\0'):
        raise ValueError('Use a device id without path separators.')
    for key in HOME_KEYS:
        clean[key] = sorted({str(Path(p).expanduser().absolute()) for p in value.get(key, []) if str(p).strip()})
    if not clean['localAccountLabel'] or len(clean['localAccountLabel']) > 80: raise ValueError('Give the local history group a name of 1 to 80 characters.')
    if clean['localAccountLabel'].casefold() in ('unassigned history', 'needs review'): raise ValueError('Choose a different local account name.')
    labels, ids, paths = {clean['localAccountLabel'].casefold(), 'unassigned history', 'needs review'}, {'local', 'unassigned', 'conflict'}, {}
    for account in value.get('accounts', []):
        label = str(account.get('label') or '').strip()
        aid = str(account.get('id') or uuid.uuid4())
        if not label or len(label) > 80: raise ValueError('Give each account a name of 1 to 80 characters.')
        if label.casefold() in labels or aid in ids: raise ValueError('Account names must be unique.')
        labels.add(label.casefold()); ids.add(aid)
        directories = []
        for directory in account.get('directories', []):
            provider, raw = directory.get('provider'), str(directory.get('path') or '').strip()
            if provider not in PROVIDERS or not raw: raise ValueError('Choose a source and folder for every account directory.')
            if not Path(raw).expanduser().is_absolute(): raise ValueError('Use a full path for each account folder.')
            path = str(Path(raw).expanduser().absolute())
            key = (provider, str(Path(path).resolve()))
            if key in paths and paths[key] != aid: raise ValueError('The same source folder cannot belong to two accounts.')
            paths[key] = aid
            if {'provider': provider, 'path': path} not in directories: directories.append({'provider': provider, 'path': path})
        if not directories: raise ValueError('Add at least one source folder to each account.')
        clean['accounts'].append({'id': aid, 'label': label, 'directories': directories})
    # A price key is a provider (local history) or a labelled account id.
    account_ids = {account['id'] for account in clean['accounts']}
    for name, amount in value.get('monthlyPrices', {}).items():
        if name not in PROVIDERS and name not in account_ids: continue
        if amount is None: continue
        try: price = float(amount)
        except (TypeError, ValueError): raise ValueError('Give each monthly price as a number.')
        if 0 <= price <= 100000: clean['monthlyPrices'][name] = price
    atomic_json(CONFIG, clean)
    return clean


def number(value):
    try: return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError): return 0


def timestamp(value):
    if isinstance(value, (int, float)): return int(value / 1000 if value > 10_000_000_000 else value)
    try: return int(dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp())
    except (ValueError, TypeError): return 0


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def record(key, provider, session, ts, model, project, client, **tokens):
    return {'id': key, 'provider': provider, 'session': session, 'ts': timestamp(ts),
            'model': model or 'unknown', 'project': str(project or ''), 'client': client,
            'timePrecision': 'event',
            **{f: number(tokens.get(f)) for f in FIELDS}}


def service_tier(value):
    # Auto, an empty value, or an unrecognised spelling records no tier.
    return SERVICE_TIERS.get(str(value or '').strip().casefold())


def codex_records(path, provider='codex'):
    session, project, client, model = path.stem, '', 'CLI', 'unknown'
    started = 0
    seen = set()
    have_metadata = False
    # Codex records the speed each turn asked for in a full thread-settings
    # snapshot. It does not record the tier the server actually used. A
    # snapshot takes effect for the requests after it; one that changes the
    # tier after a turn has produced usage leaves the rest of that turn
    # unknown, because the change may only apply from the next turn.
    route, tier, pending, turn_usage = 'openai', None, (), 0
    with path.open(errors='replace') as f:
        for raw in f:
            try: item = json.loads(raw)
            except ValueError: continue
            p = item.get('payload') or {}
            if not isinstance(p, dict): continue
            kind = item.get('type')
            if kind == 'session_meta':
                # Fork history embeds the parent's session_meta after the
                # child's header. It must not replace the child's identity.
                if have_metadata: continue
                have_metadata = True
                session = str(p.get('id') or p.get('session_id') or session)
                project = p.get('cwd') or project
                originator = str(p.get('originator') or '').casefold()
                client = 'T3 Code' if 't3code' in originator else 'Desktop' if 'desktop' in originator else 'CLI'
                provider = CODEX_ROUTE_PROVIDERS.get(provider_key(p.get('model_provider')), provider)
                route = provider_key(p.get('model_provider')) or route
                started = timestamp(p.get('timestamp'))
            elif kind == 'event_msg' and p.get('type') == 'task_started':
                if started and timestamp(item.get('timestamp')) < started: continue
                if pending != (): tier, pending = pending[0], ()
                turn_usage = 0
            elif kind == 'event_msg' and p.get('type') == 'thread_settings_applied':
                ts = timestamp(item.get('timestamp'))
                settings = p.get('thread_settings')
                if not isinstance(settings, dict) or (started and ts and ts < started): continue
                # Only OpenAI's own API has these tiers; another model provider's
                # route is not billed by them.
                value = service_tier(settings.get('service_tier')) if (provider_key(settings.get('model_provider_id')) or route) == 'openai' else None
                if not turn_usage: tier, pending = value, ()
                elif value != (pending[0] if pending else tier): tier, pending = None, (value,)
            elif kind == 'turn_context':
                model = p.get('model') or p.get('model_slug') or model
                project = p.get('cwd') or project
            elif kind == 'event_msg' and p.get('type') == 'token_count':
                info = p.get('info') or {}
                u = info.get('last_token_usage') or {}
                total = info.get('total_token_usage')
                ts = timestamp(item.get('timestamp'))
                # Forked rollouts can contain inherited history. Ignore events
                # predating this session and repeated cumulative snapshots.
                if not u or not ts or (started and ts < started): continue
                fingerprint = digest(total) if total else digest(ts, u)
                if fingerprint in seen: continue
                seen.add(fingerprint)
                turn_usage += 1
                read, write = number(u.get('cached_input_tokens')), number(u.get('cache_write_input_tokens'))
                yield record(digest('codex', session, fingerprint), provider, session, ts, model,
                             project, client, input=max(0, number(u.get('input_tokens')) - read - write),
                             output=u.get('output_tokens'), cacheRead=read, cacheWrite=write,
                             reasoning=u.get('reasoning_output_tokens')) | {'requestedServiceTier': tier}


def claude_records(path, provider='claude'):
    with path.open(errors='replace') as f:
        for raw in f:
            try: item = json.loads(raw)
            except ValueError: continue
            m = item.get('message') or {}
            if item.get('type') != 'assistant' or not isinstance(m, dict): continue
            u = m.get('usage') or {}
            model = m.get('model') or 'unknown'
            if not u or model == '<synthetic>': continue
            session = str(item.get('sessionId') or item.get('session_id') or path.stem)
            msg_id = m.get('id') or item.get('uuid')
            key = digest('claude', msg_id, item.get('requestId')) if msg_id else digest('claude', session, item.get('timestamp'), u)
            client = 'Claude Code' if provider == 'claude' else PROVIDERS[provider]
            yield record(key, provider, session, item.get('timestamp'), model, item.get('cwd'),
                         client, input=u.get('input_tokens'), output=u.get('output_tokens'),
                         cacheRead=u.get('cache_read_input_tokens'), cacheWrite=u.get('cache_creation_input_tokens'),
                         cacheWrite1h=(u.get('cache_creation') or {}).get('ephemeral_1h_input_tokens'),
                         reasoning=(u.get('output_tokens_details') or {}).get('thinking_tokens'))


def commandcode_records(path):
    # A session folder keeps the transcript, a checkpoints mirror, and a
    # metadata file side by side; only the transcript carries usage lines.
    if path.name.endswith('.checkpoints.jsonl'): return
    session = path.stem
    cwd = ''
    with path.open(errors='replace') as f:
        for raw in f:
            try: item = json.loads(raw)
            except ValueError: continue
            if not isinstance(item, dict): continue
            if item.get('type') == 'session':
                session = str(item.get('id') or session)
                cwd = str(item.get('cwd') or '')
                continue
            if item.get('type') != 'message': continue
            model = item.get('model')
            u = item.get('usage')
            if not model or not isinstance(u, dict): continue
            yield record(digest('commandcode', session, item.get('id')) if item.get('id')
                         else digest('commandcode', session, item.get('timestamp'), u),
                         'commandcode', session, item.get('timestamp'), model, cwd or path.parent.name,
                         'Command Code', input=u.get('inputTokens'), output=u.get('outputTokens'),
                         cacheRead=u.get('cacheReadTokens'), cacheWrite=u.get('cacheWriteTokens'))


def grok_records(path):
    try: summary = json.loads(path.with_name('summary.json').read_text())
    except (OSError, ValueError): summary = {}
    if not isinstance(summary, dict): summary = {}
    project = (summary.get('info') or {}).get('cwd') or urllib.parse.unquote(path.parent.parent.name)
    with path.open(errors='replace') as f:
        for raw in f:
            if 'turn_completed' not in raw: continue
            try: event = json.loads(raw)
            except ValueError: continue
            if not isinstance(event, dict): continue
            params = event.get('params') or {}
            if not isinstance(params, dict): continue
            update = params.get('update') or {}
            if not isinstance(update, dict): continue
            if update.get('sessionUpdate') != 'turn_completed': continue
            usage = update.get('usage')
            # Partial subagent totals cannot supply an input/output split.
            if not isinstance(usage, dict): continue
            meta = params.get('_meta') or {}
            session = str(params.get('sessionId') or path.parent.name)
            ts = meta.get('agentTimestampMs') or event.get('timestamp')
            # Event IDs survive copied/forked history, even when session IDs change.
            key = meta.get('eventId') or digest(session, update.get('prompt_id') or [ts, usage])
            models = usage.get('modelUsage') or {
                (update.get('_meta') or {}).get('modelId') or summary.get('current_model_id') or 'unknown': usage}
            if not isinstance(models, dict): continue
            for model, counts in models.items():
                if not isinstance(counts, dict): continue
                read, write = number(counts.get('cachedReadTokens')), number(counts.get('cacheCreationTokens'))
                r = record(digest('grok', key, model), 'grok', session, ts, model, project, 'Grok Build',
                           input=max(0, number(counts.get('inputTokens')) - read - write),
                           output=counts.get('outputTokens'), cacheRead=read, cacheWrite=write,
                           reasoning=counts.get('reasoningTokens'))
                ticks = counts.get('costUsdTicks', usage.get('costUsdTicks') if len(models) == 1 else None)
                r['reportedCostTicks'] = number(ticks) or None
                r['modelCalls'] = number(counts.get('modelCalls', usage.get('modelCalls') if len(models) == 1 else None))
                yield r


def json_records(path):
    with path.open(errors='replace') as stream:
        if path.suffix == '.json':
            value = json.load(stream)
            if isinstance(value, dict): yield value
        else:
            for line in stream:
                try: value = json.loads(line)
                except ValueError: continue
                if isinstance(value, dict): yield value


def gemini_records(path):
    session, project = path.stem, ''
    # The CLI may migrate a JSON conversation to an append-only JSONL file.
    # Stable message IDs deduplicate both copies. Rewind records do not erase
    # tokens already spent; repeated message updates merge in the ledger.
    for entry in json_records(path):
        metadata = entry.get('$set', entry)
        session = str(metadata.get('sessionId') or session)
        project = metadata.get('projectHash') or project
        messages = metadata.get('messages', [entry])
        for message in messages:
            if not isinstance(message, dict) or message.get('type') != 'gemini': continue
            usage = message.get('tokens')
            if not isinstance(usage, dict): continue
            inp, out = number(usage.get('input')), number(usage.get('output'))
            read, thoughts = number(usage.get('cached')), number(usage.get('thoughts'))
            tool = number(usage.get('tool'))
            # Only add tool-prompt tokens when the source total confirms they
            # are outside input. Never add a subset twice.
            if number(usage.get('total')) == inp + out + thoughts + tool: inp += tool
            key = message.get('id') or digest(session, message.get('timestamp'), usage)
            yield record(digest('gemini', key), 'gemini', session, message.get('timestamp'),
                         message.get('model'), 'Gemini project ' + project if project else '', 'Gemini CLI',
                         input=max(0, inp - read), output=out + thoughts, cacheRead=read, reasoning=thoughts)


def muse_records(path):
    session, project = path.parent.name, ''
    with path.open(errors='replace') as f:
        for raw in f:
            try: outer = json.loads(raw)
            except ValueError: continue
            if not isinstance(outer, dict): continue
            # The first line is a retained_frame envelope whose children hold
            # the real records as encoded strings. The rest are direct.
            records = []
            if isinstance(outer.get('children'), list) and not isinstance(outer.get('payload'), dict):
                for child in outer['children']:
                    if not isinstance(child, dict): continue
                    try: records.append(json.loads(child.get('record_json', '')))
                    except (ValueError, AttributeError): continue
            else:
                records = [outer]
            for entry in records:
                if not isinstance(entry, dict): continue
                payload = entry.get('payload')
                if not isinstance(payload, dict): continue
                if payload.get('kind') in ('metadata', 'route_facts'):
                    info = payload.get('record')
                    if not isinstance(info, dict): info = {}
                    project = info.get('workspace_root') or info.get('cwd') or project
                    continue
                # Only model_completed carries per-model token usage. The
                # goal_usage_attribution provider rows duplicate the same
                # counts and its tool rows are zero, so they are ignored.
                event = payload.get('event')
                if not isinstance(event, dict) or event.get('kind') != 'model_completed': continue
                usage = event.get('usage')
                if not isinstance(usage, dict): continue
                stream = entry.get('stream')
                if not isinstance(stream, dict): stream = {}
                session = str(stream.get('id') or session)
                model = event.get('model') or 'unknown'
                response = event.get('response_id')
                key = digest('muse', response) if response else digest('muse', session, entry.get('recorded_at'), model)
                # input_tokens includes reused context, like Codex. The two
                # cache spellings report the same count; never add both.
                read = number(usage.get('cache_read_tokens')) or number(usage.get('cached_tokens'))
                yield record(key, 'muse', session, number(entry.get('recorded_at')) // 1000000,
                             model, project, 'Muse',
                             input=max(0, number(usage.get('input_tokens')) - read),
                             output=usage.get('output_tokens'), cacheRead=read,
                             cacheWrite=usage.get('cache_write_tokens'),
                             reasoning=usage.get('reasoning_tokens'))


CURSOR_API = 'https://api2.cursor.sh/aiserver.v1.DashboardService/'


class CursorApiUnavailable(ValueError):
    """A credential-free message suitable for display."""


def cursor_token():
    # The desktop app stores its session in ItemTable; the value is used
    # verbatim as a Bearer token and is never logged, printed, or persisted.
    database = Path(os.getenv('CURSOR_HOME', str(HOME / '.config/Cursor'))) / 'User/globalStorage/state.vscdb'
    if not database.exists(): return None
    conn = None
    try:
        conn = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
        row = conn.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()
        token = row[0] if row else None
        return token if isinstance(token, str) and token else None
    except (sqlite3.Error, ValueError, TypeError, AttributeError, OSError): return None
    finally:
        if conn is not None: conn.close()


def cursor_api_error(exc):
    if getattr(exc, 'code', None) == 401:
        return 'Cursor sign-in expired. Sign in to Cursor desktop to refresh usage.'
    return 'Cursor cloud usage unavailable. Check the connection and try again.'


def cursor_post(token, method, body):
    request = urllib.request.Request(CURSOR_API + method, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                 'User-Agent': 'Omarchy-AI-Usage/0.1'})
    with urllib.request.urlopen(request, timeout=15) as response: data = json.load(response)
    if not isinstance(data, dict): raise CursorApiUnavailable('Cursor returned an unrecognized usage response.')
    return data


def cursor_summary(token):
    try: return cursor_post(token, 'GetCurrentPeriodUsage', {})
    except CursorApiUnavailable: raise
    except Exception as exc: raise CursorApiUnavailable(cursor_api_error(exc))


def event_ms(value):
    # The API carries millisecond timestamps as digit strings; timestamp()
    # only folds int/float milliseconds, so coerce strings first.
    text = str(value).strip()
    try: return int(text)
    except (ValueError, TypeError): pass
    try: return int(float(text))
    except (ValueError, TypeError, OverflowError): pass
    return int(timestamp(value) * 1000)


def cursor_api_record(ev):
    if not isinstance(ev, dict): ev = {}
    ms = event_ms(ev.get('timestamp'))
    usage = ev.get('tokenUsage')
    if not isinstance(usage, dict): usage = {}
    conv = ev.get('conversationId') or 'cloud'
    model = str(ev.get('model') or 'unknown')
    try: cents = float(usage.get('totalCents'))
    except (ValueError, TypeError): cents = None
    if cents is not None and not math.isfinite(cents): cents = None
    r = record(digest('cursor-api', ms, model, conv, number(usage.get('inputTokens')),
                      number(usage.get('outputTokens')), number(usage.get('cacheReadTokens'))),
               'cursor', conv, ms // 1000, model, '',
               'Cloud' if ev.get('isHeadless') else 'Cursor',
               input=usage.get('inputTokens'), output=usage.get('outputTokens'),
               cacheRead=usage.get('cacheReadTokens'))
    r['turns'] = 1
    # A literal float: reported_value() maps 0 to None, which would unprice
    # free rows. Non-chargeable events already carry 0 cents.
    r['reportedValue'] = max(0.0, cents / 100) if cents is not None else None
    return r


def cursor_fetch_page(token, body):
    try: return cursor_post(token, 'GetFilteredUsageEvents', body)
    except Exception as exc:
        if getattr(exc, 'code', None) == 400 and body.get('pageSize', 0) > 100:
            body['pageSize'] = 100
            return cursor_post(token, 'GetFilteredUsageEvents', body)
        raise


def cursor_walk(token, end=None, stop_ms=0, cycle_ms=0):
    """Buffered walk newest-first. Returns (records, oldest_ms, complete).
    Nothing is written here; the caller commits. Raises CursorApiUnavailable."""
    records, seen, oldest, complete = [], set(), None, False
    try:
        for _ in range(40):
            body = {'pageSize': 1000}
            if end is not None: body['endDate'] = end
            try: data = cursor_fetch_page(token, body)
            except CursorApiUnavailable: raise
            except Exception as exc: raise CursorApiUnavailable(cursor_api_error(exc))
            page = data.get('usageEventsDisplay')
            if not isinstance(page, list): raise CursorApiUnavailable('Cursor returned an unrecognized usage response.')
            fresh = 0
            for ev in page:
                r = cursor_api_record(ev)
                if r['id'] in seen: continue
                seen.add(r['id']); records.append(r); fresh += 1
            stamps = [event_ms(e.get('timestamp')) for e in page if isinstance(e, dict)]
            stamps = [s for s in stamps if s > 0]
            if not stamps: complete = True; break
            oldest = min(stamps)
            if len(page) < body['pageSize'] or not fresh: complete = True; break
            if stop_ms and oldest <= stop_ms: complete = True; break
            if cycle_ms and oldest < cycle_ms: complete = True; break
            if oldest < (time.time() - 400 * 86400) * 1000: complete = True; break
            end = oldest - 1
        else: return records, oldest, False
    except CursorApiUnavailable: raise
    except Exception as exc: raise CursorApiUnavailable(cursor_api_error(exc))
    return records, oldest, complete


def purge_legacy_cursor(ledger):
    # Branch-era local rows are identified by provenance, never by shape, so
    # no API row can match. Both tables stay consistent (see put()).
    ledger.db.execute("DELETE FROM events WHERE provider='cursor' AND id IN "
                      "(SELECT event_id FROM event_sources WHERE path LIKE '%state.vscdb')")
    ledger.db.execute("DELETE FROM event_sources WHERE path LIKE '%state.vscdb' "
                      "AND event_id NOT IN (SELECT id FROM events)")


def cursor_usage(ledger, force=False):
    source = {'provider': 'cursor', 'path': 'cursor-api', 'files': 0, 'exists': True, 'kind': 'cloud'}
    path = STATE / 'cursor-usage.json'
    try: cached = json.loads(path.read_text())
    except (OSError, ValueError): cached = {}
    # Older pagination could mark a truncated pull complete. Rebuild its
    # watermark once; stable event ids make the repair idempotent.
    if cached.get('historyVersion') != 1: cached = {}
    if not force and time.time() - cached.get('attemptedAt', 0) < 300:
        if cached.get('error') or cached.get('fullPullPending'):
            source['readErrors'] = 1
            return source, ['Cursor cloud history is incomplete or unavailable; showing previous records.']
        return source, []
    token = cursor_token()
    if token is None:
        source['readErrors'] = 1
        return source, ['Cursor desktop sign-in not found. Sign in to Cursor to collect cloud usage.']
    try:
        cycle_ms = event_ms(cursor_summary(token).get('billingCycleStart'))
        stop_ms, end = 0, None
        if cached.get('billingCycleStart') == cycle_ms and isinstance(cached.get('newestTs'), int):
            stop_ms = cached['newestTs']
            if cached.get('fullPullPending') and isinstance(cached.get('resumeFloorMs'), int):
                end = cached['resumeFloorMs'] - 1
        # Finish one contiguous history range before collecting new arrivals.
        # Advancing the newest marker during a capped top-up can strand the
        # gap between that top-up and the saved history floor permanently.
        records, oldest, complete = cursor_walk(token, end, 0 if end is not None else stop_ms, cycle_ms)
        newest = (cached.get('newestTs') or 0) if end is not None else max(
            [cached.get('newestTs') or 0] + [r['ts'] * 1000 for r in records])
        if not complete:
            for r in records: ledger.put(r, 'cursor-api')
            cached = {'historyVersion': 1, 'attemptedAt': time.time(), 'billingCycleStart': cycle_ms,
                      'newestTs': newest, 'fullPullPending': True,
                      'resumeFloorMs': oldest or 0, 'error': ''}
            ledger.db.commit()
            atomic_json(path, cached)
            source['readErrors'] = 1
            return source, ['Cursor cloud history is incomplete; the next refresh resumes where this one stopped.']
        for r in records: ledger.put(r, 'cursor-api')
        if records: purge_legacy_cursor(ledger)
        cached = {'historyVersion': 1, 'attemptedAt': time.time(), 'billingCycleStart': cycle_ms,
                  'newestTs': newest, 'fullPullPending': False,
                  'resumeFloorMs': 0, 'error': ''}
        # Persist events before the watermark so an interrupted scan cannot
        # advance past rows that SQLite would roll back.
        ledger.db.commit()
        atomic_json(path, cached)
        return source, []
    except Exception as exc:
        cached['historyVersion'] = 1
        cached['attemptedAt'] = time.time()
        cached['error'] = str(exc) if isinstance(exc, CursorApiUnavailable) else 'Cursor cloud usage unavailable. Check the connection and try again.'
        atomic_json(path, cached)
        source['readErrors'] = 1
        return source, ['Cursor cloud usage unavailable; showing previous records.']


def reported_value(value):
    try:
        amount = float(value)
        return amount if math.isfinite(amount) and amount > 0 else None
    except (TypeError, ValueError): return None


def pi_records(path, provider='pi'):
    session, project = path.stem, ''
    for entry in json_records(path):
        if entry.get('type') == 'session':
            session, project = str(entry.get('id') or session), entry.get('cwd') or project
            continue
        message = entry.get('message') or {}
        if entry.get('type') != 'message' or message.get('role') != 'assistant': continue
        usage = message.get('usage')
        if not isinstance(usage, dict): continue
        ts = message.get('timestamp') or entry.get('timestamp')
        model = message.get('model') or 'unknown'
        # Pi forks retain short entry IDs and original timestamps. Include both
        # so copies merge without collisions between unrelated sessions.
        key = digest(provider, entry.get('id'), ts, model) if entry.get('id') else digest(provider, session, ts, usage)
        r = record(key, provider, session, ts, model, project, PROVIDERS[provider],
                   input=usage.get('input'), output=usage.get('output'), cacheRead=usage.get('cacheRead'),
                   cacheWrite=usage.get('cacheWrite'), cacheWrite1h=usage.get('cacheWrite1h'), reasoning=usage.get('reasoning'))
        r['apiProvider'] = str(message.get('provider') or '')
        r['reportedValue'] = reported_value((usage.get('cost') or {}).get('total'))
        yield r


def opencode_record(mid, sid, ts, project, model, route, usage, cost, provider='opencode'):
    provider = OPENCODE_ROUTE_PROVIDERS.get(provider_key(route), provider)
    cache = usage.get('cache') or {}
    r = record(digest(provider, mid), provider, sid, ts, model, project, 'OpenCode',
               input=usage.get('input'), output=number(usage.get('output')) + number(usage.get('reasoning')),
               reasoning=usage.get('reasoning'), cacheRead=cache.get('read'), cacheWrite=cache.get('write'))
    r['apiProvider'] = str(route or 'unknown')
    # Go keeps the app's own estimate as a fallback for models the catalog
    # does not cover yet. Catalog rates still win where they exist.
    r['reportedValue'] = reported_value(cost)
    return r


def hermes_ledger_path(root):
    return Path(root).expanduser() / 'state.db'


def hermes_records(path):
    """Per-route token totals from the Hermes agent's own usage table.

    Hermes bills OpenCode Go and Ollama Cloud through the same accounts this
    dashboard reads from the CLI apps, and writes nothing to those apps'
    histories, so its ledger is the only local record of that traffic. Rows
    are per session, model, route, and task; the table accumulates in place,
    so a row's id is namespaced by those keys and the ledger keeps the
    largest totals rather than adding a rescan.
    """
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    try:
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute('PRAGMA table_info(session_model_usage)')}
        if not {'session_id', 'model', 'billing_provider', 'first_seen', 'last_seen', 'input_tokens'} <= columns:
            raise ValueError('Not a Hermes usage ledger')
        try: projects = {r[0]: r[1] for r in conn.execute('SELECT id, cwd FROM sessions')}
        except sqlite3.Error: projects = {}
        wanted = ','.join('?' for _ in HERMES_ROUTES)
        for row in conn.execute(f'SELECT * FROM session_model_usage WHERE billing_provider IN ({wanted})', HERMES_ROUTES):
            r = dict(row)
            # Collapse the wire-format variants onto one provider, keeping the
            # raw route on apiProvider for the Routes breakdown.
            route = r['billing_provider']
            provider = HERMES_ROUTE_NAMES.get(route, route)
            session, model = str(r['session_id']), r['model'] or 'unknown'
            task = str(r.get('task') or '')
            # Anchor the row at its first sighting. The table accumulates in
            # place, so using last_seen would move a row's whole total to a
            # later day on every rescan and rewrite the daily history.
            ts = timestamp(r.get('first_seen') or r.get('last_seen'))
            # Hermes records the provider's own completion_tokens, which already
            # include reasoning, so it must not be added again. Verified against
            # this table: of 36 rows carrying reasoning, none has reasoning
            # above output (max ratio 0.989), while OpenCode's disjoint counter
            # exceeds output in 59% of its rows. The row's id covers every key
            # of the source table's primary key so two distinct rows cannot
            # collapse and lose tokens under the ledger's MAX upsert.
            entry = record(digest('hermes', route, session, model, task,
                                  r.get('billing_base_url'), r.get('billing_mode')),
                           provider, session, ts, model,
                           projects.get(r['session_id']) or '', 'Hermes',
                           input=r.get('input_tokens'), output=r.get('output_tokens'),
                           reasoning=r.get('reasoning_tokens'), cacheRead=r.get('cache_read_tokens'),
                           cacheWrite=r.get('cache_write_tokens'))
            entry['timePrecision'] = 'session'
            entry['apiProvider'] = route
            entry['modelCalls'] = number(r.get('api_call_count'))
            entry['reportedValue'] = reported_value(r.get('estimated_cost_usd'))
            yield {'row': entry, 'task': HERMES_TASKS.get(task, task or 'conversation')}
    finally:
        conn.close()


class Ledger:
    def __init__(self, path, readonly=False):
        if readonly and path.exists():
            self.db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=60)
            self.db.execute('BEGIN')
            return
        if readonly:
            self.db = sqlite3.connect(':memory:')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=60)
            os.chmod(path, 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, provider TEXT, session TEXT, ts INTEGER,
            model TEXT, project TEXT, client TEXT,
            input INTEGER, output INTEGER, cacheRead INTEGER, cacheWrite INTEGER,
            cacheWrite1h INTEGER, reasoning INTEGER);
          CREATE INDEX IF NOT EXISTS events_time ON events(ts);
          CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER);
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS event_sources(event_id TEXT, path TEXT, PRIMARY KEY(event_id,path));
        ''')
        current = self.db.execute("SELECT value FROM metadata WHERE key='provenanceVersion'").fetchone()
        if not current:
            # Re-index source locations once, without deleting retained metrics.
            self.db.execute('DELETE FROM files')
            self.db.execute("INSERT INTO metadata VALUES ('provenanceVersion','2')")
        elif current[0] != '2':
            # Version 2 corrects the Hermes parser: its output no longer has
            # reasoning added on top, its rows are anchored at first sighting,
            # and its event ids cover the source table's whole primary key.
            # Events are keyed by id, so the corrected values need those rows
            # re-read rather than upserted over the old ones.
            stale = [r[0] for r in self.db.execute(
                "SELECT DISTINCT event_id FROM event_sources WHERE path LIKE '%state.db'")]
            for event in stale:
                self.db.execute('DELETE FROM events WHERE id=?', (event,))
            self.db.execute("DELETE FROM event_sources WHERE path LIKE '%state.db'")
            self.db.execute("UPDATE metadata SET value='2' WHERE key='provenanceVersion'")
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(events)')}
        for name in ('reportedCostTicks', 'modelCalls', 'turns'):
            if name not in columns: self.db.execute(f'ALTER TABLE events ADD COLUMN {name} INTEGER')
        for name, kind in [('reportedValue', 'REAL'), ('apiProvider', 'TEXT')]:
            if name not in columns: self.db.execute(f'ALTER TABLE events ADD COLUMN {name} {kind}')
        if 'timePrecision' not in columns:
            self.db.execute('ALTER TABLE events ADD COLUMN timePrecision TEXT')
        # The tier the server reports it used, and the tier the client asked for.
        for name in ('serviceTier', 'requestedServiceTier'):
            if name not in columns: self.db.execute(f'ALTER TABLE events ADD COLUMN {name} TEXT')
        if self.db.execute("SELECT value FROM metadata WHERE key='serviceTierVersion'").fetchone() != ('2',):
            # Re-read Codex rollouts once so retained events gain the tier their
            # transcript recorded. Events are upserted by id, so token totals do
            # not change, and events whose transcript is gone keep no tier.
            self.db.execute("DELETE FROM files WHERE path LIKE '%rollout-%.jsonl'")
            self.db.execute("DELETE FROM metadata WHERE key='ledgerSignature'")
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('serviceTierVersion','2')")

    def put(self, r, source=None, growing=False):
        if not r['ts'] or (not sum(r[f] for f in FIELDS[:4]) and not r.get('turns')): return
        keys = list(r)
        # Claude streams may repeat a message with a larger final usage count.
        update = ','.join(f'{f}=MAX(events.{f},excluded.{f})' for f in FIELDS)
        # A source whose row accumulates in place also moves forward in time;
        # every other source keeps the timestamp it first recorded.
        if growing: update += ',ts=MIN(events.ts,excluded.ts)'
        update += ',reportedCostTicks=COALESCE(MAX(COALESCE(events.reportedCostTicks,0),excluded.reportedCostTicks),events.reportedCostTicks)'
        update += ',modelCalls=MAX(COALESCE(events.modelCalls,0),COALESCE(excluded.modelCalls,0))'
        update += ',turns=MAX(COALESCE(events.turns,0),COALESCE(excluded.turns,0))'
        update += ",reportedValue=CASE WHEN excluded.provider='cursor' THEN excluded.reportedValue ELSE COALESCE(MAX(COALESCE(events.reportedValue,0),excluded.reportedValue),events.reportedValue) END"
        update += ',apiProvider=COALESCE(excluded.apiProvider,events.apiProvider)'
        update += ',timePrecision=COALESCE(excluded.timePrecision,events.timePrecision)'
        update += ',serviceTier=COALESCE(excluded.serviceTier,events.serviceTier)'
        # Local parsing is authoritative even when the tier becomes unknown.
        # An imported snapshot may enrich an unknown tier, but cannot replace
        # evidence from this machine's source file.
        if source is not None and 'requestedServiceTier' in r:
            update += ',requestedServiceTier=excluded.requestedServiceTier'
        else:
            incoming = 'excluded.requestedServiceTier' if 'requestedServiceTier' in r else 'events.requestedServiceTier'
            update += ",requestedServiceTier=CASE WHEN EXISTS (SELECT 1 FROM event_sources WHERE event_id=events.id AND path NOT LIKE 'machine:%') THEN events.requestedServiceTier ELSE " + incoming + ' END'
        before_tier = self.db.execute('SELECT serviceTier,requestedServiceTier FROM events WHERE id=?', (r['id'],)).fetchone()
        self.db.execute(f'INSERT INTO events ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)}) '
                        f'ON CONFLICT(id) DO UPDATE SET {update}', list(r.values()))
        after_tier = self.db.execute('SELECT serviceTier,requestedServiceTier FROM events WHERE id=?', (r['id'],)).fetchone()
        if before_tier != after_tier:
            self.db.execute("DELETE FROM metadata WHERE key='ledgerSignature'")
        if source is not None:
            self.db.execute('INSERT OR IGNORE INTO event_sources VALUES (?,?)', (r['id'], str(source)))

    def sync_ledgers(self, cfg):
        # Each machine writes a consistent ledger snapshot (VACUUM INTO) to a
        # shared folder and imports the other snapshots. Event ids deduplicate;
        # local provenance always wins over a machine-prefixed copy.
        directory = str(cfg.get('ledgerSyncDir') or '').strip()
        if not directory: return []
        directory = Path(directory).expanduser()
        device = str(cfg.get('ledgerDeviceId') or '').strip() or socket.gethostname()
        warnings = []
        try: directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return ['Could not create the synced ledger folder.']
        snapshot = directory / (device + '.sqlite')
        signature = list(self.db.execute('SELECT COUNT(*),COALESCE(MAX(ts),0),COALESCE(SUM(input+output+cacheRead+cacheWrite),0) FROM events').fetchone())
        stored = self.db.execute("SELECT value FROM metadata WHERE key='ledgerSignature'").fetchone()
        if not stored or stored[0] != json.dumps(signature):
            temporary = directory / ('.' + device + '.tmp.sqlite')
            try:
                if temporary.exists(): temporary.unlink()
                self.db.commit()
                self.db.execute('VACUUM INTO ?', (str(temporary),))
                os.chmod(temporary, 0o600)
                os.replace(temporary, snapshot)
                self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('ledgerSignature', json.dumps(signature)))
            except (OSError, sqlite3.Error):
                warnings.append('Could not write the synced ledger snapshot.')
                if temporary.exists(): temporary.unlink(missing_ok=True)
        for path in sorted(directory.glob('*.sqlite')):
            if path.name == device + '.sqlite': continue
            stamp = None
            try:
                stamp = path.stat()
                if self.db.execute('SELECT size,mtime FROM files WHERE path=?', (str(path),)).fetchone() == (stamp.st_size, stamp.st_mtime_ns): continue
                self.import_ledger(path, path.stem)
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                warnings.append('Could not read synced ledger ' + path.name)
            # Record the attempted file so a broken snapshot warns once, not every scan.
            if stamp is not None:
                self.db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?)', (str(path), stamp.st_size, stamp.st_mtime_ns))
        self.db.commit()
        return warnings

    def import_ledger(self, path, device):
        ours = {row[1] for row in self.db.execute('PRAGMA table_info(events)')}
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
        try:
            conn.row_factory = sqlite3.Row
            columns = {row[1] for row in conn.execute('PRAGMA table_info(events)')}
            if not {'id', 'provider', 'session', 'ts', 'model', 'project', 'client', *FIELDS} <= columns:
                raise ValueError('Not a usage ledger')
            try: sources = list(conn.execute('SELECT event_id,path FROM event_sources'))
            except sqlite3.Error: sources = []
            native_events = {event for event, original in sources if not original.startswith('machine:')}
            for row in conn.execute('SELECT * FROM events'):
                entry = {key: row[key] for key in row.keys() if key in ours}
                # A peer can seed a new event, but only the machine with the
                # transcript can correct its speed. Re-exported copies may be
                # stale, including after the owner explicitly cleared a tier.
                if row['id'] not in native_events and self.db.execute(
                        'SELECT 1 FROM events WHERE id=?', (row['id'],)).fetchone():
                    entry.pop('serviceTier', None)
                    entry.pop('requestedServiceTier', None)
                self.put(entry, None)
            for event, original in sources:
                # Keep one provenance per event so imported copies cannot
                # conflict with a local account or another machine.
                self.db.execute('INSERT INTO event_sources(event_id,path) SELECT ?,? '
                                'WHERE NOT EXISTS (SELECT 1 FROM event_sources WHERE event_id=?)',
                                (event, f'machine:{device}/{original}', event))
        finally:
            conn.close()

    def scan(self, cfg, force=False, local_only=False):
        cfg = history_config(cfg)
        for account in cfg.get('accounts', []):
            for directory in account['directories']:
                key = ('opencode' if directory['provider'] == 'opencode-go' else directory['provider']) + 'Homes'
                cfg[key] = cfg.get(key, []) + [directory['path']]
        warnings, sources, t3_instances = [], [], t3_provider_instances()
        for provider, roots, parser in (
            ('codex', [os.getenv('CODEX_HOME', str(HOME / '.codex'))] + cfg['codexHomes'], codex_records),
            ('claude', [os.getenv('CLAUDE_CONFIG_DIR', str(HOME / '.claude'))] + cfg['claudeHomes'], claude_records),
            ('grok', [os.getenv('GROK_HOME', str(HOME / '.grok'))] + cfg.get('grokHomes', []), grok_records),
            ('gemini', [str(HOME / '.gemini')] + cfg.get('geminiHomes', []), gemini_records),
            ('pi', [os.getenv('PI_CODING_AGENT_DIR', str(HOME / '.pi/agent'))] + cfg.get('piHomes', []), pi_records),
            ('omp', [str(HOME / '.omp/agent')] + cfg.get('ompHomes', []), lambda path: pi_records(path, 'omp')),
            ('muse', [os.getenv('MUSE_HOME') or str(Path(os.getenv('XDG_DATA_HOME', HOME / '.local/share')) / 'muse')] + cfg.get('museHomes', []), muse_records),
            ('commandcode', [str(HOME / '.commandcode')] + cfg.get('commandcodeHomes', []), commandcode_records)):
            root_providers = {str(Path(root).expanduser()): provider for root in roots}
            if provider in ('codex', 'claude'):
                for item in t3_instances:
                    if item['driver'] == provider: root_providers[item['root']] = item['provider']
            for root, record_provider in sorted(root_providers.items()):
                root = Path(root).expanduser()
                folders = [root / 'sessions', root / 'archived_sessions'] if provider == 'codex' else [root / {'claude': 'projects', 'gemini': 'tmp', 'commandcode': 'projects'}.get(provider, 'sessions')]
                for folder in folders:
                    pattern = 'updates.jsonl' if provider == 'grok' else '*.json*' if provider == 'gemini' else 'session.jsonl' if provider == 'muse' else '*.jsonl'
                    files = sorted(folder.rglob(pattern)) if folder.exists() else []
                    if provider == 'gemini': files = [p for p in files if p.suffix in ('.json', '.jsonl') and 'chats' in p.relative_to(folder).parts[:-1]]
                    source = {'provider': record_provider, 'path': str(folder), 'files': len(files), 'exists': folder.exists(),
                              'latestFileAt': None, 'readErrors': 0, 'kind': 'archive' if folder.name == 'archived_sessions' else 'history'}
                    sources.append(source)
                    for p in files:
                        try:
                            stat = p.stat()
                            source['latestFileAt'] = max(source['latestFileAt'] or 0, stat.st_mtime)
                            old = self.db.execute('SELECT size,mtime FROM files WHERE path=?', (str(p),)).fetchone()
                            if old == (stat.st_size, stat.st_mtime_ns): continue
                            parsed = parser(p, record_provider) if provider in ('codex', 'claude') else parser(p)
                            for r in parsed: self.put(r, p.resolve())
                            self.db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?)', (str(p), stat.st_size, stat.st_mtime_ns))
                        except (OSError, ValueError, TypeError, AttributeError):
                            source['readErrors'] += 1
                            warnings.append(f'Could not read {p.name}')
        opencode_roots = {str(Path(os.getenv('XDG_DATA_HOME', HOME / '.local/share')) / 'opencode'): 'opencode'}
        opencode_roots.update({str(Path(root).expanduser()): 'opencode' for root in cfg.get('opencodeHomes', [])})
        for item in t3_instances:
            if item['driver'] == 'opencode': opencode_roots[item['root']] = item['provider']
        for root, default_provider in sorted(opencode_roots.items()):
            root = Path(root).expanduser()
            opencode = root / 'opencode.db'
            source = {'provider': 'opencode', 'path': str(opencode), 'files': int(opencode.exists()), 'exists': opencode.exists(), 'kind': 'database'}
            sources.append(source)
            if opencode.exists() and (not local_only or self.database_changed(opencode)):
                conn = None
                try:
                    conn = sqlite3.connect(opencode.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
                    # Metric fields only. Go has its own card and stable IDs;
                    # other routes stay under OpenCode and never enter Go totals.
                    # A database can carry the v1 tables, the v2 tables, or both,
                    # so each query runs only when its table is present.
                    tables = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    queries = []
                    if 'message' in tables:
                        queries.append("""SELECT m.id,m.session_id,m.time_created,s.directory,
                          json_extract(m.data,'$.modelID'),json_extract(m.data,'$.providerID'),
                          json_extract(m.data,'$.tokens'),json_extract(m.data,'$.cost')
                          FROM message m LEFT JOIN session s ON s.id=m.session_id
                          WHERE json_extract(m.data,'$.role')='assistant' """)
                    if 'session_message' in tables:
                        # OpenCode v2 writes new messages to session_message and
                        # migrated the v1 rows with their original ids, so a row
                        # present in both tables upserts once under the same id.
                        directory = 's.directory' if 'session_v2' in tables else 'NULL'
                        join = 'LEFT JOIN session_v2 s ON s.id=m.session_id' if 'session_v2' in tables else ''
                        queries.append(f"""SELECT m.id,m.session_id,m.time_created,{directory},
                          json_extract(m.data,'$.model.id'),json_extract(m.data,'$.model.providerID'),
                          json_extract(m.data,'$.tokens'),json_extract(m.data,'$.cost')
                          FROM session_message m {join}
                          WHERE m.type='assistant' """)
                    for query in queries:
                        for mid, sid, ts, project, model, route, raw, cost in conn.execute(query):
                            self.put(opencode_record(mid, sid, ts, project, model, route,
                                                     json.loads(raw or '{}'), cost, default_provider), opencode.resolve())
                    if local_only: self.remember_database(opencode)
                except (sqlite3.Error, ValueError, TypeError, AttributeError):
                    source['readErrors'] = 1
                    warnings.append('OpenCode database could not be read; retained previous records.')
                finally:
                    if conn is not None: conn.close()
            legacy = root / 'storage/message'
            if legacy.exists():
                files = sorted(legacy.rglob('*.json'))
                source = {'provider': 'opencode', 'path': str(legacy), 'files': len(files), 'exists': True, 'kind': 'legacy history'}
                sources.append(source)
                for path in files:
                    try:
                        stat = path.stat()
                        if local_only and self.db.execute('SELECT size,mtime FROM files WHERE path=?', (str(path),)).fetchone() == (stat.st_size, stat.st_mtime_ns):
                            continue
                        for item in json_records(path):
                            if item.get('role') != 'assistant': continue
                            self.put(opencode_record(item.get('id') or path.stem, item.get('sessionID') or path.parent.name,
                                (item.get('time') or {}).get('created'), (item.get('path') or {}).get('cwd'),
                                item.get('modelID'), item.get('providerID'), item.get('tokens') or {}, item.get('cost'),
                                default_provider), path.resolve())
                        if local_only:
                            self.db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?)', (str(path), stat.st_size, stat.st_mtime_ns))
                    except (OSError, ValueError, TypeError, AttributeError):
                        source['readErrors'] = source.get('readErrors', 0) + 1
        hermes_roots = [str(HOME / '.hermes')] + cfg.get('hermesHomes', [])
        for root in sorted(set(hermes_roots)):
            path = hermes_ledger_path(root)
            source = {'provider': 'hermes', 'path': str(path), 'files': int(path.exists()),
                      'exists': path.exists(), 'kind': 'database', 'clients': list(HERMES_ROUTES)}
            sources.append(source)
            if not path.exists() or (local_only and not self.database_changed(path)): continue
            try:
                for item in hermes_records(path):
                    # The task dimension the CLI apps do not record, kept in the
                    # client field so the breakdowns distinguish typed prompts
                    # from background work the agent did on its own.
                    entry = item['row']
                    if item['task'] != 'conversation':
                        entry['client'] = 'Hermes · ' + item['task']
                    self.put(entry, path.resolve(), growing=True)
                if local_only: self.remember_database(path)
            except (sqlite3.Error, ValueError, TypeError, AttributeError, KeyError):
                source['readErrors'] = 1
                warnings.append('Hermes database could not be read; retained previous records.')

        if not local_only and 'cursor' in cfg.get('enabled', []) and os.getenv('AI_USAGE_DEMO') != '1':
            csource, cwarnings = cursor_usage(self, force=force)
            sources.append(csource)
            warnings.extend(cwarnings)
        for source in sources:
            source['status'] = 'missing' if not source['exists'] else 'partial' if source.get('readErrors') else 'available'
        if not local_only: warnings.extend(self.sync_ledgers(cfg))
        meta = {'sources': sources, 'warnings': warnings, 'scannedAt': time.time(), 'machine': 'demo-computer' if os.getenv('AI_USAGE_DEMO') == '1' else socket.gethostname()}
        if not local_only:
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('scan', json.dumps(meta)))
        self.db.commit()
        return meta

    def database_signature(self, path):
        # SQLite writers can leave the main file untouched while appending to
        # WAL. Include both so an active OpenCode or Hermes database is read
        # again when its usage changes.
        signature = []
        for candidate in (path, Path(str(path) + '-wal')):
            try:
                stat = candidate.stat()
                signature.append((stat.st_size, stat.st_mtime_ns))
            except OSError:
                signature.append(None)
        return json.dumps(signature)

    def database_changed(self, path):
        stored = self.db.execute('SELECT value FROM metadata WHERE key=?', ('pulseDatabase:' + str(path),)).fetchone()
        return not stored or stored[0] != self.database_signature(path)

    def remember_database(self, path):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                        ('pulseDatabase:' + str(path), self.database_signature(path)))


def load_rates():
    path = STATE / 'rates.json'
    # Bundled, attributed snapshot works offline. Documented official overrides
    # come next. A user catalog always wins where it sets a rate.
    try:
        data = json.loads(Path(__file__).with_name('catalog.json').read_text())
        if not isinstance(data.get('document'), dict): raise ValueError('Invalid catalog')
    except (OSError, ValueError, AttributeError):
        data = {'document': {}, 'source': 'Pricing catalog unavailable', 'fetchedAtMs': None}
    else:
        data.setdefault('source', 'Bundled pricing catalog')
    for name, label in OVERRIDES:
        try:
            official = json.loads(Path(__file__).with_name(name).read_text())
            if not isinstance(official.get('models'), dict): raise ValueError('Invalid override')
            verified = official['verifiedAt']
            if not isinstance(verified, str): raise ValueError('Invalid override')
            data['document'].update(official['models'])
            data['source'] += f' + {label} (' + verified + ')'
        except (OSError, ValueError, TypeError, KeyError): pass
    # Preserve user rates on top of every bundled and official entry.
    if path.exists():
        try:
            user = json.loads(path.read_text())
            if not isinstance(user.get('document'), dict): raise ValueError('Invalid catalog')
            data['document'] = data['document'] | user['document']
            data['source'] = user.get('source', 'User pricing catalog') + ' + bundled and official models'
        except (OSError, ValueError, TypeError, KeyError, AttributeError): pass
    return data


def peak_rate(rate, ts):
    multiplier, hours = rate.get('peak_cost_multiplier'), rate.get('peak_hours_utc')
    if not ts or not isinstance(multiplier, (int, float)) or not isinstance(hours, list): return rate
    moment = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    if rate.get('peak_weekdays_only') and moment.weekday() >= 5: return rate
    if not any(isinstance(window, list) and len(window) == 2 and window[0] <= moment.hour < window[1] for window in hours): return rate
    return {key: (value * multiplier if isinstance(value, (int, float)) and 'cost' in key and 'token' in key else value)
            for key, value in rate.items()}


def record_tier(r):
    """The tier a record is valued at and the evidence behind it.

    The served tier wins over the requested one, since a Fast request the
    server downgrades is billed at Standard. Neither recorded gives (None, None).
    """
    if r.get('serviceTier'): return r['serviceTier'], 'served'
    if r.get('requestedServiceTier'): return r['requestedServiceTier'], 'requested'
    return None, None


def price(r, catalog):
    return price_detail(r, catalog)[:2]


def price_detail(r, catalog):
    """(value, cache savings, (tier, basis)) for one record.

    basis is 'served' or 'requested' when a recorded tier chose the rates,
    'assumed' when the model has tier rates but the record names no tier and
    Standard rates were used, and None when tiers did not enter the price.
    """
    untiered = (None, None)
    if r['provider'] == 'grok':
        ticks = r.get('reportedCostTicks')
        return ((ticks / 10_000_000_000, None) if ticks else (None, None)) + (untiered,)
    if r['provider'] == 'cursor': return r.get('reportedValue'), None, untiered
    if r['provider'] in ('opencode', 'pi', 'omp') and r.get('reportedValue') is not None:
        return r['reportedValue'], None, untiered
    fallback = r.get('reportedValue') if r['provider'] == 'opencode-go' else None
    fallback = (fallback, None, untiered) if fallback is not None else (None, None, untiered)
    model = r['model']
    lookup = (r.get('apiProvider') or r['provider']) + '/' + model
    # CommandCode bills one resale table for the whole product, so both of its
    # wire-format routes read the same namespaced keys rather than falling
    # through to the lab's own list price for a same-named model.
    if r['provider'] in ('commandcode', 'clinepass'):
        lookup = r['provider'] + '/' + model
    rate = catalog.get(lookup)
    if not rate and r['provider'] not in ('ollama-cloud', 'commandcode', 'clinepass'):
        rate = catalog.get(model) or catalog.get('anthropic/' + model) or catalog.get('openai/' + model) or catalog.get('gemini/' + model)
    if not rate: return fallback
    # Internal models have no published rate and are not billed per token.
    if rate.get('internal'): return 0.0, None, untiered
    rate = peak_rate(rate, r['ts'])
    context = r['input'] + r['cacheRead'] + r['cacheWrite']
    suffix = ''
    for threshold, candidate in [(200000, '_above_200k_tokens'), (256000, '_above_256k_tokens'), (272000, '_above_272k_tokens')]:
        if context > threshold and 'input_cost_per_token' + candidate in rate: suffix = candidate
    tier, basis = record_tier(r)
    # Only Codex records a speed, so only its unknown tiers are an assumption
    # worth reporting; other sources never claim a tier.
    if tier is None and r['provider'] == 'codex' and 'input_cost_per_token_priority' in rate: basis = 'assumed'
    # Standard is the unsuffixed rate. Any other tier reads the catalog's
    # tier-suffixed rate for the same context band; a tier with no published
    # rate leaves the record unpriced rather than borrowing Standard.
    tag = '' if tier in (None, 'default') else '_' + tier
    def cost(key, fallback=None):
        if not tag: return rate.get(key + suffix, rate.get(key, fallback))
        if key + suffix + tag in rate or key + suffix not in rate: return rate.get(key + suffix + tag, rate.get(key + tag))
        return None
    inp, out = cost('input_cost_per_token'), cost('output_cost_per_token')
    read = cost('cache_read_input_token_cost')
    write = cost('cache_creation_input_token_cost')
    write1h = cost('cache_creation_input_token_cost_above_1hr', None)
    parts = [(r['input'], inp), (r['output'], out), (r['cacheRead'], read),
             (max(0, r['cacheWrite'] - r['cacheWrite1h']), write), (r['cacheWrite1h'], write1h)]
    if any(n and not isinstance(v, (int, float)) for n, v in parts): return fallback
    value = sum(n * (v or 0) for n, v in parts)
    saving = max(0, r['cacheRead'] * ((inp or 0) - (read or 0)))
    return value, saving, (tier, basis)


def bucket():
    return {**{f: 0 for f in FIELDS}, 'tokens': 0, 'value': 0.0, 'cacheSavings': 0.0,
            'unpricedTokens': 0, 'requests': 0, 'modelCalls': 0, 'turns': 0, 'sessions': set(),
            'fastTokens': 0, 'tierServedTokens': 0, 'tierRequestedTokens': 0, 'tierAssumedTokens': 0}


def add(b, r, value, savings, tier=(None, None)):
    for f in FIELDS: b[f] += r[f]
    total = sum(r[f] for f in FIELDS[:4])
    b['tokens'] += total; b['requests'] += 1; b['sessions'].add(r['provider'] + ':' + r['session'])
    b['modelCalls'] += r.get('modelCalls') or 0
    b['turns'] += r.get('turns') or 0
    if value is None: b['unpricedTokens'] += total
    else:
        b['value'] += value; b['cacheSavings'] += savings or 0
        # Priced tokens by the evidence behind their tier, so a view can say
        # how much of its estimate rests on a recorded speed.
        name, basis = tier
        if basis: b['tier' + basis.capitalize() + 'Tokens'] += total
        if name == 'priority': b['fastTokens'] += total


def finish(b):
    return b | {'sessions': len(b['sessions']), 'tokensPerSession': b['tokens'] / len(b['sessions']) if b['sessions'] else None,
                'valuePerSession': b['value'] / len(b['sessions']) if b['sessions'] else None}


def go_quota(force=False):
    path = STATE / 'go-quota.json'
    try: cached = json.loads(path.read_text())
    except (OSError, ValueError): cached = {}
    if not force and time.time() - cached.get('attemptedAt', 0) < 300: return cached
    try:
        auth_path = Path(os.getenv('XDG_DATA_HOME', HOME / '.local/share')) / 'opencode/auth.json'
        auth = json.loads(auth_path.read_text()).get('opencode-go', {})
        key = auth.get('key') if auth.get('type') == 'api' else None
        if not key: raise QuotaUnavailable('Connect OpenCode Go in OpenCode to read quota.')
        request = urllib.request.Request('https://opencode.ai/zen/go/v1/usage',
                    headers={'Authorization': 'Bearer ' + key, 'User-Agent': 'Omarchy-AI-Usage/0.1'})
        with urllib.request.urlopen(request, timeout=12) as response: data = json.load(response)
        usage = data.get('usage')
        if not isinstance(usage, dict): raise QuotaUnavailable('Go returned an unrecognized usage response.')
        windows = []
        for name, label in [('rolling', 'Session (5-hour)'), ('weekly', 'Weekly (7-day)'), ('monthly', 'Monthly')]:
            w = usage.get(name)
            if not isinstance(w, dict) or not isinstance(w.get('percent'), (float, int)): continue
            reset = w.get('resetsAt')
            if isinstance(reset, (float, int)): reset = dt.datetime.fromtimestamp(timestamp(reset), dt.timezone.utc).isoformat()
            windows.append({'label': label, 'percent': w['percent'] / 100, 'resetsAt': reset or ''})
        if not windows: raise QuotaUnavailable('Go returned no recognized quota windows.')
        cached = {'limits': windows, 'updatedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'error': ''}
    except Exception as exc:
        # Do not expose credential-bearing request objects or raw response bodies.
        cached['error'] = str(exc) if isinstance(exc, QuotaUnavailable) else 'Go quota unavailable. Check the connection in OpenCode.'
    cached['attemptedAt'] = time.time()
    atomic_json(path, cached)
    return cached


def credit_snapshots():
    try: data = json.loads((STATE / 'chatgpt-credit-snapshots.json').read_text())
    except (OSError, ValueError): return {}
    return data if isinstance(data, dict) else {}


def account_quotas():
    # Labelled accounts may have their own agent usage records (for example a
    # second Codex home with its own collector). Index them by record id and
    # by record name so an account can pick up its limits when either matches.
    by_id, by_name = {}, {}
    credits = credit_snapshots()
    try: paths = sorted((STATE.parent / 'agents/usage').glob('*.json'))
    except OSError: paths = []
    for path in paths:
        try: d = json.loads(path.read_text())
        except (OSError, ValueError): continue
        if not isinstance(d, dict): continue
        if path.stem in credits: d['chatgptCredits'] = credits[path.stem]
        if not (d.get('limits') or d.get('usageStatusText') or d.get('chatgptCredits')): continue
        record = {'limits': d.get('limits', []), 'updatedAt': d.get('updatedAt'), 'error': d.get('usageStatusText', ''), 'plan': d.get('tierLabel', '')}
        record['provider'] = record_provider(path.stem, d)
        if d.get('chatgptCredits'): record['chatgptCredits'] = d['chatgptCredits']
        by_id[path.stem] = record
        if d.get('name') and path.stem not in PROVIDERS:
            name = str(d['name']).casefold()
            by_name[name] = record if name not in by_name else None
    return by_id, by_name


def quota(provider):
    if provider in ('opencode', 'pi', 'omp'):
        return {'limits': [], 'error': 'Account limits belong to the underlying provider and are not collected here.'}
    if provider in ('opencode-go', 'grok', 'muse', 'ollama-cloud', 'commandcode', 'clinepass', 'cursor'):
        try: d = json.loads((STATE / {'opencode-go': 'go-quota.json', 'grok': 'grok-quota.json', 'muse': 'muse-quota.json',
                                      'ollama-cloud': 'ollama-quota.json',
                                      'commandcode': 'commandcode-quota.json',
                                      'clinepass': 'clinepass-quota.json',
                                      'cursor': 'cursor-quota.json'}[provider]).read_text())
        except (OSError, ValueError): d = {}
        result = {'limits': d.get('limits', []), 'updatedAt': d.get('updatedAt'), 'error': d.get('error', '')}
        if provider in ('muse', 'ollama-cloud', 'commandcode', 'clinepass'): result['plan'] = d.get('plan', '')
        # A prepaid wallet a provider reports alongside its windows rides
        # through to the card unchanged.
        if d.get('balance'): result['balance'] = d['balance']
        return result
    p = STATE.parent / 'agents/usage' / (provider + '.json')
    try:
        d = json.loads(p.read_text())
        credits = credit_snapshots()
        if provider in credits: d['chatgptCredits'] = credits[provider]
        result = {'limits': d.get('limits', []), 'updatedAt': d.get('updatedAt'), 'error': d.get('usageStatusText', ''), 'plan': d.get('tierLabel', '')}
        if d.get('chatgptCredits'): result['chatgptCredits'] = d['chatgptCredits']
        return result
    except (OSError, ValueError): return {'limits': [], 'error': 'Local token history only. No account quota snapshot is available.'}


class QuotaUnavailable(ValueError):
    """A credential-free message suitable for display."""


def proto_fields(data):
    index = 0
    def varint():
        nonlocal index
        value = 0
        for shift in range(0, 70, 7):
            if index >= len(data): raise ValueError('Truncated quota response.')
            byte = data[index]; index += 1
            value |= (byte & 127) << shift
            if byte < 128: return value
        raise ValueError('Invalid quota response.')
    while index < len(data):
        key = varint(); wire = key & 7
        if not key >> 3: raise ValueError('Invalid quota field.')
        if wire == 0: value = varint()
        else:
            length = varint() if wire == 2 else {1: 8, 5: 4}.get(wire)
            if length is None or index + length > len(data): raise ValueError('Invalid quota field.')
            value = data[index:index + length]; index += length
        yield key >> 3, wire, value


def grok_billing(data):
    index, config, percent, reset = 0, False, 0.0, None
    while index < len(data):
        if index + 5 > len(data): raise ValueError('Truncated Grok quota response.')
        flag = data[index]; length = int.from_bytes(data[index + 1:index + 5], 'big'); index += 5
        payload = data[index:index + length]; index += length
        if len(payload) != length: raise ValueError('Truncated Grok quota response.')
        if flag == 128:
            for line in payload.decode('ascii').splitlines():
                name, _, value = line.partition(':')
                if name.strip().lower() == 'grpc-status' and value.strip() != '0':
                    raise QuotaUnavailable('Grok quota unavailable. Run grok login if the session expired.')
        elif flag == 0:
            for n, wire, body in proto_fields(payload):
                if n != 1 or wire != 2: continue
                config = True
                for field, kind, value in proto_fields(body):
                    if field == 1 and kind == 5: percent = struct.unpack('<f', value)[0]
                    if field == 5 and kind == 2:
                        reset = next((v for n, w, v in proto_fields(value) if n == 1 and w == 0), None)
        else: raise ValueError('Unsupported Grok quota response.')
    if not config or not 0 <= percent < float('inf'): raise ValueError('Unrecognized Grok quota response.')
    limit = {'label': 'Weekly', 'percent': percent / 100}
    if reset: limit['resetsAt'] = dt.datetime.fromtimestamp(reset, dt.timezone.utc).isoformat()
    return [limit]


def grok_quota(force=False):
    path = STATE / 'grok-quota.json'
    auth_path = Path(os.getenv('GROK_HOME', HOME / '.grok')) / 'auth.json'
    try:
        stat = auth_path.stat()
        auth_version = [stat.st_mtime_ns, stat.st_size]
    except OSError: auth_version = None
    try: cached = json.loads(path.read_text())
    except (OSError, ValueError): cached = {}
    if not force and cached.get('authVersion') == auth_version and time.time() - cached.get('attemptedAt', 0) < 300: return cached
    try:
        auth = json.loads(auth_path.read_text())
        entry = next((v for v in auth.values() if isinstance(v, dict) and v.get('key')), {})
        if not entry: raise QuotaUnavailable('Run grok login to read Grok quota.')
        expires = timestamp(entry.get('expires_at'))
        if expires and expires <= time.time(): raise QuotaUnavailable('Grok sign-in expired. Run grok login to refresh quota.')
        request = urllib.request.Request('https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig',
            data=bytes(5), headers={'Authorization': 'Bearer ' + entry['key'],
                'Content-Type': 'application/grpc-web+proto', 'x-grpc-web': '1',
                'Origin': 'https://grok.com', 'User-Agent': 'Omarchy-AI-Usage/0.1'})
        with urllib.request.urlopen(request, timeout=12) as response: limits = grok_billing(response.read())
        cached = {'limits': limits, 'updatedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'error': ''}
    except Exception as exc:
        cached['error'] = str(exc) if isinstance(exc, QuotaUnavailable) else 'Grok quota unavailable. Check your Grok login.'
    cached['attemptedAt'] = time.time()
    cached['authVersion'] = auth_version
    atomic_json(path, cached)
    return cached


def muse_quota(force=False):
    path = STATE / 'muse-quota.json'
    auth_path = Path(os.getenv('XDG_CONFIG_HOME', HOME / '.config')) / 'muse/auth.json'
    try:
        stat = auth_path.stat()
        auth_version = [stat.st_mtime_ns, stat.st_size]
    except OSError: auth_version = None
    try: cached = json.loads(path.read_text())
    except (OSError, ValueError): cached = {}
    if not force and cached.get('authVersion') == auth_version and time.time() - cached.get('attemptedAt', 0) < 300: return cached
    try:
        auth = json.loads(auth_path.read_text())
        meta = (auth.get('providers') or {}).get('meta') or {}
        token = meta.get('access_token')
        if not token: raise QuotaUnavailable('Run muse login to read Muse quota.')
        base = str(meta.get('api_base_url') or 'https://api.meta.ai/v1').rstrip('/')
        if base.endswith('/v1'): base = base[:-len('/v1')]
        request = urllib.request.Request(base + '/muse-code/key', data=json.dumps({}).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                     'User-Agent': 'Omarchy-AI-Usage/0.1'})
        with urllib.request.urlopen(request, timeout=12) as response: minted = json.load(response)
        if not isinstance(minted, dict): raise ValueError('Muse returned an unrecognized quota response.')
        usage = minted.get('subs_usage')
        if not isinstance(usage, dict): raise ValueError('Muse returned no recognized quota windows.')
        windows = []
        window = usage.get('window')
        if isinstance(window, dict) and isinstance(window.get('used_percent'), (int, float)) and 0 <= window['used_percent'] <= 100:
            mins = window.get('window_duration_mins')
            label = f'Session ({mins // 60}-hour)' if isinstance(mins, int) and mins >= 60 and mins % 60 == 0 else 'Session'
            entry = {'label': label, 'percent': window['used_percent'] / 100}
            reset = window.get('resets_at')
            if reset: entry['resetsAt'] = dt.datetime.fromtimestamp(timestamp(reset), dt.timezone.utc).isoformat()
            windows.append(entry)
        weekly = usage.get('weekly')
        if isinstance(weekly, dict) and isinstance(weekly.get('used_percent'), (int, float)) and 0 <= weekly['used_percent'] <= 100:
            entry = {'label': 'Weekly (7-day)', 'percent': weekly['used_percent'] / 100}
            reset = weekly.get('resets_at')
            if reset: entry['resetsAt'] = dt.datetime.fromtimestamp(timestamp(reset), dt.timezone.utc).isoformat()
            windows.append(entry)
        if not windows: raise ValueError('Muse returned no recognized quota windows.')
        # Only display-safe fields enter the cache. Minted key material is
        # never persisted, and errors below never quote it either.
        cached = {'limits': windows, 'updatedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'error': '',
                  'plan': str(minted.get('subs_tier_name') or '')}
    except Exception as exc:
        cached['error'] = str(exc) if isinstance(exc, QuotaUnavailable) else 'Muse quota unavailable. Check your Muse login.'
    cached['attemptedAt'] = time.time()
    cached['authVersion'] = auth_version
    atomic_json(path, cached)
    return cached


def config_dir():
    """Settings and key files, resolved at call time so a test or an
    alternate XDG_CONFIG_HOME is honored."""
    return Path(os.getenv('XDG_CONFIG_HOME', HOME / '.config')) / 'omarchy/ai-usage'


def ollama_key(cfg=None):
    """Ollama Cloud API key, preferring a key the user supplied over one the
    machine happens to export. Nothing here writes or refreshes credentials."""
    cfg = settings() if cfg is None else cfg
    typed = str(cfg.get('ollamaApiKey') or '').strip()
    if typed: return typed
    from_env = str(os.getenv('OLLAMA_API_KEY') or '').strip()
    if from_env: return from_env
    for path in (config_dir() / 'ollama.key', Path(os.getenv('XDG_DATA_HOME', HOME / '.local/share')) / 'ollama/api_key'):
        try: value = path.read_text().strip()
        except OSError: continue
        if value: return value
    return ''


def quota_key_version(key, key_file, cached):
    """Detect credential changes without persisting a fast hash of the key.

    A random salt belongs to each provider's cache and is reused so unchanged
    credentials still hit the throttle. Legacy fingerprints refresh once.
    Parameters are fixed here, never taken from an editable cache file.
    """
    try:
        stat = key_file.stat()
        file_version = [stat.st_mtime_ns, stat.st_size]
    except OSError: file_version = None
    fingerprint = None
    if key:
        version = cached.get('keyVersion')
        previous = version.get('key') if isinstance(version, dict) else None
        salt = None
        if isinstance(previous, dict) and previous.get('scheme') == 'pbkdf2-sha256-v1':
            encoded = previous.get('salt')
            if isinstance(encoded, str) and len(encoded) == 32:
                try: salt = bytes.fromhex(encoded)
                except ValueError: pass
        if salt is None or len(salt) != 16: salt = os.urandom(16)
        fingerprint = {'scheme': 'pbkdf2-sha256-v1', 'salt': salt.hex(),
                       'digest': hashlib.pbkdf2_hmac('sha256', key.encode(), salt, 600_000, dklen=32).hex()}
    return {'file': file_version, 'key': fingerprint}


def ollama_quota(force=False):
    path = STATE / 'ollama-quota.json'
    key = ollama_key()
    key_file = config_dir() / 'ollama.key'
    # Throttle on the credential itself, not on one of the files that may hold
    # it: switching accounts in Settings or OLLAMA_API_KEY would otherwise show
    # the previous account's usage until the cache expired.
    try: cached = json.loads(path.read_text())
    except (OSError, ValueError): cached = {}
    key_version = quota_key_version(key, key_file, cached)
    if not force and cached.get('keyVersion') == key_version and time.time() - cached.get('attemptedAt', 0) < 300: return cached
    try:
        if not key: raise QuotaUnavailable('Add an Ollama Cloud API key in Settings to read its usage.')
        request = urllib.request.Request('https://ollama.com/api/usage',
            headers={'Authorization': 'Bearer ' + key, 'User-Agent': 'Omarchy-AI-Usage/0.1'})
        with urllib.request.urlopen(request, timeout=12) as response: data = json.load(response)
        if not isinstance(data, dict): raise ValueError('Ollama returned an unrecognized usage response.')
        limits = data.get('limits')
        if not isinstance(limits, dict): raise ValueError('Ollama returned no recognized usage windows.')
        windows = []
        # Whatever windows the plan has. Legacy plans report session and
        # weekly; credit plans report a monthly one. The endpoint carries no
        # reset time, so the meters show a share without a countdown.
        for name, label in OLLAMA_WINDOWS.items():
            window = limits.get(name)
            if not isinstance(window, dict) or not isinstance(window.get('usage'), (int, float)): continue
            # A plan can report above 100% (overage). Clamp for the meter
            # rather than discarding the window, which would read as a
            # credential failure once it is the only window left.
            windows.append({'label': label, 'percent': min(1.0, max(0.0, float(window['usage']))),
                            'raw': float(window['usage'])} if window['usage'] > 1
                           else {'label': label, 'percent': float(window['usage'])})
        if not windows: raise ValueError('Ollama returned no recognized usage windows.')
        plan = data.get('plan') if isinstance(data.get('plan'), str) else ''
        cached = {'limits': windows, 'updatedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'error': '', 'plan': plan}
    except Exception as exc:
        # Never quote a credential-bearing request object, URL, or body.
        cached['error'] = str(exc) if isinstance(exc, QuotaUnavailable) else 'Ollama Cloud usage unavailable. Check the API key.'
    cached['attemptedAt'] = time.time()
    cached['keyVersion'] = key_version
    atomic_json(path, cached)
    return cached


def commandcode_key(cfg=None):
    """CommandCode API key, preferring one the user supplied over one the
    machine happens to export. Nothing here writes or refreshes credentials."""
    cfg = settings() if cfg is None else cfg
    typed = str(cfg.get('commandcodeApiKey') or '').strip()
    if typed: return typed
    from_env = str(os.getenv('COMMANDCODE_API_KEY') or '').strip()
    if from_env: return from_env
    for path in (config_dir() / 'commandcode.key',):
        try: value = path.read_text().strip()
        except OSError: continue
        if value: return value
    return ''


def commandcode_call(key, path):
    """One read-only GET against CommandCode's own endpoints. The key rides in
    a header; nothing here writes, refreshes, or logs a credential."""
    request = urllib.request.Request('https://api.commandcode.ai' + path,
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
                 'User-Agent': 'Omarchy-AI-Usage/0.1'})
    with urllib.request.urlopen(request, timeout=12) as response:
        data = json.load(response)
    if not isinstance(data, dict): raise ValueError('CommandCode returned an unrecognized usage response.')
    return data


def commandcode_quota(force=False):
    """CommandCode measures its allowance in credit value, not tokens: GOAT
    allows $14 in any 5 hours, $35 in any 7 days, and $70 a month. The windows
    carry a reset time each, and the subscription carries the billing period
    end, so unlike the other providers this card can show countdowns.

    Every figure is read from the API rather than assumed: the monthly cap is
    the remaining credits plus what this period has spent, which the endpoints
    agree on exactly, so a plan change needs no code change.
    /alpha/billing/credits also reports the extra-credit wallet bought on top
    of the plan (its own purchasedCredits), spent only after the monthly
    credits run out and never expiring, so the snapshot carries it as a
    balance the card can show."""
    path = STATE / 'commandcode-quota.json'
    key = commandcode_key()
    key_file = config_dir() / 'commandcode.key'
    try: cached = json.loads(path.read_text())
    except (OSError, ValueError): cached = {}
    key_version = quota_key_version(key, key_file, cached)
    if not force and cached.get('keyVersion') == key_version and time.time() - cached.get('attemptedAt', 0) < 300: return cached
    try:
        if not key: raise QuotaUnavailable('Add a CommandCode API key in Settings to read its usage.')
        credits = commandcode_call(key, '/alpha/billing/credits')
        windows = credits.get('windowLimits') or {}
        limits = []
        # The endpoints express resets differently: the windows use epoch
        # milliseconds, the subscription an ISO string. Normalise to ISO, which
        # is what the panel's countdown reads.
        for name, label in (('fiveHour', '5 hours'), ('weekly', 'Weekly')):
            window = windows.get(name)
            if not isinstance(window, dict): continue
            used, cap = window.get('used'), window.get('cap')
            if not isinstance(used, (int, float)) or not isinstance(cap, (int, float)) or cap <= 0: continue
            reset = window.get('resetAt')
            limits.append({'label': label, 'percent': min(1.0, max(0.0, used / cap)),
                           'raw': used / cap,
                           'resetsAt': dt.datetime.fromtimestamp(timestamp(reset), dt.timezone.utc).isoformat() if timestamp(reset) else ''})
        wallet = credits.get('credits') or {}
        monthly = wallet.get('monthlyCredits')
        reset_at = ''
        plan = ''
        try:
            subscription = (commandcode_call(key, '/alpha/billing/subscriptions').get('data') or {})
            plan_id = subscription.get('planId')
            if isinstance(plan_id, str) and plan_id:
                # "individual-goat" reads as "GOAT" on the card.
                plan = plan_id.split('-')[-1].upper()
            reset_at = str(subscription.get('currentPeriodEnd') or '')
        except Exception: pass
        # One summary call answers both money questions below: what this
        # billing period has spent, from the same source the CLI shows, and how
        # much of the top-up wallet it drew down.
        try: summary = commandcode_call(key, '/alpha/usage/summary')
        except Exception: summary = {}
        if not isinstance(summary, dict): summary = {}
        spent = summary.get('totalCredits')
        if isinstance(monthly, (int, float)) and isinstance(spent, (int, float)):
            # Remaining + spent is the period's allowance.
            cap = monthly + spent
            if cap > 0:
                limits.append({'label': 'Monthly', 'percent': min(1.0, max(0.0, spent / cap)),
                               'raw': spent / cap, 'resetsAt': reset_at})
        # Extra credits are a wallet rather than a window: they never expire,
        # and they are spent only once the plan's own credits are gone. The
        # endpoint states what is left; what it was funded with is that plus
        # what this period has drawn from it, so the derived figure says so.
        # The card shows money, so a residue below the cent it would print
        # stays off the card rather than reading as a $0.00 meter, and a wallet
        # that outlives the plan's windows still carries the card on its own.
        remaining = wallet.get('purchasedCredits')
        drawn = summary.get('totalPurchasedCredits')
        balance = None
        if isinstance(remaining, (int, float)) and round(remaining, 2) > 0:
            balance = {'label': 'Extra credits', 'remaining': remaining, 'currency': 'USD', 'estimated': True}
            if isinstance(drawn, (int, float)) and drawn >= 0:
                balance['spent'] = drawn
                balance['funded'] = remaining + drawn
        if not limits and not isinstance(remaining, (int, float)):
            raise ValueError('CommandCode returned no recognized usage windows.')
        cached = {'limits': limits, 'updatedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'error': '', 'plan': plan}
        if balance: cached['balance'] = balance
    except Exception as exc:
        # Never quote a credential-bearing request object, URL, or body.
        cached['error'] = str(exc) if isinstance(exc, QuotaUnavailable) else 'CommandCode usage unavailable. Check the API key.'
    cached['attemptedAt'] = time.time()
    cached['keyVersion'] = key_version
    atomic_json(path, cached)
    return cached


def clinepass_key(cfg=None):
    """ClinePass API key, preferring one the user supplied over one the machine
    happens to export. Nothing here writes or refreshes credentials."""
    cfg = settings() if cfg is None else cfg
    typed = str(cfg.get('clinepassApiKey') or '').strip()
    if typed: return typed
    from_env = str(os.getenv('CLINE_API_KEY') or '').strip()
    if from_env: return from_env
    for path in (config_dir() / 'clinepass.key',):
        try: value = path.read_text().strip()
        except OSError: continue
        if value: return value
    return ''


def clinepass_call(key, path):
    """One read-only GET against Cline's own endpoints. The key rides in a
    header; nothing here writes, refreshes, or logs a credential."""
    request = urllib.request.Request('https://api.cline.bot' + path,
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
                 'User-Agent': 'Omarchy-AI-Usage/0.1'})
    with urllib.request.urlopen(request, timeout=12) as response:
        data = json.load(response)
    if not isinstance(data, dict): raise ValueError('ClinePass returned an unrecognized usage response.')
    if data.get('success') is False: raise ValueError('ClinePass refused the usage request.')
    # This provider wraps its payload, and it wraps its errors the other way:
    # {"data": {"limits": [...]}, "success": true} against {"error": ..., "success": false}.
    payload = data.get('data')
    return payload if isinstance(payload, dict) else data


# ClinePass names its windows in snake_case and gives each its own reset time.
CLINEPASS_WINDOWS = {'five_hour': '5 hours', 'weekly': 'Weekly', 'monthly': 'Monthly'}


def clinepass_window(row):
    """One window, normalised. The endpoint reports a percentage, and every
    other provider's cache holds a 0-1 fraction, so divide it here once rather
    than leaving each consumer to guess which scale it is reading."""
    if not isinstance(row, dict): return None
    used = row.get('percentUsed')
    if isinstance(used, bool) or not isinstance(used, (int, float)): return None
    kind = str(row.get('type') or '').strip()
    # A window this table has not seen still gets a label, so a plan tier that
    # reports a new one shows up rather than silently losing a meter.
    label = CLINEPASS_WINDOWS.get(kind) or kind.replace('_', ' ').strip().capitalize()
    if not label: return None
    fraction = max(0.0, float(used) / 100.0)
    reset = str(row.get('resetsAt') or '').strip()
    try: reset = dt.datetime.fromisoformat(reset.replace('Z', '+00:00')).isoformat() if reset else ''
    except ValueError: reset = ''
    return {'label': label, 'percent': min(1.0, fraction), 'raw': fraction, 'resetsAt': reset}


def clinepass_quota(force=False):
    """ClinePass measures usage as a share of three plan windows: a rolling five
    hours, the calendar week, and the calendar month. The endpoint publishes a
    percentage per window with its own reset time, so these meters carry a
    countdown, and the plan name comes from a second call.

    The key is a Cline API key from the Settings field, CLINE_API_KEY, or a
    clinepass.key file; the endpoints are the ones Cline's own dashboard uses
    and are not documented for third parties, so a failure leaves the previous
    snapshot in place with an error rather than emptying the card."""
    path = STATE / 'clinepass-quota.json'
    key = clinepass_key()
    key_file = config_dir() / 'clinepass.key'
    try: cached = json.loads(path.read_text())
    except (OSError, ValueError): cached = {}
    key_version = quota_key_version(key, key_file, cached)
    if not force and cached.get('keyVersion') == key_version and time.time() - cached.get('attemptedAt', 0) < 300: return cached
    try:
        if not key: raise QuotaUnavailable('Add a ClinePass API key in Settings to read its usage.')
        reported = clinepass_call(key, '/api/v1/users/me/plan/usage-limits').get('limits')
        limits = [window for window in (clinepass_window(row) for row in (reported if isinstance(reported, list) else [])) if window]
        if not limits: raise ValueError('ClinePass returned no recognized usage windows.')
        plan = ''
        try:
            plan = str((clinepass_call(key, '/api/v1/users/me/plan').get('plan') or {}).get('displayName') or '')
        except Exception: pass
        cached = {'limits': limits, 'updatedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'error': '', 'plan': plan}
    except Exception as exc:
        # Never quote a credential-bearing request object, URL, or body.
        cached['error'] = str(exc) if isinstance(exc, QuotaUnavailable) else 'ClinePass usage unavailable. Check the API key.'
    cached['attemptedAt'] = time.time()
    cached['keyVersion'] = key_version
    atomic_json(path, cached)
    return cached


def cursor_quota(force=False):
    path = STATE / 'cursor-quota.json'
    try: cached = json.loads(path.read_text())
    except (OSError, ValueError): cached = {}
    if not force and time.time() - cached.get('attemptedAt', 0) < 300: return cached
    try:
        token = cursor_token()
        if token is None: raise CursorApiUnavailable('Sign in to Cursor desktop to read Cursor quota.')
        summary = cursor_summary(token)
        plan = summary.get('planUsage')
        if not isinstance(plan, dict): raise CursorApiUnavailable('Cursor returned an unrecognized quota response.')
        percent = plan.get('totalPercentUsed')
        if not isinstance(percent, (int, float)): raise CursorApiUnavailable('Cursor returned an unrecognized quota response.')
        end = event_ms(summary.get('billingCycleEnd'))
        resets = dt.datetime.fromtimestamp(end // 1000, dt.timezone.utc).isoformat() if end > 0 else ''
        cached = {'limits': [{'label': 'Billing cycle', 'percent': percent / 100, 'resetsAt': resets}],
                  'updatedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'error': str(summary.get('displayMessage') or '')}
    except Exception as exc:
        cached['error'] = str(exc) if isinstance(exc, CursorApiUnavailable) else 'Cursor quota unavailable. Check your Cursor sign-in.'
    cached['attemptedAt'] = time.time()
    atomic_json(path, cached)
    return cached


def source_account(cfg):
    """Which account a recorded source path belongs to. The deepest configured
    folder wins; a synced machine's copy is its own account; anything else is
    the local login."""
    cfg = history_config(cfg)
    roots = []
    for account in cfg.get('accounts', []):
        for directory in account['directories']:
            roots.append((directory['provider'], Path(directory['path']).expanduser().resolve(), account['id']))
    roots.sort(key=lambda item: len(item[1].parts), reverse=True)
    cache = {}
    def resolve(provider, path):
        key = (provider, path)
        if key not in cache:
            source = Path(path)
            match = next((aid for p, root, aid in roots if p == provider and source.is_relative_to(root)), None)
            if match is None and str(path).startswith('machine:'): match = str(path).split('/', 1)[0]
            cache[key] = match or 'local'
        return cache[key]
    return resolve


def event_account(ids):
    """One account for an event seen under several sources. A named account
    beats the local login; copies under two named accounts need review."""
    named = set(ids) - {'local'}
    return 'conflict' if len(named) > 1 else next(iter(named)) if named else 'local'


def account_assignments(ledger, cfg):
    cfg = history_config(cfg)
    labels = {'local': cfg.get('localAccountLabel', 'Local'), 'unassigned': 'Unassigned history', 'conflict': 'Needs review'}
    for account in cfg.get('accounts', []): labels[account['id']] = account['label']
    resolve, assignments = source_account(cfg), {}
    for event, provider, path in ledger.db.execute('SELECT e.id,e.provider,s.path FROM events e JOIN event_sources s ON s.event_id=e.id'):
        aid = resolve(provider, path)
        if aid.startswith('machine:'): labels[aid] = aid.split(':', 1)[1]
        assignments.setdefault(event, set()).add(aid)
    return labels, {event: event_account(ids) for event, ids in assignments.items()}


def selected(r, selection, provider):
    """Whether a record survives the report's filters. One copy, because the
    table and the per-card model rows have to agree on what a selection means:
    two hand-maintained copies of this comparison is how a filtered view ends up
    contradicting the table it was opened from. A model selection matches the
    whole family, since the row a reader clicked can stand for several routes.
    """
    for key in ('model', 'project', 'client', 'apiProvider'):
        if not selection.get(key): continue
        if key == 'model':
            if model_family(r['model']) != model_family(selection['model']): return False
            continue
        value = r[key] or ('Unknown project' if key == 'project' else provider if key == 'apiProvider' else '')
        if value != selection[key]: return False
    return True


def timed_event(r):
    # Older ledgers and synced snapshots predate timePrecision. Hermes rows
    # already carry their source name in client, so they remain identifiable.
    precision = r.get('timePrecision')
    return precision == 'event' or (precision is None and not str(r.get('client') or '').startswith('Hermes'))


def window_start(label, reset):
    """When a limit window began, from its label and the time it resets.

    None when the label does not say how long the window runs: a count over a
    guessed span would look as exact as a real one. A monthly window steps
    back one calendar month, since billing months are not all 30 days.
    """
    text = str(label or '').lower()
    if 'month' in text:
        year, month = (reset.year, reset.month - 1) if reset.month > 1 else (reset.year - 1, 12)
        return reset.replace(year=year, month=month, day=min(reset.day, calendar.monthrange(year, month)[1]))
    if 'week' in text: return reset - dt.timedelta(days=7)
    match = re.search(r'(\d+)\s*-?\s*(d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?)\b', text)
    if not match: return None
    unit = {'d': 'days', 'h': 'hours', 'm': 'minutes'}[match.group(2)[0]]
    return reset - dt.timedelta(**{unit: int(match.group(1))})


def current_window(limit, now):
    """A countable account window; model-only limits need their own scope."""
    if not isinstance(limit, dict) or limit.get('title') or not limit.get('resetsAt'): return None
    try: reset = dt.datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', str(limit['resetsAt'])).replace('Z', '+00:00'))
    except ValueError: return None
    if reset.tzinfo is None: reset = reset.replace(tzinfo=dt.timezone.utc)
    start = window_start(limit.get('label'), reset)
    return start if start is not None and start <= now < reset else None


def account_views(ledger, cfg, labels, assignments, now):
    """Every selectable source/account pair, independent of period and filters.
    This catalog also owns quota association for cards and reset-period reports.
    """
    by_id, by_name = account_quotas()
    named = {a['id']: a for a in cfg.get('accounts', [])}
    pairs = {(p, 'local') for p in cfg['enabled']}
    pairs |= {(d['provider'], a['id']) for a in named.values() for d in a['directories']}
    pairs |= {(p, a['id']) for a in named.values() for p in a.get('providers', [])}
    for event, p in ledger.db.execute('SELECT id,provider FROM events'):
        pairs.add((p, assignments.get(event, 'unassigned')))
    views = []
    for p, aid in sorted(pairs, key=lambda pair: (list(PROVIDERS).index(pair[0]) if pair[0] in PROVIDERS else 99,
                                                pair[1] != 'local', labels.get(pair[1], pair[1]).casefold())):
        if p not in cfg['enabled']: continue
        label = labels.get(aid, aid)
        if aid == 'local':
            q, scope, record_id = quota(p), 'Current login on this PC', p
            name = ('ChatGPT' if p == 'codex' else PROVIDERS[p]) + ' · ' + label
        else:
            a = named.get(aid, {})
            record_id = p + ':' + aid if p + ':' + aid in by_id else a.get('recordId') or aid
            # Provider-qualified records take precedence; special histories
            # and names that collide with default provider ids never borrow a login.
            q = None if aid in PROVIDERS or aid in ('unassigned', 'conflict') or aid.startswith('machine:') else (
                by_id.get(p + ':' + aid) or by_id.get(record_id) or by_name.get(label.casefold()))
            if q is not None and q.get('provider') not in (None, p): q = None
            scope = 'From its own usage record' if q is not None else ''
            q = q or {'limits': [], 'error': 'No quota record linked to this account; current login quota belongs to its own account.'}
            name = label if p == 'codex' and label.casefold().startswith('chatgpt') else PROVIDERS[p] + ' · ' + label
        views.append({'id': p + ':' + aid, 'provider': p, 'accountId': aid, 'name': name,
                      'recordId': record_id, 'quota': q, 'quotaScope': scope,
                      'historyAvailable': named.get(aid, {}).get('historyAvailable', True)})
    counts = limit_window_tokens(ledger, cfg, {v['id']: ({v['provider']}, v['accountId'], v['quota'].get('limits'))
                                              for v in views if v['historyAvailable']}, now)
    for view in views:
        windows = counts.get(view['id'], {})
        view['quota'] = view['quota'] | {'limits': [limit | ({'tokens': windows[i][1], 'startAt': windows[i][0],
                                                             'windowId': digest(limit['label'], limit.get('title', ''))}
                                                           if i in windows else {})
                                                  for i, limit in enumerate(view['quota'].get('limits', []))]}
    return views


def limit_window_tokens(ledger, cfg, targets, now=None):
    """Local tokens used in each limit's current window.

    targets maps a key to (providers, account, limits), where account is the
    ledger account whose history belongs to that login. The answer maps the
    same key to {index in limits: (window start, tokens)}. A window is left out
    when it names no reset, has already reset, is scoped to one model (it
    carries a title), or its length cannot be read from the label.
    """
    now = now or dt.datetime.now().astimezone()
    windows, found = {}, {}
    for key, (providers, account, limits) in targets.items():
        for index, limit in enumerate(limits or []):
            start = current_window(limit, now)
            if start is None: continue
            since = int(start.timestamp())
            found.setdefault(key, {})[index] = [since, 0]
            for provider in providers:
                windows.setdefault((provider, account), []).append((since, found[key][index]))
    if not windows: return {}
    # Summed in SQL by each event's set of source paths, so accounts are
    # resolved once per set rather than once per event: the panel's pulse
    # runs this every 15 seconds. An event copied under several sources is
    # placed the way account_assignments places it.
    starts = sorted({since for entries in windows.values() for since, _ in entries})
    names = sorted({provider for provider, _ in windows})
    sums = ','.join('SUM(CASE WHEN ts>=? THEN tokens ELSE 0 END)' for _ in starts)
    resolve = source_account(cfg)
    for provider, paths, *values in ledger.db.execute(
            f'SELECT provider,paths,{sums} FROM (SELECT e.provider,e.ts,'
            f'e.input+e.output+e.cacheRead+e.cacheWrite AS tokens,group_concat(s.path,char(31)) AS paths '
            f'FROM events e JOIN event_sources s ON s.event_id=e.id WHERE e.ts>=? AND e.ts<=? '
            f'AND e.provider IN ({",".join("?" for _ in names)}) GROUP BY e.id) GROUP BY provider,paths',
            (*starts, starts[0], now.timestamp(), *names)):
        account = event_account(resolve(provider, path) for path in paths.split('\x1f'))
        for since, total in windows.get((provider, account), ()):
            total[1] += values[starts.index(since)] or 0
    return {key: {index: tuple(value) for index, value in entries.items()} for key, entries in found.items()}


def record_targets(cfg):
    """The ledger history behind each agent record. A provider's own record is
    the current login, whose history is the local account; any other record
    belongs to the labelled account with its id or its name, the same match the
    dashboard cards use to find an account's limits. Maps a record id to
    (providers, account, limits); limits is empty for a record that lists none.
    """
    cfg = history_config(cfg)
    targets = {}
    try: paths = sorted((STATE.parent / 'agents/usage').glob('*.json'))
    except OSError: paths = []
    for path in paths:
        try: d = json.loads(path.read_text())
        except (OSError, ValueError): continue
        if not isinstance(d, dict): continue
        limits = d['limits'] if isinstance(d.get('limits'), list) else []
        if path.stem in PROVIDERS:
            targets[path.stem] = ({path.stem}, 'local', limits)
            continue
        name = str(d.get('name') or '').casefold()
        account = next((a for a in cfg.get('accounts', [])
                        if a.get('recordId', a['id']) == path.stem or path.stem == a['id']
                        or path.stem == 'codex:' + a['id'] or (name and str(a['label']).casefold() == name)), None)
        if account:
            targets[path.stem] = ({x['provider'] for x in account['directories']}, account['id'], limits)
    return targets


def record_limit_targets(cfg):
    """record_targets for the records that carry limits."""
    return {key: target for key, target in record_targets(cfg).items() if target[2]}


def source_history(ledger, cfg, targets, providers, active=()):
    """Which agent records have indexed history at any time. A labelled
    account's history is told apart by its source paths, which is one cheap
    pass over the distinct paths rather than over every event, and only for an
    account with nothing today (active). A provider's own record is available
    whenever the provider has any history, and a provider with no record of its
    own keeps its plain id.
    """
    named = {key: (provs, account) for key, (provs, account, _) in targets.items() if account != 'local'}
    found = {key for key, (provs, _, _) in targets.items() if key not in named and provs & set(providers)} | set(active)
    named = {key: target for key, target in named.items() if key not in found}
    if named:
        resolve = source_account(cfg)
        paths = [row[0] for row in ledger.db.execute('SELECT DISTINCT path FROM event_sources')]
        found |= {key for key, (provs, account) in named.items()
                  if any(resolve(provider, path) == account for provider in provs for path in paths)}
    return sorted(found | {p for p in providers if p not in targets})


def hourly_snapshot(ledger, now=None, cfg=None):
    """A small, local panel feed. Quota records have a different writer and
    cannot reliably carry hourly history, so the panel reads this file.

    Totals are kept per provider and per agent record. A labelled second login
    is its own record in the panel but shares its provider's table with the
    main login, so the record totals split each provider by account.
    """
    cfg = cfg or DEFAULTS
    now = now or dt.datetime.now().astimezone()
    day = now.date()
    start = int(dt.datetime.combine(day, dt.time()).timestamp())
    end = int(now.timestamp())
    hours = [{'start': ts, 'label': dt.datetime.fromtimestamp(ts).strftime('%H:%M'),
              'zone': dt.datetime.fromtimestamp(ts).astimezone().strftime('%Z'),
              'tokens': 0, 'providers': {}, 'sources': {}}
             for ts in range(start, end + 1, 3600)]
    providers = {}
    available_providers = [row[0] for row in ledger.db.execute('SELECT DISTINCT provider FROM events')]
    targets = record_targets(cfg)
    # Every record has an entry, so an idle login reads as zero rather than
    # as a source the snapshot knows nothing about.
    sources = {key: {'tokens': 0, 'timedTokens': 0, 'unplacedTokens': 0} for key in targets}
    records = {}
    for key, (provs, account, _) in targets.items():
        for provider in provs: records.setdefault((provider, account), []).append(key)
    resolve, accounts = source_account(cfg), {}
    total = timed = unplaced = 0
    ledger.db.row_factory = sqlite3.Row
    for row in ledger.db.execute('SELECT e.provider,e.client,e.timePrecision,e.ts,e.input,e.output,e.cacheRead,e.cacheWrite,'
                                 'group_concat(s.path,char(31)) AS paths '
                                 'FROM events e INDEXED BY events_time LEFT JOIN event_sources s ON s.event_id=e.id '
                                 'WHERE e.ts>=? AND e.ts<=? GROUP BY e.id', (start, end)):
        r = dict(row)
        tokens = sum(number(r[f]) for f in FIELDS[:4])
        # The same account an event gets everywhere else; one without a
        # recorded source is unassigned history. The time index is named
        # because grouping by id otherwise walks every event in the ledger.
        origin = (r['provider'], r['paths'])
        if origin not in accounts:
            accounts[origin] = event_account(resolve(r['provider'], path) for path in r['paths'].split('\x1f')) if r['paths'] else 'unassigned'
        owners = records.get((r['provider'], accounts[origin]), ())
        p = providers.setdefault(r['provider'], {'tokens': 0, 'timedTokens': 0, 'unplacedTokens': 0})
        mine = [sources[o] for o in owners]
        total += tokens
        p['tokens'] += tokens
        for m in mine: m['tokens'] += tokens
        if not timed_event(r):
            unplaced += tokens
            p['unplacedTokens'] += tokens
            for m in mine: m['unplacedTokens'] += tokens
            continue
        index = int((r['ts'] - start) // 3600)
        if 0 <= index < len(hours):
            timed += tokens
            p['timedTokens'] += tokens
            for m in mine: m['timedTokens'] += tokens
            hours[index]['tokens'] += tokens
            by_provider = hours[index]['providers']
            by_provider[r['provider']] = by_provider.get(r['provider'], 0) + tokens
            by_source = hours[index]['sources']
            for o in owners: by_source[o] = by_source.get(o, 0) + tokens
    return {'schemaVersion': 1, 'date': str(day), 'generatedAt': now.timestamp(),
            'timeZone': now.tzname() or '', 'utcOffsetMinutes': int(now.utcoffset().total_seconds() // 60),
            'tokens': total, 'timedTokens': timed, 'unplacedTokens': unplaced,
            'availableProviders': available_providers, 'providers': providers, 'hours': hours,
            # The same totals for each agent record, so a second login shows
            # its own usage instead of its provider's, and the records with
            # indexed history at any time.
            'sources': sources,
            'availableSources': source_history(ledger, cfg, targets, available_providers,
                                               [key for key, entry in sources.items() if entry['tokens']]),
            # Local tokens in each record's current limit windows, keyed by
            # record id. The panel matches them to its limits by label and
            # reset time, so a window that has since rolled over shows none.
            'limitTokens': limit_tokens_by_record(ledger, cfg, now),
            'update': update_status(cfg)}


def limit_tokens_by_record(ledger, cfg, now=None):
    targets = record_limit_targets(cfg)
    counts = limit_window_tokens(ledger, cfg, targets, now)
    return {key: [{'label': str(targets[key][2][index].get('label') or ''),
                   'resetsAt': str(targets[key][2][index].get('resetsAt')),
                   'since': since, 'tokens': tokens}
                  for index, (since, tokens) in sorted(entries.items())]
            for key, entries in counts.items()}


def report(ledger, cfg, days=7, provider='all', now=None, selection=None):
    selection = selection or {}
    saved_cfg, cfg = cfg, history_config(cfg)
    # Sources left out of this view. They are dropped before anything is
    # accumulated, so the summary, the cards, every breakdown, and the model
    # filter's own list all describe the same set of sources.
    excluded_sources = {str(name) for name in (selection.get('excludeSource') or []) if name}
    today = now or dt.datetime.now().astimezone()
    labels, assignments = account_assignments(ledger, cfg)
    views = account_views(ledger, cfg, labels, assignments, today)
    views_by_id = {v['id']: v for v in views}
    reset_limit, period_error = None, ''
    if selection.get('resetWindow'):
        view = views_by_id.get(provider + ':' + str(selection.get('account', '')))
        reset_limit = next((w for w in view['quota']['limits'] if w.get('windowId') == selection['resetWindow']), None) if view else None
        if reset_limit is None:
            period_error = 'This reset window is unavailable. Choose an account and a current reset window, or select a date range.'
    start_date = today.date() - dt.timedelta(days=days - 1)
    if reset_limit:
        start_date = dt.datetime.fromtimestamp(reset_limit['startAt']).date()
        days = (today.date() - start_date).days + 1
    selected_hour = int(selection['hourStart']) if selection.get('hourStart') else None
    selected_date = dt.date.fromisoformat(selection['day']) if selection.get('day') else (dt.datetime.fromtimestamp(selected_hour).date() if selected_hour is not None else None)
    # Local calendar boundaries, including DST transitions.
    start = reset_limit['startAt'] if reset_limit else dt.datetime.combine(start_date, dt.time()).timestamp()
    previous_date = (selected_date - dt.timedelta(days=1)) if selected_date else None
    previous_start = dt.datetime.combine(previous_date if previous_date else start_date - dt.timedelta(days=days), dt.time()).timestamp()
    end = today.timestamp()
    if period_error: start = end + 1
    previous_end = (dt.datetime.combine(previous_date, today.time().replace(tzinfo=None)).timestamp()
                    if selected_date == today.date() else
                    dt.datetime.combine(selected_date, dt.time()).timestamp() if selected_date else
                    dt.datetime.combine(today.date() - dt.timedelta(days=1), today.time().replace(tzinfo=None)).timestamp()
                    if days == 1 else start)
    summary, previous = bucket(), bucket()
    hourly_unplaced = bucket()
    unplaced_providers = {}
    providers = {p: bucket() for p in cfg['enabled'] if p not in excluded_sources and provider in ('all', p)}
    daily = {str(start_date + dt.timedelta(days=n)): {'total': bucket(), 'providers': {p: bucket() for p in providers}, 'cards': {}} for n in range(days)}
    hour_start, hour_end = dt.datetime.combine(start_date, dt.time()).timestamp(), end
    if selected_date:
        hour_start = dt.datetime.combine(selected_date, dt.time()).timestamp()
        hour_end = min(end, dt.datetime.combine(selected_date + dt.timedelta(days=1), dt.time()).timestamp() - 1)
    if selected_hour is not None:
        hour_start = selected_hour
        hour_end = min(end, selected_hour + 3599)
    hourly = [{'start': ts, 'label': dt.datetime.fromtimestamp(ts).strftime('%H:%M'),
               'title': dt.datetime.fromtimestamp(ts).astimezone().strftime('%H:%M %Z') + ' to ' +
                        (dt.datetime.fromtimestamp(ts + 3600).astimezone().strftime('%H:%M %Z') if ts + 3600 <= hour_end + 1 else 'now'),
               'total': bucket(), 'providers': {p: bucket() for p in providers}, 'cards': {}}
              for ts in range(int(hour_start), int(hour_end) + 1, 3600)] if hour_end >= hour_start and (days == 1 or selected_date or selected_hour is not None) else []
    models, projects, clients, sessions, routes, accounts = {}, {}, {}, {}, {}, {}
    card_models_agg = {}
    model_routes = {}
    unpriced = {}
    # The model filter's own options: every model this scope recorded in the
    # period. Grouped like the Models table, and deliberately NOT narrowed by the
    # model selection, or the list would vanish to the one entry already chosen
    # and a reader could not switch models without clearing the filter first.
    model_options = {}
    provider_accounts = {p: set() for p in providers}
    heatmap = collections.Counter()
    heatmap_fast = provider == 'all' and not excluded_sources and not any(
        selection.get(key) for key in ('model', 'project', 'client', 'apiProvider', 'day', 'hourStart', 'account', 'resetWindow'))
    unknown = set()
    rates = load_rates()
    ledger.db.row_factory = sqlite3.Row
    earliest = ledger.db.execute('SELECT MIN(ts) FROM events').fetchone()[0]
    if heatmap_fast and providers:
        names = tuple(providers)
        placeholders = ','.join('?' for _ in names)
        for day, tokens in ledger.db.execute(
                f"SELECT date(ts,'unixepoch','localtime'),SUM(input+output+cacheRead+cacheWrite) "
                f"FROM events WHERE ts>=? AND ts<=? AND provider IN ({placeholders}) GROUP BY 1",
                (end - 365 * 86400, end, *names)):
            heatmap[day] = tokens or 0
    scan_start = previous_start if heatmap_fast or selected_date else min(previous_start, end - 365 * 86400)
    for row in ledger.db.execute('SELECT * FROM events WHERE ts>=? AND ts<=?', (scan_start, end)):
        r = dict(row); p = r['provider']
        account = assignments.get(r['id'], 'unassigned')
        if selection.get('account') and account != selection['account']: continue
        if p not in providers or (provider != 'all' and p != provider): continue
        if p in excluded_sources: continue
        day = str(dt.datetime.fromtimestamp(r['ts']).date())
        # Recorded before the selection is applied, so the model options describe
        # what this scope holds rather than what is currently being looked at.
        # The period, a chosen day, the route tab, and the account filter all
        # still apply, since those decide which history is in view at all.
        if start <= r['ts'] <= end and (not selected_date or day == str(selected_date)) and (selected_hour is None or selected_hour <= r['ts'] < selected_hour + 3600):
            family_name = model_family(r['model'])
            model_options[family_name] = model_options.get(family_name, 0) + sum(r[f] for f in FIELDS[:4])
        if not selected(r, selection, p): continue
        if selected_hour is None and previous_start <= r['ts'] < previous_end and (not previous_date or day == str(previous_date)):
            value, savings, tier = price_detail(r, rates['document'])
            add(previous, r, value, savings, tier)
        if selected_date and day != str(selected_date): continue
        if selected_hour is not None and (not timed_event(r) or not selected_hour <= r['ts'] < selected_hour + 3600): continue
        if not heatmap_fast: heatmap[day] += sum(r[f] for f in FIELDS[:4])
        value, savings, tier = price_detail(r, rates['document'])
        if r['ts'] < start: continue
        add(summary, r, value, savings, tier); add(providers[p], r, value, savings, tier)
        provider_accounts[p].add(account)
        card_id = p + ':' + account
        model_entry = card_models_agg.setdefault(card_id, {}).setdefault(r['model'],
            {'model': r['model'], 'tokens': 0, 'value': 0.0, 'unpriced': 0})
        model_tokens = sum(r[f] for f in FIELDS[:4])
        model_entry['tokens'] += model_tokens
        if value is None: model_entry['unpriced'] += model_tokens
        else: model_entry['value'] += value
        if day in daily:
            add(daily[day]['total'], r, value, savings, tier)
            add(daily[day]['providers'][p], r, value, savings, tier)
            card_bucket = daily[day]['cards'].get(card_id)
            if card_bucket is None:
                card_bucket = daily[day]['cards'][card_id] = bucket()
            add(card_bucket, r, value, savings, tier)
        if hourly:
            index = int((r['ts'] - hour_start) // 3600)
            if not timed_event(r):
                add(hourly_unplaced, r, value, savings, tier)
                unplaced_bucket = unplaced_providers.get(p)
                if unplaced_bucket is None:
                    unplaced_bucket = unplaced_providers[p] = bucket()
                add(unplaced_bucket, r, value, savings, tier)
            elif 0 <= index < len(hourly):
                add(hourly[index]['total'], r, value, savings, tier)
                add(hourly[index]['providers'][p], r, value, savings, tier)
                card_bucket = hourly[index]['cards'].get(card_id)
                if card_bucket is None:
                    card_bucket = hourly[index]['cards'][card_id] = bucket()
                add(card_bucket, r, value, savings, tier)
        if value is None:
            # Pricing coverage stays keyed by route and by the exact recorded
            # spelling, because that pair is what names the rate table that has
            # not caught up. Grouping it under one model name would blame the
            # wrong route for an unpriced token.
            unknown.add(r['model'])
            missing_key = (p, r['model'])
            missing_bucket = unpriced.get(missing_key)
            if missing_bucket is None:
                missing_bucket = unpriced[missing_key] = bucket()
            add(missing_bucket, r, value, savings, tier)
        # The Models breakdown is keyed by family rather than by provider, so one
        # model is one row across routes, and it keeps its own per-route detail.
        # Every route's value is still priced by that route's own rate table in
        # price() above, so grouping sums real figures instead of re-pricing one
        # route's tokens at another route's rates.
        family = model_family(r['model'])
        model_bucket = models.get(family)
        if model_bucket is None:
            model_bucket = models[family] = bucket()
        add(model_bucket, r, value, savings, tier)
        family_routes = model_routes.setdefault(family, {})
        route = family_routes.get(p)
        if route is None:
            route = family_routes[p] = {'models': set(), 'bucket': bucket()}
        route['models'].add(r['model'])
        add(route['bucket'], r, value, savings, tier)
        # Every other breakdown stays keyed by provider, as it always was.
        for group, key in [(projects, (p, r['project'] or 'Unknown project')),
                           (clients, (p, r['client'])), (sessions, (p, r['session'])),
                           (routes, (p, r.get('apiProvider') or p)), (accounts, (p, account))]:
            b = group.get(key)
            if b is None:
                b = group[key] = bucket()
            add(b, r, value, savings, tier)
            if group is sessions:
                b['project'] = r['project'] or 'Unknown project'
                b['client'] = r['client']
                b['firstAt'] = min(b.get('firstAt', r['ts']), r['ts'])
                b['lastAt'] = max(b.get('lastAt', r['ts']), r['ts'])
    def rows(group):
        return sorted([finish(v) | {'provider': p, 'name': name} for (p, name), v in group.items()], key=lambda x: x['tokens'], reverse=True)
    try: coverage = json.loads(ledger.db.execute("SELECT value FROM metadata WHERE key='scan'").fetchone()[0])
    except (TypeError, ValueError): coverage = {}
    inventory = [dict(r) for r in ledger.db.execute(
        'SELECT provider,client,COUNT(DISTINCT session) AS sessions,MIN(ts) AS firstAt,MAX(ts) AS lastAt FROM events GROUP BY provider,client')]
    coverage['clients'] = inventory
    has_unassigned = ledger.db.execute('SELECT 1 FROM events e WHERE NOT EXISTS (SELECT 1 FROM event_sources s WHERE s.event_id=e.id) LIMIT 1').fetchone() is not None
    account_options = [{'id': aid, 'label': label} for aid, label in labels.items()
                       if aid not in ('conflict', 'unassigned') or aid in assignments.values() or (aid == 'unassigned' and has_unassigned)]
    coverage['additionalHomes'] = sum(len(cfg.get(k, [])) for k in HOME_KEYS) + sum(len(a['directories']) for a in cfg.get('accounts', []))
    # One overview card per account so labelled histories are never merged.
    # A named account uses only its own price; the local group uses the
    # provider price. Quota association comes from the same account catalog.
    named_providers = {d['provider'] for account in cfg.get('accounts', []) for d in account['directories']}
    cards = []
    for p, b in providers.items():
        if provider != 'all' and p != provider: continue
        group = sorted(((aid, bucket) for (card_provider, aid), bucket in accounts.items() if card_provider == p),
                       key=lambda item: item[1]['tokens'], reverse=True)
        if selection.get('account') and not group and p + ':' + selection['account'] in views_by_id:
            group = [(selection['account'], bucket())]
        for shade, (aid, account_bucket) in enumerate(group):
            fin = finish(account_bucket)
            view = views_by_id[p + ':' + aid]
            if aid == 'local':
                name = PROVIDERS[p] + (' · ' + labels['local'] if p in named_providers else '')
                monthly = cfg['monthlyPrices'].get(p)
            else:
                name = PROVIDERS[p] + ' · ' + labels[aid]
                monthly = cfg['monthlyPrices'].get(aid)
            cards.append(fin | {'id': p + ':' + aid, 'provider': p, 'accountId': aid, 'name': name, 'monthlyPrice': monthly,
                                'shade': shade, 'shades': len(group),
                                'quota': view['quota'], 'quotaScope': view['quotaScope'],
                                'valueShare': 100 * fin['value'] / summary['value'] if summary['value'] else None})
    # Heaviest users of the period first. Shades were already assigned within
    # each provider, so reordering keeps every account's colour.
    cards.sort(key=lambda card: card['tokens'], reverse=True)
    # Model-level allowance for Go. The quota endpoint reports only aggregate
    # windows, so value against each model's documented monthly limit is
    # estimated from local history during the current monthly reset window.
    go_allowance = {'since': None, 'models': []}
    if 'opencode-go' in providers and provider in ('all', 'opencode-go') and 'opencode-go' not in excluded_sources:
        monthly = next((limit for limit in quota('opencode-go').get('limits', []) if limit.get('label') == 'Monthly' and limit.get('resetsAt')), None)
        go_allowance['since'] = int(timestamp(monthly['resetsAt']) - 30 * 86400) if monthly else int(dt.datetime.combine(today.date().replace(day=1), dt.time()).timestamp())
        used = {}
        for row in ledger.db.execute('SELECT * FROM events WHERE provider=? AND ts>=? AND ts<=?', ('opencode-go', go_allowance['since'], end)):
            r = dict(row)
            # The allowance rows follow the model filter too, so the card cannot
            # list models the rest of the page is excluding.
            if selection.get('account') and assignments.get(r['id'], 'unassigned') != selection['account']: continue
            if not selected(r, selection, 'opencode-go'): continue
            if selection.get('day') and str(dt.datetime.fromtimestamp(r['ts']).date()) != selection['day']: continue
            if selected_hour is not None and (not timed_event(r) or not selected_hour <= r['ts'] < selected_hour + 3600): continue
            rate = rates['document'].get('opencode-go/' + r['model']) or rates['document'].get(r['model'])
            if not isinstance(rate, dict) or not isinstance(rate.get('monthly_limit_usd'), (int, float)): continue
            entry = used.setdefault(r['model'], [rate, 0.0])
            entry[1] += price(r, rates['document'])[0] or 0
        for model, (rate, value) in sorted(used.items(), key=lambda item: item[1][1], reverse=True):
            limit, promo, promo_ends = rate['monthly_limit_usd'], False, ''
            if isinstance(rate.get('monthly_limit_promo_usd'), (int, float)) and rate.get('promo_ends') and today.date() <= dt.date.fromisoformat(rate['promo_ends']):
                limit, promo, promo_ends = rate['monthly_limit_promo_usd'], True, rate['promo_ends']
            go_allowance['models'].append({'model': model, 'value': value, 'limit': limit, 'promo': promo, 'promoEnds': promo_ends})
    # Per-card model rows from the local ledger. Providers whose quota endpoint
    # reports one aggregate number (Ollama Cloud, for example) draw a single
    # bar, so the local token totals are the only per-model view available.
    # Each card is scoped exactly as the card itself is: by account, and by the
    # same selection the report was asked for.
    for card in cards:
        card['models'] = sorted(card_models_agg.get(card['id'], {}).values(),
                                key=lambda item: item['tokens'], reverse=True)[:4]
    # The Models table: one row per model, carrying the routes that served it.
    # A grouped row's value is the sum of what each route charged its own tokens
    # at, so it is the real total and not one route's rates applied to another's
    # traffic. `provider` stays the route that carried most of it, which is what
    # the row's colour dot and a single-route drill both follow.
    model_rows = []
    for name, b in models.items():
        route_rows = []
        for p, route in model_routes.get(name, {}).items():
            done = finish(route['bucket'])
            route_rows.append({'provider': p, 'providerName': PROVIDERS.get(p, p),
                               'model': ', '.join(sorted(route['models'])),
                               'tokens': done['tokens'], 'value': done['value'],
                               'unpricedTokens': done['unpricedTokens'], 'sessions': done['sessions']})
        route_rows.sort(key=lambda item: item['tokens'], reverse=True)
        model_rows.append(finish(b) | {'name': name, 'provider': route_rows[0]['provider'] if route_rows else '', 'routes': route_rows})
    model_rows.sort(key=lambda x: x['tokens'], reverse=True)
    for view in views:
        view.update(finish(accounts.get((view['provider'], view['accountId']), bucket())))
    return {'selection': selection, 'generatedAt': time.time(), 'period': {'days': days, 'start': str(start_date), 'end': str(today.date()),
                                                                       'startAt': start, 'resetLabel': reset_limit['label'] if reset_limit else '', 'error': period_error},
            'accountViews': views,
            'accountOptions': account_options,
            'modelOptions': [{'id': name, 'name': name, 'tokens': tokens}
                             for name, tokens in sorted(model_options.items(), key=lambda item: item[1], reverse=True)],
            'accounts': [r | {'accountId': r['name'], 'name': labels[r['name']]} for r in rows(accounts)],
            'accountWarning': 'Copies of the same history belong to different accounts. Move mirrored folders into one account.' if any(a == 'conflict' for p, a in accounts) else '',
            'availableProviders': [{'id': p, 'name': name} for p, name in PROVIDERS.items()],
            'summary': finish(summary), 'previous': finish(previous),
            'cards': cards, 'goAllowance': go_allowance,
            'providers': [finish(b) | {'id': p, 'name': PROVIDERS[p], 'quota': views_by_id.get(p + ':' + selection.get('account', 'local'), {}).get('quota', {'limits': []}), 'quotaScope': 'Current login on this PC',
                                      'valueShare': 100 * b['value'] / summary['value'] if summary['value'] else None,
                                      'monthlyPrice': cfg['monthlyPrices'].get(p) if provider_accounts[p] <= {'local'} else None}
                          for p, b in providers.items() if provider == 'all' or p == provider],
            'daily': [{'date': day, 'total': finish(values['total']), 'providers': {p: finish(b) for p, b in values['providers'].items()},
                       'cards': {card_id: finish(b) for card_id, b in values['cards'].items()}} for day, values in daily.items()],
            'hourly': [h | {'total': finish(h['total']), 'providers': {p: finish(b) for p, b in h['providers'].items()},
                            'cards': {card_id: finish(b) for card_id, b in h['cards'].items()}} for h in hourly],
            'hourlyUnplaced': {'total': finish(hourly_unplaced),
                               'providers': {p: finish(b) for p, b in unplaced_providers.items()}},
            'routes': rows(routes), 'models': model_rows, 'projects': rows(projects), 'clients': rows(clients), 'sessions': rows(sessions),
            'heatmap': dict(heatmap), 'unknownModels': sorted(unknown), 'coverage': coverage | {'earliest': earliest},
            'pricing': {'source': rates['source'], 'fetchedAtMs': rates.get('fetchedAtMs'),
                        'coveragePercent': 100 * (1 - summary['unpricedTokens']/summary['tokens']) if summary['tokens'] else None,
                        'unpriced': rows(unpriced)},
            # Reports reach the bar panel and the dashboard window. The key
            # comes back masked so the settings form can show that one is
            # stored without echoing it.
            'settings': masked_settings(saved_cfg), 'theme': theme(), 'update': update_status(saved_cfg)}


def write_agent_record(ledger, provider):
    cfg = DEFAULTS | {'enabled': [provider]}
    data = report(ledger, cfg, days=7)
    summary = data['summary']; q = quota(provider)
    total_records, total_sessions = ledger.db.execute(
        'SELECT COUNT(*),COUNT(DISTINCT session) FROM events WHERE provider=?', (provider,)).fetchone()
    active_dates = [r[0] for r in ledger.db.execute(
        "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM events WHERE provider=? ORDER BY 1", (provider,))]
    today = str(dt.date.today())
    today_data = next((x['providers'][provider] for x in data['daily'] if x['date'] == today), finish(bucket()))
    record_data = {'schemaVersion': 1, 'id': provider, 'name': PROVIDERS[provider],
       'updatedAt': q.get('updatedAt'), 'ready': bool(q.get('limits') or total_records), 'hasLocalStats': True,
       'hasPromptStats': False, 'tierLabel': 'Go' if provider == 'opencode-go' else q.get('plan', '') if provider in ('muse', 'ollama-cloud', 'commandcode', 'clinepass') else '', 'limits': q.get('limits', []), 'usageStatusText': q.get('error', ''),
       'todayTotalTokens': today_data['tokens'], 'todayPrompts': today_data['requests'], 'todaySessions': today_data['sessions'],
       'totalPrompts': total_records, 'totalSessions': total_sessions,
       'activeDays': len(active_dates), 'activeDates': active_dates,
       'recentDays': [{'date': x['date'], 'messageCount': x['providers'][provider]['tokens']} for x in data['daily']],
       'modelUsage': {}}
    # A prepaid balance is part of the account's state the card shows, so it
    # travels with the record the panel reads. Providers without one leave the
    # key out rather than carrying a null.
    if q.get('balance'): record_data['balance'] = q['balance']
    # Existing popup labels model totals as all-time. Supply the full ledger.
    for model, inp, out, read, write in ledger.db.execute('SELECT model,SUM(input),SUM(output),SUM(cacheRead),SUM(cacheWrite) FROM events WHERE provider=? GROUP BY model', (provider,)):
        record_data['modelUsage'][model] = {'inputTokens': inp, 'outputTokens': out, 'cacheReadInputTokens': read, 'cacheCreationInputTokens': write}
    atomic_json(STATE.parent / ('agents/usage/' + provider + '.json'), record_data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['report', 'scan', 'pulse', 'go', 'settings', 'theme', 'pin'])
    parser.add_argument('--days', type=int, choices=[1, 7, 30, 90, 365], default=7)
    parser.add_argument('--provider', choices=['all', *PROVIDERS], default='all')
    for field in ('model', 'project', 'client', 'apiProvider', 'day', 'account', 'resetWindow'): parser.add_argument('--' + field)
    parser.add_argument('--hourStart', type=int)
    # A source (provider) can be left out of a view without turning it off in
    # settings: repeat the flag once per source to exclude.
    parser.add_argument('--excludeSource', action='append', default=[])
    parser.add_argument('--save', nargs='?', const=''); parser.add_argument('--force', action='store_true')
    parser.add_argument('--pin-provider', default=''); parser.add_argument('--pin-label', default='')
    parser.add_argument('--pin-title', default='')
    parser.add_argument('--pin-remove', action='store_true')
    args = parser.parse_args()
    # Theme reads stay out of the ledger path so a theme swap can repaint
    # without waiting on a history scan.
    if args.action == 'theme':
        print(json.dumps(theme())); return
    if args.action == 'pin':
        try: print(json.dumps(save_pinned_limit(args.pin_provider, args.pin_label, args.pin_title, args.pin_remove)))
        except ValueError as error:
            print(json.dumps({'error': str(error)})); raise SystemExit(1)
        return
    if args.action == 'report':
        ledger = Ledger(STATE / 'usage.sqlite', readonly=True)
        try:
            cfg = settings()
            if not CONFIG.exists():
                found = {r[0] for r in ledger.db.execute('SELECT DISTINCT provider FROM events')}
                cfg = cfg | {'enabled': [p for p in PROVIDERS if p in found] or cfg['enabled']}
            print(json.dumps(report(ledger, cfg, args.days, args.provider, selection={k: getattr(args, k) for k in ('model', 'project', 'client', 'apiProvider', 'day', 'account', 'resetWindow', 'hourStart', 'excludeSource') if getattr(args, k)})))
        finally: ledger.db.close()
        return
    STATE.mkdir(parents=True, exist_ok=True)
    if args.action == 'settings':
        try:
            # The payload arrives on stdin when a client sends it that way, so
            # a key never has to appear in a command line. --save with a value
            # stays supported for scripted use.
            raw = args.save
            if raw is not None and not raw.strip():
                raw = stdin_payload()
                if raw is None:
                    # Nothing to write. Reporting the settings and exiting 0
                    # would close the window with "Settings saved" and save
                    # nothing, so this is the failure case.
                    print(json.dumps({'error': 'No settings were received to save.'})); raise SystemExit(1)
            # The settings channel is what writes the key, so it is also the
            # one place the key could leak back out through stdout. Every
            # answer masks it, as the usage reports already do.
            clean = save_settings(json.loads(raw)) if raw else settings()
            print(json.dumps(masked_settings(clean)))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            print(json.dumps({'error': str(error)})); raise SystemExit(1)
        return
    with (STATE / 'collector.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = Ledger(STATE / 'usage.sqlite'); cfg = settings()
        if not CONFIG.exists():
            # Auto-enable runs before the scan so sources with recorded
            # history are collected on a fresh install, as documented.
            found = {r[0] for r in ledger.db.execute('SELECT DISTINCT provider FROM events')}
            if os.getenv('AI_USAGE_DEMO') != '1' and cursor_token(): found.add('cursor')
            cfg = cfg | {'enabled': list(dict.fromkeys(cfg['enabled'] + [p for p in PROVIDERS if p in found]))}
        if args.action == 'scan':
            if os.getenv('AI_USAGE_DEMO') != '1': check_update(cfg)
            ledger.scan(cfg, force=args.force)
            atomic_json(STATE / 'hourly-summary.json', hourly_snapshot(ledger, cfg=cfg))
            if not CONFIG.exists():
                found = {r[0] for r in ledger.db.execute('SELECT DISTINCT provider FROM events')}
                cfg = cfg | {'enabled': list(dict.fromkeys(cfg['enabled'] + [p for p in PROVIDERS if p in found]))}
        if args.action == 'pulse':
            # The live counter has a local, bounded path. It never checks
            # provider limits, fetches Cursor usage, or copies a synced ledger.
            ledger.scan(cfg, local_only=True)
            snapshot = hourly_snapshot(ledger, cfg=cfg)
            atomic_json(STATE / 'hourly-summary.json', snapshot)
            print(json.dumps(snapshot))
        if args.action in ('go', 'scan') and os.getenv('AI_USAGE_DEMO') != '1':
            if args.action == 'go' or 'opencode-go' in cfg['enabled']:
                go_quota(args.force)
                if args.action == 'go': ledger.scan(cfg)
                write_agent_record(ledger, 'opencode-go')
            if args.action == 'scan' and 'grok' in cfg['enabled']:
                grok_quota(args.force)
                write_agent_record(ledger, 'grok')
            if args.action == 'scan' and 'muse' in cfg['enabled']:
                muse_quota(args.force)
                write_agent_record(ledger, 'muse')
            if args.action == 'scan' and 'ollama-cloud' in cfg['enabled']:
                ollama_quota(args.force)
                write_agent_record(ledger, 'ollama-cloud')
            if args.action == 'scan' and 'commandcode' in cfg['enabled']:
                commandcode_quota(args.force)
                write_agent_record(ledger, 'commandcode')
            if args.action == 'scan' and 'clinepass' in cfg['enabled']:
                clinepass_quota(args.force)
                write_agent_record(ledger, 'clinepass')

            if args.action == 'scan' and 'cursor' in cfg['enabled']:
                cursor_quota(args.force)
            if args.action == 'scan':
                for p in ('gemini', 'opencode', 'pi', 'omp', 'cursor'):
                    if p in cfg['enabled']: write_agent_record(ledger, p)
            # The records above carry fresh limits, and a window that rolled
            # over needs its count recomputed against the new reset time.
            atomic_json(STATE / 'hourly-summary.json', hourly_snapshot(ledger, cfg=cfg))
        if args.action == 'scan': print(json.dumps({'ok': True, 'events': ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]}))
        elif args.action == 'go': print(json.dumps({'ok': not bool(quota('opencode-go').get('error'))}))
        ledger.db.close()


if __name__ == '__main__':
    main()
