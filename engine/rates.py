"""Currency history (CURRENCYHISTORY_V1): read, set and delete rates, and
fetch month-end rates from xe.com.

A rate is base-currency units per one unit of the currency, as MMEX keeps
it. MMEX's own history dialog rounds a typed rate to 6 decimals for every
currency (CurrencyChoiceDialog: SetAltPrecision(6)), while its online update
stores the downloaded figure unrounded. Here rates are stored at full
precision (parse_rate): xe.com's figure as given, a typed one as typed, so
they match sheets fed from the same xe.com table. round_rate is there for a
caller that wants MMEX's typed-rate rounding. Update types are MMEX's:
1 Online, 2 Manual.

Rates are a standalone table, so writes here don't go through plan ops or
the undo journal; they are still backed up and refused on unsupported DBs.
Standard library only.
"""

import calendar
import datetime
import html as htmllib
import re
import urllib.request
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import dbfile
from .core import Ids

TABLE = 'CURRENCYHISTORY_V1'
RATE_DP = 6
ONLINE, MANUAL = 1, 2
UPDATE_TYPES = {0: 'none', ONLINE: 'online', MANUAL: 'manual'}
XE_URL = 'https://www.xe.com/currencytables/?from={base}&date={date}'
USER_AGENT = 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'


class RateError(Exception):
    pass


def _decimal(value) -> Decimal:
    try:
        d = Decimal(str(value).strip().replace(',', ''))
    except InvalidOperation:
        raise RateError(f'"{value}" is not a number')
    if not d.is_finite() or d <= 0:
        raise RateError(f'a rate must be more than zero, got "{value}"')
    return d


def parse_rate(value) -> float:
    """A rate at full precision (as xe.com gives it), checked but not rounded."""
    return float(_decimal(value))


def round_rate(value) -> float:
    """Round to MMEX's 6 decimals, from the decimal text (not a binary
    float) so a value like 3.1710755 rounds the way it reads."""
    d = _decimal(value)
    q = d.quantize(Decimal(1).scaleb(-RATE_DP), rounding=ROUND_HALF_UP)
    if q <= 0:
        raise RateError(f'"{value}" rounds to zero at {RATE_DP} decimals')
    return float(q)


def month_end(year, month) -> datetime.date:
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def is_month_end(iso) -> bool:
    d = datetime.date.fromisoformat(iso)
    return d == month_end(d.year, d.month)


