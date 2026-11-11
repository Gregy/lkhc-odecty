"""drop rounds: readings/photos without round_id, point_skips per point and day, settlements without boundary_mode

Rounds ("obchůzky") were a stored session object copied from the spreadsheet. Everything that used
them is now derived from reading dates (raw per-day view, walk screen state, settlement helpers).

Data handling (upgrade):
- round_skips -> point_skips (meter_point_id, day = the round's date, note, created_by_id, created_at);
- the dropped rows (rounds with date/status/performer/note, settlements' boundary_mode) are kept as JSON
  in one audit_log entry (action "migrate_0003"), in addition to the automatic pre-migration snapshot;
- finalized settlements keep their snapshot (which records its own "mode") unchanged.

Downgrade re-creates the structure and derives one round per distinct reading date (status "closed").

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11 08:00:00

"""
import json
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        'point_skips',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meter_point_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['meter_point_id'], ['meter_points.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('meter_point_id', 'day', name='uq_point_skips_point_day'),
    )
    with op.batch_alter_table('point_skips', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_point_skips_meter_point_id'), ['meter_point_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_point_skips_day'), ['day'], unique=False)

    bind.execute(sa.text(
        "INSERT INTO point_skips (meter_point_id, day, note, created_by_id, created_at) "
        "SELECT s.meter_point_id, r.date, s.note, s.created_by_id, s.created_at "
        "FROM round_skips s JOIN rounds r ON r.id = s.round_id"
    ))

    rounds = [dict(r._mapping) for r in bind.execute(sa.text(
        "SELECT r.id, r.date, r.status, r.performed_by, r.note, r.user_id, "
        "(SELECT count(*) FROM readings x WHERE x.round_id = r.id) AS readings FROM rounds r ORDER BY r.date"
    ))]
    settlements = [dict(r._mapping) for r in bind.execute(sa.text(
        "SELECT id, name, status, period_start, period_end, boundary_mode FROM settlements ORDER BY id"
    ))]
    if rounds or settlements:
        bind.execute(
            sa.text(
                "INSERT INTO audit_log (at, user_id, user_email, action, entity_type, entity_id, changes, note) "
                "VALUES (:at, NULL, 'migrace', 'migrate_0003', 'rounds', NULL, :changes, :note)"
            ),
            {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S.000000"),
                "changes": json.dumps({"rounds": rounds, "settlements": settlements}, default=str, ensure_ascii=False),
                "note": "Obchůzky (rounds) zrušeny jako entita; přeskočená místa převedena do point_skips.",
            },
        )

    with op.batch_alter_table('readings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_readings_round_id'))
        batch_op.drop_column('round_id')
    with op.batch_alter_table('photos', schema=None) as batch_op:
        batch_op.drop_column('round_id')
    op.drop_table('round_skips')
    op.drop_table('rounds')
    with op.batch_alter_table('settlements', schema=None) as batch_op:
        batch_op.drop_constraint('ck_settlements_boundary_mode', type_='check')
        batch_op.drop_column('boundary_mode')


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table('settlements', schema=None) as batch_op:
        batch_op.add_column(sa.Column('boundary_mode', sa.String(length=16), nullable=False, server_default='months'))
        batch_op.create_check_constraint('ck_settlements_boundary_mode', "boundary_mode IN ('months', 'rounds')")
    op.create_table(
        'rounds',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('performed_by', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed')", name='ck_rounds_status'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('date'),
    )
    op.create_table(
        'round_skips',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('round_id', sa.Integer(), nullable=False),
        sa.Column('meter_point_id', sa.Integer(), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['meter_point_id'], ['meter_points.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['round_id'], ['rounds.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('round_id', 'meter_point_id', name='uq_round_skips_round_point'),
    )
    with op.batch_alter_table('photos', schema=None) as batch_op:
        batch_op.add_column(sa.Column('round_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_photos_round_id', 'rounds', ['round_id'], ['id'], ondelete='SET NULL')
    with op.batch_alter_table('readings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('round_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_readings_round_id', 'rounds', ['round_id'], ['id'], ondelete='SET NULL')
        batch_op.create_index(batch_op.f('ix_readings_round_id'), ['round_id'], unique=False)
    bind.execute(sa.text(
        "INSERT INTO rounds (date, performed_by, status, note) "
        "SELECT DISTINCT date(taken_at), '', 'closed', 'obnoveno z dat odečtů (downgrade 0003)' FROM readings"
    ))
    bind.execute(sa.text("UPDATE readings SET round_id = (SELECT id FROM rounds WHERE rounds.date = date(readings.taken_at))"))
    bind.execute(sa.text(
        "INSERT OR IGNORE INTO rounds (date, performed_by, status, note) "
        "SELECT DISTINCT day, '', 'closed', 'obnoveno z přeskočených míst (downgrade 0003)' FROM point_skips"
    ))
    bind.execute(sa.text(
        "INSERT INTO round_skips (round_id, meter_point_id, note, created_by_id, created_at) "
        "SELECT r.id, s.meter_point_id, s.note, s.created_by_id, s.created_at FROM point_skips s "
        "JOIN rounds r ON r.date = s.day"
    ))
    with op.batch_alter_table('point_skips', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_point_skips_day'))
        batch_op.drop_index(batch_op.f('ix_point_skips_meter_point_id'))
    op.drop_table('point_skips')
