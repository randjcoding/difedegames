"""Destructive acceptance setup is restricted to the exact isolated test database."""
import os
import subprocess

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def test_engine():
    url = os.environ.get('DATABASE_URL', '')
    parsed = make_url(url)
    if (parsed.get_backend_name() != 'postgresql'
            or parsed.database != 'difedegames_v3_test'
            or os.environ.get('ACCEPTANCE_RESET_DATABASE') != 'difedegames_v3_test'):
        raise RuntimeError('Refusing reset: explicit exact isolated PostgreSQL test database required')
    return create_engine(url, pool_pre_ping=True)


def reset_schema():
    engine = test_engine()
    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA public CASCADE'))
        connection.execute(text('CREATE SCHEMA public'))
    subprocess.run(['alembic', 'upgrade', 'head'], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    return engine
