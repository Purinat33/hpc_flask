"""ensure idempotency partial unique index

Revision ID: e7b64f234205
Revises: 19915e2907b1
Create Date: 2025-09-10 12:14:39.487738

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b64f234205'
down_revision: Union[str, Sequence[str], None] = '19915e2907b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_index(
        "uq_payments_provider_idem_notnull",
        "payments",
        ["provider", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        if_not_exists=True,  # Alembic/SQLAlchemy supports this on PG
    )


def downgrade():
    op.drop_index("uq_payments_provider_idem_notnull", table_name="payments")
