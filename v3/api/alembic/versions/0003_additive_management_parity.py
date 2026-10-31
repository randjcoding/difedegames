"""additive_management_parity"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('account_action_tokens',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('purpose', sa.String(length=30), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('token_hash')
    )
    op.add_column('accounts', sa.Column('site_role', sa.String(length=16), server_default='member', nullable=False))
    op.add_column('accounts', sa.Column('is_owner', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('accounts', sa.Column('preferences', sa.JSON(), server_default='{}', nullable=False))
    op.add_column('accounts', sa.Column('details', sa.JSON(), server_default='{}', nullable=False))
    op.add_column('groups', sa.Column('settings', sa.JSON(), server_default='{}', nullable=False))
    op.add_column('people', sa.Column('details', sa.JSON(), server_default='{}', nullable=False))
    connection = op.get_bind()
    metadata = sa.MetaData()
    accounts = sa.Table('accounts', metadata, autoload_with=connection)
    people = sa.Table('people', metadata, autoload_with=connection)
    groups = sa.Table('groups', metadata, autoload_with=connection)
    provenance = sa.Table('legacy_records', metadata, autoload_with=connection)
    trusted = {row.source_key: row.payload for row in connection.execute(sa.select(
        provenance.c.source_key, provenance.c.payload).where(provenance.c.source_table == 'users'))}
    for row in connection.execute(sa.select(accounts)).mappings():
        source = trusted.get(str(row['id'])) or {}
        # Never infer privileges from mutable names/emails or a client-supplied legacy object.
        role = 'super_admin' if (source.get('role') == 'super_admin' and source.get('is_verified')
            and source.get('is_approved') and source.get('is_active') and row['verified']
            and row['approved'] and row['active']) else 'member'
        legacy = row['legacy'] or {}
        contact = {k: legacy.get(k) for k in ('address', 'city', 'state', 'zipcode', 'phone_number')}
        connection.execute(accounts.update().where(accounts.c.id == row['id']).values(site_role=role, details=contact))
    for row in connection.execute(sa.select(people)).mappings():
        legacy = row['legacy'] or {}
        connection.execute(people.update().where(people.c.id == row['id']).values(
            details={k: legacy.get(k) for k in ('first_name', 'last_name', 'email')}))
    for row in connection.execute(sa.select(groups)).mappings():
        legacy = row['legacy'] or {}
        connection.execute(groups.update().where(groups.c.id == row['id']).values(settings={
            'is_discoverable': legacy.get('is_discoverable', True), 'show_roster': legacy.get('show_roster', True)}))


def downgrade():
    raise RuntimeError('Rollback uses the previous API/web images while retaining this additive schema. Destructive downgrade is disabled.')
