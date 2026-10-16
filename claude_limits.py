#!/usr/bin/env python3
"""Add Claude's banked usage-limit resets to each Claude record.

Anthropic grants Pro and Max accounts resets that clear a usage limit early.
Omarchy's Claude collector reads the same OAuth usage endpoint, but the resets
block only appears when the request asks for it (`cedar_ember=1`) and comes
from a current Claude Code CLI. Any other User-Agent is answered with
`ineligible_reason: "surface"`, an old CLI version with "cli_version". The
request therefore identifies as the Claude Code version installed here, which
is also the client the reset is spent from.

The token is read from Claude Code's own credential file and sent only to
Anthropic's usage endpoint.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib import error, request

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1'
# The endpoint rate-limits quickly, and the panel refreshes on every open.
# A fresh answer is reused without asking; after a failed read the last answer
# stands a while longer before the count becomes unknown.
FRESH_SECONDS = 300
# T3 Code keeps a recent reading per Claude login in a shared cache.
SHARE_FRESH_SECONDS = 180
STALE_SECONDS = 1800


def claude_dir():
    return Path(os.path.expandvars(os.path.expanduser(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude')))


def shared_config_dir(folder):
    return os.path.abspath(os.path.expanduser(str(folder)))


def shared_file(folder, suffix):
    """Return one of a Claude login's shared cache paths."""
    home = Path.home()
    cache = Path(os.environ.get('XDG_CACHE_HOME') or home / '.cache')
    if not cache.is_absolute():
        cache = home / '.cache'
    config_dir = shared_config_dir(folder)
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', config_dir.lstrip('/'))
    return cache / 'claude-usage' / (name + suffix)


def shared_reading(folder, max_age=SHARE_FRESH_SECONDS):
    """Return (usage, fetched_at seconds) from T3 Code's shared cache."""
    try:
        config_dir = shared_config_dir(folder)
        payload = json.loads(shared_file(folder, '.json').read_text())
        usage = payload.get('usage')
        fetched_at_ms = payload.get('fetchedAtMs')
        if payload.get('configDir') != config_dir:
            return None
        if not isinstance(usage, dict):
            return None
        if not isinstance(fetched_at_ms, (int, float)) or isinstance(fetched_at_ms, bool):
            return None
        fetched_at = fetched_at_ms / 1000
        if max_age is not None and not 0 <= time.time() - fetched_at < max_age:
            return None
        return usage, fetched_at
    except Exception:
        return None


def t3_polling(folder):
    """Whether T3 Code owns polling for this Claude login."""
    try:
        config_dir = shared_config_dir(folder)
        payload = json.loads(shared_file(folder, '.poll.json').read_text())
        attempted_at_ms = payload.get('attemptedAtMs')
        next_attempt_at_ms = payload.get('nextAttemptAtMs')
        if payload.get('configDir') != config_dir:
            return False
        if not isinstance(attempted_at_ms, (int, float)) or isinstance(attempted_at_ms, bool):
            return False
        if not isinstance(next_attempt_at_ms, (int, float)) or isinstance(next_attempt_at_ms, bool):
            return False
        deadline = max(attempted_at_ms / 1000 + SHARE_FRESH_SECONDS,
                       next_attempt_at_ms / 1000 + 60)
        return time.time() < deadline
    except Exception:
        return False


def access_token(folder):
    login = json.loads((folder / '.credentials.json').read_text()).get('claudeAiOauth') or {}
    token = login.get('accessToken')
    if not token:
        raise ValueError('Claude Code is not signed in')
    expires = login.get('expiresAt')
    if isinstance(expires, (int, float)) and 0 < expires <= time.time() * 1000:
        raise ValueError('Claude Code sign-in expired')
    return token


