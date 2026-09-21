from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict


class ConstituentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    index_code: str
    isin: str
    ticker: str
    name: str
    weight: Decimal
    shares: int
    effective_date: date
    ingested_at: datetime


class UploadResponse(BaseModel):
    upload_id: int
    filename: str
    rows_ingested: int
    uploaded_at: datetime


class DeleteResponse(BaseModel):
    id: int
    deleted: bool
    deleted_at: datetime


ExportFormat = Literal["json", "csv"]

# (DeleteResponse уже есть выше, ничего добавлять не нужно)