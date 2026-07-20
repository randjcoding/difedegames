# Deploying DiFede Games in a Container on Rocky Linux

This guide takes you from a fresh Rocky Linux server to a fully running
**DiFede Games** app in containers, with the PostgreSQL database restored from a
backup, in one clean pass.

The stack is two containers managed by Docker Compose:

- `difede-games-db` - PostgreSQL 16 (data persisted in the `dbdata` volume)
- `difede-games-web` - the Flask + SocketIO app on port **5002**

There is also a **Podman** alternative at the end (Rocky ships Podman by default).

---

## 0. What you need

- A Rocky Linux 9 server with sudo access.
- Network access to reach the server on port **5002**.
- A database backup file (a `pg_dump` `.sql`). Create a fresh one from the old
  server (see Step 4).

---

## Files that are NOT in the repo (you must supply them)

For security, secrets and data are **excluded from git** (see `.gitignore`).
After cloning, the following do not exist and must be created on the new server:

| File | How to get it | Required? |
|------|---------------|-----------|
| `.env` | `cp .env.example .env`, then fill in real values (Step 3) | Yes |
| `deploy/initdb/01_restore.sql` | Copy a database backup here (Step 4) | Yes (for data) |
| `config_ai.py` | Only if you use the optional AI game-creator. Copy it from the old machine or recreate it with your own Anthropic API key. The core app does not import it. | No |

Never paste real passwords or API keys into any tracked file or into this
guide. Everything sensitive lives only in `.env` (and `config_ai.py` if used),
both of which git ignores.

---

## 1. Install Docker Engine + Compose on Rocky Linux

```bash
# Remove any old/community packages that conflict
sudo dnf remove -y podman-docker docker docker-common 2>/dev/null || true

# Add Docker's official repo
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo

# Install Docker Engine, CLI, and the Compose plugin
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Enable and start Docker
sudo systemctl enable --now docker

# (Optional) run docker without sudo - log out/in afterward
sudo usermod -aG docker "$USER"

# Verify
docker --version
docker compose version
```

---

## 2. Get the application code onto the server

Pick ONE method.

**A. From a git remote (once you push this project to GitHub/GitLab):**

```bash
cd /opt
sudo git clone <your-repo-url> difede-games
sudo chown -R "$USER":"$USER" difede-games
cd difede-games
```

**B. Copy the code tarball from the current server** (a fresh one was created at
`/home/joe/DiFedeAppV2_code_backup_*.tar.gz`):

```bash
# On the NEW server:
mkdir -p /opt/difede-games
# From your workstation, copy the tarball over, then:
tar -xzf DiFedeAppV2_code_backup_*.tar.gz -C /opt
mv /opt/DiFedeAppV2 /opt/difede-games 2>/dev/null || true
cd /opt/difede-games   # (or /opt/DiFedeAppV2, wherever it extracted)
```

You should now be in the project folder that contains `docker-compose.yml`.

---

## 3. Create your `.env`

```bash
cp .env.example .env

# Generate a strong SECRET_KEY:
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Edit `.env` and set:

- `PG_PASSWORD` - a strong password (this becomes the DB password).
- `SECRET_KEY` - paste the value you generated.
- `EMAIL_PASSWORD` - optional Gmail app password (leave blank to disable email).

> Keep `PG_USER=difedeapp` and `PG_DATABASE=difedeappv2`. The backup's objects
> are owned by `difedeapp`; matching the name avoids ownership/permission errors.

---

## 4. Get a database backup and stage it for auto-restore

**On the OLD/source server**, create a fresh dump:

```bash
PGPASSWORD=Password pg_dump -h localhost -U difedeapp -d difedeappv2 \
  > backup_difede_$(date +%Y%m%d_%H%M%S).sql
