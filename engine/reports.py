"""Monthly reports from an MMEX DB: month-end balances, net worth (Total,
Liquid, Investment), income vs expenses, and spending by category.

Read-only, standard library only. Pure functions of a connection plus a
report config; the web pages lay the results out.

Rules:
- A month-end balance is core.balance()'s: INITIALBAL plus every row that
  isn't Void or deleted, transfers in included, up to the month's last day.
- An account linked to an MMEX asset (ASSETS_V1) is worth its balance times
  that asset's change since its start date, by MMEX's own formula:
  exp(+-rate / 36500 * days) for Appreciates / Depreciates, 1 for None.
- Converting to the base currency: a month-end balance at that day's rate,
  an income or expense row at the rate on its own date. The rate on a date
  is MMEX's: with currency history on, the latest CURRENCYHISTORY_V1 rate
  on or before it (else the earliest after); otherwise, or with no
  history, CURRENCYFORMATS_V1.BASECONVRATE. The base currency is 1.
- Income is Deposits and expenses Withdrawals; transfers never count. A
  split row counts under its split categories.
- Config (all sets of account ids):
    ignored     left out of everything (a separate "wallet")
    nonliquid   in Total, not in Liquid
    investment  in Investment (a subset of Liquid)
    assets      {account id: ASSETS_V1 id} for asset-valued accounts
"""

import bisect
import calendar
import datetime
import math
import sqlite3

from .core import TRANS

LIVE = "STATUS <> 'V' AND (DELETEDTIME IS NULL OR DELETEDTIME = '')"
SIGNED = "CASE WHEN TRANSCODE = 'Deposit' THEN TRANSAMOUNT ELSE -TRANSAMOUNT END"
NO_CATEGORY = '(no category)'


# -- months --------------------------------------------------------------------

def month_end(ym) -> str:
    """'2026-02' -> '2026-02-28'."""
    y, m = int(ym[:4]), int(ym[5:7])
    return f'{ym}-{calendar.monthrange(y, m)[1]:02d}'


def shift(ym, n) -> str:
    """The month n months after ym (n may be negative)."""
    i = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + n
    return f'{i // 12:04d}-{i % 12 + 1:02d}'


def months(end, n) -> list:
    """The n months ending with `end`, oldest first."""
    return [shift(end, i) for i in range(1 - n, 1)]


def valid_month(value) -> bool:
    try:
        datetime.date(int(value[:4]), int(value[5:7]), 1)
        return len(value) == 7 and value[4] == '-'
    except (TypeError, ValueError):
        return False


def first_month(conn):
    """The month of the earliest live row, or None."""
    row = conn.execute(f'SELECT MIN(substr(TRANSDATE, 1, 7)) FROM {TRANS} WHERE {LIVE}').fetchone()
    return row[0] if row and row[0] else None


# -- rates ---------------------------------------------------------------------

def _info(conn, name):
    row = conn.execute('SELECT INFOVALUE FROM INFOTABLE_V1 WHERE INFONAME = ?', (name,)).fetchone()
    return row[0] if row else None


class Rates:
    """The rate to the base currency for a currency on a date, MMEX's way
    (see the module notes). Loaded once; lookups are a bisect."""

    def __init__(self, conn):
        try:
            self.base = int(_info(conn, 'BASECURRENCYID'))
        except (TypeError, ValueError):
            self.base = None
        self.fixed = {}
        try:
            for cid, rate in conn.execute('SELECT CURRENCYID, BASECONVRATE FROM CURRENCYFORMATS_V1'):
                self.fixed[cid] = rate if rate else 1
        except sqlite3.OperationalError:
            pass
        self.history = {}
        if (_info(conn, 'USECURRENCYHISTORY') or '').upper() == 'TRUE':
            try:
                for cid, date, rate in conn.execute(
                        'SELECT CURRENCYID, substr(CURRDATE, 1, 10), CURRVALUE FROM CURRENCYHISTORY_V1 '
                        'WHERE CURRVALUE IS NOT NULL AND CURRVALUE <> 0 ORDER BY CURRENCYID, CURRDATE'):
                    dates, values = self.history.setdefault(cid, ([], []))
                    dates.append(date)
                    values.append(rate)
            except sqlite3.OperationalError:
                pass

    def on(self, currency_id, date) -> float:
        if currency_id == self.base:
            return 1.0
        found = self.history.get(currency_id)
        if found:
            dates, values = found
            i = bisect.bisect_right(dates, date)
            return values[i - 1] if i else values[0]
        return self.fixed.get(currency_id, 1.0)


# -- accounts and assets -------------------------------------------------------

