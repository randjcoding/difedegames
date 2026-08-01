from flask import Blueprint, render_template, jsonify, request, g, url_for, session, redirect, flash
from app import get_db_connection, socketio
from datetime import datetime
from flask_socketio import emit
from app.events import broadcast_score_update, broadcast_game_completed, broadcast_game_paused, broadcast_game_resumed
from app.auth import login_required, get_current_user, admin_required
from app.identity import (slugify_family, unique_family_slug, public_person_name,
                          audit, allied_family_ids)
from app.email_utils import APP_BASE_URL
import json
import psycopg2.extras

main = Blueprint('main', __name__)

def execute_query(conn, query, params=None):
    """Helper function to execute PostgreSQL queries with cursor"""
    cursor = conn.cursor()
    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)
    
    try:
        results = cursor.fetchall()
        cursor.close()
        return results
    except:
        cursor.close()
        return []

def execute_query_one(conn, query, params=None):
    """Helper function to execute PostgreSQL queries and return one result"""
    cursor = conn.cursor()
    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)
    
    result = cursor.fetchone()
    cursor.close()
    return result

def execute_modify(conn, query, params=None):
    """Helper function to execute INSERT/UPDATE/DELETE queries"""
    cursor = conn.cursor()
    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)
    
    # Get the last row id for INSERT operations
    if query.strip().upper().startswith('INSERT'):
        lastrowid = cursor.lastrowid if hasattr(cursor, 'lastrowid') else None
        cursor.close()
        conn.commit()
        return lastrowid
    else:
        cursor.close()
        conn.commit()
        return cursor.rowcount

def family_access_clause(user, alias='ag'):
    """Legacy broader access (alliances / seated guest). Prefer play_family_clause
    for dashboard and play paths. Kept for any non-play callers that still need it."""
    if user.get('role') == 'super_admin':
        return 'TRUE', []
    clauses = [f'{alias}.user_id = %s']
    params = [user['id']]
    family_id = user.get('family_id')
    if family_id is not None:
        clauses.append(f'{alias}.family_id = %s')
        params.append(family_id)
        clauses.append(f'''{alias}.family_id IN (
            SELECT CASE WHEN fa.requesting_family_id = %s THEN fa.target_family_id
                        ELSE fa.requesting_family_id END
            FROM family_alliances fa
            WHERE fa.status = 'accepted'
              AND (fa.requesting_family_id = %s OR fa.target_family_id = %s)
        )''')
        params.extend([family_id, family_id, family_id])
    player_id = user.get('player_id')
    if player_id:
        clauses.append(f'''EXISTS (
            SELECT 1 FROM active_game_players agx
            WHERE agx.active_game_id = {alias}.id AND agx.player_id = %s
        )''')
        params.append(player_id)
    return '(' + ' OR '.join(clauses) + ')', params


def play_family_clause(user, alias='ag'):
    """Strict play-path access: only games hosted by the user's home family.
    No super_admin bypass, alliances, or seated-elsewhere access."""
    family_id = user.get('family_id') if user else None
    if family_id is not None:
        return f'{alias}.family_id = %s', [family_id]
    user_id = user.get('id') if user else None
    if user_id is not None:
        return f'{alias}.user_id = %s', [user_id]
    return 'FALSE', []


def fetch_accessible_game(conn, game_id, user, extra_where='', extra_params=None):
    """Load an active_games row if the user may access it on play paths."""
    clause, params = play_family_clause(user, 'ag')
    sql = f'SELECT ag.* FROM active_games ag WHERE ag.id = %s AND {clause}'
    all_params = [game_id] + list(params)
    if extra_where:
        sql += ' AND ' + extra_where
        if extra_params:
            all_params.extend(extra_params)
    return execute_query_one(conn, sql, tuple(all_params))


def user_can_access_active_game(user, game):
    if not user or not game:
        return False
    fam = user.get('family_id')
    if fam is not None:
        return game.get('family_id') == fam
    return game.get('user_id') == user.get('id')


# Hall of Fame / Shame: first name + last initial only (site-wide bragging card).
_HOF_PRIVACY_NAME = (
    "TRIM(p.first_name) || ' ' || "
    "UPPER(LEFT(COALESCE(NULLIF(TRIM(p.last_name), ''), '?'), 1)) || '.'"
)


def build_hof_slides(conn):
    """Site-wide bragging/roasting slides for the dashboard carousel."""
    slides = []
    pname = _HOF_PRIVACY_NAME

    last = execute_query_one(conn, '''
        SELECT ag.id,
               COALESCE(ag.custom_game_name, g.name) AS game_name,
               g.slug,
               COALESCE(ag.scoring_direction, g.scoring_direction) AS scoring_direction,
               ag.completion_time
        FROM active_games ag
        JOIN games g ON g.id = ag.game_id
        WHERE ag.is_complete = TRUE
        ORDER BY ag.completion_time DESC NULLS LAST, ag.id DESC
        LIMIT 1
    ''')
    if last:
        low_wins = last['scoring_direction'] == 'low_wins'
        order = 'ASC' if low_wins else 'DESC'
        standings = execute_query(conn, f'''
            SELECT {pname} AS privacy_name,
                   COALESCE(SUM(gs.score), 0)::int AS total_score
            FROM active_game_players agp
            JOIN players p ON p.id = agp.player_id
            LEFT JOIN game_scores gs
              ON gs.active_game_id = agp.active_game_id AND gs.player_id = p.id
            WHERE agp.active_game_id = %s
            GROUP BY p.id, p.first_name, p.last_name
            ORDER BY total_score {order}, privacy_name
        ''', (last['id'],))
        winners = execute_query(conn, f'''
            SELECT {pname} AS privacy_name
            FROM game_stats gst
            JOIN players p ON p.id = gst.winner_id
            WHERE gst.game_id = %s
            ORDER BY privacy_name
        ''', (last['id'],))
        winner_label = ', '.join(w['privacy_name'] for w in winners) if winners else (
            standings[0]['privacy_name'] if standings else 'Unknown'
        )
        lines = [f"{s['privacy_name']} — {s['total_score']}" for s in standings]
        slides.append({
            'type': 'last_game',
            'title': 'Last Game',
            'subtitle': f"{last['game_name']} — {winner_label} won",
            'lines': lines,
            'game_name': last['game_name'],
        })

    beat = execute_query_one(conn, f'''
        WITH player_totals AS (
            SELECT ag.id AS active_game_id,
                   COALESCE(ag.custom_game_name, g.name) AS game_name,
                   COALESCE(ag.scoring_direction, g.scoring_direction) AS dir,
                   {pname} AS pname,
                   COALESCE(SUM(gs.score), 0)::int AS total_score
            FROM active_games ag
            JOIN games g ON g.id = ag.game_id
            JOIN active_game_players agp ON agp.active_game_id = ag.id
            JOIN players p ON p.id = agp.player_id
            LEFT JOIN game_scores gs
              ON gs.active_game_id = ag.id AND gs.player_id = p.id
            WHERE ag.is_complete = TRUE
              AND COALESCE(ag.scoring_direction, g.scoring_direction) IN ('low_wins', 'high_wins')
            GROUP BY ag.id, ag.custom_game_name, g.name, ag.scoring_direction,
                     g.scoring_direction, p.id, p.first_name, p.last_name
        ),
        agg AS (
            SELECT active_game_id, game_name, dir,
                   COUNT(*)::int AS n,
                   MAX(total_score)::int AS max_s,
                   MIN(total_score)::int AS min_s
            FROM player_totals
            GROUP BY active_game_id, game_name, dir
            HAVING COUNT(*) >= 2
        ),
        with_spread AS (
            SELECT a.*,
                   (a.max_s - a.min_s) AS spread,
                   CASE WHEN a.dir = 'low_wins' THEN a.min_s ELSE a.max_s END AS winner_score,
                   CASE WHEN a.dir = 'low_wins' THEN a.max_s ELSE a.min_s END AS loser_score
            FROM agg a
            WHERE (a.max_s - a.min_s) > 0
        )
        SELECT ws.game_name, ws.spread, ws.winner_score, ws.loser_score,
               (SELECT pt.pname FROM player_totals pt
                WHERE pt.active_game_id = ws.active_game_id
                  AND pt.total_score = ws.winner_score
                ORDER BY pt.pname LIMIT 1) AS winner_name,
               (SELECT pt.pname FROM player_totals pt
                WHERE pt.active_game_id = ws.active_game_id
                  AND pt.total_score = ws.loser_score
                ORDER BY pt.pname LIMIT 1) AS loser_name
        FROM with_spread ws
        ORDER BY ws.spread DESC, ws.active_game_id DESC
        LIMIT 1
    ''')
    if beat and beat.get('spread'):
        slides.append({
            'type': 'beat_down',
            'title': 'Biggest Beat-Down',
            'subtitle': beat['game_name'],
            'lines': [
                f"{beat['winner_name']} {beat['winner_score']} vs {beat['loser_name']} {beat['loser_score']}",
                f"Spread: {beat['spread']} points",
            ],
            'game_name': beat['game_name'],
        })

    worst_round = execute_query_one(conn, f'''
        WITH fc_scores AS (
            SELECT gs.score, gs.round_number, gs.player_id, gs.active_game_id
            FROM game_scores gs
            JOIN active_games ag ON ag.id = gs.active_game_id
            WHERE ag.game_id = 1 AND ag.is_complete = TRUE
            UNION ALL
            SELECT fcs.score, fcs.round_number, fcs.player_id, fcs.active_game_id
            FROM five_crowns_scores fcs
            JOIN active_games ag ON ag.id = fcs.active_game_id
            WHERE ag.game_id = 1 AND ag.is_complete = TRUE
              AND NOT EXISTS (
                  SELECT 1 FROM game_scores gs2 WHERE gs2.active_game_id = fcs.active_game_id
              )
        )
        SELECT {pname} AS privacy_name, fc.score, fc.round_number
        FROM fc_scores fc
        JOIN players p ON p.id = fc.player_id
        ORDER BY fc.score DESC, fc.round_number DESC
        LIMIT 1
    ''')
    if worst_round:
        slides.append({
            'type': 'worst_round',
            'title': 'Worst Five Crowns Round',
            'subtitle': f"Round {worst_round['round_number']}",
            'lines': [f"{worst_round['privacy_name']} — {worst_round['score']} points"],
            'game_name': 'Five Crowns',
        })

    win_leaders = execute_query(conn, f'''
        WITH win_counts AS (
            SELECT g.id AS game_type_id, g.name AS game_name, g.slug,
                   p.id AS player_id, {pname} AS privacy_name,
                   COUNT(*)::int AS wins,
                   CASE WHEN g.slug = 'five-crowns' THEN 0 ELSE 1 END AS sort_game
            FROM game_stats gst
            JOIN active_games ag ON ag.id = gst.game_id
            JOIN games g ON g.id = ag.game_id
            JOIN players p ON p.id = gst.winner_id
            WHERE ag.is_complete = TRUE AND gst.winner_id IS NOT NULL
            GROUP BY g.id, g.name, g.slug, p.id, p.first_name, p.last_name
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY game_type_id
                       ORDER BY wins DESC, privacy_name
                   ) AS rn
            FROM win_counts
        )
        SELECT game_name, privacy_name, wins
        FROM ranked
        WHERE rn = 1 AND wins >= 1
        ORDER BY sort_game, wins DESC, game_name
        LIMIT 4
    ''')
    for wl in win_leaders:
        slides.append({
            'type': 'win_leader',
            'title': f"All-Time {wl['game_name']} Wins",
            'subtitle': 'Win leader',
            'lines': [f"{wl['privacy_name']} — {wl['wins']} win{'s' if wl['wins'] != 1 else ''}"],
            'game_name': wl['game_name'],
        })

    extremes = execute_query(conn, f'''
        WITH player_totals AS (
            SELECT g.id AS game_type_id, g.name AS game_name, g.slug,
                   COALESCE(ag.scoring_direction, g.scoring_direction) AS dir,
                   {pname} AS privacy_name,
                   COALESCE(SUM(gs.score), 0)::int AS total_score,
                   ag.id AS active_game_id,
                   CASE WHEN g.slug = 'five-crowns' THEN 0 ELSE 1 END AS sort_game
            FROM active_games ag
            JOIN games g ON g.id = ag.game_id
            JOIN active_game_players agp ON agp.active_game_id = ag.id
            JOIN players p ON p.id = agp.player_id
            LEFT JOIN game_scores gs
              ON gs.active_game_id = ag.id AND gs.player_id = p.id
            WHERE ag.is_complete = TRUE
              AND COALESCE(ag.scoring_direction, g.scoring_direction) IN ('low_wins', 'high_wins')
              AND g.slug NOT IN ('trouble')
            GROUP BY g.id, g.name, g.slug, ag.id, ag.scoring_direction, g.scoring_direction,
                     p.id, p.first_name, p.last_name
        ),
        with_pc AS (
            SELECT pt.*,
                   COUNT(*) OVER (PARTITION BY pt.active_game_id) AS player_count
            FROM player_totals pt
        ),
        eligible AS (
            SELECT * FROM with_pc WHERE player_count >= 2
        ),
        worst AS (
            SELECT DISTINCT ON (game_type_id)
                   game_type_id, game_name, privacy_name, total_score, sort_game, 'worst' AS kind
            FROM eligible
            ORDER BY game_type_id,
                     CASE WHEN dir = 'low_wins' THEN total_score ELSE -total_score END DESC,
                     privacy_name
        ),
        best AS (
            SELECT DISTINCT ON (game_type_id)
                   game_type_id, game_name, privacy_name, total_score, sort_game, 'best' AS kind
            FROM eligible
            ORDER BY game_type_id,
                     CASE WHEN dir = 'low_wins' THEN total_score ELSE -total_score END ASC,
                     privacy_name
        )
        SELECT * FROM (
            SELECT * FROM worst
            UNION ALL
            SELECT * FROM best
        ) x
        ORDER BY sort_game, game_name, kind DESC
        LIMIT 4
    ''')
    for ex in extremes:
        if ex['kind'] == 'worst':
            slides.append({
                'type': 'worst_score',
                'title': f"Biggest Loser — {ex['game_name']}",
                'subtitle': 'Worst final score',
                'lines': [f"{ex['privacy_name']} — {ex['total_score']}"],
                'game_name': ex['game_name'],
            })
        else:
            slides.append({
                'type': 'best_score',
                'title': f"Best Score — {ex['game_name']}",
                'subtitle': 'Best final score',
                'lines': [f"{ex['privacy_name']} — {ex['total_score']}"],
                'game_name': ex['game_name'],
            })

    trouble = execute_query_one(conn, f'''
        SELECT {pname} AS privacy_name, gst.winning_score
        FROM game_stats gst
        JOIN active_games ag ON ag.id = gst.game_id
        JOIN games g ON g.id = ag.game_id
        JOIN players p ON p.id = gst.winner_id
        WHERE g.slug = 'trouble' AND ag.is_complete = TRUE AND gst.winner_id IS NOT NULL
        ORDER BY gst.winning_score DESC, gst.completion_time DESC NULLS LAST
        LIMIT 1
    ''')
    if trouble:
        slides.append({
            'type': 'trouble_sow',
            'title': 'Strongest Trouble Win',
            'subtitle': 'Highest Strength of Win',
            'lines': [f"{trouble['privacy_name']} — {trouble['winning_score']} SOW"],
            'game_name': 'Trouble',
        })

    return slides


def set_player_home_family(conn, player_id, target_family_id):
    """Reassign a person's primary/home family via memberships (keeps the
    one-primary invariant and, through the DB trigger, players.family_id).
    Any prior *primary* membership is removed (a true move); other guest
    memberships are preserved."""
    current_primary = execute_query_one(conn, '''
        SELECT family_id FROM player_family_memberships
        WHERE player_id = %s AND is_primary
    ''', (player_id,))
    old_family_id = current_primary['family_id'] if current_primary else None
    if old_family_id == target_family_id:
        return
    execute_modify(conn, '''
        INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
        VALUES (%s, %s, FALSE, 'active', 'member')
        ON CONFLICT (player_id, family_id) DO UPDATE SET status = 'active'
    ''', (player_id, target_family_id))
    execute_modify(conn, 'UPDATE player_family_memberships SET is_primary = FALSE WHERE player_id = %s', (player_id,))
    execute_modify(conn, '''
        UPDATE player_family_memberships SET is_primary = TRUE
        WHERE player_id = %s AND family_id = %s
    ''', (player_id, target_family_id))
    if old_family_id is not None:
        execute_modify(conn, '''
            DELETE FROM player_family_memberships
            WHERE player_id = %s AND family_id = %s
        ''', (player_id, old_family_id))
    execute_modify(conn, 'UPDATE users SET family_id = %s WHERE player_id = %s', (target_family_id, player_id))

def is_family_lead(conn, user, family_id):
    """True if the user leads the given family (or is super_admin)."""
    if not user or not family_id:
        return False
    if user.get('role') == 'super_admin':
        return True
    fam = execute_query_one(conn, 'SELECT lead_user_id FROM families WHERE id = %s', (family_id,))
    return bool(fam and fam.get('lead_user_id') == user.get('id'))

def get_family_players(family_id, include_crew=False, for_player_id=None):
    """Roster for a family from memberships (a person can belong to several
    families), optionally including allied/crew family players as guests,
    plus accepted personal crew links for for_player_id."""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        family_players = execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                FALSE as is_guest, NULL as guest_family_name,
                FALSE as is_personal_crew,
                p.family_id
            FROM players p
            JOIN player_family_memberships m ON m.player_id = p.id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
            ORDER BY p.first_name, p.last_name
        ''', (family_id,))
        
        result = list(family_players)
        seen = {p['id'] for p in result}
        
        if include_crew and family_id:
            crew_families = execute_query(conn, '''
                SELECT CASE WHEN requesting_family_id = %s THEN target_family_id
                            ELSE requesting_family_id END as ally_family_id,
                       CASE WHEN requesting_family_id = %s THEN tf.name
                            ELSE rf.name END as ally_name
                FROM family_alliances fa
                JOIN families rf ON fa.requesting_family_id = rf.id
                JOIN families tf ON fa.target_family_id = tf.id
                WHERE fa.status = 'accepted'
                  AND (fa.requesting_family_id = %s OR fa.target_family_id = %s)
            ''', (family_id, family_id, family_id, family_id))
            
            for cf in crew_families:
                crew_players = execute_query(conn, '''
                    SELECT p.id, p.first_name, p.last_name,
                        COALESCE(p.display_name, p.first_name) as display_name,
                        TRUE as is_guest, %s as guest_family_name,
                        FALSE as is_personal_crew,
                        p.family_id
                    FROM players p
                    JOIN player_family_memberships m ON m.player_id = p.id
                    WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
                    ORDER BY p.first_name, p.last_name
                ''', (cf['ally_name'], cf['ally_family_id']))
                for cp in crew_players:
                    if cp['id'] not in seen:
                        seen.add(cp['id'])
                        result.append(cp)

            if for_player_id:
                personal = execute_query(conn, '''
                    SELECT p.id, p.first_name, p.last_name,
                        COALESCE(p.display_name, p.first_name) as display_name,
                        TRUE as is_guest, 'Personal crew' as guest_family_name,
                        TRUE as is_personal_crew,
                        p.family_id
                    FROM player_crew_links l
                    JOIN players p ON p.id = CASE
                        WHEN l.player_a_id = %s THEN l.player_b_id ELSE l.player_a_id END
                    WHERE l.status = 'accepted'
                      AND (l.player_a_id = %s OR l.player_b_id = %s)
                      AND p.archived_at IS NULL AND p.purged_at IS NULL
                    ORDER BY p.first_name, p.last_name
                ''', (for_player_id, for_player_id, for_player_id))
                for pp in personal:
                    if pp['id'] not in seen:
                        seen.add(pp['id'])
                        result.append(pp)
        
        return result
    finally:
        conn.close()

def game_page(slug, game_id):
    """Generic game page handler for any game type.

    Never auto-opens a session. Open a score sheet only when ?game_id= is present
    (Resume / Continue). Otherwise show the new-game form plus family paused/completed lists.
    """
    conn = get_db_connection()
    user = get_current_user()
    
    game_def = execute_query_one(conn, 'SELECT * FROM games WHERE id = %s', (game_id,))
    
    specific_game_id = request.args.get('game_id')
    force_new = request.args.get('new') == '1'
    access_sql, access_params = play_family_clause(user, 'ag')
    
    active_game = None
    if specific_game_id and not force_new:
        active_game = fetch_accessible_game(
            conn, specific_game_id, user,
            extra_where='ag.is_complete = FALSE AND ag.game_id = %s',
            extra_params=[game_id])
        # Do not auto-unpause on GET. Pause must stick until Resume is used
        # (/api/games/.../resume). Opening ?game_id= for a paused session used
        # to silently clear is_paused and made Pause look broken. Keep the
        # board closed so the paused list / Resume button is what people use.
        if active_game and active_game.get('is_paused'):
            active_game = None
    
    paused_games = execute_query(conn, f'''
        SELECT ag.id, ag.start_time, ag.custom_game_name,
            string_agg(COALESCE(p.display_name, p.first_name), ', ' ORDER BY agp.id) as player_names,
            COUNT(DISTINCT agp.player_id) as player_count
        FROM active_games ag
        JOIN active_game_players agp ON ag.id = agp.active_game_id
        JOIN players p ON agp.player_id = p.id
        WHERE ag.game_id = %s AND {access_sql} AND ag.is_complete = FALSE AND ag.is_paused = TRUE
        GROUP BY ag.id, ag.start_time, ag.custom_game_name
        ORDER BY ag.start_time DESC LIMIT 5
    ''', tuple([game_id] + list(access_params)))
    
    completed_games = execute_query(conn, f'''
        SELECT ag.id, ag.start_time, ag.completion_time, ag.custom_game_name,
            string_agg(COALESCE(p.display_name, p.first_name), ', ' ORDER BY agp.id) as player_names,
            gs_sub.winner, gs_sub.winning_score,
            gsn.game_number, gsn.family_game_number
        FROM active_games ag
        JOIN active_game_players agp ON ag.id = agp.active_game_id
        JOIN players p ON agp.player_id = p.id
        LEFT JOIN LATERAL (
            SELECT COALESCE(pw.display_name, pw.first_name) as winner, gs.winning_score
            FROM game_stats gs JOIN players pw ON gs.winner_id = pw.id
            WHERE gs.game_id = ag.id LIMIT 1
        ) gs_sub ON TRUE
        LEFT JOIN game_sessions_numbered gsn ON gsn.id = ag.id
        WHERE ag.game_id = %s AND {access_sql} AND ag.is_complete = TRUE
        GROUP BY ag.id, ag.start_time, ag.completion_time, ag.custom_game_name,
            gs_sub.winner, gs_sub.winning_score,
            gsn.game_number, gsn.family_game_number
        ORDER BY ag.start_time DESC LIMIT 5
    ''', tuple([game_id] + list(access_params)))
    
    game_players = []
    scores = {}
    game_family_id = None
    if active_game:
        game_family_id = active_game.get('family_id')
        game_players = execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                p.family_id,
                COALESCE(f.name, '') as family_name
            FROM players p
            JOIN active_game_players agp ON p.id = agp.player_id
            LEFT JOIN families f ON p.family_id = f.id
            WHERE agp.active_game_id = %s
            ORDER BY agp.id
        ''', (active_game['id'],))
        
        scores_data = execute_query(conn, '''
            SELECT player_id, round_number, score FROM game_scores
            WHERE active_game_id = %s
        ''', (active_game['id'],))
        if not scores_data and slug == 'five-crowns':
            scores_data = execute_query(conn, '''
                SELECT player_id, round_number, score FROM five_crowns_scores
                WHERE active_game_id = %s
            ''', (active_game['id'],))
        scores = {(s['player_id'], s['round_number']): s['score'] for s in scores_data}
    
    all_players = get_family_players(user.get('family_id'), include_crew=True,
                                     for_player_id=user.get('player_id'))
    family_id = user.get('family_id')
    lead = is_family_lead(conn, user, family_id) if family_id else False
    conn.close()
    
    template = slug.replace('-', '_') + '.html'
    return render_template(template,
        game_def=game_def,
        active_game=active_game,
        paused_games=paused_games,
        completed_games=completed_games,
        game_players=game_players,
        scores=scores,
        players=all_players,
        game_family_id=game_family_id,
        user_family_id=family_id,
        is_lead=lead)

@main.route('/')
def index():
    """Landing page - redirects based on authentication status"""
    if 'user_id' in session:
        return redirect(url_for('main.dashboard'))
    return render_template('index.html')

@main.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard for authenticated users"""
    user = get_current_user()
    conn = get_db_connection()
    try:
        access_sql, access_params = play_family_clause(user, 'ag')
        active_games = execute_query(conn, f'''
            SELECT ag.id, ag.start_time, ag.is_paused, ag.game_id, ag.scoring_direction, ag.target_score,
                COALESCE(ag.custom_game_name, g.name) as game_name, g.slug,
                string_agg(COALESCE(p.display_name, p.first_name), ', ' ORDER BY agp.id) as player_names,
                COUNT(DISTINCT agp.player_id) as player_count
            FROM active_games ag
            JOIN games g ON ag.game_id = g.id
            JOIN active_game_players agp ON ag.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            WHERE {access_sql} AND ag.is_complete = FALSE
            GROUP BY ag.id, ag.start_time, ag.is_paused, ag.game_id, ag.scoring_direction, ag.target_score,
                ag.custom_game_name, g.name, g.slug
            ORDER BY ag.is_paused ASC, ag.start_time DESC
        ''', tuple(access_params))
        hof_slides = build_hof_slides(conn)
        return render_template(
            'dashboard.html', user=user, active_games=active_games, hof_slides=hof_slides)
    finally:
        conn.close()

@main.route('/admin')
@admin_required
def admin():
    conn = get_db_connection()
    try:
        player_count = execute_query_one(conn, 'SELECT COUNT(*) as c FROM players WHERE purged_at IS NULL')['c']
        game_count = execute_query_one(conn, 'SELECT COUNT(*) as c FROM active_games')['c']
        user_count = execute_query_one(conn, 'SELECT COUNT(*) as c FROM users')['c']
        family_count = execute_query_one(conn, 'SELECT COUNT(*) as c FROM families WHERE archived_at IS NULL')['c']
        families_raw = execute_query(conn, '''
            SELECT f.id, f.name,
                (SELECT COUNT(*) FROM players p WHERE p.family_id = f.id AND p.purged_at IS NULL) as player_count,
                (SELECT COUNT(*) FROM users u WHERE u.family_id = f.id) as user_count
            FROM families f WHERE f.archived_at IS NULL ORDER BY f.name
        ''')
        families = [dict(f) for f in families_raw]
        return render_template('admin.html',
            player_count=player_count, game_count=game_count,
            user_count=user_count, family_count=family_count,
            families=families)
    finally:
        conn.close()


@main.route('/admin/people')
@admin_required
def admin_people():
    """Super-admin People Hub: My Team vs Entire Site person management."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        families_raw = execute_query(conn, '''
            SELECT id, name FROM families WHERE archived_at IS NULL ORDER BY name
        ''')
        return render_template(
            'admin_people.html',
            families=[dict(f) for f in families_raw],
            my_family_id=user.get('family_id'),
        )
    finally:
        conn.close()


@main.route('/admin/teams')
@admin_required
def admin_teams():
    """Super-admin Teams Hub: leads, rosters, invites, crew, archive."""
    return render_template('admin_teams.html')


@main.route('/api/admin/families')
@admin_required
def admin_families_api():
    """List all teams with lead, roster counts, games, pending, and crew."""
    conn = get_db_connection()
    try:
        include_archived = (request.args.get('include_archived') or '').lower() in (
            '1', 'true', 'yes')
        q = (request.args.get('q') or '').strip()
        where = ['TRUE']
        params = []
        if not include_archived:
            where.append('f.archived_at IS NULL')
        if q:
            where.append('''(
                f.name ILIKE %s OR COALESCE(f.slug, '') ILIKE %s
                OR COALESCE(lp.display_name, lp.first_name, '') ILIKE %s
                OR COALESCE(lu.email, '') ILIKE %s
            )''')
            like = f'%{q}%'
            params.extend([like, like, like, like])
        rows = execute_query(conn, f'''
            SELECT f.id, f.name, f.slug, f.lead_user_id, f.created_at,
                f.archived_at, f.is_discoverable, f.show_roster,
                lu.email AS lead_email,
                COALESCE(lp.display_name, lp.first_name) AS lead_display_name,
                lp.id AS lead_player_id,
                (SELECT COUNT(*) FROM player_family_memberships m
                   JOIN players p ON p.id = m.player_id
                  WHERE m.family_id = f.id AND m.status = 'active'
                    AND p.archived_at IS NULL AND p.purged_at IS NULL) AS member_count,
                (SELECT COUNT(*) FROM player_family_memberships m
                   JOIN players p ON p.id = m.player_id
                  WHERE m.family_id = f.id AND m.status = 'active' AND m.is_primary
                    AND p.archived_at IS NULL AND p.purged_at IS NULL) AS home_member_count,
                (SELECT COUNT(*) FROM users u
                  WHERE u.family_id = f.id AND u.archived_at IS NULL) AS login_count,
                (SELECT COUNT(*) FROM active_games ag
                  WHERE ag.family_id = f.id AND ag.is_complete = TRUE) AS games_complete,
                (SELECT COUNT(*) FROM active_games ag
                  WHERE ag.family_id = f.id AND ag.is_complete = FALSE) AS games_open,
                (SELECT COUNT(*) FROM player_family_memberships m
                  WHERE m.family_id = f.id AND m.status IN ('invited', 'requested')) AS pending_memberships,
                (SELECT COUNT(*) FROM invitations i
                  WHERE i.family_id = f.id AND i.status = 'sent') AS pending_invites,
                (SELECT COUNT(*) FROM family_alliances fa
                  WHERE fa.status = 'accepted'
                    AND (fa.requesting_family_id = f.id OR fa.target_family_id = f.id)) AS crew_count
            FROM families f
            LEFT JOIN users lu ON lu.id = f.lead_user_id
            LEFT JOIN players lp ON lp.id = lu.player_id
            WHERE {' AND '.join(where)}
            ORDER BY (f.archived_at IS NOT NULL), f.name
            LIMIT 500
        ''', tuple(params) if params else None)
        out = []
        for r in rows:
            d = dict(r)
            d['is_archived'] = bool(d.get('archived_at'))
            d['has_lead'] = bool(d.get('lead_user_id'))
            if d.get('created_at'):
                d['created_at'] = d['created_at'].isoformat()
            if d.get('archived_at'):
                d['archived_at'] = d['archived_at'].isoformat()
            out.append(d)
        return jsonify({
            'families': out,
            'include_archived': include_archived,
        })
    finally:
        conn.close()


@main.route('/api/admin/families/<int:family_id>')
@admin_required
def admin_family_detail(family_id):
    """Full team dossier for super admin: members, pending, invites, crew."""
    conn = get_db_connection()
    try:
        family = execute_query_one(conn, '''
            SELECT f.id, f.name, f.slug, f.lead_user_id, f.created_at, f.updated_at,
                f.archived_at, f.is_discoverable, f.show_roster, f.created_by_user_id,
                lu.email AS lead_email, lu.id AS lead_user_id_check,
                COALESCE(lp.display_name, lp.first_name) AS lead_display_name,
                lp.id AS lead_player_id,
                cb.email AS created_by_email,
                COALESCE(cbp.display_name, cb.first_name) AS created_by_name
            FROM families f
            LEFT JOIN users lu ON lu.id = f.lead_user_id
            LEFT JOIN players lp ON lp.id = lu.player_id
            LEFT JOIN users cb ON cb.id = f.created_by_user_id
            LEFT JOIN players cbp ON cbp.id = cb.player_id
            WHERE f.id = %s
        ''', (family_id,))
        if not family:
            return jsonify({'error': 'Family not found'}), 404

        members = list(execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) AS display_name,
                p.email AS player_email, p.is_minor,
                p.archived_at AS player_archived_at,
                m.is_primary AS is_home, m.role AS membership_role, m.status AS membership_status,
                m.joined_at AS membership_joined_at,
                hf.id AS home_family_id, hf.name AS home_family_name,
                u.id AS user_id, u.email AS login_email, u.role AS user_role,
                u.is_verified, u.is_approved, u.is_active, u.last_login,
                (SELECT COUNT(DISTINCT agp.active_game_id)
                   FROM active_game_players agp
                   JOIN active_games ag ON ag.id = agp.active_game_id
                  WHERE agp.player_id = p.id AND ag.is_complete = TRUE) AS games,
                (SELECT COUNT(*) FROM game_stats gs WHERE gs.winner_id = p.id) AS wins
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            LEFT JOIN families hf ON hf.id = p.family_id
            LEFT JOIN users u ON u.player_id = p.id
            WHERE m.family_id = %s AND p.purged_at IS NULL
              AND m.status = 'active'
            ORDER BY (m.role = 'lead') DESC, m.is_primary DESC, p.first_name, p.last_name
        ''', (family_id,)))

        pending = list(execute_query(conn, '''
            SELECT m.id AS membership_id, m.status, m.joined_at,
                p.id AS player_id,
                COALESCE(p.display_name, p.first_name) AS display_name,
                p.email AS player_email,
                hf.name AS home_family_name,
                u.email AS login_email
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            LEFT JOIN families hf ON hf.id = p.family_id
            LEFT JOIN users u ON u.player_id = p.id
            WHERE m.family_id = %s AND m.status IN ('invited', 'requested')
              AND p.purged_at IS NULL
            ORDER BY m.joined_at DESC NULLS LAST
        ''', (family_id,)))

        invites = list(execute_query(conn, '''
            SELECT i.id, i.email, i.invite_type, i.status, i.created_at, i.expires_at,
                i.player_id,
                COALESCE(p.display_name, p.first_name) AS player_display_name
            FROM invitations i
            LEFT JOIN players p ON p.id = i.player_id
            WHERE i.family_id = %s AND i.status IN ('sent', 'accepted')
            ORDER BY i.created_at DESC
            LIMIT 50
        ''', (family_id,)))

        alliances = list(execute_query(conn, '''
            SELECT fa.id, fa.status, fa.created_at,
                CASE WHEN fa.requesting_family_id = %s THEN tf.name ELSE rf.name END AS ally_name,
                CASE WHEN fa.requesting_family_id = %s THEN fa.target_family_id
                     ELSE fa.requesting_family_id END AS ally_id,
                CASE WHEN fa.requesting_family_id = %s THEN 'outgoing' ELSE 'incoming' END AS direction
            FROM family_alliances fa
            JOIN families rf ON fa.requesting_family_id = rf.id
            JOIN families tf ON fa.target_family_id = tf.id
            WHERE fa.requesting_family_id = %s OR fa.target_family_id = %s
            ORDER BY (fa.status = 'accepted') DESC, fa.created_at DESC
        ''', (family_id, family_id, family_id, family_id, family_id)))

        games_complete = execute_query_one(conn, '''
            SELECT COUNT(*) AS n FROM active_games
            WHERE family_id = %s AND is_complete = TRUE
        ''', (family_id,))['n']
        games_open = execute_query_one(conn, '''
            SELECT COUNT(*) AS n FROM active_games
            WHERE family_id = %s AND is_complete = FALSE
        ''', (family_id,))['n']

        member_out = []
        for r in members:
            d = dict(r)
            games = int(d.get('games') or 0)
            wins = int(d.get('wins') or 0)
            d['losses'] = max(games - wins, 0)
            d['is_lead'] = bool(d.get('user_id') and family.get('lead_user_id')
                                and d['user_id'] == family['lead_user_id'])
            if d.get('user_id'):
                if not d.get('is_active'):
                    d['login_status'] = 'Inactive'
                elif not d.get('is_verified') or not d.get('is_approved'):
                    d['login_status'] = 'Pending'
                else:
                    d['login_status'] = 'Active'
            else:
                d['login_status'] = 'None'
            if d['is_lead']:
                d['role_label'] = 'Team Lead'
            elif d.get('membership_role') == 'lead':
                d['role_label'] = 'Lead (membership)'
            elif d.get('is_home'):
                d['role_label'] = 'Home member'
            else:
                d['role_label'] = 'Guest member'
            login_e = (d.get('login_email') or '').strip()
            profile_e = (d.get('player_email') or '').strip()
            d['email'] = login_e or profile_e or ''
            if d.get('last_login'):
                d['last_login'] = d['last_login'].isoformat()
            if d.get('membership_joined_at'):
                d['membership_joined_at'] = d['membership_joined_at'].isoformat()
            if d.get('player_archived_at'):
                d['player_archived_at'] = d['player_archived_at'].isoformat()
            member_out.append(d)

        def _iso_rows(rows):
            out = []
            for r in rows:
                d = dict(r)
                for k, v in list(d.items()):
                    if hasattr(v, 'isoformat'):
                        d[k] = v.isoformat()
                out.append(d)
            return out

        fam = dict(family)
        for k in ('created_at', 'updated_at', 'archived_at'):
            if fam.get(k) and hasattr(fam[k], 'isoformat'):
                fam[k] = fam[k].isoformat()
        fam['is_archived'] = bool(family.get('archived_at'))
        fam['has_lead'] = bool(family.get('lead_user_id'))
        fam['games_complete'] = games_complete
        fam['games_open'] = games_open

        # Other active teams for move dropdowns.
        other_families = list(execute_query(conn, '''
            SELECT id, name FROM families
            WHERE archived_at IS NULL AND id <> %s
            ORDER BY name
        ''', (family_id,)))

        return jsonify({
            'family': fam,
            'members': member_out,
            'pending': _iso_rows(pending),
            'invites': _iso_rows(invites),
            'alliances': _iso_rows(alliances),
            'other_families': [dict(f) for f in other_families],
        })
    finally:
        conn.close()


