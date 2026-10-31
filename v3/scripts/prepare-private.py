"""Create fresh V3-only credentials; refuses existing files and prints no secrets."""
import os
from pathlib import Path
import secrets
import sys

directory = Path(sys.argv[1]).resolve()
names = ('database.env', 'runtime.env', 'import.env', 'test.env', 'bootstrap.sql')
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
if directory.stat().st_uid != os.getuid():
    raise SystemExit('Private directory must belong to the current user')
directory.chmod(0o700)
if any((directory / name).exists() for name in names):
    raise SystemExit('Existing private configuration found; refusing overwrite')

cluster_password, app_password, reader_password = (secrets.token_hex(32) for _ in range(3))
target_url = f'postgresql+psycopg://difedegames_v3:{app_password}@db:5432/difedegames_v3'
source_name = 'difedegames_v3_legacy_snapshot_20261031'
files = {
    'database.env': f'POSTGRES_USER=v3_cluster_admin\nPOSTGRES_DB=postgres\nPOSTGRES_PASSWORD={cluster_password}\n',
    'runtime.env': f'DATABASE_URL={target_url}\nSECRET_KEY={secrets.token_hex(48)}\nSESSION_COOKIE_SECURE=true\nAPP_BASE_URL=https://games2.difedes.com\nALLOWED_ORIGINS=https://games2.difedes.com\n',
    'import.env': f'LEGACY_SOURCE_URL=postgresql+psycopg://v3_legacy_reader:{reader_password}@db:5432/{source_name}\n',
    'test.env': f'DATABASE_URL={target_url}_test\nSESSION_COOKIE_SECURE=false\nAPP_BASE_URL=http://localhost\nALLOWED_ORIGINS=http://localhost\n',
    'bootstrap.sql': f'''\\set ON_ERROR_STOP on
CREATE ROLE difedeapp NOLOGIN;
CREATE ROLE postgres NOLOGIN;
CREATE ROLE difedegames_v3 LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '{app_password}';
CREATE ROLE v3_legacy_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '{reader_password}';
ALTER ROLE v3_legacy_reader SET default_transaction_read_only = on;
CREATE DATABASE difedegames_v3 OWNER difedegames_v3;
CREATE DATABASE difedegames_v3_test OWNER difedegames_v3;
CREATE DATABASE {source_name} OWNER v3_cluster_admin;
REVOKE CONNECT ON DATABASE {source_name} FROM PUBLIC;
GRANT CONNECT ON DATABASE {source_name} TO v3_legacy_reader;
''',
}
os.umask(0o077)
for name, content in files.items():
    with (directory / name).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(content)
    (directory / name).chmod(0o600)
print('Created private V3 configuration; no secret values printed.')
