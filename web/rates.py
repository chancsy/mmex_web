"""The Rates page: MMEX's currency history, one column per currency, and a
page per date to download that day's rates from xe.com or edit them."""

import datetime

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from engine import rates

from .auth import login_required
from .pages import db

bp = Blueprint('rates', __name__)


def _base_symbol(conn):
    base = rates.base_currency(conn)
    row = conn.execute('SELECT CURRENCY_SYMBOL FROM CURRENCYFORMATS_V1 WHERE CURRENCYID = ?', (base,)).fetchone()
    return row[0] if row else None


def _month_ends(first, last) -> list:
    """Every month-end from first's month to last's, as ISO dates."""
    y, m = map(int, first[:7].split('-'))
    out = []
    while True:
        end = rates.month_end(y, m).isoformat()
        if end > last:
            return out
        out.append(end)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _last_month_end(today=None) -> str:
    today = today or datetime.date.today()
    return (today.replace(day=1) - datetime.timedelta(days=1)).isoformat()


def _store():
    return current_app.config['MMEX_STORE']


def _currencies(conn) -> list:
    return rates.currencies(conn, keep=_store().prefs()['rate_keep_currencies'])


@bp.route('/rates')
@login_required
def index():
    d = db()
    month = request.args.get('month', '')
    if month:
        try:
            y, m = map(int, month.split('-'))
            return redirect(url_for('rates.day', date=rates.month_end(y, m).isoformat()))
        except ValueError:
            abort(400, 'month must be YYYY-MM')
    hist = rates.history(d.conn)
    dates = set(hist) | set(_month_ends(min(hist) if hist else _last_month_end(), _last_month_end()))
    currs = _currencies(d.conn)
    active = [c['id'] for c in currs if c['active']]
    base = _base_symbol(d.conn)
    saved = _store().xe_dates(base) if base else {}
    rows = []
    for date in sorted(dates, reverse=True):
        values = hist.get(date, {})
        eom = rates.is_month_end(date)
        rows.append({'date': date, 'eom': eom, 'values': values, 'saved': date in saved,
                     'missing': eom and any(cid not in values for cid in active)})
    fillable = sum(1 for r in rows if r['missing'] and r['saved'])
    return render_template('rates.html', currencies=currs, rows=rows, base=base, fillable=fillable,
                           default_month=_last_month_end()[:7], banners=d.banners)


@bp.route('/rates/fill', methods=['POST'])
@login_required
def fill():
    """Fill every month-end that's missing a kept currency from the saved
    xe.com tables: no xe.com visits."""
    d = db()
    if d.write_block:
        abort(403, d.write_block)
    n, dates = fill_from_saved(d.conn, _base_symbol(d.conn), _currencies(d.conn))
    flash(f'Filled {n} rate(s) on {dates} date(s) from the saved xe.com tables.' if n
          else 'Nothing to fill from the saved xe.com tables.', 'info')
    return redirect(url_for('rates.index'))


def fill_from_saved(conn, base, currs, only_ids=None) -> tuple:
    """Write rates MMEX lacks for kept currencies (or just `only_ids`) on
    month-ends with a saved xe.com table. Existing rates are left alone.
    Returns (rates written, dates touched)."""
    if not base:
        return 0, 0
    hist = rates.history(conn)
    store = _store()
    changes, dates = [], set()
    for date in sorted(store.xe_dates(base)):
        if not rates.is_month_end(date):
            continue
        table = store.xe_table(base, date)['rates']
        for c in currs:
            if not c['active'] or (only_ids is not None and c['id'] not in only_ids):
                continue
            if c['id'] in hist.get(date, {}) or c['symbol'] not in table:
                continue
            changes.append({'currency_id': c['id'], 'date': date, 'value': rates.parse_rate(table[c['symbol']]),
                            'type': rates.ONLINE})
            dates.add(date)
    if changes:
        current_app.config['MMEX_WRITER'].rates(changes)
    return len(changes), len(dates)


@bp.route('/rates/<date>', methods=['GET', 'POST'])
@login_required
def day(date):
    try:
        date = datetime.date.fromisoformat(date).isoformat()
    except ValueError:
        abort(404)
    d = db()
    currs = _currencies(d.conn)
    stored = rates.history(d.conn).get(date, {})
    base = _base_symbol(d.conn)
    if request.method == 'POST':
        if d.write_block:
            abort(403, d.write_block)
        action = request.form.get('action')
        if action in ('download', 'refresh'):
            return _download(d, date, currs, stored, base, refresh=action == 'refresh')
        return _save(d, date, currs, stored)
    saved = _store().xe_table(base, date) if base else None
    return render_template('rate_day.html', date=date, eom=rates.is_month_end(date), currencies=currs, stored=stored,
                           base=base, can_download=datetime.date.fromisoformat(date) < datetime.date.today(),
                           saved=saved, banners=d.banners)


