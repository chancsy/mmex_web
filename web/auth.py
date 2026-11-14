"""Single-user login: a password checked against secrets.json, a signed
session cookie, CSRF tokens on every form (Flask-WTF), and a throttle on
failed attempts.

Changing the password (python -m web setup) logs every session out: each
session remembers a fingerprint of the hash it logged in with.
"""

import functools
import hashlib
import threading
import time
from collections import deque

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

bp = Blueprint('auth', __name__)


class LoginThrottle:
    """At most `limit` failed logins per `window` seconds, from anywhere
    (one user, so a global limit is the safe one; per-IP limits are easy to
    dodge). Once reached, every login is refused until the oldest failure
    leaves the window."""

    def __init__(self, limit=5, window=15 * 60, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.failures = deque()
        self.lock = threading.Lock()

    def _trim(self, now):
        while self.failures and now - self.failures[0] >= self.window:
            self.failures.popleft()

    def wait_seconds(self) -> int:
        with self.lock:
            now = self.clock()
            self._trim(now)
            if len(self.failures) < self.limit:
                return 0
            return int(self.window - (now - self.failures[0])) + 1

    def failed(self):
        with self.lock:
            now = self.clock()
            self._trim(now)
            self.failures.append(now)

    def succeeded(self):
        with self.lock:
            self.failures.clear()


def hash_fingerprint(password_hash) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def logged_in() -> bool:
    return session.get('owner') == hash_fingerprint(current_app.config['MMEX_SECRETS']['password_hash'])


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not logged_in():
            return redirect(url_for('auth.login', next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)
    return wrapped


def _safe_next(target) -> str:
    """`next` holds an app-relative path (request.path, without the URL
    prefix). Anything else, e.g. another host, goes to the accounts page."""
    if not target or not target.startswith('/') or target.startswith('//') or '\\' in target:
        return url_for('pages.accounts')
    return request.script_root + target


@bp.route('/login', methods=['GET', 'POST'])
def login():
    throttle = current_app.config['MMEX_THROTTLE']
    if request.method == 'POST':
        wait = throttle.wait_seconds()
        if wait:
            flash(f'Too many failed attempts. Try again in {wait // 60 + 1} min.', 'error')
            return render_template('login.html'), 429
        password = request.form.get('password', '')
        if check_password_hash(current_app.config['MMEX_SECRETS']['password_hash'], password):
            throttle.succeeded()
            session.clear()
            session.permanent = True
            session['owner'] = hash_fingerprint(current_app.config['MMEX_SECRETS']['password_hash'])
            return redirect(_safe_next(request.args.get('next')))
        throttle.failed()
        flash('Wrong password.', 'error')
        return render_template('login.html'), 401
    if logged_in():
        return redirect(url_for('pages.accounts'))
    return render_template('login.html')


@bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
