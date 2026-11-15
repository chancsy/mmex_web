"""Reports: month-end balances, net worth (Total / Liquid / Investment),
income vs expenses, spending by category, and changes by account type.
Read-only; the figures come from engine/reports.py, the settings (ignored,
not liquid and investment accounts) from the Settings page."""

import datetime

from flask import Blueprint, abort, current_app, render_template, request

from engine import reports as R

from .auth import login_required
from .pages import TYPE_LABELS, TYPE_ORDER, db
from .store import report_config

bp = Blueprint('reports', __name__, url_prefix='/reports')
SPANS = (3, 6, 12, 24, 36)
YEAR_SPANS = (3, 5, 10)
PAGES = [('reports.month_end', 'Month-end balances', 'Every account at a month-end, and its change from the month before.'),
         ('reports.net_worth', 'Net worth', 'Total, Liquid and Investment each month, with their changes and the part from exchange rates.'),
         ('reports.income', 'Income vs expenses', 'Each month\'s income, expenses, net and savings rate.'),
         ('reports.categories', 'By category', 'Income and spending by category, month by month.'),
         ('reports.account_changes', 'Account changes', 'How each account type changed, by month or by year.')]


def config(d) -> dict:
    types = {r[0]: r[1] for r in d.conn.execute('SELECT ACCOUNTID, ACCOUNTTYPE FROM ACCOUNTLIST_V1')}
    return report_config(current_app.config['MMEX_STORE'].prefs(), types)


def _this_month(today=None) -> str:
    return (today or datetime.date.today()).strftime('%Y-%m')


def _month_arg(name, default):
    value = request.args.get(name, '').strip() or default
    if not R.valid_month(value):
        abort(400, f'{name} must be a month (YYYY-MM)')
    return min(value, _this_month())


def _span():
    """(end month, number of months, range info for the template). ?year=Y
    picks that calendar year (to this month, for the current year)."""
    current = _this_month()
    year = request.args.get('year', '').strip()
    if year:
        if not (year.isdigit() and len(year) == 4):
            abort(400, 'year must be YYYY')
        end = min(f'{year}-12', current)
        n = int(end[5:7]) if end[:4] == year else 12
    else:
        end = _month_arg('end', R.shift(current, -1))
        try:
            n = int(request.args.get('n', '12'))
        except ValueError:
            abort(400, 'n must be a number')
        if n not in SPANS:
            abort(400, f'n must be one of {", ".join(map(str, SPANS))}')
    return end, n, {'end': end, 'n': n, 'year': year, 'current': current, 'spans': SPANS}


def _years(d):
    first = R.first_month(d.conn) or _this_month()
    return list(range(int(_this_month()[:4]), int(first[:4]) - 1, -1))


def _type_key(t):
    return (TYPE_ORDER.index(t) if t in TYPE_ORDER else len(TYPE_ORDER), t)


def _common(d, **kw):
    base = d.currencies.get(R.Rates(d.conn).base)
    return dict(kw, base=base, currencies=d.currencies, banners=d.banners, pages=PAGES, years=_years(d))


@bp.app_template_filter('ym')
def _ym(value):
    """'2026-10' -> 'Oct 26', for narrow month columns."""
    try:
        return datetime.date(int(value[:4]), int(value[5:7]), 1).strftime('%b %y')
    except (TypeError, ValueError):
        return value


@bp.route('/')
@login_required
def index():
    d = db()
    return render_template('reports_index.html', **_common(d))


@bp.route('/month-end')
@login_required
def month_end():
    d = db()
    month = _month_arg('month', R.shift(_this_month(), -1))
    prev = R.shift(month, -1)
    snap = R.snapshot(d.conn, [prev, month], config(d))
    groups, ignored = {}, []
    totals = {k: {'now': 0.0, 'prev': 0.0} for k in ('total', 'liquid', 'investment')}
    for a in snap['accounts'].values():
        now, before = a['own'][month], a['own'][prev]
        if a['status'] != 'Open' and abs(now) < 0.005 and abs(before) < 0.005:
            continue
        row = dict(a, now=now, change=round(now - before, 2), base_now=a['base'][month],
                   base_change=a['base'][month] - a['base'][prev])
        if a['ignored']:
            ignored.append(row)
            continue
        g = groups.setdefault(a['type'], {'type': a['type'], 'label': TYPE_LABELS.get(a['type'], a['type']),
                                          'rows': [], 'total': 0.0, 'change': 0.0})
        g['rows'].append(row)
        g['total'] += row['base_now']
        g['change'] += row['base_change']
        for key, on in (('total', True), ('liquid', a['liquid']), ('investment', a['investment'])):
            if on:
                totals[key]['now'] += a['base'][month]
                totals[key]['prev'] += a['base'][prev]
    for g in groups.values():
        g['rows'].sort(key=lambda r: r['name'].lower())
    ignored.sort(key=lambda r: r['name'].lower())
    return render_template('reports_month_end.html', month=month, prev=prev, next=R.shift(month, 1),
                           current=_this_month(), groups=sorted(groups.values(), key=lambda g: _type_key(g['type'])),
                           ignored=ignored, totals=totals, unlinked=snap['unlinked'], **_common(d))


