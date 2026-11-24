"""Settings: preferences, kept in app.db (see store.PREF_DEFAULTS). Saving
them never changes the MMEX DB by itself, so they can be saved even while
the app is read-only. One exception, when the app can write: newly ticked
extra currencies get their month-end rates filled from the saved xe.com
tables (no xe.com visits)."""

from datetime import date

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

from engine import rates, reports as report_engine

from .auth import _safe_next, login_required
from .pages import TYPE_LABELS, TYPE_ORDER, can_write, db
from . import rates as rates_page
from .store import CHART_LINE_ADD_TO, CHART_LINE_MAX, PREF_DEFAULTS, report_config

bp = Blueprint('settings', __name__)


@bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    store = current_app.config['MMEX_STORE']
    d = db()
    if request.method == 'POST':
        known = set(d.ref.accounts)
        def ids(name):
            return sorted({int(v) for v in request.form.getlist(name) if v.isdigit() and int(v) in known})
        known_curr = {c['id'] for c in rates.all_currencies(d.conn)}
        keep = sorted({int(v) for v in request.form.getlist('keep_currency') if v.isdigit() and int(v) in known_curr})
        added = set(keep) - set(store.prefs()['rate_keep_currencies'])
        pattern = request.form.get('twin_notes', '').strip() or PREF_DEFAULTS['twin_notes']
        start_year = _start_year(request.form.get('report_start_year', ''), store.prefs()['report_start_year'])
        # An account can be both investment and not liquid (Investment isn't part of Liquid).
        store.set_prefs(favourites_first=bool(request.form.get('favourites_first')), hidden_accounts=ids('hidden'),
                        accounts_ignore_future=bool(request.form.get('accounts_ignore_future')),
                        hide_balances=bool(request.form.get('hide_balances')), rate_keep_currencies=keep,
                        twin_notes=pattern, report_ignored_accounts=ids('ignored'),
                        nonliquid_accounts=ids('nonliquid'), investment_accounts=ids('investment'),
                        chart_lines=_chart_lines(request.form, known), report_start_year=start_year,
                        income_excluded_accounts=ids('income_out'),
                        income_excluded_categories=sorted({int(v) for v in request.form.getlist('income_out_category')
                                                           if v.isdigit() and int(v) in d.ref.categories}))
        flash('Settings saved.', 'info')
        if added and can_write():
            n, dates = rates_page.fill_from_saved(d.conn, rates_page._base_symbol(d.conn),
                                                  rates.currencies(d.conn, keep=keep), only_ids=added)
            flash(f'Filled {n} rate(s) on {dates} month-end(s) for the newly kept currencies, from the saved xe.com tables.'
                  if n else 'No saved xe.com tables to fill the newly kept currencies from yet; Download fills them.', 'info')
        return redirect(url_for('settings.settings'))
    prefs = store.prefs()
    base = rates_page._base_symbol(d.conn)
    saved = store.xe_dates(base) if base else {}
    on_xe = set(store.xe_table(base, max(saved))['rates']) if saved else None
    with_account = {c['id'] for c in rates.currencies(d.conn) if c['account']}
    curr = [dict(c, account=c['id'] in with_account, on_xe=None if on_xe is None else c['symbol'] in on_xe)
            for c in rates.all_currencies(d.conn)]
    return render_template('settings.html', prefs=prefs, hidden=set(prefs['hidden_accounts']), base=base,
                           report=_report_settings(d, prefs),
                           default_pattern=PREF_DEFAULTS['twin_notes'],
                           currencies=curr, keep=set(prefs['rate_keep_currencies']), banners=d.banners)


def _start_year(value, old):
    """The reports' All start year from the form: empty = none (the first
    month with data); a year from 1900 to this one; anything else keeps the
    old value, with a message."""
    value = value.strip()
    if not value:
        return None
    this_year = date.today().year
    if value.isdigit() and 1900 <= int(value) <= this_year:
        return int(value)
    flash(f'The reports start year must be a year from 1900 to {this_year}; kept '
          f'{old or "the first year with data"}.', 'error')
    return old


def _chart_lines(form, known) -> list:
    """The Net worth chart's extra lines from the form's line_N_* fields;
    a line without a name is dropped (that's how one is removed)."""
    out = []
    for i in range(CHART_LINE_MAX + 1):
        name = form.get(f'line_{i}_name', '').strip()[:40]
        if not name or len(out) == CHART_LINE_MAX:
            continue
        add_to = form.get(f'line_{i}_add_to', 'none')
        accounts = sorted({int(v) for v in form.getlist(f'line_{i}_accounts') if v.isdigit() and int(v) in known})
        out.append({'name': name, 'add_to': add_to if add_to in CHART_LINE_ADD_TO else 'none', 'accounts': accounts})
    return out


