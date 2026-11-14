"""mmex_web's Flask app.

    gunicorn --workers 1 --threads 4 'web:create_app()'

One worker on purpose: once the app writes, a single process is the single
writer. Settings come from the environment (see config.py); the secrets
file comes from `python -m web setup`.
"""

import datetime
import hashlib
import os

from flask import Flask, render_template, request, send_from_directory, url_for
from markupsafe import Markup
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import safe_join

from . import api, auth, entry, pages, queue as queue_page, rates as rates_page, secrets_file, settings as settings_page
from .config import Settings
from .store import Store
from .writer import Writer


class PrefixMiddleware:
    """Serve the app under a path prefix (e.g. /money) when the proxy in
    front passes the full path through. Requests outside the prefix get 404."""

    def __init__(self, app, prefix):
        self.app, self.prefix = app, prefix

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == '/healthz':
            # The container healthcheck doesn't know the prefix.
            return self.app(environ, start_response)
        if path == self.prefix or path.startswith(self.prefix + '/'):
            environ['SCRIPT_NAME'] = environ.get('SCRIPT_NAME', '') + self.prefix
            environ['PATH_INFO'] = path[len(self.prefix):] or '/'
            return self.app(environ, start_response)
        start_response('404 Not Found', [('Content-Type', 'text/plain; charset=utf-8')])
        return [b'Not found']


def _money(value, currency=None):
    currency = currency or {'prefix': '', 'suffix': '', 'decimals': 2}
    sign = '-' if round(value, currency['decimals']) < 0 else ''
    number = f'{abs(value):,.{currency["decimals"]}f}'
    return f'{sign}{currency["prefix"]}{number}{currency["suffix"]}'


class Assets:
    """Static file URLs with a content hash (?v=...), so a changed file gets
    a new URL. Proxies such as Cloudflare can keep static files in browsers
    for hours whatever the app says; an old stylesheet with new pages
    breaks the layout. Hashes are computed once per process (a code change
    needs a restart anyway)."""

    def __init__(self, folder):
        self.folder, self.versions = folder, {}

    def version(self, filename) -> str:
        if filename not in self.versions:
            with open(os.path.join(self.folder, filename), 'rb') as fh:
                self.versions[filename] = hashlib.sha256(fh.read()).hexdigest()[:12]
        return self.versions[filename]

    def url(self, filename) -> str:
        return url_for('static', filename=filename, v=self.version(filename))


def _secret(value, currency=None):
    """A balance the eye toggle can hide: the figure, plus dots shown in
    its place while balances are hidden (see .secret in app.css)."""
    return Markup('<span class="secret"><span class="v">{}</span><span class="m" aria-label="hidden">••••••</span></span>').format(
        _money(value, currency))


def _next_url(target):
    """Where a Cancel link goes: the page the form was opened from."""
    return auth._safe_next(target) if target else url_for('pages.accounts')


def _localtime(utc_text):
    """'2026-11-12T03:04:05' (UTC, as MMEX and app.db store it) -> local 'YYYY-MM-DD HH:MM'."""
    try:
        t = datetime.datetime.strptime(utc_text, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return utc_text or ''
    return t.astimezone().strftime('%Y-%m-%d %H:%M')


def create_app(settings=None):
    settings = settings or Settings.from_env()
    secrets = secrets_file.load(settings.secrets_path)
    store = Store(settings.store_path)

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets['session_key'],
        SESSION_COOKIE_NAME='mmex_web_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=settings.cookie_secure,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=settings.session_days),
        WTF_CSRF_TIME_LIMIT=None,  # the token lives as long as the session
        MAX_CONTENT_LENGTH=1024 * 1024,
        MMEX_SETTINGS=settings,
        MMEX_SECRETS=secrets,
        MMEX_THROTTLE=auth.LoginThrottle(),
        MMEX_STORE=store,
        MMEX_WRITER=Writer(settings, store),
    )
    csrf = CSRFProtect(app)
    csrf.exempt(api.bp)  # token auth, no cookies
    app.register_blueprint(api.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(entry.bp)
    app.register_blueprint(settings_page.bp)
    app.register_blueprint(rates_page.bp)
    app.register_blueprint(queue_page.bp)
    app.jinja_env.filters['money'] = _money
    app.jinja_env.filters['localtime'] = _localtime
    app.jinja_env.filters['secret'] = _secret
    assets = Assets(app.static_folder)

    @app.context_processor
    def _globals():
        return {'logged_in': auth.logged_in, 'can_write': pages.can_write, 'next_url': _next_url, 'asset': assets.url,
                'theme': settings_page.theme, 'flag_button': entry.flag_button,
                'queue_pending': lambda: store.queue_summary()['pending'] if auth.logged_in() else 0,
                'balances_hidden': lambda: auth.logged_in() and store.prefs()['hide_balances']}

    @app.get('/healthz')
    def healthz():
        # For the container healthcheck: says nothing about the data.
        return 'ok', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    @app.get('/favicon.ico')
    def favicon():
        # Browsers ask for /favicon.ico whatever the page links; no login needed.
        return send_from_directory(app.static_folder, 'favicon.ico', mimetype='image/vnd.microsoft.icon',
                                   max_age=86400)

    @app.after_request
    def _headers(response):
        response.headers.setdefault('Content-Security-Policy',
                                    "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        # same-origin, not no-referrer: Flask-WTF's CSRF check needs a same-site Referer over HTTPS
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        if response.mimetype == 'text/html':
            response.headers['Cache-Control'] = 'no-store'
        elif request.endpoint == 'static':
            # A URL carrying the file's current hash never changes content,
            # so it may be cached for good; anything else (e.g. the icon the
            # manifest names) must be revalidated.
            path = safe_join(app.static_folder, (request.view_args or {}).get('filename', ''))
            versioned = (request.args.get('v') and path and os.path.isfile(path)
                         and request.args['v'] == assets.version(request.view_args['filename']))
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable' if versioned else 'no-cache'
        return response

    @app.errorhandler(CSRFError)
    def _csrf(exc):
        return render_template('error.html', code=400, message='The form could not be verified. Reload the page and try again.'), 400

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def _http_error(exc):
        return render_template('error.html', code=exc.code, message=exc.description), exc.code

    @app.errorhandler(FileNotFoundError)
    def _no_db(exc):
        return render_template('error.html', code=503, message='The MMEX database file is not available.'), 503

    if settings.trusted_proxies:
        n = settings.trusted_proxies
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n, x_port=n)
    if settings.url_prefix:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, settings.url_prefix)
    return app
