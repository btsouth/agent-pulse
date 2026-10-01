#!/usr/bin/env python3
"""Repair Codex limit records after Omarchy's app-server read times out.

Omarchy's RPC reader uses select() with a buffered text stream. A reply can
already be in Python's buffer when select() checks the file descriptor, making
account/read time out despite a healthy connection. Read bytes directly here.
"""

import json
import fcntl
import hashlib
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib import request

RESET_CREDITS_URL = 'https://chatgpt.com/backend-api/wham/rate-limit-reset-credits'
USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'


def fetch_credit_balance(home):
    """Read purchased ChatGPT credits and track observed balance decreases.

    This account-wide balance includes Work and Codex. It is not an API dollar
    balance or an earned rate-limit reset. Never infer credits from tokens.
    """
    auth = json.loads((Path(home) / 'auth.json').read_text())
    tokens = auth.get('tokens') or {}
    token, account = tokens.get('access_token'), tokens.get('account_id')
    if not token or not account:
        raise ValueError('Codex account credentials unavailable')
    state = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'omarchy/ai-usage'
    state.mkdir(parents=True, exist_ok=True)
    tracker = state / 'chatgpt-credits.json'
    # Serialize refreshes so two collectors cannot count the same decrease.
    with open(state / '.chatgpt-credits.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        headers = {'Authorization': 'Bearer ' + token, 'ChatGPT-Account-Id': account,
                   'Accept': 'application/json', 'Cache-Control': 'no-cache',
                   'User-Agent': 'omarchy-usage-dashboard'}
        with request.urlopen(request.Request(USAGE_URL, headers=headers), timeout=10) as response:
            payload = json.load(response)
        credits = payload.get('credits') if isinstance(payload, dict) else None
        if not isinstance(credits, dict):
            raise ValueError('ChatGPT credit balance unavailable')
        now = datetime.now(timezone.utc).isoformat()
        if credits.get('unlimited') is True:
            return {'unlimited': True, 'updatedAt': now}
        raw = credits.get('balance')
        if raw is None or isinstance(raw, bool):
            raise ValueError('ChatGPT credit balance unavailable')
        try:
            remaining = Decimal(str(raw))
        except InvalidOperation:
            raise ValueError('ChatGPT credit balance unavailable') from None
        if not remaining.is_finite() or remaining < 0:
            raise ValueError('ChatGPT credit balance unavailable')
        try:
            history = json.loads(tracker.read_text())
        except (OSError, ValueError):
            history = {}
        if not isinstance(history, dict):
            history = {}
        # Account ids are opaque identifiers, not credentials. Keep even those
        # out of the persisted file; credentials are never saved here.
        key = hashlib.sha256(str(account).encode()).hexdigest()
        previous = history.get(key) or {}
        try:
            spent = Decimal(str(previous.get('spent', 0)))
            prior_balance = Decimal(str(previous.get('remaining', remaining)))
            if not spent.is_finite() or spent < 0 or not prior_balance.is_finite() or prior_balance < 0:
                raise ValueError('Invalid saved balance')
        except (InvalidOperation, ValueError, AttributeError):
            previous, spent, prior_balance = {}, Decimal(0), remaining
        spent += max(Decimal(0), prior_balance - remaining)
        entry = {'remaining': str(remaining), 'spent': str(spent),
                 'trackingSince': previous.get('trackingSince') or now, 'updatedAt': now}
        history[key] = entry
        fd, temporary = tempfile.mkstemp(prefix='.chatgpt-credits-', dir=state)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(history, output)
                output.write('\n')
            os.replace(temporary, tracker)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return entry | {'remaining': float(remaining), 'spent': float(spent), 'unlimited': False, 'estimated': True}


def rpc(proc, request_id, method, params=None, timeout=8):
    proc.stdin.write((json.dumps({'id': request_id, 'method': method,
                                  'params': params or {}}) + '\n').encode())
    proc.stdin.flush()
    deadline = time.monotonic() + timeout
    buffer = getattr(proc, '_usage_rpc_buffer', b'')
    while time.monotonic() < deadline:
        while b'\n' in buffer:
            line, buffer = buffer.split(b'\n', 1)
            proc._usage_rpc_buffer = buffer
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if message.get('id') == request_id:
                if message.get('error'):
                    raise RuntimeError(method)
                return message.get('result') or {}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not select.select([proc.stdout], [], [], min(remaining, 0.25))[0]:
            continue
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            break
        buffer += chunk
        proc._usage_rpc_buffer = buffer
    raise TimeoutError(method)


def window(raw):
    if not isinstance(raw, dict) or raw.get('usedPercent') is None:
        return None
    minutes = int(raw.get('windowDurationMins') or 0)
    label = ('Weekly (7-day)' if minutes == 10080 else
             f'{minutes // 60}h window' if minutes and minutes % 60 == 0 else
             f'{minutes}m window' if minutes else 'Limit')
    reset = raw.get('resetsAt')
    return {'label': label, 'percent': float(raw['usedPercent']) / 100,
            'resetsAt': datetime.fromtimestamp(float(reset), timezone.utc).isoformat() if reset else ''}


