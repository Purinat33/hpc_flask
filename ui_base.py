# ui_base.py
from flask import url_for
from flask_login import current_user


def nav(active: str = "home") -> str:
    def item(label: str, href: str, key: str):
        cls = "active" if key == active else ""
        return f'<a class="{cls}" href="{href}">{label}</a>'

    home = item("Home", url_for("playground"), "home")

    if current_user.is_authenticated:
        usage_href = url_for("admin.admin_form") if getattr(
            current_user, "is_admin", False) else url_for("user.my_usage")
        usage = item("Usage", usage_href, "usage")
        auth = f'<a href="{url_for("auth.logout")}">Logout ({current_user.username})</a>'
    else:
        usage = item("Usage", url_for("auth.login"), "usage")
        auth = f'<a href="{url_for("auth.login")}">Login</a>'

    return f"""
    <style>
      .site {{ max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }}
      nav{{display:flex;gap:.5rem;align-items:center;margin-bottom:1rem}}
      nav a{{text-decoration:none;color:#1f2937;padding:.45rem .7rem;border-radius:8px;border:1px solid #e5e7eb}}
      nav a.active{{background:#eef2ff;color:#1f7aec;border-color:#c7d2fe}}
      nav .sp{{flex:1}}
    </style>
    <div class="site">
      <nav>
        {home}
        {usage}
        <span class="sp"></span>
        {auth}
      </nav>
    """
