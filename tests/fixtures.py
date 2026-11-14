"""Test DBs built from the official MMEX v21 schema (schema/mmex_tables_v21.sql),
never from a real DB, so the tests run anywhere and touch nothing real.

legacy=True adds the columns an upgraded DB still carries (SUBCATEGID on
CHECKINGACCOUNT_V1 and PAYEE_V1), like most long-lived real DBs.
"""

import hashlib
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
OFFICIAL_SQL = os.path.join(HERE, 'schema', 'mmex_tables_v21.sql')

EUR, USD = 2, 1  # CURRENCYIDs in the official CURRENCYFORMATS_V1 rows
BANK, CASH_EUR, CASH_USD, CLOSED = 'Bank (EUR)', 'Cash (EUR)', 'Cash (USD)', 'Old Card'
ACCOUNT_IDS = {BANK: 1, CASH_EUR: 2, CASH_USD: 3, CLOSED: 4}
TRANSFER, TAKEAWAY, INTEREST, RETIRED = 1000, 1002, 1004, 1005
DINER, OLD_SHOP = 2000, 2001
TRIP, RENO = 3000, 3001
# Existing rows: (id, account, to_account, payee, code, amount, status, categ, date, to_amount)
SEED_ROWS = [
    (5001, 1, -1, DINER, 'Withdrawal', 12.5, '', TAKEAWAY, '2026-01-05', 12.5),
    (5002, 1, -1, DINER, 'Withdrawal', 8.0, 'R', TAKEAWAY, '2026-01-06', 8.0),
    (5003, 1, 2, -1, 'Transfer', 100.0, '', TRANSFER, '2026-01-07', 100.0),
    (5004, 1, -1, DINER, 'Deposit', 3.21, '', INTEREST, '2026-01-31', 3.21),
    (5005, 1, -1, DINER, 'Withdrawal', 50.0, 'V', TAKEAWAY, '2026-01-08', 50.0),
]


def build_db(path, legacy=False, version=21):
    # Built in memory, then copied to `path` in one go: executescript commits
    # every statement, which is slow on a real disk.
    conn = sqlite3.connect(':memory:')
    with open(OFFICIAL_SQL, encoding='utf-8') as fh:
        conn.executescript(fh.read())
    if legacy:
        conn.execute('ALTER TABLE CHECKINGACCOUNT_V1 ADD COLUMN SUBCATEGID integer')
        conn.execute('ALTER TABLE PAYEE_V1 ADD COLUMN SUBCATEGID integer')
    conn.execute(f'PRAGMA user_version = {version}')
    conn.executemany('INSERT INTO INFOTABLE_V1 (INFONAME, INFOVALUE) VALUES (?, ?)',
                     [('ISUSED', 'FALSE'), ('UID', 'test-db-' + ('legacy' if legacy else 'fresh'))])
    conn.executemany(
        'INSERT INTO ACCOUNTLIST_V1 (ACCOUNTID, ACCOUNTNAME, ACCOUNTTYPE, STATUS, FAVORITEACCT, CURRENCYID, '
        'INITIALBAL, INITIALDATE) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        [(1, BANK, 'Checking', 'Open', 'TRUE', EUR, 1000, '2026-01-01'),
         (2, CASH_EUR, 'Cash', 'Open', 'FALSE', EUR, 0, '2026-01-01'),
         (3, CASH_USD, 'Cash', 'Open', 'FALSE', USD, 0, '2026-01-01'),
         (4, CLOSED, 'Credit Card', 'Closed', 'FALSE', EUR, 0, '2026-01-01')])
    conn.executemany('INSERT INTO CATEGORY_V1 (CATEGID, CATEGNAME, ACTIVE, PARENTID) VALUES (?, ?, ?, ?)',
                     [(TRANSFER, 'Transfer', 1, -1), (1001, 'Food', 1, -1), (TAKEAWAY, 'Takeaway', 1, 1001),
                      (1003, 'Income', 1, -1), (INTEREST, 'Interest', 1, 1003), (RETIRED, 'Retired', 0, -1)])
    conn.executemany('INSERT INTO PAYEE_V1 (PAYEEID, PAYEENAME, CATEGID, NUMBER, WEBSITE, NOTES, ACTIVE, PATTERN) '
                     "VALUES (?, ?, ?, '', '', '', ?, '')", [(DINER, 'Diner', TAKEAWAY, 1), (OLD_SHOP, 'Old Shop', -1, 0)])
    conn.executemany('INSERT INTO TAG_V1 (TAGID, TAGNAME, ACTIVE) VALUES (?, ?, 1)', [(TRIP, 'trip'), (RENO, 'reno')])
    conn.executemany(
        'INSERT INTO CHECKINGACCOUNT_V1 (TRANSID, ACCOUNTID, TOACCOUNTID, PAYEEID, TRANSCODE, TRANSAMOUNT, STATUS, '
        'TRANSACTIONNUMBER, NOTES, CATEGID, TRANSDATE, FOLLOWUPID, TOTRANSAMOUNT, LASTUPDATEDTIME, DELETEDTIME, COLOR) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, '', 'seed', ?, ?, -1, ?, '2026-01-01T00:00:00', '', -1)",
        [(i, a, t, p, c, amt, s, cat, d + 'T00:00:00', ta) for i, a, t, p, c, amt, s, cat, d, ta in SEED_ROWS])
    conn.commit()
    if os.path.exists(path):
        os.remove(path)
    disk = sqlite3.connect(path)
    conn.backup(disk)
    disk.close()
    conn.close()
    return path


def sha(path) -> str:
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def dump(path) -> dict:
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    out = {t: conn.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall()
           for t in ('CHECKINGACCOUNT_V1', 'TAGLINK_V1', 'PAYEE_V1')}
    conn.close()
    return out


def q(path, sql, *args):
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def set_info(path, name, value):
    conn = sqlite3.connect(path)
    conn.execute('UPDATE INFOTABLE_V1 SET INFOVALUE = ? WHERE INFONAME = ?', (value, name))
    conn.commit()
    conn.close()