def _report_settings(d, prefs) -> dict:
    """The Accounts section: every account by type (closed ones apart) and
    the current report choices (hidden ones are in prefs)."""
    accts = report_engine.accounts(d.conn)
    cfg = report_config(prefs, {a['id']: a['type'] for a in accts.values()})
    groups = {}
    for a in accts.values():
        groups.setdefault((a['status'] != 'Open', a['type']), []).append(a)
    def key(k):
        return (k[0], TYPE_ORDER.index(k[1]) if k[1] in TYPE_ORDER else len(TYPE_ORDER), k[1])
    rows = [{'closed': closed, 'label': TYPE_LABELS.get(t, t),
             'accounts': sorted(accts_, key=lambda a: a['name'].lower())}
            for (closed, t), accts_ in sorted(groups.items(), key=lambda kv: key(kv[0]))]
    # Every slot up to the maximum: the saved lines, then blank ones. With
    # script off the page shows one blank, as a new line; with script,
    # Add line shows the next blank one and Remove blanks one again.
    lines = [dict(l, names=[accts[a]['name'] for a in l['accounts'] if a in accts]) for l in prefs['chart_lines']]
    blank = [{'name': '', 'add_to': 'none', 'accounts': [], 'names': []}] * (CHART_LINE_MAX - len(lines))
    first = report_engine.first_month(d.conn)
    out_cats = set(prefs['income_excluded_categories'])
    kids = {}
    for c in d.ref.categories.values():
        kids.setdefault(c['parent'] if c['parent'] in d.ref.categories else None, []).append(c)
    def tree(parent, depth):
        for c in sorted(kids.get(parent, []), key=lambda c: c['name'].lower()):
            yield {'id': c['id'], 'name': c['name'], 'depth': depth, 'checked': c['id'] in out_cats,
                   'active': c['active'] not in (0, '0')}
            yield from tree(c['id'], depth + 1)
    return {'categories': list(tree(None, 0)), 'out_cats': [d.ref.category_path(c) for c in sorted(out_cats)
                                                              if c in d.ref.categories],
            'income_out': set(prefs['income_excluded_accounts']),'groups': rows, 'cfg': cfg, 'defaulted': prefs['nonliquid_accounts'] is None,
            'first_year': first[:4] if first else None, 'this_year': date.today().year,
            'lines': lines + blank, 'add_to': CHART_LINE_ADD_TO, 'line_max': CHART_LINE_MAX}


@bp.route('/settings/balances', methods=['POST'])
@login_required
def balances():
    """The eye toggle: hide=1 hides balances, hide=0 shows them. Saved at
    once. The page's script calls it with fetch and gets JSON; without
    script, the button's form posts here and comes back to the page."""
    hide = request.form.get('hide') == '1'
    current_app.config['MMEX_STORE'].set_prefs(hide_balances=hide)
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify(hide_balances=hide)
    return redirect(_safe_next(request.form.get('next')))


THEME_COOKIE = 'mmex_web_theme'


def theme():
    """'light' or 'dark' when chosen on this device, else None (follow the system)."""
    value = request.cookies.get(THEME_COOKIE)
    return value if value in ('light', 'dark') else None


@bp.route('/settings/theme', methods=['POST'])
def theme_toggle():
    """The light/dark button. Per device (a cookie), so a phone can stay dark
    while a PC is light; the page is drawn in it from the start, with no
    flash. The script posts the theme it switched to with fetch; without
    script the form posts and comes back to the page. No login needed: it
    only sets this cookie, and the login page has the button too."""
    value = request.form.get('theme')
    value = value if value in ('light', 'dark') else None
    response = (jsonify(theme=value) if request.headers.get('X-Requested-With') == 'fetch'
                else redirect(_safe_next(request.form.get('next'))))
    path = request.script_root or '/'
    if value:
        response.set_cookie(THEME_COOKIE, value, max_age=400 * 24 * 3600, path=path, samesite='Lax', httponly=True,
                            secure=current_app.config['MMEX_SETTINGS'].cookie_secure)
    else:
        response.delete_cookie(THEME_COOKIE, path=path)
    return response
