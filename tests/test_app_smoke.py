# tests/test_app_smoke.py
from contextlib import contextmanager
from flask import template_rendered


@contextmanager
def captured_templates(app):
    rec = []

    def receiver(sender, template, context, **extra):
        rec.append((template, context))
    template_rendered.connect(receiver, app)
    try:
        yield rec
    finally:
        template_rendered.disconnect(receiver, app)


def test_root_redirects_to_playground(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/playground" in r.headers["Location"]


def test_playground_renders(client, app):
    with captured_templates(app) as rec:
        r = client.get("/playground")
        assert r.status_code == 200
    assert rec and rec[-1][0].name.endswith("playground.html")


def test_set_locale_sets_cookie_and_redirects(client):
    r = client.post("/i18n/set", data={"lang": "th"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    # cookie present and lasts a while
    set_cookie = r.headers.get("Set-Cookie", "")
    assert "lang=th" in set_cookie and "Max-Age=" in set_cookie


def test_set_locale_rejects_invalid_lang(client):
    r = client.post("/i18n/set", data={"lang": "xx"}, follow_redirects=False)
    assert r.status_code == 400
