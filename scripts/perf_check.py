"""
One-off performance verification script for the README's D.1/D.2 claims.
Not part of the application; run manually, not via pytest.

Usage:
    python scripts/perf_check.py
"""
import os
import random
import time
import tracemalloc
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, ConstituentRecord, Upload
from app.services.query import get_current_constituents

DATABASE_URL = os.environ.get("PERF_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Set PERF_DATABASE_URL (or DATABASE_URL) before running this script.")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

TOTAL_ROWS = 100_000
N_INDEX_CODES = 5
N_ISINS_PER_INDEX = 2000  # 5 * 2000 = 10,000 distinct business keys
DATE_START = date(2026, 1, 1)


def reset_and_seed():
    print(f"Dropping and recreating tables, then seeding {TOTAL_ROWS} rows...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    upload = Upload(filename="perf_seed.csv", row_count=0)
    db.add(upload)
    db.flush()
    upload_id = upload.id

    batch = []
    inserted = 0
    while inserted < TOTAL_ROWS:
        index_code = f"IDX{inserted % N_INDEX_CODES}"
        isin = f"US{(inserted % N_ISINS_PER_INDEX):010d}"
        batch.append({
            "index_code": index_code,
            "isin": isin,
            "ticker": f"T{inserted % 1000}",
            "name": f"Company {inserted}",
            "weight": round(random.uniform(0.01, 10.0), 4),
            "shares": random.randint(1000, 10_000_000),
            "effective_date": DATE_START,
            "ingested_at": datetime.now(timezone.utc),
            "upload_id": upload_id,
            "is_deleted": False,
            "deleted_at": None,
        })
        inserted += 1
        if len(batch) >= 5000:
            db.execute(ConstituentRecord.__table__.insert(), batch)
            batch.clear()
    if batch:
        db.execute(ConstituentRecord.__table__.insert(), batch)

    upload.row_count = inserted
    db.commit()
    db.close()
    print(f"Seeded {inserted} rows across {N_INDEX_CODES * N_ISINS_PER_INDEX} business keys.")


def run_explain():
    print("\n--- D.1: EXPLAIN on the export query (one-day range) ---")
    db = SessionLocal()
    explain_sql = text("""
        EXPLAIN (ANALYZE, BUFFERS)
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY index_code, isin, effective_date
                ORDER BY ingested_at DESC, id DESC
            ) AS rn
            FROM constituent_records
            WHERE effective_date >= :start_date
              AND effective_date <= :end_date
              AND NOT is_deleted
        ) sub
        WHERE rn = 1
    """)
    result = db.execute(explain_sql, {"start_date": DATE_START, "end_date": DATE_START})
    for row in result:
        print(row[0])
    db.close()


def measure_memory(n_rows_label: str, start_date: date, end_date: date):
    db = SessionLocal()
    tracemalloc.start()
    records = get_current_constituents(db, start_date, end_date)
    materialized = list(records)  # force full materialization, same as the JSON export path
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    db.close()
    print(
        f"D.2 [{n_rows_label}]: {len(materialized)} rows returned, "
        f"peak memory = {peak / 1024 / 1024:.2f} MB"
    )


if __name__ == "__main__":
    reset_and_seed()
    run_explain()

    print("\n--- D.2: memory measurement ---")
    # ~100k rows in the table, all one business-key universe -> exporting the
    # full range returns one row per distinct key (~10,000 rows here).
    measure_memory("full ~10k-key export", DATE_START, DATE_START)

    # A narrower manual comparison isn't meaningful with this synthetic
    # dataset's single date; the peak above already reflects the ~10k-row
    # "current" result set drawn from a 100k-row underlying table.