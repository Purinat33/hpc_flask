# models/base.py
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy import create_engine
from contextlib import contextmanager
import os


class Base(DeclarativeBase):
    pass


def make_engine_from_env():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return create_engine(url, pool_pre_ping=True, future=True)


# Private globals
_Engine = None
_SessionFactory = None


def init_engine_and_session():
    """Idempotently init engine + session factory and return them."""
    global _Engine, _SessionFactory
    if _Engine is None:
        _Engine = make_engine_from_env()
        _SessionFactory = sessionmaker(
            bind=_Engine,
            autoflush=False,
            autocommit=False,
            future=True,
            expire_on_commit=False,  # prevents DetachedInstanceError after commit
        )
    return _Engine, _SessionFactory


def SessionLocal():
    """Return a new Session bound to the current engine."""
    _, factory = init_engine_and_session()
    return factory()


@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def make_engine(url: str):
    return create_engine(url, pool_pre_ping=True, future=True)


_EngineAuditWriter = None
_EngineAuditReader = None


def get_engine_audit_writer():
    global _EngineAuditWriter
    if _EngineAuditWriter is None:
        url = os.getenv("AUDIT_DATABASE_URL") or os.getenv("DATABASE_URL")
        _EngineAuditWriter = make_engine(url)
    return _EngineAuditWriter


def get_engine_audit_reader():
    global _EngineAuditReader
    if _EngineAuditReader is None:
        url = os.getenv("AUDIT_READER_URL") or os.getenv("DATABASE_URL")
        _EngineAuditReader = make_engine(url)
    return _EngineAuditReader
