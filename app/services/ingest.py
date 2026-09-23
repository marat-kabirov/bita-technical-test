import csv
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import insert
from sqlalchemy.orm import Session
from app.models import Upload, ConstituentRecord

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
REQUIRED_COLUMNS = {"index_code", "isin", "ticker", "name", "weight", "shares", "effective_date"}


class IngestError(ValueError):
    """Raised for any problem with an uploaded file's structure or content.
    Caught in main.py and translated into an HTTPException(400, ...)."""

    def __init__(self, message: str, line_number: int | None = None):
        self.line_number = line_number
        super().__init__(message)


def parse_row(row: dict, line_number: int) -> dict:
    try:
        shares = int(row["shares"])
    except (ValueError, TypeError):
        raise ValueError(f"invalid 'shares' value: {row['shares']!r} (expected an integer)")
    if shares < 0:
        raise ValueError(f"invalid 'shares' value: {shares!r} (must be >= 0)")

    try:
        effective_date = datetime.strptime(row["effective_date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise ValueError(
            f"invalid 'effective_date' value: {row['effective_date']!r} (expected YYYY-MM-DD)"
        )

    try:
        weight = Decimal(row["weight"])
    except (InvalidOperation, TypeError):
        raise ValueError(f"invalid 'weight' value: {row['weight']!r} (expected a number)")
    if not weight.is_finite():
        raise ValueError(f"invalid 'weight' value: {row['weight']!r} (must be a finite number)")
    if weight < 0:
        raise ValueError(f"invalid 'weight' value: {row['weight']!r} (must be >= 0)")

    return {
        "index_code": row["index_code"].strip(),
        "isin": row["isin"].strip(),
        "ticker": row["ticker"].strip(),
        "name": row["name"].strip(),
        "weight": weight,
        "shares": shares,
        "effective_date": effective_date,
    }


def ingest_csv(db: Session, filename: str, text_stream) -> Upload:
    try:
        reader = csv.DictReader(text_stream)
        fieldnames = reader.fieldnames
    except UnicodeDecodeError:
        raise IngestError("File is not valid UTF-8 encoded text")

    missing_columns = REQUIRED_COLUMNS - set(fieldnames or [])
    if missing_columns:
        raise IngestError(f"Missing required CSV columns: {sorted(missing_columns)}")

    logger.info(f"Starting ingestion of '{filename}'")

    upload = Upload(filename=filename, row_count=0)
    db.add(upload)
    db.flush()

    batch = []
    row_count = 0

    try:
        for line_number, row in enumerate(reader, start=2):
            try:
                parsed = parse_row(row, line_number)
            except (ValueError, KeyError) as e:
                db.rollback()
                logger.error(f"Ingestion of '{filename}' failed at CSV line {line_number}: {e}")
                raise IngestError(str(e), line_number=line_number)

            parsed["upload_id"] = upload.id
            batch.append(parsed)
            row_count += 1

            if len(batch) >= BATCH_SIZE:
                db.execute(insert(ConstituentRecord), batch)
                batch.clear()
    except UnicodeDecodeError:
        db.rollback()
        logger.error(f"Ingestion of '{filename}' failed: file is not valid UTF-8")
        raise IngestError("File is not valid UTF-8 encoded text")

    if row_count == 0:
        db.rollback()
        logger.error(f"Ingestion of '{filename}' failed: no data rows")
        raise IngestError("CSV contains no data rows")

    if batch:
        db.execute(insert(ConstituentRecord), batch)

    upload.row_count = row_count
    db.commit()
    db.refresh(upload)

    logger.info(f"Ingestion of '{filename}' complete: upload_id={upload.id}, rows={row_count}")

    return upload