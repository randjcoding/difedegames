# Analytics API contract

All endpoints require the current active/verified/approved session and return
private/no-store JSON. Ordinary users see only games they may access (participant,
organizer, assigned scorekeeper, explicitly shared group, or owning group admin).
Superadmins may see site-wide games. “All time” changes no permissions.

Common query parameters: `scope=visible|site` (default visible; site requires
superadmin), `group_id` (host group, not all members' lifetime games), `person_id`,
`slug`, `title` (exact Basic/custom title; only basic-other), `mode=rounds|single|winner`,
`direction=low|high`, `q` (literal case-insensitive title search, max120),
`page=1`, `page_size=20` (max100). Omitted filters mean all authorized dates and
categories. Deleted/incomplete/active games do not contribute wins. Archiving a
person does not erase their retained historical outcomes.
History and leaderboard pagination are independent requests with these parameters.

Category = `{id,slug,name,title,mode,scoring_direction,score_metric,label}`.
`score_metric` is `round_total`, `strength_of_win`, or `outcome_only`.
Category IDs are opaque stable strings; compare scores only within the same ID.
Basic titles, modes and session directions have separate categories.

PersonStats = `{person_id,name,played,known_results,unknown_results,solo_wins,
shared_wins,wins,losses,ties,win_percentage,win_percentage_denominator,
winning_scores:[{category_id,scored_wins,average,best}]}`.
`ties` credits actual shared winners only; losers in a tied game remain losses.
`win_percentage` uses decided completed games, is null with denominator0, and is
rounded to one decimal. Raw points are never averaged across categories.

Summary = `{games,known_results,unknown_results,players,player_appearances,
solo_wins,shared_wins}`. Categories = `[{category,games,known_results,unknown_results}]`.
Pagination = `{page,page_size,total,pages}`.
GameBrief = `{id,title,slug,mode,scoring_direction,group_id,category_id,
finished_at,created_at,player_count,result_known,winners:[{player_id,name,
winning_score,is_tie}]}`. No score board/cells are included: open the existing
game detail endpoint when needed.

## GET /api/statistics

`{scope,filters,summary,categories,history:GameBrief[],pagination,labels}`.
The `labels` object explains authorized scope, decided-game denominator and
category-only score comparisons. History is newest completed first.

## GET /api/leaderboards

`{scope,filters,summary,categories,players:PersonStats[],pagination,labels}`.
Sort by wins descending, decided win percentage descending, name/id. Points
appear only as per-category `winning_scores`; do not display a mixed average.

## GET /api/streaks

`{scope,filters,streaks:[{person_id,name,length,shared_wins,start_at,end_at,
start_game_id,end_game_id}],pagination,labels}`.
Explicit request because streaks require a player's ordered finished-game
sequence. Unrelated games do not interrupt it. Unknown outcomes interrupt an
unprovable streak; deleted/incomplete games are excluded. Minimum length2.

## GET /api/hall-of-fame

Additional `limit=5` (max20) controls each record list.
`{scope,filters,latest_game:GameBrief|null,biggest_beatdowns:SpreadRecord[],
worst_five_crowns_hands:HandRecord[],win_leaders:[{category,leaders:PersonStats[]}],
best_scores:ScoreRecord[],worst_scores:ScoreRecord[],trouble_strength:ScoreRecord[],labels}`.

SpreadRecord = `{category,game:GameBrief,value,low_total,high_total}`;
one biggest positive max-minus-min spread per category. There is no cross-type
raw-score ranking. ScoreRecord = `{category,game:GameBrief,person_id,name,value}`;
best/worst according to that category's session direction. Trouble uses its
separate winning-strength record list. HandRecord adds `round_number` and is an
actual saved Five Crowns hand, not its final total. All record games have known
official outcomes; latest_game may explicitly have an unknown original result.

## GET /api/people/{id}/statistics

`{person:{id,name},scope,filters,summary:PersonStats,categories,history,pagination,labels}`.
404 for people outside the caller's authorized person/game scope. Lifetime here
means all authorized completed games involving that person, not hidden groups.

## GET /api/groups/{id}/statistics

`{group:{id,name},scope,filters,summary,categories,players:PersonStats[],history,pagination,player_pagination,labels}`.
Host-group statistics over authorized games. It does not imply visibility into
members' other private games. 404 for a group outside the caller's scope.
`pagination` describes history; `player_pagination` independently describes the
player slice using the same requested page/page_size.

## Outcome and privacy rules

Current official frozen results are authoritative. Uncorrected `v2-original`
results retain their original recorded winner/score/tie rows. Imported completed
games missing winner records remain explicit unknown outcomes; never infer a
winner from score totals. A later V3 completion/correction supersedes the old
official result while immutable original provenance remains preserved.

No emails/passwords/private profile fields are returned. Statistics names obey
surname consent and mask purged people; only the person themself or superadmin
gets unrestricted names. Group/visibility filters are applied before aggregation.
