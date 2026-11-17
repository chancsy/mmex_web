"""Reports: month-end balances, net worth (Total / Liquid / Investment),
income vs expenses, spending by category, and changes by account type.
Read-only; the figures come from engine/reports.py, the settings (ignored,
not liquid and investment accounts) from the Settings page."""

import datetime

from flask import Blueprint, abort, current_app, render_template, request

from engine import reports as R
from engine.core import CATEG_DELIM

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


def _span(first=None, all_label='All'):
    """(end month, number of months, range info for the template). ?year=Y
    picks that calendar year (to this month, for the current year). With
    first (the All range's first month, see _all_from), n=all is allowed:
    from that month."""
    current = _this_month()
    year = request.args.get('year', '').strip()
    shown_n = None
    if year:
        if not (year.isdigit() and len(year) == 4):
            abort(400, 'year must be YYYY')
        end = min(f'{year}-12', current)
        n = int(end[5:7]) if end[:4] == year else 12
    else:
        end = _month_arg('end', R.shift(current, -1))
        raw = request.args.get('n', '12')
        if first and raw == 'all':
            n, shown_n = max(1, R.months_between(first, end) + 1), 'all'
        else:
            try:
                n = int(raw)
            except ValueError:
                abort(400, 'n must be a number')
            if n not in SPANS:
                abort(400, f'n must be one of {", ".join(map(str, SPANS))}')
    spans = SPANS + (('all',) if first else ())
    return end, n, {'end': end, 'n': shown_n or n, 'year': year, 'current': current, 'spans': spans,
                    'all_label': all_label}


def _by():
    by = request.args.get('by', 'month')
    if by not in ('month', 'year'):
        abort(400, 'by must be month or year')
    return by


def _year_span(first, all_label='All'):
    """(year labels, last full month, range info) for ?by=year&n=N: the
    last N calendar years, the current one to the last full month.
    n=all starts from the year of first (see _all_from)."""
    current = _this_month()
    last = R.shift(current, -1)
    raw = request.args.get('n', '5')
    if raw == 'all':
        n = max(1, int(last[:4]) - int((first or last)[:4]) + 1)
    else:
        try:
            n = int(raw)
        except ValueError:
            abort(400, 'n must be a number')
        if n not in YEAR_SPANS:
            abort(400, f'n must be one of {", ".join(map(str, YEAR_SPANS))}, or all')
    cols = [str(y) for y in range(int(last[:4]) - n + 1, int(last[:4]) + 1)]
    return cols, last, {'by': 'year', 'n': raw if raw == 'all' else n, 'spans': YEAR_SPANS + ('all',),
                        'current': current, 'end': last, 'all_label': all_label}


def _all_from(d):
    """(first month of the All range, its label): the first month with
    data, or January of the Settings page's start year if that's later."""
    first = R.first_month(d.conn)
    year = current_app.config['MMEX_STORE'].prefs()['report_start_year']
    if year and (not first or f'{year}-01' > first):
        return f'{year}-01', f'All from {year}'
    return first, 'All'


def _years(d):
    first = R.first_month(d.conn) or _this_month()
    return list(range(int(_this_month()[:4]), int(first[:4]) - 1, -1))


def _type_key(t):
    return (TYPE_ORDER.index(t) if t in TYPE_ORDER else len(TYPE_ORDER), t)


def _common(d, **kw):
    base = d.currencies.get(R.Rates(d.conn).base)
    if kw.get('chart'):
        # How charts.js formats amounts: the base currency's prefix, suffix and decimals.
        b = base or {'prefix': '', 'suffix': '', 'decimals': 2}
        kw['chart'] = dict(kw['chart'], money={k: b[k] for k in ('prefix', 'suffix', 'decimals')})
    return dict(kw, base=base, currencies=d.currencies, banners=d.banners, pages=PAGES, years=_years(d))


# -- charts: the data charts.js draws (see the <figure class="chart"> on each page)