def cli_version():
    # The bar runs refreshes with the desktop session's PATH, which can miss
    # where Claude Code was installed, so try its installers' own spots too.
    home = Path.home()
    for command in (shutil.which('claude'), home / '.local/bin/claude', home / '.claude/local/claude'):
        if not command:
            continue
        try:
            output = subprocess.run([str(command), '--version'], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.match(r'\s*(\d+\.\d+\.\d+)', output)
        if match:
            return match.group(1)
    raise ValueError('Claude Code CLI version unavailable')


def parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def banked(block, now=None):
    """Spendable resets and the earliest expiry among them."""
    if not isinstance(block, dict):
        raise ValueError('No reset block in the usage response')
    if not block.get('eligible'):
        # These two describe the request, not the account.
        if block.get('ineligible_reason') in ('surface', 'cli_version'):
            raise ValueError('Reset block refused: %s' % block.get('ineligible_reason'))
        return 0, ''
    now = now or datetime.now(timezone.utc)
    count, expiry = 0, None
    for grant in block.get('grants') or []:
        if not isinstance(grant, dict):
            continue
        left = grant.get('resets_left')
        if not isinstance(left, int) or isinstance(left, bool) or left <= 0:
            continue
        ends = parse_time(grant.get('ends_at')) if grant.get('ends_at') else None
        if ends and ends <= now:
            continue
        count += left
        if ends and (expiry is None or ends < expiry):
            expiry = ends
    return count, expiry.isoformat() if expiry else ''


def fetch(folder):
    headers = {'Authorization': 'Bearer ' + access_token(folder),
               'anthropic-beta': 'oauth-2025-04-20',
               'Accept': 'application/json',
               'User-Agent': 'claude-cli/%s (external, cli)' % cli_version()}
    with request.urlopen(request.Request(USAGE_URL, headers=headers), timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError('Unrecognized usage response')
    return banked(payload.get('cedar_ember'))


def answer(cache, force, folder):
    """(count, expiry), from the cache while it is fresh; count None when unknown."""
    shared = shared_reading(folder)
    if shared is not None:
        try:
            result = banked(shared[0].get('cedar_ember'))
        except ValueError:
            pass
        else:
            save(cache, {'fetchedAt': shared[1], 'count': result[0], 'expiresAt': result[1]})
            return result
    try:
        cached = json.loads(cache.read_text())
    except (OSError, ValueError):
        cached = {}
    if not isinstance(cached, dict):
        cached = {}
    try:
        age = time.time() - float(cached['fetchedAt'])
        last = (cached['count'], cached['expiresAt'])
    except (KeyError, TypeError, ValueError):
        age, last = None, None
    stale = last if last and age < STALE_SECONDS else (None, '')
    if t3_polling(folder):
        shared = shared_reading(folder, max_age=None)
        if shared is not None:
            try:
                cached_at = float(cached['fetchedAt'])
            except (KeyError, TypeError, ValueError):
                cached_at = None
            if cached_at is None or shared[1] > cached_at:
                try:
                    result = banked(shared[0].get('cedar_ember'))
                except ValueError:
                    pass
                else:
                    save(cache, {'fetchedAt': shared[1], 'count': result[0], 'expiresAt': result[1]})
                    return result
        return stale
    if last and not force and age < FRESH_SECONDS:
        return last
    # A rate-limited endpoint said when to come back; even --force waits.
    if time.time() < float(cached.get('retryAt') or 0):
        return stale
    try:
        result = fetch(folder)
    except error.HTTPError as failure:
        if failure.code == 429:
            try:
                wait = float(failure.headers.get('retry-after') or 60)
            except (TypeError, ValueError):
                wait = 60
            save(cache, dict(cached, retryAt=time.time() + wait))
        return stale
    except (OSError, ValueError):
        return stale
    save(cache, {'fetchedAt': time.time(), 'count': result[0], 'expiresAt': result[1]})
    return result


def save(cache, data):
    # A timer refresh and a panel refresh can overlap; never let one read the
    # other's half-written cache.
    cache.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + cache.name, dir=cache.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output)
        os.replace(temporary, cache)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update(path, count, expiry):
    try:
        before = path.read_bytes()
        record = json.loads(before)
    except (OSError, ValueError):
        return False
    fields = {'resetCreditsAvailable': count, 'resetCreditsExpiresAt': expiry}
    # A record without the fields already reads as unknown.
    if record.get('resetCreditsAvailable') == count and record.get('resetCreditsExpiresAt', '') == expiry:
        return False
    record.update(fields)
    # A concurrent refresh owns a newer record; leave it alone.
    try:
        if path.read_bytes() != before:
            return False
    except OSError:
        return False
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(record, output, separators=(',', ':'))
            output.write('\n')
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def homes(config, usage):
    """Map each Claude record to its login folder.

    The main record belongs to the default folder. Another Claude record is
    linked to a configured account with one Claude folder the way the
    dashboard links them: by account id, then by a unique matching name.
    """
    result = {'claude': claude_dir()}
    try:
        accounts = json.loads(config.read_text()).get('accounts') or []
    except (OSError, ValueError, AttributeError):
        return result
    folders = {}
    for account in accounts if isinstance(accounts, list) else []:
        if not isinstance(account, dict) or not account.get('id'):
            continue
        paths = [item.get('path') for item in account.get('directories') or []
                 if isinstance(item, dict) and item.get('provider') == 'claude' and item.get('path')]
        if len(paths) == 1:
            folders[str(account['id'])] = (str(account.get('label') or '').casefold(), Path(paths[0]).expanduser())
    for path in sorted(usage.glob('claude[-:]*.json')):
        key = path.stem
        try:
            name = str(json.loads(path.read_text()).get('name') or '').casefold()
        except (OSError, ValueError, AttributeError):
            continue
        matches = [folder for aid, (_, folder) in folders.items() if key in (aid, 'claude:' + aid)]
        if not matches and name:
            matches = [folder for label, folder in folders.values() if label == name]
        if len(matches) == 1:
            result[key] = matches[0]
    return result


def main(argv=None):
    force = '--force' in (sys.argv[1:] if argv is None else argv)
    home = Path.home()
    usage = Path(os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 'omarchy/agents/usage'
    config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'omarchy/ai-usage/settings.json'
    caches = Path(os.environ.get('XDG_CACHE_HOME', home / '.cache')) / 'omarchy-usage-dashboard'
    for key, folder in homes(config, usage).items():
        record = usage / (key + '.json')
        if not record.exists():
            continue
        cache = caches / ('claude-resets.json' if key == 'claude' else 'claude-resets-%s.json' % key)
        count, expiry = answer(cache, force, folder)
        update(record, count, expiry)
    return 0


if __name__ == '__main__':
    sys.exit(main())
