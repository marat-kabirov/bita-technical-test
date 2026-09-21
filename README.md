# BITA Technical Test — Index Constituents API

A FastAPI service that ingests index constituent data from CSV files into PostgreSQL,
preserving a full ingestion history, and exposes it through a small REST API.

## Setup

### Requirements
- Python 3.11+ (developed and tested on 3.13)
- PostgreSQL running locally

### Running PostgreSQL via Docker (alternative)

If you don't have PostgreSQL installed locally, a `docker-compose.yml` is provided:

```powershell
docker-compose up -d
```

This starts Postgres on `localhost:5432` with user/password/db `postgres`/`postgres`/`bita_test`.
Use `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bita_test` in your `.env`
in this case.

### Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file in the project root (see `.env.example`):

​```
DATABASE_URL=postgresql://<user>:<password>@localhost:5432/<database_name>
​```

Create the target database (e.g. via psql or pgAdmin):

```sql
CREATE DATABASE bita_test;
```

### Running

```powershell
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
SQLAlchemy's `bulk_insert_mappings`, in batches of 500 rows. This issues batched
multi-row `INSERT` statements (effectively `executemany` under the hood) rather than one
round-trip per row, which is meaningfully faster than row-by-row ORM inserts while
staying well clear of the `COPY` protocol.

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

## Tests

```powershell
pytest
```

Covers: CSV ingestion (row counts persisted correctly), repeated uploads not overwriting
prior data, soft-delete behavior, and current-resolution/export logic including the
delete-then-fallback edge case.