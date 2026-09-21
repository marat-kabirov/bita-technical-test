import io
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import Base, engine, SessionLocal

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
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
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