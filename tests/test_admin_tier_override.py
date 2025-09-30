# tests/test_admin_tier_override.py
import pytest
from models.users_db import create_user
from models.tiers_store import load_overrides_dict, bulk_save


@pytest.mark.db
def test_admin_tiers_post_sets_and_updates_overrides(client, admin_user):
    # Ensure two normal users exist
    for u in ("alice", "bob"):
        try:
            create_user(u, "pw", role="user")
        except Exception:
            pass

    # Seed existing override for bob that the admin form should UPDATE to 'mu'
    bulk_save([("bob", "private")])
    assert load_overrides_dict().get("bob") == "private"

    # Submit admin tiers form:
    # - alice -> gov  (create/overwrite override to gov)
    # - bob   -> mu   (implementation sets explicit override to 'mu', it does not auto-clear)
    resp = client.post(
        "/admin/tiers",
        data={"tier_alice": "gov", "tier_bob": "mu"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302)

    ov = load_overrides_dict()
    assert ov.get("alice") == "gov"   # override set
    assert ov.get("bob") == "mu"      # override updated (not cleared)


@pytest.mark.db
def test_admin_tiers_rejects_invalid_values(client, admin_user):
    try:
        create_user("carol", "pw", role="user")
    except Exception:
        pass

    before = load_overrides_dict().copy()

    # Invalid desired tier should be ignored server-side
    r = client.post(
        "/admin/tiers",
        data={"tier_carol": "not-a-tier"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 302)

    after = load_overrides_dict()
    # No new invalid override should be written
    assert after == before
