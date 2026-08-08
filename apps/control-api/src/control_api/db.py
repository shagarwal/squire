"""Database engine + session helpers.

Sync SQLAlchemy on purpose: FastAPI runs sync endpoints in a threadpool, the
provisioning state machine is a plain loop, and there is no async plumbing to get
wrong. At alpha volumes (tens of tenants, one ingress lookup per Telegram message)
this is nowhere near a bottleneck.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from control_api.config import get_settings

_engine: Engine | None = None


def _normalise_url(url: str) -> str:
    """Railway hands out `postgresql://...`; point SQLAlchemy at psycopg 3."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):  # legacy Heroku-style prefix
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = _normalise_url(settings.database_url)
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            # SQLite is test/dev only; allow cross-thread use for the worker thread.
            kwargs = {"connect_args": {"check_same_thread": False}}
        _engine = create_engine(url, **kwargs)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine (tests; also safe after a config reload)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_db() -> None:
    """Create tables if absent.

    v0 has no migration tool. The schema is three tables and Phase 1 will bring
    Alembic in when the first real migration is needed; until then `create_all` on
    boot is honest and sufficient.
    """
    # Import for the side effect of registering the tables on SQLModel.metadata.
    from control_api import models  # noqa: F401

    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session. Commits on success, rolls back on exception."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