def base_currency(conn):
    row = conn.execute("SELECT INFOVALUE FROM INFOTABLE_V1 WHERE INFONAME = 'BASECURRENCYID'").fetchone()
    try:
        return int(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def currencies(conn, keep=()) -> list:
    """Currencies the rates page shows, base excluded: those kept in MMEX
    (used by an open account, or in `keep`, the ids the user chose), then
    any others that already have history. `active` = kept: only those get
    rates written. `account` = used by an open account."""
    base = base_currency(conn)
    accounts = {r[0] for r in conn.execute("SELECT DISTINCT CURRENCYID FROM ACCOUNTLIST_V1 WHERE STATUS = 'Open'")}
    active = accounts | set(keep)
    with_history = {r[0] for r in conn.execute(f'SELECT DISTINCT CURRENCYID FROM {TABLE}')}
    out = []
    for cid, symbol, name in conn.execute('SELECT CURRENCYID, CURRENCY_SYMBOL, CURRENCYNAME FROM CURRENCYFORMATS_V1'):
        if cid == base or cid not in active | with_history:
            continue
        out.append({'id': cid, 'symbol': symbol or str(cid), 'name': name or '', 'active': cid in active,
                    'account': cid in accounts})
    out.sort(key=lambda c: (not c['active'], c['symbol'], c['id']))
    seen = {}
    for c in out:
        seen[c['symbol']] = seen.get(c['symbol'], 0) + 1
    for c in out:
        # Two currencies can share a symbol in an old DB; tell them apart.
        c['label'] = c['symbol'] if seen[c['symbol']] == 1 else f'{c["symbol"]} (#{c["id"]})'
    return out


def all_currencies(conn) -> list:
    """Every MMEX currency but the base: [{'id', 'symbol', 'name'}] by symbol."""
    base = base_currency(conn)
    return [{'id': cid, 'symbol': symbol or str(cid), 'name': name or ''} for cid, symbol, name in conn.execute(
        'SELECT CURRENCYID, CURRENCY_SYMBOL, CURRENCYNAME FROM CURRENCYFORMATS_V1 ORDER BY CURRENCY_SYMBOL, CURRENCYID')
        if cid != base]


def history(conn) -> dict:
    """{date: {currency id: {'value', 'type'}}}, every history row."""
    out = {}
    for cid, date, value, utype in conn.execute(f'SELECT CURRENCYID, CURRDATE, CURRVALUE, CURRUPDTYPE FROM {TABLE}'):
        out.setdefault(date, {})[cid] = {'value': value, 'type': UPDATE_TYPES.get(utype, str(utype))}
    return out


def apply(db_path, changes, backups) -> list:
    """Write rate changes in one transaction, after a backup (per policy).
    Each change: {'currency_id', 'date', 'value'} with value None to delete,
    else a rate, stored as given (round typed ones first), and 'type'
    (ONLINE or MANUAL). Returns human-readable lines. Same (currency, date)
    is one row, as in MMEX."""
    conn, schema = dbfile.open_rw(db_path)
    lines = []
    try:
        if backups is not None:
            backups.take(db_path)
        conn.execute('BEGIN IMMEDIATE')
        try:
            ids = Ids()
            for ch in changes:
                cid, date = ch['currency_id'], datetime.date.fromisoformat(ch['date']).isoformat()
                found = conn.execute(f'SELECT CURRHISTID, CURRVALUE FROM {TABLE} WHERE CURRENCYID = ? AND CURRDATE = ?',
                                     (cid, date)).fetchone()
                if ch['value'] is None:
                    if found:
                        conn.execute(f'DELETE FROM {TABLE} WHERE CURRHISTID = ?', (found[0],))
                        lines.append(f'deleted {cid} {date} (was {found[1]})')
                    continue
                value, utype = parse_rate(ch['value']), ch.get('type', MANUAL)
                if found:
                    conn.execute(f'UPDATE {TABLE} SET CURRVALUE = ?, CURRUPDTYPE = ? WHERE CURRHISTID = ?',
                                 (value, utype, found[0]))
                    lines.append(f'set {cid} {date} {found[1]} -> {value}')
                else:
                    conn.execute(f'INSERT INTO {TABLE} (CURRHISTID, CURRENCYID, CURRDATE, CURRVALUE, CURRUPDTYPE) '
                                 'VALUES (?, ?, ?, ?, ?)', (ids.next(conn, TABLE, 'CURRHISTID'), cid, date, value, utype))
                    lines.append(f'added {cid} {date} {value}')
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        conn.execute('COMMIT')
        return lines
    finally:
        conn.close()


def parse_xe_rows(html) -> list:
    """Every row of an xe.com currency table page, in page order:
    [{'code', 'name', 'units_per', 'per_unit'}], numbers as the page shows
    them (commas dropped). `per_unit` is base units per one unit (what MMEX
    stores); `units_per` the other way round. Read cell by cell: some rows
    link their code and some don't, and a few codes are 4 letters (USDT)."""
    out = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S):
        cells = [htmllib.unescape(re.sub(r'<[^>]+>', '', c)).strip()
                 for c in re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', tr, re.S)]
        if (len(cells) == 4 and re.fullmatch(r'[A-Z0-9]{2,6}', cells[0])
                and re.fullmatch(r'[\d.,]+', cells[2]) and re.fullmatch(r'[\d.,]+', cells[3])):
            out.append({'code': cells[0], 'name': cells[1], 'units_per': cells[2].replace(',', ''),
                        'per_unit': cells[3].replace(',', '')})
    return out


def parse_xe_table(html) -> dict:
    """{currency code: base units per one unit, as text} from an xe.com
    currency table page."""
    return {r['code']: r['per_unit'] for r in parse_xe_rows(html)}


def fetch_xe(base_code, date, timeout=30) -> list:
    """xe.com's whole table for `base_code` on `date` (a past day), as
    parse_xe_rows gives it. One page visit, every currency."""
    if datetime.date.fromisoformat(date) >= datetime.date.today():
        raise RateError(f'{date} is not over yet; xe.com\'s table for it may still change')
    req = urllib.request.Request(XE_URL.format(base=base_code, date=date), headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode('utf-8', 'replace')
    except OSError as exc:
        raise RateError(f'could not reach xe.com: {exc}')
    rows = parse_xe_rows(html)
    if not rows:
        raise RateError('xe.com answered, but its page had no rate table (the site may have changed or blocked the request)')
    return rows