def _net_worth_chart(snap, monthly, lines) -> dict:
    """Total, Liquid, Investment (when any) and the Settings page's extra
    lines, one point per month-end. An extra line is its accounts'
    balances alone, added to Liquid or Total (skipping any already in it)
    or taken out of Liquid or Total (only those in it, e.g. Total minus a
    pension). Each series keeps its colour slot whichever others are drawn."""
    accts = snap['accounts']
    series = [{'name': 'Total', 'slot': 1, 'values': [r['total'] for r in monthly]},
              {'name': 'Liquid', 'slot': 2, 'values': [r['liquid'] for r in monthly]}]
    if any(abs(r['investment']) > 0.005 for r in monthly):
        series.append({'name': 'Investment', 'slot': 3, 'values': [r['investment'] for r in monthly]})
    for i, line in enumerate(lines):
        base, _, minus = line['add_to'].partition('_')
        def counted(a):
            return not a['ignored'] and (base == 'total' or (base == 'liquid' and a['liquid']))
        chosen = [accts[a] for a in line['accounts'] if a in accts]
        if minus:
            values = [r[base] - sum(a['base'][r['month']] for a in chosen if counted(a)) for r in monthly]
        else:
            mine = [a for a in chosen if not counted(a)]
            values = [sum(a['base'][r['month']] for a in mine) + (r[base] if base in ('liquid', 'total') else 0.0)
                      for r in monthly]
        series.append({'name': line['name'], 'slot': 4 + i, 'values': values})
    return {'kind': 'line', 'secret': True, 'x': [r['month'] for r in monthly], 'series': series}


def _rolling(rows, months, first, size=12) -> list:
    """Each month's average net over the `size` months ending with it,
    counting only months from `first` (the first with data) on."""
    net = {r['month']: r['net'] for r in rows}
    out = []
    for ym in months:
        window = [net.get(m, 0.0) for m in R.months(ym, size) if m >= first]
        out.append(sum(window) / len(window) if window else 0.0)
    return out


@bp.app_template_filter('ym')
def _ym(value):
    """'2026-10' -> 'Oct 26', for narrow month columns."""
    try:
        return datetime.date(int(value[:4]), int(value[5:7]), 1).strftime('%b %y')
    except (TypeError, ValueError):
        return value


@bp.app_template_filter('spark_points')
def _spark_points(values, keys=None):
    """A sparkline's polyline points in a 60x16 box, and where zero sits;
    values may be a {key: value} dict read in `keys` order (missing = 0).
    None when there's nothing to draw."""
    if keys is not None:
        values = [values.get(k, 0.0) for k in keys]
    if len(values) < 2 or all(abs(v) < 0.005 for v in values):
        return None
    lo, hi = min(min(values), 0.0), max(max(values), 0.0)
    def y(v):
        return round(15 - (v - lo) / (hi - lo) * 14, 2)
    step = 60 / (len(values) - 1)
    return {'points': ' '.join(f'{round(i * step, 2)},{y(v)}' for i, v in enumerate(values)), 'zero': y(0.0)}


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
    for key, t in totals.items():
        t['change'] = t['now'] - t['prev']
    totals['investment']['moved'] = snap['investment_transfers'][month]
    totals['investment']['change'] -= totals['investment']['moved']
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
    if _by() == 'year':
        cols, last, span = _year_span(*_all_from(d))
        # Every month from the December before the first year, so each
        # year's changes are its months' changes added up (and the chart
        # still draws every month-end).
        month_list = R.months(last, R.months_between(f'{int(cols[0]) - 1}-12', last) + 1)
    else:
        end, n, span = _span(*_all_from(d))
        span['by'] = 'month'
        month_list = R.months(end, n + 1)
    snap = R.snapshot(d.conn, month_list, config(d))
    monthly = R.net_worth(snap)
    rows = R.by_year(monthly) if span['by'] == 'year' else monthly
    chart = _net_worth_chart(snap, monthly, current_app.config['MMEX_STORE'].prefs()['chart_lines'])
    return render_template('reports_net_worth.html', rows=rows, span=span, **_common(d, chart=chart))


def _lines(d, month_list):
    cfg = config(d)
    return R.flows(d.conn, d.ref, month_list[0] + '-01', R.month_end(month_list[-1]), ignored=cfg['ignored'])


def _with_subcategories(ref, ids) -> set:
    """The category ids and every category under them."""
    ids, out = set(ids), set()
    for c in ref.categories:
        seen, cur = set(), c
        while cur is not None and cur in ref.categories and cur not in seen:
            if cur in ids:
                out.add(c)
                break
            seen.add(cur)
            cur = ref.categories[cur]['parent']
    return out


