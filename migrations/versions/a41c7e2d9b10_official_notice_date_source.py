"""evidence.date_source: add official_notice (government open-data publication dates)

Revision ID: a41c7e2d9b10
Revises: bffbd3780c78
Create Date: 2026-11-01 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a41c7e2d9b10'
down_revision: Union[str, Sequence[str], None] = 'bffbd3780c78'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "date_source IN ('search_result', 'job_posting', 'article_meta', 'json_ld', 'url_path', 'html_markup')"
NEW = "date_source IN ('search_result', 'job_posting', 'article_meta', 'json_ld', 'url_path', 'html_markup', 'official_notice')"


def upgrade() -> None:
    op.drop_constraint('ck_evidence_ck_datesource', 'evidence', type_='check')
    op.create_check_constraint('ck_evidence_ck_datesource', 'evidence', NEW)


def downgrade() -> None:
    op.execute("DELETE FROM evidence WHERE date_source = 'official_notice'")
    op.drop_constraint('ck_evidence_ck_datesource', 'evidence', type_='check')
    op.create_check_constraint('ck_evidence_ck_datesource', 'evidence', OLD)
