from datetime import date, datetime
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased
from app.models import ConstituentRecord


def get_current_constituents(
    db: Session,
    start_date: date,
    end_date: date,
    as_of: Optional[datetime] = None,
):
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

    filters = [
        ConstituentRecord.effective_date >= start_date,
        ConstituentRecord.effective_date <= end_date,
    ]

    if as_of is None:
        filters.append(ConstituentRecord.is_deleted == False)
    else:
        filters.append(ConstituentRecord.ingested_at <= as_of)
        filters.append(
            or_(
                ConstituentRecord.deleted_at.is_(None),
                ConstituentRecord.deleted_at > as_of,
            )
        )

    # Filter is_deleted (or, with as_of, the deleted_at-based equivalent) BEFORE
    # the window function. If we filtered after, a deleted "current" row would
    # still take rn=1 and then be dropped, making the whole key vanish instead
    # of falling back to the next most recent surviving version.
    subquery = (
        db.query(ConstituentRecord, row_number)
        .filter(*filters)
        .subquery()
    )

    aliased_record = aliased(ConstituentRecord, subquery)

    results = (
        db.query(aliased_record)
        .filter(subquery.c.rn == 1)
        .order_by(aliased_record.effective_date, aliased_record.index_code, aliased_record.isin)
        .yield_per(1000)
    )

    return results