def _income_split(d, lines):
    """(lines Income vs expenses counts, lines it leaves out, left-out
    account ids, left-out category ids): the Settings page's choices, a
    category with its subcategories."""
    prefs = current_app.config['MMEX_STORE'].prefs()
    accounts = set(prefs['income_excluded_accounts'])
    cats = _with_subcategories(d.ref, prefs['income_excluded_categories'])
    kept, out = [], []
    for x in lines:
        (out if x['account'] in accounts or x['category'] in cats else kept).append(x)
    return kept, out, accounts, cats


@bp.route('/income')
@login_required
def income():
    d = db()
    end, n, span = _span()
    ms = R.months(end, n)
    wide = R.months(end, n + 11)          # 11 months more, for the first months' 12-month average
    lines, left_out, out_accounts, out_cats = _income_split(d, _lines(d, wide))
    rows = R.income_expense(lines, ms)
    total = {k: sum(r[k] for r in rows) for k in ('income', 'expenses', 'net')}
    total['rate'] = total['net'] / total['income'] if total['income'] > 0.005 else None
    average = {k: total[k] / len(rows) for k in ('income', 'expenses', 'net')}
    chart = {'kind': 'bars', 'secret': False, 'x': ms,
             'bars': {'name': 'Net saved', 'values': [r['net'] for r in rows]},
             'line': {'name': '12-month average', 'values': _rolling(R.income_expense(lines, wide), ms,
                                                                     R.first_month(d.conn) or ms[0])},
             'extra': [{'name': 'Income', 'values': [r['income'] for r in rows]},
                       {'name': 'Expenses', 'values': [r['expenses'] for r in rows]}]}
    # What the Settings page leaves out over the range shown: per account, then per top-level category.
    gone = {}
    for x in left_out:
        if x['month'] in ms:
            key = ((0, d.ref.account_name(x['account'])) if x['account'] in out_accounts
                   else (1, x['path'].split(CATEG_DELIM)[0]))
            gone[key] = gone.get(key, 0.0) + x['amount']
    excluded = [{'name': k[1], 'account': k[0] == 0, 'amount': v} for k, v in sorted(gone.items())]
    return render_template('reports_income.html', rows=rows, total=total, average=average, span=span,
                           excluded=excluded, excluding=bool(out_accounts or out_cats), **_common(d, chart=chart))


@bp.route('/categories')
@login_required
def categories():
    d = db()
    end, n, span = _span()
    ms = R.months(end, n)
    tops = R.by_category(_lines(d, ms), ms)
    total = {ym: sum(t['months'].get(ym, 0.0) for t in tops) for ym in ms}
    # Spending by top-level category over the range, largest first; past
    # ten, the rest fold into "Other".
    spend = sorted(({'name': t['name'], 'value': -t['total']} for t in tops if t['total'] < -0.005),
                   key=lambda x: -x['value'])
    if len(spend) > 10:
        spend = spend[:9] + [{'name': f'Other ({len(spend) - 9})', 'value': sum(x['value'] for x in spend[9:])}]
    chart = {'kind': 'hbar', 'secret': False, 'name': 'Spending', 'items': spend} if spend else None
    return render_template('reports_categories.html', tops=tops, months=ms, total=total, span=span,
                           **_common(d, chart=chart))


@bp.route('/accounts')
@login_required
def account_changes():
    d = db()
    by = _by()
    if by == 'year':
        cols, last, span = _year_span(*_all_from(d))
        # Each column is a year: from the previous December to this one (or
        # to the last full month, for the current year).
        points = [f'{int(cols[0]) - 1}-12'] + [min(f'{y}-12', last) for y in cols]
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
    # Each type keeps one colour (its place in TYPE_ORDER), whichever types show.
    chart = {'kind': 'stack', 'secret': True, 'x': cols, 'net': total, 'net_name': 'Net change',
             'series': [{'name': g['label'], 'slot': min(_type_key(g['type'])[0] + 1, 8), 'values': g['changes']}
                        for g in ordered]}
    return render_template('reports_accounts.html', groups=ordered, cols=cols, total=total, span=span,
                           end_total=sum(g['end'] for g in ordered), **_common(d, chart=chart))
