"""saved_setups"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone
import json


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('saved_game_layouts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('group_id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=60), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('player_ids', sa.JSON(), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('scoring_direction', sa.String(length=8), nullable=True),
    sa.Column('target_score', sa.Integer(), nullable=True),
    sa.Column('title', sa.String(length=160), nullable=True),
    sa.Column('is_default', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('legacy', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('group_id', 'slug', 'name')
    )
    op.create_index('one_default_saved_layout', 'saved_game_layouts', ['group_id', 'slug'], unique=True, postgresql_where=sa.text('is_default = true'), sqlite_where=sa.text('is_default = 1'))
    connection = op.get_bind()
    metadata = sa.MetaData()
    provenance = sa.Table('legacy_records', metadata, autoload_with=connection)
    target = sa.Table('saved_game_layouts', metadata, autoload_with=connection)
    definitions = {row.source_key: row.payload for row in connection.execute(sa.select(
        provenance.c.source_key, provenance.c.payload).where(provenance.c.source_table == 'games'))}
    accounts = sa.Table('accounts', metadata, autoload_with=connection)
    account_ids = set(connection.scalars(sa.select(accounts.c.id)))
    for row in connection.execute(sa.select(provenance.c.payload).where(provenance.c.source_table == 'game_layouts')):
        source = row.payload
        definition = definitions.get(str(source.get('game_id'))) or {}
        players = source.get('player_ids') or []
        if isinstance(players, str):
            players = json.loads(players)
        def timestamp(value):
            return datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
        creator = source.get('created_by_user_id')
        direction = source.get('scoring_direction')
        slug = definition.get('slug') or 'legacy-' + str(source.get('game_id'))
        mode = 'winner' if direction == 'manual' else 'single' if direction == 'single_round' or slug == 'trouble' else 'rounds'
        connection.execute(target.insert().values(id=source['id'], group_id=source['family_id'],
            slug=slug, name=source['name'], player_ids=players,
            mode=mode, scoring_direction='high' if direction == 'high_wins' or mode == 'single' else 'low' if direction == 'low_wins' else None,
            target_score=source.get('target_score'), title=None, is_default=bool(source.get('is_default')),
            created_by=creator if creator in account_ids else None, created_at=timestamp(source.get('created_at')),
            updated_at=timestamp(source.get('updated_at')), legacy=source))
    if connection.dialect.name == 'postgresql':
        connection.execute(sa.text("SELECT setval(pg_get_serial_sequence('saved_game_layouts','id'), "
            "COALESCE((SELECT MAX(id) FROM saved_game_layouts),1), EXISTS(SELECT 1 FROM saved_game_layouts))"))


def downgrade():
    raise RuntimeError('Rollback retains additive schema and saved setups; deploy the previous API/web images instead.')