```

(Or reuse the one already created: `backup_pre_rebrand_*.sql`.)

**On the NEW server**, copy that file into the auto-restore folder. The Postgres
container runs anything in `deploy/initdb/` the first time it initializes:

```bash
mkdir -p deploy/initdb
cp /path/to/backup_difede_YYYYMMDD_HHMMSS.sql deploy/initdb/01_restore.sql
```

That's it - the restore happens automatically on first `up`.

---

## 5. Build and start

```bash
docker compose up -d --build
```

First run will: build the web image, start Postgres, create the `difedeappv2`
database, run `deploy/initdb/01_restore.sql` to load your data, then start the
web app once the DB is healthy.

Watch it come up:

```bash
docker compose ps
docker compose logs -f web      # Ctrl-C to stop following
docker compose logs db | tail -n 40
```

---

## 6. Open the firewall (firewalld)

```bash
sudo firewall-cmd --permanent --add-port=5002/tcp
sudo firewall-cmd --reload
```

Then browse to: `http://<server-ip>:5002`

---

## 7. Verify

```bash
# App responds
curl -s -o /dev/null -w "web: %{http_code}\n" http://localhost:5002/

# Data restored (should print the game count, e.g. 11)
docker compose exec db psql -U difedeapp -d difedeappv2 -c "SELECT COUNT(*) FROM games;"
```

Log in with your existing admin account (`joe_71@yahoo.com`) - the users table
came over in the restore.

---

## Day-2 operations

**Scale web replicas (traffic / SocketIO-safe):**

The stack includes Redis + an nginx proxy on `127.0.0.1:5002`. Scale only the `web` service:

```bash
cd /home/joe/difedegames

# 2 or 3 app pods behind the proxy (tunnel URL stays localhost:5002)
docker compose up -d --scale web=2
# or
docker compose up -d --scale web=3

docker compose ps
```

Do **not** scale `db`, `redis`, or `proxy`. Cloudflare still points at `localhost:5002`.

**Update the app after code changes:**

```bash
git pull            # or re-copy the code
docker compose up -d --build --scale web=2
```

**Back up the containerized database:**

```bash
docker compose exec -T db pg_dump -U difedeapp difedeappv2 \
  > backup_$(date +%Y%m%d_%H%M%S).sql
```

**Manual restore into an already-running DB** (alternative to the auto-restore
folder - note this restores into the existing database):

```bash
docker compose exec -T db psql -U difedeapp -d difedeappv2 < your_backup.sql
```

**Wipe everything and re-restore from scratch** (DELETES the DB volume):

```bash
docker compose down -v
cp /path/to/backup.sql deploy/initdb/01_restore.sql
docker compose up -d --build
```

**Stop / start:**

```bash
docker compose stop
docker compose start
docker compose down        # stop and remove containers (keeps volumes/data)
```

---

## SELinux note (Rocky runs SELinux enforcing)

Bind-mounted host folders must be labeled for containers to read them. The
compose file already uses the `:ro,Z` flag on `deploy/initdb`, which relabels it
automatically. If you add other bind mounts, append `:Z` (private) or `:z`
(shared) to them, or you'll get "permission denied" inside the container.
Named volumes (`dbdata`, `sessions`) are handled automatically.

---

## Podman alternative (no Docker install)

Rocky ships Podman. To use it with this same compose file:

```bash
sudo dnf install -y podman podman-compose

# Rootful (simplest for binding port 5002):
sudo podman-compose up -d --build

# Verify
sudo podman ps
sudo podman-compose logs -f web
```

The `:Z` SELinux flags and the `deploy/initdb` auto-restore work the same under
Podman. Firewall and verification steps are identical.

To auto-start on boot with Podman, generate a systemd unit:

```bash
sudo podman generate systemd --new --files --name difede-games-web
# then install the generated unit under /etc/systemd/system and enable it
```

---

## Troubleshooting

- **Web keeps restarting / can't reach DB:** check `docker compose logs web`.
  The entrypoint waits up to 120s for the DB; if the DB failed to init, fix the
  DB first (`docker compose logs db`).
- **Restore didn't happen:** the `deploy/initdb` scripts only run on a *fresh*
  `dbdata` volume. If you started once before adding the backup, run
  `docker compose down -v` and `up` again.
- **Password/permission errors during restore:** ensure `PG_USER=difedeapp` and
  `PG_DATABASE=difedeappv2` in `.env` match the dump.
- **Port 5002 in use:** change the left side of `"5002:5002"` in
  `docker-compose.yml` (e.g. `"8080:5002"`).
