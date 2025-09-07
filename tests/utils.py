import os


def login_admin(client, password=None):
    pw = password or os.getenv("ADMIN_PASSWORD", "admin123")
    return client.post("/login", data={"username": "admin", "password": pw}, follow_redirects=False)
