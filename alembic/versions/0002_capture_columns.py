"""capture: photo source / reported_by, reading client_ref (idempotent offline sync)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10 21:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('photos', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=16), server_default='upload', nullable=False))
        batch_op.add_column(sa.Column('reported_by', sa.String(length=200), server_default='', nullable=False))
    with op.batch_alter_table('readings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('client_ref', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_readings_client_ref'), ['client_ref'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('readings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_readings_client_ref'))
        batch_op.drop_column('client_ref')
    with op.batch_alter_table('photos', schema=None) as batch_op:
        batch_op.drop_column('reported_by')
        batch_op.drop_column('source')
