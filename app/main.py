import csv
import io
from datetime import date, datetime, timezone

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db, SessionLocal
from app import models
from app.schemas import UploadResponse, DeleteResponse, ConstituentOut, ExportFormat
from app.services.ingest import ingest_csv
from app.services.query import get_current_constituents

Base.metadata.create_all(bind=engine)

app = FastAPI(title="BITA Index Constituents API")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/upload", response_model=UploadResponse)
def upload_constituents(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

    text_stream = io.TextIOWrapper(file.file, encoding="utf-8-sig", newline="")
    upload = ingest_csv(db, file.filename, text_stream)

    return UploadResponse(
        upload_id=upload.id,
        filename=upload.filename,
        rows_ingested=upload.row_count,
        uploaded_at=upload.uploaded_at,
    )


@app.delete("/constituents/{record_id}", response_model=DeleteResponse)
def delete_constituent(record_id: int, db: Session = Depends(get_db)):
    record = db.get(models.ConstituentRecord, record_id)

    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    if record.is_deleted:
        raise HTTPException(status_code=400, detail="Record already deleted")

    record.is_deleted = True
    record.deleted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)

    return DeleteResponse(id=record.id, deleted=True, deleted_at=record.deleted_at)


def stream_csv(start_date: date, end_date: date):
    """
    Streams the CSV export row by row using its own DB session, since this
    generator keeps running after the request's Depends(get_db) session
    would normally be closed by FastAPI.
    """
    db = SessionLocal()
    try:
        header_buf = io.StringIO()
        csv.writer(header_buf).writerow(
            ["id", "index_code", "isin", "ticker", "name", "weight", "shares", "effective_date", "ingested_at"]
        )
        yield header_buf.getvalue()

        records = get_current_constituents(db, start_date, end_date)

        row_buf = io.StringIO()
        writer = csv.writer(row_buf)
        for r in records:
            writer.writerow(
                [r.id, r.index_code, r.isin, r.ticker, r.name, r.weight, r.shares, r.effective_date, r.ingested_at]
            )
            yield row_buf.getvalue()
            row_buf.seek(0)
            row_buf.truncate(0)
    finally:
        db.close()


@app.get("/constituents/export")
def export_constituents(
    start_date: date = Query(...),
    end_date: date = Query(...),
    format: ExportFormat = Query("json"),
    db: Session = Depends(get_db),
):
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    if format == "json":
        records = get_current_constituents(db, start_date, end_date)
        return [ConstituentOut.model_validate(r) for r in records]

    # format == "csv" — streamed row by row via a server-side cursor,
    # rather than materialising the full result set and CSV buffer in memory
    return StreamingResponse(
        stream_csv(start_date, end_date),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=constituents_export.csv"},
    )