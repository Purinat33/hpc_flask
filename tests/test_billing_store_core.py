import numbers
import pytest
from datetime import datetime, timezone

from sqlalchemy import Numeric
from models.base import session_scope
from models.schema import Receipt
from models import billing_store as bs


def _dt(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


@pytest.mark.db
def test_admin_list_receipts_filters_and_shapes():
    # 1 paid, 1 pending
    with session_scope() as s:
        r1 = Receipt(username="alice", pricing_tier="mu",
                     rate_cpu=0, rate_gpu=0, rate_mem=0,
                     rates_locked_at=_dt(2025, 1, 10),
                     start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
                     created_at=_dt(2025, 1, 5), paid_at=_dt(2025, 1, 9),
                     total=100.0, status="paid")
        r2 = Receipt(username="bob", pricing_tier="mu",
                     rate_cpu=0, rate_gpu=0, rate_mem=0,
                     rates_locked_at=_dt(2025, 1, 10),
                     start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
                     created_at=_dt(2025, 1, 10),
                     total=200.0, status="pending")
        s.add_all([r1, r2])
        s.flush()

    all_rows = bs.admin_list_receipts(status=None)
    assert isinstance(all_rows, list) and len(all_rows) >= 2
    assert {r["status"] for r in all_rows} >= {"paid", "pending"}

    paid_rows = bs.admin_list_receipts(status="paid")
    assert all(r["status"] == "paid" for r in paid_rows)


@pytest.mark.db
def test_tax_cfg_tuple_shape_and_types(monkeypatch):
    # We just assert the tuple shape/types so we don’t depend on exact env names here.
    enabled, label, rate, inclusive = bs._tax_cfg()
    assert isinstance(enabled, (bool, int))
    assert isinstance(label, (str, type(None)))
    assert isinstance(rate, numbers.Number)
    assert isinstance(inclusive, (bool, int))
