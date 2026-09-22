from datetime import date
from sqlalchemy import func, and_
from sqlalchemy.orm import Session
from app.models import ConstituentRecord


def get_current_constituents(db: Session, start_date: date, end_date: date):
    row_number = (
        func.row_number()
        .over(
            partition_by=[
                ConstituentRecord.index_code,
                ConstituentRecord.isin,
                ConstituentRecord.effective_date,
            ],
            order_by=[
                ConstituentRecord.ingested_at.desc(),
                ConstituentRecord.id.desc(),
            ],
        )
        .label("rn")
    )

    subquery = (
        db.query(ConstituentRecord, row_number)
        .filter(
            ConstituentRecord.effective_date >= start_date,
            ConstituentRecord.effective_date <= end_date,
            ConstituentRecord.is_deleted == False,
        )
        .subquery()
    )

    from sqlalchemy.orm import aliased
    aliased_record = aliased(ConstituentRecord, subquery)

    results = (
        db.query(aliased_record)
        .filter(subquery.c.rn == 1)
        .order_by(aliased_record.effective_date, aliased_record.index_code, aliased_record.isin)
        .yield_per(1000)
    )

    return results