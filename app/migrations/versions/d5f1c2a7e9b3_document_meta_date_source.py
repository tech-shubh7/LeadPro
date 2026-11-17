"""evidence.date_source: add document_meta (creation date embedded in a PDF, e.g. an RFP brief)

Revision ID: d5f1c2a7e9b3
Revises: a41c7e2d9b10
Create Date: 2026-11-15 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'd5f1c2a7e9b3'
down_revision: Union[str, Sequence[str], None] = 'a41c7e2d9b10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = ("date_source IN ('search_result', 'job_posting', 'article_meta', 'json_ld', 'url_path', 'html_markup', "
       "'official_notice')")
NEW = ("date_source IN ('search_result', 'job_posting', 'article_meta', 'json_ld', 'url_path', 'html_markup', "
       "'official_notice', 'document_meta')")


def upgrade() -> None:
    op.drop_constraint('ck_evidence_ck_datesource', 'evidence', type_='check')
    op.create_check_constraint('ck_evidence_ck_datesource', 'evidence', NEW)


def downgrade() -> None:
    op.execute("DELETE FROM evidence WHERE date_source = 'document_meta'")
    op.drop_constraint('ck_evidence_ck_datesource', 'evidence', type_='check')
    op.create_check_constraint('ck_evidence_ck_datesource', 'evidence', OLD)
