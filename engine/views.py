"""Read-only views of an MMEX DB for the web UI: accounts with balances,
one account's register with a running balance, and recently changed rows.

Balance rules are the same as core.balance(): the account's INITIALBAL,
plus every row that isn't Void or deleted. A transfer counts against its
source account (TRANSAMOUNT) and for its target account (TOTRANSAMOUNT).
Deleted rows never appear. Void rows appear in the register but don't move
the running balance.
"""

import datetime
import sqlite3

from .core import TRANS, fetch_row, friendly

SIGNED = "CASE WHEN TRANSCODE = 'Deposit' THEN TRANSAMOUNT ELSE -TRANSAMOUNT END"
NOT_DELETED = "(DELETEDTIME IS NULL OR DELETEDTIME = '')"
STATUS_FILTERS = {
    'all': None,
    'unreconciled': lambda s: s != 'R' and s != 'V',
    'blank': lambda s: s == '',
    'R': lambda s: s == 'R',
    'F': lambda s: s == 'F',
    'V': lambda s: s == 'V',
}


def currencies(conn) -> dict:
    out = {}
    for cid, pfx, sfx, scale, symbol in conn.execute(
            'SELECT CURRENCYID, PFX_SYMBOL, SFX_SYMBOL, SCALE, CURRENCY_SYMBOL FROM CURRENCYFORMATS_V1'):
        scale = scale or 100
        out[cid] = {'id': cid, 'prefix': pfx or '', 'suffix': sfx or '', 'symbol': symbol or '',
                    'decimals': max(0, len(str(int(scale))) - 1)}
    return out


def _info(conn, name):
    row = conn.execute('SELECT INFOVALUE FROM INFOTABLE_V1 WHERE INFONAME = ?', (name,)).fetchone()
    return row[0] if row else None


def base_currency(conn):
    """MMEX's base currency id (INFOTABLE BASECURRENCYID), or None."""
    try:
        return int(_info(conn, 'BASECURRENCYID'))
    except (TypeError, ValueError):
        return None


def base_rates(conn, today=None) -> dict:
    """{currency id: rate to the base currency}, the way MMEX's home page
    converts balances: with USECURRENCYHISTORY on, the CURRENCYHISTORY_V1
    rate nearest to today (the latest one up to today, else the earliest
    after); otherwise, or with no history, CURRENCYFORMATS_V1.BASECONVRATE.
    The base currency is always 1."""
    today = today or datetime.date.today().isoformat()
    rates = {}
    try:
        for cid, rate in conn.execute('SELECT CURRENCYID, BASECONVRATE FROM CURRENCYFORMATS_V1'):
            rates[cid] = rate if rate else 1
    except sqlite3.OperationalError:
        pass
    if (_info(conn, 'USECURRENCYHISTORY') or '').upper() == 'TRUE':
        try:
            for cid, rate in conn.execute(
                    'SELECT h.CURRENCYID, h.CURRVALUE FROM CURRENCYHISTORY_V1 h WHERE h.CURRDATE = COALESCE('
                    '(SELECT MAX(CURRDATE) FROM CURRENCYHISTORY_V1 WHERE CURRENCYID = h.CURRENCYID AND CURRDATE <= ?), '
                    '(SELECT MIN(CURRDATE) FROM CURRENCYHISTORY_V1 WHERE CURRENCYID = h.CURRENCYID))', (today,)):
                if rate:
                    rates[cid] = rate
        except sqlite3.OperationalError:
            pass
    base = base_currency(conn)
    if base is not None:
        rates[base] = 1
    return rates


