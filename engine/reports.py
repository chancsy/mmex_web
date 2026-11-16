"""Monthly reports from an MMEX DB: month-end balances, net worth (Total,
Liquid, Investment), income vs expenses, and spending by category.

Read-only, standard library only. Pure functions of a connection plus a
report config; the web pages lay the results out.

Rules:
- A month-end balance is core.balance()'s: INITIALBAL plus every row that
  isn't Void or deleted, transfers in included, up to the month's last day.
- An Asset account is worth its balance plus the value of the MMEX assets
  (ASSETS_V1) it holds, as MMEX shows it: an asset belongs to the Asset
  account named like the asset (else like its type, e.g. "Automobile").
  Other assets have no account and aren't counted.
- An asset's value on a date is MMEX's (AssetModel::get_data_value_date):
  0 before its start date. With no transactions linked to it (TRANSLINK_V1,
  LINKTYPE 'Asset'), VALUE grown from the start date; otherwise, in date
  order, each linked row adds what it paid in (Buy: a withdrawal or a
  transfer out of its account) or takes out what it received (Sell: a
  deposit), a self-transfer (Revalue) sets the value to its TOTRANSAMOUNT,
  and the value grows between rows and after the last. Growth is
  exp(+-rate / 36500 * days) for Appreciates / Depreciates, none for None.
  Void rows are skipped (MMEX marks counting them as a FIXME).
- Converting to the base currency: a month-end balance at that day's rate,
  an income or expense row at the rate on its own date. The rate on a date
  is MMEX's: with currency history on, the latest CURRENCYHISTORY_V1 rate
  on or before it (else the earliest after); otherwise, or with no
  history, CURRENCYFORMATS_V1.BASECONVRATE. The base currency is 1.
- Income is Deposits and expenses Withdrawals; transfers never count, nor
  do rows linked to an asset (buying or selling it moves money into or out
  of the asset; it isn't spending). A split row counts under its split
  categories.
- Investment's change leaves out transfers between an investment account
  and any other account (money moved in or out isn't a gain or loss); it's
  what rates and real rows (income, fees, revaluations) did. The moved
  amount is reported apart, at the investment account's month-end rate.
- Config (all sets of account ids):
    ignored     left out of everything (a separate "wallet")
    nonliquid   in Total, not in Liquid
    investment  in Investment (a subset of Liquid)
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
        try:
            self.account_currency = dict(conn.execute('SELECT ACCOUNTID, CURRENCYID FROM ACCOUNTLIST_V1'))
        except sqlite3.OperationalError:
            self.account_currency = {}
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


def _asset_links(conn) -> dict:
    """{asset id: [(date, transid, account id, code, amount, to account,
    to amount)]} for every live row linked to an asset, oldest first."""
    out = {}
    try:
        rows = conn.execute(
            f"SELECT l.LINKRECORDID, substr(t.TRANSDATE, 1, 10), t.TRANSID, t.ACCOUNTID, t.TRANSCODE, "
            f"t.TRANSAMOUNT, t.TOACCOUNTID, t.TOTRANSAMOUNT FROM TRANSLINK_V1 l "
            f"JOIN {TRANS} t ON t.TRANSID = l.CHECKINGACCOUNTID "
            f"WHERE l.LINKTYPE = 'Asset' AND {LIVE.replace('STATUS', 't.STATUS').replace('DELETEDTIME', 't.DELETEDTIME')} "
            f"ORDER BY 2, 3").fetchall()
    except sqlite3.OperationalError:
        return {}
    for aid, *row in rows:
        out.setdefault(aid, []).append(tuple(row))
    return out


def asset_linked_rows(conn) -> set:
    """TRANSIDs linked to an asset (any status)."""
    try:
        return {r[0] for r in conn.execute("SELECT CHECKINGACCOUNTID FROM TRANSLINK_V1 WHERE LINKTYPE = 'Asset'")}
    except sqlite3.OperationalError:
        return set()


def assets(conn) -> dict:
    """{id: {id, name, type, value, start, change, rate, currency (None =
    base), links}} from MMEX's Assets list; links as _asset_links()."""
    try:
        rows = conn.execute('SELECT ASSETID, ASSETNAME, ASSETTYPE, VALUE, STARTDATE, VALUECHANGE, VALUECHANGERATE, '
                            'CURRENCYID FROM ASSETS_V1 ORDER BY ASSETNAME').fetchall()
    except sqlite3.OperationalError:
        return {}
    links = _asset_links(conn)
    return {r[0]: {'id': r[0], 'name': r[1], 'type': r[2] or '', 'value': float(r[3] or 0),
                   'start': (r[4] or '')[:10], 'change': r[5] or 'None', 'rate': float(r[6] or 0),
                   'currency': r[7] if r[7] is not None and r[7] > 0 else None, 'links': links.get(r[0], [])}
            for r in rows}


def _days(a, b) -> int:
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days


def _grow(asset, value, a, b) -> float:
    """value carried from date a to date b at the asset's rate."""
    daily = asset['rate'] / 36500.0
    if asset['change'] == 'Appreciates':
        return value * math.exp(daily * _days(a, b))
    if asset['change'] == 'Depreciates':
        return value * math.exp(-daily * _days(a, b))
    return value


