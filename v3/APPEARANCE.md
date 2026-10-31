# Shared features and appearance

V3 is one application. Classic, Arcade, Scoreboard, Clubhouse and Playground are presentation choices over the same routes, permissions, forms, policies and score queue.

- Put a feature page in `web/src/pages`, register its route once in `App.tsx`, and use `core.tsx`, `ListTools.tsx`, `CompactSheet.tsx`, `SpecialEntry.tsx` and the other shared feature components.
- Keep authenticated requests, authorization, validation, game lifecycle and draft handling outside style-specific code. Never add style-specific API requests or permissions.
- Add design metadata to `web/src/appearance.ts`; add presentation rules under its `data-style` selector in `appearance.css`. Designs may change typography, framing, navigation and density, while interactive targets and score reliability remain shared.
- Add a palette to the same typed registry. Palette maps supply semantic surface, text, muted, border, action, link and feedback colors. Features consume those tokens rather than hardcoded theme colors. `--accent` is an action background; `--link` is readable accent text.
- The default is Classic / Midnight Blue. Layout and palette are independent. The account preference API persists them, an account-scoped local cache paints before the next request, and cross-tab changes stay in that account's namespace. Anonymous choices do not inherit another account's preferences.
- Appearance previews are reversible. Saving blocks dismissal until the request finishes; Cancel restores the pre-preview appearance. A shared `style` / `palette` URL remains a preview through account boot and becomes an account preference only when saved.
- Small read-only score cells are separate from touch entry. Preserve full names, blank versus zero, stable inputs, pending drafts and revision checks in every design.

Verification covers all five designs and nine palettes, phone widths of 375/390px, desktop, compact two-/three-/seven-player sheets, favorite filtering and preferences, role-gated management, authentication recovery and the shared scoring checks. Automated browser tests do not replace physical iPhone Safari validation.
