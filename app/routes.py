from flask import Blueprint, render_template, jsonify, request, g, url_for, session, redirect
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
    """SQL fragment + params deciding which active_games a user may access:
    started by them, hosted by their home family, hosted by a directly allied
    crew family, or any game they personally played in (guest scoring)."""
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

def fetch_accessible_game(conn, game_id, user, extra_where='', extra_params=None):
    """Load an active_games row if the user may access it (family-shared)."""
    clause, params = family_access_clause(user, 'ag')
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
    if user.get('role') == 'super_admin':
        return True
    if game.get('user_id') == user.get('id'):
        return True
    fam = user.get('family_id')
    return fam is not None and game.get('family_id') == fam

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

def get_family_players(family_id, include_crew=False):
    """Roster for a family from memberships (a person can belong to several
    families), optionally including allied/crew family players as guests."""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        family_players = execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                FALSE as is_guest, NULL as guest_family_name,
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
    access_sql, access_params = family_access_clause(user, 'ag')
    
    active_game = None
    if specific_game_id and not force_new:
        active_game = fetch_accessible_game(
            conn, specific_game_id, user,
            extra_where='ag.is_complete = FALSE AND ag.game_id = %s',
            extra_params=[game_id])
        if active_game and active_game.get('is_paused'):
            execute_modify(conn, 'UPDATE active_games SET is_paused = FALSE WHERE id = %s', (active_game['id'],))
            conn.commit()
            active_game = execute_query_one(conn, 'SELECT * FROM active_games WHERE id = %s', (active_game['id'],))
    
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
    
    all_players = get_family_players(user.get('family_id'), include_crew=True)
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
        user_family_id=user.get('family_id'))

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
        access_sql, access_params = family_access_clause(user, 'ag')
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
        return render_template('dashboard.html', user=user, active_games=active_games)
    finally:
        conn.close()

@main.route('/admin')
@admin_required
def admin():
    conn = get_db_connection()
    try:
        player_count = execute_query_one(conn, 'SELECT COUNT(*) as c FROM players')['c']
        game_count = execute_query_one(conn, 'SELECT COUNT(*) as c FROM active_games')['c']
        user_count = execute_query_one(conn, 'SELECT COUNT(*) as c FROM users')['c']
        family_count = execute_query_one(conn, 'SELECT COUNT(*) as c FROM families')['c']
        families_raw = execute_query(conn, '''
            SELECT f.id, f.name,
                (SELECT COUNT(*) FROM players p WHERE p.family_id = f.id) as player_count,
                (SELECT COUNT(*) FROM users u WHERE u.family_id = f.id) as user_count
            FROM families f ORDER BY f.name
        ''')
        families = [dict(f) for f in families_raw]
        return render_template('admin.html',
            player_count=player_count, game_count=game_count,
            user_count=user_count, family_count=family_count,
            families=families)
    finally:
        conn.close()

def notify_user(conn, user_id, ntype, title, message, data=None):
    """Insert an in-app notification (best effort; caller commits)."""
    if not user_id:
        return
    execute_modify(conn, '''
        INSERT INTO notifications (user_id, type, title, message, data)
        VALUES (%s, %s, %s, %s, %s)
    ''', (user_id, ntype, title, message, json.dumps(data or {})))