def accounts(conn, ref, include_closed=False, until=None) -> list:
    """Every account (open only, unless include_closed) with its current
    balance and its reconciled balance (initial plus R rows only), each also
    in the base currency (base_balance, base_reconciled; unrounded). With
    until (YYYY-MM-DD), rows dated after it are left out, as MMEX's home
    page does with "ignore future transactions"."""
    upto = "AND substr(TRANSDATE, 1, 10) <= ?" if until else ''
    args = (until,) if until else ()
    sums = {}
    for acct, total, rec in conn.execute(
            f"SELECT ACCOUNTID, SUM({SIGNED}), SUM(CASE WHEN STATUS = 'R' THEN {SIGNED} ELSE 0 END) "
            f"FROM {TRANS} WHERE STATUS <> 'V' AND {NOT_DELETED} {upto} GROUP BY ACCOUNTID", args):
        sums[acct] = [total or 0, rec or 0]
    for acct, total, rec in conn.execute(
            "SELECT TOACCOUNTID, SUM(TOTRANSAMOUNT), SUM(CASE WHEN STATUS = 'R' THEN TOTRANSAMOUNT ELSE 0 END) "
            f"FROM {TRANS} WHERE TRANSCODE = 'Transfer' AND STATUS <> 'V' AND {NOT_DELETED} {upto} GROUP BY TOACCOUNTID",
            args):
        s = sums.setdefault(acct, [0, 0])
        s[0] += total or 0
        s[1] += rec or 0
    meta = {r[0]: {'type': r[1], 'favorite': (r[2] or '').upper() == 'TRUE'}
            for r in conn.execute('SELECT ACCOUNTID, ACCOUNTTYPE, FAVORITEACCT FROM ACCOUNTLIST_V1')}
    rates = base_rates(conn)
    out = []
    for a in ref.accounts.values():
        if a['status'] != 'Open' and not include_closed:
            continue
        total, rec = sums.get(a['id'], [0, 0])
        rate = rates.get(a['currency'], 1)
        out.append({'id': a['id'], 'name': a['name'], 'status': a['status'], 'currency': a['currency'],
                    'type': meta[a['id']]['type'], 'favorite': meta[a['id']]['favorite'],
                    'balance': round(a['initial'] + total, 2), 'reconciled': round(a['initial'] + rec, 2),
                    'base_balance': (a['initial'] + total) * rate, 'base_reconciled': (a['initial'] + rec) * rate})
    return sorted(out, key=lambda a: (a['type'] or '', a['name'].lower()))


def _tags_for(conn, ref, ids) -> dict:
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for ref_id, tag_id in conn.execute(
                f"SELECT REFID, TAGID FROM TAGLINK_V1 WHERE REFTYPE = 'Transaction' AND REFID IN ({','.join('?' * len(chunk))})",
                chunk):
            if tag_id in ref.tags:
                out.setdefault(ref_id, []).append(ref.tags[tag_id]['name'])
    return {k: sorted(v) for k, v in out.items()}


def _row_view(ref, account_id, r) -> dict:
    trans_id, acct, to_acct, payee_id, code, amount, to_amount, status, number, notes, categ, tdate = r
    status = status or ''
    if code == 'Transfer' and to_acct == account_id and acct != account_id:
        kind, signed, other = 'Transfer in', to_amount or 0, f'← {ref.account_name(acct)}'
    elif code == 'Transfer':
        kind, signed, other = 'Transfer out', -(amount or 0), f'→ {ref.account_name(to_acct)}'
    elif code == 'Deposit':
        kind, signed, other = 'Deposit', amount or 0, ref.payee_name(payee_id)
    else:
        kind, signed, other = 'Withdrawal', -(amount or 0), ref.payee_name(payee_id)
    return {'id': trans_id, 'date': (tdate or '')[:10], 'kind': kind, 'type': code, 'other': other,
            'category': ref.category_path(categ), 'notes': notes or '', 'number': number or '',
            'status': status, 'amount': round(signed, 2), 'raw': signed, 'void': status == 'V'}


