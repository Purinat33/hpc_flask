# tests/test_tier_store_unit.py
import pytest
from models.tiers_store import load_overrides_dict, bulk_save, clear_override


@pytest.mark.db
def test_bulk_save_and_clear_roundtrip_is_idempotent():
    # start clean (ignore errors if already absent)
    for u in ("u1", "u2", "u3"):
        try:
            clear_override(u)
        except Exception:
            pass

    # create two overrides
    bulk_save([("u1", "gov"), ("u2", "private")])
    ov = load_overrides_dict()
    assert ov.get("u1") == "gov"
    assert ov.get("u2") == "private"

    # overwrite one and add a new one
    bulk_save([("u1", "mu"), ("u3", "gov")])
    ov2 = load_overrides_dict()
    assert ov2.get("u1") == "mu"
    assert ov2.get("u2") == "private"
    assert ov2.get("u3") == "gov"

    # clearing works and is idempotent
    clear_override("u2")
    assert "u2" not in load_overrides_dict()
    clear_override("u2")  # second clear is a no-op
    assert "u2" not in load_overrides_dict()


@pytest.mark.db
def test_bulk_save_empty_is_noop():
    before = load_overrides_dict().copy()
    bulk_save([])  # should not raise or change anything
    after = load_overrides_dict()
    assert after == before
