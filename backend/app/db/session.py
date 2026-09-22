"""Database engine/session setup."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models.models import Base

# Load .env directly here too — this module is sometimes imported on its
# own (e.g. `python -c "from backend.app.db.session import init_db; init_db()"`)
# without going through backend/app/core/config.py first, so relying on
# that module's load_dotenv() call alone isn't enough. load_dotenv() is
# safe to call more than once per process.
load_dotenv(dotenv_path=Path(__file__).resolve().parents[3] / ".env")


def _database_url() -> str:
    user = os.getenv("POSTGRES_USER", "neuravex")
    password = os.getenv("POSTGRES_PASSWORD", "change-me")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "neuravex")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


_engine = None
_SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(_database_url(), pool_pre_ping=True)
    return _engine


def _get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal


def init_db() -> None:
    Base.metadata.create_all(bind=_get_engine())


def get_db():
    session = _get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_session():
    session = _get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