def notify_family_lead(conn, family_id, ntype, title, message, data=None):
    lead = execute_query_one(conn, 'SELECT lead_user_id FROM families WHERE id = %s', (family_id,))
    if lead and lead.get('lead_user_id'):
        notify_user(conn, lead['lead_user_id'], ntype, title, message, data)

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
                p.email as player_email,
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

        return render_template('my_team.html',
            family=family, players=players, alliances=alliances,
            pending=pending, my_memberships=my_memberships,
            joinable_families=joinable_families, claimable=claimable,
            lead_candidates=lead_candidates,
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
            SELECT p.* FROM players p
            JOIN player_family_memberships m ON m.player_id = p.id
            WHERE p.id = %s AND m.family_id = %s
        ''', (player_id, family_id))
        if not member:
            return jsonify({'error': 'Player not found in your family'}), 404

        data = request.json
        execute_modify(conn, '''
            UPDATE players SET first_name = %s, last_name = %s, display_name = %s,
                email = %s WHERE id = %s
        ''', (data.get('first_name', member['first_name']),
              data.get('last_name', member['last_name']),
              data.get('display_name', member.get('display_name')),
              data.get('email') or member.get('email'),
              player_id))
        conn.commit()
        return jsonify({'message': 'Player updated'})
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
    """'DiFede (led by Joe D.)' - lead shown by public name, never email."""
    name = row.get('family_name') or row.get('name') or 'Unknown family'
    lead = None
    if row.get('lead_first_name'):
        lead = public_person_name({
            'first_name': row.get('lead_first_name'),
            'last_name': row.get('lead_last_name'),
            'display_name': row.get('lead_display_name'),
            'show_full_last_name': row.get('lead_show_full_last_name'),
        })
    return f'{name} (led by {lead})' if lead else name

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
    if len(q) < 2:
        return jsonify({'results': [], 'page': 1})
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    conn = get_db_connection()
    try:
        minor_families = _visible_family_ids_for_minors(conn, user) or [-1]
        like = f'%{q}%'
        rows = execute_query(conn, f'''
            SELECT p.id, p.first_name, p.last_name, p.display_name,
                p.show_full_last_name, p.is_minor, p.family_id,
                f.name AS family_name,
                EXISTS (SELECT 1 FROM users u WHERE u.player_id = p.id) AS is_claimed,
                {_LEAD_COLS}
            FROM players p
            JOIN families f ON f.id = p.family_id
            {_LEAD_JOIN}
            WHERE p.archived_at IS NULL AND p.purged_at IS NULL
              AND p.is_discoverable = TRUE
              AND f.archived_at IS NULL
              AND (p.first_name ILIKE %s OR p.last_name ILIKE %s
                   OR COALESCE(p.display_name, '') ILIKE %s)
              AND (p.is_minor = FALSE OR p.family_id = ANY(%s))
            ORDER BY p.first_name, p.last_name
            LIMIT 25 OFFSET %s
        ''', (like, like, like, minor_families, (page - 1) * 25))

        my_family = user.get('family_id')
        return jsonify({'page': page, 'results': [{
            'player_id': r['id'],
            'name': public_person_name(r),
            'family_id': r['family_id'],
            'family_name': r['family_name'],
            'family_label': _family_label(r),
            'is_claimed': bool(r['is_claimed']),
            'in_my_family': r['family_id'] == my_family,
        } for r in rows]})
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
              AND (lower(p.email) = %s
                   OR p.id = (SELECT player_id FROM users u WHERE lower(u.email) = %s))
            LIMIT 1
        ''', (email, email))

        if not row or not row['is_discoverable'] or row['family_archived']:
            return jsonify({'found': False})
        if row['is_minor'] and row['family_id'] not in _visible_family_ids_for_minors(conn, user):
            return jsonify({'found': False})
        return jsonify({
            'found': True,
            'player_id': row['id'],
            'name': public_person_name(row),
            'family_id': row['family_id'],
            'family_label': _family_label(row),
        })
    finally:
        conn.close()

@main.route('/api/directory/families')
@login_required
def directory_families():
    user = get_current_user()
    if _directory_rate_limited(user['id']):
        return jsonify({'error': 'Too many searches. Please wait a minute.'}), 429
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'results': [], 'page': 1})
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    conn = get_db_connection()
    try:
        rows = execute_query(conn, f'''
            SELECT f.id, f.name, f.slug, f.show_roster,
                (SELECT COUNT(*) FROM player_family_memberships m
                  JOIN players mp ON mp.id = m.player_id
                  WHERE m.family_id = f.id AND m.status = 'active'
                    AND mp.archived_at IS NULL) AS member_count,
                {_LEAD_COLS}
            FROM families f
            {_LEAD_JOIN}
            WHERE f.archived_at IS NULL AND f.is_discoverable = TRUE
              AND f.name ILIKE %s
            ORDER BY f.name
            LIMIT 25 OFFSET %s
        ''', (f'%{q}%', (page - 1) * 25))

        my_family = user.get('family_id')
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
            results.append({
                'family_id': r['id'],
                'name': r['name'],
                'slug': r['slug'],
                'label': _family_label(r),
                'lead_name': lead_name,
                'member_count': r['member_count'],
                'roster_visible': bool(r['show_roster']),
                'is_my_family': r['id'] == my_family,
            })
        return jsonify({'page': page, 'results': results})
    finally:
        conn.close()

