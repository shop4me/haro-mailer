from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    nature_of_business: Mapped[str] = mapped_column(Text, nullable=False, default="")
    keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    brand_voice: Mapped[str] = mapped_column(Text, nullable=False, default="")
    website_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    sending_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    smtp_user: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    smtp_password: Mapped[str] = mapped_column(Text, nullable=False, default="")
    signature: Mapped[str] = mapped_column(Text, nullable=False, default="")

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_send_threshold: Mapped[float] = mapped_column(Float, default=0.8, nullable=False)

    # Optional: send replies via this mailbox's SMTP instead of business SMTP / name match.
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id"), nullable=True)
    mailbox: Mapped["Mailbox | None"] = relationship("Mailbox", back_populates="businesses_for_smtp")

    # Strict AI relevance gate (template: Regency Shop ships with prompt + enabled; other businesses can opt in).
    strict_ai_relevance_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    strict_ai_relevance_system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    strict_ai_relevance_min_confidence: Mapped[float] = mapped_column(Float, default=0.82, nullable=False)

    replies: Mapped[list["Reply"]] = relationship("Reply", back_populates="business")


class Mailbox(Base):
    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_host: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, default=993)
    imap_user: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_password: Mapped[str] = mapped_column(Text, nullable=False)
    folder: Mapped[str] = mapped_column(String(255), nullable=False, default="INBOX")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Skip SSL cert verification (use only when server cert doesn't match hostname, e.g. connect by IP)
    imap_skip_ssl_verify: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Only one mailbox is used for fetching/parsing HARO; others are not used for download
    use_for_haro: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Optional outgoing SMTP (same account as IMAP). If empty, replies use each Business's SMTP settings.
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    smtp_user: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    smtp_password: Mapped[str] = mapped_column(Text, nullable=False, default="")

    inbound_emails: Mapped[list["InboundEmail"]] = relationship("InboundEmail", back_populates="mailbox")
    businesses_for_smtp: Mapped[list["Business"]] = relationship("Business", back_populates="mailbox")


class InboundEmail(Base):
    __tablename__ = "inbound_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id"), nullable=False)
    message_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)

    from_addr: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    raw_headers: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="NEW")

    mailbox: Mapped["Mailbox"] = relationship("Mailbox", back_populates="inbound_emails")
    requests: Mapped[list["HaroRequest"]] = relationship("HaroRequest", back_populates="inbound_email")


class HaroRequest(Base):
    __tablename__ = "haro_requests"
    __table_args__ = (UniqueConstraint("haro_query_id", name="uq_haro_query_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inbound_email_id: Mapped[int] = mapped_column(ForeignKey("inbound_emails.id"), nullable=False)
    haro_query_id: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outlet: Mapped[str | None] = mapped_column(String(255), nullable=True)
    journalist_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reply_to_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deadline: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    requirements_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    inbound_email: Mapped["InboundEmail"] = relationship("InboundEmail", back_populates="requests")
    classification: Mapped["Classification"] = relationship(
        "Classification",
        back_populates="haro_request",
        uselist=False,
    )
    reply: Mapped["Reply"] = relationship("Reply", back_populates="haro_request", uselist=False)


class Classification(Base):
    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    haro_request_id: Mapped[int] = mapped_column(ForeignKey("haro_requests.id"), nullable=False, unique=True)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched_business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reasoning_short: Mapped[str] = mapped_column(Text, nullable=False, default="")
    topic_tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Per-business relevance audit (JSON array): id, name, relevant, reason, source
    per_business_audit_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    haro_request: Mapped["HaroRequest"] = relationship("HaroRequest", back_populates="classification")


class Reply(Base):
    __tablename__ = "replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    haro_request_id: Mapped[int] = mapped_column(ForeignKey("haro_requests.id"), nullable=False, unique=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), nullable=True)
    reply_subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reply_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    send_status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    asset_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    asset_plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_asset_metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_paths_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    inline_preview_paths_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_res_link: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    must_disclose_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manual_review_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manual_review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_send_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    haro_request: Mapped["HaroRequest"] = relationship("HaroRequest", back_populates="reply")
    business: Mapped["Business"] = relationship("Business", back_populates="replies")


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
