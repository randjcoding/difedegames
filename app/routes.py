from flask import Blueprint, render_template, jsonify, request, g, url_for, session, redirect
from app import get_db_connection, socketio
from datetime import datetime
from flask_socketio import emit
from app.events import broadcast_score_update, broadcast_game_completed, broadcast_game_paused, broadcast_game_resumed
from app.auth import login_required, get_current_user, admin_required
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

def get_family_players(family_id, include_crew=False):
    """Get all players for a family, optionally including crew family players"""
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
            WHERE p.family_id = %s
            ORDER BY p.first_name, p.last_name
        ''', (family_id,))
        
        result = list(family_players)
        
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
                    WHERE p.family_id = %s
                    ORDER BY p.first_name, p.last_name
                ''', (cf['ally_name'], cf['ally_family_id']))
                result.extend(list(crew_players))
        
        return result
    finally:
        conn.close()

def game_page(slug, game_id):
    """Generic game page handler for any game type"""
    conn = get_db_connection()
    user = get_current_user()
    
    game_def = execute_query_one(conn, 'SELECT * FROM games WHERE id = %s', (game_id,))
    
    specific_game_id = request.args.get('game_id')
    force_new = request.args.get('new') == '1'
    
    active_game = None
    if specific_game_id and not force_new:
        active_game = execute_query_one(conn, '''
            SELECT * FROM active_games 
            WHERE id = %s AND user_id = %s AND is_complete = FALSE
        ''', (specific_game_id, user['id']))
        if active_game and active_game.get('is_paused'):
            execute_modify(conn, 'UPDATE active_games SET is_paused = FALSE WHERE id = %s', (active_game['id'],))
            conn.commit()
            active_game = execute_query_one(conn, 'SELECT * FROM active_games WHERE id = %s', (active_game['id'],))
    elif not force_new:
        active_game = execute_query_one(conn, '''
            SELECT * FROM active_games 
            WHERE game_id = %s AND user_id = %s AND is_complete = FALSE
            ORDER BY is_paused ASC, start_time DESC LIMIT 1
        ''', (game_id, user['id']))
        if active_game and active_game.get('is_paused'):
            execute_modify(conn, 'UPDATE active_games SET is_paused = FALSE WHERE id = %s', (active_game['id'],))
            conn.commit()
            active_game = execute_query_one(conn, 'SELECT * FROM active_games WHERE id = %s', (active_game['id'],))
    
    paused_games = execute_query(conn, '''
        SELECT ag.id, ag.start_time, ag.custom_game_name,
            string_agg(COALESCE(p.display_name, p.first_name), ', ' ORDER BY agp.id) as player_names,
            COUNT(DISTINCT agp.player_id) as player_count
        FROM active_games ag
        JOIN active_game_players agp ON ag.id = agp.active_game_id
        JOIN players p ON agp.player_id = p.id
        WHERE ag.game_id = %s AND ag.user_id = %s AND ag.is_complete = FALSE AND ag.is_paused = TRUE
        GROUP BY ag.id, ag.start_time, ag.custom_game_name
        ORDER BY ag.start_time DESC LIMIT 5
    ''', (game_id, user['id']))
    
    completed_games = execute_query(conn, '''
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
        WHERE ag.game_id = %s AND ag.user_id = %s AND ag.is_complete = TRUE
        GROUP BY ag.id, ag.start_time, ag.completion_time, ag.custom_game_name,
            gs_sub.winner, gs_sub.winning_score,
            gsn.game_number, gsn.family_game_number
        ORDER BY ag.start_time DESC LIMIT 5
    ''', (game_id, user['id']))
    
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
        active_games = execute_query(conn, '''
            SELECT ag.id, ag.start_time, ag.is_paused, ag.game_id, ag.scoring_direction, ag.target_score,
                COALESCE(ag.custom_game_name, g.name) as game_name, g.slug,
                string_agg(COALESCE(p.display_name, p.first_name), ', ' ORDER BY agp.id) as player_names,
                COUNT(DISTINCT agp.player_id) as player_count
            FROM active_games ag
            JOIN games g ON ag.game_id = g.id
            JOIN active_game_players agp ON ag.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            WHERE ag.user_id = %s AND ag.is_complete = FALSE
            GROUP BY ag.id, ag.start_time, ag.is_paused, ag.game_id, ag.scoring_direction, ag.target_score,
                ag.custom_game_name, g.name, g.slug
            ORDER BY ag.is_paused ASC, ag.start_time DESC
        ''', (user['id'],))
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
        players = list(execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                p.user_id, p.email as player_email,
                u.email as user_email, u.is_active, u.is_approved, u.role as user_role
            FROM players p
            LEFT JOIN users u ON p.user_id = u.id
            WHERE p.family_id = %s
            ORDER BY p.first_name, p.last_name
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

        is_lead = user.get('role') in ('family_admin', 'super_admin')

        return render_template('my_team.html',
            family=family, players=players, alliances=alliances,
            is_lead=is_lead, user=user)
    finally:
        conn.close()

@main.route('/api/team/players', methods=['POST'])
@login_required
def team_add_player():
    conn = get_db_connection()
    user = get_current_user()
    try:
        if user.get('role') not in ('family_admin', 'super_admin'):
            return jsonify({'error': 'Only family leads can add players'}), 403
        data = request.json
        family_id = user.get('family_id')
        first_name = data.get('first_name', '').strip()
        last_name = data.get('last_name', '').strip()
        display_name = data.get('display_name', '').strip()
        email = data.get('email', '').strip() or None
        if not first_name or not last_name or not display_name:
            return jsonify({'error': 'First name, last name, and display name are required'}), 400

        new_player = execute_query_one(conn, '''
            INSERT INTO players (first_name, last_name, display_name, family_id, email)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        ''', (first_name, last_name, display_name, family_id, email))
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
        if user.get('role') not in ('family_admin', 'super_admin'):
            return jsonify({'error': 'Only family leads can edit players'}), 403
        player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s AND family_id = %s', (player_id, user.get('family_id')))
        if not player:
            return jsonify({'error': 'Player not found in your family'}), 404

        data = request.json
        execute_modify(conn, '''
            UPDATE players SET first_name = %s, last_name = %s, display_name = %s,
                email = %s WHERE id = %s
        ''', (data.get('first_name', player['first_name']),
              data.get('last_name', player['last_name']),
              data.get('display_name', player.get('display_name')),
              data.get('email') or player.get('email'),
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
    conn = get_db_connection()
    user = get_current_user()
    try:
        if user.get('role') not in ('family_admin', 'super_admin'):
            return jsonify({'error': 'Only family leads can remove players'}), 403
        player = execute_query_one(conn, 'SELECT * FROM players WHERE id = %s AND family_id = %s', (player_id, user.get('family_id')))
        if not player:
            return jsonify({'error': 'Player not found in your family'}), 404
        in_game = execute_query_one(conn, '''
            SELECT COUNT(*) as c FROM active_game_players agp
            JOIN active_games ag ON agp.active_game_id = ag.id
            WHERE agp.player_id = %s AND ag.is_complete = FALSE
        ''', (player_id,))
        if in_game and in_game['c'] > 0:
            return jsonify({'error': 'Cannot remove a player who is in an active game'}), 400

        history = execute_query_one(conn, '''
            SELECT 1 FROM game_scores WHERE player_id = %s
            UNION ALL
            SELECT 1 FROM game_stats WHERE winner_id = %s
            LIMIT 1
        ''', (player_id, player_id))
        if history:
            return jsonify({'error': 'This player has recorded game history and cannot be removed. Their past games must be preserved.'}), 400

        execute_modify(conn, 'DELETE FROM active_game_players WHERE player_id = %s', (player_id,))
        execute_modify(conn, 'DELETE FROM players WHERE id = %s', (player_id,))
        conn.commit()
        return jsonify({'message': 'Player removed'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
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
        execute_modify(conn, 'UPDATE players SET family_id = %s WHERE id = %s', (target_family_id, player_id))
        conn.commit()
        return jsonify({'success': True})
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
        
        linked_user = None
        if player.get('user_id'):
            linked_user = execute_query_one(conn, '''
                SELECT id, email, first_name, last_name, role, is_active, is_verified,
                       is_approved, phone_number, address, city, state, zipcode, family_name
                FROM users WHERE id = %s
            ''', (player['user_id'],))
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
            UPDATE players SET first_name = %s, last_name = %s, display_name = %s, family_id = %s
            WHERE id = %s
        ''', (data.get('first_name', player['first_name']),
              data.get('last_name', player['last_name']),
              display_name,
              data.get('family_id', player['family_id']),
              player_id))
        
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
                  data.get('role', 'family_admin'),
                  data.get('family_id', player['family_id'])))
            execute_modify(conn, 'UPDATE players SET user_id = %s WHERE id = %s', (new_user['id'], player_id))
        
        elif data.get('update_user') and player.get('user_id'):
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
            
            if user_updates:
                user_params.append(player['user_id'])
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
            execute_modify(conn, 'UPDATE players SET family_id = %s WHERE id = %s', (target_family_id, pid))
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
            FROM users u WHERE u.family_id = %s AND u.role IN ('family_admin', 'super_admin')
            ORDER BY u.role DESC, u.created_at ASC LIMIT 1
        ''', (family_id,))
        
        members = list(execute_query(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                p.user_id
            FROM players p WHERE p.family_id = %s
            ORDER BY p.first_name, p.last_name
        ''', (family_id,)))
        
        stats = list(execute_query(conn, '''
            WITH FamilyGames AS (
                SELECT DISTINCT ag.id as game_id, g.name as game_name, g.slug
                FROM active_games ag
                JOIN active_game_players agp ON ag.id = agp.active_game_id
                JOIN players p ON agp.player_id = p.id
                JOIN games g ON ag.game_id = g.id
                WHERE p.family_id = %s AND ag.is_complete = TRUE
            )
            SELECT game_name, slug, COUNT(*) as games_played
            FROM FamilyGames GROUP BY game_name, slug ORDER BY games_played DESC
        ''', (family_id,)))
        
        top_players = list(execute_query(conn, '''
            SELECT p.id, COALESCE(p.display_name, p.first_name) as display_name,
                COUNT(DISTINCT gs.game_id) as wins,
                COUNT(DISTINCT agp.active_game_id) as total_games
            FROM players p
            JOIN active_game_players agp ON p.id = agp.player_id
            JOIN active_games ag ON agp.active_game_id = ag.id AND ag.is_complete = TRUE
            LEFT JOIN game_stats gs ON gs.game_id = ag.id AND gs.winner_id = p.id
            WHERE p.family_id = %s
            GROUP BY p.id, p.display_name
            ORDER BY wins DESC, total_games DESC
            LIMIT 10
        ''', (family_id,)))
        
        total_games = execute_query_one(conn, '''
            SELECT COUNT(DISTINCT ag.id) as cnt
            FROM active_games ag
            JOIN active_game_players agp ON ag.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            WHERE p.family_id = %s AND ag.is_complete = TRUE
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
            stats=stats, top_players=top_players,
            total_games=total_games['cnt'] if total_games else 0,
            is_own_family=is_own_family, is_allied=is_allied)
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

        active_games = execute_query(conn, '''
            SELECT ag.id, ag.start_time, ag.is_paused,
                string_agg(COALESCE(p.display_name, p.first_name), ', ' ORDER BY agp.id) as player_names,
                COUNT(DISTINCT agp.player_id) as player_count
            FROM active_games ag
            JOIN active_game_players agp ON ag.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            WHERE ag.game_id = %s AND ag.user_id = %s AND ag.is_complete = FALSE
            GROUP BY ag.id, ag.start_time, ag.is_paused
            ORDER BY ag.is_paused, ag.start_time DESC
        ''', (game_def['id'], user['id']))

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
            INSERT INTO players (first_name, last_name, display_name, user_id, family_id)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        ''', (data['first_name'], data['last_name'], display_name, user['id'], family_id))
        
        player = execute_query_one(conn, '''
            SELECT p.id, p.first_name, p.last_name,
                COALESCE(p.display_name, p.first_name) as display_name,
                FALSE as has_duplicate, FALSE as is_guest, NULL as guest_family_name
            FROM players p WHERE p.id = %s
        ''', (player['id'],))
        
        conn.commit()
        conn.close()
        return jsonify(dict(player))
    
    include_crew = request.args.get('include_crew', 'false') == 'true'
    
    family_players = execute_query(conn, '''
        SELECT p.id, p.first_name, p.last_name,
            COALESCE(p.display_name, p.first_name) as display_name,
            FALSE as is_guest, NULL as guest_family_name,
            p.family_id
        FROM players p
        WHERE p.family_id = %s
        ORDER BY p.first_name, p.last_name
    ''', (family_id,))
    
    result = [dict(p) for p in family_players]
    
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
                WHERE p.family_id = %s
                ORDER BY p.first_name, p.last_name
            ''', (cf['ally_name'], cf['ally_family_id']))
            result.extend([dict(cp) for cp in crew_players])
    
    conn.close()
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
            cursor.execute('''
                SELECT id, is_complete FROM active_games 
                WHERE id = %s AND user_id = %s
                FOR UPDATE
            ''', (game_id, user['id']))
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
        allied_ids = [r['ally_id'] for r in execute_query(conn, '''
            SELECT CASE WHEN requesting_family_id = %s THEN target_family_id
                        ELSE requesting_family_id END as ally_id
            FROM family_alliances
            WHERE status = 'accepted' AND (requesting_family_id = %s OR target_family_id = %s)
        ''', (family_id, family_id, family_id))]
        allowed_families = [family_id] + allied_ids
        
        player_check = execute_query(conn, '''
            SELECT id FROM players WHERE id = ANY(%s) AND family_id = ANY(%s)
        ''', (player_ids_int, allowed_families))
        
        if len(player_check) != len(player_ids):
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
        player_check = execute_query_one(conn, '''
            SELECT id FROM players WHERE id = %s AND user_id = %s
        ''', (player_id, user['id']))
        
        if not player_check:
            return jsonify({'error': 'Player not found or access denied'}), 403
        
        display_name = data.get('display_name') or data['first_name']
        execute_modify(conn, '''
            UPDATE players 
            SET first_name = %s, last_name = %s, display_name = %s
            WHERE id = %s AND user_id = %s
        ''', (data['first_name'], data['last_name'], display_name, player_id, user['id']))
        
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

@main.route('/api/players/<int:player_id>', methods=['DELETE'])
@login_required
def delete_player(player_id):
    conn = get_db_connection()
    user = get_current_user()
    family_id = user.get('family_id')
    try:
        if user.get('role') not in ('family_admin', 'super_admin'):
            return jsonify({'success': False, 'error': 'Only family leads can delete players'}), 403

        player_check = execute_query_one(conn, '''
            SELECT id FROM players WHERE id = %s AND family_id = %s
        ''', (player_id, family_id))
        
        if not player_check:
            return jsonify({
                'success': False,
                'error': 'Player not found or access denied'
            }), 403
        
        active_game = execute_query_one(conn, '''
            SELECT 1 FROM active_game_players agp
            JOIN active_games ag ON agp.active_game_id = ag.id
            WHERE agp.player_id = %s AND ag.is_complete = FALSE
        ''', (player_id,))
        
        if active_game:
            return jsonify({
                'success': False,
                'error': 'Cannot delete player who is in an active game'
            }), 400

        # Preserve game history: don't allow deleting a player who has recorded
        # scores or wins in completed games (it would corrupt others' game records/stats).
        history = execute_query_one(conn, '''
            SELECT 1 FROM game_scores WHERE player_id = %s
            UNION ALL
            SELECT 1 FROM game_stats WHERE winner_id = %s
            LIMIT 1
        ''', (player_id, player_id))
        if history:
            return jsonify({
                'success': False,
                'error': 'This player has recorded game history and cannot be deleted. Their past games must be preserved.'
            }), 400
        
        execute_modify(conn, 'DELETE FROM active_game_players WHERE player_id = %s', (player_id,))
        execute_modify(conn, 'DELETE FROM players WHERE id = %s AND family_id = %s', (player_id, family_id))
        
        return jsonify({'success': True})
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
        game = execute_query_one(conn, '''
            SELECT id FROM active_games 
            WHERE game_id = 1 
            AND user_id = %s
            AND is_complete = FALSE 
            AND is_paused = FALSE 
            ORDER BY start_time DESC 
            LIMIT 1
        ''', (user['id'],))
        
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
        game = execute_query_one(conn, '''
            SELECT id FROM active_games 
            WHERE game_id = 1 
            AND user_id = %s
            AND is_complete = FALSE 
            AND is_paused = FALSE 
            ORDER BY start_time DESC 
            LIMIT 1
        ''', (user['id'],))
        
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
        game = execute_query_one(conn, '''
            SELECT id FROM active_games 
            WHERE id = %s AND user_id = %s AND game_id = 1 AND is_paused = TRUE AND is_complete = FALSE
        ''', (game_id, user['id']))
        
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
        game = execute_query_one(conn, '''
            SELECT id FROM active_games 
            WHERE id = %s AND user_id = %s AND is_complete = FALSE AND is_paused = FALSE
        ''', (game_id, user['id']))
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
        game = execute_query_one(conn, '''
            SELECT id FROM active_games 
            WHERE id = %s AND user_id = %s AND is_paused = TRUE AND is_complete = FALSE
        ''', (game_id, user['id']))
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
        game = execute_query_one(conn, '''
            SELECT ag.id, ag.scoring_direction FROM active_games ag
            WHERE ag.id = %s AND ag.user_id = %s AND ag.is_complete = FALSE
        ''', (game_id, user['id']))
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

        game = execute_query_one(conn, '''
            SELECT ag.id, ag.user_id FROM active_games ag
            WHERE ag.id = %s AND ag.user_id = %s AND ag.is_complete = FALSE
        ''', (game_id, user['id']))
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

        game = execute_query_one(conn, '''
            SELECT ag.id, ag.user_id FROM active_games ag
            WHERE ag.id = %s AND ag.user_id = %s AND ag.is_complete = FALSE
        ''', (game_id, user['id']))
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

        game = execute_query_one(conn, '''
            SELECT ag.id, ag.user_id FROM active_games ag
            WHERE ag.id = %s AND ag.user_id = %s AND ag.is_complete = FALSE
        ''', (game_id, user['id']))
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
        
        if game['user_id'] != user['id'] and user.get('role') != 'super_admin':
            is_family_lead = (user.get('role') == 'family_admin'
                              and user.get('family_id') is not None
                              and user.get('family_id') == game.get('family_id'))
            if not is_family_lead:
                return jsonify({'error': 'Only game creator or family lead can delete games'}), 403
        
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
        
        active_games = list(execute_query(conn, '''
            SELECT ag.id, ag.start_time, ag.is_paused,
                string_agg(COALESCE(p.display_name, p.first_name), ', ' ORDER BY agp.id) as player_names,
                COUNT(DISTINCT agp.player_id) as player_count
            FROM active_games ag
            JOIN active_game_players agp ON ag.id = agp.active_game_id
            JOIN players p ON agp.player_id = p.id
            WHERE ag.game_id = %s AND ag.user_id = %s AND ag.is_complete = FALSE
            GROUP BY ag.id, ag.start_time, ag.is_paused
            ORDER BY ag.start_time DESC LIMIT 10
        ''', (game_def['id'], user['id'])))
        
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
        game = execute_query_one(conn, '''
            SELECT id FROM active_games 
            WHERE id = %s 
            AND user_id = %s
            AND game_id = 1 
            AND is_complete = FALSE 
        ''', (game_id, user['id']))
        
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
                        SELECT COALESCE(p2.display_name, p2.first_name)
                        FROM players p2
                        JOIN game_scores sc ON p2.id = sc.player_id
                        WHERE sc.active_game_id = ag.id AND p2.user_id = %s
                        GROUP BY p2.id
                        ORDER BY SUM(sc.score) ASC
                        LIMIT 1
                    ) as winner
                FROM active_games ag
                JOIN games g ON ag.game_id = g.id
                WHERE ag.user_id = %s
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
            WHERE p.user_id = %s
            GROUP BY gd.id, gd.game_name, gd.completion_time, gd.is_complete, gd.is_paused, gd.winner
            ORDER BY gd.is_complete DESC, gd.completion_time DESC, gd.id DESC
        ''', (user['id'], user['id'], user['id']))
        
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
        game_check = execute_query_one(conn, '''
            SELECT id FROM active_games 
            WHERE id = %s AND user_id = %s
        ''', (game_id, user['id']))
        
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
            'pending_received': [dict(p) for p in pending_received]
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

        target_users = execute_query(conn, '''
            SELECT id, email, first_name FROM users
            WHERE family_id = %s AND is_active = TRUE AND role IN ('family_admin', 'super_admin')
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
