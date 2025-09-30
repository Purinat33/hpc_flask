import pytest
from models.base import session_scope
from models.gl import AccountingPeriod
from services.gl_posting import close_period, reopen_period
from services.accounting_export import run_formal_gl_export


@pytest.mark.db
def test_close_and_reopen_empty_period_and_export_noop():
    actor = "pytest"
    # Close an empty month
    assert close_period(2025, 3, actor) is True
    with session_scope() as s:
        p = s.query(AccountingPeriod).filter_by(year=2025, month=3).one()
        assert p.status == "closed"

    # Reopen it
    assert reopen_period(2025, 3, actor) is True
    with session_scope() as s:
        p = s.query(AccountingPeriod).filter_by(year=2025, month=3).one()
        assert p.status == "open"

    # With no posted lines in March, the formal export should be a NOOP
    fname, blob = run_formal_gl_export("2025-03-01", "2025-03-31", actor)
    assert fname is None and blob is None
