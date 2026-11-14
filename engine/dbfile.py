"""Opening an MMEX DB file safely, plus the file-level checks around it.

- open_rw: writable connection, schema-checked, refuses WAL mode, waits
  (busy timeout) instead of failing when another writer holds the lock.
- memory_copy: the DB copied into memory, for previews; the file on disk is
  only read, so not even a -journal file appears.
- copy_db: consistent copy via SQLite's backup API, integrity-checked,
  written to a temp name first so the target is never half-written.
- guard_warnings: a Syncthing conflict copy next to the DB, or MMEX's
  ISUSED flag set.
"""

import hashlib
import os
import sqlite3

from .schema import SchemaError, check

BUSY_TIMEOUT_MS = 10_000


def _ro_uri(db_path) -> str:
    return f'file:{os.path.abspath(db_path)}?mode=ro'


def _refuse_wal(conn, db_path):
    # Syncthing copies the .mmb alone; in WAL mode recent writes live in the
    # -wal file and would never reach the other devices.
    mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
    if str(mode).lower() == 'wal':
        raise SchemaError(f'{db_path} is in WAL journal mode; the engine only writes rollback-journal DBs')


def open_rw(db_path):
    """(connection, schema) for writing. Autocommit mode; callers BEGIN IMMEDIATE."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f'No such DB: {db_path}')
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}')
        _refuse_wal(conn, db_path)
        schema = check(conn)
    except Exception:
        conn.close()
        raise
    return conn, schema


def open_ro(db_path):
    """(connection, schema) that can only read the file on disk."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f'No such DB: {db_path}')
    conn = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        schema = check(conn)
    except Exception:
        conn.close()
        raise
    return conn, schema


def memory_copy(db_path):
    """(in-memory connection, schema): a private copy to run previews on."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f'No such DB: {db_path}')
    src = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    mem = sqlite3.connect(':memory:', isolation_level=None)
    try:
        src.backup(mem)
    finally:
        src.close()
    try:
        schema = check(mem)
    except Exception:
        mem.close()
        raise
    return mem, schema


def copy_db(src_path, dst_path):
    """Consistent copy via the sqlite backup API (safe while MMEX has the
    source open), written to a temp file first so dst is never half-written."""
    tmp = dst_path + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    src = sqlite3.connect(_ro_uri(src_path), uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        result = dst.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        dst.close()
        src.close()
    if result != 'ok':
        os.remove(tmp)
        raise OSError(f'Copy of {src_path} failed integrity_check: {result}')
    os.replace(tmp, dst_path)


def sync_conflicts(db_path) -> list:
    """Syncthing's own conflict copies sit next to the file as
    <stem>.sync-conflict-<date>-<time>-<device>.<ext>."""
    stem = os.path.splitext(os.path.basename(db_path))[0]
    folder = os.path.dirname(os.path.abspath(db_path))
    return sorted(f for f in os.listdir(folder) if f.startswith(stem + '.sync-conflict-'))


def info_value(conn, name):
    row = conn.execute('SELECT INFOVALUE FROM INFOTABLE_V1 WHERE INFONAME = ?', (name,)).fetchone()
    return row[0] if row else None


def in_use(db_path) -> bool:
    """MMEX sets ISUSED to TRUE when it opens a DB and back to FALSE on a
    clean close."""
    conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    try:
        value = info_value(conn, 'ISUSED')
    finally:
        conn.close()
    return bool(value) and value.upper() == 'TRUE'


def guard_warnings(db_path) -> dict:
    """{'conflict': [...files], 'in_use': bool}, each falsy when clear."""
    return {'conflict': sync_conflicts(db_path), 'in_use': in_use(db_path)}


def fingerprint(conn) -> str:
    """A stable identity for the DB, independent of its path: a hash of
    MMEX's own INFOTABLE UID (falls back to CREATEDATE). Lets a journal or
    queue entry refuse to be applied to a different DB. The UID itself is
    never stored or shown."""
    uid = info_value(conn, 'UID') or info_value(conn, 'CREATEDATE') or ''
    return hashlib.sha256(('mmex-db:' + uid).encode()).hexdigest()[:16]
