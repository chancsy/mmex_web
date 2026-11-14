"""The engine's column list against the official v21 schema, and (when
MMEX_LIVE_DB names a real DB) against that DB, opened read-only.

    MMEX_LIVE_DB=/path/to/your.mmb python -m unittest tests.test_schema -v
"""

import os
import re
import sqlite3
import unittest

from engine import SUPPORTED_SCHEMA_VERSIONS
from engine.schema import LEGACY_NULL_COLUMNS, REQUIRED_COLUMNS, check, table_columns
from tests.fixtures import OFFICIAL_SQL


def official_conn():
    conn = sqlite3.connect(':memory:')
    with open(OFFICIAL_SQL, encoding='utf-8') as fh:
        conn.executescript(fh.read())
    return conn


class OfficialSchemaTests(unittest.TestCase):
    def test_header_names_the_pinned_source(self):
        with open(OFFICIAL_SQL, encoding='utf-8') as fh:
            head = fh.read(600)
        self.assertIn('user_version = 21', head)
        self.assertRegex(head, r'moneymanagerex/database/blob/[0-9a-f]{40}/tables\.sql')
        self.assertEqual(SUPPORTED_SCHEMA_VERSIONS, frozenset({21}),
                         'a new supported version needs its own official schema file and tests')

    def test_required_columns_exist_in_official_schema(self):
        conn = official_conn()
        for table, cols in REQUIRED_COLUMNS.items():
            present = set(table_columns(conn, table))
            self.assertFalse([c for c in cols if c not in present], f'{table} lacks columns the engine uses')

    def test_legacy_columns_are_not_in_fresh_schema(self):
        conn = official_conn()
        for table, cols in LEGACY_NULL_COLUMNS.items():
            self.assertFalse(set(cols) & set(table_columns(conn, table)),
                             f'{table}: a "legacy" column is in the fresh schema; it should be required instead')

    def test_engine_writes_every_not_null_column(self):
        """A NOT NULL column without a default that the engine doesn't fill
        would make every insert fail on a fresh DB."""
        conn = official_conn()
        for table in ('CHECKINGACCOUNT_V1', 'PAYEE_V1', 'TAGLINK_V1'):
            for _, name, _, notnull, default, pk in conn.execute(f'PRAGMA table_info({table})'):
                if notnull and default is None and not pk:
                    self.assertIn(name, REQUIRED_COLUMNS[table], f'{table}.{name} is NOT NULL')


@unittest.skipUnless(os.environ.get('MMEX_LIVE_DB'), 'set MMEX_LIVE_DB to check a real DB (read-only)')
class LiveDbTests(unittest.TestCase):
    def setUp(self):
        path = os.environ['MMEX_LIVE_DB']
        self.conn = sqlite3.connect(f'file:{os.path.abspath(path)}?mode=ro', uri=True)

    def tearDown(self):
        self.conn.close()

    def test_live_db_passes_the_schema_check(self):
        schema = check(self.conn)
        self.assertIn(schema.version, SUPPORTED_SCHEMA_VERSIONS)

    def test_live_db_is_not_wal(self):
        self.assertNotEqual(self.conn.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')

    def test_live_columns_beyond_engine_are_known(self):
        """Columns the live DB has in the tables the engine writes, that
        neither the engine nor the official schema knows about, would be
        left NULL on insert: list them so a new MMEX column can't slip by."""
        official = official_conn()
        for table in ('CHECKINGACCOUNT_V1', 'PAYEE_V1', 'TAGLINK_V1'):
            live = set(table_columns(self.conn, table))
            known = set(REQUIRED_COLUMNS[table]) | set(LEGACY_NULL_COLUMNS.get(table, ())) | set(table_columns(official, table))
            self.assertFalse(live - known, f'{table} has unknown columns')


if __name__ == '__main__':
    unittest.main()
