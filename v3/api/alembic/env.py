from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import create_engine, pool

from difede_api.db import Base
from difede_api import models

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
url = os.environ['DATABASE_URL']
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
