# V3 feature restoration and appearance architecture

This phase restores the capabilities requested after the pilot. It changes only V3. V2 stays online, and neither database is reimported or synchronized. The pre-change recovery point is `recovery-v3-pre-parity-20261031T141257Z` at `e952d0f`; its fresh database backup includes post-launch changes to game 606 and has passed an isolated restore comparison.

## One application, five designs

All routes, permissions, forms, validation, score queues, statistics and management features are shared. Appearance is a presentation choice, never a separate application or a feature flag.

- Classic: familiar V2 navigation, navy/blue/purple cards and clear compact sections; default.
- Arcade: bold type, playful accents and energetic framing.
- Scoreboard: compact, tabular, sports-inspired presentation.
- Clubhouse: the pilot's quieter editorial type and composition, retained as an option.
- Playground: friendly rounded shapes and cheerful visual emphasis.

Palette is independent of design: DiFede Dark, Light, Midnight Blue, Espresso, Emerald Forest, Rose Wine, Sunset Blaze, Arctic Frost and Royal Purple. Default Classic/Midnight Blue. Central typed registries and semantic CSS variables own the allowed choices; shared components consume those variables. Reuse Radix primitives for accessible interactions. Avoid adding a second styling framework just to supply preset colors.

Preferences and favorites belong to the signed-in account, persist on the server, and have an account-scoped local boot cache. Login/logout must not leak another account's choices or favorites. Future features use the same shared page shell and controls and therefore appear in every design automatically. A style is more than a palette: typography, section framing, density and card treatment differ while score entry keeps safe touch targets.

## Navigation and discoverability

Home brings back meaningful statistics, Hall of Fame and Hall of Shame, resume/watch, and the latest game's full results. Dedicated leaderboards, history, people/groups, account and role-gated administration remain available in every design. Growing lists have labeled search, meaningful filters, clear reset/empty states and bounded pagination. Favorite game types and people sort first without hiding the rest. Filters survive navigation through URL state when practical.

Read-only sheets use compact rows and natural page height, not giant rows inside a vertical scroll box. Two- and three-person sheets should fit phone widths. Wide seven-person sheets may scroll horizontally with sticky labels; entering scores always remains one-round-at-a-time with comfortable targets. Preserve zero versus missing, queued drafts, conflicts, idempotent retries and completion review.

## Statistics semantics and privacy

Normal users see games already authorized by participant/scorekeeper/organizer access or explicitly shared groups. Site superadmins may inspect all games. All-time means all dates, not permission to reveal private groups. Scope is shown in the interface.

Only completed, non-deleted games contribute official wins. Missing historical result records remain unknown. Current recorded results are authoritative; original imported result records remain immutable provenance after an explicit correction. Shared-win credit belongs only to the actual tied winners. Streaks follow each person's own sequence of played completed games; unrelated games do not interrupt them. Expose unknown outcomes rather than silently counting them as losses.

Compare wins and participation across game types; compare raw scores only within a compatible game, mode and scoring direction. Exact Basic game titles can be filtered separately. Include type leaders, best/worst compatible scores, biggest compatible winning margins, worst Five Crowns hand and Trouble strength of win with source-game links. Search and filtering must not widen authorization.

## Management and corrections

Restore trusted legacy superadmin roles through an additive migration. Runtime authorization uses normalized persisted roles, not a special-case email string. Site-owner role management is distinct from superadmin and requires an explicit operator bootstrap matching the preserved owner's identity. Protect the owner from accidental lockout.

Group leads manage their own roster and invitations; people manage their own display/profile and password. Account actions must preserve player identity and score history. Password changes and resets revoke sessions. Recovery tokens are hashed, expiring and single-use; no test messages are sent to real users.

Historical corrections require a reason, current revision and durable operation ID. Lock and validate the complete change before writing scores, official result, audit and event atomically. Preserve original imported data. Permission failures, stale revisions and invalid specialty scoring must leave all records unchanged.

## Release gates

API permission and computation tests; frontend unit/type/build checks; browser checks at 375/390px and desktop with all five designs; a real PostgreSQL migration rehearsal on the verified private clone; data reconciliation before/after additive migration; scoped V3 deployment; public health/sign-in checks; unchanged V2 container/runtime evidence. Record actual evidence and remaining limitations without claiming that automated tests replace a real iPhone game night.

Implementation contracts: `PARITY_API.md` for account/group/admin/favorites; `ANALYTICS_API.md` for statistics. The existing game command and draft contracts remain in `CONTRACT.md`.
