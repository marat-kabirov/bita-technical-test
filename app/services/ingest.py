import csv
import io
from datetime import datetime

from sqlalchemy.orm import Session
from app.models import Upload, ConstituentRecord

BATCH_SIZE = 500


def ingest_csv(db: Session, filename: str, file_bytes: bytes) -> Upload:
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    upload = Upload(filename=filename, row_count=0)
    db.add(upload)
    db.flush()

    batch = []
    row_count = 0

    for row in reader:
        batch.append({
            "index_code": row["index_code"],
            "isin": row["isin"],
            "ticker": row["ticker"],
            "name": row["name"],
            "weight": row["weight"],
            "shares": int(row["shares"]),
            "effective_date": datetime.strptime(row["effective_date"], "%Y-%m-%d").date(),
            "upload_id": upload.id,
        })
        row_count += 1

        if len(batch) >= BATCH_SIZE:
            db.bulk_insert_mappings(ConstituentRecord, batch)
            batch = []

    if batch:
        db.bulk_insert_mappings(ConstituentRecord, batch)

    upload.row_count = row_count
    db.commit()
    db.refresh(upload)

    return upload