def accounts(conn) -> dict:
    """{id: {id, name, type, status, currency, initial}} for every account."""
    return {r[0]: {'id': r[0], 'name': r[1], 'type': r[2] or 'Other', 'status': r[3], 'currency': r[4],
                   'initial': r[5] or 0}
            for r in conn.execute('SELECT ACCOUNTID, ACCOUNTNAME, ACCOUNTTYPE, STATUS, CURRENCYID, INITIALBAL '
                                  'FROM ACCOUNTLIST_V1')}


def assets(conn) -> dict:
    """{id: {id, name, start, change, rate}} from MMEX's Assets list."""
    try:
        rows = conn.execute('SELECT ASSETID, ASSETNAME, STARTDATE, VALUECHANGE, VALUECHANGERATE FROM ASSETS_V1 '
                            'ORDER BY ASSETNAME').fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: {'id': r[0], 'name': r[1], 'start': (r[2] or '')[:10], 'change': r[3] or 'None',
                   'rate': float(r[4] or 0)} for r in rows}


def asset_factor(asset, date) -> float:
    """How much an asset's value has changed from its start date to `date`,
    as MMEX computes it (AssetModel::get_data_value_date): continuous daily
    compounding of the yearly percentage. 1 before the start date."""
    try:
        days = (datetime.date.fromisoformat(date) - datetime.date.fromisoformat(asset['start'])).days
    except ValueError:
        return 1.0
    if days <= 0:
        return 1.0
    daily = asset['rate'] / 36500.0
    if asset['change'] == 'Appreciates':
        return math.exp(daily * days)
    if asset['change'] == 'Depreciates':
        return math.exp(-daily * days)
    return 1.0


# -- balances ------------------------------------------------------------------

def _flows(conn) -> dict:
    """{account id: [(month, net change), ...]} oldest first."""
    out = {}
    for acct, ym, amount in conn.execute(
            f'SELECT ACCOUNTID, substr(TRANSDATE, 1, 7), SUM({SIGNED}) FROM {TRANS} WHERE {LIVE} GROUP BY 1, 2'):
        out.setdefault(acct, {})[ym] = amount or 0
    for acct, ym, amount in conn.execute(
            f"SELECT TOACCOUNTID, substr(TRANSDATE, 1, 7), SUM(TOTRANSAMOUNT) FROM {TRANS} "
            f"WHERE TRANSCODE = 'Transfer' AND {LIVE} GROUP BY 1, 2"):
        d = out.setdefault(acct, {})
        d[ym] = d.get(ym, 0) + (amount or 0)
    return {a: sorted(d.items()) for a, d in out.items()}


def balances(conn, month_list, accts=None) -> dict:
    """{account id: {month: balance in its own currency}} at each month's
    end, for every account. month_list must be oldest first."""
    accts = accts if accts is not None else accounts(conn)
    flows = _flows(conn)
    out = {}
    for aid, a in accts.items():
        seq, i, running = flows.get(aid, []), 0, a['initial']
        row = {}
        for ym in month_list:
            while i < len(seq) and seq[i][0] <= ym:
                running += seq[i][1]
                i += 1
            row[ym] = round(running, 2)
        out[aid] = row
    return out


def snapshot(conn, month_list, config) -> dict:
    """Every account at each month-end: {'accounts': {id: account dict plus
    'own': {month: value}, 'base': {month: value}, 'rate': {month: rate},
    'ignored', 'liquid', 'investment', 'asset'}}, 'months', 'base'
    (currency id)}. 'own' is the asset-valued figure where one applies."""
    accts = accounts(conn)
    rates = Rates(conn)
    found_assets = assets(conn)
    bal = balances(conn, month_list, accts)
    ignored = set(config.get('ignored') or ())
    nonliquid = set(config.get('nonliquid') or ())
    investment = set(config.get('investment') or ())
    asset_map = {int(k): v for k, v in (config.get('assets') or {}).items()}
    for aid, a in accts.items():
        asset = found_assets.get(asset_map.get(aid))
        own, base, rate = {}, {}, {}
        for ym in month_list:
            day = month_end(ym)
            value = bal[aid][ym] * (asset_factor(asset, day) if asset else 1.0)
            rate[ym] = rates.on(a['currency'], day)
            own[ym] = round(value, 2)
            base[ym] = value * rate[ym]
        a.update(own=own, base=base, rate=rate, ignored=aid in ignored, liquid=aid not in nonliquid,
                 investment=aid in investment and aid not in nonliquid, asset=asset)
    return {'accounts': accts, 'months': list(month_list), 'base': rates.base}


