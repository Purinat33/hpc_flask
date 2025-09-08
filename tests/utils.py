import os


# tests/utils.py
def login_user(client, username, password):
    return client.post("/login",
                       data={"username": username, "password": password},
                       follow_redirects=False)


def login_admin(client, password=None):
    pw = password or os.getenv("ADMIN_PASSWORD", "admin123")
    return login_user(client, "admin", pw)
