# models/base.py
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os


class Base(DeclarativeBase):
    pass


def make_engine_from_env():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return create_engine(url, pool_pre_ping=True, future=True)


Engine = None
SessionLocal = None


def init_engine_and_session():
    global Engine, SessionLocal
    if Engine is None:
        Engine = make_engine_from_env()
        SessionLocal = sessionmaker(
            bind=Engine, autoflush=False, autocommit=False, future=True)
    return Engine, SessionLocal
