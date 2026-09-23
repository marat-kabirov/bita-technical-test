# BITA Technical Test — Index Constituents API

A FastAPI service that ingests index constituent data from CSV files into PostgreSQL,
preserving a full ingestion history, and exposes it through a small REST API.

## Setup

### Requirements
- Python 3.11+ (developed and tested on 3.13)
- PostgreSQL running locally

### Running PostgreSQL via Docker (alternative)

If you don't have PostgreSQL installed locally, a `docker-compose.yml` is provided:

```bash
docker-compose up -d
```

This starts Postgres on `localhost:5432` with user/password/db `postgres`/`postgres`/`bita_test`.
Use `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bita_test` in your `.env`
in this case.

### Installation

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux (bash):**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root (see `.env.example`):

```
DATABASE_URL=postgresql://<user>:<password>@localhost:5432/<database_name>
```

Create the target database (e.g. via psql or pgAdmin):

```sql
CREATE DATABASE bita_test;
```

### Running

```bash
uvicorn app.main:app --reload
```

Tables are created automatically on startup, inside a FastAPI `lifespan` handler
(`Base.metadata.create_all`). No migration tool was used given the scope of this
exercise — see "Alternatives considered" below.

Interactive API docs: http://localhost:8000/docs

## API

### `POST /upload`
Accepts a `.csv` file (multipart/form-data) and loads its rows into the database.
Every call is purely additive — no existing row is ever updated or deleted, so calling
this endpoint multiple times with the same or an updated file preserves the full
ingestion history. Returns `201 Created` on success, since it creates an `uploads`
resource.