@bp.route('/net-worth')
@login_required
def net_worth():
    d = db()
    end, n, span = _span()
    snap = R.snapshot(d.conn, R.months(end, n + 1), config(d))
    rows = R.net_worth(snap)
    return render_template('reports_net_worth.html', rows=rows, span=span, **_common(d))


def _lines(d, month_list):
    cfg = config(d)
    return R.flows(d.conn, d.ref, month_list[0] + '-01', R.month_end(month_list[-1]), ignored=cfg['ignored'])


@bp.route('/income')
@login_required
def income():
    d = db()
    end, n, span = _span()
    ms = R.months(end, n)
    rows = R.income_expense(_lines(d, ms), ms)
    total = {k: sum(r[k] for r in rows) for k in ('income', 'expenses', 'net')}
    total['rate'] = total['net'] / total['income'] if total['income'] > 0.005 else None
    average = {k: total[k] / len(rows) for k in ('income', 'expenses', 'net')}
    return render_template('reports_income.html', rows=rows, total=total, average=average, span=span, **_common(d))


@bp.route('/categories')
@login_required
def categories():
    d = db()
    end, n, span = _span()
    ms = R.months(end, n)
    tops = R.by_category(_lines(d, ms), ms)
    total = {ym: sum(t['months'].get(ym, 0.0) for t in tops) for ym in ms}
    return render_template('reports_categories.html', tops=tops, months=ms, total=total, span=span, **_common(d))


@bp.route('/accounts')
@login_required
def account_changes():
    d = db()
    by = request.args.get('by', 'month')
    if by not in ('month', 'year'):
        abort(400, 'by must be month or year')
    current = _this_month()
    if by == 'year':
        try:
            n = int(request.args.get('n', '5'))
        except ValueError:
            abort(400, 'n must be a number')
        if n not in YEAR_SPANS:
            abort(400, f'n must be one of {", ".join(map(str, YEAR_SPANS))}')
        last = R.shift(current, -1)
        last_year = int(last[:4])
        # Each column is a year: from the previous December to this one (or
        # to the last full month, for the current year).
        cols = [str(y) for y in range(last_year - n + 1, last_year + 1)]
        points = [f'{int(cols[0]) - 1}-12'] + [min(f'{y}-12', last) for y in cols]
        span = {'by': by, 'n': n, 'spans': YEAR_SPANS, 'current': current, 'end': last}
    else:
        end, n, span = _span()
        points = R.months(end, n + 1)
        cols = points[1:]
        span['by'] = by
    snap = R.snapshot(d.conn, points, config(d))
    groups = {}
    for a in snap['accounts'].values():
        if a['ignored']:
            continue
        changes = [a['base'][b] - a['base'][p] for p, b in zip(points, points[1:])]
        if all(abs(c) < 0.005 for c in changes) and abs(a['base'][points[-1]]) < 0.005:
            continue
        g = groups.setdefault(a['type'], {'type': a['type'], 'label': TYPE_LABELS.get(a['type'], a['type']),
                                          'rows': [], 'changes': [0.0] * len(cols), 'end': 0.0})
        g['rows'].append({'id': a['id'], 'name': a['name'], 'changes': changes, 'end': a['base'][points[-1]]})
        g['changes'] = [x + y for x, y in zip(g['changes'], changes)]
        g['end'] += a['base'][points[-1]]
    ordered = sorted(groups.values(), key=lambda g: _type_key(g['type']))
    for g in ordered:
        g['rows'].sort(key=lambda r: r['name'].lower())
    total = [sum(g['changes'][i] for g in ordered) for i in range(len(cols))]
    return render_template('reports_accounts.html', groups=ordered, cols=cols, total=total, span=span,
                           end_total=sum(g['end'] for g in ordered), **_common(d))
