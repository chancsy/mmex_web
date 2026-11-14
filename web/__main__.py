"""python -m web setup           set the login password (creates secrets.json)
   python -m web setup --rotate  also replace the session key (logs everyone out)
   python -m web token SOURCE    new API token for a queue source, printed once
   python -m web token SOURCE --revoke
   python -m web token --list    the sources that have a token
   python -m web run [--port N]  development server on 127.0.0.1 (use gunicorn otherwise)

Both read MMEX_DB_PATH / APP_DATA_DIR etc. from the environment.
"""

import argparse
import getpass
import os
import sys

from . import secrets_file
from .config import ConfigError, Settings


def cmd_setup(args):
    data_dir = os.environ.get('APP_DATA_DIR')
    if not data_dir:
        sys.exit('set APP_DATA_DIR')
    path = os.path.join(data_dir, 'secrets.json')
    if args.password_stdin:
        password = sys.stdin.readline().rstrip('\n')
    else:
        password = getpass.getpass('New password: ')
        if getpass.getpass('Again: ') != password:
            sys.exit('Passwords differ; nothing changed.')
    try:
        secrets_file.create_or_update(path, password, rotate_session_key=args.rotate)
    except secrets_file.SecretsError as exc:
        sys.exit(str(exc))
    print(f'Wrote {path} (mode 600).' + (' Session key replaced; everyone is logged out.' if args.rotate else ''))


def cmd_token(args):
    data_dir = os.environ.get('APP_DATA_DIR')
    if not data_dir:
        sys.exit('set APP_DATA_DIR')
    path = os.path.join(data_dir, 'secrets.json')
    try:
        if args.list:
            print('\n'.join(sorted(secrets_file.load(path)['api_tokens'])) or '(none)')
            return
        if not args.source:
            sys.exit('name a source, e.g. python -m web token check_bills')
        token = secrets_file.set_token(path, args.source, revoke=args.revoke)
    except secrets_file.SecretsError as exc:
        sys.exit(str(exc))
    if args.revoke:
        print(f'Revoked the token for {args.source}.', file=sys.stderr)
    else:
        # The token alone on stdout, so a script can capture it; the note on stderr.
        print(token)
        print(f'New token for {args.source}; any earlier one stops working. It is not stored, '
              'so keep it now.', file=sys.stderr)


def cmd_run(args):
    from . import create_app
    try:
        app = create_app(Settings.from_env())
    except (ConfigError, secrets_file.SecretsError) as exc:
        sys.exit(str(exc))
    app.run(host='127.0.0.1', port=args.port, debug=False)


def main():
    ap = argparse.ArgumentParser(prog='python -m web')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('setup', help='set the login password')
    p.add_argument('--rotate', action='store_true', help='also replace the session-signing key')
    p.add_argument('--password-stdin', action='store_true', help='read the password from stdin (for scripts)')
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser('token', help='make (or revoke) the API token of a queue source')
    p.add_argument('source', nargs='?')
    p.add_argument('--revoke', action='store_true')
    p.add_argument('--list', action='store_true')
    p.set_defaults(func=cmd_token)
    p = sub.add_parser('run', help='development server on 127.0.0.1')
    p.add_argument('--port', type=int, default=8000)
    p.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