def _download(d, date, currs, stored, base, refresh=False):
    """Store one date's rates for the kept currencies, from the saved xe.com
    table, or from xe.com (one visit, the whole table saved) when there's
    none or `refresh`. A fetch() from the list (X-Requested-With: fetch)
    gets JSON to update that row in place, so the page doesn't reload and
    jump; a plain form post gets a flash and a redirect."""
    messages = _fetch_and_store(date, currs, stored, base, refresh)
    if request.headers.get('X-Requested-With') == 'fetch':
        values = rates.history(d.conn).get(date, {})    # read-only, not immutable: sees the write
        active = [c['id'] for c in currs if c['active']]
        return jsonify(
            ok=not any(level == 'error' and text.startswith('Nothing downloaded') for level, text in messages),
            messages=[{'level': level, 'text': text} for level, text in messages],
            missing=rates.is_month_end(date) and any(cid not in values for cid in active),
            values={str(cid): {'value': v['value'], 'type': v['type'], 'shown': f'{v["value"]:.6f}'}
                    for cid, v in values.items()})
    for level, text in messages:
        flash(text, level)
    if request.form.get('back') == 'list':
        return redirect(url_for('rates.index') + f'#d{date}')
    return redirect(url_for('rates.day', date=date))


def _table(base, date, refresh) -> tuple:
    """({code: per_unit}, where from): the saved table unless refreshing."""
    store = _store()
    saved = None if refresh else store.xe_table(base, date)
    if saved:
        return saved['rates'], 'the saved xe.com table'
    rows = rates.fetch_xe(base, date)
    store.save_xe_table(base, date, rows)
    return {r['code']: r['per_unit'] for r in rows}, 'xe.com'


def _fetch_and_store(date, currs, stored, base, refresh=False) -> list:
    """[(level, message)] for what happened."""
    try:
        table, source = _table(base, date, refresh)
    except rates.RateError as exc:
        return [('error', f'Nothing downloaded for {date}: {exc}.')]
    changes, notes, missing = [], [], []
    for c in currs:
        if not c['active']:
            continue
        if c['symbol'] not in table:
            missing.append(c['label'])
            continue
        # xe.com's figure as shown, unrounded, as MMEX's own online update stores it.
        new = rates.parse_rate(table[c['symbol']])
        old = stored.get(c['id'], {}).get('value')
        if old == new:
            continue
        changes.append({'currency_id': c['id'], 'date': date, 'value': new, 'type': rates.ONLINE})
        notes.append(f'{c["label"]} {new!r}' + (f' (was {old!r})' if old is not None else ''))
    out = []
    if changes:
        current_app.config['MMEX_WRITER'].rates(changes)
        verb = 'Downloaded from xe.com' if source == 'xe.com' else f'Filled from {source}'
        out.append(('info', f'{verb} for {date}: ' + ', '.join(notes) + '.'))
    else:
        out.append(('info', f'Rates for {date} from {source} match what\'s stored; nothing changed.'))
    if missing:
        out.append(('error', f'Not in xe.com\'s table: {", ".join(missing)}.'))
    return out


def _save(d, date, currs, stored):
    changes, errors = [], []
    for c in currs:
        text = request.form.get(f'rate_{c["id"]}', '').strip()
        old = stored.get(c['id'], {}).get('value')
        if old is not None and text == repr(old):
            continue  # untouched: keep the stored value as is, even if it has more than 6 decimals
        if not text:
            if old is not None:
                changes.append({'currency_id': c['id'], 'date': date, 'value': None})
            continue
        try:
            # As typed, not rounded: a figure pasted from the ledger sheet
            # (xe.com's full precision) must stay identical to it.
            new = rates.parse_rate(text)
        except rates.RateError as exc:
            errors.append(f'{c["label"]}: {exc}')
            continue
        if old != new:
            changes.append({'currency_id': c['id'], 'date': date, 'value': new, 'type': rates.MANUAL})
    if errors:
        flash('Nothing saved. ' + '; '.join(errors) + '.', 'error')
        return redirect(url_for('rates.day', date=date))
    if not changes:
        flash('No changes to save.', 'info')
        return redirect(url_for('rates.day', date=date))
    current_app.config['MMEX_WRITER'].rates(changes)
    flash(f'Saved {len(changes)} rate change(s) for {date}.', 'info')
    return redirect(url_for('rates.day', date=date))
