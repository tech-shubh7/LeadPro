"""job leads: where each company was found, and the evidence a rejection saw

Revision ID: 5b8e2f41c7a9
Revises: 88250559fce1
Create Date: 2026-11-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5b8e2f41c7a9'
down_revision: Union[str, Sequence[str], None] = '88250559fce1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('job_leads', sa.Column('found_by', sa.String(length=40), nullable=True))
    op.add_column('job_leads', sa.Column('found_query', sa.Text(), nullable=True))
    op.add_column('job_leads', sa.Column('seen_evidence', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('job_leads', 'seen_evidence')
    op.drop_column('job_leads', 'found_query')
    op.drop_column('job_leads', 'found_by')