def asset_value(asset, date, rates=None) -> float:
    """The asset's value at the end of `date`, MMEX's way (see the module
    notes). Unlinked: in the asset's currency (converted to the base when
    `rates` is given); linked: in the base currency, as MMEX sums it."""
    try:
        if not asset['start'] or date < asset['start']:
            return 0.0
        if not asset['links']:
            value = _grow(asset, asset['value'], asset['start'], date)
            return value * rates.on(asset['currency'], date) if rates and asset['currency'] else value
        value, last = 0.0, None
        for day, _tid, acct, code, amount, to_acct, to_amount in asset['links']:
            if day > date:
                break
            if last is not None and last < day:
                value = _grow(asset, value, last, day)
            last = day
            if code == 'Transfer' and acct == to_acct:      # Revalue
                value = to_amount or 0.0
                continue
            paid = -(amount or 0) if code == 'Deposit' else (amount or 0)   # Buy +, Sell -
            value += paid * (rates.on(rates.account_currency.get(acct), day) if rates else 1.0)
        return _grow(asset, value, last, date) if last is not None else 0.0
    except ValueError:
        return 0.0


def account_assets(accts, found) -> dict:
    """{account id: [asset, ...]}: each asset in the Asset account named
    like it, else like its type (MMEX's link; one account per asset)."""
    by_name = {}
    for aid, a in accts.items():
        if a['type'] == 'Asset':
            by_name.setdefault(a['name'], aid)
    out = {}
    for asset in found.values():
        aid = by_name.get(asset['name'], by_name.get(asset['type']))
        if aid is not None:
            out.setdefault(aid, []).append(asset)
    return out


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
    'cash': {month: balance}, 'ignored', 'liquid', 'investment', 'assets'}},
    'months', 'base' (currency id), 'unlinked' (assets in no account),
    'investment_transfers' ({month: net amount moved into Investment})}.
    'own' is the balance plus the value of the assets the account holds."""
    accts = accounts(conn)
    rates = Rates(conn)
    found = assets(conn)
    held = account_assets(accts, found)
    bal = balances(conn, month_list, accts)
    ignored = set(config.get('ignored') or ())
    nonliquid = set(config.get('nonliquid') or ())
    investment = set(config.get('investment') or ())
    for aid, a in accts.items():
        mine = held.get(aid, [])
        own, base, rate = {}, {}, {}
        for ym in month_list:
            day = month_end(ym)
            rate[ym] = rates.on(a['currency'], day)
            in_base = sum(asset_value(x, day, rates) for x in mine)
            value = bal[aid][ym] + (in_base / rate[ym] if rate[ym] else 0.0)
            own[ym] = round(value, 2)
            base[ym] = value * rate[ym]
        a.update(own=own, base=base, rate=rate, cash=bal[aid], ignored=aid in ignored, liquid=aid not in nonliquid,
                 investment=aid in investment and aid not in nonliquid, assets=mine)
    linked = {x['id'] for xs in held.values() for x in xs}
    members = {aid for aid, a in accts.items() if a['investment'] and not a['ignored']}
    return {'accounts': accts, 'months': list(month_list), 'base': rates.base,
            'unlinked': [x for x in found.values() if x['id'] not in linked],
            'investment_transfers': _moved(conn, month_list, members, accts)}


def _moved(conn, month_list, members, accts) -> dict:
    """{month: base amount transferred into `members` from other accounts,
    less what went out}, each side at its account's month-end rate (as in
    the snapshot's 'rate')."""
    out = {ym: 0.0 for ym in month_list}
    if not members:
        return out
    for src, dst, amount, to_amount, ym in conn.execute(
            f"SELECT ACCOUNTID, TOACCOUNTID, TRANSAMOUNT, TOTRANSAMOUNT, substr(TRANSDATE, 1, 7) FROM {TRANS} "
            f"WHERE TRANSCODE = 'Transfer' AND {LIVE} AND substr(TRANSDATE, 1, 7) BETWEEN ? AND ?",
            (month_list[0], month_list[-1])):
        if ym not in out or (src in members) == (dst in members):
            continue
        if dst in members:
            out[ym] += (to_amount or 0) * accts[dst]['rate'][ym]
        else:
            out[ym] -= (amount or 0) * accts[src]['rate'][ym]
    return out


def net_worth(snap) -> list:
    """One dict per month after the first in the snapshot: total, liquid,
    investment, their changes (d_total, ...; d_investment without transfers
    in or out, which are in moved_investment), and fx (how much of d_total
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
        row['moved_investment'] = snap['investment_transfers'][ym]
        row['d_investment'] -= row['moved_investment']
        row['fx'] = sum(a['own'][prev] * (a['rate'][ym] - a['rate'][prev]) for a in counted
                        if a['currency'] != snap['base'])
        out.append(row)
    return out


# -- income and expenses -------------------------------------------------------

def flows(conn, ref, date_from, date_to, ignored=()) -> list:
    """Income and expense amounts between two dates (inclusive), in the
    base currency: [{month, category (id), path, kind ('income' or
    'expense'), amount (signed: income +, expense -)}], one per row or split
    line. Transfers, rows linked to an asset and ignored accounts are left
    out."""
    rates = Rates(conn)
    ignored = set(ignored)
    skip = asset_linked_rows(conn)
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
        if acct in ignored or tid in skip:
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
