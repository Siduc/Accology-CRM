"""Practice documents stored in OneDrive (metadata in CRM)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base

DOCUMENT_CATEGORIES = (
    "Engagement Letter",
    "Accounts",
    "Tax Return",
    "ID/KYC",
    "Correspondence",
    "Working Papers",
    "Invoices",
    "Other",
)

# OneDrive folder segment for each category (path-safe)
CATEGORY_FOLDER = {
    "Engagement Letter": "Engagement Letter",
    "Accounts": "Accounts",
    "Tax Return": "Tax Return",
    "ID/KYC": "ID-KYC",
    "Correspondence": "Correspondence",
    "Working Papers": "Working Papers",
    "Invoices": "Invoices",
    "Other": "Other",
}

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".txt",
    ".rtf",
    ".msg",
    ".eml",
}

PREVIEW_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}
PREVIEW_PDF_TYPES = {"application/pdf"}


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=True)
    tags = Column(String, nullable=True)  # comma-separated
    category = Column(String, default="Other", index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    is_key = Column(Boolean, default=False, index=True)
    original_filename = Column(String, nullable=True)
    content_type = Column(String, nullable=True)
    size_bytes = Column(Integer, default=0)
    onedrive_item_id = Column(String, nullable=True, index=True)
    onedrive_path = Column(String, nullable=True)
    onedrive_web_url = Column(String, nullable=True)
    onedrive_etag = Column(String, nullable=True)
    version = Column(Integer, default=1)
    uploaded_by = Column(String, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow, index=True)
    replaced_at = Column(DateTime, nullable=True)
    # active | deleted
    status = Column(String, default="active", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", foreign_keys=[client_id])
    job = relationship("Job", foreign_keys=[job_id])
    versions = relationship(
        "DocumentVersion",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="desc(DocumentVersion.version)",
    )

    def is_previewable(self) -> bool:
        ct = (self.content_type or "").lower()
        if ct in PREVIEW_PDF_TYPES or ct in PREVIEW_IMAGE_TYPES:
            return True
        name = (self.original_filename or "").lower()
        return name.endswith((".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"))

    def preview_kind(self) -> str:
        ct = (self.content_type or "").lower()
        name = (self.original_filename or "").lower()
        if ct in PREVIEW_PDF_TYPES or name.endswith(".pdf"):
            return "pdf"
        if ct in PREVIEW_IMAGE_TYPES or name.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp")
        ):
            return "image"
        return "other"

    def tags_list(self) -> list:
        if not self.tags:
            return []
        return [t.strip() for t in self.tags.split(",") if t.strip()]


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=False, index=True
    )
    version = Column(Integer, nullable=False, default=1)
    onedrive_item_id = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)
    size_bytes = Column(Integer, default=0)
    content_type = Column(String, nullable=True)
    uploaded_by = Column(String, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    note = Column(String, nullable=True)

    document = relationship("Document", back_populates="versions")