def net_worth(snap) -> list:
    """One dict per month after the first in the snapshot: total, liquid,
    investment, their changes (d_total, ...), and fx (how much of d_total
    came only from rate moves on foreign balances held over the month).
    The snapshot's first month is only the baseline for the changes."""
    ms = snap['months']
    counted = [a for a in snap['accounts'].values() if not a['ignored']]
    sums = {}
    for ym in ms:
        sums[ym] = {'total': sum(a['base'][ym] for a in counted),
                    'liquid': sum(a['base'][ym] for a in counted if a['liquid']),
                    'investment': sum(a['base'][ym] for a in counted if a['investment'])}
    out = []
    for prev, ym in zip(ms, ms[1:]):
        row = dict(month=ym, **sums[ym])
        for k in ('total', 'liquid', 'investment'):
            row['d_' + k] = sums[ym][k] - sums[prev][k]
        row['fx'] = sum(a['own'][prev] * (a['rate'][ym] - a['rate'][prev]) for a in counted
                        if a['currency'] != snap['base'])
        out.append(row)
    return out


# -- income and expenses -------------------------------------------------------

def flows(conn, ref, date_from, date_to, ignored=()) -> list:
    """Income and expense amounts between two dates (inclusive), in the
    base currency: [{month, category (id), path, kind ('income' or
    'expense'), amount (signed: income +, expense -)}], one per row or split
    line. Transfers and ignored accounts are left out."""
    rates = Rates(conn)
    ignored = set(ignored)
    currency = {r[0]: r[1] for r in conn.execute('SELECT ACCOUNTID, CURRENCYID FROM ACCOUNTLIST_V1')}
    splits = {}
    try:
        for tid, cat, amount in conn.execute('SELECT TRANSID, CATEGID, SPLITTRANSAMOUNT FROM SPLITTRANSACTIONS_V1'):
            splits.setdefault(tid, []).append((cat, amount or 0))
    except sqlite3.OperationalError:
        pass
    out = []
    for tid, acct, code, amount, cat, day in conn.execute(
            f"SELECT TRANSID, ACCOUNTID, TRANSCODE, TRANSAMOUNT, CATEGID, substr(TRANSDATE, 1, 10) FROM {TRANS} "
            f"WHERE TRANSCODE IN ('Deposit', 'Withdrawal') AND {LIVE} AND substr(TRANSDATE, 1, 10) BETWEEN ? AND ?",
            (date_from, date_to)):
        if acct in ignored:
            continue
        rate = rates.on(currency.get(acct), day)
        sign = 1 if code == 'Deposit' else -1
        kind = 'income' if code == 'Deposit' else 'expense'
        for c, amt in splits.get(tid) or [(cat, amount or 0)]:
            out.append({'month': day[:7], 'category': c, 'path': ref.category_path(c) or NO_CATEGORY,
                        'kind': kind, 'amount': sign * amt * rate})
    return out


def income_expense(lines, month_list) -> list:
    """Per month: income, expenses (positive), net, rate (net / income, or
    None)."""
    rows = {ym: {'month': ym, 'income': 0.0, 'expenses': 0.0} for ym in month_list}
    for x in lines:
        r = rows.get(x['month'])
        if r is None:
            continue
        if x['kind'] == 'income':
            r['income'] += x['amount']
        else:
            r['expenses'] -= x['amount']
    for r in rows.values():
        r['net'] = r['income'] - r['expenses']
        r['rate'] = r['net'] / r['income'] if r['income'] > 0.005 else None
    return [rows[ym] for ym in month_list]


def by_category(lines, month_list) -> list:
    """Top-level categories, each {name, months: {month: amount}, total,
    children: [{name, months, total}]}, amounts signed (income +). A row
    in a top-level category itself is its child "(itself)" when it also
    has subcategories. Sorted: income (total > 0) first, then by name."""
    tops = {}
    for x in lines:
        if x['month'] not in month_list:
            continue
        top, _, rest = x['path'].partition(':')
        t = tops.setdefault(top, {'name': top, 'months': {}, 'total': 0.0, 'kids': {}})
        k = t['kids'].setdefault(rest, {'name': rest or '(itself)', 'months': {}, 'total': 0.0})
        for node in (t, k):
            node['months'][x['month']] = node['months'].get(x['month'], 0.0) + x['amount']
            node['total'] += x['amount']
    out = []
    for t in tops.values():
        kids = sorted(t.pop('kids').values(), key=lambda k: (k['name'] != '(itself)', k['name'].lower()))
        t['children'] = kids if len(kids) > 1 or (kids and kids[0]['name'] != '(itself)') else []
        out.append(t)
    return sorted(out, key=lambda t: (t['total'] <= 0, t['name'].lower()))
