from contextlib import contextmanager

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings

# Ensure postgresql:// URLs use psycopg2 driver
_database_url = settings.database_url
if _database_url.startswith("postgresql://") and "+" not in _database_url.split("?")[0]:
    _database_url = _database_url.replace("postgresql://", "postgresql+psycopg2://", 1)

# SQLite: busy timeout + single-connection pool to avoid "database is locked"
_connect_args = {}
_engine_kw = {"future": True, "pool_pre_ping": True}
if "sqlite" in _database_url:
    _connect_args["timeout"] = 20
    _engine_kw["connect_args"] = _connect_args
    _engine_kw["poolclass"] = StaticPool  # one connection, no concurrent access = no lock
else:
    if _connect_args:
        _engine_kw["connect_args"] = _connect_args

Base = declarative_base()
engine = create_engine(_database_url, **_engine_kw)
# expire_on_commit=False so template can read row.id, row.name etc after session is closed
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, future=True, expire_on_commit=False
)


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _add_haro_mailbox_column_if_missing()
    _add_mailbox_smtp_columns_if_missing()
    _add_business_mailbox_id_column_if_missing()
    _add_business_strict_ai_columns_if_missing()
    _add_reply_asset_columns_if_missing()
    _seed_regency_strict_ai_defaults()


def _add_haro_mailbox_column_if_missing() -> None:
    """Add use_for_haro to mailboxes if table exists but column is missing (e.g. existing DBs)."""
    try:
        with engine.connect() as conn:
            if "sqlite" in str(engine.url):
                conn.execute(text("ALTER TABLE mailboxes ADD COLUMN use_for_haro BOOLEAN DEFAULT 0"))
            else:
                conn.execute(text("ALTER TABLE mailboxes ADD COLUMN use_for_haro BOOLEAN DEFAULT FALSE"))
            conn.commit()
    except Exception:
        pass  # column already exists or table missing


def _add_mailbox_smtp_columns_if_missing() -> None:
    """Add optional SMTP columns to mailboxes for existing databases."""
    stmts = [
        "ALTER TABLE mailboxes ADD COLUMN smtp_host VARCHAR(255) DEFAULT ''",
        "ALTER TABLE mailboxes ADD COLUMN smtp_port INTEGER DEFAULT 587",
        "ALTER TABLE mailboxes ADD COLUMN smtp_user VARCHAR(255) DEFAULT ''",
        "ALTER TABLE mailboxes ADD COLUMN smtp_password TEXT DEFAULT ''",
    ]
    for sql in stmts:
        try:
            with engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
        except Exception:
            pass


def _add_business_mailbox_id_column_if_missing() -> None:
    """Add mailbox_id to businesses for linking outbound SMTP to a Mailboxes row."""
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE businesses ADD COLUMN mailbox_id INTEGER"))
            conn.commit()
    except Exception:
        pass


def _add_business_strict_ai_columns_if_missing() -> None:
    """Strict AI relevance fields (per-business prompt + threshold)."""
    stmts = [
        "ALTER TABLE businesses ADD COLUMN strict_ai_relevance_enabled BOOLEAN DEFAULT 0 NOT NULL",
        "ALTER TABLE businesses ADD COLUMN strict_ai_relevance_system_prompt TEXT DEFAULT '' NOT NULL",
        "ALTER TABLE businesses ADD COLUMN strict_ai_relevance_min_confidence FLOAT DEFAULT 0.82 NOT NULL",
    ]
    for sql in stmts:
        try:
            with engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
        except Exception:
            pass


def _add_reply_asset_columns_if_missing() -> None:
    """Asset pipeline metadata on replies (existing DBs get columns via ALTER)."""
    stmts = [
        "ALTER TABLE replies ADD COLUMN asset_mode VARCHAR(32)",
        "ALTER TABLE replies ADD COLUMN asset_plan_json TEXT",
        "ALTER TABLE replies ADD COLUMN selected_asset_metadata_json TEXT",
        "ALTER TABLE replies ADD COLUMN attachment_paths_json TEXT",
        "ALTER TABLE replies ADD COLUMN inline_preview_paths_json TEXT",
        "ALTER TABLE replies ADD COLUMN full_res_link VARCHAR(1024)",
        "ALTER TABLE replies ADD COLUMN must_disclose_ai BOOLEAN DEFAULT 0 NOT NULL",
        "ALTER TABLE replies ADD COLUMN manual_review_required BOOLEAN DEFAULT 0 NOT NULL",
        "ALTER TABLE replies ADD COLUMN manual_review_reason TEXT",
        "ALTER TABLE replies ADD COLUMN asset_send_status VARCHAR(32)",
    ]
    for sql in stmts:
        try:
            with engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
        except Exception:
            pass


def _seed_regency_strict_ai_defaults() -> None:
    """Populate default strict-relevance prompt for Regency Shop once columns exist."""
    try:
        from app.models import Business
        from app.regency_ai_relevance import REGENCY_RELEVANCE_SYSTEM_PROMPT

        with SessionLocal() as session:
            for b in session.scalars(select(Business)).all():
                n = (b.name or "").strip().lower()
                is_regency = n == "regency shop" or ("regency" in n and "shop" in n)
                if not is_regency:
                    continue
                if not (b.strict_ai_relevance_system_prompt or "").strip():
                    b.strict_ai_relevance_enabled = True
                    b.strict_ai_relevance_system_prompt = REGENCY_RELEVANCE_SYSTEM_PROMPT
                    if not b.strict_ai_relevance_min_confidence or b.strict_ai_relevance_min_confidence <= 0:
                        b.strict_ai_relevance_min_confidence = 0.82
            session.commit()
    except Exception:
        pass


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_session_for_reprocess():
    """Separate engine/session for reprocess background thread so it never blocks on main pool."""
    url = _database_url
    kw = {"future": True, "pool_pre_ping": True}
    if "sqlite" in url:
        kw["connect_args"] = {"timeout": 30}
        kw["poolclass"] = StaticPool
    else:
        if _connect_args:
            kw["connect_args"] = dict(_connect_args)
    reprocess_engine = create_engine(url, **kw)
    ReprocessSession = sessionmaker(
        bind=reprocess_engine, autoflush=False, autocommit=False, future=True, expire_on_commit=False
    )
    sess = ReprocessSession()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()