### `DELETE /constituents/{id}`
Soft-deletes a single ingested record by its surrogate id. The underlying row is never
physically removed — it is flagged (`is_deleted = true`, `deleted_at` set) and excluded
from future reads via `/export`. Returns `404` if the id doesn't exist, or `409
Conflict` if the record was already deleted (the request is well-formed, it just
conflicts with the record's current state).

### `GET /constituents/export?start_date=...&end_date=...&format=json|csv&as_of=...`
Returns the *current* view of constituent data within the given `effective_date` range:
for each business key (see below), only the most recently ingested, non-deleted version
is returned. Format is selectable via the `format` query parameter. The optional `as_of`
parameter reconstructs the export as it would have looked at that point in time instead
— see "Point-in-time export" below.

## Data model

**`uploads`** — one row per file upload (filename, timestamp, row count). Exists purely
to make ingestion history traceable to its source file.

**`constituent_records`** — append-only. Every row from every upload is inserted as a
new record; nothing is ever updated in place.

| Column                                 | Purpose                                            |
|----------------------------------------|----------------------------------------------------|
| `id`                                   | Surrogate primary key (`BigInteger`), identifies one ingested row |
| `index_code`, `isin`, `effective_date` | Business key (see below)                           |
| `ingested_at`                          | When this specific row was loaded                  |
| `upload_id`                            | Which upload produced this row                     |
| `is_deleted`, `deleted_at`             | Soft-delete flag, never a real DELETE              |

`id` is `BigInteger` rather than the default `Integer`: this is an append-only table
that only ever grows, so the wider range costs nothing and avoids a theoretical
exhaustion of a 32-bit primary key over the table's lifetime.

Two indexes support the export query: `ix_constituent_business_key` on
`(index_code, isin, effective_date, ingested_at)` supports the window function's
partition/sort, and a partial index `ix_constituent_effective_date_live` on
`effective_date` (`WHERE NOT is_deleted`) supports the range filter that `/export`
actually runs, which a composite index leading with `index_code` wouldn't serve well.

Verified empirically against a synthetic 100,000-row table (2,000 distinct business
keys, all on one `effective_date`): `EXPLAIN (ANALYZE, BUFFERS)` on the export query
confirms Postgres uses `ix_constituent_effective_date_live` for the date-range filter
(`Bitmap Index Scan on ix_constituent_effective_date_live`), not a full table scan.
Peak memory for materialising the resulting 2,000-row export (measured with
`tracemalloc`) was 3.81 MB — a small fraction of the underlying 100,000-row table,
consistent with memory scaling with the result set rather than the table size.

## Technical decisions

### Resolving "current" for a business key
A row is uniquely identified, from a business perspective, by
`(index_code, isin, effective_date)`. Because uploads are append-only, this combination
can appear multiple times in the table (e.g. a corrected re-upload of the same file).
"Current" is resolved as the version with the latest `ingested_at` for that key that is
not soft-deleted, using a `ROW_NUMBER() OVER (PARTITION BY index_code, isin,
effective_date ORDER BY ingested_at DESC)` window function, filtering `is_deleted` before
the window is applied so that a deleted "current" row correctly falls back to the next
most recent surviving version of the same key, rather than removing the key from the
result entirely.

`server_default=func.now()` on `ingested_at` returns the transaction's start time in
Postgres, not the moment each individual row is written — so every row inserted within
one upload (one transaction) shares the same `ingested_at`. The secondary sort key
`id DESC` breaks that tie deterministically: `id` is a monotonically increasing
surrogate key, so if the same business key appears twice within a single upload, the
later row in the file always wins.

### Point-in-time export (`as_of`)
`/export` normally resolves to the *latest* non-deleted ingested version per business
key. Passing `as_of` (an ISO-8601 datetime) reconstructs the export as it would have
looked at that earlier moment instead: only rows with `ingested_at <= as_of` are
candidates, and a row counts as deleted only if `deleted_at <= as_of` — so a row deleted
*after* the requested `as_of` still appears, exactly as it would have at that time. Both
filters are applied in the same place as the normal `is_deleted` filter, before the
window function, for the same reason described above. Without `as_of`, behaviour is
unchanged.

### Delete semantics
`DELETE /constituents/{id}` targets a specific ingested row by its own surrogate id, not
the business key as a whole. This was a deliberate choice: since a business key can have
several ingested versions over time, deleting "the row currently shown for key X" should
mean exactly that — the specific version currently surfaced — and not silently erase
every historical version ever ingested for that key. If the deleted row was the current
one, the next most recent surviving version becomes current automatically, through the
resolution logic above, without any extra bookkeeping.

### Bulk insert without `COPY`
Per the exercise restrictions, `COPY` and other native bulk-import mechanisms are not
used. Instead, rows are parsed with the standard library `csv` module and inserted via
SQLAlchemy Core's `insert()` construct, executed in batches of 500 rows via
`db.execute(insert(ConstituentRecord), batch)`. This issues batched multi-row `INSERT`
statements (effectively `executemany` under the hood) rather than one round-trip per row,
which is meaningfully faster than row-by-row ORM inserts while staying well clear of the
`COPY` protocol.

### Streaming upload
The uploaded file is not read into memory as a whole before parsing. `UploadFile.file`
is already a spooled temporary file (buffered in memory up to ~1MB, then spilled to
disk), so it's wrapped directly in `io.TextIOWrapper` and fed to `csv.DictReader`, which
reads and parses the file incrementally, row by row, rather than materialising the full
file content as a single string first. A `UnicodeDecodeError` raised mid-file (rather
than up front, since decoding now happens lazily per line) is still caught and returns
the same `400` response with the file's rejection reason.

### Streaming export
The CSV export path streams results row by row rather than materialising the full
result set in memory: `get_current_constituents` uses `yield_per(1000)`, which issues
a server-side cursor against Postgres so rows are fetched in batches rather than all at
once, and each row is written and yielded to the `StreamingResponse` as soon as it's
read. The generator opens its own DB session (rather than reusing a request-scoped
`Depends(get_db)` session) because FastAPI closes that session before a streamed
response finishes sending on the pinned FastAPI version (0.115) in this project.

The generator itself is a plain (synchronous) function, not wrapped in `async def`:
Starlette runs a synchronous generator passed to `StreamingResponse` in its threadpool,
which keeps the blocking psycopg2 calls off the main event loop — the same reason the
request handlers above are plain `def` rather than `async def`.

The JSON export path is not streamed — the full result list is built in memory before
being returned as a JSON array. Streaming a valid JSON array incrementally is more
complex than CSV (matching brackets/commas across chunks) and was judged unnecessary
for this exercise; if this needed to scale, NDJSON (one JSON object per line) would be
the natural streaming alternative.

### Error handling
Row-level validation reports the specific invalid row and the reason it failed, but a
validation failure anywhere in the file rolls back the entire upload — partial loads are
never committed. A header-only CSV (no data rows) is rejected the same way, so no empty
`uploads` row is left behind. **Alternative considered and discarded:** accepting valid
rows and reporting invalid ones separately as a partial-success response. This was
rejected to keep each upload atomic and avoid ambiguity about which rows from a single
file actually made it into the "current" view after a partially-failed load.

Ingestion errors are raised in `app/services/ingest.py` as a domain-specific
`IngestError` (not `HTTPException`), and translated to `HTTPException(400, ...)` in the
route handler — the service layer doesn't need to know it's being called from an HTTP
API. A global handler for `sqlalchemy.exc.OperationalError` returns `503` with a plain
message, so a database outage surfaces as a clear, generic error instead of a raw
traceback.

### Alternatives considered and discarded
- **A `deleted` flag resolved *after* the window function**, rather than before: this
  would have caused a deleted "current" row to hide the entire business key rather than
  correctly exposing the next most recent version — discarded once this edge case was
  identified.
- **Database migrations (Alembic)**: given the scope and single-environment nature of
  this exercise, `create_all()` was judged sufficient; a real production system handling
  schema evolution over time would use proper migrations.
- **Unique constraint on the business key**: deliberately not added, since the exercise
  explicitly allows the same business key to appear multiple times across loads.

## Tests

```bash
pytest
```

**⚠️ Warning:** the test suite calls `drop_all()` against the database pointed to by
`TEST_DATABASE_URL` (falling back to `DATABASE_URL` if unset). Do not run tests against
a database whose data you want to keep — set `TEST_DATABASE_URL` to a separate database
first.

Covers: CSV ingestion (row counts persisted correctly), repeated uploads not overwriting
prior data, soft-delete behavior (both hiding from export and physical persistence in
the database, and rejecting a second delete with `409`), current-resolution/export logic
including the delete-then-fallback edge case, a deterministic tie-break when the same
business key appears twice within a single upload, atomic rollback on invalid rows (no
`uploads` or `constituent_records` rows left behind, including for a header-only CSV),
CSV export content (parsed and checked against the uploaded values), input validation
(non-`.csv` filename, missing required column, non-UTF-8 payload, `NaN`/negative
`weight`, negative `shares`, `start_date > end_date`), and point-in-time export via
`as_of` (both for a superseded value and for a since-deleted row).

A separate, one-off script (`scripts/perf_check.py`, not part of the application or the
test suite) seeds a synthetic 100,000-row table and runs the `EXPLAIN`/`tracemalloc`
checks referenced in "Data model" above.