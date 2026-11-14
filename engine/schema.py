"""Which MMEX databases the engine may write, and the column layout it uses.

Two kinds of DB legitimately carry the same `PRAGMA user_version`:
- one created fresh by a recent MMEX, matching the official tables.sql
  (tests/schema/mmex_tables_v21.sql), and
- one upgraded over the years from older versions, which keeps columns the
  fresh schema has since dropped (SUBCATEGID on several tables).

So the version number alone isn't enough. On top of it, every column the
engine reads or writes must exist, and a legacy column is written only
when the DB has it (always NULL, as MMEX itself does).
"""

SUPPORTED_SCHEMA_VERSIONS = frozenset({21})

# Columns the engine reads or writes, per table, in its own order.
REQUIRED_COLUMNS = {
    'CHECKINGACCOUNT_V1': ('TRANSID', 'ACCOUNTID', 'TOACCOUNTID', 'PAYEEID', 'TRANSCODE', 'TRANSAMOUNT',
                           'STATUS', 'TRANSACTIONNUMBER', 'NOTES', 'CATEGID', 'TRANSDATE',
                           'FOLLOWUPID', 'TOTRANSAMOUNT', 'LASTUPDATEDTIME', 'DELETEDTIME', 'COLOR'),
    'ACCOUNTLIST_V1': ('ACCOUNTID', 'ACCOUNTNAME', 'ACCOUNTTYPE', 'STATUS', 'FAVORITEACCT', 'CURRENCYID', 'INITIALBAL'),
    'CURRENCYFORMATS_V1': ('CURRENCYID', 'CURRENCYNAME', 'PFX_SYMBOL', 'SFX_SYMBOL', 'SCALE', 'CURRENCY_SYMBOL'),
    'CURRENCYHISTORY_V1': ('CURRHISTID', 'CURRENCYID', 'CURRDATE', 'CURRVALUE', 'CURRUPDTYPE'),
    'CATEGORY_V1': ('CATEGID', 'CATEGNAME', 'ACTIVE', 'PARENTID'),
    'PAYEE_V1': ('PAYEEID', 'PAYEENAME', 'CATEGID', 'NUMBER', 'WEBSITE', 'NOTES', 'ACTIVE', 'PATTERN'),
    'TAG_V1': ('TAGID', 'TAGNAME', 'ACTIVE'),
    'TAGLINK_V1': ('TAGLINKID', 'REFTYPE', 'REFID', 'TAGID'),
    'INFOTABLE_V1': ('INFONAME', 'INFOVALUE'),
}

# Dropped from the fresh schema but still present in upgraded DBs. Written
# as NULL when present, never required.
LEGACY_NULL_COLUMNS = {
    'CHECKINGACCOUNT_V1': ('SUBCATEGID',),
    'PAYEE_V1': ('SUBCATEGID',),
}


class SchemaError(Exception):
    """The DB isn't one the engine has been checked against. Nothing may be written."""


def table_columns(conn, table) -> list:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


class Schema:
    """The checked layout of one open DB."""

    def __init__(self, version, columns):
        self.version = version
        self.columns = columns  # table -> set of column names actually present

    def cols(self, table) -> list:
        """Required columns, then any legacy columns this DB has."""
        return list(REQUIRED_COLUMNS[table]) + [c for c in LEGACY_NULL_COLUMNS.get(table, ())
                                                if c in self.columns[table]]

    def legacy(self, table) -> list:
        return [c for c in LEGACY_NULL_COLUMNS.get(table, ()) if c in self.columns[table]]


def check(conn) -> Schema:
    """Raise SchemaError unless the DB's version is supported and every
    column the engine uses exists."""
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaError(f'schema version {version}; the engine is checked against '
                          f'{sorted(SUPPORTED_SCHEMA_VERSIONS)} only')
    columns = {}
    for table, wanted in REQUIRED_COLUMNS.items():
        present = set(table_columns(conn, table))
        missing = [c for c in wanted if c not in present]
        if missing:
            raise SchemaError(f'schema version {version} but {table} lacks {missing}')
        columns[table] = present
    return Schema(version, columns)
