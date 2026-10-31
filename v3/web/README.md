# V3 web pilot

React / TypeScript / Vite client for the API in `../CONTRACT.md`. All API traffic uses same-origin `/api`; development proxies to `127.0.0.1:8000`. Use `http://localhost:5173` when running with the synthetic API fixture's origin allowlist.

```
npm ci
npm run dev -- --port 5173
npm run build
npm test
npm run test:browser
```

The build generates 768px and 1536px WebP derivatives from `public/art/*.png`, preserving originals. It outputs static files in `dist`; deployment must serve `index.html` for SPA routes and proxy `/api` to FastAPI, with SSE buffering disabled. There are no required external fonts or runtime third-party requests.

Browser checks use installed Chrome by default (`PLAYWRIGHT_CHANNEL` can override), covering 390px, 375px, landscape and desktop. They mock API state to deterministically test draft persistence, competing revisions, access revocation, missing-score navigation and specialty forms. They do not replace physical iPhone Safari validation.

`node scripts/smoke-local.mjs` and `node scripts/smoke-all-games.mjs` exercise the actual local synthetic API fixture, including writes. They reject non-localhost URLs and must only run against the disposable fixture configured by the API developer. `PILOT_BASE_URL` selects the local origin. They use the synthetic `person1@example.com` / `ExamplePass!123` account and are never production data migration tools.

Score drafts retain their original per-cell base version and operation ID in localStorage keyed by signed-in account, game and cell. Saved indicators require acknowledgment. Conflicts require explicit review; failed refresh cannot discard a draft. Completion captures and displays a fixed reviewed revision and invalidates confirmation on newer snapshots. Revoked access hides private game content and prevents queued writes. Specialty result drafts remain local until acknowledged finish; ended-early local details remain available for later corrections.

All ten catalog policies use one lifecycle/synchronization layer. Trouble, Kings single-round and Basic winner mode have distinct result forms. No board photo recognition or automatic Skyjo/Dutch Blitz rule calculator is claimed.