@main.route('/api/directory/families/<int:family_id>/roster')
@login_required
def directory_family_roster(family_id):
    """Roster preview so someone can confirm they found the right family."""
    user = get_current_user()
    if _directory_rate_limited(user['id']):
        return jsonify({'error': 'Too many searches. Please wait a minute.'}), 429
    conn = get_db_connection()
    try:
        family = execute_query_one(conn, '''
            SELECT id, name, show_roster FROM families
            WHERE id = %s AND archived_at IS NULL AND is_discoverable = TRUE
        ''', (family_id,))
        if not family:
            return jsonify({'error': 'Family not found'}), 404

        member_count = execute_query_one(conn, '''
            SELECT COUNT(*) AS n FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
        ''', (family_id,))['n']

        if not family['show_roster'] and not is_family_lead(conn, user, family_id) \
                and user.get('family_id') != family_id:
            return jsonify({'family_id': family_id, 'name': family['name'],
                            'roster_visible': False, 'member_count': member_count})

        can_see_minors = family_id in _visible_family_ids_for_minors(conn, user) \
            or user.get('role') == 'super_admin'
        rows = execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name, p.display_name,
                p.show_full_last_name, p.is_minor,
                EXISTS (SELECT 1 FROM users u WHERE u.player_id = p.id) AS is_claimed
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            WHERE m.family_id = %s AND m.status = 'active'
              AND p.archived_at IS NULL AND p.is_discoverable = TRUE
            ORDER BY p.first_name, p.last_name
        ''', (family_id,))

        members = [{
            'player_id': r['id'],
            'name': public_person_name(r),
            'is_claimed': bool(r['is_claimed']),
            'is_minor': bool(r['is_minor']),
        } for r in rows if can_see_minors or not r['is_minor']]

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
                    p.first_name, p.last_name, f.name AS family_name
                FROM players p LEFT JOIN families f ON f.id = p.family_id WHERE p.id = %s
            ''', (pid,))
            if not p:
                return None
            d = dict(p)
            d['history'] = _player_history_counts(conn, pid)
            return d
        keep, dup = info(keep_id), info(dup_id)
        if not keep or not dup:
            return jsonify({'error': 'Player not found'}), 404
        return jsonify({'keep': keep, 'dup': dup})
    finally:
        conn.close()

