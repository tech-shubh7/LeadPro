"""job leads: watchlist status (near misses with a fresh signal, kept on their own sheet)

Revision ID: 7c4d9a1e2f60
Revises: 5b8e2f41c7a9
Create Date: 2026-11-18 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7c4d9a1e2f60'
down_revision: Union[str, Sequence[str], None] = '5b8e2f41c7a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME = 'ck_job_leads_ck_leadstatus'  # naming convention prefixes the table: ck_job_leads_ck_job_leads_...
OLD = "status IN ('candidate', 'qualified', 'qualified_no_contact', 'rejected', 'dropped_on_revalidation')"
NEW = ("status IN ('candidate', 'qualified', 'qualified_no_contact', 'rejected', 'dropped_on_revalidation', "
       "'watchlist')")


def upgrade() -> None:
    op.drop_constraint(NAME, 'job_leads', type_='check')
    op.create_check_constraint(NAME, 'job_leads', NEW)


def downgrade() -> None:
    op.execute("UPDATE job_leads SET status = 'rejected', reject_reason = 'watchlist (status removed)' "
               "WHERE status = 'watchlist'")
    op.drop_constraint(NAME, 'job_leads', type_='check')
    op.create_check_constraint(NAME, 'job_leads', OLD)
