from sqlalchemy import (
    Column, Integer, String, Numeric, BigInteger, Date, DateTime,
    Boolean, ForeignKey, Index, func
)
from sqlalchemy.orm import relationship
from app.db import Base


class Upload(Base):
    __tablename__ = "uploads"

    id = Column(Integer, primary_key=True)
    filename = Column(String, nullable=False)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())
    row_count = Column(Integer, nullable=False, default=0)

    records = relationship("ConstituentRecord", back_populates="upload")


class ConstituentRecord(Base):
    __tablename__ = "constituent_records"

    id = Column(Integer, primary_key=True)

    index_code = Column(String, nullable=False)
    isin = Column(String, nullable=False)
    ticker = Column(String, nullable=False)
    name = Column(String, nullable=False)
    weight = Column(Numeric, nullable=False)
    shares = Column(BigInteger, nullable=False)
    effective_date = Column(Date, nullable=False)

    ingested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    upload_id = Column(Integer, ForeignKey("uploads.id"), nullable=False)

    is_deleted = Column(Boolean, nullable=False, default=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    upload = relationship("Upload", back_populates="records")

    __table_args__ = (
        Index(
            "ix_constituent_business_key",
            "index_code", "isin", "effective_date", "ingested_at",
        ),
    )