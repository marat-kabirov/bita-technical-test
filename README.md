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

Tables are created automatically on startup (`Base.metadata.create_all`). No migration
tool was used given the scope of this exercise — see "Alternatives considered" below.

Interactive API docs: http://localhost:8000/docs

## API

### `POST /upload`
Accepts a `.csv` file (multipart/form-data) and loads its rows into the database.
Every call is purely additive — no existing row is ever updated or deleted, so calling
this endpoint multiple times with the same or an updated file preserves the full
ingestion history.

### `DELETE /constituents/{id}`
Soft-deletes a single ingested record by its surrogate id. The underlying row is never
physically removed — it is flagged (`is_deleted = true`, `deleted_at` set) and excluded
from future reads via `/export`.

### `GET /constituents/export?start_date=...&end_date=...&format=json|csv`
Returns the *current* view of constituent data within the given `effective_date` range:
for each business key (see below), only the most recently ingested, non-deleted version
is returned. Format is selectable via the `format` query parameter.

## Data model

**`uploads`** — one row per file upload (filename, timestamp, row count). Exists purely
to make ingestion history traceable to its source file.

**`constituent_records`** — append-only. Every row from every upload is inserted as a
new record; nothing is ever updated in place.

| Column                                 | Purpose                                            |
|----------------------------------------|----------------------------------------------------|
| `id`                                   | Surrogate primary key, identifies one ingested row |
| `index_code`, `isin`, `effective_date` | Business key (see below)                           |
| `ingested_at`                          | When this specific row was loaded                  |
| `upload_id`                            | Which upload produced this row                     |
| `is_deleted`, `deleted_at`             | Soft-delete flag, never a real DELETE              |

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

**Note on scope — point-in-time reconstruction:** `/export` always resolves to the
*latest* non-deleted ingested version per business key at query time. It does not
currently support reconstructing "what the index looked like as of a past point in time"
— i.e. querying "as it was believed to be" using `ingested_at` as of some earlier moment,
rather than always taking the latest. The append-only data model retains everything
needed to add this (an `as_of` parameter filtering on `ingested_at` before the window
function is applied), but implementing that query path was judged out of scope for this
exercise.

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

### Streaming export
The CSV export path streams results row by row rather than materialising the full
result set in memory: `get_current_constituents` uses `yield_per(1000)`, which issues
a server-side cursor against Postgres so rows are fetched in batches rather than all at
once, and each row is written and yielded to the `StreamingResponse` as soon as it's
read. The generator opens its own DB session (rather than reusing the request's
`Depends(get_db)` session) because FastAPI closes that session before a streamed
response finishes sending on the pinned FastAPI version (0.115) in this project.

The JSON export path is not streamed — the full result list is built in memory before
being returned as a JSON array. Streaming a valid JSON array incrementally is more
complex than CSV (matching brackets/commas across chunks) and was judged unnecessary
for this exercise; if this needed to scale, NDJSON (one JSON object per line) would be
the natural streaming alternative.

### Error handling on ingestion
Row-level validation reports the specific invalid row and the reason it failed, but a
validation failure anywhere in the file rolls back the entire upload — partial loads are
never committed. **Alternative considered and discarded:** accepting valid rows and
reporting invalid ones separately as a partial-success response. This was rejected to
keep each upload atomic and avoid ambiguity about which rows from a single file actually
made it into the "current" view after a partially-failed load.

### Alternatives considered and discarded
- **A `deleted` flag resolved *after* the window function**, rather than before: this
  would have caused a deleted "current" row to hide the entire business key rather than
  correctly exposing the next most recent version — discarded once this edge case was
  identified.
- **Database migrations (Alembic)**: given the scope and single-environment nature of
  this exercise, `create_all()` on startup was judged sufficient; a real production
  system handling schema evolution over time would use proper migrations.
- **Unique constraint on the business key**: deliberately not added, since the exercise
  explicitly allows the same business key to appear multiple times across loads.
- **Streaming file processing**: the uploaded file is read fully into memory before
  parsing, rather than processed as a stream. This is adequate for files in the 1–20MB
  range typical of this exercise; a production system ingesting much larger files would
  switch to streaming CSV parsing to bound memory usage. (Note: this applies to the
  *upload* path only — the *export* path is streamed, see "Streaming export" above.)

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
the database), current-resolution/export logic including the delete-then-fallback edge
case, a deterministic tie-break when the same business key appears twice within a single
upload, atomic rollback on invalid rows (no `uploads` or `constituent_records` rows left
behind), and CSV export content (parsed and checked against the uploaded values).