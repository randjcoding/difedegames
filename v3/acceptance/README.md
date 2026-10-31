# Independent V3 acceptance

These checks complement the backend's SQLite HTTP/unit tests. `postgres_http.py`
uses actual HTTP against two Uvicorn workers and PostgreSQL, with deterministic
synthetic accounts only in `difedegames_v3_test`. It tests concurrent same-cell,
unrelated-cell, duplicate-operation and completion/edit races; permissions;
zero/null/ties/trailing blanks; invitation claims; actual open-stream logout
revocation; every catalog game/mode; and production secure-cookie headers.

`import_rehearsal.py` restores normalized data into that same exact test database
from the retained isolated read-only source, independently reconciles table/row,
identity/password-hash, completion and seat-order counts, and confirms that a
second import is refused. It prints only aggregate checks, never records/hashes.

Both scripts RESET the exact test database. Their shared guard requires a
PostgreSQL URL naming `difedegames_v3_test` and explicit
`ACCEPTANCE_RESET_DATABASE=difedegames_v3_test`. Coordinate temporal ownership with
the backend worker before running. Never use pilot runtime.env as the test URL.
Run import rehearsal first, then HTTP acceptance (which resets fixtures).

Use the reviewed API image, private test/import envs, only the V3 internal data
network, no published ports, conservative limits, and this folder mounted read-only:

```sh
docker run --name <new-exact-acceptance-container> \
  --network difedegames-v3_data --memory 512m --cpus 1 \
  --env-file /home/joe/.config/difedegames-v3/test.env \
  --env-file /home/joe/.config/difedegames-v3/import.env \
  -e ACCEPTANCE_RESET_DATABASE=difedegames_v3_test \
  -v /home/joe/difedegames-v3/v3/acceptance:/acceptance:ro,Z \
  --entrypoint python difedegames-v3-api:pilot /acceptance/import_rehearsal.py
```

Use a new exact container name for `postgres_http.py` after import rehearsal.
Reports are stdout JSON, suitable for private backup evidence. Remove only the
exact acceptance containers after recording results; retain source/pilot volumes.

`deployed_smoke.py --base-url https://games2.difedes.com` makes read-only anonymous
HTTP requests after accepted cutover: app/SPA/API routing, database health,
security headers, missing-static 404/cache behavior, and anonymous account denial.
It creates no users, games, or real-password sessions. Authenticated browser/mobile
behavior and live Cloudflare interactions remain separate validation steps.
