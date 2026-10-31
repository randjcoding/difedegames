import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


def make_engine(url=None):
    url = url or os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL is required; V3 never falls back to a V2 database')
    kwargs = {'pool_pre_ping': True, 'hide_parameters': True}
    if url.startswith('sqlite'):
        kwargs['connect_args'] = {'check_same_thread': False, 'timeout': 30}
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def sqlite_foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
    return engine


def make_sessions(engine):
    return sessionmaker(engine, expire_on_commit=False)
