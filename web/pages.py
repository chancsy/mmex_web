"""Pages that only read: accounts, one account's register, recent changes.
The pages that write are in entry.py."""

import datetime
import os
import sqlite3

from flask import Blueprint, abort, current_app, g, render_template, request

from engine import core, dbfile, reports, views
from engine.schema import SchemaError, check

from .auth import login_required

bp = Blueprint('pages', __name__)
PAGE_SIZE = 100


class ReadDb:
    """A read-only connection for one request, plus anything the page should
    warn about (unsupported schema, sync conflict, MMEX in use)."""

    def __init__(self, path):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        self.conn = sqlite3.connect(f'file:{os.path.abspath(path)}?mode=ro', uri=True,
                                    timeout=dbfile.BUSY_TIMEOUT_MS / 1000)
        self.banners = []
        try:
            self.schema = check(self.conn)
        except SchemaError as exc:
            # Reading may still work; writing never will. Say so on every page.
            self.schema = None
            self.banners.append(('error', f'This database can\'t be written by mmex_web: {exc}.'))
        found = dbfile.guard_warnings(path)
        if found['conflict']:
            self.banners.append(('warn', f'Syncthing conflict copy next to the database: {", ".join(found["conflict"])}.'))
        if found['in_use']:
            self.banners.append(('warn', 'MMEX has this database open somewhere (ISUSED is set).'))
        self.ref = core.Ref(self.conn)
        self.currencies = views.currencies(self.conn)

    def close(self):
        self.conn.close()


def db() -> ReadDb:
    if 'mmex_db' not in g:
        d = ReadDb(current_app.config['MMEX_SETTINGS'].db_path)
        # Why the app can't write right now (None when it can).
        d.write_block = ('This database can\'t be written by mmex_web.' if d.schema is None
                         else current_app.config['MMEX_WRITER'].unavailable())
        if d.write_block and d.schema is not None:
            d.banners.append(('info', d.write_block))
        g.mmex_db = d
    return g.mmex_db


def can_write() -> bool:
    d = g.get('mmex_db')
    return d is not None and not d.write_block


@bp.teardown_app_request
def _close_db(exc):
    conn = g.pop('mmex_db', None)
    if conn is not None:
        conn.close()


def _date_arg(name):
    value = request.args.get(name, '').strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        abort(400, f'{name} must be a date (YYYY-MM-DD)')


# MMEX's home page order and names for account types; others follow, A-Z.
TYPE_ORDER = ['Checking', 'Credit Card', 'Cash', 'Loan', 'Term', 'Investment', 'Shares', 'Asset']
TYPE_LABELS = {'Checking': 'Bank Accounts', 'Credit Card': 'Credit Card Accounts', 'Cash': 'Cash Accounts',
               'Loan': 'Loan Accounts', 'Term': 'Term Accounts', 'Investment': 'Investment Accounts',
               'Shares': 'Share Accounts', 'Asset': 'Asset Accounts'}


def account_groups(rows, prefs) -> tuple:
    """(groups, grand total) for the accounts page.

    Each group: type, label, shown (accounts), others (accounts the user
    hid; listed under "Others"), and totals in the base currency. Totals
    count open accounts only, hidden ones included (hiding only tidies the
    page), as MMEX's home page leaves closed accounts out."""
    hidden = set(prefs['hidden_accounts'])
    favourites_first = prefs['favourites_first']
    groups = {}
    grand = {'balance': 0.0, 'reconciled': 0.0}
    for a in rows:
        t = a['type'] or 'Other'
        g = groups.setdefault(t, {'type': t, 'label': TYPE_LABELS.get(t, t), 'shown': [], 'others': [],
                                  'balance': 0.0, 'reconciled': 0.0, 'others_balance': 0.0})
        is_hidden = a['id'] in hidden
        (g['others'] if is_hidden else g['shown']).append(a)
        if a['status'] == 'Open':
            g['balance'] += a['base_balance']
            g['reconciled'] += a['base_reconciled']
            grand['balance'] += a['base_balance']
            grand['reconciled'] += a['base_reconciled']
            if is_hidden:
                g['others_balance'] += a['base_balance']

    def order(a):
        return (favourites_first and not a['favorite'], a['name'].lower())

    for g in groups.values():
        g['shown'].sort(key=order)
        g['others'].sort(key=order)
    ordered = sorted(groups.values(), key=lambda g: (TYPE_ORDER.index(g['type']) if g['type'] in TYPE_ORDER
                                                     else len(TYPE_ORDER), g['label'].lower()))
    return ordered, grand


@bp.route('/')
@login_required
def accounts():
    include_closed = request.args.get('closed') == '1'
    # "Show all in <base>": each balance converted at the rate the totals use
    # (MMEX's: the history rate nearest today, i.e. the latest month-end).
    in_base = request.args.get('base') == '1'
    d = db()
    prefs = current_app.config['MMEX_STORE'].prefs()
    until = datetime.date.today().isoformat() if prefs['accounts_ignore_future'] else None
    rows = views.accounts(d.conn, d.ref, include_closed=include_closed, until=until)
    groups, grand = account_groups(rows, prefs)
    base = d.currencies.get(views.base_currency(d.conn))
    return render_template('accounts.html', groups=groups, grand=grand, base=base, include_closed=include_closed,
                           in_base=in_base and base is not None, currencies=d.currencies, banners=d.banners,
                           assets=assets_total(d.conn))


def assets_total(conn, today=None) -> float:
    """Every asset in MMEX's Assets list at its value today, in the base
    currency, as MMEX's home page adds them up (whether or not an account
    holds them)."""
    day = today or datetime.date.today().isoformat()
    rates = reports.Rates(conn)
    return sum(reports.asset_value(x, day, rates) for x in reports.assets(conn).values())


@bp.route('/account/<int:account_id>')
@login_required
def register(account_id):
    d = db()
    if account_id not in d.ref.accounts:
        abort(404)
    status = request.args.get('status', 'all')
    if status not in views.STATUS_FILTERS:
        abort(400, 'unknown status filter')
    try:
        page = max(1, int(request.args.get('page', '1')))
    except ValueError:
        abort(400, 'page must be a number')
    date_from, date_to = _date_arg('from'), _date_arg('to')
    text = request.args.get('q', '').strip()[:100]
    reg = views.register(d.conn, d.ref, account_id, status=status, date_from=date_from, date_to=date_to,
                         text=text or None, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    pages = max(1, -(-reg['total'] // PAGE_SIZE))
    account = d.ref.accounts[account_id]
    return render_template('register.html', account=account, reg=reg, page=page, pages=pages,
                           currency=d.currencies.get(account['currency']), banners=d.banners,
                           filters={'status': status, 'from': date_from or '', 'to': date_to or '', 'q': text},
                           status_filters=list(views.STATUS_FILTERS))


@bp.route('/recent')
@login_required
def recent():
    by = request.args.get('by', 'added')
    if by not in ('added', 'edited', 'deleted'):
        abort(400, 'by must be added, edited or deleted')
    d = db()
    rows = views.recent(d.conn, d.ref, by=by, limit=100)
    sources = current_app.config['MMEX_STORE'].last_source(v['id'] for v in rows)
    for v in rows:
        v['currency'] = d.currencies.get(d.ref.accounts[v['account_id']]['currency'])
        v['source'] = sources.get(v['id'])
    return render_template('recent.html', rows=rows, by=by, banners=d.banners)
