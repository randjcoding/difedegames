# V3 additive parity API

Existing gameplay endpoints, score versions, operation IDs, SSE and imported results remain compatible. All mutations require the existing CSRF header and same-origin session. JSON errors retain `detail` and optional `code`. Existing V3 rows are never reimported or reset.

Game detail/list adds `result_source:"v2-original"|"v3-completion"|"v3-correction"|null`. Use original recorded winning points for unmodified `v2-original` history; corrected/current official results come from current standings. `imported_result` always remains the separate immutable original record.

## Identity and preferences

`GET /api/me` retains its current response and adds `user.display_name` (preferred person name, fallback account name), `user.site_role:"member"|"super_admin"`, `user.is_owner:boolean`, and `preferences:{style:string,palette:string,favorite_game_slugs:string[],favorite_person_ids:number[]}` at response top level. `user.name` keeps its account-name meaning. Defaults are `style:"classic"`, `palette:"midnight-blue"`, empty favorites. Styles: classic, arcade, scoreboard, clubhouse, playground. Palettes: difede-dark, classic-light, midnight-blue, espresso, emerald-forest, rose-wine, sunset-blaze, arctic-frost, royal-purple. Preferences are account-specific.

- `PATCH /api/preferences` accepts any subset of that preferences object; returns `{preferences:...}`. Favorite people must be visible to the caller; game slugs must exist in the catalog.
- `GET /api/profile` -> `{user:{id,email,name,person_id,site_role,is_owner},person:{id,name,first_name,last_name,email,is_discoverable,show_full_last_name,is_minor}|null,contact:{address,city,state,zipcode,phone_number},groups:Group[]}`. Private legal/contact fields are only returned to self or explicit administrator detail routes.
- `PATCH /api/profile` accepts `{name?,first_name?,last_name?,person_email?,is_discoverable?,show_full_last_name?,contact?:{address?,city?,state?,zipcode?,phone_number?}}`; returns the profile. `name` edits the person's preferred display name, not the separate account's legal name. Nullable `person_email:null` explicitly clears that field; omit unchanged fields. Login email changes use administrator identity repair, not an unchecked self-service field.
- `POST /api/auth/password` `{current_password,new_password}` -> `{ok:true}`. Verify either supported legacy hash format; revoke every existing session, including the current one. Sign in again afterward.
- `POST /api/auth/password-reset` `{email}` -> generic `{ok:true,message:string}`; never disclose account existence. Delivery depends on configured mail transport; do not claim delivery when unavailable.
- `GET /api/auth/password-reset/{token}` -> `{valid:true}` or 404; `POST` `{password}` -> `{ok:true}`. Tokens are opaque, hashed in DB, single-use and expire after one hour; reset revokes every session.

## Administrator and audit

Site role is migrated from preserved trusted legacy user records matched to existing account IDs; no runtime email allowlist. Owner capability is separately bootstrapped by an operator using `OWNER_EMAIL` from private configuration, matching an existing active verified approved legacy superadmin. Ordinary users cannot set either capability. Owners cannot be deactivated, deleted or demoted through another administrator; role changes require owner capability. Self-deactivation is rejected.

- `GET /api/admin/overview` -> `{counts:{people,accounts,groups,games,active_games,completed_games,incomplete_games},pending_accounts:number}`.
- `GET /api/admin/accounts?q=&include_archived=false&limit=50&offset=0` -> `{accounts:[{id,email,name,person_id,site_role,is_owner,active,verified,approved,group_ids:number[]}],total}`.
- `PATCH /api/admin/accounts/{id}` `{name?,email?,active?,verified?,approved?,site_role?}` -> account. Role changes are owner-only; account disabling revokes sessions.
- `POST /api/admin/accounts/{id}/reset-link` `{}` -> `{url,expires_at}`. Owner accounts may only be targeted by the owner. This returns a manual link without pretending email was sent.
- `DELETE /api/admin/accounts/{id}` `{reason}` -> `{ok:true}`. Deactivate/retain account identity rather than cascading through history; owner/self protections apply.
- `GET /api/admin/people?q=&include_archived=false&limit=50&offset=0` -> `{people:[Person plus first_name,last_name,email,archived,purged,privacy,account status,group_ids],total}`.
- `GET /api/admin/groups?q=&include_archived=false&limit=50&offset=0` -> `{groups:[Group plus archived,settings,member_count],total}`.
- `GET /api/admin/audit?game_id=&actor_id=&limit=50&offset=0` -> `{events:[{id,game_id,actor_id,action,payload,created_at}],total}`; never expose password hashes, token secrets or unrelated private account fields through audit payloads.
- `GET /api/games/{id}/audit` -> same event shape, visible to game managers. `GET /api/admin/games/{id}` -> full game plus `{deleted,organizer_id,rules_version}`.
- `POST /api/admin/games/{id}/corrections` `{operation_id,expected_revision,reason,scores?:[{player_id,round_number,score}],winner_id?,details?}` -> full Game. Existing game managers (organizer, site admin, owning-group admin) may correct completed/incomplete games. Scores are patches, not destructive replacement. Reuse game policies for specialty results; validate roster, completeness and mode; commit corrections/result/audit/event atomically. Preserve `imported_result` and immutable original provenance. Stale revisions and operation conflicts return 409.

