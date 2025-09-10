"""fix receipt_items PK -> (receipt_id, job_key)

Revision ID: 19915e2907b1
Revises: 27631cf6e931
Create Date: 2025-09-10 12:05:26.039280

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '19915e2907b1'
down_revision: Union[str, Sequence[str], None] = '27631cf6e931'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("receipt_items_pkey", "receipt_items", type_="primary")
    # Create composite PK (receipt_id, job_key)
    op.create_primary_key(
        "pk_receipt_items",
        "receipt_items",
        ["receipt_id", "job_key"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("pk_receipt_items", "receipt_items", type_="primary")
    op.create_primary_key("receipt_items_pkey",
                          "receipt_items", ["receipt_id"])
    # If you added uq in upgrade(), also drop it here:
    # op.drop_constraint("uq_receipt_items_job_key", "receipt_items", type_="unique")
