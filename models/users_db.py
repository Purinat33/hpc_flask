# models/users_db.py
import os
import sqlite3
from datetime import datetime
from flask import g, current_app
from werkzeug.security import generate_password_hash, check_password_hash


def get_users_db():
    if "users_db" not in g:
        db_path = current_app.config.get(
            "USERS_DB",
            os.path.join(current_app.instance_path, "users.sqlite3"),
        )
        g.users_db = sqlite3.connect(
            db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        g.users_db.row_factory = sqlite3.Row
        g.users_db.execute("PRAGMA foreign_keys = ON")
    return g.users_db


def close_users_db(_=None):
    db = g.pop("users_db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username      TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL CHECK(role IN ('admin','user')),
  created_at    TEXT NOT NULL
);
"""


def init_users_db():
    db = get_users_db()
    db.executescript(SCHEMA)
    db.commit()


def get_user(username: str):
    db = get_users_db()
    cur = db.execute("SELECT * FROM users WHERE username = ?", (username,))
    return cur.fetchone()


def create_user(username: str, password: str, role: str = "user"):
    db = get_users_db()
    db.execute(
        "INSERT OR REPLACE INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
        (username, generate_password_hash(password),
         role, datetime.utcnow().isoformat()),
    )
    db.commit()


def verify_password(username: str, password: str) -> bool:
    row = get_user(username)
    return bool(row and check_password_hash(row["password_hash"], password))


def init_app(app):
    app.teardown_appcontext(close_users_db)
