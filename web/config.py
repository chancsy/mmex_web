"""Settings, all from environment variables (the compose file sets them).

| Variable | Default | Meaning |
|---|---|---|
| MMEX_DB_PATH | (required) | The MMEX .mmb file. |
| APP_DATA_DIR | (required) | mmex_web's own data: secrets.json and app.db (history of the app's writes). |
| MMEX_BACKUP_DIR | '' | Where DB backups go. Without it the app never writes (read-only). |
| MMEX_BACKUP_KEEP | 20 | How many of the app's backups to keep. |
| MMEX_BACKUP_MIN_INTERVAL_MIN | 30 | Skip the backup when the newest is younger than this; 0 backs up every write. |
| MMEX_WEB_URL_PREFIX | '' | Serve under a path, e.g. /money (behind a proxy that keeps the path). |
| MMEX_WEB_COOKIE_SECURE | 1 | Send the session cookie over HTTPS only. Set 0 only for plain-HTTP local testing. |
| MMEX_WEB_TRUSTED_PROXIES | 0 | Reverse proxies in front that set X-Forwarded-*; 0 trusts none. |
| MMEX_WEB_SESSION_DAYS | 30 | How long a login lasts. |
| MMEX_WEB_API_VIA_PROXY | 0 | Let /api/* answer requests that came through a reverse proxy. Off: the API answers only direct callers. |
"""

import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


def _flag(value) -> bool:
    return str(value).strip().lower() not in ('0', 'false', 'no', 'off', '')


def _int(name, value, minimum=0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f'{name} must be a whole number, got {value!r}')
    if n < minimum:
        raise ConfigError(f'{name} must be at least {minimum}')
    return n


@dataclass
class Settings:
    db_path: str
    data_dir: str
    url_prefix: str = ''
    cookie_secure: bool = True
    trusted_proxies: int = 0
    session_days: int = 30
    backup_dir: str = ''
    backup_keep: int = 20
    backup_min_interval_min: int = 30
    api_via_proxy: bool = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        missing = [k for k in ('MMEX_DB_PATH', 'APP_DATA_DIR') if not env.get(k)]
        if missing:
            raise ConfigError(f'set {", ".join(missing)}')
        prefix = env.get('MMEX_WEB_URL_PREFIX', '').strip().rstrip('/')
        if prefix and not prefix.startswith('/'):
            raise ConfigError('MMEX_WEB_URL_PREFIX must start with /')
        return cls(
            db_path=env['MMEX_DB_PATH'],
            data_dir=env['APP_DATA_DIR'],
            url_prefix=prefix,
            cookie_secure=_flag(env.get('MMEX_WEB_COOKIE_SECURE', '1')),
            trusted_proxies=_int('MMEX_WEB_TRUSTED_PROXIES', env.get('MMEX_WEB_TRUSTED_PROXIES', '0')),
            session_days=_int('MMEX_WEB_SESSION_DAYS', env.get('MMEX_WEB_SESSION_DAYS', '30'), minimum=1),
            backup_dir=env.get('MMEX_BACKUP_DIR', '').strip(),
            backup_keep=_int('MMEX_BACKUP_KEEP', env.get('MMEX_BACKUP_KEEP', '20'), minimum=1),
            backup_min_interval_min=_int('MMEX_BACKUP_MIN_INTERVAL_MIN', env.get('MMEX_BACKUP_MIN_INTERVAL_MIN', '30')),
            api_via_proxy=_flag(env.get('MMEX_WEB_API_VIA_PROXY', '0')),
        )

    @property
    def secrets_path(self) -> str:
        return os.path.join(self.data_dir, 'secrets.json')

    @property
    def store_path(self) -> str:
        return os.path.join(self.data_dir, 'app.db')
