"""secrets.json in APP_DATA_DIR: the login password's hash, the key that
signs session cookies, and one API token hash per queue source.

Written by `python -m web setup` and `python -m web token`, mode 600, never
by the running app. Neither the password nor a token is stored, only hashes.
"""

import hashlib
import json
import os
import re
import secrets

from werkzeug.security import generate_password_hash

MIN_PASSWORD_LENGTH = 12
SOURCE_NAME = re.compile(r'[a-z][a-z0-9_]{0,31}')
RESERVED_SOURCES = ('you',)  # the History page's name for the UI's own writes


class SecretsError(Exception):
    pass


def load(path) -> dict:
    if not os.path.isfile(path):
        raise SecretsError(f'{path} not found; create it with: python -m web setup')
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    for key in ('password_hash', 'session_key'):
        if not data.get(key):
            raise SecretsError(f'{path} has no {key}; re-run: python -m web setup')
    data.setdefault('api_tokens', {})
    return data


def _write(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def create_or_update(path, password, rotate_session_key=False) -> dict:
    """Set the password (and a session key if there isn't one, or if asked
    to rotate it). Keeps existing API tokens."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SecretsError(f'password must be at least {MIN_PASSWORD_LENGTH} characters')
    data = {}
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    data['password_hash'] = generate_password_hash(password)
    if rotate_session_key or not data.get('session_key'):
        data['session_key'] = secrets.token_hex(32)
    data.setdefault('api_tokens', {})
    _write(path, data)
    return data


def set_token(path, source, revoke=False):
    """Make a new API token for `source` (replacing any old one) and return
    it; only its hash is kept. revoke=True removes the source's token."""
    if not SOURCE_NAME.fullmatch(source or '') or source in RESERVED_SOURCES:
        raise SecretsError('a source name is lowercase letters, digits and _, starting with a letter, '
                           f'at most 32 characters, and not {RESERVED_SOURCES}')
    data = load(path)
    if revoke:
        if data['api_tokens'].pop(source, None) is None:
            raise SecretsError(f'no token for {source}')
        _write(path, data)
        return None
    token = secrets.token_urlsafe(32)
    data['api_tokens'][source] = hashlib.sha256(token.encode()).hexdigest()
    _write(path, data)
    return token
