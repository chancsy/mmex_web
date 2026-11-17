# mmex_web

A self-hosted web app for [Money Manager Ex](https://moneymanagerex.org/)
(MMEX) databases, meant to be the single writer to an `.mmb` file. Python
3.12 and Flask, server-rendered, with no JavaScript build and nothing
loaded from a CDN. Light enough for a Raspberry Pi.

Status: in daily use by its author. Accounts, registers, entry and edit
with undo, reconciliation flags, currency rates, monthly reports, and a
queue (with a JSON API) through which other programs propose entries for
approval.

<!-- TODO: add screenshots of the app (accounts, register, entry form, reconcile, queue) once the UI is further along. -->


## Why

MMEX keeps everything in one SQLite file. Syncing that file between desktop,
phone and server works until two of them write to it, and then you get sync
conflicts. mmex_web runs next to the file and does every write itself:
- **Your entries** from a phone or browser go straight in.
- **Entries from automation** wait in a queue until you approve them.

## How it works

```
phone / PC browser ──(HTTPS reverse proxy or VPN)──► web UI ──your edits───────┐
                                                                               │
automation (e.g. a bill fetcher) ──POST /api/queue──► queue ──you approve──────┤
                                                      (app.db)                 ▼
                                                                engine ──► MMEX .mmb file
                                                                  │       (synced to desktop/phone)
                                                                  └──► backups + journal (undo)
```

- **One writer.** Every write goes through the engine in one gunicorn
  process (one worker, a few threads), one at a time, with a backup first.
- **Two databases:** the MMEX file, where the app touches only what MMEX
  itself would; and `app.db`, the app's own SQLite in its data dir, with
  the write history (for undo), the queue, settings and login state.
- **Desktop MMEX still works** for reports and anything the app doesn't
  do, on the same file through your sync tool. Just don't write from two
  places at once (see Caveats).

## Web app (`web/`)

- **Pages:**
  - **Accounts:** grouped by type, with each account's current and
    reconciled balance.
  - **Account register:** newest first, with a running balance that
    always covers every row, whatever the filter. Filters: status, dates,
    text.
  - **Flag button** on every row (register and Recent): one tap moves it
    to the next status, unflagged → R → F → unflagged, in place; the
    register's Reconciled and Unreconciled totals follow, so ticking rows
    against a statement is a matter of tapping. Taps less than 15 minutes
    apart, with no other write between, are one History entry and one
    undo. Void rows keep their chip (set V in the edit form).
  - **Recent:** rows recently added, edited or deleted, across all
    accounts. Each row says whether mmex_web wrote it.
  - **New transaction** (phone first): Withdrawal, Deposit, Transfer or Twin.
    - Payee and category boxes search as you type, recently used first;
      choosing a payee fills in the category it was last used with.
    - Tags: tick any number, or type new ones.
    - A transfer between currencies asks for the amount received too.
    - "Create new payee" appears when the payee doesn't exist yet.
    - Warns about a possible duplicate (same account and amount within 3
      days) and saves only if you confirm.
    - "Save & add another" keeps the account, type and date.
    - **Twin:** one purchase saved as two rows, for books that record
      spending paid on another account's behalf (e.g. a household purchase
      paid by card): a transfer from the paying account to the To account
      (e.g. Cash), notes filled from a pattern ("{payee},
      {expense_account} -> {to_account}", editable in Settings), plus a
      withdrawal with the real payee in the expense account (e.g.
      Household). The transfer's notes are filled in as you type and can
      be changed. One save, one undo. The To and expense accounts used
      last are preselected.
    - "+ New" in the top bar starts in the account whose page you're on;
      elsewhere, in the account used last.
    - Can be added to a phone's home screen.
  - **Edit and delete:** tap a row's payee. Every field can change,
    including the type (Withdrawal, Deposit or Transfer). Delete is MMEX's own soft delete (the row goes to MMEX's
    trash). A form opened before someone else changed the row is refused
    rather than overwriting their change.
  - **History:** every write mmex_web made, newest first, each with an
    Undo. Undoing an undo is a redo. Either refuses if the row has changed
    since.
  - **Queue:** batches other programs send through the API (below), e.g. a
    bill-fetching job's new bill or a reconciliation plan. Nothing reaches
    the DB until it's approved here; the top bar shows how many wait.
    - Each batch shows what approving it does to the DB *as it is now*:
      every row, and each account's balance before and after. If that
      differs from what it did when sent (other entries since, or a row it
      expects has changed), the page says so, keeps the as-sent version
      to compare, and asks for a tick before approving.
    - Approve applies the whole batch in one transaction, always with a
      backup first, and only if what it does still matches what the page
      showed. It lands in History under the sender's name and can be
      undone there.
    - Reject files it away with an optional note the sender can read.
    - A batch of one add can be edited first (amount, date, notes); a
      bigger one is approved or rejected whole.
    - A batch whose checks fail now (e.g. its `absent` check finds the
      row was entered by hand meanwhile) can't be approved, only rejected.
    - **Review layout:** the batch reads as a timeline in plan order, with
      each run of balance checks as a divider (✓ or ✗). A twin's two rows
      show as one line; badges mark twins, splits, transfers, new payees
      and tags, and changed or deleted rows (old → new value). Filter
      chips narrow it to twins, transfers, own rows or changes. The
      sender's `summary` sits at the top, each op's `why` under its row,
      and the Approve button stays in view at the bottom. The plain-text
      preview is still there, folded.
  - **Accounts page:** groups in MMEX's order with a total each, and a
    grand total, in the base currency. A balance converts at the latest
    rate in MMEX's currency history (or the currency's fixed rate). Groups
    collapse (remembered per device), with Expand all / Collapse all.
    "Show all in <base>" shows every account's balance converted too, its
    own-currency amount underneath.
  - **Hide balances:** the eye in the top bar swaps every balance and
    total for dots at once (transaction amounts stay), and remembers it.
  - **Rates:** MMEX's currency history, one column per currency, with
    every month-end listed. One tap downloads a month-end's rates from
    xe.com's daily table (one page request, standard library only) at
    xe.com's full precision, as MMEX's own online update stores them. The
    whole table (every currency) is saved in `app.db`, so later fills for
    that date, or for a currency kept in Settings, need no further request;
    Refresh fetches a date again. Rates can also be typed (stored as typed),
    changed or deleted per date. Rate changes are backed up like any write
    but aren't in History.
  - **Reports** (read-only; amounts in the base currency, balances at
    each month-end's rate, income and expenses at the rate on their date):
    - **Month-end balances:** every account at a chosen month-end, in its
      own currency and converted, with its change from the month before.
      An asset account includes the MMEX assets it holds (the asset named
      like the account, or like its type), valued as MMEX does: its value
      grown at its rate from its start date, or, once transactions are
      linked to it, the buys, sells and revaluations, grown between them.
    - **Net worth:** Total, Liquid and Investment by month or by year with their
      changes, and how much of Total's change came only from exchange
      rates. Investment's change leaves out transfers into or out of
      investment accounts. A line chart draws them month by month, plus up
      to five extra lines chosen in Settings: accounts alone, or added to
      or taken out of Liquid or Total (e.g. "Liquid + pension", or
      "Without pension": Total minus a pension account).
    - **Income vs expenses:** each month's income (deposits), expenses
      (withdrawals), net and savings rate. Transfers and rows linked to an
      asset never count; split rows count under their split categories.
      Settings can leave out whole accounts (e.g. a retirement fund) and
      categories with their subcategories (e.g. windfalls); the page says
      what was left out over the range.
      A chart shows each month's net as a bar, with its 12-month average.
    - **By category:** categories × months with total and average;
      top-level categories expand to their subcategories. A chart ranks
      spending by category over the range; each row has a small trend line.
    - **Account changes:** how each account type changed by month or by
      year; types expand to their accounts. A chart stacks each type's
      gain above zero and loss below, with the net change as a dot.
    - Ranges: the last 3 to 36 months to a chosen month, or a calendar year
      (Net worth also: all months); by year, the last 3, 5 or 10 years, or
      all. "All" starts at the first month with data, or at a start year
      set in Settings.
    - Charts are drawn in the page (`web/static/charts.js`, no library);
      the table under each has every value, and the eye hides the amounts
      on balance charts as it does in the tables.
  - **Settings:** favourite accounts first, hide balances, accounts to
    hide in a collapsed "Others" list at the bottom of their group, the
    Twin notes pattern, and extra currencies to keep rates for. For the
    reports, per account: **Ignore** (a separate "wallet", left out of
    every report), **Not liquid** (in Total, not Liquid; asset and loan
    accounts until first saved), **Investment** (liquid or not) and **Not
    in Income vs expenses**; categories left out of Income vs expenses; and
    the Net worth chart's extra lines and the year "All" starts from. Kept
    in `app.db`, so they're the same on every device.
  - **Light or dark:** a button in the top bar, remembered per device (a
    cookie); until it's used, the page follows the system setting.
- **Login:** one user, with a password set from the command line. There's
  no sign-up page.
  - The session cookie is signed, HttpOnly, SameSite=Lax, and HTTPS-only
    by default.
  - Every form carries a CSRF token.
  - After 5 failed logins in 15 minutes, logging in is blocked until the
    oldest failure is 15 minutes old.
  - Setting a new password logs every session out.
- **Database access:** a read-only SQLite connection per request; every
  write goes through the engine (below), one at a time.
  - The app only writes when `MMEX_BACKUP_DIR` is set and the DB file and
    its folder are writable. Otherwise it's read-only and says why.
  - A banner appears when the DB's schema isn't one the engine supports,
    when a Syncthing conflict copy sits next to it, or when MMEX has it
    open. The last two are warnings only; writes still go ahead.
- **Headers:** a strict Content-Security-Policy, no framing, and
  `no-store` on every page.
- **API (`/api/*`):** JSON, for programs. Nothing it does writes the MMEX
  DB; a submitted batch waits for approval on the Queue page.
  - **Tokens:** one per sender ("source"), made with `python -m web token
    <source>`, printed once, kept only as a hash in `secrets.json`; making
    a new one replaces the old, `--revoke` removes it, and neither needs a
    restart. A batch is filed under the token's source, so one sender
    can't pose as another, and a sender sees only its own batches.
  - **Direct callers only:** a request that came through a reverse proxy
    (any `X-Forwarded-*`, `Forwarded` or `X-Real-IP` header) gets 404, so
    the internet-facing route can't reach the API, while a job on the same
    host or docker network can. `MMEX_WEB_API_VIA_PROXY=1` lifts that.
  - **Endpoints** (`Authorization: Bearer <token>`):

    | Request | Does |
    |---|---|
    | `POST /api/queue` `{"key", "title", "ops", "replace", "summary"}` | Previews the batch and queues it (`summary`, optional, is shown at the top of the batch on the Queue page; any op may carry a `"why"`, shown under what it does): 201 with the preview; 422 if it's invalid or a check fails (with the reason and the preview, so the sender can tell e.g. "already entered"); 409 if that key is already pending (`"replace": true` supersedes it); 503 if the DB can't be read. |
    | `GET /api/queue?key=&status=` | This source's batches, newest first. |
    | `GET /api/queue/<id>` | One batch: `status` (pending, approved, rejected, superseded), `note`, `rows` (the TRANSIDs an approved batch added), `undone`, and its preview. |
    | `GET /api/queue/summary` | How many batches wait, from every source, and since when. |
    | `GET /api/balance?account=&as_of=` | An account's balance, the engine's way. |
    | `GET /api/find?account=&from=&to=&amount=&payee=&category=&notes=&id=&include_deleted=&limit=` | Transactions, as plan-level fields. |
    | `GET /api/lookup/<accounts\|payees\|categories\|tags>?like=` | Names and ids. |
    | `GET /api/reports/month-end?month=YYYY-MM` | Every account at that month-end (own currency and base), its change from the month before, its report settings, and Total / Liquid / Investment. |

### Run with Docker

```bash
cp docker-compose.example.yml docker-compose.yml
cp .env.example .env            # set MMEX_DIR, MMEX_DB_FILE, APP_DATA_DIR, PUID/PGID
docker compose build
docker compose run --rm mmex_web python -m web setup    # choose the login password
docker compose up -d
```

The app listens on `127.0.0.1:${PORT}` (8000 by default). Put it behind an
HTTPS reverse proxy or a VPN. To change the password later, run the `setup`
command again; `--rotate` also replaces the session key.

For a program that sends batches to the queue, make it a token and give
the token to that program (it's shown once):

```bash
docker compose run --rm mmex_web python -m web token bills    # prints the token
docker compose run --rm mmex_web python -m web token --list
```

The program then calls the API directly (e.g. `http://127.0.0.1:8000/api`
on the same host), not through the reverse proxy.

The example mounts the MMEX folder read-only, so the app starts read-only.
To let it write, remove `:ro` from that mount. Before you do, stop every
other copy of MMEX from writing the same file (for example a phone app on
a synced copy); otherwise sync tools will see conflicting versions.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MMEX_DB_PATH` | (required) | The `.mmb` file (the compose file builds it from `MMEX_DIR`/`MMEX_DB_FILE`). |
| `APP_DATA_DIR` | (required) | The app's own data: `secrets.json` (login, API token hashes) and `app.db` (history of writes for undo, settings, the queue). |
| `MMEX_BACKUP_DIR` | empty | Where DB backups go. Without it the app never writes. |
| `MMEX_BACKUP_KEEP` | `20` | How many of the app's backups to keep. |
| `MMEX_BACKUP_MIN_INTERVAL_MIN` | `30` | Skip the backup before a write when the newest is younger than this; `0` backs up every write. Undo and redo follow it too (each is recorded in app.db and can itself be reversed). |
| `MMEX_WEB_TRUSTED_PROXIES` | `0` (`1` in the compose example) | Reverse proxies in front that set `X-Forwarded-*`. |
| `MMEX_WEB_COOKIE_SECURE` | `1` | HTTPS-only session cookie. Set `0` only for plain-HTTP testing. |
| `MMEX_WEB_URL_PREFIX` | empty | Serve under a path such as `/money`. `/healthz` stays at the root. |
| `MMEX_WEB_SESSION_DAYS` | `30` | How long a login lasts. |
| `MMEX_WEB_API_VIA_PROXY` | `0` | Let `/api/*` answer requests that came through a reverse proxy. |

### Development

```bash
pip install -r requirements.txt
export MMEX_DB_PATH=/path/to/a/copy.mmb APP_DATA_DIR=/tmp/mmex_web_data MMEX_BACKUP_DIR=/tmp/mmex_web_bak MMEX_WEB_COOKIE_SECURE=0
python -m web setup
python -m web run --port 8000        # http://127.0.0.1:8000
```

Point it at a copy of your DB, not the original.

## Caveats

- **MMEX apps write even when you change nothing.** Opening a DB in MMEX
  (the Android app especially) updates it, so a sync tool sees a new file.
  If MMEX and mmex_web both write after the same sync, one side's changes
  end up in a conflict copy (the app shows a banner when it sees one).
  Retire other writers, and keep desktop MMEX closed when not in use, or
  open reports on a copy.
- **Same-day order.** Running balances within a day may differ from
  MMEX's if it orders same-day rows differently; end-of-day figures agree.
- **New MMEX schema versions.** The app goes read-only by itself until the
  engine is checked against the new version (see "Supporting a new MMEX
  schema version").
- **One worker only.** Don't raise gunicorn's `--workers`: the single
  process is what makes the app the single writer.

## Write engine (`engine/`)

Standard library only, no dependencies (Flask and friends are only for `web/`).

- **Schema pinning:** the engine only writes DBs whose `PRAGMA user_version`
  it has been checked against (currently 21), and only if every column it
  uses exists. Anything else is refused, and nothing is written.
- **Fresh and upgraded DBs:** a v21 DB created fresh and one upgraded from
  older versions differ (the latter keeps legacy columns such as
  `SUBCATEGID`). The engine handles both.
- **Batches of plan ops:** `add`, `update`, `delete`, `flag`, `check` and
  `absent`, in a JSON plan format (documented at the top of
  `engine/core.py`). An add or update can also create the payee or tags it
  names. `check` asserts an account's balance on a date; `absent` asserts
  an account has no live row in a date range (of a type, to a payee), e.g.
  "this month's bill isn't entered yet", put before the add it guards.
  - Each batch is all-or-nothing: an invalid op or a failing `check` or
    `absent` rolls the whole batch back.
  - Rows are shaped the way the MMEX GUI writes them. IDs are UTC
    microseconds, timestamps UTC, deletes soft (to MMEX's trash), tags go
    through `TAGLINK_V1`.
- **Preview:** runs a batch on an in-memory copy and reports each row
  before and after, plus account balances and check results. The file on
  disk is only read.
- **Journal and undo:** every write returns a journal. Undo reverses it,
  and refuses rows that changed since.
- **Backups:** a set number of timestamped copies
  (`<db>_web_YYYY-MM-DD_HHMMSS.bak`). They're made with SQLite's backup
  API and integrity-checked. MMEX's own `_update_` backups are never
  touched.
- **Guards:**
  - Never writes a DB in WAL mode, because sync tools would copy the
    `.mmb` without the `-wal` file.
  - Warns about, or refuses on, a Syncthing `.sync-conflict` copy next to
    the DB, or MMEX's in-use flag (`ISUSED`).
  - A DB fingerprint stops a journal from being applied to a different DB.

```python
from engine import preview, apply, undo, BackupPolicy

plan = {'ops': [{'op': 'add', 'date': '2026-11-11', 'type': 'Withdrawal', 'account': 'Wallet',
                 'payee': 'Bakery', 'category': 'Food:Groceries', 'amount': 4.50}]}
result = preview('money.mmb', plan)            # nothing written
result = apply('money.mmb', plan, BackupPolicy('bak', keep=20, min_interval_min=30))
undo('money.mmb', result.journal, BackupPolicy('bak'), commit=True)
```

### CLI

```bash
python -m engine preview plan.json --db money.mmb
python -m engine apply plan.json --db money.mmb --commit --backup-dir bak --journal-dir journal
python -m engine undo journal/<file>.json --db money.mmb --commit --backup-dir bak
python -m engine balance --db money.mmb --account Wallet --as-of 2026-10-31
python -m engine find --db money.mmb --account Wallet --from 2026-10-01 --to 2026-10-31
python -m engine lookup payees --db money.mmb --like bak
```

No command has a default DB.

## Layout

| Path | What |
|---|---|
| `engine/schema.py` | Supported schema versions and the columns the engine needs. |
| `engine/core.py` | Plan ops, name lookups, balance, undo, read-only queries. |
| `engine/batch.py` | Entry points: `preview`, `apply`, `undo`, `read`. |
| `engine/backups.py` | Count-based backups. |
| `engine/dbfile.py` | Opening DBs (read-only, in-memory, writable), guards, fingerprint. |
| `engine/render.py` | Plain-text output for the CLI. |
| `engine/views.py` | Read-only views for the UI: accounts, register with running balance, recent rows, entry-form pickers, near duplicates. |
| `engine/reports.py`, `web/reports.py` | Report figures (month-end balances, rates by date, asset values, income and expenses); the Reports pages. |
| `web/` | Flask app: settings, login, pages, templates, static files. |
| `web/pages.py` | The read-only pages: accounts, register, recent. |
| `web/queue.py`, `web/review.py`, `web/api.py` | The Queue pages; the review layout (twins, badges, check dividers) built from a preview; the JSON API. |
| `web/entry.py`, `web/writer.py`, `web/store.py` | The pages that write; the single writer (lock, backups); `app.db` with the history and settings. |
| `web/settings.py` | The Settings page. |
| `engine/rates.py`, `web/rates.py` | Currency history: xe.com table parsing and fetch, writes; the Rates pages and the saved-table fill. |
| `Dockerfile`, `docker-compose.example.yml`, `.env.example` | Container image and an example deployment. |
| `tests/` | unittest suite. The test DBs are built from the official MMEX schema in `tests/schema/`. |

## Tests

```bash
python -m unittest
# also check a real DB against the engine's schema rules (opened read-only):
MMEX_LIVE_DB=/path/to/your.mmb python -m unittest tests.test_schema -v
```

## Supporting a new MMEX schema version

1. Read the new `incremental_upgrade/database_version_N.sql` in
   [moneymanagerex/database](https://github.com/moneymanagerex/database).
2. Copy that commit's `tables.sql` to `tests/schema/mmex_tables_vN.sql`, with
   the same header.
3. In `engine/schema.py`, add N to `SUPPORTED_SCHEMA_VERSIONS` and adjust
   `REQUIRED_COLUMNS` or `LEGACY_NULL_COLUMNS`.
4. Run the tests, including the real-DB check.

## Licence

`tests/schema/mmex_tables_v21.sql` is copied unmodified from
moneymanagerex/database and is GPL-2.0; see
`tests/schema/LICENSE-moneymanagerex-database`. The app icon
(`web/static/icon.svg`, and `web/static/favicon.ico` made from MMEX's
`mmex.ico` by keeping its 16, 32 and 48 px images) is Money Manager Ex's
own, from `resources/` in moneymanagerex/moneymanagerex, also GPL-2.0.

The rest of mmex_web is licensed under the GNU General Public License,
version 2 or (at your option) any later version. See `LICENSE`.
