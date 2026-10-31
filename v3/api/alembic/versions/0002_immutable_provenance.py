"""Protect immutable audit, command results, events, and import provenance."""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

TABLES = ('audit_events', 'game_events', 'operations', 'legacy_records', 'legacy_imports')


def upgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("""CREATE FUNCTION v3_immutable_record() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'V3 immutable record cannot be changed'; END;
        $$""")
        for table in TABLES:
            op.execute(f'CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} '
                       'FOR EACH ROW EXECUTE FUNCTION v3_immutable_record()')
    elif op.get_bind().dialect.name == 'sqlite':
        for table in TABLES:
            for action in ('UPDATE', 'DELETE'):
                op.execute(f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'V3 immutable record cannot be changed'); END")


def downgrade():
    for table in TABLES:
        if op.get_bind().dialect.name == 'postgresql':
            op.execute(f'DROP TRIGGER IF EXISTS immutable_{table} ON {table}')
        else:
            for action in ('update', 'delete'):
                op.execute(f'DROP TRIGGER IF EXISTS immutable_{table}_{action}')
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP FUNCTION v3_immutable_record()')