## People and groups

Site administrators may inspect/manage all groups, people and games. Group leads manage their membership, never another group's canonical person. Canonical guest rename/archive requires no linked account and exactly one active membership in a managed group; other canonical edits require self/site admin. Lead removal/account disabling cannot leave a nonarchived group without an active lead. Membership and GroupAdmin representations stay synchronized.

- `GET /api/groups/{id}` -> `{group:{id,name,role,archived,settings},people:Person[],pending_people:[{id,name}],memberships:[{id,person_id,group_id,role,status,is_home}],admins:[{id,name}],invites:[{id,person_id,expires_at,used_at}]}`. Pending display names are visible to the lead; linked-account private details and other private group IDs are not.
- `PATCH /api/groups/{id}` `{name?,is_discoverable?,show_roster?}` -> group.
- `POST /api/groups/{id}/archive` / `reinstate` `{reason?}` -> group; archive refuses unfinished games. Superadmin-only archive/reinstate.
- `POST /api/groups/{id}/transfer-lead` `{user_id}` -> group; target must be an active account with active membership. Prior lead becomes member; audit records the handover.
- `PATCH /api/people/{id}` `{name?,first_name?,last_name?,email?,is_minor?,is_discoverable?,show_full_last_name?}` -> person detail; self/group lead/site admin authorization applies, with account-owned privacy restricted to self/site admin.
- `POST /api/people/{id}/archive` / `reinstate` `{reason?}` -> person; no active-game participant may be archived. Group lead/site admin archive; site admin reinstate.
- `POST /api/people/{id}/purge` `{confirm:"PURGE",reason}` -> person; site-admin-only anonymization of the active profile, retaining person ID and score history. Refuse active-game participants and owner/self accounts. Immutable imported provenance remains sensitive; this operation is not a GDPR erasure guarantee.
- `POST /api/groups/{id}/memberships` `{person_id}` -> membership; site admins may explicitly assign. Group leads may directly add only a sole-group manageable guest, or approve that person's own pending join request. Linked accounts otherwise join through invitation/self-acceptance; shared-group visibility alone does not authorize recruitment.
- `POST /api/groups/{id}/request-join` `{}` -> membership with `status:"requested"`.
- `POST /api/memberships/{id}/decide` `{decision:"approve"|"deny"|"remove"}` -> membership; group lead/site admin, or self leaving with lead-transfer safeguards.
- `POST /api/memberships/{id}/home` `{}` -> membership; self/site admin, requires active membership. Historical game-host groups do not change.
- `GET /api/groups/{id}/invites` -> `{invites:[...]}`; `POST /api/invites/{token_hash}/revoke` `{}` -> `{ok:true}`; privileged list includes a nonsecret ID, never raw accepted-token secrets. Existing invite creation/acceptance endpoints remain.

## Statistics integration

`GET /api/games` additionally accepts `group_id`, `person_id`, `from_date`, `to_date` (ISO dates, inclusive UTC calendar bounds); existing filters and response shapes remain compatible.

Analytics router is implemented separately. It will reuse the shared authenticated dependency and `is_superadmin(user)` predicate. It must use current official results, preserve original imported records, exclude incomplete/deleted games from win credit, respect scope/privacy, and distinguish group-hosted from lifetime records.

Additional legacy directory, connections, transfers, duplicate/merge, saved layouts, feedback and notification routes are being inventoried alongside this core contract; they must not be silently described as complete before implemented and verified.

## Saved setups and directory

Layout = `{id,group_id,slug,name,player_ids:number[],mode,scoring_direction,target_score,title,is_default,created_by}`. Layouts never save viewer groups or scorekeepers, and applying a layout never grants sharing.

- `GET /api/layouts?group_id=&slug=` -> `{layouts:Layout[]}`; membership required in each returned group.
- `POST /api/layouts` `{group_id,slug,name,player_ids,mode?,scoring_direction?,target_score?,title?,is_default?}` -> Layout; group lead/site admin only.
- `PUT /api/layouts/{id}` same body -> Layout; host group and slug cannot change.
- `DELETE /api/layouts/{id}` `{}` -> `{ok:true}`; lead/site admin only.
- `POST /api/layouts/{id}/set-default` `{}` -> Layout; one default per host group/game.
- `POST /api/layouts/{id}/start` `{viewer_group_ids?:number[],scorekeeper_ids?:number[]}` -> Game. Revalidate current memberships, every player's availability, scorekeeper authority, game mode/limits and explicit viewers using the normal create-game policy. Stale/unavailable saved players produce 422 rather than silently changing a roster.
- `GET /api/directory/groups?q=` -> `{groups:[{id,name,show_roster,member_count:number|null,membership_status:string|null}]}`.
- `GET /api/directory/people?q=` -> `{people:[{id,name,has_account,group_ids:number[]}]}`. Minimum two name characters, max 30 requests/minute/account and 30 results. No emails or private legal/contact data; hidden groups/rosters, non-discoverable people and minors are protected unless self/shared-group/site-admin authorization applies. Only authorized/public group IDs are included.

Directory results permit the existing join-request and invitation flows; they do not grant score access or automatic crew/alliance visibility. Existing accepted crew/alliance records remain immutable provenance in this phase.