@main.route('/api/admin/merge-players', methods=['POST'])
@login_required
def merge_players():
    """Merge a duplicate person into a canonical one. All history (scores,
    wins, games, memberships) is repointed to the kept player, then the
    duplicate row is deleted. This is the ONLY place a players row may be
    hard-deleted, and only after every reference has been repointed."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        data = request.json or {}
        keep_id = data.get('keep_id')
        dup_id = data.get('dup_id')
        if not keep_id or not dup_id:
            return jsonify({'error': 'keep_id and dup_id are required'}), 400
        if keep_id == dup_id:
            return jsonify({'error': 'Cannot merge a player into itself'}), 400
        if not _can_merge(conn, user, keep_id, dup_id):
            return jsonify({'error': 'Only a super admin, or the lead of both players\' home family, can merge'}), 403
        keep = execute_query_one(conn, 'SELECT id FROM players WHERE id = %s', (keep_id,))
        dup = execute_query_one(conn, 'SELECT id, first_name, last_name FROM players WHERE id = %s', (dup_id,))
        if not keep or not dup:
            return jsonify({'error': 'Player not found'}), 404

        # Repoint score tables, dropping duplicate rows that would collide on
        # the (game, player, round) unique keys.
        for tbl in ('game_scores', 'five_crowns_scores'):
            execute_modify(conn, f'''
                DELETE FROM {tbl} a WHERE a.player_id = %s AND EXISTS (
                    SELECT 1 FROM {tbl} b
                    WHERE b.active_game_id = a.active_game_id
                      AND b.round_number = a.round_number AND b.player_id = %s)
            ''', (dup_id, keep_id))
            execute_modify(conn, f'UPDATE {tbl} SET player_id = %s WHERE player_id = %s', (keep_id, dup_id))

        # Roster rows: avoid two entries for the same game.
        execute_modify(conn, '''
            DELETE FROM active_game_players a WHERE a.player_id = %s AND EXISTS (
                SELECT 1 FROM active_game_players b
                WHERE b.active_game_id = a.active_game_id AND b.player_id = %s)
        ''', (dup_id, keep_id))
        execute_modify(conn, 'UPDATE active_game_players SET player_id = %s WHERE player_id = %s', (keep_id, dup_id))

        execute_modify(conn, 'UPDATE game_stats SET winner_id = %s WHERE winner_id = %s', (keep_id, dup_id))

        # Memberships: bring over families the kept player isn't already in.
        execute_modify(conn, '''
            INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
            SELECT %s, d.family_id, FALSE, d.status, 'member'
            FROM player_family_memberships d
            WHERE d.player_id = %s AND NOT EXISTS (
                SELECT 1 FROM player_family_memberships k
                WHERE k.player_id = %s AND k.family_id = d.family_id)
        ''', (keep_id, dup_id, keep_id))
        execute_modify(conn, 'DELETE FROM player_family_memberships WHERE player_id = %s', (dup_id,))

        # Guarantee exactly one primary for the kept player.
        has_primary = execute_query_one(conn, '''
            SELECT 1 FROM player_family_memberships WHERE player_id = %s AND is_primary
        ''', (keep_id,))
        if not has_primary:
            execute_modify(conn, '''
                UPDATE player_family_memberships SET is_primary = TRUE
                WHERE id = (SELECT id FROM player_family_memberships
                            WHERE player_id = %s ORDER BY joined_at ASC, id ASC LIMIT 1)
            ''', (keep_id,))

        # Account identity: move an owning account if the kept player has none.
        execute_modify(conn, '''
            UPDATE users SET player_id = %s
            WHERE player_id = %s AND NOT EXISTS (SELECT 1 FROM users WHERE player_id = %s)
        ''', (keep_id, dup_id, keep_id))
        execute_modify(conn, 'UPDATE users SET player_id = NULL WHERE player_id = %s', (dup_id,))

        execute_modify(conn, 'DELETE FROM players WHERE id = %s', (dup_id,))
        audit(conn, user['id'], 'players_merged', 'players', keep_id,
              old={'duplicate_id': dup_id,
                   'duplicate_name': f"{dup.get('first_name','')} {dup.get('last_name','')}".strip()},
              new={'kept_id': keep_id})
        conn.commit()
        return jsonify({'success': True, 'message': 'Players merged'})
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
        data = request.json
        player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found'}), 404
        
        display_name = data.get('display_name') or data.get('first_name', player['first_name'])
        execute_modify(conn, '''
            UPDATE players SET first_name = %s, last_name = %s, display_name = %s
            WHERE id = %s
        ''', (data.get('first_name', player['first_name']),
              data.get('last_name', player['last_name']),
              display_name,
              player_id))
        new_family_id = data.get('family_id')
        if new_family_id and int(new_family_id) != player['family_id']:
            set_player_home_family(conn, player_id, int(new_family_id))
        
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
                  # super_admin can never be granted here; only the site owner
                  # can change roles, via /auth/admin/users/<id>/set-role.
                  'family_admin',
                  data.get('family_id', player['family_id'])))
            # users.player_id is the identity link; created_by_user_id is provenance.
            execute_modify(conn, 'UPDATE users SET player_id = %s WHERE id = %s', (player_id, new_user['id']))
            execute_modify(conn, 'UPDATE players SET created_by_user_id = %s, email = %s WHERE id = %s',
                           (new_user['id'], data['email'], player_id))

        elif data.get('update_user'):
            linked_user = execute_query_one(conn, 'SELECT id FROM users WHERE player_id = %s', (player_id,))
            user_updates = []
            user_params = []
            
            for field in ['email', 'first_name', 'last_name', 'phone_number', 'address', 'city', 'state', 'zipcode', 'role']:
                if field in data.get('user_data', {}):
                    user_updates.append(f'{field} = %s')
                    user_params.append(data['user_data'][field])
            
            if 'is_active' in data.get('user_data', {}):
                user_updates.append('is_active = %s')
                user_params.append(data['user_data']['is_active'])
            
            if 'is_approved' in data.get('user_data', {}):
                user_updates.append('is_approved = %s')
                user_params.append(data['user_data']['is_approved'])
            
            if user_updates and linked_user:
                user_params.append(linked_user['id'])
                execute_modify(conn, f"UPDATE users SET {', '.join(user_updates)} WHERE id = %s", user_params)
        
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
        if not family:
            return redirect(url_for('main.dashboard'))
        
        leader = execute_query_one(conn, '''
            SELECT u.id, u.email, u.first_name, u.last_name, u.role
            FROM users u
            JOIN families f ON f.lead_user_id = u.id
            WHERE f.id = %s
        ''', (family_id,))
        
        members = list(execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                m.is_primary AS is_home
            FROM player_family_memberships m
            JOIN players p ON p.id = m.player_id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
            ORDER BY m.is_primary DESC, p.first_name, p.last_name
        ''', (family_id,)))
        
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
        
        is_own_family = user.get('family_id') == family_id
        is_allied = False
        if not is_own_family:
            uf = user.get('family_id')
            ally = execute_query_one(conn, '''
                SELECT id FROM family_alliances
                WHERE status = 'accepted'
                AND ((requesting_family_id = %s AND target_family_id = %s)
                  OR (requesting_family_id = %s AND target_family_id = %s))
            ''', (uf, family_id, family_id, uf))
            is_allied = ally is not None
        
        return render_template('family_page.html',
            family=family, leader=leader, members=members,
            stats=stats, family_top=family_top, lifetime_top=lifetime_top,
            total_games=total_games['cnt'] if total_games else 0,
            is_own_family=is_own_family, is_allied=is_allied)
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
            SELECT g.name AS game_name, ag.completion_time,
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

        access_sql, access_params = family_access_clause(user, 'ag')
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

        return render_template('game_landing.html',
            game=game_def,
            rules=rules,
            active_games=active_games,
            total_games=total_games['c'],
            family_games=family_games['c'],
            game_url=game_url,
            slug=slug)
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
    result = [dict(p) for p in get_family_players(family_id, include_crew=include_crew)]
    return jsonify(result)

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
            access_sql, access_params = family_access_clause(user, 'ag')
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
    for everyone else's records, but the identity is gone forever. Requires
    the player to be archived first, as a two-step safety."""
    conn = get_db_connection()
    user = get_current_user()
    try:
        player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s AND purged_at IS NULL', (player_id,))
        if not player:
            return jsonify({'error': 'Player not found or already purged'}), 404
        if not player.get('archived_at'):
            return jsonify({'error': 'Archive the player first; purge is the second, irreversible step'}), 400
        confirm = (request.get_json(silent=True) or {}).get('confirm')
        if confirm != 'PURGE':
            return jsonify({'error': 'Send {"confirm": "PURGE"} to confirm this irreversible action'}), 400

        execute_modify(conn, 'UPDATE users SET player_id = NULL WHERE player_id = %s', (player_id,))
        execute_modify(conn, '''
            UPDATE players SET first_name = 'Deleted', last_name = 'Player',
                display_name = 'Deleted Player', email = NULL, email_verified = FALSE,
                is_discoverable = FALSE, purged_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (player_id,))
        execute_modify(conn, "UPDATE invitations SET status = 'revoked' WHERE player_id = %s AND status = 'sent'", (player_id,))
        audit(conn, user['id'], 'player_purged', 'players', player_id,
              old={'first_name': player['first_name'], 'last_name': player['last_name']})
        conn.commit()
        return jsonify({'message': 'Player permanently anonymized. Game records remain for other players.'})
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
            access_sql, access_params = family_access_clause(user, 'ag')
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
                        SUM(gs.score) as total_score,
                        RANK() OVER (ORDER BY SUM(gs.score) ASC) as rank
                    FROM players p
                    JOIN game_scores gs ON p.id = gs.player_id
                    LEFT JOIN DisplayNameCounts dc ON COALESCE(p.display_name, p.first_name) = dc.display_name
                    WHERE gs.active_game_id = %s
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
            access_sql, access_params = family_access_clause(user, 'ag')
            game = execute_query_one(conn, f'''
                SELECT ag.id FROM active_games ag
                WHERE ag.game_id = 1 AND {access_sql}
                AND ag.is_complete = FALSE AND ag.is_paused = FALSE
                ORDER BY ag.start_time DESC LIMIT 1
            ''', tuple(access_params))
        
        if game:
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
        
        access_sql, access_params = family_access_clause(user, 'ag')
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

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@main.route('/leaderboard')
@login_required
def leaderboard():
    conn = get_db_connection()
    try:
        game_type = request.args.get('game_type', type=int)
        
        available_games_list = execute_query(conn, '''
            SELECT id, name, slug FROM games WHERE is_variant_group = FALSE ORDER BY COALESCE(display_order, id * 10), id
        ''')
        
        sub_game = request.args.get('sub_game')
        BASIC_GAME_ID = 7

        # Score columns are only meaningful for a single game (mixed scoring
        # directions make aggregate avg/best scores meaningless across games).
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
                WHERE ag.is_complete = TRUE ''' + gt_filter + '''
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
        ''', gt_params or None)

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
                            scoring_direction=scoring_direction)
    finally:
        conn.close()

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
                json.dumps({'alliance_id': alliance_id, 'from_family': my_family['name']})
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
                json.dumps({'alliance_id': alliance_id, 'family_name': my_family['name']})
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
