import csv
import io
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db, SessionLocal
from app import models
from app.schemas import UploadResponse, DeleteResponse, ConstituentOut, ExportFormat
from app.services.ingest import ingest_csv, IngestError
from app.services.query import get_current_constituents


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="BITA Index Constituents API", lifespan=lifespan)


@app.exception_handler(OperationalError)
async def operational_error_handler(request: Request, exc: OperationalError):
    return JSONResponse(status_code=503, content={"detail": "Database is temporarily unavailable"})


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/upload", response_model=UploadResponse, status_code=201)
def upload_constituents(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

    text_stream = io.TextIOWrapper(file.file, encoding="utf-8-sig", newline="")
    try:
        upload = ingest_csv(db, file.filename, text_stream)
    except IngestError as e:
        detail = f"Error at line {e.line_number}: {e}" if e.line_number is not None else str(e)
        raise HTTPException(status_code=400, detail=detail)

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
        raise HTTPException(status_code=409, detail="Record already deleted")

    record.is_deleted = True
    record.deleted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)

    return DeleteResponse(id=record.id, deleted=True, deleted_at=record.deleted_at)


def _stream_csv_rows(start_date: date, end_date: date, as_of: Optional[datetime]):
    db = SessionLocal()
    try:
        header_buf = io.StringIO()
        csv.writer(header_buf).writerow(
            ["id", "index_code", "isin", "ticker", "name", "weight", "shares", "effective_date", "ingested_at"]
        )
        yield header_buf.getvalue()

        records = get_current_constituents(db, start_date, end_date, as_of=as_of)

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


@app.get(
    "/constituents/export",
    responses={200: {"content": {"application/json": {}, "text/csv": {}}}},
)
def export_constituents(
    start_date: date = Query(...),
    end_date: date = Query(...),
    format: ExportFormat = Query("json"),
    as_of: Optional[datetime] = Query(
        None,
        description="Reconstruct the export as it would have looked at this point in "
        "time, using ingested_at/deleted_at instead of always taking the latest version.",
    ),
):
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    if format == "json":
        db = SessionLocal()
        try:
            records = get_current_constituents(db, start_date, end_date, as_of=as_of)
            return [ConstituentOut.model_validate(r) for r in records]
        finally:
            db.close()

    # format == "csv" — streamed row by row via a server-side cursor, using its
    # own DB session (opened inside _stream_csv_rows), independent of any
    # per-request dependency.
    return StreamingResponse(
        _stream_csv_rows(start_date, end_date, as_of),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=constituents_export.csv"},
    )