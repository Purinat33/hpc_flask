import os
import io
import textwrap
import sqlite3
import pytest
from app import create_app
from datetime import date
from pathlib import Path


@pytest.fixture
def tmp_instance(tmp_path: Path):
    # temp instance dir with a temp DB and CSV used by the app config
    (tmp_path / "instance").mkdir()
    return tmp_path / "instance"


@pytest.fixture
def sample_csv_text():
    return textwrap.dedent("""\
        User|JobID|Elapsed|TotalCPU|ReqTRES|End|State
        a.u|1|01:00:00|02:00:00|cpu=2,mem=4G|2025-02-05T10:00:00|COMPLETED
        b.u|2|00:30:00|00:30:00|cpu=1,mem=1G|2025-12-31T00:00:00|COMPLETED
    """)


# tests/conftest.py
@pytest.fixture
def app(tmp_instance, monkeypatch, sample_csv_text):
    (tmp_instance / "test.csv").write_text(sample_csv_text, encoding="utf-8")

    from app import create_app
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,   # <- disable CSRF for tests
        SECRET_KEY="test-secret",
        BILLING_DB=str(tmp_instance / "billing.sqlite3"),
        FALLBACK_CSV=str(tmp_instance / "test.csv"),
    )

    # Force network fetchers off in tests
    import services.data_sources as ds
    monkeypatch.setattr(ds, "fetch_from_slurmrestd", lambda *a,
                        **k: (_ for _ in ()).throw(RuntimeError("off")))
    monkeypatch.setattr(ds, "fetch_from_sacct", lambda *a,
                        **k: (_ for _ in ()).throw(RuntimeError("off")))

    # Push app context for entire test
    ctx = app.app_context()
    ctx.push()
    try:
        from models.db import init_db
        init_db()
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def client(app):
    return app.test_client()
