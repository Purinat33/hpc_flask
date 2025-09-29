import pytest
from flask.testing import FlaskClient
from models.users_db import create_user


@pytest.fixture
def client(app, db_engine) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def admin_user():
    # If your app seeds admin from env in test, skip this. Otherwise ensure it exists:
    create_user("admin", "admin", role="admin")
    return ("admin", "admin")


@pytest.fixture
def login_admin(client, admin_user):
    u, p = admin_user
    # Your login form field names may differ
    client.post("/login", data={"username": u,
                "password": p}, follow_redirects=True)
    yield
    client.post("/logout")
