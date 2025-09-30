import pandas as pd
import pytest
from services import accounting as acc


@pytest.mark.db
def test_trial_balance_income_and_balance_sheet_from_manual_journal():
    # Build a tiny consistent journal: net=100, VAT=7 → gross=107, fully settled
    CA = acc._acc("Contract Asset (Unbilled A/R)")
    REV = acc._acc("Service Revenue")
    AR = acc._acc("Accounts Receivable")
    VAT = acc._acc("VAT Output Payable")
    CASH = acc._acc("Cash/Bank")

    def line(date, account_id, debit=0.0, credit=0.0, ref="R1", memo="t"):
        return {
            "date": date, "ref": ref, "memo": memo,
            "account_id": account_id,
            "account_name": acc._ACC[account_id]["name"],
            "account_type": acc._ACC[account_id]["type"],
            "debit": round(float(debit), 2),
            "credit": round(float(credit), 2),
        }

    j = pd.DataFrame([
        # Revenue recognition at period end: Dr CA 100 / Cr REV 100
        line("2025-01-31", CA,  debit=100.0),
        line("2025-01-31", REV, credit=100.0),
        # Issue: Dr AR 107 / Cr CA 100 / Cr VAT 7
        line("2025-01-12", AR,  debit=107.0),
        line("2025-01-12", CA,  credit=100.0),
        line("2025-01-12", VAT, credit=7.0),
        # Payment: Dr CASH 107 / Cr AR 107
        line("2025-01-25", CASH, debit=107.0),
        line("2025-01-25", AR,   credit=107.0),
    ])

    # Dr = Cr on the derived journal
    assert round(float(j["debit"].sum()), 2) == round(
        float(j["credit"].sum()), 2)

    tb = acc.trial_balance(j)
    assert isinstance(tb, pd.DataFrame) and not tb.empty
    assert tb.attrs.get("out_of_balance") == 0.0

    pnl = acc.income_statement(j)
    assert float(pnl.iloc[0]["Revenue"]) == 100.0
    assert float(pnl.iloc[0]["Expenses"]) == 0.0
    assert float(pnl.iloc[0]["Net_Income"]) == 100.0

    bs = acc.balance_sheet(j)
    check = float(bs.iloc[0]["Check(Assets - L-E)"])
    assert abs(check) < 0.01
