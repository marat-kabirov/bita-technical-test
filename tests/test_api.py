import io
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db import Base, get_db
from app import models

# Tests run against TEST_DATABASE_URL if set, falling back to DATABASE_URL only if not.
# WARNING: this fixture calls drop_all() on whatever database this resolves to.
# Do NOT run the test suite against a database whose data you want to keep.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

if not TEST_DATABASE_URL:
    raise RuntimeError(
        "TEST_DATABASE_URL (or DATABASE_URL) must be set to run the test suite. "
        "This database WILL be wiped by the tests — do not point it at data you need."
    )

test_engine = create_engine(TEST_DATABASE_URL)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

SAMPLE_CSV = (
    "index_code,isin,ticker,name,weight,shares,effective_date\n"
    "TESTIDX,US0000000001,AAA,Alpha Corp,10.5,1000,2026-01-01\n"
    "TESTIDX,US0000000002,BBB,Beta Corp,20.5,2000,2026-01-01\n"
)

SAMPLE_CSV_UPDATED = (
    "index_code,isin,ticker,name,weight,shares,effective_date\n"
    "TESTIDX,US0000000001,AAA,Alpha Corp,15.0,1500,2026-01-01\n"
    "TESTIDX,US0000000002,BBB,Beta Corp,25.0,2500,2026-01-01\n"
)


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield


def upload_csv(content: str, filename: str = "test.csv"):
    return client.post(
        "/upload",
        files={"file": (filename, io.BytesIO(content.encode()), "text/csv")},
    )


def test_upload_ingests_all_rows():
    response = upload_csv(SAMPLE_CSV)
    assert response.status_code == 200
    body = response.json()
    assert body["rows_ingested"] == 2
    assert body["upload_id"] is not None


def test_repeated_upload_does_not_overwrite_history():
    upload_csv(SAMPLE_CSV)
    upload_csv(SAMPLE_CSV_UPDATED)

    export = client.get(
        "/constituents/export",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01", "format": "json"},
    )
    assert export.status_code == 200
    data = export.json()
    assert len(data) == 2

    weights = {row["isin"]: float(row["weight"]) for row in data}
    assert weights["US0000000001"] == 15.0
    assert weights["US0000000002"] == 25.0


def test_delete_hides_record_from_export():
    upload_csv(SAMPLE_CSV)

    export_before = client.get(
        "/constituents/export",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01", "format": "json"},
    ).json()
    record_id = export_before[0]["id"]

    delete_response = client.delete(f"/constituents/{record_id}")
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] is True

    export_after = client.get(
        "/constituents/export",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01", "format": "json"},
    ).json()
    remaining_ids = [row["id"] for row in export_after]
    assert record_id not in remaining_ids
    assert len(export_after) == 1


def test_delete_falls_back_to_previous_version():
    upload_csv(SAMPLE_CSV)
    upload_csv(SAMPLE_CSV_UPDATED)

    export = client.get(
        "/constituents/export",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01", "format": "json"},
    ).json()

    target = next(row for row in export if row["isin"] == "US0000000001")
    current_id = target["id"]

    client.delete(f"/constituents/{current_id}")

    export_after = client.get(
        "/constituents/export",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01", "format": "json"},
    ).json()

    assert len(export_after) == 2
    fallback = next(row for row in export_after if row["isin"] == "US0000000001")
    assert fallback["id"] != current_id
    assert float(fallback["weight"]) == 10.5


def test_delete_nonexistent_record_returns_404():
    response = client.delete("/constituents/999999")
    assert response.status_code == 404


def test_csv_export_format():
    upload_csv(SAMPLE_CSV)
    response = client.get(
        "/constituents/export",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01", "format": "csv"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    content = response.text
    assert content.count("\n") >= 2


def test_invalid_row_rolls_back_entire_upload():
    bad_csv = (
        "index_code,isin,ticker,name,weight,shares,effective_date\n"
        "TESTIDX,US0000000001,AAA,Alpha Corp,10.5,1000,2026-01-01\n"
        "TESTIDX,US0000000002,BBB,Beta Corp,not_a_number,2000,2026-01-01\n"
    )

    response = upload_csv(bad_csv, filename="bad.csv")
    assert response.status_code == 400

    # nothing from this upload should have been committed: no uploads row,
    # and no constituent_records row, even for the valid line before the bad one
    db = TestSessionLocal()
    try:
        upload_count = db.query(models.Upload).count()
        record_count = db.query(models.ConstituentRecord).count()
        assert upload_count == 0
        assert record_count == 0
    finally:
        db.close()


def test_delete_preserves_row_in_database():
    upload_csv(SAMPLE_CSV)

    export = client.get(
        "/constituents/export",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01", "format": "json"},
    ).json()
    record_id = export[0]["id"]

    client.delete(f"/constituents/{record_id}")

    # the row must still physically exist in the database, not just be
    # excluded from /export — this is the "recoverable at all times" requirement
    db = TestSessionLocal()
    try:
        record = db.get(models.ConstituentRecord, record_id)
        assert record is not None
        assert record.is_deleted is True
        assert record.deleted_at is not None
    finally:
        db.close()


def test_csv_export_content_matches_uploaded_data():
    upload_csv(SAMPLE_CSV)
    response = client.get(
        "/constituents/export",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01", "format": "csv"},
    )
    assert response.status_code == 200

    import csv as csv_module
    import io as io_module

    reader = csv_module.DictReader(io_module.StringIO(response.text))
    rows = {row["isin"]: row for row in reader}

    assert len(rows) == 2
    assert rows["US0000000001"]["ticker"] == "AAA"
    assert float(rows["US0000000001"]["weight"]) == 10.5
    assert rows["US0000000002"]["ticker"] == "BBB"
    assert float(rows["US0000000002"]["weight"]) == 20.5


def test_duplicate_key_in_one_upload_resolves_to_later_row():
    csv_text = (
        "index_code,isin,ticker,name,weight,shares,effective_date\n"
        "TESTIDX,US0000000001,AAA,Alpha Corp,10.0,1000,2026-01-01\n"
        "TESTIDX,US0000000001,AAA,Alpha Corp,20.0,2000,2026-01-01\n"
    )
    upload_csv(csv_text)
    data = client.get(
        "/constituents/export",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01", "format": "json"},
    ).json()
    assert len(data) == 1
    assert float(data[0]["weight"]) == 20.0