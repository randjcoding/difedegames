# Auto-restore folder

Any `.sql` or `.sh` file placed in this folder is executed automatically by the
PostgreSQL container **the first time it starts with an empty data directory**
(the `dbdata` volume). This is how the database gets restored on the first run.

## Usage

1. Copy your backup here and name it so it sorts first, e.g.:

   ```bash
   cp /path/to/backup_pre_rebrand_YYYYMMDD_HHMMSS.sql deploy/initdb/01_restore.sql
   ```

2. Start the stack (see DEPLOY_ROCKY.md). The dump runs inside the
   `difedeappv2` database that the container creates from `.env`.

## Notes

- These scripts run ONLY on first initialization. If the `dbdata` volume already
  has data, they are ignored. To force a clean re-restore:
  `docker compose down -v` (this DELETES the database volume), then `up` again.
- Do not commit real backups to git; they may contain user data.
- `.gitignore` already excludes `deploy/initdb/*.sql`.
