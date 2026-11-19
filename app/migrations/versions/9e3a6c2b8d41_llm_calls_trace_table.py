"""llm_calls: one row per model call (prompt, answer, tokens, price, time) for the Trace view

Revision ID: 9e3a6c2b8d41
Revises: 7c4d9a1e2f60
Create Date: 2026-11-19 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9e3a6c2b8d41'
down_revision: Union[str, Sequence[str], None] = '7c4d9a1e2f60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'llm_calls',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('stage', sa.String(length=30), nullable=False),
        sa.Column('channel', sa.String(length=40), nullable=False),
        sa.Column('domain', sa.String(length=255), nullable=False),
        sa.Column('provider', sa.String(length=20), nullable=False),
        sa.Column('model', sa.String(length=80), nullable=False),
        sa.Column('schema_name', sa.String(length=80), nullable=False),
        sa.Column('effort', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False),
        sa.Column('cached_tokens', sa.Integer(), nullable=False),
        sa.Column('output_tokens', sa.Integer(), nullable=False),
        sa.Column('reasoning_tokens', sa.Integer(), nullable=False),
        sa.Column('cost_inr', sa.Float(), nullable=False),
        sa.Column('from_cache', sa.Boolean(), nullable=False),
        sa.Column('instructions', sa.Text(), nullable=False),
        sa.Column('input_text', sa.Text(), nullable=False),
        sa.Column('output_text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_llm_calls_job_id_jobs'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_calls')),
    )
    op.create_index('ix_llm_calls_job_stage', 'llm_calls', ['job_id', 'stage'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_llm_calls_job_stage', table_name='llm_calls')
    op.drop_table('llm_calls')