@main.route('/api/admin/families', methods=['POST'])
@admin_required
def admin_create_family():
    """Create a team. Optional lead_player_id makes that person lead (creates login
    from email if needed). Without a lead, creates an empty orphan team."""
    conn = get_db_connection()
    admin = get_current_user()
    try:
        from werkzeug.security import generate_password_hash
        import secrets as _secrets
        from app.auth import create_action_token
        from app.email_utils import send_set_password_email

        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'Team name is required'}), 400

        lead_player_id = data.get('lead_player_id')
        player = None
        lead_user = None
        new_login_email = None
        if lead_player_id:
            lead_player_id = int(lead_player_id)
            player = execute_query_one(conn, '''
                SELECT * FROM players WHERE id = %s AND purged_at IS NULL AND archived_at IS NULL
            ''', (lead_player_id,))
            if not player:
                return jsonify({'error': 'Lead player not found'}), 404
            lead_user = execute_query_one(conn, 'SELECT * FROM users WHERE player_id = %s',
                                         (lead_player_id,))
            if not lead_user:
                new_login_email = (data.get('email') or player.get('email') or '').strip().lower()
                if not new_login_email or '@' not in new_login_email:
                    return jsonify({
                        'error': 'Lead has no login. Provide an email to create one.',
                    }), 400
                taken = execute_query_one(conn, '''
                    SELECT id FROM users WHERE lower(email) = %s
                ''', (new_login_email,))
                if taken:
                    return jsonify({'error': 'That email already has a login.'}), 409

        fam = execute_query_one(conn, '''
            INSERT INTO families (name, slug, lead_user_id, created_by_user_id,
                is_discoverable, show_roster)
            VALUES (%s, %s, NULL, %s, %s, %s)
            RETURNING id, name
        ''', (name, unique_family_slug(conn, name), admin['id'],
              bool(data.get('is_discoverable', True)),
              bool(data.get('show_roster', True))))
        family_id = fam['id']
        created_login = False
        invite_sent = False
        lead_user_id = None

        if lead_player_id and player:
            set_player_home_family(conn, lead_player_id, family_id)
            if not lead_user:
                temp = _secrets.token_urlsafe(10) + 'Aa1!'
                lead_user = execute_query_one(conn, '''
                    INSERT INTO users (email, password_hash, first_name, last_name, family_name,
                        role, is_verified, is_approved, is_active, family_id, player_id)
                    VALUES (%s, %s, %s, %s, %s, 'family_admin', TRUE, TRUE, TRUE, %s, %s)
                    RETURNING *
                ''', (new_login_email, generate_password_hash(temp),
                      player['first_name'], player['last_name'], name,
                      family_id, lead_player_id))
                created_login = True
                execute_modify(conn, 'UPDATE players SET email = COALESCE(email, %s) WHERE id = %s',
                               (new_login_email, lead_player_id))
            execute_modify(conn, '''
                UPDATE player_family_memberships SET role = 'lead', status = 'active'
                WHERE player_id = %s AND family_id = %s
            ''', (lead_player_id, family_id))
            execute_modify(conn, 'UPDATE families SET lead_user_id = %s WHERE id = %s',
                           (lead_user['id'], family_id))
            execute_modify(conn, '''
                UPDATE users SET family_id = %s,
                    role = CASE WHEN role = 'super_admin' THEN role ELSE 'family_admin' END
                WHERE id = %s
            ''', (family_id, lead_user['id']))
            lead_user_id = lead_user['id']
            if data.get('send_password_invite') and created_login:
                token = create_action_token(
                    'set_password', player_id=lead_player_id, user_id=lead_user['id'],
                    family_id=family_id, ttl_hours=168)
                if token:
                    invite_sent = send_set_password_email(
                        lead_user['email'],
                        player.get('display_name') or player['first_name'],
                        name,
                        f"{admin.get('first_name', '')} {admin.get('last_name', '')}".strip() or 'Admin',
                        f"{APP_BASE_URL}/auth/set-password/{token}",
                    )

        audit(conn, admin['id'], 'family_created_admin', 'families', family_id,
              new={'name': name, 'lead_player_id': lead_player_id,
                   'lead_user_id': lead_user_id, 'created_login': created_login})
        conn.commit()
        return jsonify({
            'success': True,
            'family_id': family_id,
            'family_name': fam['name'],
            'lead_user_id': lead_user_id,
            'created_login': created_login,
            'invite_sent': invite_sent,
            'message': f'Created {fam["name"]}'
                       + (' and assigned lead' if lead_user_id else ' (no lead yet)')
                       + ('. Password email sent.' if invite_sent else '.'),
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/admin/families/<int:family_id>', methods=['PUT'])
@admin_required
def admin_update_family(family_id):
    """Update team name / discoverability / roster visibility."""
    conn = get_db_connection()
    admin = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        family = execute_query_one(conn, 'SELECT * FROM families WHERE id = %s', (family_id,))
        if not family:
            return jsonify({'error': 'Family not found'}), 404
        name = data.get('name')
        updates = []
        params = []
        if name is not None:
            name = name.strip()
            if not name:
                return jsonify({'error': 'Team name cannot be empty'}), 400
            updates.append('name = %s')
            params.append(name)
        if 'is_discoverable' in data:
            updates.append('is_discoverable = %s')
            params.append(bool(data.get('is_discoverable')))
        if 'show_roster' in data:
            updates.append('show_roster = %s')
            params.append(bool(data.get('show_roster')))
        if not updates:
            return jsonify({'error': 'No changes'}), 400
        updates.append('updated_at = CURRENT_TIMESTAMP')
        params.append(family_id)
        execute_modify(conn, f'UPDATE families SET {", ".join(updates)} WHERE id = %s',
                       tuple(params))
        audit(conn, admin['id'], 'family_updated', 'families', family_id,
              old={'name': family['name'], 'is_discoverable': family.get('is_discoverable'),
                   'show_roster': family.get('show_roster')},
              new=data)
        conn.commit()
        return jsonify({'success': True, 'message': 'Team updated.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/admin/people')
@admin_required
def admin_people_api():
    """Site-wide or my-team player list with login + win/loss stats."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        scope = (request.args.get('scope') or 'site').strip().lower()
        q = (request.args.get('q') or '').strip()
        include_archived = (request.args.get('include_archived') or '').lower() in (
            '1', 'true', 'yes')
        params = []
        where = ['p.purged_at IS NULL']
        if not include_archived:
            where.append('p.archived_at IS NULL')
        if scope == 'mine':
            where.append('p.family_id = %s')
            params.append(user.get('family_id') or -1)
        if q:
            where.append('''(
                p.first_name ILIKE %s OR p.last_name ILIKE %s
                OR COALESCE(p.display_name, '') ILIKE %s
                OR COALESCE(p.email, '') ILIKE %s
                OR COALESCE(u.email, '') ILIKE %s
                OR COALESCE(f.name, '') ILIKE %s
            )''')
            like = f'%{q}%'
            params.extend([like, like, like, like, like, like])
        sql = '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) AS display_name,
                p.email AS player_email, p.family_id,
                p.archived_at AS player_archived_at,
                f.name AS family_name, f.lead_user_id,
                u.id AS user_id, u.email AS login_email, u.role AS user_role,
                u.is_verified, u.is_approved, u.is_active, u.last_login,
                u.created_at AS user_created_at,
                u.phone_number,
                u.archived_at AS user_archived_at,
                (SELECT i.email FROM invitations i
                  WHERE i.player_id = p.id AND i.status IN ('sent', 'accepted')
                  ORDER BY i.id DESC LIMIT 1) AS invite_email,
                (SELECT COUNT(DISTINCT agp.active_game_id)
                   FROM active_game_players agp
                   JOIN active_games ag ON ag.id = agp.active_game_id
                  WHERE agp.player_id = p.id AND ag.is_complete = TRUE) AS games,
                (SELECT COUNT(*) FROM game_stats gs WHERE gs.winner_id = p.id) AS wins
            FROM players p
            LEFT JOIN families f ON f.id = p.family_id
            LEFT JOIN users u ON u.player_id = p.id
            WHERE ''' + ' AND '.join(where) + '''
            ORDER BY (p.archived_at IS NOT NULL), f.name NULLS LAST, p.first_name, p.last_name
            LIMIT 500
        '''
        rows = execute_query(conn, sql, tuple(params) if params else None)
        out = []
        for r in rows:
            d = dict(r)
            games = int(d.get('games') or 0)
            wins = int(d.get('wins') or 0)
            d['losses'] = max(games - wins, 0)
            d['is_archived'] = bool(d.get('player_archived_at'))
            d['is_lead'] = bool(d.get('user_id') and d.get('lead_user_id')
                                and d['user_id'] == d['lead_user_id'])
            if d.get('user_id'):
                if d.get('user_archived_at') or not d.get('is_active'):
                    d['login_status'] = 'Inactive'
                elif not d.get('is_verified') or not d.get('is_approved'):
                    d['login_status'] = 'Pending'
                else:
                    d['login_status'] = 'Active'
            else:
                d['login_status'] = 'None'
            if d['is_archived']:
                d['role_label'] = 'Archived'
            elif d['is_lead']:
                d['role_label'] = 'Team Lead'
            elif d.get('user_id'):
                d['role_label'] = 'Member'
            else:
                d['role_label'] = 'No login'
            login_e = (d.get('login_email') or '').strip()
            profile_e = (d.get('player_email') or '').strip()
            invite_e = (d.get('invite_email') or '').strip()
            # Never hide a mismatched profile email behind the login email.
            if login_e and profile_e and login_e.lower() != profile_e.lower():
                d['email'] = login_e
                d['email_mismatch'] = True
                d['email_display'] = f'login: {login_e} | profile: {profile_e}'
            else:
                d['email'] = login_e or profile_e or invite_e or ''
                d['email_mismatch'] = False
                d['email_display'] = d['email']
            created = d.get('user_created_at')
            d['joined_at'] = created.isoformat() if created else None
            if d.get('last_login'):
                d['last_login'] = d['last_login'].isoformat()
            if d.get('player_archived_at'):
                d['player_archived_at'] = d['player_archived_at'].isoformat()
            out.append(d)
        return jsonify({
            'people': out, 'scope': scope, 'include_archived': include_archived,
        })
    finally:
        conn.close()


@main.route('/api/admin/player/<int:player_id>/clear-email', methods=['POST'])
@admin_required
def admin_clear_player_email(player_id):
    """Clear stuck profile email(s) without touching the login by default.

    Always nulls players.email and revokes invites for that profile address.
    Pass remove_login=true to also delete the linked login (frees login email).
    Pass sync_to_login=true to set players.email = users.email instead of null
    (useful when profile email drifted from the real login)."""
    conn = get_db_connection()
    admin = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        remove_login = bool(data.get('remove_login', False))
        sync_to_login = bool(data.get('sync_to_login', False))
        player = execute_query_one(conn, '''
            SELECT * FROM players WHERE id = %s AND purged_at IS NULL
        ''', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found'}), 404

        linked = execute_query_one(conn, 'SELECT id, email FROM users WHERE player_id = %s', (player_id,))
        old_profile = (player.get('email') or '').strip().lower() or None
        login_email = (linked.get('email') or '').strip().lower() if linked else None

        # Emails we are freeing from the profile / invite side (not the login
        # unless remove_login is set).
        emails = set()
        if old_profile:
            emails.add(old_profile)
        inv_rows = execute_query(conn, '''
            SELECT email FROM invitations
            WHERE player_id = %s AND status IN ('sent', 'accepted')
        ''', (player_id,))
        for row in inv_rows or []:
            if row.get('email'):
                emails.add(row['email'].strip().lower())

        if linked and remove_login:
            if linked['id'] == admin['id']:
                return jsonify({'error': 'You cannot remove your own login from here'}), 400
            if login_email:
                emails.add(login_email)
            execute_modify(conn, 'UPDATE users SET player_id = NULL WHERE id = %s', (linked['id'],))
            _retire_login_account(conn, admin['id'], linked['id'])
            linked = None
            login_email = None

        if sync_to_login and login_email and not remove_login:
            execute_modify(conn, '''
                UPDATE players SET email = %s, email_verified = TRUE WHERE id = %s
            ''', (login_email, player_id))
            # Still revoke invites for the OLD mismatched profile email.
            for e in emails:
                if e != login_email:
                    execute_modify(conn, '''
                        UPDATE invitations SET status = 'revoked'
                        WHERE lower(email) = %s AND status IN ('sent', 'accepted')
                    ''', (e,))
            msg = (f'Profile email was {old_profile or "(none)"}; synced to login '
                   f'{login_email}. Old address is free.')
        else:
            execute_modify(conn, '''
                UPDATE players SET email = NULL, email_verified = FALSE WHERE id = %s
            ''', (player_id,))
            for e in emails:
                execute_modify(conn, '''
                    UPDATE invitations SET status = 'revoked'
                    WHERE lower(email) = %s AND status IN ('sent', 'accepted')
                ''', (e,))
            execute_modify(conn, '''
                UPDATE invitations SET status = 'revoked'
                WHERE player_id = %s AND status IN ('sent', 'accepted')
            ''', (player_id,))
            msg = 'Profile email cleared'
            if remove_login:
                msg += ' and login removed'
            msg += '. Address is free to use elsewhere.'

        audit(conn, admin['id'], 'player_email_cleared', 'players', player_id,
              old={'profile_email': old_profile, 'login_email': login_email,
                   'emails': sorted(emails), 'removed_login': remove_login,
                   'sync_to_login': sync_to_login})
        conn.commit()
        return jsonify({
            'success': True,
            'message': msg,
            'cleared_emails': sorted(emails),
            'login_kept': bool(login_email) and not remove_login,
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/admin/player/<int:player_id>/make-lead', methods=['POST'])
@admin_required
def admin_make_player_lead(player_id):
    """Super admin: make this person the lead of their home team (or a chosen
    family they belong to). Creates a login + password invite when needed."""
    conn = get_db_connection()
    admin = get_current_user()
    try:
        from app.auth import create_action_token
        from app.email_utils import send_set_password_email
        from werkzeug.security import generate_password_hash
        import secrets as _secrets

        data = request.get_json(silent=True) or {}
        send_invite = bool(data.get('send_password_invite', True))
        player = execute_query_one(conn, '''
            SELECT * FROM players WHERE id = %s AND purged_at IS NULL AND archived_at IS NULL
        ''', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found (or archived)'}), 404

        family_id = data.get('family_id') or player.get('family_id')
        if not family_id:
            return jsonify({'error': 'This person has no home family. Move them to a team first.'}), 400
        family_id = int(family_id)
        family = execute_query_one(conn, '''
            SELECT id, name, lead_user_id FROM families
            WHERE id = %s AND archived_at IS NULL
        ''', (family_id,))
        if not family:
            return jsonify({'error': 'Family not found'}), 404

        member = execute_query_one(conn, '''
            SELECT 1 FROM player_family_memberships
            WHERE player_id = %s AND family_id = %s AND status = 'active'
        ''', (player_id, family_id))
        if not member:
            # Ensure they belong to the team before leading it.
            set_player_home_family(conn, player_id, family_id)

        lead_user = execute_query_one(conn, 'SELECT * FROM users WHERE player_id = %s', (player_id,))
        created_login = False
        if not lead_user:
            email = (data.get('email') or player.get('email') or '').strip().lower()
            if not email or '@' not in email:
                return jsonify({
                    'error': 'This person has no login. Provide an email to create one, then they can be lead.',
                }), 400
            taken_player = execute_query_one(conn, '''
                SELECT id, COALESCE(display_name, first_name) AS display_name
                FROM players
                WHERE lower(email) = %s AND id <> %s
                  AND email IS NOT NULL AND email <> ''
            ''', (email, player_id))
            if taken_player:
                return jsonify({
                    'error': f'That email is already on {taken_player["display_name"]}. Merge first.',
                    'conflict_player_id': taken_player['id'],
                    'suggest_merge': True,
                }), 409
            existing = execute_query_one(conn, '''
                SELECT id, player_id FROM users WHERE lower(email) = %s
            ''', (email,))
            if existing:
                if existing.get('player_id') and existing['player_id'] != player_id:
                    return jsonify({
                        'error': 'That email already has a login on another person. Merge first.',
                        'conflict_player_id': existing['player_id'],
                        'suggest_merge': True,
                    }), 409
                # Orphan login with this email — bind it.
                execute_modify(conn, '''
                    UPDATE users SET player_id = %s, family_id = %s,
                        is_verified = TRUE, is_approved = TRUE, is_active = TRUE,
                        role = 'family_admin'
                    WHERE id = %s
                ''', (player_id, family_id, existing['id']))
                lead_user = execute_query_one(conn, 'SELECT * FROM users WHERE id = %s', (existing['id'],))
            else:
                temp = data.get('temp_password') or (_secrets.token_urlsafe(10) + 'Aa1!')
                lead_user = execute_query_one(conn, '''
                    INSERT INTO users (email, password_hash, first_name, last_name, family_name,
                        role, is_verified, is_approved, is_active, family_id, player_id)
                    VALUES (%s, %s, %s, %s, %s, 'family_admin', TRUE, TRUE, TRUE, %s, %s)
                    RETURNING *
                ''', (email, generate_password_hash(temp),
                      player['first_name'], player['last_name'], family['name'],
                      family_id, player_id))
                created_login = True
            execute_modify(conn, 'UPDATE players SET email = COALESCE(email, %s) WHERE id = %s',
                           (email, player_id))

        # Previous lead membership becomes member; this person becomes home + lead.
        execute_modify(conn, "UPDATE player_family_memberships SET role = 'member' WHERE family_id = %s",
                       (family_id,))
        set_player_home_family(conn, player_id, family_id)
        execute_modify(conn, '''
            UPDATE player_family_memberships SET role = 'lead', status = 'active'
            WHERE player_id = %s AND family_id = %s
        ''', (player_id, family_id))
        execute_modify(conn, 'UPDATE families SET lead_user_id = %s WHERE id = %s',
                       (lead_user['id'], family_id))
        execute_modify(conn, '''
            UPDATE users SET family_id = %s,
                role = CASE WHEN role = 'super_admin' THEN role ELSE 'family_admin' END,
                is_active = TRUE, is_approved = TRUE
            WHERE id = %s
        ''', (family_id, lead_user['id']))

        invite_sent = False
        if send_invite and (created_login or data.get('force_invite')):
            token = create_action_token(
                'set_password', player_id=player_id, user_id=lead_user['id'],
                family_id=family_id, ttl_hours=168)
            if token:
                invite_sent = send_set_password_email(
                    lead_user['email'],
                    player.get('display_name') or player['first_name'],
                    family['name'],
                    f"{admin.get('first_name', '')} {admin.get('last_name', '')}".strip() or 'Admin',
                    f"{APP_BASE_URL}/auth/set-password/{token}",
                )

        if family.get('lead_user_id') and family['lead_user_id'] != lead_user['id']:
            notify_user(conn, family['lead_user_id'], 'family_lead_transfer',
                'Family leadership changed',
                f"A super admin made {player.get('display_name') or player['first_name']} "
                f"the lead of {family['name']}.")
        notify_user(conn, lead_user['id'], 'family_lead_transfer',
            'You are now a family lead',
            f"You have been made lead of {family['name']}.")

        audit(conn, admin['id'], 'player_made_lead', 'families', family_id,
              old={'previous_lead_user_id': family.get('lead_user_id')},
              new={'player_id': player_id, 'lead_user_id': lead_user['id'],
                   'created_login': created_login, 'invite_sent': invite_sent})
        conn.commit()
        msg = f"{player.get('display_name') or player['first_name']} is now lead of {family['name']}."
        if created_login:
            msg += ' A login was created for them.'
        if invite_sent:
            msg += ' Password setup email sent.'
        elif created_login and send_invite:
            msg += ' Login created, but the password email could not be sent.'
        return jsonify({
            'success': True,
            'message': msg,
            'family_id': family_id,
            'lead_user_id': lead_user['id'],
            'created_login': created_login,
            'invite_sent': invite_sent,
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/admin/create-family-for-player', methods=['POST'])
@admin_required
def admin_create_family_for_player():
    """Create a team, make a player its lead (creating/linking login as needed),
    optionally move other players onto that team, optionally email set-password."""
    conn = get_db_connection()
    admin = get_current_user()
    try:
        from app.auth import create_action_token
        from app.email_utils import send_set_password_email
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        player_id = data.get('player_id')
        move_ids = [int(x) for x in (data.get('move_player_ids') or []) if x]
        send_invite = bool(data.get('send_password_invite'))
        if not name:
            return jsonify({'error': 'Family name is required'}), 400
        if not player_id:
            return jsonify({'error': 'player_id is required'}), 400
        player_id = int(player_id)
        player = execute_query_one(conn, '''
            SELECT * FROM players WHERE id = %s AND purged_at IS NULL
        ''', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found'}), 404

        lead_user = execute_query_one(conn, 'SELECT * FROM users WHERE player_id = %s', (player_id,))
        if not lead_user:
            email = (data.get('email') or player.get('email') or '').strip().lower()
            if not email:
                return jsonify({'error': 'This person has no login. Provide an email to create one.'}), 400
            taken_player = execute_query_one(conn, '''
                SELECT id, COALESCE(display_name, first_name) AS display_name
                FROM players
                WHERE lower(email) = %s AND id <> %s
                  AND email IS NOT NULL AND email <> ''
            ''', (email, player_id))
            if taken_player:
                return jsonify({
                    'error': f'That email is already on {taken_player["display_name"]}. '
                             'Merge those two people first, then create the team.',
                    'conflict_player_id': taken_player['id'],
                    'suggest_merge': True,
                }), 409
            existing = execute_query_one(conn, '''
                SELECT id, player_id FROM users WHERE lower(email) = %s
            ''', (email,))
            if existing:
                return jsonify({
                    'error': 'That email already has a login. Merge or remove that login first.',
                    'conflict_user_id': existing['id'],
                    'conflict_player_id': existing.get('player_id'),
                    'suggest_merge': bool(existing.get('player_id')),
                }), 409
            from werkzeug.security import generate_password_hash
            import secrets as _secrets
            temp = data.get('temp_password') or (_secrets.token_urlsafe(10) + 'Aa1!')
            lead_user = execute_query_one(conn, '''
                INSERT INTO users (email, password_hash, first_name, last_name, family_name,
                    role, is_verified, is_approved, is_active, player_id)
                VALUES (%s, %s, %s, %s, %s, 'family_admin', TRUE, TRUE, TRUE, %s)
                RETURNING *
            ''', (email, generate_password_hash(temp),
                  player['first_name'], player['last_name'], name, player_id))
            execute_modify(conn, 'UPDATE players SET email = COALESCE(email, %s) WHERE id = %s',
                           (email, player_id))

        fam = execute_query_one(conn, '''
            INSERT INTO families (name, slug, lead_user_id, created_by_user_id)
            VALUES (%s, %s, %s, %s) RETURNING id, name
        ''', (name, unique_family_slug(conn, name), lead_user['id'], admin['id']))
        family_id = fam['id']

        set_player_home_family(conn, player_id, family_id)
        execute_modify(conn, '''
            UPDATE player_family_memberships SET role = 'lead'
            WHERE player_id = %s AND family_id = %s
        ''', (player_id, family_id))
        execute_modify(conn, 'UPDATE users SET family_id = %s, role = COALESCE(role, %s) WHERE id = %s',
                       (family_id, 'family_admin', lead_user['id']))

        moved = 0
        for pid in move_ids:
            if pid == player_id:
                continue
            set_player_home_family(conn, pid, family_id)
            moved += 1

        invite_sent = False
        invite_link = None
        if send_invite and lead_user.get('email'):
            token = create_action_token(
                'set_password', player_id=player_id, user_id=lead_user['id'],
                family_id=family_id, ttl_hours=168)
            if token:
                invite_link = f"{APP_BASE_URL}/auth/set-password/{token}"
                invite_sent = send_set_password_email(
                    lead_user['email'],
                    player.get('display_name') or player['first_name'],
                    name,
                    f"{admin.get('first_name', '')} {admin.get('last_name', '')}".strip() or 'Admin',
                    invite_link,
                )

        audit(conn, admin['id'], 'family_created_for_player', 'families', family_id,
              new={'player_id': player_id, 'moved': moved, 'invite_sent': invite_sent})
        conn.commit()
        return jsonify({
            'success': True,
            'family_id': family_id,
            'family_name': fam['name'],
            'lead_user_id': lead_user['id'],
            'moved': moved,
            'invite_sent': invite_sent,
            'message': f'Created {fam["name"]}, made them lead'
                       + (f', moved {moved} other member(s)' if moved else '')
                       + (', and emailed a password setup link' if invite_sent else '')
                       + '.',
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

def notify_user(conn, user_id, ntype, title, message, data=None):
    """Insert an in-app notification (best effort; caller commits).
    Ensures data.url exists so the bell row can navigate to a destination."""
    if not user_id:
        return
    payload = dict(data or {})
    if not payload.get('url'):
        for act in (payload.get('actions') or []):
            if (act.get('method') or 'POST').upper() == 'GET' and act.get('url'):
                payload['url'] = act['url']
                break
        if not payload.get('url'):
            defaults = {
                'crew_up_request': '/my-team',
                'crew_up_accepted': '/my-team',
                'family_join_request': '/my-team',
                'family_join_approved': '/my-team',
                'family_join_denied': '/my-team',
                'family_invite': '/my-team',
                'release_request': '/my-team',
                'release_decided': '/my-team',
                'home_family_changed': '/my-team',
                'family_lead_transfer': '/my-team',
                'player_claim_request': '/my-team',
                'player_claim_approved': '/profile',
                'possible_duplicate': '/my-team',
                'user_pending_approval': '/auth/admin/users',
                'personal_crew_request': '/my-team',
                'personal_crew_accepted': '/my-team',
                'personal_crew_ended': '/my-team',
            }
            if ntype in defaults:
                payload['url'] = defaults[ntype]
            elif payload.get('feedback_id') and ntype in ('user_feedback', 'user_feedback_reply'):
                # Admins land on admin detail; submitter replies use /feedback/<id> when set by caller.
                payload['url'] = f"/admin/feedback/{payload['feedback_id']}"
    execute_modify(conn, '''
        INSERT INTO notifications (user_id, type, title, message, data)
        VALUES (%s, %s, %s, %s, %s)
    ''', (user_id, ntype, title, message, json.dumps(payload)))

def notify_family_lead(conn, family_id, ntype, title, message, data=None):
    lead = execute_query_one(conn, 'SELECT lead_user_id FROM families WHERE id = %s', (family_id,))
    if lead and lead.get('lead_user_id'):
        notify_user(conn, lead['lead_user_id'], ntype, title, message, data)


def notify_super_admins(conn, ntype, title, message, data=None):
    """Notify every active super_admin (site owners / operators)."""
    admins = execute_query(conn, '''
        SELECT id FROM users
        WHERE role = 'super_admin' AND is_active = TRUE AND archived_at IS NULL
    ''')
    for admin in admins:
        notify_user(conn, admin['id'], ntype, title, message, data)


FEEDBACK_CATEGORIES = {
    'bug': 'Bug report',
    'enhancement': 'Enhancement request',
    'new_game': 'New game request',
    'feedback': 'General feedback',
    'other': 'Other',
}
FEEDBACK_STATUSES = {
    'open': 'Open',
    'in_progress': 'In progress',
    'closed': 'Closed',
}


def _record_feedback_view(conn, feedback_id, user_id):
    """Upsert paper-trail row: first_seen stays, last_seen updates."""
    execute_modify(conn, '''
        INSERT INTO feedback_views (feedback_id, user_id, first_seen_at, last_seen_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (feedback_id, user_id) DO UPDATE
        SET last_seen_at = CURRENT_TIMESTAMP
    ''', (feedback_id, user_id))
    execute_modify(conn, '''
        UPDATE notifications
        SET is_read = TRUE
        WHERE user_id = %s AND is_read = FALSE
          AND type IN ('user_feedback', 'user_feedback_reply')
          AND (data->>'feedback_id') = %s
    ''', (user_id, str(feedback_id)))


def _feedback_replies(conn, feedback_id):
    return list(execute_query(conn, '''
        SELECT r.id, r.body, r.created_at, r.user_id,
               u.first_name, u.last_name, u.email, u.role
        FROM feedback_replies r
        JOIN users u ON u.id = r.user_id
        WHERE r.feedback_id = %s
        ORDER BY r.created_at ASC
    ''', (feedback_id,)))


def _can_access_feedback(user, item):
    if not item:
        return False
    if user.get('role') == 'super_admin':
        return True
    return item.get('user_id') == user.get('id')


@main.route('/feedback', methods=['GET', 'POST'])
@login_required
def feedback_page():
    """Any logged-in user can send feedback to super admins."""
    user = get_current_user()
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            category = (request.form.get('category') or '').strip()
            subject = (request.form.get('subject') or '').strip()
            body = (request.form.get('body') or '').strip()
            if category not in FEEDBACK_CATEGORIES:
                flash('Choose a feedback type.', 'error')
                return redirect(url_for('main.feedback_page'))
            if not subject or len(subject) > 200:
                flash('Enter a short subject (max 200 characters).', 'error')
                return redirect(url_for('main.feedback_page'))
            if not body or len(body) > 10000:
                flash('Enter your message (max 10,000 characters).', 'error')
                return redirect(url_for('main.feedback_page'))

            row = execute_query_one(conn, '''
                INSERT INTO feedback_items (user_id, category, subject, body)
                VALUES (%s, %s, %s, %s) RETURNING id
            ''', (user['id'], category, subject, body))
            cat_label = FEEDBACK_CATEGORIES[category]
            who = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('email')
            detail_url = f"/admin/feedback/{row['id']}"
            notify_super_admins(
                conn, 'user_feedback',
                f'{cat_label}: {subject}',
                f'{who} sent {cat_label.lower()}.',
                {
                    'feedback_id': row['id'],
                    'category': category,
                    'url': detail_url,
                    'actions': [{
                        'label': 'Open',
                        'style': 'primary',
                        'method': 'GET',
                        'url': detail_url,
                    }],
                })
            conn.commit()
            flash('Thanks. Your message was sent to the site admins.', 'success')
            return redirect(url_for('main.feedback_detail', feedback_id=row['id']))

        mine = list(execute_query(conn, '''
            SELECT id, category, subject, status, created_at,
                   (SELECT COUNT(*) FROM feedback_replies r WHERE r.feedback_id = feedback_items.id) AS reply_count
            FROM feedback_items
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 20
        ''', (user['id'],)))
        return render_template('feedback.html',
                               categories=FEEDBACK_CATEGORIES,
                               statuses=FEEDBACK_STATUSES,
                               my_items=mine)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@main.route('/feedback/<int:feedback_id>', methods=['GET', 'POST'])
@login_required
def feedback_detail(feedback_id):
    """Submitter thread view (admins use /admin/feedback/<id>)."""
    user = get_current_user()
    conn = get_db_connection()
    try:
        item = execute_query_one(conn, '''
            SELECT f.*, u.first_name, u.last_name, u.email, u.family_name
            FROM feedback_items f
            JOIN users u ON u.id = f.user_id
            WHERE f.id = %s
        ''', (feedback_id,))
        if not _can_access_feedback(user, item):
            flash('That feedback item was not found.', 'error')
            return redirect(url_for('main.feedback_page'))
        # Super admins use the admin inbox unless they are the original submitter.
        if user.get('role') == 'super_admin' and item.get('user_id') != user.get('id'):
            return redirect(url_for('main.admin_feedback_detail', feedback_id=feedback_id))

        if request.method == 'POST':
            body = (request.form.get('body') or '').strip()
            if not body or len(body) > 10000:
                flash('Enter a reply (max 10,000 characters).', 'error')
                return redirect(url_for('main.feedback_detail', feedback_id=feedback_id))
            execute_query_one(conn, '''
                INSERT INTO feedback_replies (feedback_id, user_id, body)
                VALUES (%s, %s, %s) RETURNING id
            ''', (feedback_id, user['id'], body))
            execute_modify(conn, '''
                UPDATE feedback_items SET updated_at = CURRENT_TIMESTAMP,
                    status = CASE WHEN status = 'closed' THEN 'open' ELSE status END
                WHERE id = %s
            ''', (feedback_id,))
            who = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('email')
            admin_url = f"/admin/feedback/{feedback_id}"
            notify_super_admins(
                conn, 'user_feedback_reply',
                f'Reply: {item["subject"]}',
                f'{who} replied to feedback #{feedback_id}.',
                {
                    'feedback_id': feedback_id,
                    'url': admin_url,
                    'actions': [{'label': 'Open', 'style': 'primary', 'method': 'GET', 'url': admin_url}],
                })
            conn.commit()
            flash('Reply sent.', 'success')
            return redirect(url_for('main.feedback_detail', feedback_id=feedback_id))

        replies = _feedback_replies(conn, feedback_id)
        return render_template('feedback_detail.html',
                               item=item,
                               replies=replies,
                               categories=FEEDBACK_CATEGORIES,
                               statuses=FEEDBACK_STATUSES,
                               is_admin=False)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@main.route('/api/feedback', methods=['POST'])
@login_required
def api_submit_feedback():
    """JSON submit (used by smoke tests and alternate clients)."""
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    category = (data.get('category') or '').strip()
    subject = (data.get('subject') or '').strip()
    body = (data.get('body') or '').strip()
    if category not in FEEDBACK_CATEGORIES:
        return jsonify({'error': 'Choose a feedback type'}), 400
    if not subject or len(subject) > 200:
        return jsonify({'error': 'Subject is required (max 200 characters)'}), 400
    if not body or len(body) > 10000:
        return jsonify({'error': 'Message is required (max 10,000 characters)'}), 400
    conn = get_db_connection()
    try:
        row = execute_query_one(conn, '''
            INSERT INTO feedback_items (user_id, category, subject, body)
            VALUES (%s, %s, %s, %s) RETURNING id, category, subject, status, created_at
        ''', (user['id'], category, subject, body))
        cat_label = FEEDBACK_CATEGORIES[category]
        who = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('email')
        detail_url = f"/admin/feedback/{row['id']}"
        notify_super_admins(
            conn, 'user_feedback',
            f'{cat_label}: {subject}',
            f'{who} sent {cat_label.lower()}.',
            {
                'feedback_id': row['id'],
                'category': category,
                'url': detail_url,
                'actions': [{
                    'label': 'Open',
                    'style': 'primary',
                    'method': 'GET',
                    'url': detail_url,
                }],
            })
        conn.commit()
        return jsonify({
            'message': 'Feedback sent to site admins.',
            'id': row['id'],
            'feedback': {
                'id': row['id'], 'category': row['category'],
                'subject': row['subject'], 'status': row['status'],
            },
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/feedback/<int:feedback_id>/replies', methods=['POST'])
@login_required
def api_feedback_reply(feedback_id):
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    body = (data.get('body') or '').strip()
    if not body or len(body) > 10000:
        return jsonify({'error': 'Reply text is required'}), 400
    conn = get_db_connection()
    try:
        item = execute_query_one(conn, 'SELECT * FROM feedback_items WHERE id = %s', (feedback_id,))
        if not _can_access_feedback(user, item):
            return jsonify({'error': 'Feedback not found'}), 404
        reply = execute_query_one(conn, '''
            INSERT INTO feedback_replies (feedback_id, user_id, body)
            VALUES (%s, %s, %s) RETURNING id, created_at
        ''', (feedback_id, user['id'], body))
        execute_modify(conn, '''
            UPDATE feedback_items SET updated_at = CURRENT_TIMESTAMP,
                status = CASE WHEN status = 'closed' THEN 'open' ELSE status END
            WHERE id = %s
        ''', (feedback_id,))
        who = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('email')
        is_admin = user.get('role') == 'super_admin'
        if is_admin and item['user_id'] != user['id']:
            user_url = f"/feedback/{feedback_id}"
            notify_user(conn, item['user_id'], 'user_feedback_reply',
                        f'Reply: {item["subject"]}',
                        'A site admin replied to your feedback.',
                        {
                            'feedback_id': feedback_id,
                            'url': user_url,
                            'actions': [{'label': 'Open', 'style': 'primary', 'method': 'GET', 'url': user_url}],
                        })
            _record_feedback_view(conn, feedback_id, user['id'])
        else:
            admin_url = f"/admin/feedback/{feedback_id}"
            notify_super_admins(
                conn, 'user_feedback_reply',
                f'Reply: {item["subject"]}',
                f'{who} replied to feedback #{feedback_id}.',
                {
                    'feedback_id': feedback_id,
                    'url': admin_url,
                    'actions': [{'label': 'Open', 'style': 'primary', 'method': 'GET', 'url': admin_url}],
                })
        conn.commit()
        return jsonify({'message': 'Reply sent.', 'id': reply['id']})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/admin/feedback')
@admin_required
def admin_feedback_list():
    conn = get_db_connection()
    try:
        status = (request.args.get('status') or '').strip()
        q = (request.args.get('q') or '').strip()
        clauses = []
        params = []
        if status in FEEDBACK_STATUSES:
            clauses.append('f.status = %s')
            params.append(status)
        if q:
            like = f'%{q}%'
            clauses.append('''(
                f.subject ILIKE %s OR f.body ILIKE %s
                OR u.first_name ILIKE %s OR u.last_name ILIKE %s OR u.email ILIKE %s
                OR EXISTS (
                    SELECT 1 FROM feedback_replies r
                    WHERE r.feedback_id = f.id AND r.body ILIKE %s
                )
            )''')
            params.extend([like, like, like, like, like, like])
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = list(execute_query(conn, f'''
            SELECT f.id, f.category, f.subject, f.status, f.created_at,
                   u.first_name, u.last_name, u.email,
                   (SELECT COUNT(*) FROM feedback_views v WHERE v.feedback_id = f.id) AS seen_count,
                   (SELECT COUNT(*) FROM feedback_replies r WHERE r.feedback_id = f.id) AS reply_count
            FROM feedback_items f
            JOIN users u ON u.id = f.user_id
            {where}
            ORDER BY
                CASE f.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                f.created_at DESC
            LIMIT 200
        ''', tuple(params) if params else None))
        open_count = execute_query_one(conn, '''
            SELECT COUNT(*) AS n FROM feedback_items WHERE status = 'open'
        ''')['n']
        return render_template('admin_feedback.html',
                               items=rows,
                               categories=FEEDBACK_CATEGORIES,
                               statuses=FEEDBACK_STATUSES,
                               filter_status=status,
                               filter_q=q,
                               open_count=open_count)
    finally:
        conn.close()


@main.route('/admin/feedback/<int:feedback_id>', methods=['GET', 'POST'])
@admin_required
def admin_feedback_detail(feedback_id):
    user = get_current_user()
    conn = get_db_connection()
    try:
        item = execute_query_one(conn, '''
            SELECT f.*, u.first_name, u.last_name, u.email, u.family_name
            FROM feedback_items f
            JOIN users u ON u.id = f.user_id
            WHERE f.id = %s
        ''', (feedback_id,))
        if not item:
            flash('That feedback item was not found.', 'error')
            return redirect(url_for('main.admin_feedback_list'))

        if request.method == 'POST':
            body = (request.form.get('body') or '').strip()
            if not body or len(body) > 10000:
                flash('Enter a reply (max 10,000 characters).', 'error')
                return redirect(url_for('main.admin_feedback_detail', feedback_id=feedback_id))
            execute_query_one(conn, '''
                INSERT INTO feedback_replies (feedback_id, user_id, body)
                VALUES (%s, %s, %s) RETURNING id
            ''', (feedback_id, user['id'], body))
            execute_modify(conn, '''
                UPDATE feedback_items SET updated_at = CURRENT_TIMESTAMP WHERE id = %s
            ''', (feedback_id,))
            user_url = f"/feedback/{feedback_id}"
            notify_user(conn, item['user_id'], 'user_feedback_reply',
                        f'Reply: {item["subject"]}',
                        'A site admin replied to your feedback.',
                        {
                            'feedback_id': feedback_id,
                            'url': user_url,
                            'actions': [{'label': 'Open', 'style': 'primary', 'method': 'GET', 'url': user_url}],
                        })
            _record_feedback_view(conn, feedback_id, user['id'])
            conn.commit()
            flash('Reply sent.', 'success')
            return redirect(url_for('main.admin_feedback_detail', feedback_id=feedback_id))

        _record_feedback_view(conn, feedback_id, user['id'])
        conn.commit()

        viewers = list(execute_query(conn, '''
            SELECT v.first_seen_at, v.last_seen_at,
                   u.id AS user_id, u.first_name, u.last_name, u.email
            FROM feedback_views v
            JOIN users u ON u.id = v.user_id
            WHERE v.feedback_id = %s
            ORDER BY v.first_seen_at ASC
        ''', (feedback_id,)))
        replies = _feedback_replies(conn, feedback_id)
        return render_template('admin_feedback_detail.html',
                               item=item,
                               viewers=viewers,
                               replies=replies,
                               categories=FEEDBACK_CATEGORIES,
                               statuses=FEEDBACK_STATUSES)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@main.route('/api/admin/feedback/<int:feedback_id>/status', methods=['POST'])
@admin_required
def admin_feedback_set_status(feedback_id):
    data = request.get_json(silent=True) or {}
    status = (data.get('status') or '').strip()
    if status not in FEEDBACK_STATUSES:
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db_connection()
    user = get_current_user()
    try:
        row = execute_query_one(conn, '''
            UPDATE feedback_items
            SET status = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING id, status
        ''', (status, feedback_id))
        if not row:
            return jsonify({'error': 'Feedback not found'}), 404
        _record_feedback_view(conn, feedback_id, user['id'])
        conn.commit()
        return jsonify({'message': f'Status set to {FEEDBACK_STATUSES[status]}.',
                        'status': row['status']})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/my-team')
@login_required
def my_team():
    conn = get_db_connection()
    user = get_current_user()
    try:
        family_id = user.get('family_id')
        if not family_id:
            return redirect(url_for('main.dashboard'))

        family = execute_query_one(conn, 'SELECT * FROM families WHERE id = %s', (family_id,))
        is_lead = is_family_lead(conn, user, family_id)
        my_player_id = user.get('player_id')

        # Active roster (people who belong to this family, home or guest).
        players = list(execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                p.email as player_email, p.is_discoverable, p.is_minor,
                m.is_primary AS is_home, m.role AS membership_role,
                p.family_id AS home_family_id, hf.name AS home_family_name,
                acc.id AS account_id, acc.email AS account_email
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            LEFT JOIN families hf ON hf.id = p.family_id
            LEFT JOIN users acc ON acc.player_id = p.id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
            ORDER BY m.is_primary DESC, p.first_name, p.last_name
        ''', (family_id,)))

        # Pending join/invite requests awaiting the lead's decision.
        pending = list(execute_query(conn, '''
            SELECT m.id AS membership_id, m.status, p.id AS player_id,
                COALESCE(p.display_name, p.first_name) AS display_name,
                hf.name AS home_family_name, acc.email AS account_email
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            LEFT JOIN families hf ON hf.id = p.family_id
            LEFT JOIN users acc ON acc.player_id = p.id
            WHERE m.family_id = %s AND m.status IN ('requested', 'invited')
              AND p.archived_at IS NULL
            ORDER BY m.joined_at DESC
        ''', (family_id,)))

        # The current user's own memberships (for switching primary family).
        my_memberships = []
        joinable_families = []
        if my_player_id:
            my_memberships = list(execute_query(conn, '''
                SELECT m.id, m.family_id, f.name, m.is_primary, m.status
                FROM player_family_memberships m
                JOIN families f ON f.id = m.family_id
                WHERE m.player_id = %s
                ORDER BY m.is_primary DESC, f.name
            ''', (my_player_id,)))
            joinable_families = list(execute_query(conn, '''
                SELECT id, name FROM families
                WHERE archived_at IS NULL AND id NOT IN (
                    SELECT family_id FROM player_family_memberships WHERE player_id = %s
                )
                ORDER BY name
            ''', (my_player_id,)))
        else:
            joinable_families = list(execute_query(conn,
                'SELECT id, name FROM families WHERE archived_at IS NULL ORDER BY name'))

        # If the user has no identity yet, they can claim an unclaimed roster spot.
        claimable = []
        if not my_player_id:
            claimable = list(execute_query(conn, '''
                SELECT p.id, COALESCE(p.display_name, p.first_name) AS display_name
                FROM player_family_memberships m
                JOIN players p ON p.id = m.player_id
                WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM users u WHERE u.player_id = p.id)
                ORDER BY display_name
            ''', (family_id,)))

        # Members with accounts are eligible to become lead.
        lead_candidates = list(execute_query(conn, '''
            SELECT acc.id AS user_id, COALESCE(p.display_name, p.first_name) AS display_name
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            JOIN users acc ON acc.player_id = p.id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
            ORDER BY display_name
        ''', (family_id,)))

        alliances = list(execute_query(conn, '''
            SELECT fa.id, fa.status,
                CASE WHEN fa.requesting_family_id = %s THEN tf.name ELSE rf.name END as ally_name,
                CASE WHEN fa.requesting_family_id = %s THEN fa.target_family_id ELSE fa.requesting_family_id END as ally_id
            FROM family_alliances fa
            JOIN families rf ON fa.requesting_family_id = rf.id
            JOIN families tf ON fa.target_family_id = tf.id
            WHERE fa.status = 'accepted'
            AND (fa.requesting_family_id = %s OR fa.target_family_id = %s)
        ''', (family_id, family_id, family_id, family_id)))

        personal_crew = {'pending_in': [], 'pending_out': [], 'accepted': []}
        if my_player_id:
            links = list(execute_query(conn, '''
                SELECT l.id, l.status, l.requested_by_player_id,
                       l.player_a_id, l.player_b_id,
                       CASE WHEN l.player_a_id = %s THEN l.player_b_id ELSE l.player_a_id END AS other_id
                FROM player_crew_links l
                WHERE (l.player_a_id = %s OR l.player_b_id = %s)
                  AND l.status IN ('pending', 'accepted')
            ''', (my_player_id, my_player_id, my_player_id)))
            other_ids = [l['other_id'] for l in links]
            others = {}
            if other_ids:
                for row in execute_query(conn, '''
                    SELECT p.id, COALESCE(p.display_name, p.first_name) AS display_name,
                           f.name AS family_name
                    FROM players p
                    LEFT JOIN families f ON f.id = p.family_id
                    WHERE p.id = ANY(%s)
                ''', (other_ids,)):
                    others[row['id']] = row
            for l in links:
                other = others.get(l['other_id']) or {'display_name': 'Player', 'family_name': ''}
                entry = {
                    'id': l['id'],
                    'other_player_id': l['other_id'],
                    'display_name': other['display_name'],
                    'family_name': other.get('family_name') or '',
                    'i_requested': l['requested_by_player_id'] == my_player_id,
                }
                if l['status'] == 'accepted':
                    personal_crew['accepted'].append(entry)
                elif entry['i_requested']:
                    personal_crew['pending_out'].append(entry)
                else:
                    personal_crew['pending_in'].append(entry)

        return render_template('my_team.html',
            family=family, players=players, alliances=alliances,
            pending=pending, my_memberships=my_memberships,
            joinable_families=joinable_families, claimable=claimable,
            lead_candidates=lead_candidates,
            personal_crew=personal_crew,
            lead_user_id=family.get('lead_user_id') if family else None,
            my_player_id=my_player_id, is_lead=is_lead, user=user)
    finally:
        conn.close()

@main.route('/api/team/players', methods=['POST'])
@login_required
def team_add_player():
    conn = get_db_connection()
    user = get_current_user()
    try:
        family_id = user.get('family_id')
        if not is_family_lead(conn, user, family_id):
            return jsonify({'error': 'Only the family lead can add players'}), 403
        data = request.json
        first_name = data.get('first_name', '').strip()
        last_name = data.get('last_name', '').strip()
        display_name = data.get('display_name', '').strip()
        email = data.get('email', '').strip() or None
        if not first_name or not last_name or not display_name:
            return jsonify({'error': 'First name, last name, and display name are required'}), 400

        new_player = execute_query_one(conn, '''
            INSERT INTO players (first_name, last_name, display_name, family_id, email,
                                 is_minor, created_by_user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        ''', (first_name, last_name, display_name, family_id, email,
              bool(data.get('is_minor', False)), user['id']))
        execute_modify(conn, '''
            INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
            VALUES (%s, %s, TRUE, 'active', 'member')
            ON CONFLICT (player_id, family_id) DO NOTHING
        ''', (new_player['id'], family_id))
        conn.commit()
        return jsonify({'id': new_player['id'], 'message': 'Player added'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/team/players/<int:player_id>', methods=['PUT'])
@login_required
def team_edit_player(player_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        family_id = user.get('family_id')
        is_lead = is_family_lead(conn, user, family_id)
        is_self = user.get('player_id') == player_id
        if not is_lead and not is_self:
            return jsonify({'error': 'You can only edit your own profile'}), 403
        # Player must belong to this family (any membership).
        member = execute_query_one(conn, '''
            SELECT p.*,
                EXISTS (SELECT 1 FROM users u WHERE u.player_id = p.id) AS has_login
            FROM players p
            JOIN player_family_memberships m ON m.player_id = p.id
            WHERE p.id = %s AND m.family_id = %s
        ''', (player_id, family_id))
        if not member:
            return jsonify({'error': 'Player not found in your family'}), 404

        data = request.json or {}
        # Empty string must clear email (NULL). Never fall back to the old value.
        if 'email' in data:
            email = (data.get('email') or '').strip().lower() or None
        else:
            email = member.get('email')
        if email:
            taken = execute_query_one(conn, '''
                SELECT id FROM players
                WHERE lower(email) = %s AND id <> %s
                  AND email IS NOT NULL AND email <> ''
            ''', (email, player_id))
            if taken:
                return jsonify({'error': 'That email is already on another player profile'}), 409

        # Public visibility: people with a login control their own setting
        # (Profile / self-edit). Leads may set it only for people without a login.
        is_discoverable = member.get('is_discoverable', True)
        if 'is_discoverable' in data:
            want = bool(data.get('is_discoverable'))
            if is_self or (is_lead and not member.get('has_login')):
                is_discoverable = want
            elif is_lead and member.get('has_login') and want != bool(member.get('is_discoverable')):
                return jsonify({
                    'error': 'This person has a login and controls their own public visibility. '
                             'Ask them to change it on their Profile.',
                }), 403

        execute_modify(conn, '''
            UPDATE players SET first_name = %s, last_name = %s, display_name = %s,
                email = %s, is_discoverable = %s WHERE id = %s
        ''', (data.get('first_name', member['first_name']),
              data.get('last_name', member['last_name']),
              data.get('display_name', member.get('display_name')),
              email,
              is_discoverable,
              player_id))
        conn.commit()
        return jsonify({'message': 'Player updated'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/team/settings', methods=['PUT'])
@login_required
def team_update_settings():
    """Family lead: toggle team discoverability and public roster visibility."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        family_id = user.get('family_id')
        if not family_id or not is_family_lead(conn, user, family_id):
            return jsonify({'error': 'Only the team lead can change these settings'}), 403
        data = request.get_json(silent=True) or {}
        family = execute_query_one(conn, '''
            SELECT id, name, is_discoverable, show_roster FROM families
            WHERE id = %s AND archived_at IS NULL
        ''', (family_id,))
        if not family:
            return jsonify({'error': 'Family not found'}), 404
        is_discoverable = (bool(data['is_discoverable']) if 'is_discoverable' in data
                           else bool(family['is_discoverable']))
        show_roster = (bool(data['show_roster']) if 'show_roster' in data
                       else bool(family['show_roster']))
        execute_modify(conn, '''
            UPDATE families SET is_discoverable = %s, show_roster = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (is_discoverable, show_roster, family_id))
        audit(conn, user['id'], 'family_privacy_updated', 'families', family_id,
              old={'is_discoverable': family['is_discoverable'],
                   'show_roster': family['show_roster']},
              new={'is_discoverable': is_discoverable, 'show_roster': show_roster})
        conn.commit()
        return jsonify({
            'success': True,
            'message': 'Team visibility updated.',
            'is_discoverable': is_discoverable,
            'show_roster': show_roster,
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/team/players/<int:player_id>', methods=['DELETE'])
@login_required
def team_remove_player(player_id):
    """Remove a player's membership in the lead's family. If it was their only
    family and they have no history, the player record is deleted too."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        family_id = user.get('family_id')
        if not is_family_lead(conn, user, family_id):
            return jsonify({'error': 'Only the family lead can remove players'}), 403
        membership = execute_query_one(conn, '''
            SELECT m.*, p.first_name FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            WHERE m.player_id = %s AND m.family_id = %s
        ''', (player_id, family_id))
        if not membership:
            return jsonify({'error': 'Player not found in your family'}), 404

        in_game = execute_query_one(conn, '''
            SELECT COUNT(*) as c FROM active_game_players agp
            JOIN active_games ag ON agp.active_game_id = ag.id
            WHERE agp.player_id = %s AND ag.is_complete = FALSE AND ag.family_id = %s
        ''', (player_id, family_id))
        if in_game and in_game['c'] > 0:
            return jsonify({'error': 'Cannot remove a player who is in an active game'}), 400

        other = execute_query_one(conn, '''
            SELECT COUNT(*) as c FROM player_family_memberships
            WHERE player_id = %s AND family_id <> %s
        ''', (player_id, family_id))
        has_other_family = other and other['c'] > 0

        # Drop the membership in this family.
        execute_modify(conn, '''
            DELETE FROM player_family_memberships WHERE player_id = %s AND family_id = %s
        ''', (player_id, family_id))

        if has_other_family:
            # Ensure the person still has exactly one primary family.
            remaining_primary = execute_query_one(conn, '''
                SELECT 1 FROM player_family_memberships
                WHERE player_id = %s AND is_primary AND status = 'active'
            ''', (player_id,))
            if not remaining_primary:
                execute_modify(conn, '''
                    UPDATE player_family_memberships SET is_primary = TRUE
                    WHERE id = (
                        SELECT id FROM player_family_memberships
                        WHERE player_id = %s AND status = 'active'
                        ORDER BY joined_at ASC, id ASC LIMIT 1
                    )
                ''', (player_id,))
            conn.commit()
            return jsonify({'message': 'Player removed from this family'})

        # No other family: archive instead of delete. Their history stays, they
        # vanish from rosters and pickers, and a super admin can reinstate.
        owner = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (player_id,))
        if owner and owner['id'] != user['id'] and user.get('role') != 'super_admin':
            conn.rollback()
            return jsonify({'error': 'This person owns their profile with a login. They can leave the family themselves, or another family lead can request a transfer.'}), 400

        # Restore the membership row (dropped above) so a reinstated player has
        # a family to come back to; the archive flag is what hides them.
        execute_modify(conn, '''
            INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
            VALUES (%s, %s, TRUE, 'active', 'member')
            ON CONFLICT (player_id, family_id) DO UPDATE SET status = 'active', is_primary = TRUE
        ''', (player_id, family_id))
        execute_modify(conn, '''
            UPDATE players SET archived_at = CURRENT_TIMESTAMP, archived_by_user_id = %s,
                archive_reason = 'Removed from family roster' WHERE id = %s
        ''', (user['id'], player_id))
        audit(conn, user['id'], 'player_archived', 'players', player_id,
              new={'reason': 'Removed from family roster'})
        conn.commit()
        return jsonify({'message': 'Player archived. Their game history is preserved and a super admin can reinstate them.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/families', methods=['POST'])
@login_required
def create_family():
    """Any user can start their own family and become its lead."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.json or {}
        name = (data.get('name') or '').strip()
        make_primary = bool(data.get('make_primary', True))
        if not name:
            return jsonify({'error': 'Family name is required'}), 400

        # Duplicate family names are allowed on purpose (two Seibert households
        # can coexist); the slug and the lead's public name disambiguate them.
        new_family = execute_query_one(conn, '''
            INSERT INTO families (name, slug, lead_user_id, created_by_user_id)
            VALUES (%s, %s, %s, %s) RETURNING id
        ''', (name, unique_family_slug(conn, name), user['id'], user['id']))
        new_family_id = new_family['id']

        my_player_id = user.get('player_id')
        if my_player_id:
            if make_primary:
                execute_modify(conn, '''
                    UPDATE player_family_memberships SET is_primary = FALSE WHERE player_id = %s
                ''', (my_player_id,))
            execute_modify(conn, '''
                INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
                VALUES (%s, %s, %s, 'active', 'lead')
                ON CONFLICT (player_id, family_id)
                DO UPDATE SET is_primary = EXCLUDED.is_primary, status = 'active', role = 'lead'
            ''', (my_player_id, new_family_id, make_primary))
            if make_primary:
                execute_modify(conn, 'UPDATE users SET family_id = %s WHERE id = %s', (new_family_id, user['id']))
        conn.commit()
        return jsonify({'id': new_family_id, 'message': 'Family created'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/families/<int:family_id>/request-join', methods=['POST'])
@login_required
def request_join_family(family_id):
    """Ask a family's lead to add you (your player identity) to their roster."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        my_player_id = user.get('player_id')
        if not my_player_id:
            return jsonify({'error': 'Claim your player profile before joining other families'}), 400
        family = execute_query_one(conn, '''
            SELECT id, name FROM families WHERE id = %s AND archived_at IS NULL
        ''', (family_id,))
        if not family:
            return jsonify({'error': 'Family not found'}), 404
        existing = execute_query_one(conn, '''
            SELECT status FROM player_family_memberships WHERE player_id = %s AND family_id = %s
        ''', (my_player_id, family_id))
        if existing:
            if existing['status'] == 'active':
                return jsonify({'error': 'You are already a member of that family'}), 400
            return jsonify({'error': 'A request is already pending'}), 400

        membership = execute_query_one(conn, '''
            INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
            VALUES (%s, %s, FALSE, 'requested', 'member') RETURNING id
        ''', (my_player_id, family_id))
        me = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s', (my_player_id,))
        my_name = public_person_name(me) if me else user.get('first_name', 'A player')
        notify_family_lead(conn, family_id, 'family_join_request',
            'New family join request',
            f"{my_name} wants to join {family['name']}.",
            {'player_id': my_player_id, 'actions': [
                {'label': 'Approve', 'style': 'success', 'method': 'POST',
                 'url': f"/api/memberships/{membership['id']}/decide?decision=approve"},
                {'label': 'Decline', 'style': 'outline-secondary', 'method': 'POST',
                 'url': f"/api/memberships/{membership['id']}/decide?decision=deny"},
            ]})

        from app.auth import create_action_token
        from app.email_utils import send_join_request_email
        lead = execute_query_one(conn, '''
            SELECT u.email, u.first_name FROM users u
            JOIN families f ON f.lead_user_id = u.id WHERE f.id = %s AND u.is_active = TRUE
        ''', (family_id,))
        if lead and lead.get('email'):
            token = create_action_token('join_family', family_id=family_id,
                                        payload={'membership_id': membership['id']}, ttl_hours=168)
            if token:
                send_join_request_email(lead['email'], lead['first_name'], my_name,
                                        family['name'], f"{APP_BASE_URL}/action/{token}")
        conn.commit()
        return jsonify({'message': 'Request sent to the family lead'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/memberships/<int:membership_id>/decide', methods=['POST'])
@login_required
def decide_membership(membership_id):
    """Family lead approves or denies a pending join/invite request."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        decision = request.args.get('decision') or (request.get_json(silent=True) or {}).get('decision')
        if decision not in ('approve', 'deny'):
            return jsonify({'error': 'Invalid decision'}), 400
        membership = execute_query_one(conn, '''
            SELECT m.*, p.display_name, p.first_name FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id WHERE m.id = %s
        ''', (membership_id,))
        if not membership:
            return jsonify({'error': 'Request not found'}), 404
        if not is_family_lead(conn, user, membership['family_id']):
            return jsonify({'error': 'Only the family lead can decide this'}), 403
        if membership['status'] not in ('requested', 'invited'):
            return jsonify({'error': 'This request was already handled'}), 400

        target_account = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (membership['player_id'],))
        family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (membership['family_id'],))
        if decision == 'approve':
            execute_modify(conn, "UPDATE player_family_memberships SET status = 'active' WHERE id = %s", (membership_id,))
            if target_account:
                notify_user(conn, target_account['id'], 'family_join_approved',
                    'Join request approved',
                    f"You are now a member of {family['name']}.")
            # If another roster member looks like the same person, nudge the lead.
            twin = execute_query_one(conn, '''
                SELECT p2.id, COALESCE(p2.display_name, p2.first_name) AS display_name
                FROM players p1
                JOIN players p2 ON p2.id <> p1.id
                  AND lower(p2.first_name) = lower(p1.first_name)
                  AND lower(p2.last_name) = lower(p1.last_name)
                  AND p2.archived_at IS NULL AND p2.purged_at IS NULL
                JOIN player_family_memberships m2 ON m2.player_id = p2.id
                  AND m2.family_id = %s AND m2.status = 'active'
                WHERE p1.id = %s
                  AND NOT EXISTS (
                    SELECT 1 FROM player_not_duplicates nd
                    WHERE nd.player_a_id = LEAST(p1.id, p2.id)
                      AND nd.player_b_id = GREATEST(p1.id, p2.id))
                LIMIT 1
            ''', (membership['family_id'], membership['player_id']))
            if twin:
                notify_family_lead(conn, membership['family_id'], 'possible_duplicate',
                    'Possible duplicate player',
                    f"{membership.get('display_name') or membership.get('first_name')} may be the same person as "
                    f"{twin['display_name']}. Open My Team to confirm or mark them as different.",
                    {'actions': [
                        {'label': 'Open My Team', 'style': 'primary', 'method': 'GET',
                         'url': '/my-team'},
                    ]})
            msg = 'Member added'
        else:
            execute_modify(conn, 'DELETE FROM player_family_memberships WHERE id = %s', (membership_id,))
            if target_account:
                notify_user(conn, target_account['id'], 'family_join_denied',
                    'Join request declined',
                    f"Your request to join {family['name']} was declined.")
            msg = 'Request declined'
        conn.commit()
        return jsonify({'message': msg})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/players/<int:player_id>/set-primary', methods=['POST'])
@login_required
def set_primary_family(player_id):
    """Set which family is a person's primary/home. Allowed for the person
    themselves or a lead of the target family."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        target_family_id = (request.json or {}).get('family_id')
        if not target_family_id:
            return jsonify({'error': 'family_id is required'}), 400
        is_self = user.get('player_id') == player_id
        if not is_self and not is_family_lead(conn, user, target_family_id):
            return jsonify({'error': 'Not allowed'}), 403
        membership = execute_query_one(conn, '''
            SELECT id FROM player_family_memberships
            WHERE player_id = %s AND family_id = %s AND status = 'active'
        ''', (player_id, target_family_id))
        if not membership:
            return jsonify({'error': 'That person is not an active member of this family'}), 400

        execute_modify(conn, 'UPDATE player_family_memberships SET is_primary = FALSE WHERE player_id = %s', (player_id,))
        execute_modify(conn, 'UPDATE player_family_memberships SET is_primary = TRUE WHERE id = %s', (membership['id'],))
        # Keep the person's account "home" pointer in step.
        execute_modify(conn, 'UPDATE users SET family_id = %s WHERE player_id = %s', (target_family_id, player_id))
        conn.commit()
        return jsonify({'message': 'Primary family updated'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Member directory: privacy-aware search so people can find each other without
# knowing email addresses. Hard rules:
#   - email is NEVER returned by any directory endpoint
#   - archived/purged people never appear
#   - minors appear only to their own family or a DIRECTLY allied crew family
#   - name search needs >= 2 chars, capped results, rate limited

import time as _time

_search_hits = {}

def _directory_rate_limited(user_id, limit=30, window=60):
    """In-process rate limiter. NOTE: becomes per-replica if the app scales to
    multiple web workers (Rocky compose web=2); move to Redis then."""
    now = _time.time()
    hits = [t for t in _search_hits.get(user_id, []) if now - t < window]
    if len(hits) >= limit:
        _search_hits[user_id] = hits
        return True
    hits.append(now)
    _search_hits[user_id] = hits
    return False

def _visible_family_ids_for_minors(conn, user):
    """Families whose minors this user may see: their own plus direct allies."""
    family_id = user.get('family_id')
    if not family_id:
        return []
    return [family_id] + allied_family_ids(conn, family_id)

def _family_label(row):
    """'DiFede (led by Joe D.)' - lead shown by name, never email.
    Emails are never shown on family labels (privacy). Lead name is how
    families with the same name are told apart."""
    name = row.get('family_name') or row.get('name') or 'Unknown family'
    lead = None
    if row.get('lead_first_name'):
        lead = public_person_name({
            'first_name': row.get('lead_first_name'),
            'last_name': row.get('lead_last_name'),
            'display_name': row.get('lead_display_name'),
            'show_full_last_name': True,
        })
    return f'{name} (led by {lead})' if lead else name


def _directory_person_name(player):
    """Full first + last for logged-in Directory search (easy find). Emails never shown."""
    first = (player.get('display_name') or player.get('first_name') or 'Player').strip()
    last = (player.get('last_name') or '').strip()
    if not last:
        return first
    # If display_name already looks like a full name, keep it; else append last.
    if player.get('display_name') and last.lower() in (player.get('display_name') or '').lower():
        return player['display_name']
    if player.get('display_name') and player.get('display_name') != player.get('first_name'):
        return f"{player['display_name']} ({player.get('first_name')} {last})"
    return f'{first} {last}'

_LEAD_JOIN = '''
    LEFT JOIN users lu ON lu.id = f.lead_user_id
    LEFT JOIN players lp ON lp.id = lu.player_id
'''
_LEAD_COLS = '''
    COALESCE(lp.first_name, lu.first_name) AS lead_first_name,
    COALESCE(lp.last_name, lu.last_name) AS lead_last_name,
    lp.display_name AS lead_display_name,
    COALESCE(lp.show_full_last_name, FALSE) AS lead_show_full_last_name
'''

@main.route('/api/directory/people')
@login_required
def directory_people():
    user = get_current_user()
    if _directory_rate_limited(user['id']):
        return jsonify({'error': 'Too many searches. Please wait a minute.'}), 429
    q = (request.args.get('q') or '').strip()
    # Empty q = browse publicly visible people. Short typed search still works.
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    conn = get_db_connection()
    try:
        my_family = user.get('family_id')
        allied = set(allied_family_ids(conn, my_family)) if my_family else set()
        trusted_families = list({fid for fid in ([my_family] if my_family else []) + list(allied) if fid})
        if not trusted_families:
            trusted_families = [-1]
        minor_families = _visible_family_ids_for_minors(conn, user) or [-1]
        params = []
        where = [
            'p.archived_at IS NULL',
            'p.purged_at IS NULL',
            'f.archived_at IS NULL',
            '(p.is_minor = FALSE OR p.family_id = ANY(%s))',
            # Public people OR anyone in your own/crew families.
            '(p.is_discoverable = TRUE OR p.family_id = ANY(%s))',
            # Their team must also be public unless you are crewed with them.
            '(f.is_discoverable = TRUE OR f.id = ANY(%s))',
        ]
        params.extend([minor_families, trusted_families, trusted_families])
        if q:
            where.append('''(
                p.first_name ILIKE %s OR p.last_name ILIKE %s
                OR COALESCE(p.display_name, '') ILIKE %s
                OR f.name ILIKE %s
            )''')
            like = f'%{q}%'
            params.extend([like, like, like, like])
        limit = 100 if not q else 40
        params.append((page - 1) * limit)
        rows = execute_query(conn, f'''
            SELECT p.id, p.first_name, p.last_name, p.display_name,
                p.show_full_last_name, p.is_minor, p.is_discoverable, p.family_id,
                f.name AS family_name,
                EXISTS (SELECT 1 FROM users u WHERE u.player_id = p.id) AS is_claimed,
                {_LEAD_COLS}
            FROM players p
            JOIN families f ON f.id = p.family_id
            {_LEAD_JOIN}
            WHERE {' AND '.join(where)}
            ORDER BY p.first_name, p.last_name
            LIMIT {int(limit)} OFFSET %s
        ''', tuple(params))

        my_pid = user.get('player_id')
        linked = set()
        if my_pid:
            for L in execute_query(conn, '''
                SELECT player_a_id, player_b_id FROM player_crew_links
                WHERE status IN ('pending', 'accepted')
                  AND (player_a_id = %s OR player_b_id = %s)
            ''', (my_pid, my_pid)):
                linked.add(L['player_a_id'])
                linked.add(L['player_b_id'])
        results = []
        for r in rows:
            can_pc = bool(my_pid and r['id'] != my_pid and r['id'] not in linked
                          and r['family_id'] != my_family and r['is_claimed'])
            results.append({
                'player_id': r['id'],
                'name': _directory_person_name(r),
                'family_id': r['family_id'],
                'family_name': r['family_name'],
                'family_label': _family_label(r),
                'is_claimed': bool(r['is_claimed']),
                'in_my_family': r['family_id'] == my_family,
                'is_allied': r['family_id'] in allied,
                'can_personal_crew': can_pc,
            })
        return jsonify({'page': page, 'results': results})
    finally:
        conn.close()

@main.route('/api/directory/lookup-email', methods=['POST'])
@login_required
def directory_lookup_email():
    """Exact-match email lookup only. No partials, no wildcards, at most one
    result, and the email itself is never echoed back."""
    user = get_current_user()
    if _directory_rate_limited(user['id']):
        return jsonify({'error': 'Too many searches. Please wait a minute.'}), 429
    email = ((request.json or {}).get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'found': False})

    conn = get_db_connection()
    try:
        row = execute_query_one(conn, f'''
            SELECT p.id, p.first_name, p.last_name, p.display_name,
                p.show_full_last_name, p.is_minor, p.is_discoverable, p.family_id,
                f.name AS family_name, f.archived_at AS family_archived,
                {_LEAD_COLS}
            FROM players p
            JOIN families f ON f.id = p.family_id
            {_LEAD_JOIN}
            WHERE p.archived_at IS NULL AND p.purged_at IS NULL
              AND f.archived_at IS NULL
              AND (lower(p.email) = %s
                   OR p.id = (SELECT player_id FROM users u WHERE lower(u.email) = %s))
            LIMIT 1
        ''', (email, email))

        if not row or row['family_archived']:
            return jsonify({'found': False})
        my_family = user.get('family_id')
        allied = set(allied_family_ids(conn, my_family)) if my_family else set()
        trusted = (row['family_id'] == my_family or row['family_id'] in allied
                   or user.get('role') == 'super_admin')
        if not trusted:
            if not row.get('is_discoverable'):
                return jsonify({'found': False})
            fam_pub = execute_query_one(conn, '''
                SELECT is_discoverable FROM families WHERE id = %s
            ''', (row['family_id'],))
            if not fam_pub or not fam_pub.get('is_discoverable'):
                return jsonify({'found': False})
        if row['is_minor'] and row['family_id'] not in _visible_family_ids_for_minors(conn, user):
            return jsonify({'found': False})
        return jsonify({
            'found': True,
            'player_id': row['id'],
            'name': _directory_person_name(row),
            'family_id': row['family_id'],
            'family_label': _family_label(row),
        })
    finally:
        conn.close()

@main.route('/api/directory/families')
@login_required
def directory_families():
    """List / search teams. Empty q browses all teams that are public to you
    (discoverable) plus your own team and accepted crew alliances."""
    user = get_current_user()
    if _directory_rate_limited(user['id']):
        return jsonify({'error': 'Too many searches. Please wait a minute.'}), 429
    q = (request.args.get('q') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    conn = get_db_connection()
    try:
        my_family = user.get('family_id')
        i_am_lead = bool(my_family and is_family_lead(conn, user, my_family))
        allied = set(allied_family_ids(conn, my_family)) if my_family else set()
        trusted_families = list({fid for fid in ([my_family] if my_family else []) + list(allied) if fid})
        if not trusted_families:
            trusted_families = [-1]

        params = [trusted_families]
        where = [
            'f.archived_at IS NULL',
            '(f.is_discoverable = TRUE OR f.id = ANY(%s))',
        ]
        if q:
            where.append('f.name ILIKE %s')
            params.append(f'%{q}%')
        limit = 200 if not q else 40
        params.append((page - 1) * limit)

        rows = execute_query(conn, f'''
            SELECT f.id, f.name, f.slug, f.show_roster, f.is_discoverable,
                (SELECT COUNT(*) FROM player_family_memberships m
                  JOIN players mp ON mp.id = m.player_id
                  WHERE m.family_id = f.id AND m.status = 'active'
                    AND mp.archived_at IS NULL) AS member_count,
                {_LEAD_COLS}
            FROM families f
            {_LEAD_JOIN}
            WHERE {' AND '.join(where)}
            ORDER BY f.name
            LIMIT {int(limit)} OFFSET %s
        ''', tuple(params))

        results = []
        for r in rows:
            lead_name = None
            if r.get('lead_first_name'):
                lead_name = public_person_name({
                    'first_name': r['lead_first_name'],
                    'last_name': r['lead_last_name'],
                    'display_name': r['lead_display_name'],
                    'show_full_last_name': r['lead_show_full_last_name'],
                })
            is_trusted = r['id'] == my_family or r['id'] in allied
            # Crew always sees roster; strangers only when the team opts in.
            roster_visible = bool(r['show_roster']) or is_trusted
            results.append({
                'family_id': r['id'],
                'name': r['name'],
                'slug': r['slug'],
                'label': _family_label(r),
                'lead_name': lead_name,
                'member_count': r['member_count'],
                'roster_visible': roster_visible,
                'is_discoverable': bool(r['is_discoverable']),
                'is_my_family': r['id'] == my_family,
                'is_allied': r['id'] in allied,
                'can_crew_up': i_am_lead and r['id'] != my_family and r['id'] not in allied,
            })
        return jsonify({'page': page, 'results': results, 'i_am_lead': i_am_lead})
    finally:
        conn.close()

@main.route('/api/directory/families/<int:family_id>/roster')
@login_required
def directory_family_roster(family_id):
    """Roster preview. Crew / own family always see members. Strangers only
    when show_roster is on, and then only publicly visible people."""
    user = get_current_user()
    if _directory_rate_limited(user['id']):
        return jsonify({'error': 'Too many searches. Please wait a minute.'}), 429
    conn = get_db_connection()
    try:
        family = execute_query_one(conn, '''
            SELECT id, name, show_roster, is_discoverable FROM families
            WHERE id = %s AND archived_at IS NULL
        ''', (family_id,))
        if not family:
            return jsonify({'error': 'Family not found'}), 404

        my_family = user.get('family_id')
        allied = set(allied_family_ids(conn, my_family)) if my_family else set()
        is_trusted = (family_id == my_family or family_id in allied
                      or is_family_lead(conn, user, family_id)
                      or user.get('role') == 'super_admin')

        member_count = execute_query_one(conn, '''
            SELECT COUNT(*) AS n FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
        ''', (family_id,))['n']

        # Non-crew cannot browse a private (non-discoverable) team at all.
        if not is_trusted and not family.get('is_discoverable'):
            return jsonify({'error': 'Family not found'}), 404

        if not family['show_roster'] and not is_trusted:
            return jsonify({'family_id': family_id, 'name': family['name'],
                            'roster_visible': False, 'member_count': member_count})

        can_see_minors = family_id in _visible_family_ids_for_minors(conn, user) \
            or user.get('role') == 'super_admin'
        rows = execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name, p.display_name,
                p.show_full_last_name, p.is_minor, p.is_discoverable,
                EXISTS (SELECT 1 FROM users u WHERE u.player_id = p.id) AS is_claimed
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            WHERE m.family_id = %s AND m.status = 'active'
              AND p.archived_at IS NULL
            ORDER BY p.first_name, p.last_name
        ''', (family_id,))

        members = []
        for r in rows:
            if not can_see_minors and r['is_minor']:
                continue
            # Strangers only see people marked publicly visible.
            if not is_trusted and not r.get('is_discoverable'):
                continue
            members.append({
                'player_id': r['id'],
                'name': _directory_person_name(r),
                'is_claimed': bool(r['is_claimed']),
                'is_minor': bool(r['is_minor']),
            })

        # A lead viewing another family's roster can select people to request
        # a transfer into their own family (W5). Minors can only be moved by
        # their own family lead, enforced server-side on submission.
        can_request_transfer = (user.get('family_id')
                                and user['family_id'] != family_id
                                and is_family_lead(conn, user, user['family_id']))

        return jsonify({'family_id': family_id, 'name': family['name'],
                        'roster_visible': True, 'member_count': member_count,
                        'members': members,
                        'can_request_transfer': bool(can_request_transfer)})
    finally:
        conn.close()

@main.route('/directory')
@login_required
def directory():
    return render_template('directory.html')

@main.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    conn = get_db_connection()
    user = get_current_user()
    try:
        my_player_id = user.get('player_id')
        my_family_id = user.get('family_id')
        lead_of = execute_query_one(conn, '''
            SELECT id, name, is_discoverable, show_roster FROM families
            WHERE lead_user_id = %s AND archived_at IS NULL
        ''', (user['id'],))

        if request.method == 'POST':
            form = request.form
            if my_player_id:
                old = execute_query_one(conn, '''
                    SELECT display_name, is_discoverable, show_full_last_name
                    FROM players WHERE id = %s
                ''', (my_player_id,))
                execute_modify(conn, '''
                    UPDATE players
                    SET display_name = %s, is_discoverable = %s, show_full_last_name = %s
                    WHERE id = %s
                ''', ((form.get('display_name') or '').strip() or None,
                      form.get('is_discoverable') == 'on',
                      form.get('show_full_last_name') == 'on',
                      my_player_id))
                audit(conn, user['id'], 'profile_privacy_updated', 'players', my_player_id,
                      old=dict(old) if old else None,
                      new={'display_name': form.get('display_name'),
                           'is_discoverable': form.get('is_discoverable') == 'on',
                           'show_full_last_name': form.get('show_full_last_name') == 'on'})
            if lead_of:
                execute_modify(conn, '''
                    UPDATE families SET is_discoverable = %s, show_roster = %s
                    WHERE id = %s
                ''', (form.get('family_discoverable') == 'on',
                      form.get('family_show_roster') == 'on',
                      lead_of['id']))
                audit(conn, user['id'], 'family_privacy_updated', 'families', lead_of['id'],
                      new={'is_discoverable': form.get('family_discoverable') == 'on',
                           'show_roster': form.get('family_show_roster') == 'on'})
            conn.commit()
            lead_of = execute_query_one(conn, '''
                SELECT id, name, is_discoverable, show_roster FROM families
                WHERE lead_user_id = %s AND archived_at IS NULL
            ''', (user['id'],))

        person = None
        if my_player_id:
            person = execute_query_one(conn, '''
                SELECT p.*, f.name AS family_name FROM players p
                LEFT JOIN families f ON f.id = p.family_id
                WHERE p.id = %s
            ''', (my_player_id,))

        saved = request.method == 'POST'
        return render_template('profile.html', person=person, lead_of=lead_of,
                               family_id=my_family_id, saved=saved)
    except Exception as e:
        conn.rollback()
        return render_template('profile.html', person=None, lead_of=None,
                               family_id=None, saved=False, error=str(e))
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Invitations (W4): bring people in by email without ever exposing addresses.

def _invite_rate_limited(conn, user_id, limit=20):
    row = execute_query_one(conn, '''
        SELECT COUNT(*) AS n FROM invitations
        WHERE invited_by_user_id = %s AND created_at > CURRENT_TIMESTAMP - INTERVAL '24 hours'
    ''', (user_id,))
    return row['n'] >= limit

@main.route('/api/invitations', methods=['GET', 'POST'])
@login_required
def invitations():
    from app.auth import create_action_token
    from app.email_utils import send_site_invite_email, send_claim_invite_email
    conn = get_db_connection()
    user = get_current_user()
    try:
        if request.method == 'GET':
            sent = execute_query(conn, '''
                SELECT i.id, i.email, i.invite_type, i.status, i.created_at, i.expires_at,
                       f.name AS family_name
                FROM invitations i
                LEFT JOIN families f ON f.id = i.family_id
                WHERE i.invited_by_user_id = %s
                ORDER BY i.created_at DESC LIMIT 50
            ''', (user['id'],))
            return jsonify([dict(r) for r in sent])

        data = request.json or {}
        email = (data.get('email') or '').strip().lower()
        invite_type = data.get('invite_type') or 'join_family'
        if not email or '@' not in email:
            return jsonify({'error': 'A valid email address is required'}), 400
        if invite_type not in ('join_family', 'join_site'):
            return jsonify({'error': 'Invalid invite type'}), 400
        if _invite_rate_limited(conn, user['id']):
            return jsonify({'error': 'Invite limit reached (20 per day). Try again tomorrow.'}), 429

        family_id = user.get('family_id')
        family = None
        if invite_type == 'join_family':
            if not family_id:
                return jsonify({'error': 'You are not in a family'}), 400
            if not is_family_lead(conn, user, family_id):
                return jsonify({'error': 'Only the family lead can invite people to the family'}), 403
            family = execute_query_one(conn, 'SELECT id, name FROM families WHERE id = %s', (family_id,))

        inviter_person = None
        if user.get('player_id'):
            inviter_person = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s', (user['player_id'],))
        inviter_name = public_person_name(inviter_person) if inviter_person else user['first_name']

        # Case 1: the email already belongs to an account. Never send a signup
        # link (that would leak registration status); invite them in-app.
        existing_user = execute_query_one(conn, '''
            SELECT id, first_name, player_id FROM users WHERE lower(email) = %s
        ''', (email,))
        if existing_user:
            if invite_type != 'join_family':
                return jsonify({'message': 'They already have an account here.'})
            if not existing_user.get('player_id'):
                return jsonify({'error': 'That person has an account but no player profile yet; ask them to claim one first'}), 400
            membership = execute_query_one(conn, '''
                SELECT id, status FROM player_family_memberships
                WHERE player_id = %s AND family_id = %s
            ''', (existing_user['player_id'], family_id))
            if membership and membership['status'] == 'active':
                return jsonify({'error': 'They are already a member of your family'}), 400
            if membership:
                execute_modify(conn, "UPDATE player_family_memberships SET status = 'invited' WHERE id = %s",
                               (membership['id'],))
                membership_id = membership['id']
            else:
                membership_id = execute_query_one(conn, '''
                    INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
                    VALUES (%s, %s, FALSE, 'invited', 'member') RETURNING id
                ''', (existing_user['player_id'], family_id))['id']
            notify_user(conn, existing_user['id'], 'family_invite',
                f"Invitation to join {family['name']}",
                f"{inviter_name} invited you to join the {family['name']} family.",
                {'actions': [
                    {'label': 'Accept', 'style': 'success', 'method': 'POST',
                     'url': f'/api/memberships/{membership_id}/respond?choice=accept'},
                    {'label': 'Decline', 'style': 'outline-secondary', 'method': 'POST',
                     'url': f'/api/memberships/{membership_id}/respond?choice=decline'},
                ]})
            audit(conn, user['id'], 'invite_sent_in_app', 'player_family_memberships', membership_id,
                  new={'family_id': family_id, 'player_id': existing_user['player_id']})
            conn.commit()
            return jsonify({'message': 'They already have an account - an invite was sent to them in the app.'})

        # Case 2: the email matches an unclaimed player profile: claim invite.
        unclaimed = execute_query_one(conn, '''
            SELECT p.id, p.first_name, p.last_name, p.display_name, p.family_id
            FROM players p
            WHERE lower(p.email) = %s AND p.purged_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM users u WHERE u.player_id = p.id)
        ''', (email,))
        if unclaimed:
            pf = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (unclaimed['family_id'],))
            token = create_action_token('claim_profile', player_id=unclaimed['id'], ttl_hours=168)
            execute_query_one(conn, '''
                INSERT INTO invitations (email, invited_by_user_id, family_id, player_id, invite_type, token, expires_at, status)
                VALUES (%s, %s, %s, %s, 'claim_profile', %s, CURRENT_TIMESTAMP + INTERVAL '7 days', 'sent')
                RETURNING id
            ''', (email, user['id'], unclaimed['family_id'], unclaimed['id'], token))
            send_claim_invite_email(email, unclaimed['first_name'],
                                    pf['name'] if pf else 'their', inviter_name,
                                    f"{APP_BASE_URL}/claim/{token}")
            audit(conn, user['id'], 'claim_invite_sent', 'players', unclaimed['id'])
            conn.commit()
            return jsonify({'message': 'That email belongs to an existing player profile - a claim invite was sent.'})

        # Case 3: unknown email: signup invite (optionally straight into a family).
        import secrets as _secrets
        token = _secrets.token_urlsafe(32)
        execute_query_one(conn, '''
            INSERT INTO invitations (email, invited_by_user_id, family_id, invite_type, token, expires_at, status)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP + INTERVAL '7 days', 'sent')
            RETURNING id
        ''', (email, user['id'],
              family_id if invite_type == 'join_family' else None,
              invite_type, token))
        sent = send_site_invite_email(email, inviter_name,
                                      family['name'] if family else None,
                                      f"{APP_BASE_URL}/invite/{token}")
        audit(conn, user['id'], 'invite_sent_email', 'invitations', None, new={'invite_type': invite_type})
        conn.commit()
        if sent:
            return jsonify({'message': f'Invite sent to {email}.'})
        return jsonify({'message': 'Invite created, but the email could not be sent. Use Resend to try again.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/invitations/<int:invite_id>/resend', methods=['POST'])
@login_required
def resend_invitation(invite_id):
    from app.email_utils import send_site_invite_email, send_claim_invite_email
    conn = get_db_connection()
    user = get_current_user()
    try:
        inv = execute_query_one(conn, '''
            SELECT i.*, f.name AS family_name FROM invitations i
            LEFT JOIN families f ON f.id = i.family_id
            WHERE i.id = %s AND i.invited_by_user_id = %s
        ''', (invite_id, user['id']))
        if not inv:
            return jsonify({'error': 'Invite not found'}), 404
        if inv['status'] != 'sent':
            return jsonify({'error': 'This invite is no longer active'}), 400
        if _invite_rate_limited(conn, user['id']):
            return jsonify({'error': 'Invite limit reached (20 per day). Try again tomorrow.'}), 429
        execute_modify(conn, '''
            UPDATE invitations SET expires_at = CURRENT_TIMESTAMP + INTERVAL '7 days' WHERE id = %s
        ''', (invite_id,))
        if inv['invite_type'] == 'claim_profile':
            person = execute_query_one(conn, 'SELECT first_name FROM players WHERE id = %s', (inv['player_id'],))
            ok = send_claim_invite_email(inv['email'], person['first_name'] if person else 'there',
                                         inv['family_name'] or 'their', user['first_name'],
                                         f"{APP_BASE_URL}/claim/{inv['token']}")
        else:
            ok = send_site_invite_email(inv['email'], user['first_name'], inv['family_name'],
                                        f"{APP_BASE_URL}/invite/{inv['token']}")
        conn.commit()
        return jsonify({'message': 'Invite resent.' if ok else 'Could not send the email. Check the address and try again.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/invitations/<int:invite_id>/revoke', methods=['POST'])
@login_required
def revoke_invitation(invite_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        updated = execute_query_one(conn, '''
            UPDATE invitations SET status = 'revoked'
            WHERE id = %s AND invited_by_user_id = %s AND status = 'sent'
            RETURNING id
        ''', (invite_id, user['id']))
        if not updated:
            return jsonify({'error': 'Invite not found or already handled'}), 404
        audit(conn, user['id'], 'invite_revoked', 'invitations', invite_id)
        conn.commit()
        return jsonify({'message': 'Invite revoked.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/memberships/<int:membership_id>/respond', methods=['POST'])
@login_required
def respond_membership(membership_id):
    """The INVITED PERSON accepts or declines a family invitation (contrast
    with /decide, where the family lead rules on a join request)."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        choice = request.args.get('choice') or (request.get_json(silent=True) or {}).get('choice')
        if choice not in ('accept', 'decline'):
            return jsonify({'error': 'Invalid choice'}), 400
        membership = execute_query_one(conn, '''
            SELECT m.*, f.name AS family_name FROM player_family_memberships m
            JOIN families f ON f.id = m.family_id
            WHERE m.id = %s
        ''', (membership_id,))
        if not membership or membership['player_id'] != user.get('player_id'):
            return jsonify({'error': 'Invitation not found'}), 404
        if membership['status'] != 'invited':
            return jsonify({'error': 'This invitation was already handled'}), 400

        if choice == 'accept':
            execute_modify(conn, "UPDATE player_family_memberships SET status = 'active' WHERE id = %s",
                           (membership_id,))
            notify_family_lead(conn, membership['family_id'], 'family_join_approved',
                'Invitation accepted',
                f"{user['first_name']} accepted the invitation to join {membership['family_name']}.")
            msg = f"You are now a member of {membership['family_name']}."
        else:
            execute_modify(conn, 'DELETE FROM player_family_memberships WHERE id = %s', (membership_id,))
            notify_family_lead(conn, membership['family_id'], 'family_join_denied',
                'Invitation declined',
                f"{user['first_name']} declined the invitation to join {membership['family_name']}.")
            msg = 'Invitation declined.'
        audit(conn, user['id'], f'family_invite_{choice}', 'player_family_memberships', membership_id)
        conn.commit()
        return jsonify({'message': msg})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Release / transfer requests (W5): a lead pulls their people out of another
# family with that family lead's approval. Game history NEVER moves; only the
# person's home family changes.

@main.route('/api/release-requests', methods=['GET', 'POST'])
@login_required
def release_requests():
    from app.auth import create_action_token
    from app.email_utils import send_release_request_email
    conn = get_db_connection()
    user = get_current_user()
    try:
        if request.method == 'GET':
            my_family = user.get('family_id')
            incoming = execute_query(conn, '''
                SELECT r.batch_id, r.status, MIN(r.created_at) AS created_at,
                    tf.name AS to_family_name,
                    json_agg(json_build_object(
                        'request_id', r.id, 'player_id', r.player_id,
                        'name', COALESCE(p.display_name, p.first_name) || ' ' || LEFT(p.last_name, 1) || '.'
                    ) ORDER BY p.first_name) AS members,
                    MAX(r.note) AS note
                FROM player_release_requests r
                JOIN players p ON p.id = r.player_id
                JOIN families tf ON tf.id = r.to_family_id
                WHERE r.from_family_id = %s AND r.status = 'pending'
                GROUP BY r.batch_id, r.status, tf.name
                ORDER BY MIN(r.created_at) DESC
            ''', (my_family,)) if my_family and is_family_lead(conn, user, my_family) else []
            outgoing = execute_query(conn, '''
                SELECT r.batch_id, r.status, MIN(r.created_at) AS created_at,
                    ff.name AS from_family_name,
                    json_agg(json_build_object(
                        'request_id', r.id, 'player_id', r.player_id,
                        'name', COALESCE(p.display_name, p.first_name) || ' ' || LEFT(p.last_name, 1) || '.'
                    ) ORDER BY p.first_name) AS members
                FROM player_release_requests r
                JOIN players p ON p.id = r.player_id
                JOIN families ff ON ff.id = r.from_family_id
                WHERE r.requested_by_user_id = %s AND r.status = 'pending'
                GROUP BY r.batch_id, r.status, ff.name
                ORDER BY MIN(r.created_at) DESC
            ''', (user['id'],))
            return jsonify({'incoming': [dict(r) for r in incoming],
                            'outgoing': [dict(r) for r in outgoing]})

        data = request.json or {}
        player_ids = [int(p) for p in (data.get('player_ids') or [])]
        to_family_id = data.get('to_family_id') or user.get('family_id')
        note = (data.get('note') or '').strip() or None
        if not player_ids:
            return jsonify({'error': 'Select at least one person to move'}), 400
        if not is_family_lead(conn, user, to_family_id):
            return jsonify({'error': 'Only the lead of the receiving family can request transfers'}), 403

        people = execute_query(conn, '''
            SELECT id, first_name, last_name, display_name, family_id, is_minor, show_full_last_name
            FROM players WHERE id = ANY(%s) AND archived_at IS NULL AND purged_at IS NULL
        ''', (player_ids,))
        if len(people) != len(set(player_ids)):
            return jsonify({'error': 'One or more selected people were not found'}), 400
        from_ids = {p['family_id'] for p in people}
        if len(from_ids) != 1:
            return jsonify({'error': 'All selected people must currently be in the same family'}), 400
        from_family_id = from_ids.pop()
        if from_family_id is None:
            return jsonify({'error': 'Selected people have no current family'}), 400
        if from_family_id == to_family_id:
            return jsonify({'error': 'They are already in that family'}), 400

        # Minors may only be requested by a directly allied family's lead (the
        # current family lead still has to approve the move). Strangers cannot
        # even reference them.
        if any(p['is_minor'] for p in people) and user.get('role') != 'super_admin':
            if from_family_id not in allied_family_ids(conn, to_family_id):
                return jsonify({'error': 'Your families need to be crewed up before you can request a transfer that includes children'}), 403

        pending = execute_query(conn, '''
            SELECT player_id FROM player_release_requests
            WHERE player_id = ANY(%s) AND status = 'pending'
        ''', (player_ids,))
        if pending:
            return jsonify({'error': 'A transfer request is already pending for one or more of these people'}), 409

        batch = execute_query_one(conn, 'SELECT gen_random_uuid() AS b')['b']
        for p in people:
            execute_query_one(conn, '''
                INSERT INTO player_release_requests
                    (batch_id, player_id, from_family_id, to_family_id, requested_by_user_id, note)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            ''', (batch, p['id'], from_family_id, to_family_id, user['id'], note))

        names = [public_person_name(p) for p in people]
        to_family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (to_family_id,))
        requester_person = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s', (user['player_id'],)) if user.get('player_id') else None
        requester_name = public_person_name(requester_person) if requester_person else user['first_name']

        summary = ', '.join(names[:3]) + (f' and {len(names) - 3} more' if len(names) > 3 else '')
        notify_family_lead(conn, from_family_id, 'release_request',
            'Transfer request',
            f"{requester_name} wants to move {summary} to the {to_family['name']} family. Their game history with your family stays with your family.",
            {'batch_id': str(batch), 'actions': [
                {'label': 'Approve All', 'style': 'success', 'method': 'POST',
                 'url': f'/api/release-requests/{batch}/decide?decision=approve'},
                {'label': 'Deny', 'style': 'outline-secondary', 'method': 'POST',
                 'url': f'/api/release-requests/{batch}/decide?decision=deny'},
            ]})

        lead = execute_query_one(conn, '''
            SELECT u.email, u.first_name FROM users u
            JOIN families f ON f.lead_user_id = u.id WHERE f.id = %s AND u.is_active = TRUE
        ''', (from_family_id,))
        if lead and lead.get('email'):
            token = create_action_token('approve_release', family_id=from_family_id,
                                        payload={'batch_id': str(batch)}, ttl_hours=168)
            if token:
                send_release_request_email(lead['email'], lead['first_name'], requester_name,
                                           to_family['name'], names,
                                           f"{APP_BASE_URL}/action/{token}")

        audit(conn, user['id'], 'release_request_created', 'player_release_requests', None,
              new={'batch_id': str(batch), 'player_ids': player_ids,
                   'from_family_id': from_family_id, 'to_family_id': to_family_id})
        conn.commit()
        return jsonify({'message': 'Transfer request sent to their family lead.', 'batch_id': str(batch)})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

def _decide_release_batch(conn, batch_id, decision, acting_user, only_player_ids=None):
    """Core approval/denial shared by the API endpoint and the email token
    route. Returns (payload, status_code). Caller commits."""
    from app.email_utils import send_release_decided_email
    rows = execute_query(conn, '''
        SELECT r.*, p.first_name, p.last_name, p.display_name, p.show_full_last_name
        FROM player_release_requests r
        JOIN players p ON p.id = r.player_id
        WHERE r.batch_id = %s AND r.status = 'pending'
    ''', (batch_id,))
    if not rows:
        return {'error': 'No pending transfer request found for this batch'}, 404
    from_family_id = rows[0]['from_family_id']
    to_family_id = rows[0]['to_family_id']
    if not is_family_lead(conn, acting_user, from_family_id):
        return {'error': 'Only the current family lead can decide this request'}, 403

    selected = rows
    if only_player_ids:
        wanted = {int(p) for p in only_player_ids}
        selected = [r for r in rows if r['player_id'] in wanted]
        if not selected:
            return {'error': 'None of the selected people are part of this request'}, 400

    new_status = 'approved' if decision == 'approve' else 'denied'
    moved_names = []
    for r in selected:
        execute_modify(conn, '''
            UPDATE player_release_requests
            SET status = %s, decided_by_user_id = %s, decided_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (new_status, acting_user['id'], r['id']))
        if decision == 'approve':
            set_player_home_family(conn, r['player_id'], to_family_id)
        moved_names.append(public_person_name(r))
        audit(conn, acting_user['id'], f'release_{new_status}', 'player_release_requests', r['id'],
              old={'from_family_id': from_family_id},
              new={'to_family_id': to_family_id, 'player_id': r['player_id']})
        moved_account = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (r['player_id'],))
        if moved_account and decision == 'approve':
            notify_user(conn, moved_account['id'], 'home_family_changed',
                'Your home family changed',
                'Your home family was updated after a transfer request was approved. Your lifetime stats came with you.')

    # A partial decision leaves the rest pending for a later decision.
    from_family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (from_family_id,))
    to_family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (to_family_id,))
    requester = execute_query_one(conn, 'SELECT id, email, first_name FROM users WHERE id = %s',
                                  (rows[0]['requested_by_user_id'],))
    if requester:
        notify_user(conn, requester['id'], 'release_decided',
            f"Transfer {new_status}",
            f"{from_family['name']} {new_status} the transfer of {', '.join(moved_names)}.")
        if requester.get('email'):
            send_release_decided_email(requester['email'], requester['first_name'], new_status,
                                       moved_names, from_family['name'], to_family['name'])
    return {'message': f"{new_status.capitalize()}: {', '.join(moved_names)}"}, 200

@main.route('/api/release-requests/<batch_id>/decide', methods=['POST'])
@login_required
def decide_release(batch_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        decision = request.args.get('decision') or (request.get_json(silent=True) or {}).get('decision')
        if decision not in ('approve', 'deny'):
            return jsonify({'error': 'Invalid decision'}), 400
        only = (request.get_json(silent=True) or {}).get('player_ids')
        payload, code = _decide_release_batch(conn, batch_id, decision, user, only)
        if code == 200:
            conn.commit()
        else:
            conn.rollback()
        return jsonify(payload), code
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/release-requests/<batch_id>/cancel', methods=['POST'])
@login_required
def cancel_release(batch_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        updated = execute_query(conn, '''
            UPDATE player_release_requests SET status = 'cancelled'
            WHERE batch_id = %s AND requested_by_user_id = %s AND status = 'pending'
            RETURNING id
        ''', (batch_id, user['id']))
        if not updated:
            return jsonify({'error': 'No pending request of yours found for this batch'}), 404
        audit(conn, user['id'], 'release_cancelled', 'player_release_requests', None,
              new={'batch_id': str(batch_id)})
        conn.commit()
        return jsonify({'message': 'Transfer request withdrawn.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# One-tap email approvals (W3): /action/<token> executes a request identified
# by a single-use token, after verifying the logged-in user is authorized.

@main.route('/action/<token>')
@login_required
def action_token(token):
    from app.auth import peek_action_token, consume_action_token
    conn = get_db_connection()
    user = get_current_user()
    try:
        row = peek_action_token(conn, token, ('approve_release', 'join_family', 'family_invite'))
        if not row:
            return render_template('action_result.html', success=False,
                title='Link expired or already used',
                message='This link is no longer valid. If the request is still open, you can handle it from My Team or your notifications.')

        if row['purpose'] == 'approve_release':
            batch_id = (row.get('payload') or {}).get('batch_id')
            payload, code = _decide_release_batch(conn, batch_id, 'approve', user)
            if code != 200:
                conn.rollback()
                return render_template('action_result.html', success=False,
                    title='Could not approve', message=payload.get('error', 'Something went wrong.'))
            consume_action_token(conn, token)
            conn.commit()
            return render_template('action_result.html', success=True,
                title='Transfer approved', message=payload.get('message', 'Done.'))

        membership_id = (row.get('payload') or {}).get('membership_id')
        membership = execute_query_one(conn, '''
            SELECT m.*, f.name AS family_name FROM player_family_memberships m
            JOIN families f ON f.id = m.family_id WHERE m.id = %s
        ''', (membership_id,))
        if not membership or membership['status'] not in ('requested', 'invited'):
            return render_template('action_result.html', success=False,
                title='Already handled', message='This request was already handled in the app.')

        if row['purpose'] == 'join_family':
            if not is_family_lead(conn, user, membership['family_id']):
                return render_template('action_result.html', success=False,
                    title='Not allowed', message='Only the family lead can approve this join request.')
        else:
            if membership['player_id'] != user.get('player_id'):
                return render_template('action_result.html', success=False,
                    title='Not allowed', message='This invitation was addressed to someone else.')

        execute_modify(conn, "UPDATE player_family_memberships SET status = 'active' WHERE id = %s",
                       (membership_id,))
        member_account = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s',
                                           (membership['player_id'],))
        if row['purpose'] == 'join_family' and member_account:
            notify_user(conn, member_account['id'], 'family_join_approved',
                'Join request approved', f"You are now a member of {membership['family_name']}.")
        audit(conn, user['id'], f"action_token_{row['purpose']}", 'player_family_memberships', membership_id)
        consume_action_token(conn, token)
        conn.commit()
        return render_template('action_result.html', success=True,
            title='Done', message=f"Membership in {membership['family_name']} is now active.")
    except Exception as e:
        conn.rollback()
        return render_template('action_result.html', success=False,
            title='Something went wrong', message=str(e))
    finally:
        conn.close()

@main.route('/api/families/<int:family_id>/transfer-lead', methods=['POST'])
@login_required
def transfer_lead(family_id):
    """Current lead (or super admin) hands leadership to another member."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        if not is_family_lead(conn, user, family_id):
            return jsonify({'error': 'Only the current lead can transfer leadership'}), 403
        new_lead_user_id = (request.json or {}).get('user_id')
        if not new_lead_user_id:
            return jsonify({'error': 'Choose a member to lead'}), 400
        new_lead = execute_query_one(conn, '''
            SELECT u.id, u.player_id FROM users u WHERE u.id = %s
        ''', (new_lead_user_id,))
        if not new_lead or not new_lead.get('player_id'):
            return jsonify({'error': 'That account has no player profile'}), 400
        member = execute_query_one(conn, '''
            SELECT 1 FROM player_family_memberships
            WHERE player_id = %s AND family_id = %s AND status = 'active'
        ''', (new_lead['player_id'], family_id))
        if not member:
            return jsonify({'error': 'The new lead must be an active member of this family'}), 400

        execute_modify(conn, 'UPDATE families SET lead_user_id = %s WHERE id = %s', (new_lead_user_id, family_id))
        execute_modify(conn, "UPDATE player_family_memberships SET role = 'member' WHERE family_id = %s", (family_id,))
        execute_modify(conn, "UPDATE player_family_memberships SET role = 'lead' WHERE family_id = %s AND player_id = %s", (family_id, new_lead['player_id']))
        family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (family_id,))
        notify_user(conn, new_lead_user_id, 'family_lead_transfer',
            'You are now a family lead',
            f"You have been made lead of {family['name']}.")
        conn.commit()
        return jsonify({'message': 'Leadership transferred'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/players/<int:player_id>/claim', methods=['POST'])
@login_required
def claim_player(player_id):
    """A logged-in user without a player identity requests to own an unclaimed
    roster spot. The family lead must approve."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        if user.get('player_id'):
            return jsonify({'error': 'Your account already owns a player profile'}), 400
        player = execute_query_one(conn, '''
            SELECT p.*, (SELECT COUNT(*) FROM users u WHERE u.player_id = p.id) AS claimed
            FROM players p WHERE p.id = %s
        ''', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found'}), 404
        if player['claimed']:
            return jsonify({'error': 'That profile is already owned by another account'}), 400
        # Must share a family with the profile.
        shared = execute_query_one(conn, '''
            SELECT m.family_id FROM player_family_memberships m
            WHERE m.player_id = %s AND m.family_id = %s AND m.status = 'active'
        ''', (player_id, user.get('family_id')))
        if not shared:
            return jsonify({'error': 'You can only claim a profile in your own family'}), 400

        notify_family_lead(conn, user.get('family_id'), 'player_claim_request',
            'Profile claim request',
            f"{user.get('first_name','A user')} ({user.get('email')}) wants to own the profile "
            f"\"{player.get('display_name') or player.get('first_name')}\".",
            {'player_id': player_id, 'user_id': user['id'], 'actions': [
                {'label': 'Approve', 'style': 'success', 'method': 'POST',
                 'url': f"/api/players/{player_id}/approve-claim?user_id={user['id']}"},
            ]})
        conn.commit()
        return jsonify({'message': 'Claim request sent to your family lead'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/players/<int:player_id>/approve-claim', methods=['POST'])
@login_required
def approve_claim(player_id):
    """Family lead links a user account to a player profile (identity)."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        claim_user_id = request.args.get('user_id') or (request.get_json(silent=True) or {}).get('user_id')
        if not claim_user_id:
            return jsonify({'error': 'user_id is required'}), 400
        claim_user_id = int(claim_user_id)
        player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found'}), 404
        if not is_family_lead(conn, user, player.get('family_id')):
            return jsonify({'error': 'Only the family lead can approve claims'}), 403
        already = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (player_id,))
        if already:
            return jsonify({'error': 'That profile is already owned'}), 400
        claimer = execute_query_one(conn, 'SELECT id, player_id FROM users WHERE id = %s', (claim_user_id,))
        if not claimer:
            return jsonify({'error': 'Account not found'}), 404
        if claimer.get('player_id'):
            return jsonify({'error': 'That account already owns a profile'}), 400

        execute_modify(conn, 'UPDATE users SET player_id = %s WHERE id = %s', (player_id, claim_user_id))
        execute_modify(conn, '''
            INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
            VALUES (%s, %s, TRUE, 'active', 'member')
            ON CONFLICT (player_id, family_id) DO UPDATE SET status = 'active'
        ''', (player_id, player.get('family_id')))
        notify_user(conn, claim_user_id, 'player_claim_approved',
            'Profile claim approved',
            'You now own your player profile and your stats follow you everywhere.')
        conn.commit()
        return jsonify({'message': 'Profile ownership granted'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/players/<int:player_id>/claim-invite', methods=['POST'])
@login_required
def send_claim_invite(player_id):
    """Family lead sets or updates an unclaimed member's email and sends them a
    claim invite. Clicking the emailed link proves ownership, so no further
    lead approval is needed."""
    from app.auth import create_action_token
    from app.email_utils import send_claim_invite_email
    conn = get_db_connection()
    user = get_current_user()
    try:
        player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s AND purged_at IS NULL', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found'}), 404
        email = ((request.get_json(silent=True) or {}).get('email') or player.get('email') or '').strip().lower()
        if not email or '@' not in email:
            return jsonify({'error': 'A valid email address is required'}), 400
        if not is_family_lead(conn, user, player.get('family_id')):
            return jsonify({'error': 'Only the family lead can send claim invites'}), 403
        if execute_query_one(conn, 'SELECT 1 FROM users WHERE player_id = %s', (player_id,)):
            return jsonify({'error': 'That profile is already owned by an account'}), 400
        taken = execute_query_one(conn, '''
            SELECT 1 FROM players WHERE lower(email) = %s AND id <> %s
        ''', (email, player_id))
        if taken or execute_query_one(conn, 'SELECT 1 FROM users WHERE lower(email) = %s', (email,)):
            return jsonify({'error': 'That email address already belongs to someone else'}), 409
        if _invite_rate_limited(conn, user['id']):
            return jsonify({'error': 'Invite limit reached (20 per day). Try again tomorrow.'}), 429

        execute_modify(conn, 'UPDATE players SET email = %s, email_verified = FALSE WHERE id = %s',
                       (email, player_id))
        token = create_action_token('claim_profile', player_id=player_id, ttl_hours=168)
        if not token:
            conn.rollback()
            return jsonify({'error': 'Could not create the invite. Try again.'}), 500
        execute_query_one(conn, '''
            INSERT INTO invitations (email, invited_by_user_id, family_id, player_id, invite_type, token, expires_at, status)
            VALUES (%s, %s, %s, %s, 'claim_profile', %s, CURRENT_TIMESTAMP + INTERVAL '7 days', 'sent')
            RETURNING id
        ''', (email, user['id'], player['family_id'], player_id, token))
        family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (player['family_id'],))
        inviter_person = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s', (user['player_id'],)) if user.get('player_id') else None
        inviter_name = public_person_name(inviter_person) if inviter_person else user['first_name']
        sent = send_claim_invite_email(email, player['first_name'],
                                       family['name'] if family else 'their', inviter_name,
                                       f"{APP_BASE_URL}/claim/{token}")
        audit(conn, user['id'], 'claim_invite_sent', 'players', player_id, new={'email_set': True})
        conn.commit()
        if sent:
            return jsonify({'message': f"Claim invite sent to {email}."})
        return jsonify({'message': 'Email saved, but the invite email could not be sent. Use Resend from your invites list.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/claim/<token>')
def claim_landing(token):
    """Emailed claim link. Logged out: hand off to registration with the token
    stashed in the session. Logged in: bind the profile to this account, as
    long as the account does not already own a person with history."""
    from app.auth import peek_action_token, consume_action_token
    conn = get_db_connection()
    try:
        row = peek_action_token(conn, token, 'claim_profile')
        if not row:
            return render_template('action_result.html', success=False,
                title='Link expired or already used',
                message='Ask your family lead to send a fresh claim invite.')
        player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s AND purged_at IS NULL',
                                   (row['player_id'],))
        if not player:
            return render_template('action_result.html', success=False,
                title='Profile not found', message='This player profile no longer exists.')
        if execute_query_one(conn, 'SELECT 1 FROM users WHERE player_id = %s', (player['id'],)):
            return render_template('action_result.html', success=False,
                title='Already claimed', message='This profile is already owned by an account.')

        user = get_current_user()
        if not user:
            session['claim_token'] = token
            return redirect(url_for('auth.register'))

        my_player_id = user.get('player_id')
        if my_player_id and my_player_id != player['id']:
            history = execute_query_one(conn, '''
                SELECT 1 FROM game_scores WHERE player_id = %s
                UNION ALL SELECT 1 FROM game_stats WHERE winner_id = %s
                UNION ALL SELECT 1 FROM active_game_players WHERE player_id = %s
                LIMIT 1
            ''', (my_player_id, my_player_id, my_player_id))
            if history:
                return render_template('action_result.html', success=False,
                    title='Your account already has a profile',
                    message='Your current player profile has recorded games, so it cannot be replaced. Ask your family lead to merge the two profiles instead.')
            # The auto-created empty profile is retired, never deleted.
            execute_modify(conn, '''
                UPDATE players SET archived_at = CURRENT_TIMESTAMP, archived_by_user_id = %s,
                    archive_reason = 'Replaced by claimed profile' WHERE id = %s
            ''', (user['id'], my_player_id))

        if player.get('archived_at'):
            execute_modify(conn, '''
                UPDATE players SET archived_at = NULL, archived_by_user_id = NULL, archive_reason = NULL
                WHERE id = %s
            ''', (player['id'],))
        execute_modify(conn, 'UPDATE users SET player_id = %s, family_id = %s WHERE id = %s',
                       (player['id'], player['family_id'], user['id']))
        execute_modify(conn, 'UPDATE players SET email_verified = TRUE WHERE id = %s', (player['id'],))
        execute_modify(conn, '''
            INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
            VALUES (%s, %s, TRUE, 'active', 'member')
            ON CONFLICT (player_id, family_id) DO UPDATE SET status = 'active'
        ''', (player['id'], player['family_id']))
        execute_modify(conn, "UPDATE invitations SET status = 'accepted', accepted_at = CURRENT_TIMESTAMP WHERE token = %s", (token,))
        consume_action_token(conn, token)
        audit(conn, user['id'], 'profile_claimed', 'players', player['id'])
        conn.commit()
        name = player.get('display_name') or player.get('first_name')
        return render_template('action_result.html', success=True,
            title='Profile claimed',
            message=f'You now own the profile "{name}" and all of its game history.')
    except Exception as e:
        conn.rollback()
        return render_template('action_result.html', success=False,
            title='Something went wrong', message=str(e))
    finally:
        conn.close()

@main.route('/invite/<token>')
def invite_landing(token):
    """Emailed signup invite. Stashes the token and sends the visitor to
    registration with their email prefilled."""
    conn = get_db_connection()
    try:
        inv = execute_query_one(conn, '''
            SELECT i.*, f.name AS family_name FROM invitations i
            LEFT JOIN families f ON f.id = i.family_id
            WHERE i.token = %s
        ''', (token,))
        if not inv or inv['status'] != 'sent':
            return render_template('action_result.html', success=False,
                title='Invite not available',
                message='This invite link is no longer valid. Ask for a new one.')
        if inv['expires_at'] < datetime.utcnow():
            return render_template('action_result.html', success=False,
                title='Invite expired',
                message='This invite expired. Ask the sender to resend it.')
        if inv['invite_type'] == 'claim_profile':
            return redirect(url_for('main.claim_landing', token=token))

        user = get_current_user()
        if user:
            return render_template('action_result.html', success=False,
                title='You already have an account',
                message='This invite was for creating a new account. To join a family, use the Directory to send a join request.')
        session['invite_token'] = token
        return redirect(url_for('auth.register'))
    finally:
        conn.close()

@main.route('/api/admin/archived')
@admin_required
def admin_archived():
    """Everything a super admin can reinstate: archived players and accounts."""
    conn = get_db_connection()
    try:
        players = execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) AS display_name,
                p.archived_at, p.archive_reason, f.name AS family_name
            FROM players p
            LEFT JOIN families f ON f.id = p.family_id
            WHERE p.archived_at IS NOT NULL AND p.purged_at IS NULL
            ORDER BY p.archived_at DESC
        ''')
        accounts = execute_query(conn, '''
            SELECT id, email, first_name, last_name, archived_at
            FROM users WHERE archived_at IS NOT NULL
            ORDER BY archived_at DESC
        ''')
        families = execute_query(conn, '''
            SELECT id, name, archived_at FROM families
            WHERE archived_at IS NOT NULL ORDER BY archived_at DESC
        ''')
        return jsonify({
            'players': [dict(p) for p in players],
            'accounts': [dict(a) for a in accounts],
            'families': [dict(f) for f in families],
        })
    finally:
        conn.close()

@main.route('/api/admin/move-player', methods=['POST'])
@admin_required
def move_player_family():
    conn = get_db_connection()
    try:
        data = request.json
        player_id = data['player_id']
        target_family_id = data['target_family_id']
        set_player_home_family(conn, player_id, target_family_id)
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

def _player_history_counts(conn, player_id):
    row = execute_query_one(conn, '''
        SELECT
            (SELECT COUNT(*) FROM game_scores WHERE player_id = %s) AS scores,
            (SELECT COUNT(*) FROM game_stats WHERE winner_id = %s) AS wins,
            (SELECT COUNT(DISTINCT active_game_id) FROM active_game_players WHERE player_id = %s) AS games,
            (SELECT COUNT(*) FROM player_family_memberships WHERE player_id = %s) AS families,
            (SELECT COUNT(*) FROM users WHERE player_id = %s) AS accounts
    ''', (player_id, player_id, player_id, player_id, player_id))
    return dict(row) if row else {}

def _can_merge(conn, user, keep_id, dup_id):
    """Super admin anywhere; a family lead may merge two players whose home
    family is the one they lead."""
    if user.get('role') == 'super_admin':
        return True
    rows = execute_query(conn, 'SELECT id, family_id FROM players WHERE id = ANY(%s)',
                         ([keep_id, dup_id],))
    if len(rows) != 2:
        return False
    fams = {r['family_id'] for r in rows}
    return len(fams) == 1 and is_family_lead(conn, user, fams.pop())


def _retire_login_account(conn, actor_user_id, user_id):
    """Hard-delete a login after clearing FK blockers. Player history stays."""
    if not user_id:
        return
    if actor_user_id and int(user_id) == int(actor_user_id):
        raise ValueError('Cannot retire your own login during a merge')
    execute_modify(conn, 'UPDATE families SET lead_user_id = NULL WHERE lead_user_id = %s', (user_id,))
    execute_modify(conn, 'UPDATE families SET archived_by_user_id = NULL WHERE archived_by_user_id = %s', (user_id,))
    execute_modify(conn, 'UPDATE players SET archived_by_user_id = NULL WHERE archived_by_user_id = %s', (user_id,))
    execute_modify(conn, 'UPDATE users SET archived_by_user_id = NULL WHERE archived_by_user_id = %s', (user_id,))
    execute_modify(conn, '''
        UPDATE player_release_requests SET decided_by_user_id = NULL WHERE decided_by_user_id = %s
    ''', (user_id,))
    execute_modify(conn, 'DELETE FROM user_sessions WHERE user_id = %s', (user_id,))
    execute_modify(conn, 'DELETE FROM users WHERE id = %s', (user_id,))


def _merge_players_core(conn, keep_id, dup_id, actor_user_id,
                        adopt_login_from='keep', prefer_email_from='keep'):
    """Repoint all history from dup -> keep, resolve dual logins, delete dup player.
    adopt_login_from / prefer_email_from: 'keep' or 'dup'. Caller commits and audits."""
    if adopt_login_from not in ('keep', 'dup'):
        adopt_login_from = 'keep'
    if prefer_email_from not in ('keep', 'dup'):
        prefer_email_from = 'keep'

    keep = execute_query_one(conn, 'SELECT id, family_id, email FROM players WHERE id = %s', (keep_id,))
    dup = execute_query_one(conn, '''
        SELECT id, first_name, last_name, family_id, email FROM players WHERE id = %s
    ''', (dup_id,))
    if not keep or not dup:
        raise ValueError('Player not found')

    keep_user = execute_query_one(conn, 'SELECT id, email FROM users WHERE player_id = %s', (keep_id,))
    dup_user = execute_query_one(conn, 'SELECT id, email FROM users WHERE player_id = %s', (dup_id,))

    for tbl in ('game_scores', 'five_crowns_scores'):
        execute_modify(conn, f'''
            DELETE FROM {tbl} a WHERE a.player_id = %s AND EXISTS (
                SELECT 1 FROM {tbl} b
                WHERE b.active_game_id = a.active_game_id
                  AND b.round_number = a.round_number AND b.player_id = %s)
        ''', (dup_id, keep_id))
        execute_modify(conn, f'UPDATE {tbl} SET player_id = %s WHERE player_id = %s', (keep_id, dup_id))

    execute_modify(conn, '''
        DELETE FROM active_game_players a WHERE a.player_id = %s AND EXISTS (
            SELECT 1 FROM active_game_players b
            WHERE b.active_game_id = a.active_game_id AND b.player_id = %s)
    ''', (dup_id, keep_id))
    execute_modify(conn, 'UPDATE active_game_players SET player_id = %s WHERE player_id = %s', (keep_id, dup_id))
    execute_modify(conn, 'UPDATE game_stats SET winner_id = %s WHERE winner_id = %s', (keep_id, dup_id))

    execute_modify(conn, '''
        INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
        SELECT %s, d.family_id, FALSE, d.status, 'member'
        FROM player_family_memberships d
        WHERE d.player_id = %s AND NOT EXISTS (
            SELECT 1 FROM player_family_memberships k
            WHERE k.player_id = %s AND k.family_id = d.family_id)
    ''', (keep_id, dup_id, keep_id))
    execute_modify(conn, 'DELETE FROM player_family_memberships WHERE player_id = %s', (dup_id,))
    has_primary = execute_query_one(conn, '''
        SELECT 1 FROM player_family_memberships WHERE player_id = %s AND is_primary
    ''', (keep_id,))
    if not has_primary:
        execute_modify(conn, '''
            UPDATE player_family_memberships SET is_primary = TRUE
            WHERE id = (SELECT id FROM player_family_memberships
                        WHERE player_id = %s ORDER BY joined_at ASC, id ASC LIMIT 1)
        ''', (keep_id,))

    # Personal crew links are ordered (player_a_id < player_b_id). Rebuild
    # each link involving the duplicate onto the kept player.
    crew_links = execute_query(conn, '''
        SELECT * FROM player_crew_links
        WHERE player_a_id = %s OR player_b_id = %s
           OR requested_by_player_id = %s OR responded_by_player_id = %s
    ''', (dup_id, dup_id, dup_id, dup_id))
    for link in crew_links or []:
        a = keep_id if link['player_a_id'] == dup_id else link['player_a_id']
        b = keep_id if link['player_b_id'] == dup_id else link['player_b_id']
        req = keep_id if link['requested_by_player_id'] == dup_id else link['requested_by_player_id']
        resp = link.get('responded_by_player_id')
        if resp == dup_id:
            resp = keep_id
        execute_modify(conn, 'DELETE FROM player_crew_links WHERE id = %s', (link['id'],))
        if a == b:
            continue
        lo, hi = (a, b) if a < b else (b, a)
        exists = execute_query_one(conn, '''
            SELECT id FROM player_crew_links WHERE player_a_id = %s AND player_b_id = %s
        ''', (lo, hi))
        if exists:
            continue
        execute_modify(conn, '''
            INSERT INTO player_crew_links
                (player_a_id, player_b_id, status, requested_by_player_id, responded_by_player_id)
            VALUES (%s, %s, %s, %s, %s)
        ''', (lo, hi, link.get('status') or 'accepted', req, resp))

    # Capture emails before freeing uniqueness on the discard row.
    # Prefer players.email; fall back to the linked login email.
    keep_email = keep.get('email') or (keep_user.get('email') if keep_user else None)
    dup_email = dup.get('email') or (dup_user.get('email') if dup_user else None)
    preferred_email = keep_email if prefer_email_from == 'keep' else dup_email
    if not preferred_email:
        preferred_email = dup_email if prefer_email_from == 'keep' else keep_email
    execute_modify(conn, 'UPDATE players SET email = NULL WHERE id = %s', (dup_id,))
    if preferred_email:
        execute_modify(conn, 'UPDATE players SET email = %s WHERE id = %s',
                       (preferred_email, keep_id))
    else:
        execute_modify(conn, 'UPDATE players SET email = NULL WHERE id = %s', (keep_id,))

    retired_login = None
    surviving_user_id = None
    if keep_user and dup_user:
        if adopt_login_from == 'dup':
            # Adopt discard login onto keep; retire keep's old login.
            execute_modify(conn, 'UPDATE users SET player_id = NULL WHERE id = %s', (keep_user['id'],))
            execute_modify(conn, '''
                UPDATE users SET player_id = %s, family_id = COALESCE(family_id, %s)
                WHERE id = %s
            ''', (keep_id, keep.get('family_id'), dup_user['id']))
            _retire_login_account(conn, actor_user_id, keep_user['id'])
            retired_login = keep_user.get('email')
            surviving_user_id = dup_user['id']
        else:
            execute_modify(conn, 'UPDATE users SET player_id = NULL WHERE id = %s', (dup_user['id'],))
            _retire_login_account(conn, actor_user_id, dup_user['id'])
            retired_login = dup_user.get('email')
            execute_modify(conn, '''
                UPDATE users SET family_id = COALESCE(family_id, %s) WHERE id = %s
            ''', (keep.get('family_id'), keep_user['id']))
            surviving_user_id = keep_user['id']
    elif dup_user and not keep_user:
        execute_modify(conn, '''
            UPDATE users SET player_id = %s, family_id = COALESCE(family_id, %s)
            WHERE id = %s
        ''', (keep_id, keep.get('family_id'), dup_user['id']))
        surviving_user_id = dup_user['id']
    elif keep_user:
        surviving_user_id = keep_user['id']
        execute_modify(conn, 'UPDATE users SET player_id = NULL WHERE player_id = %s', (dup_id,))
    else:
        execute_modify(conn, 'UPDATE users SET player_id = NULL WHERE player_id = %s', (dup_id,))

    if surviving_user_id and preferred_email:
        execute_modify(conn, 'UPDATE users SET email = %s WHERE id = %s',
                       (preferred_email, surviving_user_id))

    execute_modify(conn, 'UPDATE invitations SET player_id = %s WHERE player_id = %s', (keep_id, dup_id))
    execute_modify(conn, 'DELETE FROM players WHERE id = %s', (dup_id,))
    execute_modify(conn, '''
        DELETE FROM player_not_duplicates
        WHERE player_a_id IN (%s, %s) OR player_b_id IN (%s, %s)
    ''', (keep_id, dup_id, keep_id, dup_id))
    return {
        'kept_id': keep_id,
        'duplicate_id': dup_id,
        'duplicate_name': f"{dup.get('first_name', '')} {dup.get('last_name', '')}".strip(),
        'retired_login': retired_login,
        'adopt_login_from': adopt_login_from,
        'prefer_email_from': prefer_email_from,
        'email': preferred_email,
    }


@main.route('/api/admin/merge-preview')
@login_required
def merge_preview():
    conn = get_db_connection()
    user = get_current_user()
    try:
        keep_id = request.args.get('keep_id', type=int)
        dup_id = request.args.get('dup_id', type=int)
        if not keep_id or not dup_id or keep_id == dup_id:
            return jsonify({'error': 'Pick two different players'}), 400
        if not _can_merge(conn, user, keep_id, dup_id):
            return jsonify({'error': 'Only a super admin, or the lead of both players\' home family, can merge'}), 403
        def info(pid):
            p = execute_query_one(conn, '''
                SELECT p.id, COALESCE(p.display_name, p.first_name) AS display_name,
                    p.first_name, p.last_name, p.email AS player_email,
                    f.name AS family_name,
                    u.id AS user_id, u.email AS login_email
                FROM players p
                LEFT JOIN families f ON f.id = p.family_id
                LEFT JOIN users u ON u.player_id = p.id
                WHERE p.id = %s
            ''', (pid,))
            if not p:
                return None
            d = dict(p)
            d['history'] = _player_history_counts(conn, pid)
            d['email'] = d.get('login_email') or d.get('player_email')
            return d
        a, b = info(keep_id), info(dup_id)
        if not a or not b:
            return jsonify({'error': 'Player not found'}), 404
        allow_dual = user.get('role') == 'super_admin'
        try:
            rec_keep, rec_dup, why, _rows = _choose_keep_player(
                conn, keep_id, dup_id, allow_dual_accounts=allow_dual)
        except ValueError as e:
            return jsonify({'error': str(e), 'keep': a, 'dup': b}), 400
        # Default credential side: prefer whoever already has a login/email.
        if a.get('user_id') and not b.get('user_id'):
            rec_login_from = 'keep'
        elif b.get('user_id') and not a.get('user_id'):
            rec_login_from = 'dup'
        elif a.get('email') and not b.get('email'):
            rec_login_from = 'keep'
        elif b.get('email') and not a.get('email'):
            rec_login_from = 'dup'
        else:
            rec_login_from = 'keep' if rec_keep == keep_id else 'dup'
        return jsonify({
            'keep': a, 'dup': b,
            'recommended_keep_id': rec_keep,
            'recommended_dup_id': rec_dup,
            'recommended_reason': why,
            'recommended_adopt_login_from': rec_login_from,
            'recommended_prefer_email_from': rec_login_from,
        })
    finally:
        conn.close()

@main.route('/api/admin/merge-players', methods=['POST'])
@login_required
def merge_players():
    """Merge a duplicate person into a canonical one. All history (scores,
    wins, games, memberships) is repointed to the kept player, then the
    duplicate row is deleted. Dual logins: keep the kept player's login and
    retire the other. With auto_keep=true, keep the side with more games."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.json or {}
        keep_id = data.get('keep_id')
        dup_id = data.get('dup_id')
        if not keep_id or not dup_id:
            return jsonify({'error': 'keep_id and dup_id are required'}), 400
        keep_id, dup_id = int(keep_id), int(dup_id)
        if keep_id == dup_id:
            return jsonify({'error': 'Cannot merge a player into itself'}), 400
        if not _can_merge(conn, user, keep_id, dup_id):
            return jsonify({'error': 'Only a super admin, or the lead of both players\' home family, can merge'}), 403

        allow_dual = user.get('role') == 'super_admin'
        why = 'manual selection'
        adopt_login_from = (data.get('adopt_login_from') or 'keep').strip().lower()
        prefer_email_from = (data.get('prefer_email_from') or 'keep').strip().lower()
        if adopt_login_from not in ('keep', 'dup'):
            adopt_login_from = 'keep'
        if prefer_email_from not in ('keep', 'dup'):
            prefer_email_from = 'keep'

        if data.get('auto_keep'):
            keep_id, dup_id, why, _rows = _choose_keep_player(
                conn, keep_id, dup_id, allow_dual_accounts=allow_dual)
            # After auto flip, map adopt/prefer relative to the original labels
            # only when caller did not send explicit survivor rules.
            if not data.get('adopt_login_from') and not data.get('prefer_email_from'):
                adopt_login_from = 'keep'
                prefer_email_from = 'keep'
        else:
            # Still block non-admins from dual-login merges.
            _choose_keep_player(conn, keep_id, dup_id, allow_dual_accounts=allow_dual)

        # Non-admins cannot adopt the discard login over the keep login.
        if not allow_dual and adopt_login_from == 'dup':
            keep_u = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (keep_id,))
            dup_u = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (dup_id,))
            if keep_u and dup_u:
                return jsonify({'error': 'Only a super admin can choose which login survives a dual-login merge'}), 403

        result = _merge_players_core(
            conn, keep_id, dup_id, user['id'],
            adopt_login_from=adopt_login_from,
            prefer_email_from=prefer_email_from,
        )
        audit(conn, user['id'], 'players_merged', 'players', keep_id,
              old={'duplicate_id': result['duplicate_id'],
                   'duplicate_name': result['duplicate_name'],
                   'retired_login': result.get('retired_login'),
                   'reason': why,
                   'adopt_login_from': adopt_login_from,
                   'prefer_email_from': prefer_email_from},
              new={'kept_id': keep_id})
        conn.commit()
        msg = 'Players merged. All game history is on the kept profile.'
        if result.get('retired_login'):
            msg += f" Login {result['retired_login']} was removed."
        if result.get('email'):
            msg += f" Email on kept profile: {result['email']}."
        if data.get('auto_keep'):
            msg += f' Kept the profile with {why}.'
        return jsonify({'success': True, 'message': msg, 'kept_id': keep_id,
                        'retired_login': result.get('retired_login'),
                        'email': result.get('email')})
    except ValueError as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


def _choose_keep_player(conn, a_id, b_id, allow_dual_accounts=False):
    """Smart keep: prefer more games played; tie-break login, then older id.
    Returns (keep_id, discard_id, reason, rows). Raises ValueError if both have
    logins and allow_dual_accounts is False."""
    rows = {}
    for pid in (a_id, b_id):
        hist = _player_history_counts(conn, pid)
        acct = execute_query_one(conn, 'SELECT id, email FROM users WHERE player_id = %s', (pid,))
        p = execute_query_one(conn, '''
            SELECT id, first_name, last_name,
                COALESCE(display_name, first_name) AS display_name, email, family_id
            FROM players WHERE id = %s AND purged_at IS NULL
        ''', (pid,))
        if not p:
            raise ValueError('Player not found')
        rows[pid] = {'player': p, 'history': hist, 'account': acct,
                     'score': int(hist.get('games') or 0) * 1000 + int(hist.get('scores') or 0)}
    both_accounts = bool(rows[a_id]['account'] and rows[b_id]['account'])
    if both_accounts and not allow_dual_accounts:
        raise ValueError(
            'Both profiles have logins. Ask a super admin to merge these, or revoke one account first.')
    if rows[a_id]['score'] > rows[b_id]['score']:
        keep, discard, why = a_id, b_id, 'more games played'
    elif rows[b_id]['score'] > rows[a_id]['score']:
        keep, discard, why = b_id, a_id, 'more games played'
    elif rows[a_id]['account'] and not rows[b_id]['account']:
        keep, discard, why = a_id, b_id, 'has the login'
    elif rows[b_id]['account'] and not rows[a_id]['account']:
        keep, discard, why = b_id, a_id, 'has the login'
    else:
        keep, discard, why = min(a_id, b_id), max(a_id, b_id), 'older profile id'
    return keep, discard, why, rows


@main.route('/api/team/duplicate-suggestions')
@login_required
def team_duplicate_suggestions():
    """Possible same-person pairs in the lead's family."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        family_id = user.get('family_id')
        if not family_id or not is_family_lead(conn, user, family_id):
            return jsonify({'pairs': []})
        rows = execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) AS display_name,
                EXISTS (SELECT 1 FROM users u WHERE u.player_id = p.id) AS has_login,
                (SELECT COUNT(DISTINCT agp.active_game_id) FROM active_game_players agp
                  WHERE agp.player_id = p.id) AS games,
                (SELECT COUNT(*) FROM game_scores gs WHERE gs.player_id = p.id) AS scores
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            WHERE m.family_id = %s AND m.status = 'active'
              AND p.archived_at IS NULL AND p.purged_at IS NULL
            ORDER BY p.first_name, p.last_name, p.id
        ''', (family_id,))
        blocked = {(r['player_a_id'], r['player_b_id']) for r in execute_query(conn, '''
            SELECT player_a_id, player_b_id FROM player_not_duplicates
            WHERE player_a_id = ANY(%s) OR player_b_id = ANY(%s)
        ''', ([p['id'] for p in rows] or [0], [p['id'] for p in rows] or [0]))}
        pairs = []
        seen = set()
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                key = (min(a['id'], b['id']), max(a['id'], b['id']))
                if key in blocked or key in seen:
                    continue
                same_full = (a['first_name'] or '').lower() == (b['first_name'] or '').lower() \
                    and (a['last_name'] or '').lower() == (b['last_name'] or '').lower()
                same_initial = (a['first_name'] or '').lower() == (b['first_name'] or '').lower() \
                    and (a['last_name'] or '')[:1].lower() == (b['last_name'] or '')[:1].lower() \
                    and bool(a['last_name']) and bool(b['last_name'])
                one_login = bool(a['has_login']) != bool(b['has_login'])
                if same_full or (same_initial and one_login):
                    seen.add(key)
                    pairs.append({
                        'a': dict(a), 'b': dict(b),
                        'reason': 'Same first and last name' if same_full else 'Same first name and last initial; one has a login',
                    })
        return jsonify({'pairs': pairs})
    finally:
        conn.close()


@main.route('/api/team/same-person/preview')
@login_required
def same_person_preview():
    conn = get_db_connection()
    user = get_current_user()
    try:
        a = request.args.get('a', type=int)
        b = request.args.get('b', type=int)
        if not a or not b or a == b:
            return jsonify({'error': 'Pick two different players'}), 400
        if not _can_merge(conn, user, a, b):
            return jsonify({'error': 'Only the family lead of both home families can do this'}), 403
        try:
            keep, discard, why, rows = _choose_keep_player(conn, a, b)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        def pack(pid):
            r = rows[pid]
            return {
                'id': pid,
                'display_name': r['player']['display_name'],
                'first_name': r['player']['first_name'],
                'last_name': r['player']['last_name'],
                'has_login': bool(r['account']),
                'login_email': r['account']['email'] if r['account'] else None,
                'history': r['history'],
            }
        return jsonify({
            'keep': pack(keep), 'discard': pack(discard), 'reason': why,
            'message': (
                f"Keep {rows[keep]['player']['display_name']} ({why}). "
                f"Move any login onto that profile and remove the empty duplicate. "
                f"All game scores stay with the kept profile."
            ),
        })
    finally:
        conn.close()


@main.route('/api/team/same-person', methods=['POST'])
@login_required
def same_person_confirm():
    """Lead confirms two profiles are one person. Smart-keeps history."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        a = data.get('player_a_id') or data.get('a')
        b = data.get('player_b_id') or data.get('b')
        if not a or not b or int(a) == int(b):
            return jsonify({'error': 'Pick two different players'}), 400
        a, b = int(a), int(b)
        if not _can_merge(conn, user, a, b):
            return jsonify({'error': 'Only the family lead of both home families can do this'}), 403
        allow_dual = user.get('role') == 'super_admin'
        try:
            keep_id, dup_id, why, rows = _choose_keep_player(
                conn, a, b, allow_dual_accounts=allow_dual)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        result = _merge_players_core(conn, keep_id, dup_id, user['id'])
        audit(conn, user['id'], 'same_person_merged', 'players', keep_id,
              old={'discarded_id': dup_id, 'reason': why,
                   'retired_login': result.get('retired_login')},
              new={'kept_id': keep_id})
        conn.commit()
        kept_name = rows[keep_id]['player']['display_name']
        msg = f'Merged into {kept_name}. History preserved ({why}).'
        if result.get('retired_login'):
            msg += f" Login {result['retired_login']} was removed."
        return jsonify({
            'success': True,
            'kept_id': keep_id,
            'message': msg,
        })
    except ValueError as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/team/not-same-person', methods=['POST'])
@login_required
def not_same_person():
    """Lead says two similar profiles are different people. Requires distinct
    display names and remembers the pair so suggestions stop."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        a = data.get('player_a_id') or data.get('a')
        b = data.get('player_b_id') or data.get('b')
        name_a = (data.get('display_name_a') or '').strip()
        name_b = (data.get('display_name_b') or '').strip()
        if not a or not b or int(a) == int(b):
            return jsonify({'error': 'Pick two different players'}), 400
        a, b = int(a), int(b)
        if not _can_merge(conn, user, a, b):
            return jsonify({'error': 'Only the family lead of both home families can do this'}), 403
        if not name_a or not name_b:
            return jsonify({'error': 'Give each person a different display name so score sheets stay clear'}), 400
        if name_a.lower() == name_b.lower():
            return jsonify({'error': 'Display names must be different (for example Mike and Grandpa)'}), 400
        execute_modify(conn, 'UPDATE players SET display_name = %s WHERE id = %s', (name_a, a))
        execute_modify(conn, 'UPDATE players SET display_name = %s WHERE id = %s', (name_b, b))
        lo, hi = min(a, b), max(a, b)
        execute_modify(conn, '''
            INSERT INTO player_not_duplicates (player_a_id, player_b_id, decided_by_user_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (player_a_id, player_b_id) DO UPDATE
              SET decided_by_user_id = EXCLUDED.decided_by_user_id,
                  created_at = CURRENT_TIMESTAMP
        ''', (lo, hi, user['id']))
        audit(conn, user['id'], 'not_same_person', 'players', lo,
              new={'player_a': a, 'player_b': b, 'display_a': name_a, 'display_b': name_b})
        conn.commit()
        return jsonify({'message': 'Saved. They will stay separate and will not be suggested again.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/players/<int:player_id>/password-invite', methods=['POST'])
@login_required
def send_password_invite(player_id):
    """Lead adds/updates email on an existing player and emails a set-password
    link. Never creates a second players row."""
    from app.auth import create_action_token
    from app.email_utils import send_set_password_email
    conn = get_db_connection()
    user = get_current_user()
    try:
        player = execute_query_one(conn, '''
            SELECT * FROM players WHERE id = %s AND purged_at IS NULL AND archived_at IS NULL
        ''', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found'}), 404
        if not is_family_lead(conn, user, player.get('family_id')):
            return jsonify({'error': 'Only the family lead can send login invites'}), 403
        if execute_query_one(conn, 'SELECT 1 FROM users WHERE player_id = %s', (player_id,)):
            return jsonify({'error': 'That profile already has a login'}), 400
        email = ((request.get_json(silent=True) or {}).get('email') or player.get('email') or '').strip().lower()
        if not email or '@' not in email:
            return jsonify({'error': 'A valid email address is required'}), 400
        taken_user = execute_query_one(conn, 'SELECT id, player_id FROM users WHERE lower(email) = %s', (email,))
        if taken_user and taken_user.get('player_id') and taken_user['player_id'] != player_id:
            return jsonify({
                'error': 'That email already belongs to another account. Merge those people first.',
                'conflict_player_id': taken_user['player_id'],
                'suggest_merge': True,
            }), 409
        taken_player = execute_query_one(conn, '''
            SELECT id, COALESCE(display_name, first_name) AS display_name
            FROM players WHERE lower(email) = %s AND id <> %s AND purged_at IS NULL
        ''', (email, player_id))
        if taken_player:
            return jsonify({
                'error': f'That email is already on {taken_player["display_name"]}. Merge those people first.',
                'conflict_player_id': taken_player['id'],
                'suggest_merge': True,
            }), 409

        execute_modify(conn, 'UPDATE players SET email = %s, email_verified = FALSE WHERE id = %s',
                       (email, player_id))
        token = create_action_token('set_password', player_id=player_id,
                                   payload={'email': email}, ttl_hours=168)
        if not token:
            conn.rollback()
            return jsonify({'error': 'Could not create the invite. Try again.'}), 500
        execute_query_one(conn, '''
            INSERT INTO invitations (email, invited_by_user_id, family_id, player_id, invite_type, token, expires_at, status)
            VALUES (%s, %s, %s, %s, 'set_password', %s, CURRENT_TIMESTAMP + INTERVAL '7 days', 'sent')
            RETURNING id
        ''', (email, user['id'], player['family_id'], player_id, token))
        family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (player['family_id'],))
        inviter = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s', (user['player_id'],)) if user.get('player_id') else None
        inviter_name = public_person_name(inviter) if inviter else user['first_name']
        sent = send_set_password_email(
            email, player.get('display_name') or player['first_name'],
            family['name'] if family else 'their', inviter_name,
            f"{APP_BASE_URL}/auth/set-password/{token}")
        audit(conn, user['id'], 'password_invite_sent', 'players', player_id, new={'email': email})
        conn.commit()
        if sent:
            return jsonify({'message': f'Password setup invite sent to {email}.'})
        return jsonify({'message': 'Email saved, but the invite could not be sent. Try again shortly.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/admin/player/<int:player_id>/details', methods=['GET'])
@admin_required
def admin_player_details(player_id):
    conn = get_db_connection()
    try:
        player = execute_query_one(conn, '''
            SELECT p.*, COALESCE(p.display_name, p.first_name) as display_name,
                   f.name as family_name
            FROM players p
            LEFT JOIN families f ON p.family_id = f.id
            WHERE p.id = %s
        ''', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found'}), 404
        
        # users.player_id is the identity link (created_by_user_id is provenance).
        linked_user = execute_query_one(conn, '''
            SELECT id, email, first_name, last_name, role, is_active, is_verified,
                   is_approved, phone_number, address, city, state, zipcode, family_name
            FROM users WHERE player_id = %s
        ''', (player_id,))
        if linked_user:
            linked_user = dict(linked_user)
        
        families = list(execute_query(conn, 'SELECT id, name FROM families ORDER BY name'))
        
        return jsonify({
            'player': dict(player),
            'linked_user': linked_user,
            'families': [dict(f) for f in families]
        })
    finally:
        conn.close()

@main.route('/api/admin/player/<int:player_id>', methods=['PUT'])
@admin_required
def admin_update_player(player_id):
    conn = get_db_connection()
    try:
        data = request.json or {}
        player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found'}), 404

        display_name = data.get('display_name') or data.get('first_name', player['first_name'])
        # Always allow set/clear of players.email from admin people save.
        email_provided = 'email' in data or 'player_email' in data
        if email_provided:
            raw = data.get('email') if 'email' in data else data.get('player_email')
            player_email = (raw or '').strip().lower() or None
        else:
            player_email = player.get('email')

        if player_email:
            taken_p = execute_query_one(conn, '''
                SELECT id, COALESCE(display_name, first_name) AS display_name
                FROM players
                WHERE lower(email) = %s AND id <> %s
                  AND email IS NOT NULL AND email <> ''
            ''', (player_email, player_id))
            if taken_p:
                return jsonify({
                    'error': f'That email is already on another player ({taken_p["display_name"]}). Merge them or clear it there first.',
                    'conflict_player_id': taken_p['id'],
                }), 409

        execute_modify(conn, '''
            UPDATE players SET first_name = %s, last_name = %s, display_name = %s, email = %s
            WHERE id = %s
        ''', (data.get('first_name', player['first_name']),
              data.get('last_name', player['last_name']),
              display_name,
              player_email,
              player_id))

        new_family_id = data.get('family_id')
        if new_family_id and int(new_family_id) != player['family_id']:
            set_player_home_family(conn, player_id, int(new_family_id))

        linked_user = execute_query_one(conn, 'SELECT id, email FROM users WHERE player_id = %s', (player_id,))

        if data.get('create_account') and data.get('email'):
            from werkzeug.security import generate_password_hash
            temp_password = data.get('password', 'TempPass123!')
            pwd_hash = generate_password_hash(temp_password)
            new_user = execute_query_one(conn, '''
                INSERT INTO users (email, password_hash, first_name, last_name, family_name,
                    phone_number, address, city, state, zipcode, role, is_verified, is_active, is_approved, family_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, TRUE, TRUE, %s)
                RETURNING id
            ''', (data['email'], pwd_hash,
                  data.get('first_name', player['first_name']),
                  data.get('last_name', player['last_name']),
                  data.get('user_family_name', ''),
                  data.get('phone_number', ''),
                  data.get('address', ''),
                  data.get('city', ''),
                  data.get('state', ''),
                  data.get('zipcode', ''),
                  'family_admin',
                  data.get('family_id', player['family_id'])))
            execute_modify(conn, 'UPDATE users SET player_id = %s WHERE id = %s', (player_id, new_user['id']))
            execute_modify(conn, 'UPDATE players SET created_by_user_id = %s, email = %s WHERE id = %s',
                           (new_user['id'], data['email'], player_id))
            linked_user = {'id': new_user['id'], 'email': data['email']}

        elif data.get('update_user') or (linked_user and email_provided):
            if not linked_user:
                linked_user = execute_query_one(conn, 'SELECT id, email FROM users WHERE player_id = %s', (player_id,))
            user_data = dict(data.get('user_data') or {})
            # Sync login email when admin changed the profile email field.
            if email_provided and linked_user:
                if player_email is None:
                    return jsonify({
                        'error': 'This person has a login. Clear the email by using Remove login (frees email), '
                                 'or change the email to a different address.',
                    }), 400
                taken_u = execute_query_one(conn, '''
                    SELECT id FROM users WHERE lower(email) = %s AND id <> %s
                ''', (player_email, linked_user['id']))
                if taken_u:
                    return jsonify({'error': 'That email already belongs to another login account'}), 409
                user_data['email'] = player_email

            user_updates = []
            user_params = []
            for field in ['email', 'first_name', 'last_name', 'phone_number', 'address', 'city', 'state', 'zipcode', 'role']:
                if field in user_data:
                    if field == 'email':
                        new_e = (user_data.get('email') or '').strip().lower()
                        if not new_e:
                            return jsonify({
                                'error': 'Login email cannot be blank. Use Remove login to free the address.',
                            }), 400
                        user_updates.append('email = %s')
                        user_params.append(new_e)
                    else:
                        user_updates.append(f'{field} = %s')
                        user_params.append(user_data[field])

            if 'is_active' in user_data:
                user_updates.append('is_active = %s')
                user_params.append(user_data['is_active'])
            if 'is_approved' in user_data:
                user_updates.append('is_approved = %s')
                user_params.append(user_data['is_approved'])
            if 'is_verified' in user_data:
                user_updates.append('is_verified = %s')
                user_params.append(user_data['is_verified'])

            if user_updates and linked_user:
                user_params.append(linked_user['id'])
                execute_modify(conn, f"UPDATE users SET {', '.join(user_updates)} WHERE id = %s", user_params)
                # Keep players.email in sync when login email changes via user_data only.
                if 'email' in user_data and not email_provided:
                    execute_modify(conn, 'UPDATE players SET email = %s WHERE id = %s',
                                   (user_data['email'], player_id))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/admin/bulk-move-players', methods=['POST'])
@admin_required
def bulk_move_players():
    conn = get_db_connection()
    try:
        data = request.json
        player_ids = data.get('player_ids', [])
        target_family_id = data.get('target_family_id')
        for pid in player_ids:
            set_player_home_family(conn, pid, target_family_id)
        conn.commit()
        return jsonify({'success': True, 'moved': len(player_ids)})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/admin/players-by-family')
@admin_required
def players_by_family():
    conn = get_db_connection()
    try:
        players = execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                f.id as family_id, f.name as family_name
            FROM players p
            LEFT JOIN families f ON p.family_id = f.id
            ORDER BY f.name, p.first_name
        ''')
        return jsonify([dict(p) for p in players])
    finally:
        conn.close()

SLUG_TO_URL = {
    'five-crowns': '/five-crowns',
    'uno-classic': '/uno',
    'uno-flip': '/uno-flip',
    'dutch-blitz': '/dutch-blitz',
    'trouble': '/trouble',
    'basic-other': '/basic-other',
    'kings-corner': '/kings-corner',
    'gin-rummy': '/gin-rummy',
    'sevens': '/sevens',
    'skyjo': '/skyjo',
}

@main.route('/games')
@login_required
def games():
    conn = get_db_connection()
    try:
        available_games = execute_query(conn, '''
            SELECT g.*, 
                (SELECT COUNT(*) FROM active_games ag WHERE ag.game_id = g.id AND ag.is_complete = TRUE) as games_played
            FROM games g
            WHERE g.is_variant_group = FALSE
            ORDER BY COALESCE(g.display_order, g.id * 10), g.id
        ''')
        variant_groups = execute_query(conn, '''
            SELECT g.* FROM games g WHERE g.is_variant_group = TRUE ORDER BY g.name
        ''')
        return render_template('games.html', games=available_games, variant_groups=variant_groups, slug_to_url=SLUG_TO_URL)
    finally:
        conn.close()

@main.route('/family/<int:family_id>')
@login_required
def family_page(family_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        family = execute_query_one(conn, 'SELECT * FROM families WHERE id = %s', (family_id,))
        if not family or family.get('archived_at'):
            return redirect(url_for('main.dashboard'))

        my_family = user.get('family_id')
        allied = set(allied_family_ids(conn, my_family)) if my_family else set()
        is_trusted = (family_id == my_family or family_id in allied
                      or user.get('role') == 'super_admin')
        # Private teams are only visible to own family + crew (+ super admin).
        if not is_trusted and not family.get('is_discoverable'):
            return redirect(url_for('main.directory'))

        leader = execute_query_one(conn, '''
            SELECT u.id, u.email, u.first_name, u.last_name, u.role
            FROM users u
            JOIN families f ON f.lead_user_id = u.id
            WHERE f.id = %s
        ''', (family_id,))

        show_members = is_trusted or bool(family.get('show_roster'))
        if show_members:
            members = list(execute_query(conn, '''
                SELECT p.id, p.first_name, p.last_name,
                    COALESCE(p.display_name, p.first_name) as display_name,
                    m.is_primary AS is_home, p.is_discoverable, p.is_minor
                FROM player_family_memberships m
                JOIN players p ON p.id = m.player_id
                WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
                ORDER BY m.is_primary DESC, p.first_name, p.last_name
            ''', (family_id,)))
            if not is_trusted:
                can_see_minors = family_id in _visible_family_ids_for_minors(conn, user)
                members = [m for m in members
                           if m.get('is_discoverable')
                           and (can_see_minors or not m.get('is_minor'))]
        else:
            members = []
        
        stats = list(execute_query(conn, '''
            SELECT g.name as game_name, g.slug, COUNT(*) as games_played
            FROM active_games ag
            JOIN games g ON ag.game_id = g.id
            WHERE ag.family_id = %s AND ag.is_complete = TRUE
            GROUP BY g.name, g.slug ORDER BY games_played DESC
        ''', (family_id,)))
        
        # Honest stats, two scopes: what members did IN THIS FAMILY's game
        # nights, and their LIFETIME record across every family. A guest's
        # lifetime wins never inflate this family's own numbers unlabeled.
        family_top = list(execute_query(conn, '''
            SELECT p.id, COALESCE(p.display_name, p.first_name) as display_name,
                COUNT(DISTINCT gs.game_id) as wins,
                COUNT(DISTINCT ag.id) as total_games
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            LEFT JOIN active_game_players agp ON agp.player_id = p.id
            LEFT JOIN active_games ag ON agp.active_game_id = ag.id
                AND ag.is_complete = TRUE AND ag.family_id = %s
            LEFT JOIN game_stats gs ON gs.game_id = ag.id AND gs.winner_id = p.id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
            GROUP BY p.id, p.display_name
            HAVING COUNT(DISTINCT ag.id) > 0
            ORDER BY wins DESC, total_games DESC
            LIMIT 10
        ''', (family_id, family_id)))

        lifetime_top = list(execute_query(conn, '''
            SELECT p.id, COALESCE(p.display_name, p.first_name) as display_name,
                COUNT(DISTINCT gs.game_id) as wins,
                COUNT(DISTINCT ag.id) as total_games
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            LEFT JOIN active_game_players agp ON agp.player_id = p.id
            LEFT JOIN active_games ag ON agp.active_game_id = ag.id AND ag.is_complete = TRUE
            LEFT JOIN game_stats gs ON gs.game_id = ag.id AND gs.winner_id = p.id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
            GROUP BY p.id, p.display_name
            ORDER BY wins DESC, total_games DESC
            LIMIT 10
        ''', (family_id,)))
        
        total_games = execute_query_one(conn, '''
            SELECT COUNT(*) as cnt FROM active_games ag
            WHERE ag.family_id = %s AND ag.is_complete = TRUE
        ''', (family_id,))
        
        is_own_family = my_family == family_id
        is_allied = family_id in allied

        return render_template('family_page.html',
            family=family, leader=leader, members=members,
            stats=stats, family_top=family_top, lifetime_top=lifetime_top,
            total_games=total_games['cnt'] if total_games else 0,
            is_own_family=is_own_family, is_allied=is_allied,
            roster_visible=show_members)
    finally:
        conn.close()

@main.route('/player/<int:player_id>')
@login_required
def player_profile(player_id):
    """Public player page: lifetime record with an honest per-family breakdown.
    Respects privacy: strangers see first name + last initial, minors are only
    visible to their own family and directly allied crews."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        player = execute_query_one(conn, '''
            SELECT p.*, f.name AS family_name FROM players p
            LEFT JOIN families f ON f.id = p.family_id
            WHERE p.id = %s AND p.purged_at IS NULL
        ''', (player_id,))
        if not player:
            return redirect(url_for('main.dashboard'))

        is_super = user.get('role') == 'super_admin'
        is_self = user.get('player_id') == player_id
        my_family = user.get('family_id')
        trusted_families = [player['family_id']] + allied_family_ids(conn, player['family_id'])
        is_trusted = is_super or is_self or (my_family in trusted_families)

        if player.get('archived_at') and not (is_super or is_family_lead(conn, user, player['family_id'])):
            return redirect(url_for('main.dashboard'))
        if player.get('is_minor') and not is_trusted:
            return redirect(url_for('main.dashboard'))
        if not player.get('is_discoverable') and not is_trusted:
            return redirect(url_for('main.dashboard'))

        shown_name = (f"{player['first_name']} {player['last_name']}".strip()
                      if is_trusted else public_person_name(player))

        totals = execute_query_one(conn, '''
            SELECT COUNT(DISTINCT ag.id) AS games,
                   COUNT(DISTINCT gs.id) AS wins
            FROM active_game_players agp
            JOIN active_games ag ON ag.id = agp.active_game_id AND ag.is_complete = TRUE
            LEFT JOIN game_stats gs ON gs.game_id = ag.id AND gs.winner_id = agp.player_id
            WHERE agp.player_id = %s
        ''', (player_id,))

        by_family = list(execute_query(conn, '''
            SELECT COALESCE(f.name, 'No family') AS family_name, f.id AS family_id,
                   COUNT(DISTINCT ag.id) AS games,
                   COUNT(DISTINCT gs.id) AS wins
            FROM active_game_players agp
            JOIN active_games ag ON ag.id = agp.active_game_id AND ag.is_complete = TRUE
            LEFT JOIN families f ON f.id = ag.family_id
            LEFT JOIN game_stats gs ON gs.game_id = ag.id AND gs.winner_id = agp.player_id
            WHERE agp.player_id = %s
            GROUP BY f.id, f.name
            ORDER BY games DESC
        ''', (player_id,)))

        by_game = list(execute_query(conn, '''
            SELECT g.name AS game_name,
                   COUNT(DISTINCT ag.id) AS games,
                   COUNT(DISTINCT gs.id) AS wins
            FROM active_game_players agp
            JOIN active_games ag ON ag.id = agp.active_game_id AND ag.is_complete = TRUE
            JOIN games g ON g.id = ag.game_id
            LEFT JOIN game_stats gs ON gs.game_id = ag.id AND gs.winner_id = agp.player_id
            WHERE agp.player_id = %s
            GROUP BY g.name
            ORDER BY games DESC
        ''', (player_id,)))

        recent = list(execute_query(conn, '''
            SELECT ag.id AS active_game_id, g.name AS game_name, ag.completion_time,
                   COALESCE(f.name, 'No family') AS family_name,
                   (gs.winner_id = %s) AS won
            FROM active_game_players agp
            JOIN active_games ag ON ag.id = agp.active_game_id AND ag.is_complete = TRUE
            JOIN games g ON g.id = ag.game_id
            LEFT JOIN families f ON f.id = ag.family_id
            LEFT JOIN game_stats gs ON gs.game_id = ag.id
            WHERE agp.player_id = %s
            ORDER BY ag.completion_time DESC NULLS LAST
            LIMIT 10
        ''', (player_id, player_id)))

        memberships = list(execute_query(conn, '''
            SELECT f.id, f.name, m.is_primary FROM player_family_memberships m
            JOIN families f ON f.id = m.family_id
            WHERE m.player_id = %s AND m.status = 'active'
            ORDER BY m.is_primary DESC, f.name
        ''', (player_id,))) if is_trusted else []

        return render_template('player_profile.html',
            player=player, shown_name=shown_name, is_trusted=is_trusted,
            totals=totals, by_family=by_family, by_game=by_game,
            recent=recent, memberships=memberships)
    finally:
        conn.close()

@main.route('/game/<slug>')
@login_required
def game_landing(slug):
    conn = get_db_connection()
    user = get_current_user()
    try:
        game_def = execute_query_one(conn, "SELECT * FROM games WHERE slug = %s", (slug,))
        if not game_def:
            return redirect(url_for('main.games'))

        rules = execute_query(conn, "SELECT * FROM game_details WHERE game_id = %s ORDER BY id", (game_def['id'],))

        access_sql, access_params = play_family_clause(user, 'ag')
        active_games = execute_query(conn, f'''
            SELECT ag.id, ag.start_time, ag.is_paused,
                string_agg(COALESCE(p.display_name, p.first_name), ', ' ORDER BY agp.id) as player_names,
                COUNT(DISTINCT agp.player_id) as player_count
            FROM active_games ag
            JOIN active_game_players agp ON ag.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            WHERE ag.game_id = %s AND {access_sql} AND ag.is_complete = FALSE
            GROUP BY ag.id, ag.start_time, ag.is_paused
            ORDER BY ag.is_paused, ag.start_time DESC
        ''', tuple([game_def['id']] + list(access_params)))

        total_games = execute_query_one(conn, "SELECT COUNT(*) as c FROM active_games WHERE game_id = %s AND is_complete = TRUE", (game_def['id'],))
        family_games = execute_query_one(conn, "SELECT COUNT(*) as c FROM active_games WHERE game_id = %s AND family_id = %s AND is_complete = TRUE", (game_def['id'], user.get('family_id')))

        game_url = SLUG_TO_URL.get(slug, '/' + slug)

        family_id = user.get('family_id')
        lead = is_family_lead(conn, user, family_id) if family_id else False
        return render_template('game_landing.html',
            game=game_def,
            rules=rules,
            active_games=active_games,
            total_games=total_games['c'],
            family_games=family_games['c'],
            game_url=game_url,
            slug=slug,
            is_lead=lead)
    finally:
        conn.close()

@main.route('/five-crowns')
@login_required
def five_crowns():
    return game_page('five-crowns', 1)

@main.route('/uno')
@login_required
def uno():
    return game_page('uno-classic', 3)

@main.route('/uno-flip')
@login_required
def uno_flip():
    return game_page('uno-flip', 4)

@main.route('/dutch-blitz')
@login_required
def dutch_blitz():
    return game_page('dutch-blitz', 5)

@main.route('/trouble')
@login_required
def trouble():
    return game_page('trouble', 6)

@main.route('/basic-other')
@login_required
def basic_other():
    return game_page('basic-other', 7)

@main.route('/kings-corner')
@login_required
def kings_corner():
    return game_page('kings-corner', 8)

@main.route('/gin-rummy')
@login_required
def gin_rummy():
    return game_page('gin-rummy', 9)

@main.route('/sevens')
@login_required
def sevens():
    return game_page('sevens', 10)

@main.route('/skyjo')
@login_required
def skyjo():
    return game_page('skyjo', 11)


def _parse_layout_player_ids(raw):
    """Normalize JSON/list player ids into a list of ints."""
    if raw is None:
        return []
    if isinstance(raw, str):
        import json as _json
        try:
            raw = _json.loads(raw)
        except Exception:
            return []
    out = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _validate_layout_players(conn, family_id, player_ids, game):
    if not player_ids:
        return 'Select at least one player'
    if len(player_ids) != len(set(player_ids)):
        return 'Each player can only appear once'
    mn = game.get('min_players') or 1
    mx = game.get('max_players') or 20
    if len(player_ids) < mn or len(player_ids) > mx:
        return f'This game needs {mn} to {mx} players'
    allowed = {p['id'] for p in get_family_players(family_id, include_crew=True)}
    missing = [pid for pid in player_ids if pid not in allowed]
    if missing:
        return 'One or more players are not available to your family'
    return None


def _layout_row_to_dict(row):
    return {
        'id': row['id'],
        'family_id': row['family_id'],
        'game_id': row['game_id'],
        'name': row['name'],
        'player_ids': _parse_layout_player_ids(row.get('player_ids')),
        'scoring_direction': row.get('scoring_direction'),
        'target_score': row.get('target_score'),
        'is_default': bool(row.get('is_default')),
    }


@main.route('/api/games/<int:game_id>/layouts', methods=['GET', 'POST'])
@login_required
def game_layouts(game_id):
    """List or create saved layouts for a game type within the user's family."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        family_id = user.get('family_id')
        if not family_id:
            return jsonify({'error': 'You need a family to use layouts'}), 400
        game = execute_query_one(conn, 'SELECT * FROM games WHERE id = %s', (game_id,))
        if not game:
            return jsonify({'error': 'Game not found'}), 404

        if request.method == 'GET':
            rows = execute_query(conn, '''
                SELECT * FROM game_layouts
                WHERE family_id = %s AND game_id = %s
                ORDER BY is_default DESC, name ASC
            ''', (family_id, game_id))
            return jsonify({
                'layouts': [_layout_row_to_dict(r) for r in rows],
                'is_lead': is_family_lead(conn, user, family_id),
            })

        if not is_family_lead(conn, user, family_id):
            return jsonify({'error': 'Only the family lead can save layouts'}), 403
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name or len(name) > 80:
            return jsonify({'error': 'Layout name is required (max 80 characters)'}), 400
        player_ids = _parse_layout_player_ids(data.get('player_ids'))
        err = _validate_layout_players(conn, family_id, player_ids, game)
        if err:
            return jsonify({'error': err}), 400
        scoring_direction = data.get('scoring_direction') or None
        if scoring_direction and scoring_direction not in ('high_wins', 'low_wins'):
            return jsonify({'error': 'Invalid scoring direction'}), 400
        target_score = data.get('target_score')
        if target_score is not None and target_score != '':
            try:
                target_score = int(target_score)
            except (TypeError, ValueError):
                return jsonify({'error': 'Target score must be a number'}), 400
            if target_score < 1:
                return jsonify({'error': 'Target score must be at least 1'}), 400
        else:
            target_score = None
        make_default = bool(data.get('is_default'))
        import json as _json
        if make_default:
            execute_modify(conn, '''
                UPDATE game_layouts SET is_default = FALSE
                WHERE family_id = %s AND game_id = %s AND is_default = TRUE
            ''', (family_id, game_id))
        row = execute_query_one(conn, '''
            INSERT INTO game_layouts
                (family_id, game_id, name, player_ids, scoring_direction, target_score,
                 is_default, created_by_user_id)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            RETURNING *
        ''', (family_id, game_id, name, _json.dumps(player_ids),
              scoring_direction, target_score, make_default, user['id']))
        conn.commit()
        return jsonify({'message': 'Layout saved', 'layout': _layout_row_to_dict(row)})
    except Exception as e:
        conn.rollback()
        if 'game_layouts_name_unique' in str(e):
            return jsonify({'error': 'A layout with that name already exists'}), 409
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/layouts/<int:layout_id>', methods=['PUT', 'DELETE'])
@login_required
def game_layout_detail(layout_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        layout = execute_query_one(conn, 'SELECT * FROM game_layouts WHERE id = %s', (layout_id,))
        if not layout:
            return jsonify({'error': 'Layout not found'}), 404
        if not is_family_lead(conn, user, layout['family_id']):
            return jsonify({'error': 'Only the family lead can change layouts'}), 403

        if request.method == 'DELETE':
            execute_modify(conn, 'DELETE FROM game_layouts WHERE id = %s', (layout_id,))
            conn.commit()
            return jsonify({'message': 'Layout deleted'})

        data = request.get_json(silent=True) or {}
        game = execute_query_one(conn, 'SELECT * FROM games WHERE id = %s', (layout['game_id'],))
        name = (data.get('name') if 'name' in data else layout['name']) or ''
        name = name.strip()
        if not name or len(name) > 80:
            return jsonify({'error': 'Layout name is required (max 80 characters)'}), 400
        player_ids = _parse_layout_player_ids(
            data['player_ids'] if 'player_ids' in data else layout.get('player_ids'))
        err = _validate_layout_players(conn, layout['family_id'], player_ids, game)
        if err:
            return jsonify({'error': err}), 400
        scoring_direction = data['scoring_direction'] if 'scoring_direction' in data \
            else layout.get('scoring_direction')
        if scoring_direction and scoring_direction not in ('high_wins', 'low_wins'):
            return jsonify({'error': 'Invalid scoring direction'}), 400
        target_score = data['target_score'] if 'target_score' in data else layout.get('target_score')
        if target_score is not None and target_score != '':
            try:
                target_score = int(target_score)
            except (TypeError, ValueError):
                return jsonify({'error': 'Target score must be a number'}), 400
        else:
            target_score = None
        make_default = bool(data['is_default']) if 'is_default' in data else bool(layout['is_default'])
        import json as _json
        if make_default:
            execute_modify(conn, '''
                UPDATE game_layouts SET is_default = FALSE
                WHERE family_id = %s AND game_id = %s AND is_default = TRUE AND id <> %s
            ''', (layout['family_id'], layout['game_id'], layout_id))
        row = execute_query_one(conn, '''
            UPDATE game_layouts
            SET name = %s, player_ids = %s::jsonb, scoring_direction = %s,
                target_score = %s, is_default = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING *
        ''', (name, _json.dumps(player_ids), scoring_direction, target_score,
              make_default, layout_id))
        conn.commit()
        return jsonify({'message': 'Layout updated', 'layout': _layout_row_to_dict(row)})
    except Exception as e:
        conn.rollback()
        if 'game_layouts_name_unique' in str(e):
            return jsonify({'error': 'A layout with that name already exists'}), 409
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/layouts/<int:layout_id>/set-default', methods=['POST'])
@login_required
def game_layout_set_default(layout_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        layout = execute_query_one(conn, 'SELECT * FROM game_layouts WHERE id = %s', (layout_id,))
        if not layout:
            return jsonify({'error': 'Layout not found'}), 404
        if not is_family_lead(conn, user, layout['family_id']):
            return jsonify({'error': 'Only the family lead can set the default layout'}), 403
        execute_modify(conn, '''
            UPDATE game_layouts SET is_default = FALSE
            WHERE family_id = %s AND game_id = %s AND is_default = TRUE
        ''', (layout['family_id'], layout['game_id']))
        execute_modify(conn, '''
            UPDATE game_layouts SET is_default = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (layout_id,))
        conn.commit()
        return jsonify({'message': 'Default layout updated'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/players', methods=['GET', 'POST'])
@login_required
def players():
    conn = get_db_connection()
    user = get_current_user()
    family_id = user.get('family_id')
    
    if request.method == 'POST':
        data = request.json
        display_name = data.get('display_name') or data['first_name']
        
        player = execute_query_one(conn, '''
            INSERT INTO players (first_name, last_name, display_name, created_by_user_id, family_id, is_minor)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (data['first_name'], data['last_name'], display_name, user['id'], family_id,
              bool(data.get('is_minor', False))))
        
        if family_id:
            execute_modify(conn, '''
                INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
                VALUES (%s, %s, TRUE, 'active', 'member')
                ON CONFLICT (player_id, family_id) DO NOTHING
            ''', (player['id'], family_id))
        
        player = execute_query_one(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                FALSE as has_duplicate, FALSE as is_guest, NULL as guest_family_name
            FROM players p WHERE p.id = %s
        ''', (player['id'],))
        
        conn.commit()
        conn.close()
        return jsonify(dict(player))
    
    # Crew (allied-family) players are included by default so every game's
    # player picker can select them; pass include_crew=false to opt out.
    include_crew = request.args.get('include_crew', 'true') != 'false'
    conn.close()
    result = [dict(p) for p in get_family_players(
        family_id, include_crew=include_crew, for_player_id=user.get('player_id'))]
    return jsonify(result)

@main.route('/api/games/<int:game_id>/live-scores', methods=['GET'])
@login_required
def live_game_scores(game_id):
    """Current scores for an active game — used by live sync poll backup so
    every open score sheet stays current even if a websocket drops."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        access_sql, access_params = play_family_clause(user, 'ag')
        game = execute_query_one(conn, f'''
            SELECT ag.id, ag.is_complete FROM active_games ag
            WHERE ag.id = %s AND {access_sql}
        ''', tuple([game_id] + list(access_params)))
        if not game:
            return jsonify({'error': 'Game not found or access denied'}), 404
        rows = execute_query(conn, '''
            SELECT player_id, round_number, score
            FROM game_scores
            WHERE active_game_id = %s
            ORDER BY round_number, player_id
        ''', (game_id,))
        return jsonify({
            'game_id': game_id,
            'is_complete': bool(game['is_complete']),
            'scores': [{
                'player_id': r['player_id'],
                'round_number': r['round_number'],
                'score': r['score'],
            } for r in rows],
        })
    finally:
        conn.close()


@main.route('/api/scores', methods=['POST'])
@login_required
def update_score():
    conn = get_db_connection()
    try:
        data = request.json
        game_id = data['game_id']
        player_id = data['player_id']
        round_number = data['round_number']
        score = data['score']
        user = get_current_user()

        # Single transaction so the FOR UPDATE row locks actually hold until commit.
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            access_sql, access_params = play_family_clause(user, 'ag')
            cursor.execute(f'''
                SELECT ag.id, ag.is_complete FROM active_games ag
                WHERE ag.id = %s AND {access_sql}
                FOR UPDATE OF ag
            ''', tuple([game_id] + list(access_params)))
            game = cursor.fetchone()

            if not game:
                conn.rollback()
                return jsonify({'error': 'Game not found or access denied'}), 404

            if game['is_complete']:
                conn.rollback()
                return jsonify({'success': True, 'message': 'Game already completed, ignoring score update'})

            cursor.execute('''
                SELECT 1 FROM active_game_players
                WHERE active_game_id = %s AND player_id = %s
            ''', (game_id, player_id))
            if not cursor.fetchone():
                conn.rollback()
                return jsonify({'error': 'Player is not part of this game'}), 400

            if score is None:
                cursor.execute('''
                    DELETE FROM game_scores
                    WHERE active_game_id = %s AND player_id = %s AND round_number = %s
                ''', (game_id, player_id, round_number))
            else:
                cursor.execute('''
                    INSERT INTO game_scores (active_game_id, player_id, round_number, score)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (active_game_id, player_id, round_number)
                    DO UPDATE SET score = EXCLUDED.score
                ''', (game_id, player_id, round_number, score))

            conn.commit()
        finally:
            cursor.close()

        broadcast_score_update(game_id, player_id, round_number, score if score is not None else "")
        
        return jsonify({'success': True})
        
    except Exception as e:
        conn.rollback()
        print(f"Error updating score: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/five-crowns/new', methods=['POST'])
@login_required
def new_five_crowns_game():
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.json
        player_ids = data.get('player_ids', [])
        
        if not player_ids:
            return jsonify({'error': 'No players selected'}), 400
        
        player_ids_int = [int(pid) for pid in player_ids]
        family_id = user.get('family_id')
        allied_ids = [r['ally_id'] for r in execute_query(conn, '''
            SELECT CASE WHEN requesting_family_id = %s THEN target_family_id
                        ELSE requesting_family_id END as ally_id
            FROM family_alliances
            WHERE status = 'accepted' AND (requesting_family_id = %s OR target_family_id = %s)
        ''', (family_id, family_id, family_id))]
        allowed_families = [family_id] + allied_ids
        
        player_check = execute_query(conn, '''
            SELECT id FROM players
            WHERE id = ANY(%s) AND family_id = ANY(%s)
        ''', (player_ids_int, allowed_families))
        
        if len(player_check) != len(player_ids):
            return jsonify({'error': 'Invalid player selection'}), 400
        
        game = execute_query_one(conn, '''
            INSERT INTO active_games (game_id, user_id, is_complete, is_paused, scoring_direction, family_id)
            VALUES (1, %s, FALSE, FALSE, 'low_wins', %s)
            RETURNING id, start_time
        ''', (user['id'], family_id))
        
        for player_id in player_ids_int:
            execute_modify(conn, '''
                INSERT INTO active_game_players (active_game_id, player_id, family_id)
                VALUES (%s, %s, (SELECT family_id FROM players WHERE id = %s))
            ''', (game['id'], player_id, player_id))
        
        conn.commit()
        return jsonify({
            'id': game['id'],
            'start_time': game['start_time']
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/new', methods=['POST'])
@login_required
def new_game():
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.json
        game_id = data.get('game_id')
        player_ids = data.get('player_ids', [])
        scoring_direction = data.get('scoring_direction')
        target_score = data.get('target_score')
        
        if not player_ids or not game_id:
            return jsonify({'error': 'Game type and players required'}), 400
        
        game_def = execute_query_one(conn, 'SELECT * FROM games WHERE id = %s', (game_id,))
        if not game_def:
            return jsonify({'error': 'Invalid game type'}), 400
        
        player_ids_int = [int(pid) for pid in player_ids]
        family_id = user.get('family_id')
        allowed_families = [family_id] + allied_family_ids(conn, family_id)

        # Validate against memberships (same source as the roster), so a guest
        # whose home is elsewhere but who belongs to an allowed family counts.
        player_check = execute_query(conn, '''
            SELECT DISTINCT p.id FROM players p
            JOIN player_family_memberships m ON m.player_id = p.id
            WHERE p.id = ANY(%s) AND m.family_id = ANY(%s)
              AND m.status = 'active' AND p.archived_at IS NULL
        ''', (player_ids_int, allowed_families))

        if len(player_check) != len(set(player_ids_int)):
            return jsonify({'error': 'Invalid player selection'}), 400
        
        direction = scoring_direction or game_def['scoring_direction']
        target = target_score or game_def.get('default_target_score')
        custom_game_name = data.get('custom_game_name')
        
        game = execute_query_one(conn, '''
            INSERT INTO active_games (game_id, user_id, is_complete, is_paused, scoring_direction, target_score, family_id, custom_game_name)
            VALUES (%s, %s, FALSE, FALSE, %s, %s, %s, %s)
            RETURNING id, start_time
        ''', (game_id, user['id'], direction, target, user.get('family_id'), custom_game_name))
        
        for pid in player_ids_int:
            execute_modify(conn, '''
                INSERT INTO active_game_players (active_game_id, player_id, family_id)
                VALUES (%s, %s, (SELECT family_id FROM players WHERE id = %s))
            ''', (game['id'], pid, pid))
        
        conn.commit()
        return jsonify({'id': game['id'], 'start_time': game['start_time']})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/players/<int:player_id>', methods=['PUT'])
@login_required
def update_player(player_id):
    conn = get_db_connection()
    user = get_current_user()
    data = request.json
    
    try:
        target = execute_query_one(conn, '''
            SELECT id, family_id, created_by_user_id FROM players WHERE id = %s
        ''', (player_id,))

        # Editable by the lead of the player's home family, the row's creator,
        # or the person themselves (their account's identity link).
        allowed = target is not None and (
            is_family_lead(conn, user, target.get('family_id'))
            or target.get('created_by_user_id') == user['id']
            or user.get('player_id') == player_id
        )
        if not allowed:
            return jsonify({'error': 'Player not found or access denied'}), 403
        
        display_name = data.get('display_name') or data['first_name']
        execute_modify(conn, '''
            UPDATE players 
            SET first_name = %s, last_name = %s, display_name = %s
            WHERE id = %s
        ''', (data['first_name'], data['last_name'], display_name, player_id))
        
        player = execute_query_one(conn, '''
            WITH DisplayNameCounts AS (
                SELECT COALESCE(display_name, first_name) as display_name, 
                       COUNT(*) as name_count
                FROM players
                GROUP BY COALESCE(display_name, first_name)
                HAVING COUNT(*) > 1
            )
            SELECT 
                p.id,
                p.first_name,
                p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                dc.name_count IS NOT NULL as has_duplicate
            FROM players p
            LEFT JOIN DisplayNameCounts dc ON COALESCE(p.display_name, p.first_name) = dc.display_name
            WHERE p.id = %s
        ''', (player_id,))
        
        return jsonify({
            'success': True,
            'player': {
                'id': player['id'],
                'first_name': player['first_name'],
                'last_name': player['last_name'],
                'display_name': player['display_name'],
                'has_duplicate': bool(player['has_duplicate'])
            }
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

def _archive_player_guarded(conn, player_id, acting_user, reason=None):
    """Shared archive logic. Returns (error_message, http_code) or (None, 200).
    Never deletes anything; history stays intact and a super admin can
    reinstate. Caller commits."""
    player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s AND purged_at IS NULL', (player_id,))
    if not player:
        return 'Player not found', 404
    if player.get('archived_at'):
        return 'Player is already archived', 400
    if not is_family_lead(conn, acting_user, player.get('family_id')):
        return 'Only the family lead can archive this player', 403

    active_game = execute_query_one(conn, '''
        SELECT 1 FROM active_game_players agp
        JOIN active_games ag ON agp.active_game_id = ag.id
        WHERE agp.player_id = %s AND ag.is_complete = FALSE
    ''', (player_id,))
    if active_game:
        return 'Cannot archive a player who is in an active game. Finish or delete the game first.', 400

    owner = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (player_id,))
    if owner and owner['id'] != acting_user['id'] and acting_user.get('role') != 'super_admin':
        return 'This person owns their profile with a login. They can leave the family themselves, or another family lead can request a transfer.', 400

    execute_modify(conn, '''
        UPDATE players SET archived_at = CURRENT_TIMESTAMP, archived_by_user_id = %s,
            archive_reason = %s WHERE id = %s
    ''', (acting_user['id'], reason, player_id))
    audit(conn, acting_user['id'], 'player_archived', 'players', player_id,
          new={'reason': reason})
    return None, 200

@main.route('/api/players/<int:player_id>', methods=['DELETE'])
@main.route('/api/players/<int:player_id>/archive', methods=['POST'])
@login_required
def archive_player(player_id):
    """Archive (soft delete). The person disappears from rosters and pickers,
    but every score they ever recorded stays, and a super admin can reinstate
    them exactly as they were."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        reason = (request.get_json(silent=True) or {}).get('reason')
        err, code = _archive_player_guarded(conn, player_id, user, reason)
        if err:
            conn.rollback()
            return jsonify({'success': False, 'error': err}), code
        conn.commit()
        return jsonify({'success': True, 'message': 'Player archived. Their game history is preserved and a super admin can reinstate them.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/players/<int:player_id>/reinstate', methods=['POST'])
@admin_required
def reinstate_player(player_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        player = execute_query_one(conn, '''
            SELECT * FROM players WHERE id = %s AND archived_at IS NOT NULL AND purged_at IS NULL
        ''', (player_id,))
        if not player:
            return jsonify({'error': 'No archived player with that id'}), 404
        execute_modify(conn, '''
            UPDATE players SET archived_at = NULL, archived_by_user_id = NULL, archive_reason = NULL
            WHERE id = %s
        ''', (player_id,))
        # Make sure they surface somewhere: an active membership with one primary.
        if player.get('family_id'):
            execute_modify(conn, '''
                INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
                VALUES (%s, %s, TRUE, 'active', 'member')
                ON CONFLICT (player_id, family_id) DO UPDATE SET status = 'active'
            ''', (player_id, player['family_id']))
        has_primary = execute_query_one(conn, '''
            SELECT 1 FROM player_family_memberships WHERE player_id = %s AND is_primary
        ''', (player_id,))
        if not has_primary:
            execute_modify(conn, '''
                UPDATE player_family_memberships SET is_primary = TRUE
                WHERE id = (SELECT id FROM player_family_memberships
                            WHERE player_id = %s AND status = 'active'
                            ORDER BY joined_at ASC, id ASC LIMIT 1)
            ''', (player_id,))
        audit(conn, user['id'], 'player_reinstated', 'players', player_id)
        conn.commit()
        return jsonify({'message': 'Player reinstated with all of their history.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/players/<int:player_id>/purge', methods=['POST'])
@admin_required
def purge_player(player_id):
    """Irreversibly anonymize a person (GDPR-style). Scores keep their shape
    for everyone else's records, but the identity is gone forever.
    By default requires archive first; pass archive_first=true to do both."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s AND purged_at IS NULL', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found or already purged'}), 404
        confirm = data.get('confirm')
        if confirm != 'PURGE':
            return jsonify({'error': 'Send {"confirm": "PURGE"} to confirm this irreversible action'}), 400

        if not player.get('archived_at'):
            if data.get('archive_first'):
                # Skip active-game check only if explicitly forcing; still block
                # unfinished games for safety.
                active_game = execute_query_one(conn, '''
                    SELECT 1 FROM active_game_players agp
                    JOIN active_games ag ON agp.active_game_id = ag.id
                    WHERE agp.player_id = %s AND ag.is_complete = FALSE
                ''', (player_id,))
                if active_game:
                    return jsonify({
                        'error': 'Cannot permanently delete a player in an active game. Finish or delete the game first.',
                    }), 400
                execute_modify(conn, '''
                    UPDATE players SET archived_at = CURRENT_TIMESTAMP, archived_by_user_id = %s,
                        archive_reason = %s WHERE id = %s
                ''', (user['id'], 'Permanently deleted from People Hub', player_id))
            else:
                return jsonify({'error': 'Archive the player first; purge is the second, irreversible step'}), 400

        linked = execute_query_one(conn, 'SELECT id, email FROM users WHERE player_id = %s', (player_id,))
        emails = set()
        if player.get('email'):
            emails.add(player['email'].strip().lower())
        if linked and linked.get('email'):
            emails.add(linked['email'].strip().lower())

        if linked:
            if linked['id'] == user['id']:
                return jsonify({'error': 'You cannot permanently delete your own account'}), 400
            execute_modify(conn, 'UPDATE users SET player_id = NULL WHERE id = %s', (linked['id'],))
            _retire_login_account(conn, user['id'], linked['id'])
        else:
            execute_modify(conn, 'UPDATE users SET player_id = NULL WHERE player_id = %s', (player_id,))

        old_name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
        execute_modify(conn, '''
            UPDATE players SET first_name = 'Deleted', last_name = 'Player',
                display_name = %s, email = NULL, email_verified = FALSE,
                is_discoverable = FALSE, purged_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (f'Deleted ({old_name})' if old_name else 'Deleted Player', player_id))
        for e in emails:
            execute_modify(conn, '''
                UPDATE invitations SET status = 'revoked'
                WHERE lower(email) = %s AND status IN ('sent', 'accepted')
            ''', (e,))
        execute_modify(conn, '''
            UPDATE invitations SET status = 'revoked'
            WHERE player_id = %s AND status IN ('sent', 'accepted')
        ''', (player_id,))
        audit(conn, user['id'], 'player_purged', 'players', player_id,
              old={'first_name': player['first_name'], 'last_name': player['last_name'],
                   'email': player.get('email'), 'emails_cleared': sorted(emails)})
        conn.commit()
        return jsonify({
            'success': True,
            'message': 'Person permanently removed (shown as Deleted). '
                       'Game score rows remain for other players\' history. Email is free.',
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/families/<int:family_id>/archive', methods=['POST'])
@admin_required
def archive_family(family_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        family = execute_query_one(conn, 'SELECT * FROM families WHERE id = %s', (family_id,))
        if not family:
            return jsonify({'error': 'Family not found'}), 404
        if family.get('archived_at'):
            return jsonify({'error': 'Family is already archived'}), 400
        open_games = execute_query_one(conn, '''
            SELECT COUNT(*) AS n FROM active_games WHERE family_id = %s AND is_complete = FALSE
        ''', (family_id,))
        if open_games['n'] > 0:
            return jsonify({'error': f"This family has {open_games['n']} unfinished game(s). Finish or delete them first."}), 400
        execute_modify(conn, '''
            UPDATE families SET archived_at = CURRENT_TIMESTAMP, archived_by_user_id = %s WHERE id = %s
        ''', (user['id'], family_id))
        audit(conn, user['id'], 'family_archived', 'families', family_id)
        conn.commit()
        return jsonify({'message': 'Family archived. Its games and stats are preserved.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/families/<int:family_id>/reinstate', methods=['POST'])
@admin_required
def reinstate_family(family_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        updated = execute_query_one(conn, '''
            UPDATE families SET archived_at = NULL, archived_by_user_id = NULL
            WHERE id = %s AND archived_at IS NOT NULL RETURNING id
        ''', (family_id,))
        if not updated:
            return jsonify({'error': 'No archived family with that id'}), 404
        audit(conn, user['id'], 'family_reinstated', 'families', family_id)
        conn.commit()
        return jsonify({'message': 'Family reinstated.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/users/<int:user_id>/archive', methods=['POST'])
@admin_required
def archive_user(user_id):
    """Disable a login without touching the person or their history. The
    players row stays active; only the account is locked out."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        if user_id == user['id']:
            return jsonify({'error': 'You cannot archive your own account'}), 400
        target = execute_query_one(conn, 'SELECT id, archived_at FROM users WHERE id = %s', (user_id,))
        if not target:
            return jsonify({'error': 'Account not found'}), 404
        if target.get('archived_at'):
            return jsonify({'error': 'Account is already archived'}), 400
        execute_modify(conn, '''
            UPDATE users SET archived_at = CURRENT_TIMESTAMP, archived_by_user_id = %s,
                is_active = FALSE WHERE id = %s
        ''', (user['id'], user_id))
        execute_modify(conn, 'DELETE FROM user_sessions WHERE user_id = %s', (user_id,))
        audit(conn, user['id'], 'account_archived', 'users', user_id)
        conn.commit()
        return jsonify({'message': 'Account archived and signed out everywhere. The person and their stats are untouched.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/users/<int:user_id>/reinstate', methods=['POST'])
@admin_required
def reinstate_user(user_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        updated = execute_query_one(conn, '''
            UPDATE users SET archived_at = NULL, archived_by_user_id = NULL, is_active = TRUE
            WHERE id = %s AND archived_at IS NOT NULL RETURNING id
        ''', (user_id,))
        if not updated:
            return jsonify({'error': 'No archived account with that id'}), 404
        audit(conn, user['id'], 'account_reinstated', 'users', user_id)
        conn.commit()
        return jsonify({'message': 'Account reinstated. They can sign in again with the same credentials.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/five-crowns/complete', methods=['POST'])
@login_required
def complete_five_crowns_game():
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        specific_id = data.get('game_id')
        if specific_id:
            game = fetch_accessible_game(
                conn, specific_id, user,
                extra_where='ag.game_id = 1 AND ag.is_complete = FALSE')
        else:
            access_sql, access_params = play_family_clause(user, 'ag')
            game = execute_query_one(conn, f'''
                SELECT ag.id FROM active_games ag
                WHERE ag.game_id = 1 AND {access_sql}
                AND ag.is_complete = FALSE AND ag.is_paused = FALSE
                ORDER BY ag.start_time DESC LIMIT 1
            ''', tuple(access_params))
        
        if game:
            scores = execute_query(conn, '''
                WITH DisplayNameCounts AS (
                    SELECT COALESCE(display_name, first_name) as display_name, 
                           COUNT(*) as name_count
                    FROM players
                    GROUP BY COALESCE(display_name, first_name)
                    HAVING COUNT(*) > 1
                ),
                PlayerScores AS (
                    SELECT 
                        p.id, 
                        p.first_name,
                        p.last_name,
                        COALESCE(p.display_name, p.first_name) as display_name,
                        dc.name_count IS NOT NULL as has_duplicate,
                        COALESCE(SUM(gs.score), 0) as total_score,
                        RANK() OVER (ORDER BY COALESCE(SUM(gs.score), 0) ASC) as rank
                    FROM active_game_players agp
                    JOIN players p ON p.id = agp.player_id
                    LEFT JOIN game_scores gs
                      ON gs.player_id = p.id AND gs.active_game_id = agp.active_game_id
                    LEFT JOIN DisplayNameCounts dc ON COALESCE(p.display_name, p.first_name) = dc.display_name
                    WHERE agp.active_game_id = %s
                    GROUP BY p.id, p.first_name, p.last_name, p.display_name, dc.name_count
                )
                SELECT *
                FROM PlayerScores
                ORDER BY rank ASC, display_name ASC
            ''', (game['id'],))
            
            if scores:
                player_count = execute_query_one(conn, '''
                    SELECT COUNT(DISTINCT player_id) as count
                    FROM active_game_players
                    WHERE active_game_id = %s
                ''', (game['id'],))['count']
                
                execute_modify(conn, '''
                    UPDATE active_games 
                    SET is_complete = TRUE, completion_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                ''', (game['id'],))

                winners = [s for s in scores if s['rank'] == 1]
                
                summary = "Game Complete!\n\n"
                summary += "Final Scores:\n"
                for player in scores:
                    name = player['display_name']
                    if player['has_duplicate']:
                        name += f" ({player['first_name']} {player['last_name']})"
                    if player['rank'] == 1:
                        name = f"{name} [WINNER]"
                    summary += f"{name}: {player['total_score']}\n"
                
                if len(winners) > 1:
                    winner_names = []
                    for winner in winners:
                        name = winner['display_name']
                        if winner['has_duplicate']:
                            name += f" ({winner['first_name']} {winner['last_name']})"
                        winner_names.append(name)
                    
                    summary += f"\nTie Game! Winners: {', '.join(winner_names)} with {winners[0]['total_score']} points!"
                    
                    for winner in winners:
                        execute_modify(conn, '''
                            INSERT INTO game_stats (game_id, winner_id, winning_score, player_count, is_tie)
                            VALUES (%s, %s, %s, %s, TRUE)
                        ''', (game['id'], winner['id'], winner['total_score'], player_count))
                else:
                    winner = winners[0]
                    name = winner['display_name']
                    if winner['has_duplicate']:
                        name += f" ({winner['first_name']} {winner['last_name']})"
                    summary += f"\nWinner: {name} with {winner['total_score']} points!"
                    
                    execute_modify(conn, '''
                        INSERT INTO game_stats (game_id, winner_id, winning_score, player_count, is_tie)
                        VALUES (%s, %s, %s, %s, FALSE)
                    ''', (game['id'], winner['id'], winner['total_score'], player_count))
                
                conn.commit()
                
                broadcast_game_completed(game['id'], summary)
                
                return jsonify({'success': True, 'summary': summary})
            else:
                conn.rollback()
                return jsonify({'error': 'No scores recorded for this game'}), 400
        else:
            return jsonify({'error': 'No active game found'}), 404
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/five-crowns/pause', methods=['POST'])
@login_required
def pause_five_crowns_game():
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        specific_id = data.get('game_id') or request.args.get('game_id')
        if specific_id:
            game = fetch_accessible_game(
                conn, specific_id, user,
                extra_where='ag.game_id = 1 AND ag.is_complete = FALSE AND ag.is_paused = FALSE')
        else:
            access_sql, access_params = play_family_clause(user, 'ag')
            game = execute_query_one(conn, f'''
                SELECT ag.id FROM active_games ag
                WHERE ag.game_id = 1 AND {access_sql}
                AND ag.is_complete = FALSE AND ag.is_paused = FALSE
                ORDER BY ag.start_time DESC LIMIT 1
            ''', tuple(access_params))
        
        if not game:
            return jsonify({'error': 'Game not found'}), 404

        execute_modify(conn, '''
            UPDATE active_games
            SET is_paused = TRUE
            WHERE id = %s
        ''', (game['id'],))
        conn.commit()
        broadcast_game_paused(game['id'])
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/five-crowns/resume/<int:game_id>', methods=['POST'])
@login_required
def resume_five_crowns_game(game_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        game = fetch_accessible_game(
            conn, game_id, user,
            extra_where='ag.game_id = 1 AND ag.is_paused = TRUE AND ag.is_complete = FALSE')
        
        if not game:
            conn.close()
            return jsonify({'error': 'Game not found or not paused'}), 404
        
        execute_modify(conn, '''
            UPDATE active_games 
            SET is_paused = FALSE
            WHERE id = %s
        ''', (game_id,))
        
        conn.commit()
        
        broadcast_game_resumed(game_id)
        
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/pause/<int:game_id>', methods=['POST'])
@login_required
def pause_game_generic(game_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        game = fetch_accessible_game(
            conn, game_id, user,
            extra_where='ag.is_complete = FALSE AND ag.is_paused = FALSE')
        if not game:
            return jsonify({'error': 'Game not found'}), 404
        execute_modify(conn, 'UPDATE active_games SET is_paused = TRUE WHERE id = %s', (game_id,))
        conn.commit()
        try:
            broadcast_game_paused(game_id)
        except:
            pass
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/resume/<int:game_id>', methods=['POST'])
@login_required
def resume_game_generic(game_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        game = fetch_accessible_game(
            conn, game_id, user,
            extra_where='ag.is_paused = TRUE AND ag.is_complete = FALSE')
        if not game:
            return jsonify({'error': 'Game not found or not paused'}), 404
        execute_modify(conn, 'UPDATE active_games SET is_paused = FALSE WHERE id = %s', (game_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/complete/<int:game_id>', methods=['POST'])
@login_required
def complete_game_generic(game_id):
    """Generic game completion endpoint for any game type."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        game = fetch_accessible_game(conn, game_id, user, extra_where='ag.is_complete = FALSE')
        if not game:
            return jsonify({'error': 'Game not found'}), 404
        
        low_wins = game['scoring_direction'] == 'low_wins'
        order = 'ASC' if low_wins else 'DESC'
        
        scores = execute_query(conn, f'''
            WITH PlayerScores AS (
                SELECT p.id, p.first_name, p.last_name,
                    COALESCE(p.display_name, p.first_name) as display_name,
                    SUM(gs.score) as total_score,
                    RANK() OVER (ORDER BY SUM(gs.score) {order}) as rank
                FROM players p
                JOIN game_scores gs ON p.id = gs.player_id
                WHERE gs.active_game_id = %s
                GROUP BY p.id, p.first_name, p.last_name, p.display_name
            )
            SELECT * FROM PlayerScores ORDER BY rank ASC
        ''', (game_id,))
        
        player_count = execute_query_one(conn, '''
            SELECT COUNT(DISTINCT player_id) as count FROM active_game_players WHERE active_game_id = %s
        ''', (game_id,))['count']
        
        execute_modify(conn, '''
            UPDATE active_games SET is_complete = TRUE, completion_time = CURRENT_TIMESTAMP WHERE id = %s
        ''', (game_id,))
        
        summary = "Game Complete!\n\n"
        if scores:
            winners = [s for s in scores if s['rank'] == 1]
            is_tie = len(winners) > 1

            if is_tie:
                winner_names = ', '.join([w['display_name'] for w in winners])
                summary += f"Tie Game! Winners: {winner_names} with {winners[0]['total_score']} points!\n\n"
            else:
                summary += f"Winner: {winners[0]['display_name']} with {winners[0]['total_score']} points!\n\n"

            summary += "Final Rankings:\n"
            for p in scores:
                trophy = " [WINNER]" if p['rank'] == 1 else ""
                summary += f"  {p['rank']}. {p['display_name']}: {p['total_score']}{trophy}\n"

            for w in winners:
                execute_modify(conn, '''
                    INSERT INTO game_stats (game_id, winner_id, winning_score, player_count, is_tie)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (game_id, w['id'], w['total_score'], player_count, is_tie))
        
        conn.commit()
        try:
            broadcast_game_completed(game_id, summary)
        except:
            pass
        return jsonify({'success': True, 'summary': summary})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/trouble/complete', methods=['POST'])
@login_required
def complete_trouble_game():
    """Custom completion endpoint for Trouble with SOW scoring."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.get_json()
        game_id = data.get('game_id')
        winner_id = data.get('winner_id')
        loser_data = data.get('loser_data', [])

        game = fetch_accessible_game(conn, game_id, user, extra_where='ag.is_complete = FALSE')
        if not game:
            return jsonify({'error': 'Game not found'}), 404

        participant_rows = execute_query(conn, '''
            SELECT player_id FROM active_game_players WHERE active_game_id = %s
        ''', (game_id,))
        participant_ids = {r['player_id'] for r in participant_rows}
        submitted_ids = [winner_id] + [l.get('player_id') for l in loser_data]
        if not winner_id or any(pid not in participant_ids for pid in submitted_ids):
            return jsonify({'error': 'All players must be participants in this game'}), 400

        base_points = 25
        board_bonus = 0
        home_bonus = 0

        for loser in loser_data:
            board_bonus += loser.get('pegs_board', 0) * 5
            home_bonus += loser.get('pegs_home', 0) * 10

        total_sow = base_points + board_bonus + home_bonus

        execute_modify(conn, '''
            INSERT INTO game_scores (active_game_id, player_id, round_number, score, metadata)
            VALUES (%s, %s, 1, %s, %s)
            ON CONFLICT (active_game_id, player_id, round_number) DO UPDATE SET score = %s, metadata = %s
        ''', (game_id, winner_id, total_sow,
              json.dumps({'sow': total_sow, 'base': base_points, 'board_bonus': board_bonus, 'home_bonus': home_bonus}),
              total_sow,
              json.dumps({'sow': total_sow, 'base': base_points, 'board_bonus': board_bonus, 'home_bonus': home_bonus})))

        for loser in loser_data:
            lid = loser['player_id']
            meta = json.dumps({
                'pegs_finish': loser.get('pegs_finish', 0),
                'pegs_board': loser.get('pegs_board', 0),
                'pegs_home': loser.get('pegs_home', 0)
            })
            execute_modify(conn, '''
                INSERT INTO game_scores (active_game_id, player_id, round_number, score, metadata)
                VALUES (%s, %s, 1, 0, %s)
                ON CONFLICT (active_game_id, player_id, round_number) DO UPDATE SET score = 0, metadata = %s
            ''', (game_id, lid, meta, meta))

        player_count = execute_query_one(conn, '''
            SELECT COUNT(DISTINCT player_id) as count FROM active_game_players WHERE active_game_id = %s
        ''', (game_id,))['count']

        execute_modify(conn, '''
            UPDATE active_games SET is_complete = TRUE, completion_time = CURRENT_TIMESTAMP WHERE id = %s
        ''', (game_id,))

        execute_modify(conn, '''
            INSERT INTO game_stats (game_id, winner_id, winning_score, player_count, is_tie)
            VALUES (%s, %s, %s, %s, FALSE)
        ''', (game_id, winner_id, total_sow, player_count))

        winner_name = execute_query_one(conn, '''
            SELECT COALESCE(display_name, first_name) as name FROM players WHERE id = %s
        ''', (winner_id,))['name']

        summary = "Game Complete!\n\n"
        summary += f"Winner: {winner_name}\n"
        summary += f"SOW (Strength of Win): {total_sow} pts\n\n"
        summary += f"  Base win: 25 pts\n"
        if board_bonus > 0:
            summary += f"  Board pegs bonus: +{board_bonus} pts\n"
        if home_bonus > 0:
            summary += f"  Home pegs bonus: +{home_bonus} pts\n"

        conn.commit()
        try:
            broadcast_game_completed(game_id, summary)
        except:
            pass
        return jsonify({'success': True, 'summary': summary})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/kings-corner/complete', methods=['POST'])
@login_required
def complete_kings_corner_sow():
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.get_json()
        game_id = data.get('game_id')
        winner_id = data.get('winner_id')
        loser_penalties = data.get('loser_penalties', [])

        game = fetch_accessible_game(conn, game_id, user, extra_where='ag.is_complete = FALSE')
        if not game:
            return jsonify({'error': 'Game not found'}), 404

        participant_rows = execute_query(conn, '''
            SELECT player_id FROM active_game_players WHERE active_game_id = %s
        ''', (game_id,))
        participant_ids = {r['player_id'] for r in participant_rows}
        submitted_ids = [winner_id] + [l.get('player_id') for l in loser_penalties]
        if not winner_id or any(pid not in participant_ids for pid in submitted_ids):
            return jsonify({'error': 'All players must be participants in this game'}), 400

        total_sow = 0
        for loser in loser_penalties:
            total_sow += loser.get('penalty', 0)

        execute_modify(conn, '''
            INSERT INTO game_scores (active_game_id, player_id, round_number, score, metadata)
            VALUES (%s, %s, 1, %s, %s)
            ON CONFLICT (active_game_id, player_id, round_number) DO UPDATE SET score = %s, metadata = %s
        ''', (game_id, winner_id, total_sow,
              json.dumps({'sow': total_sow, 'type': 'single_round'}),
              total_sow,
              json.dumps({'sow': total_sow, 'type': 'single_round'})))

        for loser in loser_penalties:
            lid = loser['player_id']
            penalty = loser.get('penalty', 0)
            execute_modify(conn, '''
                INSERT INTO game_scores (active_game_id, player_id, round_number, score, metadata)
                VALUES (%s, %s, 1, 0, %s)
                ON CONFLICT (active_game_id, player_id, round_number) DO UPDATE SET score = 0, metadata = %s
            ''', (game_id, lid,
                  json.dumps({'penalty': penalty, 'type': 'loser'}),
                  json.dumps({'penalty': penalty, 'type': 'loser'})))

        player_count = execute_query_one(conn, '''
            SELECT COUNT(DISTINCT player_id) as count FROM active_game_players WHERE active_game_id = %s
        ''', (game_id,))['count']

        execute_modify(conn, '''
            UPDATE active_games SET is_complete = TRUE, completion_time = CURRENT_TIMESTAMP WHERE id = %s
        ''', (game_id,))

        execute_modify(conn, '''
            INSERT INTO game_stats (game_id, winner_id, winning_score, player_count, is_tie)
            VALUES (%s, %s, %s, %s, FALSE)
        ''', (game_id, winner_id, total_sow, player_count))

        winner_name = execute_query_one(conn, '''
            SELECT COALESCE(display_name, first_name) as name FROM players WHERE id = %s
        ''', (winner_id,))['name']

        summary = "Game Complete!\n\n"
        summary += f"Winner: {winner_name}\n"
        summary += f"SOW (Strength of Win): {total_sow} pts\n\n"
        summary += "Final Rankings:\n"
        summary += f"  1. {winner_name}: {total_sow} pts [WINNER]\n"
        rank = 2
        sorted_losers = sorted(loser_penalties, key=lambda x: x.get('penalty', 0), reverse=True)
        for loser in sorted_losers:
            lname = loser.get('name', 'Player')
            lpenalty = loser.get('penalty', 0)
            summary += f"  {rank}. {lname}: {lpenalty} pts remaining\n"
            rank += 1

        conn.commit()
        try:
            broadcast_game_completed(game_id, summary)
        except:
            pass
        return jsonify({'success': True, 'summary': summary})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/basic-games/names')
@login_required
def basic_game_names():
    conn = get_db_connection()
    user = get_current_user()
    try:
        BASIC_GAME_ID = 7
        names = execute_query(conn, '''
            SELECT custom_game_name, COUNT(*) as cnt
            FROM active_games
            WHERE game_id = %s AND family_id = %s AND custom_game_name IS NOT NULL
            GROUP BY custom_game_name
            ORDER BY cnt DESC, custom_game_name ASC
        ''', (BASIC_GAME_ID, user.get('family_id')))
        return jsonify({'names': [n['custom_game_name'] for n in names]})
    finally:
        conn.close()

@main.route('/api/games/basic/complete-quick', methods=['POST'])
@login_required
def complete_basic_quick():
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.get_json()
        game_id = data.get('game_id')
        winner_id = data.get('winner_id')

        game = fetch_accessible_game(conn, game_id, user, extra_where='ag.is_complete = FALSE')
        if not game:
            return jsonify({'error': 'Game not found'}), 404

        winner_participant = execute_query_one(conn, '''
            SELECT 1 FROM active_game_players WHERE active_game_id = %s AND player_id = %s
        ''', (game_id, winner_id))
        if not winner_participant:
            return jsonify({'error': 'Winner must be a participant in this game'}), 400

        player_count = execute_query_one(conn, '''
            SELECT COUNT(DISTINCT player_id) as count FROM active_game_players WHERE active_game_id = %s
        ''', (game_id,))['count']

        execute_modify(conn, '''
            UPDATE active_games SET is_complete = TRUE, completion_time = CURRENT_TIMESTAMP WHERE id = %s
        ''', (game_id,))

        execute_modify(conn, '''
            INSERT INTO game_stats (game_id, winner_id, winning_score, player_count, is_tie)
            VALUES (%s, %s, 1, %s, FALSE)
        ''', (game_id, winner_id, player_count))

        winner_name = execute_query_one(conn, '''
            SELECT COALESCE(display_name, first_name) as name FROM players WHERE id = %s
        ''', (winner_id,))['name']

        summary = "Game Complete!\n\nWinner: " + winner_name

        conn.commit()
        try:
            broadcast_game_completed(game_id, summary)
        except:
            pass
        return jsonify({'success': True, 'summary': summary})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/delete/<int:game_id>', methods=['POST'])
@login_required
def delete_game(game_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        game = execute_query_one(conn, '''
            SELECT ag.id, ag.user_id, ag.family_id FROM active_games ag
            WHERE ag.id = %s AND ag.is_complete = FALSE
        ''', (game_id,))
        
        if not game:
            return jsonify({'error': 'Game not found'}), 404
        
        if not user_can_access_active_game(user, game):
            return jsonify({'error': 'Access denied'}), 403
        
        execute_modify(conn, 'DELETE FROM game_scores WHERE active_game_id = %s', (game_id,))
        execute_modify(conn, 'DELETE FROM active_game_players WHERE active_game_id = %s', (game_id,))
        execute_modify(conn, 'DELETE FROM active_games WHERE id = %s', (game_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/<slug>/session-info', methods=['GET'])
@login_required
def game_session_info(slug):
    """Returns active, paused games and available players for the Play modal."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        game_def = execute_query_one(conn, 'SELECT * FROM games WHERE slug = %s', (slug,))
        if not game_def:
            return jsonify({'error': 'Game not found'}), 404
        
        access_sql, access_params = play_family_clause(user, 'ag')
        active_games = list(execute_query(conn, f'''
            SELECT ag.id, ag.start_time, ag.is_paused,
                string_agg(COALESCE(p.display_name, p.first_name), ', ' ORDER BY agp.id) as player_names,
                COUNT(DISTINCT agp.player_id) as player_count
            FROM active_games ag
            JOIN active_game_players agp ON ag.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            WHERE ag.game_id = %s AND {access_sql} AND ag.is_complete = FALSE
            GROUP BY ag.id, ag.start_time, ag.is_paused
            ORDER BY ag.start_time DESC LIMIT 10
        ''', tuple([game_def['id']] + list(access_params))))
        
        for g in active_games:
            g['start_time'] = g['start_time'].strftime('%b %d, %I:%M %p') if g.get('start_time') else ''
        
        players = get_family_players(user.get('family_id'), include_crew=True)
        
        slug_to_route = {'five-crowns': 'five_crowns', 'uno-classic': 'uno', 'uno-flip': 'uno_flip'}
        route_name = slug_to_route.get(slug, slug.replace('-', '_'))
        
        return jsonify({
            'game': dict(game_def),
            'active_games': active_games,
            'players': [dict(p) for p in players],
            'route_name': route_name
        })
    finally:
        conn.close()

@main.route('/api/games/five-crowns/complete/<int:game_id>', methods=['POST'])
@login_required
def complete_paused_game(game_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        game = fetch_accessible_game(
            conn, game_id, user,
            extra_where='ag.game_id = 1 AND ag.is_complete = FALSE')
        
        if game:
            scores = execute_query(conn, '''
                WITH DisplayNameCounts AS (
                    SELECT COALESCE(display_name, first_name) as display_name, 
                           COUNT(*) as name_count
                    FROM players
                    GROUP BY COALESCE(display_name, first_name)
                    HAVING COUNT(*) > 1
                )
                SELECT 
                    p.id, 
                    p.first_name,
                    p.last_name,
                    COALESCE(p.display_name, p.first_name) as display_name,
                    dc.name_count IS NOT NULL as has_duplicate,
                    SUM(gs.score) as total_score
                FROM players p
                JOIN game_scores gs ON p.id = gs.player_id
                LEFT JOIN DisplayNameCounts dc ON COALESCE(p.display_name, p.first_name) = dc.display_name
                WHERE gs.active_game_id = %s
                GROUP BY p.id, p.first_name, p.last_name, p.display_name
                ORDER BY total_score ASC
            ''', (game['id'],))
            
            winner = scores[0] if scores else None
            
            if winner:
                player_count = execute_query_one(conn, '''
                    SELECT COUNT(DISTINCT player_id) as count
                    FROM active_game_players
                    WHERE active_game_id = %s
                ''', (game['id'],))['count']
                
                execute_modify(conn, '''
                    INSERT INTO game_stats 
                    (game_id, winner_id, winning_score, player_count)
                    VALUES (%s, %s, %s, %s)
                ''', (game['id'], winner['id'], winner['total_score'], player_count))
                
                execute_modify(conn, '''
                    UPDATE active_games 
                    SET is_complete = TRUE, completion_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                ''', (game['id'],))
                
                conn.commit()
                
                score_summary = "Game Complete!\n\n"
                if winner:
                    display_name = winner['display_name']
                    if winner['has_duplicate']:
                        display_name = f"{winner['display_name']} ({winner['first_name']} {winner['last_name']})"
                    score_summary += f"Winner: {display_name} ({winner['total_score']} points)\n\n"
                    
                    score_summary += "Final Rankings:\n"
                    current_rank = 1
                    current_score = None
                    for i, player in enumerate(scores, 1):
                        if player['total_score'] != current_score:
                            current_rank = i
                            current_score = player['total_score']
                        
                        display_name = player['display_name']
                        if player['has_duplicate']:
                            display_name = f"{player['display_name']} ({player['first_name']} {player['last_name']})"
                        
                        score_summary += f"{current_rank}. {display_name}: {player['total_score']} points\n"
                
                broadcast_game_completed(game['id'], score_summary)
                
                return jsonify({
                    'success': True,
                    'summary': score_summary
                })
        
        return jsonify({'success': True})
    finally:
        conn.close()

@main.route('/api/games/five-crowns/clear-history', methods=['POST'])
@login_required
def clear_completed_games():
    """Clear completed game history. Scoped to the current user's own games and,
    when game_id (a games.id game type) is provided, to that game type only.
    This matches exactly what each game page displays as 'Recent Completed Games'."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.get_json(silent=True) or {}
        game_type_id = data.get('game_id')

        where = 'is_complete = TRUE AND user_id = %s'
        params = [user['id']]
        if game_type_id:
            where += ' AND game_id = %s'
            params.append(int(game_type_id))

        completed_ids = execute_query(conn,
            'SELECT id FROM active_games WHERE ' + where, tuple(params))
        if completed_ids:
            ids = [g['id'] for g in completed_ids]
            execute_modify(conn, 'DELETE FROM game_stats WHERE game_id = ANY(%s)', (ids,))
            execute_modify(conn, 'DELETE FROM game_scores WHERE active_game_id = ANY(%s)', (ids,))
            execute_modify(conn, 'DELETE FROM active_game_players WHERE active_game_id = ANY(%s)', (ids,))
            execute_modify(conn, 'DELETE FROM active_games WHERE id = ANY(%s)', (ids,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

def _viewer_circle_player_ids(conn, user):
    """Home family + accepted alliance families + personal crew."""
    fam = user.get('family_id')
    pid = user.get('player_id')
    if not fam and not pid:
        return [-1]
    roster = get_family_players(fam, include_crew=True, for_player_id=pid) if fam else []
    ids = [p['id'] for p in roster]
    if pid and pid not in ids:
        ids.append(pid)
    return ids or [-1]


def _leaderboard_page(scope='circle'):
    """Build leaderboard.html context. scope=circle|all."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        game_type = request.args.get('game_type', type=int)

        available_games_list = execute_query(conn, '''
            SELECT id, name, slug FROM games WHERE is_variant_group = FALSE ORDER BY COALESCE(display_order, id * 10), id
        ''')

        sub_game = request.args.get('sub_game')
        BASIC_GAME_ID = 7

        show_scores = bool(game_type)
        scoring_direction = 'low_wins'
        if game_type:
            sel_game = execute_query_one(conn, 'SELECT scoring_direction FROM games WHERE id = %s', (game_type,))
            if sel_game and sel_game.get('scoring_direction'):
                scoring_direction = sel_game['scoring_direction']
        if scoring_direction == 'high_wins':
            best_score_expr = 'MAX(game_stats.winning_score)'
            best_score_order = 'best_score DESC'
        else:
            best_score_expr = 'MIN(game_stats.winning_score)'
            best_score_order = 'best_score ASC'

        gt_filter = ''
        gt_params = []
        if game_type:
            gt_filter = 'AND ag.game_id = %s'
            gt_params.append(game_type)
        if sub_game and game_type == BASIC_GAME_ID:
            gt_filter += ' AND ag.custom_game_name = %s'
            gt_params.append(sub_game)

        circle_ids = None
        if scope == 'circle':
            circle_ids = _viewer_circle_player_ids(conn, user)
            gt_filter += ' AND p.id = ANY(%s)'
            gt_params.append(circle_ids)

        gt_params = tuple(gt_params) if gt_params else ()

        sub_game_names = []
        if game_type == BASIC_GAME_ID:
            sub_names = execute_query(conn, '''
                SELECT DISTINCT ag.custom_game_name
                FROM active_games ag
                WHERE ag.game_id = %s AND ag.custom_game_name IS NOT NULL AND ag.is_complete = TRUE
                ORDER BY ag.custom_game_name
            ''', (BASIC_GAME_ID,))
            sub_game_names = [r['custom_game_name'] for r in sub_names]

        top_winners = execute_query(conn, '''
            WITH PlayerStats AS (
                SELECT 
                    p.id,
                    p.first_name,
                    p.last_name,
                    COALESCE(p.display_name, p.first_name) as display_name,
                    COUNT(*) FILTER (WHERE NOT game_stats.is_tie) as solo_wins,
                    COUNT(*) FILTER (WHERE game_stats.is_tie) as tied_wins,
                    COUNT(*) as total_wins,
                    AVG(game_stats.winning_score) as avg_score,
                    ''' + best_score_expr + ''' as best_score
                FROM players p
                JOIN game_stats ON p.id = game_stats.winner_id
                JOIN active_games ag ON game_stats.game_id = ag.id
                WHERE TRUE ''' + gt_filter + '''
                GROUP BY p.id, p.first_name, p.last_name, p.display_name
            )
            SELECT 
                display_name,
                solo_wins,
                tied_wins,
                total_wins,
                ROUND(avg_score::numeric, 1) as avg_score,
                best_score
            FROM PlayerStats
            ORDER BY total_wins DESC, ''' + best_score_order + '''
            LIMIT 10
        ''', gt_params or None)
        
        longest_streaks = execute_query(conn, '''
            WITH PlayerWins AS (
                SELECT 
                    gs.winner_id,
                    COALESCE(p.display_name, p.first_name) as display_name,
                    ag.completion_time,
                    gs.is_tie,
                    LAG(gs.winner_id) OVER (ORDER BY ag.completion_time) as prev_winner,
                    ROW_NUMBER() OVER (ORDER BY ag.completion_time) as win_order
                FROM game_stats gs
                JOIN active_games ag ON gs.game_id = ag.id
                JOIN players p ON gs.winner_id = p.id
                WHERE ag.is_complete = TRUE ''' + gt_filter + '''
                ORDER BY ag.completion_time
            ),
            StreakGroups AS (
                SELECT 
                    winner_id,
                    display_name,
                    completion_time,
                    is_tie,
                    win_order,
                    SUM(CASE WHEN winner_id != COALESCE(prev_winner, -1) THEN 1 ELSE 0 END) 
                        OVER (ORDER BY win_order) as streak_group
                FROM PlayerWins
            ),
            StreakCounts AS (
                SELECT 
                    winner_id,
                    display_name,
                    streak_group,
                    COUNT(*) as streak_length,
                    COUNT(*) FILTER (WHERE is_tie) as ties_in_streak,
                    MIN(completion_time) as streak_start_date
                FROM StreakGroups
                GROUP BY winner_id, display_name, streak_group
                HAVING COUNT(*) >= 2
            )
            SELECT 
                display_name,
                streak_length,
                ties_in_streak,
                streak_start_date
            FROM StreakCounts
            ORDER BY streak_length DESC, ties_in_streak ASC
            LIMIT 10
        ''', gt_params or None)

        overall_records = execute_query(conn, '''
            WITH PlayerGames AS (
                SELECT 
                    p.id,
                    p.first_name,
                    p.last_name,
                    COALESCE(p.display_name, p.first_name) as display_name,
                    ag.id as game_id,
                    gs.winner_id,
                    gs.is_tie
                FROM players p
                JOIN active_game_players agp ON p.id = agp.player_id
                JOIN active_games ag ON agp.active_game_id = ag.id
                LEFT JOIN game_stats gs ON ag.id = gs.game_id
                WHERE ag.is_complete = TRUE ''' + gt_filter + '''
            )
            SELECT 
                id,
                first_name,
                last_name,
                display_name,
                COUNT(DISTINCT game_id) as total_games,
                COUNT(DISTINCT CASE WHEN id = winner_id AND NOT is_tie THEN game_id END) as wins,
                COUNT(DISTINCT CASE WHEN winner_id IS NOT NULL AND id != winner_id AND NOT is_tie THEN game_id END) as losses,
                COUNT(DISTINCT CASE WHEN is_tie THEN game_id END) as ties,
                CAST(
                    CAST(COUNT(DISTINCT CASE WHEN id = winner_id OR is_tie THEN game_id END) AS FLOAT) * 100.0 / 
                    NULLIF(COUNT(DISTINCT game_id), 0)
                AS NUMERIC(10,1)
                ) as win_percentage
            FROM PlayerGames
            GROUP BY id, first_name, last_name, display_name
            HAVING COUNT(DISTINCT game_id) > 0
            ORDER BY win_percentage DESC, wins DESC
        ''', gt_params or None)

        # Recent games: for circle scope, include any completed game that
        # involved at least one circle player (not only winners).
        recent_filter = gt_filter
        recent_params = list(gt_params) if gt_params else []
        if scope == 'circle' and circle_ids is not None:
            # Drop the p.id filter from gt_filter for this query and use EXISTS.
            recent_filter = ''
            recent_params = []
            if game_type:
                recent_filter += ' AND ag.game_id = %s'
                recent_params.append(game_type)
            if sub_game and game_type == BASIC_GAME_ID:
                recent_filter += ' AND ag.custom_game_name = %s'
                recent_params.append(sub_game)
            recent_filter += ''' AND EXISTS (
                SELECT 1 FROM active_game_players cx
                WHERE cx.active_game_id = ag.id AND cx.player_id = ANY(%s)
            )'''
            recent_params.append(circle_ids)

        recent_games = execute_query(conn, '''
            WITH GameDetails AS (
                SELECT 
                    ag.id as game_id,
                    g.name as game_name,
                    ag.custom_game_name,
                    ag.start_time,
                    COUNT(DISTINCT agp.player_id) as player_count,
                    STRING_AGG(
                        DISTINCT CASE 
                            WHEN dc.name_count IS NOT NULL 
                            THEN p.display_name || ' (' || p.first_name || ' ' || p.last_name || ')'
                            ELSE COALESCE(p.display_name, p.first_name)
                        END,
                        ', ' ORDER BY CASE 
                            WHEN dc.name_count IS NOT NULL 
                            THEN p.display_name || ' (' || p.first_name || ' ' || p.last_name || ')'
                            ELSE COALESCE(p.display_name, p.first_name)
                        END
                    ) as player_names,
                    MIN(gst.winning_score) as winning_score,
                    bool_or(gst.is_tie) as is_tie,
                    STRING_AGG(
                        DISTINCT CASE 
                            WHEN dc.name_count IS NOT NULL 
                            THEN pw.display_name || ' (' || pw.first_name || ' ' || pw.last_name || ')'
                            ELSE COALESCE(pw.display_name, pw.first_name)
                        END,
                        ' & ' ORDER BY CASE 
                            WHEN dc.name_count IS NOT NULL 
                            THEN pw.display_name || ' (' || pw.first_name || ' ' || pw.last_name || ')'
                            ELSE COALESCE(pw.display_name, pw.first_name)
                        END
                    ) as winner_names,
                    json_agg(
                        json_build_object(
                            'player_name', COALESCE(p.display_name, p.first_name),
                            'total_score', COALESCE((
                                SELECT SUM(score)
                                FROM game_scores sc
                                WHERE sc.active_game_id = ag.id
                                AND sc.player_id = p.id
                            ), 0)
                        )
                    ) as player_scores
                FROM active_games ag
                JOIN games g ON ag.game_id = g.id
                JOIN active_game_players agp ON ag.id = agp.active_game_id
                JOIN players p ON agp.player_id = p.id
                LEFT JOIN game_stats gst ON ag.id = gst.game_id
                LEFT JOIN players pw ON gst.winner_id = pw.id
                LEFT JOIN (
                    SELECT COALESCE(display_name, first_name) as display_name, 
                           COUNT(*) as name_count
                    FROM players
                    GROUP BY COALESCE(display_name, first_name)
                    HAVING COUNT(*) > 1
                ) dc ON COALESCE(p.display_name, p.first_name) = dc.display_name
                WHERE ag.is_complete = TRUE ''' + recent_filter + '''
                GROUP BY ag.id, ag.start_time, g.name, ag.custom_game_name
            )
            SELECT 
                game_id,
                COALESCE(custom_game_name, game_name) as game_name,
                start_time,
                player_count,
                player_names,
                winning_score,
                is_tie,
                winner_names || CASE 
                    WHEN is_tie THEN ' (Tie)'
                    ELSE ''
                END as winner_display,
                player_scores,
                ROW_NUMBER() OVER (PARTITION BY game_name ORDER BY start_time ASC) as game_number
            FROM GameDetails
            ORDER BY start_time DESC
        ''', tuple(recent_params) if recent_params else None)

        return render_template('leaderboard.html',
                            top_winners=top_winners,
                            longest_streaks=longest_streaks,
                            recent_games=recent_games,
                            stats=overall_records,
                            overall_records=overall_records,
                            available_games=available_games_list,
                            current_game_type=game_type,
                            sub_game_names=sub_game_names,
                            current_sub_game=sub_game,
                            show_scores=show_scores,
                            scoring_direction=scoring_direction,
                            leaderboard_scope=scope)
    finally:
        conn.close()


@main.route('/leaderboard')
@login_required
def leaderboard():
    return _leaderboard_page(scope='circle')


@main.route('/stats/all-time')
@login_required
def all_time_stats():
    return _leaderboard_page(scope='all')


@main.route('/api/games', methods=['GET'])
@login_required
def get_games():
    conn = get_db_connection()
    user = get_current_user()
    try:
        games = execute_query(conn, '''
            WITH GameDetails AS (
                SELECT 
                    ag.id,
                    g.name as game_name,
                    ag.completion_time,
                    ag.is_complete,
                    ag.is_paused,
                    (
                        SELECT COALESCE(pw.display_name, pw.first_name)
                        FROM game_stats gs
                        JOIN players pw ON gs.winner_id = pw.id
                        WHERE gs.game_id = ag.id
                        ORDER BY gs.id DESC
                        LIMIT 1
                    ) as winner
                FROM active_games ag
                JOIN games g ON ag.game_id = g.id
                WHERE ag.user_id = %s OR ag.id IN (
                    SELECT agm.active_game_id FROM active_game_players agm
                    WHERE agm.player_id = (SELECT player_id FROM users WHERE id = %s)
                )
            )
            SELECT 
                gd.*,
                CASE 
                    WHEN gd.is_complete THEN 'completed'
                    WHEN gd.is_paused THEN 'paused'
                    ELSE 'active'
                END as status,
                string_agg(DISTINCT COALESCE(p.display_name, p.first_name), ', ' ORDER BY COALESCE(p.display_name, p.first_name)) as player_names,
                json_agg(
                    json_build_object(
                        'player_name', COALESCE(p.display_name, p.first_name),
                        'total_score', COALESCE((
                            SELECT SUM(score)
                            FROM game_scores sc
                            WHERE sc.active_game_id = gd.id
                            AND sc.player_id = p.id
                        ), 0)
                    )
                ) as player_scores
            FROM GameDetails gd
            JOIN active_game_players agp ON gd.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            GROUP BY gd.id, gd.game_name, gd.completion_time, gd.is_complete, gd.is_paused, gd.winner
            ORDER BY gd.is_complete DESC, gd.completion_time DESC, gd.id DESC
        ''', (user['id'], user['id']))
        
        return jsonify([{
            'id': g['id'],
            'game_name': g['game_name'],
            'completion_time': g['completion_time'].isoformat() if g['completion_time'] else None,
            'status': g['status'],
            'winner': g['winner'],
            'player_names': g['player_names'].split(', ') if g['player_names'] else [],
            'player_scores': g['player_scores'] or []
        } for g in games])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/<int:game_id>', methods=['DELETE'])
@login_required
def delete_game_legacy(game_id):
    conn = get_db_connection()
    user = get_current_user()
    try:
        game_check = fetch_accessible_game(conn, game_id, user)
        
        if not game_check:
            return jsonify({'success': False, 'error': 'Game not found or access denied'}), 403
        
        execute_modify(conn, 'DELETE FROM game_scores WHERE active_game_id = %s', (game_id,))
        execute_modify(conn, 'DELETE FROM active_game_players WHERE active_game_id = %s', (game_id,))
        execute_modify(conn, 'DELETE FROM game_stats WHERE game_id = %s', (game_id,))
        execute_modify(conn, 'DELETE FROM active_games WHERE id = %s', (game_id,))
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/game-details/<game_id>')
@login_required
def get_game_details(game_id):
    game_id = game_id.replace('_', '-')
    conn = get_db_connection()
    try:
        game = execute_query_one(conn, '''
            SELECT * FROM games WHERE slug = %s OR id::text = %s
        ''', (game_id, game_id))
        
        if not game:
            game_id_map = {
                'five_crowns': 1,
                '1': 1
            }
            mapped_id = game_id_map.get(game_id)
            if mapped_id:
                game = execute_query_one(conn, 'SELECT * FROM games WHERE id = %s', (mapped_id,))
        
        if not game:
            return jsonify({'success': False, 'error': 'Game details not found'})
        
        try:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute('''
                SELECT *
                FROM game_details gd
                WHERE gd.game_id = %s
            ''', (game['id'],))
            game_details = cursor.fetchone()
            cursor.close()
        except Exception:
            game_details = None
        
        if game_details:
            return jsonify({
                'success': True,
                'details': {
                    'game_name': game['name'],
                    'notes': game_details.get('notes', 'No notes available'),
                    'rules': game_details.get('rules', 'No rules available'),
                    'scoring_system': game_details.get('scoring_system', ''),
                    'winning_conditions': game_details.get('winning_conditions', ''),
                    'setup_instructions': game_details.get('setup_instructions', ''),
                    'tips_and_strategies': game_details.get('tips_and_strategies', ''),
                    'description_long': game_details.get('description_long', game.get('description', '')),
                    'min_players': game_details.get('min_players', 2),
                    'max_players': game_details.get('max_players', 7),
                    'estimated_duration_minutes': game_details.get('estimated_duration_minutes', 60),
                    'difficulty_level': game_details.get('difficulty_level', 'Medium'),
                    'age_recommendation': game_details.get('age_recommendation', '8+')
                }
            })
        elif game['id'] == 1:
            return jsonify({
                'success': True,
                'details': {
                    'game_name': 'Five Crowns',
                    'notes': '''
                    <h4>Five Crowns Rules</h4>
                    <p><strong>Five Crowns</strong> is a fun rummy-style card game for 2-7 players.</p>
                    
                    <h5>Objective</h5>
                    <p>Have the lowest score after all 11 hands have been played.</p>
                    
                    <h5>Game Play</h5>
                    <ul>
                        <li><strong>Round 1:</strong> 3 cards (3s are wild)</li>
                        <li><strong>Round 2:</strong> 4 cards (4s are wild)</li>
                        <li><strong>Round 3:</strong> 5 cards (5s are wild)</li>
                        <li><strong>Round 4:</strong> 6 cards (6s are wild)</li>
                        <li><strong>Round 5:</strong> 7 cards (7s are wild)</li>
                        <li><strong>Round 6:</strong> 8 cards (8s are wild)</li>
                        <li><strong>Round 7:</strong> 9 cards (9s are wild)</li>
                        <li><strong>Round 8:</strong> 10 cards (10s are wild)</li>
                        <li><strong>Round 9:</strong> 11 cards (Jacks are wild)</li>
                        <li><strong>Round 10:</strong> 12 cards (Queens are wild)</li>
                        <li><strong>Round 11:</strong> 13 cards (Kings are wild)</li>
                    </ul>
                    
                    <h5>Scoring</h5>
                    <ul>
                        <li>3-10: Face value</li>
                        <li>Jacks: 11 points</li>
                        <li>Queens: 12 points</li>
                        <li>Kings: 13 points</li>
                        <li>Jokers: 50 points</li>
                    </ul>
                    
                    <p><strong>Going Out:</strong> All cards must be in books (3+ of a kind) or runs (3+ in sequence of same suit). One card may be discarded to go out.</p>
                    ''',
                    'rules': 'See notes for complete rules',
                    'description_long': 'A strategic rummy-style card game with rotating wild cards',
                    'min_players': 2,
                    'max_players': 7,
                    'estimated_duration_minutes': 60,
                    'difficulty_level': 'Medium',
                    'age_recommendation': '8+'
                }
            })
        else:
            return jsonify({
                'success': True,
                'details': {
                    'game_name': game['name'],
                    'description': game.get('description', ''),
                    'scoring_direction': game.get('scoring_direction', 'low'),
                    'has_rounds': game.get('has_rounds', True),
                    'default_target_score': game.get('default_target_score'),
                    'image_url': game.get('image_url', '')
                }
            })
    except Exception as e:
        print(f"Route error: {e}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

@main.route('/games/<int:game_id>/summary')
@login_required
def game_summary(game_id):
    """Full final standings + round-by-round page for a completed game."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        game = fetch_accessible_game(conn, game_id, user)
        if not game:
            # Also allow anyone who sat in the game.
            seated = execute_query_one(conn, '''
                SELECT 1 FROM active_game_players agp
                WHERE agp.active_game_id = %s AND agp.player_id = %s
            ''', (game_id, user.get('player_id') or -1))
            if seated or user.get('role') == 'super_admin':
                game = execute_query_one(conn, 'SELECT * FROM active_games WHERE id = %s', (game_id,))
        if not game:
            flash('That game is not available.', 'error')
            return redirect(url_for('main.games'))
        if not game.get('is_complete'):
            flash('That game is not finished yet.', 'info')
            return redirect(url_for('main.games'))

        meta = execute_query_one(conn, '''
            SELECT ag.id, ag.start_time, ag.completion_time, ag.custom_game_name,
                   g.name AS game_name, g.slug,
                   COALESCE(ag.scoring_direction, g.scoring_direction) AS scoring_direction,
                   gsn.game_number
            FROM active_games ag
            JOIN games g ON g.id = ag.game_id
            LEFT JOIN game_sessions_numbered gsn ON gsn.id = ag.id
            WHERE ag.id = %s
        ''', (game_id,))
        order = 'DESC' if (meta and meta.get('scoring_direction') == 'high_wins') else 'ASC'
        standings = execute_query(conn, '''
            SELECT p.id,
                COALESCE(p.display_name, p.first_name) AS display_name,
                p.first_name, p.last_name,
                COALESCE(SUM(gs.score), 0) AS total_score
            FROM active_game_players agp
            JOIN players p ON p.id = agp.player_id
            LEFT JOIN game_scores gs ON gs.active_game_id = agp.active_game_id AND gs.player_id = p.id
            WHERE agp.active_game_id = %s
            GROUP BY p.id, p.display_name, p.first_name, p.last_name
            ORDER BY total_score ''' + order + '''
        ''', (game_id,))
        players = execute_query(conn, '''
            SELECT p.id, COALESCE(p.display_name, p.first_name) AS display_name
            FROM active_game_players agp
            JOIN players p ON p.id = agp.player_id
            WHERE agp.active_game_id = %s
            ORDER BY agp.id
        ''', (game_id,))
        score_rows = execute_query(conn, '''
            SELECT player_id, round_number, score
            FROM game_scores WHERE active_game_id = %s
            ORDER BY round_number, player_id
        ''', (game_id,))
        by_round = {}
        for s in score_rows or []:
            by_round.setdefault(s['round_number'], {})[s['player_id']] = s['score']
        rounds = sorted(by_round.keys())
        totals = {p['id']: 0 for p in (players or [])}
        for r in rounds:
            for p in players or []:
                val = by_round.get(r, {}).get(p['id'])
                if val is not None:
                    totals[p['id']] += val
        winner = execute_query_one(conn, '''
            SELECT COALESCE(p.display_name, p.first_name) AS display_name, gs.winning_score, gs.is_tie
            FROM game_stats gs JOIN players p ON p.id = gs.winner_id
            WHERE gs.game_id = %s ORDER BY gs.id DESC LIMIT 1
        ''', (game_id,))
        return render_template(
            'game_summary.html',
            game=meta,
            standings=standings,
            players=players,
            rounds=rounds,
            by_round=by_round,
            totals=totals,
            winner=winner,
        )
    finally:
        conn.close()


@main.route('/api/games/<int:game_id>/final-scores')
@login_required
def get_final_scores(game_id):
    conn = get_db_connection()
    try:
        ag = execute_query_one(conn, '''
            SELECT COALESCE(ag.scoring_direction, g.scoring_direction) as scoring_direction
            FROM active_games ag JOIN games g ON ag.game_id = g.id
            WHERE ag.id = %s
        ''', (game_id,))
        order = 'DESC' if (ag and ag.get('scoring_direction') == 'high_wins') else 'ASC'

        scores = execute_query(conn, '''
            SELECT 
                COALESCE(p.display_name, p.first_name) as player_name,
                COALESCE(SUM(gs.score), 0) as total_score
            FROM active_games ag
            JOIN active_game_players agp ON ag.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            LEFT JOIN game_scores gs ON ag.id = gs.active_game_id AND p.id = gs.player_id
            WHERE ag.id = %s
            GROUP BY p.id, p.first_name, p.last_name, p.display_name
            ORDER BY total_score ''' + order + '''
        ''', (game_id,))
        
        return jsonify({
            'success': True,
            'scores': scores
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

@main.route('/api/games/<int:game_id>/round-by-round')
@login_required
def get_round_by_round_scores(game_id):
    conn = get_db_connection()
    try:
        game_info = execute_query_one(conn, '''
            SELECT 
                ag.id,
                ag.start_time,
                ag.completion_time
            FROM active_games ag
            WHERE ag.id = %s
        ''', (game_id,))
        
        if not game_info:
            return jsonify({'success': False, 'error': 'Game not found'})
        
        players = execute_query(conn, '''
            SELECT 
                p.id,
                COALESCE(p.display_name, p.first_name) as display_name,
                p.first_name,
                p.last_name
            FROM active_games ag
            JOIN active_game_players agp ON ag.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            WHERE ag.id = %s
            ORDER BY agp.id
        ''', (game_id,))
        
        if not players:
            return jsonify({'success': False, 'error': 'Game not found'})
        
        scores = execute_query(conn, '''
            SELECT 
                player_id,
                round_number,
                score
            FROM game_scores
            WHERE active_game_id = %s
            ORDER BY round_number, player_id
        ''', (game_id,))
        
        scores_dict = {}
        for score in scores:
            if score['round_number'] not in scores_dict:
                scores_dict[score['round_number']] = {}
            scores_dict[score['round_number']][score['player_id']] = score['score']
        
        html = '''
        <div class="table-responsive">
            <table class="table table-bordered">
                <thead class="table-dark">
                    <tr>
                        <th>Round</th>
        '''
        
        for player in players:
            html += f'<th>{player["display_name"]}</th>'
        
        html += '''
                    </tr>
                </thead>
                <tbody>
        '''
        
        running_totals = {player['id']: 0 for player in players}
        round_numbers = sorted(scores_dict.keys())

        for round_num in round_numbers:
            html += f'<tr><td class="fw-bold">{round_num}</td>'
            
            for player in players:
                score = scores_dict.get(round_num, {}).get(player['id'], '')
                if score:
                    running_totals[player['id']] += score
                html += f'<td>{score}</td>'
            
            html += '</tr>'
        
        html += '<tr class="table-warning"><td class="fw-bold">Total</td>'
        for player in players:
            html += f'<td class="fw-bold">{running_totals[player["id"]]}</td>'
        html += '</tr>'
        
        html += '''
                </tbody>
            </table>
        </div>
        '''
        
        game_date = game_info['completion_time'] or game_info['start_time']
        formatted_date = game_date.strftime('%B %d, %Y at %I:%M %p') if game_date else 'Unknown'
        
        return jsonify({
            'success': True,
            'html': html,
            'game_id': game_id,
            'game_date': formatted_date
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close() 

@main.route('/api/games/<int:game_id>/update-scores', methods=['POST'])
@login_required
@admin_required
def update_game_scores(game_id):
    """Update scores for a completed game and recalculate winner"""
    conn = get_db_connection()
    try:
        data = request.json
        scores_data = data.get('scores', [])
        
        if not scores_data:
            return jsonify({'error': 'No scores provided'}), 400
        
        conn.autocommit = False
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT ag.id, ag.is_complete, COALESCE(ag.scoring_direction, g.scoring_direction) as scoring_direction
            FROM active_games ag JOIN games g ON ag.game_id = g.id
            WHERE ag.id = %s AND ag.is_complete = TRUE
        ''', (game_id,))
        
        game = cursor.fetchone()
        if not game:
            conn.rollback()
            return jsonify({'error': 'Game not found or not completed'}), 404

        rank_order = 'DESC' if game['scoring_direction'] == 'high_wins' else 'ASC'
        
        cursor.execute('''
            DELETE FROM game_scores WHERE active_game_id = %s
        ''', (game_id,))
        
        for score_entry in scores_data:
            player_id = score_entry.get('player_id')
            round_number = score_entry.get('round_number')
            score = score_entry.get('score')
            
            if score is not None and score != '':
                cursor.execute('''
                    INSERT INTO game_scores (active_game_id, player_id, round_number, score)
                    VALUES (%s, %s, %s, %s)
                ''', (game_id, player_id, round_number, int(score)))
        
        cursor.execute(f'''
            WITH PlayerTotals AS (
                SELECT 
                    p.id,
                    p.first_name,
                    p.last_name,
                    COALESCE(p.display_name, p.first_name) as display_name,
                    COALESCE(SUM(gs.score), 0) as total_score,
                    RANK() OVER (ORDER BY COALESCE(SUM(gs.score), 0) {rank_order}) as rank
                FROM players p
                JOIN active_game_players agp ON p.id = agp.player_id
                LEFT JOIN game_scores gs ON p.id = gs.player_id AND gs.active_game_id = %s
                WHERE agp.active_game_id = %s
                GROUP BY p.id, p.first_name, p.last_name, p.display_name
            )
            SELECT id, display_name, total_score, rank
            FROM PlayerTotals
            ORDER BY rank ASC, display_name ASC
        ''', (game_id, game_id))
        
        results = cursor.fetchall()
        
        if not results:
            conn.rollback()
            return jsonify({'error': 'No players found for this game'}), 400
        
        cursor.execute('''
            SELECT COUNT(DISTINCT player_id) as count
            FROM active_game_players
            WHERE active_game_id = %s
        ''', (game_id,))
        player_count = cursor.fetchone()['count']
        
        cursor.execute('DELETE FROM game_stats WHERE game_id = %s', (game_id,))
        
        winners = [r for r in results if r['rank'] == 1]
        is_tie = len(winners) > 1
        
        for winner in winners:
            cursor.execute('''
                INSERT INTO game_stats (game_id, winner_id, winning_score, player_count, is_tie)
                VALUES (%s, %s, %s, %s, %s)
            ''', (game_id, winner['id'], winner['total_score'], player_count, is_tie))
        
        conn.commit()
        
        winner_names = [w['display_name'] for w in winners]
        winner_display = ' & '.join(winner_names)
        if is_tie:
            winner_display += ' (Tie)'
        
        return jsonify({
            'success': True,
            'message': 'Game scores updated successfully',
            'winner': winner_display,
            'winning_score': winners[0]['total_score'],
            'is_tie': is_tie,
            'final_scores': [
                {
                    'player_name': r['display_name'],
                    'total_score': r['total_score'],
                    'rank': r['rank']
                } for r in results
            ]
        })
        
    except Exception as e:
        conn.rollback()
        print(f"Error updating game scores: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/notifications')
@login_required
def get_notifications():
    user = get_current_user()
    conn = get_db_connection()
    try:
        notifs = execute_query(conn, '''
            SELECT id, type, title, message, data, is_read, created_at
            FROM notifications
            WHERE user_id = %s
            ORDER BY created_at DESC LIMIT 50
        ''', (user['id'],))
        return jsonify([dict(n) for n in notifs])
    finally:
        conn.close()

@main.route('/api/notifications/count')
@login_required
def notification_count():
    user = get_current_user()
    conn = get_db_connection()
    try:
        row = execute_query_one(conn, '''
            SELECT COUNT(*) as c FROM notifications
            WHERE user_id = %s AND is_read = FALSE
        ''', (user['id'],))
        return jsonify({'count': row['c']})
    finally:
        conn.close()

@main.route('/api/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    user = get_current_user()
    conn = get_db_connection()
    try:
        execute_modify(conn, '''
            UPDATE notifications SET is_read = TRUE
            WHERE id = %s AND user_id = %s
        ''', (notif_id, user['id']))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@main.route('/api/notifications/read-all', methods=['POST'])
@login_required
def mark_all_notifications_read():
    user = get_current_user()
    conn = get_db_connection()
    try:
        execute_modify(conn, '''
            UPDATE notifications SET is_read = TRUE
            WHERE user_id = %s AND is_read = FALSE
        ''', (user['id'],))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@main.route('/api/crew-links', methods=['GET', 'POST'])
@login_required
def crew_links():
    """Person-to-person crew (individual). Any logged-in player can request/accept."""
    user = get_current_user()
    my_pid = user.get('player_id')
    if not my_pid:
        return jsonify({'error': 'You need a player profile first'}), 400
    conn = get_db_connection()
    try:
        if request.method == 'GET':
            links = list(execute_query(conn, '''
                SELECT l.*, 
                    CASE WHEN l.player_a_id = %s THEN l.player_b_id ELSE l.player_a_id END AS other_id
                FROM player_crew_links l
                WHERE (l.player_a_id = %s OR l.player_b_id = %s)
                  AND l.status IN ('pending', 'accepted')
                ORDER BY l.updated_at DESC
            ''', (my_pid, my_pid, my_pid)))
            return jsonify({'links': [dict(l) for l in links], 'my_player_id': my_pid})

        data = request.get_json(silent=True) or {}
        try:
            other_id = int(data.get('player_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'player_id is required'}), 400
        if other_id == my_pid:
            return jsonify({'error': 'You cannot crew up with yourself'}), 400
        other = execute_query_one(conn, '''
            SELECT id, display_name, first_name, family_id, archived_at, purged_at
            FROM players WHERE id = %s
        ''', (other_id,))
        if not other or other.get('archived_at') or other.get('purged_at'):
            return jsonify({'error': 'Player not found'}), 404
        a, b = (my_pid, other_id) if my_pid < other_id else (other_id, my_pid)
        existing = execute_query_one(conn, '''
            SELECT * FROM player_crew_links WHERE player_a_id = %s AND player_b_id = %s
        ''', (a, b))
        if existing:
            if existing['status'] == 'accepted':
                return jsonify({'error': 'You are already personal crew with that person'}), 409
            if existing['status'] == 'pending':
                return jsonify({'error': 'A crew request is already pending'}), 409
            # Re-open declined/ended
            row = execute_query_one(conn, '''
                UPDATE player_crew_links
                SET status = 'pending', requested_by_player_id = %s,
                    responded_by_player_id = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s RETURNING *
            ''', (my_pid, existing['id']))
        else:
            row = execute_query_one(conn, '''
                INSERT INTO player_crew_links
                    (player_a_id, player_b_id, status, requested_by_player_id)
                VALUES (%s, %s, 'pending', %s) RETURNING *
            ''', (a, b, my_pid))

        me_name = execute_query_one(conn, '''
            SELECT COALESCE(display_name, first_name) AS n FROM players WHERE id = %s
        ''', (my_pid,))
        target_user = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (other_id,))
        if target_user:
            notify_user(conn, target_user['id'], 'personal_crew_request',
                        'Personal crew request',
                        f"{me_name['n']} wants to crew up with you for game nights.",
                        {
                            'crew_link_id': row['id'],
                            'url': '/my-team',
                            'actions': [
                                {'label': 'Accept', 'style': 'success', 'method': 'POST',
                                 'url': f"/api/crew-links/{row['id']}/accept"},
                                {'label': 'Decline', 'style': 'outline-secondary', 'method': 'POST',
                                 'url': f"/api/crew-links/{row['id']}/decline"},
                            ],
                        })
        conn.commit()
        return jsonify({'message': 'Crew request sent.', 'id': row['id']})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/crew-links/<int:link_id>/accept', methods=['POST'])
@login_required
def crew_link_accept(link_id):
    user = get_current_user()
    my_pid = user.get('player_id')
    if not my_pid:
        return jsonify({'error': 'You need a player profile first'}), 400
    conn = get_db_connection()
    try:
        link = execute_query_one(conn, 'SELECT * FROM player_crew_links WHERE id = %s', (link_id,))
        if not link or link['status'] != 'pending':
            return jsonify({'error': 'Request not found'}), 404
        if my_pid not in (link['player_a_id'], link['player_b_id']):
            return jsonify({'error': 'Not your request'}), 403
        if link['requested_by_player_id'] == my_pid:
            return jsonify({'error': 'You cannot accept your own request'}), 400
        execute_modify(conn, '''
            UPDATE player_crew_links
            SET status = 'accepted', responded_by_player_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (my_pid, link_id))
        requester_user = execute_query_one(conn, '''
            SELECT id FROM users WHERE player_id = %s
        ''', (link['requested_by_player_id'],))
        me_name = execute_query_one(conn, '''
            SELECT COALESCE(display_name, first_name) AS n FROM players WHERE id = %s
        ''', (my_pid,))
        if requester_user:
            notify_user(conn, requester_user['id'], 'personal_crew_accepted',
                        'Personal crew accepted',
                        f"{me_name['n']} accepted your crew request. You can pick each other for games.",
                        {'crew_link_id': link_id, 'url': '/my-team'})
        conn.commit()
        return jsonify({'message': 'You are now personal crew.', 'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/crew-links/<int:link_id>/decline', methods=['POST'])
@login_required
def crew_link_decline(link_id):
    user = get_current_user()
    my_pid = user.get('player_id')
    if not my_pid:
        return jsonify({'error': 'You need a player profile first'}), 400
    conn = get_db_connection()
    try:
        link = execute_query_one(conn, 'SELECT * FROM player_crew_links WHERE id = %s', (link_id,))
        if not link or link['status'] != 'pending':
            return jsonify({'error': 'Request not found'}), 404
        if my_pid not in (link['player_a_id'], link['player_b_id']):
            return jsonify({'error': 'Not your request'}), 403
        execute_modify(conn, '''
            UPDATE player_crew_links
            SET status = 'declined', responded_by_player_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (my_pid, link_id))
        conn.commit()
        return jsonify({'message': 'Request declined.', 'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/crew-links/<int:link_id>/end', methods=['POST'])
@login_required
def crew_link_end(link_id):
    user = get_current_user()
    my_pid = user.get('player_id')
    if not my_pid:
        return jsonify({'error': 'You need a player profile first'}), 400
    conn = get_db_connection()
    try:
        link = execute_query_one(conn, 'SELECT * FROM player_crew_links WHERE id = %s', (link_id,))
        if not link or link['status'] not in ('accepted', 'pending'):
            return jsonify({'error': 'Link not found'}), 404
        if my_pid not in (link['player_a_id'], link['player_b_id']):
            return jsonify({'error': 'Not your crew link'}), 403
        execute_modify(conn, '''
            UPDATE player_crew_links
            SET status = 'ended', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (link_id,))
        other_id = link['player_b_id'] if link['player_a_id'] == my_pid else link['player_a_id']
        other_user = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (other_id,))
        me_name = execute_query_one(conn, '''
            SELECT COALESCE(display_name, first_name) AS n FROM players WHERE id = %s
        ''', (my_pid,))
        if other_user:
            notify_user(conn, other_user['id'], 'personal_crew_ended',
                        'Personal crew ended',
                        f"{me_name['n']} ended your personal crew link.",
                        {'crew_link_id': link_id, 'url': '/my-team'})
        conn.commit()
        return jsonify({'message': 'Personal crew ended.', 'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@main.route('/api/alliances')
@login_required
def get_alliances():
    user = get_current_user()
    family_id = user.get('family_id')
    if not family_id:
        return jsonify({'alliances': [], 'pending_sent': [], 'pending_received': []})
    conn = get_db_connection()
    try:
        alliances = execute_query(conn, '''
            SELECT fa.id, fa.created_at, fa.responded_at,
                CASE WHEN fa.requesting_family_id = %s THEN tf.name ELSE rf.name END as ally_name,
                CASE WHEN fa.requesting_family_id = %s THEN tf.id ELSE rf.id END as ally_family_id
            FROM family_alliances fa
            JOIN families rf ON fa.requesting_family_id = rf.id
            JOIN families tf ON fa.target_family_id = tf.id
            WHERE fa.status = 'accepted'
              AND (fa.requesting_family_id = %s OR fa.target_family_id = %s)
            ORDER BY fa.responded_at DESC
        ''', (family_id, family_id, family_id, family_id))

        pending_sent = execute_query(conn, '''
            SELECT fa.id, fa.created_at, tf.name as target_family_name
            FROM family_alliances fa
            JOIN families tf ON fa.target_family_id = tf.id
            WHERE fa.requesting_family_id = %s AND fa.status = 'pending'
            ORDER BY fa.created_at DESC
        ''', (family_id,))

        pending_received = execute_query(conn, '''
            SELECT fa.id, fa.created_at, rf.name as requesting_family_name,
                u.first_name || ' ' || u.last_name as requested_by
            FROM family_alliances fa
            JOIN families rf ON fa.requesting_family_id = rf.id
            LEFT JOIN users u ON fa.requested_by_user_id = u.id
            WHERE fa.target_family_id = %s AND fa.status = 'pending'
            ORDER BY fa.created_at DESC
        ''', (family_id,))

        return jsonify({
            'alliances': [dict(a) for a in alliances],
            'pending_sent': [dict(p) for p in pending_sent],
            'pending_received': [dict(p) for p in pending_received],
            'can_manage': is_family_lead(conn, user, family_id),
        })
    finally:
        conn.close()

@main.route('/api/alliances/families')
@login_required
def get_available_families():
    """Get families available for alliance requests, with optional search by name/email"""
    user = get_current_user()
    family_id = user.get('family_id')
    if not family_id:
        return jsonify([])
    conn = get_db_connection()
    try:
        q = request.args.get('q', '').strip()
        if q:
            families = execute_query(conn, '''
                SELECT DISTINCT f.id, f.name,
                    (SELECT COUNT(*) FROM players p WHERE p.family_id = f.id) as player_count
                FROM families f
                LEFT JOIN users u ON u.family_id = f.id
                WHERE f.id != %s
                  AND f.id NOT IN (
                      SELECT CASE WHEN requesting_family_id = %s THEN target_family_id ELSE requesting_family_id END
                      FROM family_alliances
                      WHERE (requesting_family_id = %s OR target_family_id = %s)
                        AND status IN ('pending', 'accepted')
                  )
                  AND (f.name ILIKE %s OR u.email ILIKE %s OR u.first_name ILIKE %s OR u.last_name ILIKE %s)
                ORDER BY f.name
            ''', (family_id, family_id, family_id, family_id, f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%'))
        else:
            families = execute_query(conn, '''
                SELECT f.id, f.name,
                    (SELECT COUNT(*) FROM players p WHERE p.family_id = f.id) as player_count
                FROM families f
                WHERE f.id != %s
                  AND f.id NOT IN (
                      SELECT CASE
                          WHEN requesting_family_id = %s THEN target_family_id
                          ELSE requesting_family_id
                      END
                      FROM family_alliances
                      WHERE (requesting_family_id = %s OR target_family_id = %s)
                        AND status IN ('pending', 'accepted')
                  )
                ORDER BY f.name
            ''', (family_id, family_id, family_id, family_id))
        return jsonify([dict(f) for f in families])
    finally:
        conn.close()

@main.route('/api/alliances', methods=['POST'])
@login_required
def send_alliance_request():
    user = get_current_user()
    family_id = user.get('family_id')
    if not family_id:
        return jsonify({'error': 'You must belong to a family'}), 400

    data = request.json
    target_family_id = data.get('target_family_id')
    if not target_family_id or int(target_family_id) == family_id:
        return jsonify({'error': 'Invalid target family'}), 400

    conn = get_db_connection()
    try:
        # Alliances control who can see this family's minors, so only the
        # family lead may create them.
        if not is_family_lead(conn, user, family_id):
            return jsonify({'error': 'Only the family lead can manage crew alliances'}), 403
        existing = execute_query_one(conn, '''
            SELECT id FROM family_alliances
            WHERE (requesting_family_id = %s AND target_family_id = %s)
               OR (requesting_family_id = %s AND target_family_id = %s)
        ''', (family_id, target_family_id, target_family_id, family_id))

        if existing:
            return jsonify({'error': 'A request already exists between these families'}), 409

        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO family_alliances (requesting_family_id, target_family_id, requested_by_user_id, status)
            VALUES (%s, %s, %s, 'pending') RETURNING id
        ''', (family_id, target_family_id, user['id']))
        alliance_id = cursor.fetchone()['id']
        conn.commit()
        cursor.close()

        my_family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (family_id,))
        target_family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (target_family_id,))

        # Notify the family's actual lead (families.lead_user_id is the single
        # source of truth for leadership), not everyone with a legacy role.
        target_users = execute_query(conn, '''
            SELECT u.id, u.email, u.first_name FROM users u
            JOIN families f ON f.lead_user_id = u.id
            WHERE f.id = %s AND u.is_active = TRUE
        ''', (target_family_id,))

        for tu in target_users:
            execute_modify(conn, '''
                INSERT INTO notifications (user_id, type, title, message, data)
                VALUES (%s, 'crew_up_request', %s, %s, %s)
            ''', (
                tu['id'],
                f"Crew Up Request from {my_family['name']}!",
                f"The {my_family['name']} family wants to crew up for game nights! Accept to start playing together.",
                json.dumps({
                    'alliance_id': alliance_id,
                    'from_family': my_family['name'],
                    'url': '/my-team',
                    'actions': [
                        {'label': 'Accept', 'style': 'success', 'method': 'POST',
                         'url': f'/api/alliances/{alliance_id}/accept'},
                        {'label': 'Decline', 'style': 'outline-secondary', 'method': 'POST',
                         'url': f'/api/alliances/{alliance_id}/decline'},
                    ],
                })
            ))
            conn.commit()

            try:
                from app.email_utils import send_alliance_request_email
                send_alliance_request_email(
                    tu['email'], tu['first_name'],
                    my_family['name'], user['first_name'] + ' ' + user['last_name'])
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Alliance email failed: {e}")

        return jsonify({'success': True, 'alliance_id': alliance_id})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/alliances/<int:alliance_id>/accept', methods=['POST'])
@login_required
def accept_alliance(alliance_id):
    user = get_current_user()
    family_id = user.get('family_id')
    conn = get_db_connection()
    try:
        if not is_family_lead(conn, user, family_id):
            return jsonify({'error': 'Only the family lead can manage crew alliances'}), 403
        already = execute_query_one(conn, '''
            SELECT id FROM family_alliances
            WHERE id = %s AND status = 'accepted'
        ''', (alliance_id,))
        if already:
            return jsonify({'success': True, 'message': 'Already accepted'})

        alliance = execute_query_one(conn, '''
            SELECT fa.*, rf.name as requesting_family_name
            FROM family_alliances fa
            JOIN families rf ON fa.requesting_family_id = rf.id
            WHERE fa.id = %s AND fa.target_family_id = %s AND fa.status = 'pending'
        ''', (alliance_id, family_id))

        if not alliance:
            return jsonify({'error': 'Alliance request not found or already handled'}), 404

        execute_modify(conn, '''
            UPDATE family_alliances
            SET status = 'accepted', responded_by_user_id = %s, responded_at = NOW()
            WHERE id = %s
        ''', (user['id'], alliance_id))
        conn.commit()

        my_family = execute_query_one(conn, 'SELECT name FROM families WHERE id = %s', (family_id,))

        requesting_users = execute_query(conn, '''
            SELECT id, email, first_name FROM users
            WHERE family_id = %s AND is_active = TRUE
        ''', (alliance['requesting_family_id'],))

        for ru in requesting_users:
            execute_modify(conn, '''
                INSERT INTO notifications (user_id, type, title, message, data)
                VALUES (%s, 'crew_up_accepted', %s, %s, %s)
            ''', (
                ru['id'],
                f"The {my_family['name']} family accepted your Crew Up!",
                f"You're now game night crew with the {my_family['name']} family. Time to play!",
                json.dumps({
                    'alliance_id': alliance_id,
                    'family_name': my_family['name'],
                    'url': '/my-team',
                })
            ))
            conn.commit()

            try:
                from app.email_utils import send_alliance_accepted_email
                send_alliance_accepted_email(ru['email'], ru['first_name'], my_family['name'])
            except Exception:
                pass

        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/alliances/<int:alliance_id>/decline', methods=['POST'])
@login_required
def decline_alliance(alliance_id):
    user = get_current_user()
    family_id = user.get('family_id')
    conn = get_db_connection()
    try:
        if not is_family_lead(conn, user, family_id):
            return jsonify({'error': 'Only the family lead can manage crew alliances'}), 403
        execute_modify(conn, '''
            DELETE FROM family_alliances
            WHERE id = %s AND target_family_id = %s AND status = 'pending'
        ''', (alliance_id, family_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@main.route('/api/games/<int:game_id>/round-scores', methods=['GET'])
@login_required
@admin_required
def get_game_round_scores(game_id):
    """Get round-by-round scores for a game (for admin editing)"""
    conn = get_db_connection()
    try:
        players_and_scores = execute_query(conn, '''
            SELECT 
                p.id as player_id,
                COALESCE(p.display_name, p.first_name) as player_name,
                p.first_name,
                p.last_name,
                gs.round_number,
                gs.score
            FROM players p
            JOIN active_game_players agp ON p.id = agp.player_id
            LEFT JOIN game_scores gs ON p.id = gs.player_id AND gs.active_game_id = %s
            WHERE agp.active_game_id = %s
            ORDER BY agp.id, gs.round_number
        ''', (game_id, game_id))
        
        if not players_and_scores:
            return jsonify({'error': 'Game not found or no players'}), 404
        
        players = {}
        rounds = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
        
        for entry in players_and_scores:
            player_id = entry['player_id']
            if player_id not in players:
                players[player_id] = {
                    'player_id': player_id,
                    'player_name': entry['player_name'],
                    'first_name': entry['first_name'],
                    'last_name': entry['last_name'],
                    'scores': {}
                }
            
            if entry['round_number']:
                players[player_id]['scores'][entry['round_number']] = entry['score']
        
        players_list = []
        for player_data in players.values():
            for round_num in rounds:
                if round_num not in player_data['scores']:
                    player_data['scores'][round_num] = None
            players_list.append(player_data)
        
        return jsonify({
            'success': True,
            'players': players_list,
            'rounds': rounds
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
