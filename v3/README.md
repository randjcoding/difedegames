# V3 preview foundation

This independent static preview is under development and does not accept scores.
Current games remain at https://games.difedes.com (V2). Keep V2 running in tandem
until the user explicitly requests its retirement.

## Deployment

Use the separate checkout `/home/joe/difedegames-v3`, branch `codex/v3-preview`.
Run these commands from that checkout. Always specify both the V3 project and
file; the repository root's legacy Compose file belongs to V2.

```sh
docker compose --project-name difedegames-v3 -f v3/compose.yaml config --quiet
docker compose --project-name difedegames-v3 -f v3/compose.yaml run --rm --no-deps preview -t
docker compose --project-name difedegames-v3 -f v3/compose.yaml up -d --pull never
curl -fsS http://127.0.0.1:5003/healthz
```

The image is pinned to the existing Rocky Nginx image digest; no build or pull is
needed. On another host, provision that exact image deliberately before starting.
The preview runs as UID/GID 101 with a read-only root filesystem, a small temporary
filesystem, dropped capabilities, limited resources, and read-only V3-only mounts.
Its dedicated internal network has no V2 connections. Only host loopback port
5003 is published; Nginx listens on 8080 inside the container.

The user manages the Cloudflare route `games2.difedes.com` to
`http://localhost:5003`. These files do not configure DNS or a tunnel.

## Stop only this preview

```sh
docker compose --project-name difedegames-v3 -f v3/compose.yaml down
```

Run from `/home/joe/difedegames-v3`. This removes only the V3 preview container and
its project network. Never run root legacy Compose commands for this preview.

## Boundaries for future work

There is no V3 database, authentication, score entry, or data migration yet.
Future services must use a separate V3 database/user (suggested `difedegames_v3`),
fresh V3 volumes, distinct session cookies, and new secrets. Do not reuse V2
database volumes, credentials, or session secrets. Keep all runtime secrets out of
Git. The root `.dockerignore` excludes `v3/` on this branch so the preview files do
not enter a future legacy application build context.
