import os
import sqlite3
from flask import g, current_app


def get_db():
    if "db" not in g:
        db_path = current_app.config.get("BILLING_DB")
        g.db = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS receipts(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  username   TEXT NOT NULL,
  start      TEXT NOT NULL,
  end        TEXT NOT NULL,
  total      REAL NOT NULL DEFAULT 0 CHECK(total >= 0),
  status     TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','void')), -- pending|paid|void
  created_at TEXT NOT NULL,
  paid_at    TEXT,
  method     TEXT,
  tx_ref     TEXT
);

CREATE TABLE IF NOT EXISTS receipt_items(
  receipt_id      INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  job_key         TEXT NOT NULL,          -- canonical job id (unique across all receipts)
  job_id_display  TEXT NOT NULL,          -- original string shown in PDFs/UI
  cost            REAL NOT NULL,
  cpu_core_hours  REAL NOT NULL,
  gpu_hours       REAL NOT NULL,
  mem_gb_hours    REAL NOT NULL,
  UNIQUE(job_key)
);

CREATE INDEX IF NOT EXISTS idx_items_receipt ON receipt_items(receipt_id);
"""


def init_db():
    db = get_db()
    db.executescript(SCHEMA_SQL)
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