def register(conn, ref, account_id, status='all', date_from=None, date_to=None, text=None,
             limit=100, offset=0, newest_first=True) -> dict:
    """One account's rows with a running balance.

    The running balance always runs over every row of the account, in MMEX's
    order (date, then ID); filters only choose which rows are shown. Returns
    {'rows', 'total' (rows matching the filters), 'balance', 'reconciled'}."""
    if account_id not in ref.accounts:
        raise KeyError(account_id)
    if status not in STATUS_FILTERS:
        raise ValueError(f'status must be one of {sorted(STATUS_FILTERS)}')
    rows = conn.execute(
        'SELECT TRANSID, ACCOUNTID, TOACCOUNTID, PAYEEID, TRANSCODE, TRANSAMOUNT, TOTRANSAMOUNT, STATUS, '
        f'TRANSACTIONNUMBER, NOTES, CATEGID, TRANSDATE FROM {TRANS} '
        f"WHERE (ACCOUNTID = ? OR (TRANSCODE = 'Transfer' AND TOACCOUNTID = ?)) AND {NOT_DELETED} "
        'ORDER BY TRANSDATE, TRANSID', (account_id, account_id)).fetchall()
    # Sum the stored values and round only what's shown: old cross-currency
    # rows hold unrounded amounts (e.g. 0.7503...), and rounding each row
    # first drifts from MMEX's own totals by a cent here and there.
    running = reconciled = ref.accounts[account_id]['initial'] or 0
    views = []
    for r in rows:
        v = _row_view(ref, account_id, r)
        if not v['void']:
            running += v['raw']
            if v['status'] == 'R':
                reconciled += v['raw']
        v['balance'] = round(running, 2)
        views.append(v)

    keep = STATUS_FILTERS[status]
    needle = (text or '').strip().lower()

    def wanted(v):
        if keep and not keep(v['status']):
            return False
        if date_from and v['date'] < date_from:
            return False
        if date_to and v['date'] > date_to:
            return False
        if needle and needle not in ' '.join((v['other'], v['category'], v['notes'], v['number'])).lower():
            return False
        return True

    shown = [v for v in views if wanted(v)]
    if newest_first:
        shown.reverse()
    page = shown[offset:offset + limit]
    tags = _tags_for(conn, ref, (v['id'] for v in page))
    for v in page:
        v['tags'] = tags.get(v['id'], [])
    return {'rows': page, 'total': len(shown), 'balance': round(running, 2), 'reconciled': round(reconciled, 2)}


def recent(conn, ref, by='added', limit=50) -> list:
    """Recently added (by ID: MMEX IDs are the creation time), edited (by
    LASTUPDATEDTIME) or deleted (by DELETEDTIME) rows, newest first, each
    shown from its own account's side."""
    if by == 'added':
        where, order = NOT_DELETED, 'TRANSID DESC'
    elif by == 'edited':
        where, order = f"{NOT_DELETED} AND LASTUPDATEDTIME IS NOT NULL AND LASTUPDATEDTIME <> ''", 'LASTUPDATEDTIME DESC, TRANSID DESC'
    elif by == 'deleted':
        where, order = "DELETEDTIME IS NOT NULL AND DELETEDTIME <> ''", 'DELETEDTIME DESC, TRANSID DESC'
    else:
        raise ValueError("by must be 'added', 'edited' or 'deleted'")
    rows = conn.execute(
        'SELECT TRANSID, ACCOUNTID, TOACCOUNTID, PAYEEID, TRANSCODE, TRANSAMOUNT, TOTRANSAMOUNT, STATUS, '
        f'TRANSACTIONNUMBER, NOTES, CATEGID, TRANSDATE, LASTUPDATEDTIME, DELETEDTIME FROM {TRANS} '
        f'WHERE {where} ORDER BY {order} LIMIT ?', (limit,)).fetchall()
    out = []
    for r in rows:
        v = _row_view(ref, r[1], r[:12])
        v.update({'account': ref.account_name(r[1]), 'account_id': r[1], 'updated': r[12] or '', 'deleted': r[13] or ''})
        out.append(v)
    tags = _tags_for(conn, ref, (v['id'] for v in out))
    for v in out:
        v['tags'] = tags.get(v['id'], [])
    return out