def fetch(home):
    codex = shutil.which('codex')
    if not codex:
        raise FileNotFoundError('codex')
    env = os.environ.copy()
    env['CODEX_HOME'] = str(home)
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN'):
        env.pop(key, None)
    proc = subprocess.Popen([codex, '-s', 'read-only', '-a', 'on-request', 'app-server'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env)
    try:
        rpc(proc, 1, 'initialize', {'clientInfo': {'name': 'omarchy-usage-dashboard', 'version': '1'}})
        proc.stdin.write(b'{"method":"initialized","params":{}}\n')
        proc.stdin.flush()
        account = (rpc(proc, 2, 'account/read').get('account') or {})
        limits = (rpc(proc, 3, 'account/rateLimits/read').get('rateLimits') or {})
        entries = [window(item) for item in (limits.get('primary'), limits.get('secondary'))]
        entries = [entry for entry in entries if entry]
        if not entries:
            raise ValueError('No Codex limit windows')
        return entries, str(limits.get('planType') or account.get('planType') or account.get('type') or '')
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def homes(config, default):
    result = {'codex': default}
    try:
        settings = json.loads(config.read_text())
    except (OSError, ValueError):
        return result
    for account in settings.get('accounts', []):
        folders = [item.get('path') for item in account.get('directories', [])
                   if item.get('provider') == 'codex' and item.get('path')]
        if len(folders) == 1 and account.get('id'):
            result['codex:' + str(account['id'])] = Path(folders[0])
            result[str(account['id'])] = Path(folders[0])
    return result


def fetch_banked_resets(home):
    """Read one account's spendable reset count, and the earliest expiry among
    its live credits, from that home's credentials."""
    auth = json.loads((Path(home) / 'auth.json').read_text())
    tokens = auth.get('tokens') or {}
    access_token = tokens.get('access_token')
    account_id = tokens.get('account_id')
    if not access_token or not account_id:
        raise ValueError('Codex account credentials unavailable')
    headers = {'Authorization': 'Bearer ' + access_token,
               'ChatGPT-Account-Id': account_id,
               'Accept': 'application/json', 'Cache-Control': 'no-cache',
               'User-Agent': 'omarchy-usage-dashboard'}
    with request.urlopen(request.Request(RESET_CREDITS_URL, headers=headers), timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError('Unrecognized reset-credit response')
    credits = payload.get('credits')
    count = payload.get('available_count')
    reported = isinstance(count, int) and not isinstance(count, bool) and count >= 0
    if not reported and not isinstance(credits, list):
        raise ValueError('Unrecognized reset-credit response')
    now = datetime.now(timezone.utc)
    total, expiry = 0, None
    for credit in credits if isinstance(credits, list) else []:
        if not isinstance(credit, dict) or credit.get('status') != 'available':
            continue
        try:
            ends = datetime.fromisoformat(str(credit.get('expires_at')).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            ends = None
        if ends and ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
        if ends and ends <= now:
            continue
        total += 1
        if ends and (expiry is None or ends < expiry):
            expiry = ends
    count = count if reported else total
    return count, expiry.isoformat() if expiry and count else ''


def repair(path, home, credit_balance=None):
    try:
        before = path.read_bytes()
        record = json.loads(before)
    except (OSError, ValueError):
        return False
    changed = False
    if record.get('usageStatusText') == 'Codex limits unavailable' and record.get('authHelpText') in ('account/read', 'account/rateLimits/read'):
        try:
            limits, tier = fetch(home)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            pass
        else:
            record.update(limits=limits, tierLabel=tier, usageStatusText='', authHelpText='Run `codex login` to authenticate.')
            changed = True
    # Custom collectors opt in by writing this field. Refresh it against the
    # matching account, even when the limits RPC succeeded. A failed read is
    # unknown, never a reason to keep displaying a possibly spent credit.
    if 'resetCreditsAvailable' in record:
        try:
            count, expiry = fetch_banked_resets(home)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            count, expiry = None, ''
        if record['resetCreditsAvailable'] != count or record.get('resetCreditsExpiresAt', '') != expiry:
            record['resetCreditsAvailable'] = count
            record['resetCreditsExpiresAt'] = expiry
            changed = True
    try:
        credits = credit_balance if credit_balance is not None else fetch_credit_balance(home)
    except (OSError, ValueError, RuntimeError, TimeoutError):
        credits = {'error': 'Credit balance unavailable'}
    if record.get('chatgptCredits') != credits:
        record['chatgptCredits'] = credits
        changed = True
    if not changed:
        return False
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


def main():
    home = Path.home()
    config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'omarchy/ai-usage/settings.json'
    usage = Path(os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 'omarchy/agents/usage'
    # The main card belongs to the default home. A caller may itself be
    # running under another CODEX_HOME (for example a second Codex session).
    balances = {}
    snapshots = {}
    for agent, folder in homes(config, home / '.codex').items():
        path = usage / (agent + '.json')
        if path.exists():
            if folder not in balances:
                try:
                    balances[folder] = fetch_credit_balance(folder)
                except (OSError, ValueError, RuntimeError, TimeoutError):
                    balances[folder] = {'error': 'Credit balance unavailable',
                                        'updatedAt': datetime.now(timezone.utc).isoformat()}
            repair(path, folder, balances[folder])
            snapshots[agent] = balances[folder]
    if snapshots:
        # Native collectors replace their records independently. An owned
        # snapshot keeps those writes from erasing the credit section.
        state = usage.parent.parent / 'ai-usage'
        state.mkdir(parents=True, exist_ok=True)
        with open(state / '.chatgpt-credit-snapshots.lock', 'a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                saved = json.loads((state / 'chatgpt-credit-snapshots.json').read_text())
            except (OSError, ValueError):
                saved = {}
            if isinstance(saved, dict):
                for agent, value in snapshots.items():
                    prior = saved.get(agent)
                    if isinstance(prior, dict) and prior.get('updatedAt', '') > value.get('updatedAt', ''):
                        snapshots[agent] = prior
            fd, temporary = tempfile.mkstemp(prefix='.chatgpt-credit-snapshots-', dir=state)
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(snapshots, output)
                    output.write('\n')
                os.replace(temporary, state / 'chatgpt-credit-snapshots.json')
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


if __name__ == '__main__':
    sys.exit(main())