def picker(conn, ref) -> dict:
    """What the entry form offers, most recently used first:
    - accounts: open ones, with type, currency and favourite flag
    - payees: active ones, each with the category it was last used with
      (else the payee's own default category)
    - categories: active category paths
    - tags: active tags
    - last_account: the account of the most recently added row"""
    last_payee = {pid: (tid, categ) for pid, tid, categ in conn.execute(
        f'SELECT t.PAYEEID, t.TRANSID, t.CATEGID FROM {TRANS} t JOIN '
        f'(SELECT PAYEEID, MAX(TRANSID) AS m FROM {TRANS} WHERE PAYEEID > 0 AND {NOT_DELETED} GROUP BY PAYEEID) x '
        'ON t.TRANSID = x.m')}
    payee_default = dict(conn.execute('SELECT PAYEEID, CATEGID FROM PAYEE_V1'))
    payees = []
    for p in ref.payees.values():
        if not p['active']:
            continue
        used, categ = last_payee.get(p['id'], (0, None))
        if categ in (None, -1):
            categ = payee_default.get(p['id'])
        payees.append({'name': p['name'], 'category': ref.category_path(categ) if categ not in (None, -1) else '',
                       'used': used})
    payees.sort(key=lambda p: (-p['used'], p['name'].lower()))

    categ_used = dict(conn.execute(f'SELECT CATEGID, MAX(TRANSID) FROM {TRANS} WHERE {NOT_DELETED} GROUP BY CATEGID'))
    categories = sorted(({'path': ref.category_path(c['id']), 'used': categ_used.get(c['id'], 0)}
                         for c in ref.categories.values() if c['active']),
                        key=lambda c: (-c['used'], c['path'].lower()))

    tag_used = dict(conn.execute("SELECT TAGID, MAX(TAGLINKID) FROM TAGLINK_V1 WHERE REFTYPE = 'Transaction' GROUP BY TAGID"))
    tags = sorted(({'name': t['name'], 'used': tag_used.get(t['id'], 0)} for t in ref.tags.values() if t['active']),
                  key=lambda t: (-t['used'], t['name'].lower()))

    meta = {r[0]: {'type': r[1] or 'Other', 'favorite': (r[2] or '').upper() == 'TRUE'}
            for r in conn.execute('SELECT ACCOUNTID, ACCOUNTTYPE, FAVORITEACCT FROM ACCOUNTLIST_V1')}
    accts = sorted(({'id': a['id'], 'name': a['name'], 'currency': a['currency'], **meta.get(a['id'], {'type': 'Other', 'favorite': False})}
                    for a in ref.accounts.values() if a['status'] == 'Open'),
                   key=lambda a: (a['type'], a['name'].lower()))
    last = conn.execute(f'SELECT ACCOUNTID FROM {TRANS} WHERE {NOT_DELETED} ORDER BY TRANSID DESC LIMIT 1').fetchone()
    return {'accounts': accts, 'payees': payees, 'categories': [c['path'] for c in categories],
            'tags': [t['name'] for t in tags], 'last_account': last[0] if last else None}


def near_duplicates(conn, ref, account_id, amount, date, days=3, exclude_id=None, limit=10) -> list:
    """Rows in the account (either side of a transfer) with the same amount
    within `days` of `date`, for an "is this a duplicate?" warning. Looser
    than the engine's own duplicate refusal (same day, same payee)."""
    day = datetime.date.fromisoformat(date)
    lo, hi = (day - datetime.timedelta(days=days)).isoformat(), (day + datetime.timedelta(days=days)).isoformat()
    rows = conn.execute(
        'SELECT TRANSID, ACCOUNTID, TOACCOUNTID, PAYEEID, TRANSCODE, TRANSAMOUNT, TOTRANSAMOUNT, STATUS, '
        f'TRANSACTIONNUMBER, NOTES, CATEGID, TRANSDATE FROM {TRANS} '
        f"WHERE {NOT_DELETED} AND STATUS <> 'V' AND substr(TRANSDATE, 1, 10) BETWEEN ? AND ? AND TRANSID <> ? "
        "AND ((ACCOUNTID = ? AND ABS(TRANSAMOUNT - ?) < 0.005) "
        "  OR (TRANSCODE = 'Transfer' AND TOACCOUNTID = ? AND ABS(TOTRANSAMOUNT - ?) < 0.005)) "
        'ORDER BY TRANSDATE, TRANSID LIMIT ?',
        (lo, hi, exclude_id or 0, account_id, amount, account_id, amount, limit)).fetchall()
    return [_row_view(ref, account_id, r) for r in rows]


def transaction(conn, schema, ref, trans_id):
    """One row as plan-level fields (core.friendly), plus 'deleted'; None if
    there's no such row."""
    row = fetch_row(conn, schema, trans_id)
    if row is None:
        return None
    out = friendly(ref, conn, row)
    out['account_id'] = row['ACCOUNTID']
    out['to_account_id'] = row['TOACCOUNTID'] if row['TRANSCODE'] == 'Transfer' else None
    out['deleted'] = row['DELETEDTIME'] or ''
    return out


__all__ = ['accounts', 'base_currency', 'base_rates', 'currencies', 'near_duplicates', 'picker', 'recent', 'register', 'transaction', 'STATUS_FILTERS']
