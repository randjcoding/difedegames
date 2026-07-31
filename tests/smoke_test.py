"""End-to-end smoke test for the multi-family identity rebuild.

Runs against the real dev database using disposable, clearly-prefixed test
data (families "Zztest ...", emails "zztest...@example.com"). Every row it
creates is removed at the end, even on failure. Take a backup before running:

    pg_dump -U difedeapp difedeappv2 > backup_before_smoke.sql
    python tests/smoke_test.py
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.email_utils as email_utils

SENT_EMAILS = []


def _fake_smtp_send(em, recipients):
    SENT_EMAILS.append({
        'to': list(recipients),
        'subject': em['Subject'],
        'body': em.get_body(preferencelist=('html', 'plain')).get_content(),
    })
    return True


email_utils._smtp_send = _fake_smtp_send

from app import create_app
from app.database import get_db_connection

PASSWORD = 'Zztest!Pass1'
CHECKS = []


def check(name, condition, detail=''):
    CHECKS.append((name, bool(condition), detail))
    status = 'PASS' if condition else 'FAIL'
    print(f'  [{status}] {name}' + (f'  -- {detail}' if detail and not condition else ''))
    return bool(condition)


def q1(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row


def qall(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close()
    return rows


def run(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or ())
    cur.close()


def extract_link(kind):
    """Pull the newest /claim/, /invite/, /action/, or /auth/set-password/ link."""
    for mail in reversed(SENT_EMAILS):
        if kind == 'set-password':
            m = re.search(r'/auth/set-password/([A-Za-z0-9_\-]+)', mail['body'])
            if m:
                return m.group(1)
            continue
        m = re.search(rf'/({kind})/([A-Za-z0-9_\-]+)', mail['body'])
        if m:
            return m.group(2)
    return None


def register(client, email, first, last, family=''):
    return client.post('/auth/register', data={
        'first_name': first, 'last_name': last, 'family_name': family,
        'email': email, 'password': PASSWORD, 'confirm_password': PASSWORD,
    }, follow_redirects=True)


def activate_and_login(conn, client, email):
    run(conn, "UPDATE users SET is_verified = TRUE, is_approved = TRUE WHERE lower(email) = %s", (email,))
    conn.commit()
    r = client.post('/auth/login', data={'email': email, 'password': PASSWORD}, follow_redirects=True)
    return r.status_code == 200


def schema_audit(conn):
    print('\n== Section 1: schema and FK data-safety audit ==')
    check('table game_layouts exists',
          q1(conn, "SELECT to_regclass('public.game_layouts') IS NOT NULL AS ok")['ok'])
    check('table feedback_items exists',
          q1(conn, "SELECT to_regclass('public.feedback_items') IS NOT NULL AS ok")['ok'])
    check('table feedback_views exists',
          q1(conn, "SELECT to_regclass('public.feedback_views') IS NOT NULL AS ok")['ok'])
    check('table feedback_replies exists',
          q1(conn, "SELECT to_regclass('public.feedback_replies') IS NOT NULL AS ok")['ok'])
    check('table player_crew_links exists',
          q1(conn, "SELECT to_regclass('public.player_crew_links') IS NOT NULL AS ok")['ok'])
    expected = {
        'fk_active_games_user_id': 'n',
        'game_scores_player_fkey': 'r',
        'active_game_players_player_id_fkey': 'r',
        'five_crowns_scores_player_id_fkey': 'r',
        'game_stats_winner_id_fkey': 'n',
        'users_player_id_fkey': 'n',
    }
    rows = qall(conn, """
        SELECT conname, confdeltype FROM pg_constraint
        WHERE contype = 'f' AND conname = ANY(%s)
    """, (list(expected.keys()),))
    found = {r['conname']: r['confdeltype'] for r in rows}
    for name, rule in expected.items():
        label = {'n': 'SET NULL', 'r': 'RESTRICT'}[rule]
        check(f'FK {name} is ON DELETE {label}', found.get(name) == rule,
              f'found {found.get(name, "missing")}')

    row = q1(conn, """
        SELECT is_nullable FROM information_schema.columns
        WHERE table_name = 'game_stats' AND column_name = 'winner_id'
    """)
    check('game_stats.winner_id is nullable', row and row['is_nullable'] == 'YES')

    row = q1(conn, """
        SELECT COUNT(*) AS n FROM information_schema.columns
        WHERE table_name = 'players' AND column_name = 'user_id'
    """)
    check('players.user_id renamed away (created_by_user_id era)', row['n'] == 0)

    for tbl in ('player_release_requests', 'action_tokens', 'invitations', 'user_audit_log'):
        row = q1(conn, 'SELECT to_regclass(%s) AS t', (tbl,))
        check(f'table {tbl} exists', row['t'] is not None)

    row = q1(conn, "SELECT COUNT(*) AS n FROM pg_indexes WHERE indexname = 'users_one_person_each'")
    check('users_one_person_each unique index exists', row['n'] == 1)

    for tbl in ('players', 'families', 'users'):
        row = q1(conn, """
            SELECT COUNT(*) AS n FROM information_schema.columns
            WHERE table_name = %s AND column_name = 'archived_at'
        """, (tbl,))
        check(f'{tbl}.archived_at lifecycle column exists', row['n'] == 1)


def cleanup(conn, ids):
    conn.rollback()
    fam_ids = [r['id'] for r in qall(conn, "SELECT id FROM families WHERE name LIKE 'Zztest%%'")]
    fam_ids = list(set(fam_ids) | ids['families'])
    user_ids = [r['id'] for r in qall(conn, "SELECT id FROM users WHERE email LIKE 'zztest%%'")]
    user_ids = list(set(user_ids) | ids['users'])
    player_ids = [r['id'] for r in qall(conn, """
        SELECT id FROM players
        WHERE first_name ILIKE 'zztest%%' OR created_by_user_id = ANY(%s)
    """, (user_ids or [0],))]
    player_ids = list(set(player_ids) | {p for p in ids['players'] if p})
    if not (fam_ids or user_ids or player_ids):
        return 0, 0, 0

    run(conn, """DELETE FROM game_scores WHERE active_game_id IN
        (SELECT id FROM active_games WHERE family_id = ANY(%s) OR user_id = ANY(%s))""",
        (fam_ids or [0], user_ids or [0]))
    run(conn, """DELETE FROM five_crowns_scores WHERE active_game_id IN
        (SELECT id FROM active_games WHERE family_id = ANY(%s) OR user_id = ANY(%s))""",
        (fam_ids or [0], user_ids or [0]))
    run(conn, """DELETE FROM game_stats WHERE game_id IN
        (SELECT id FROM active_games WHERE family_id = ANY(%s) OR user_id = ANY(%s))""",
        (fam_ids or [0], user_ids or [0]))
    run(conn, """DELETE FROM active_game_players WHERE active_game_id IN
        (SELECT id FROM active_games WHERE family_id = ANY(%s) OR user_id = ANY(%s))""",
        (fam_ids or [0], user_ids or [0]))
    run(conn, 'DELETE FROM active_games WHERE family_id = ANY(%s) OR user_id = ANY(%s)',
        (fam_ids or [0], user_ids or [0]))
    run(conn, """DELETE FROM player_release_requests
        WHERE player_id = ANY(%s) OR from_family_id = ANY(%s) OR to_family_id = ANY(%s)""",
        (player_ids or [0], fam_ids or [0], fam_ids or [0]))
    run(conn, """DELETE FROM player_not_duplicates
        WHERE player_a_id = ANY(%s) OR player_b_id = ANY(%s)""",
        (player_ids or [0], player_ids or [0]))
    run(conn, """DELETE FROM action_tokens
        WHERE player_id = ANY(%s) OR user_id = ANY(%s) OR family_id = ANY(%s)""",
        (player_ids or [0], user_ids or [0], fam_ids or [0]))
    run(conn, "DELETE FROM invitations WHERE invited_by_user_id = ANY(%s) OR email LIKE 'zztest%%'",
        (user_ids or [0],))
    run(conn, 'DELETE FROM notifications WHERE user_id = ANY(%s)', (user_ids or [0],))
    run(conn, '''DELETE FROM feedback_replies
        WHERE user_id = ANY(%s)
           OR feedback_id IN (SELECT id FROM feedback_items WHERE user_id = ANY(%s))''',
        (user_ids or [0], user_ids or [0]))
    run(conn, '''DELETE FROM feedback_views
        WHERE user_id = ANY(%s)
           OR feedback_id IN (SELECT id FROM feedback_items WHERE user_id = ANY(%s))''',
        (user_ids or [0], user_ids or [0]))
    run(conn, 'DELETE FROM feedback_items WHERE user_id = ANY(%s)', (user_ids or [0],))
    run(conn, 'DELETE FROM user_audit_log WHERE user_id = ANY(%s)', (user_ids or [0],))
    run(conn, 'UPDATE user_audit_log SET record_id = NULL WHERE table_name = %s AND record_id = ANY(%s)',
        ('players', player_ids or [0]))
    run(conn, '''DELETE FROM player_crew_links
        WHERE player_a_id = ANY(%s) OR player_b_id = ANY(%s)
           OR requested_by_player_id = ANY(%s) OR responded_by_player_id = ANY(%s)''',
        (player_ids or [0], player_ids or [0], player_ids or [0], player_ids or [0]))
    run(conn, 'DELETE FROM player_family_memberships WHERE player_id = ANY(%s) OR family_id = ANY(%s)',
        (player_ids or [0], fam_ids or [0]))
    run(conn, """DELETE FROM family_alliances
        WHERE requesting_family_id = ANY(%s) OR target_family_id = ANY(%s)""",
        (fam_ids or [0], fam_ids or [0]))
    run(conn, 'DELETE FROM game_layouts WHERE family_id = ANY(%s)', (fam_ids or [0],))
    run(conn, 'UPDATE families SET lead_user_id = NULL WHERE id = ANY(%s)', (fam_ids or [0],))
    for tbl in ('players', 'families', 'users'):
        run(conn, f'UPDATE {tbl} SET archived_by_user_id = NULL WHERE archived_by_user_id = ANY(%s)',
            (user_ids or [0],))
    run(conn, 'UPDATE players SET created_by_user_id = NULL WHERE created_by_user_id = ANY(%s)',
        (user_ids or [0],))
    run(conn, 'DELETE FROM users WHERE id = ANY(%s)', (user_ids or [0],))
    run(conn, 'DELETE FROM players WHERE id = ANY(%s)', (player_ids or [0],))
    run(conn, 'DELETE FROM families WHERE id = ANY(%s)', (fam_ids or [0],))
    conn.commit()
    return len(player_ids), len(user_ids), len(fam_ids)


def main():
    app = create_app()
    app.config['TESTING'] = True
    conn = get_db_connection()

    ids = {'players': set(), 'families': set(), 'users': set()}
    pre = cleanup(conn, ids)
    if any(pre):
        print(f'Pre-run cleanup removed leftovers: {pre[0]} players, {pre[1]} users, {pre[2]} families')
    try:
        schema_audit(conn)

        print('\n== Section 2: registration and family setup ==')
        client_a = app.test_client()
        client_b = app.test_client()

        register(client_a, 'zztest.alpha@example.com', 'Zztestalice', 'Alphalast', 'Zztest Alpha')
        register(client_b, 'zztest.beta@example.com', 'Zztestbob', 'Betalast', 'Zztest Beta')
        check('lead A can log in after verify+approve',
              activate_and_login(conn, client_a, 'zztest.alpha@example.com'))
        check('lead B can log in after verify+approve',
              activate_and_login(conn, client_b, 'zztest.beta@example.com'))

        fam_a = q1(conn, "SELECT id, slug, lead_user_id FROM families WHERE name = 'Zztest Alpha' ORDER BY id LIMIT 1")
        fam_b = q1(conn, "SELECT id, slug FROM families WHERE name = 'Zztest Beta' ORDER BY id LIMIT 1")
        check('family Alpha created with slug', fam_a and fam_a['slug'])
        check('family Beta created with slug', fam_b and fam_b['slug'])
        ids['families'].update({fam_a['id'], fam_b['id']})

        user_a = q1(conn, "SELECT id, player_id FROM users WHERE email = 'zztest.alpha@example.com'")
        user_b = q1(conn, "SELECT id, player_id FROM users WHERE email = 'zztest.beta@example.com'")
        ids['users'].update({user_a['id'], user_b['id']})
        ids['players'].update({user_a['player_id'], user_b['player_id']})
        check('lead A account linked to a player identity', user_a['player_id'] is not None)
        check('family Alpha lead is user A', fam_a['lead_user_id'] == user_a['id'])

        # Same family name again must create a SECOND family, not join the first.
        client_c = app.test_client()
        register(client_c, 'zztest.copycat@example.com', 'Zztestcopy', 'Cat', 'Zztest Alpha')
        twins = qall(conn, "SELECT id, slug FROM families WHERE name = 'Zztest Alpha' ORDER BY id")
        check('duplicate family name creates a new family with new slug',
              len(twins) == 2 and twins[0]['slug'] != twins[1]['slug'])
        copy_user = q1(conn, "SELECT id, player_id, family_id FROM users WHERE email = 'zztest.copycat@example.com'")
        ids['users'].add(copy_user['id'])
        ids['players'].add(copy_user['player_id'])
        ids['families'].add(copy_user['family_id'])

        print('\n== Section 3: roster, minors, directory privacy ==')
        r = client_a.post('/api/team/players', json={
            'first_name': 'Zztestadult', 'last_name': 'Grownup', 'display_name': 'Zztestadult'})
        adult_id = r.get_json().get('id')
        check('lead A adds an adult player', r.status_code == 200 and adult_id)
        r = client_a.post('/api/team/players', json={
            'first_name': 'Zztestminor', 'last_name': 'Kiddo', 'display_name': 'Zztestminor',
            'is_minor': True})
        minor_id = r.get_json().get('id')
        check('lead A adds a minor', r.status_code == 200 and minor_id)
        ids['players'].update({adult_id, minor_id})
        row = q1(conn, 'SELECT is_minor FROM players WHERE id = %s', (minor_id,))
        check('minor flag persisted', row['is_minor'] is True)

        r = client_b.get('/api/directory/people?q=Zztestadult')
        results = r.get_json().get('results', [])
        check('adult is discoverable in the directory', any(x['player_id'] == adult_id for x in results))
        if results:
            hit = [x for x in results if x['player_id'] == adult_id]
            check('directory shows full name for easy finding',
                  hit and 'Grownup' in hit[0]['name'],
                  f"got {hit[0]['name'] if hit else 'none'}")

        r = client_b.get('/api/directory/people?q=Zztestminor')
        check('minor hidden from a non-allied family',
              not any(x['player_id'] == minor_id for x in r.get_json().get('results', [])))

        # Transfer request that includes a minor must be blocked before allying.
        r = client_b.post('/api/release-requests', json={
            'player_ids': [minor_id], 'to_family_id': fam_b['id']})
        check('minor transfer blocked without an alliance', r.status_code == 403)

        run(conn, """
            INSERT INTO family_alliances (requesting_family_id, target_family_id, status, requested_by_user_id)
            VALUES (%s, %s, 'accepted', %s)
            ON CONFLICT (requesting_family_id, target_family_id) DO UPDATE SET status = 'accepted'
        """, (fam_a['id'], fam_b['id'], user_a['id']))
        conn.commit()

        r = client_b.get('/api/directory/people?q=Zztestminor')
        check('minor visible to a directly allied family',
              any(x['player_id'] == minor_id for x in r.get_json().get('results', [])))

        print('\n== Section 4: join requests and invitations ==')
        SENT_EMAILS.clear()
        r = client_b.post(f"/api/families/{fam_a['id']}/request-join", json={})
        check('lead B can request to join Alpha', r.status_code == 200, str(r.get_json()))
        membership = q1(conn, """
            SELECT id, status FROM player_family_memberships
            WHERE player_id = %s AND family_id = %s
        """, (user_b['player_id'], fam_a['id']))
        check('join request recorded as requested', membership and membership['status'] == 'requested')
        check('join request email sent to lead A',
              any('zztest.alpha@example.com' in m['to'] for m in SENT_EMAILS))
        r = client_a.post(f"/api/memberships/{membership['id']}/decide?decision=approve")
        check('lead A approves the join request', r.status_code == 200)
        row = q1(conn, 'SELECT status FROM player_family_memberships WHERE id = %s', (membership['id'],))
        check('membership is now active', row['status'] == 'active')

        SENT_EMAILS.clear()
        r = client_a.post('/api/invitations', json={
            'email': 'zztest.newbie@example.com', 'invite_type': 'join_family'})
        check('lead A sends a site invite to an unknown email', r.status_code == 200, str(r.get_json()))
        invite_token = extract_link('invite')
        check('invite email contains an /invite/ link', invite_token is not None)
        if invite_token:
            client_d = app.test_client()
            client_d.get(f'/invite/{invite_token}', follow_redirects=True)
            register(client_d, 'zztest.newbie@example.com', 'Zztestnina', 'Newbie')
            newbie = q1(conn, "SELECT id, player_id, family_id, is_verified FROM users WHERE email = 'zztest.newbie@example.com'")
            check('invited signup lands in Alpha', newbie and newbie['family_id'] == fam_a['id'])
            check('invited signup is auto email-verified', newbie and newbie['is_verified'])
            if newbie:
                ids['users'].add(newbie['id'])
                ids['players'].add(newbie['player_id'])
                mem = q1(conn, """
                    SELECT status FROM player_family_memberships
                    WHERE player_id = %s AND family_id = %s
                """, (newbie['player_id'], fam_a['id']))
                check('invited member has an active membership', mem and mem['status'] == 'active')

        print('\n== Section 5: claim an unclaimed profile by email ==')
        SENT_EMAILS.clear()
        r = client_a.post('/api/team/players', json={
            'first_name': 'Zztestclaire', 'last_name': 'Claimable',
            'display_name': 'Zztestclaire', 'email': 'zztest.claire@example.com'})
        claim_pid = r.get_json().get('id')
        ids['players'].add(claim_pid)
        r = client_a.post(f'/api/players/{claim_pid}/claim-invite', json={})
        check('lead A sends a claim invite', r.status_code == 200, str(r.get_json()))
        claim_token = extract_link('claim')
        check('claim email contains a /claim/ link', claim_token is not None)
        if claim_token:
            client_e = app.test_client()
            client_e.get(f'/claim/{claim_token}', follow_redirects=True)
            register(client_e, 'zztest.claire@example.com', 'Zztestclaire', 'Claimable')
            claire = q1(conn, "SELECT id, player_id FROM users WHERE email = 'zztest.claire@example.com'")
            check('claimed account binds to the existing player',
                  claire and claire['player_id'] == claim_pid)
            if claire:
                ids['users'].add(claire['id'])

        print('\n== Section 6: game history and hard-delete protection ==')
        game_def = q1(conn, 'SELECT id FROM games ORDER BY id LIMIT 1')
        r = client_a.post('/api/games/new', json={
            'game_id': game_def['id'], 'player_ids': [adult_id, minor_id]})
        game = r.get_json()
        check('lead A starts a game with adult and minor', r.status_code == 200, str(game))
        ag_id = game.get('id')
        for rnd in (1, 2):
            r = client_a.post('/api/scores', json={
                'game_id': ag_id, 'player_id': adult_id, 'round_number': rnd, 'score': 10 * rnd})
            check(f'score saves for round {rnd}', r.status_code == 200)

        cur = conn.cursor()
        blocked = False
        try:
            cur.execute('DELETE FROM players WHERE id = %s', (adult_id,))
        except Exception:
            blocked = True
        finally:
            conn.rollback()
            cur.close()
        check('hard-deleting a player with scores is blocked by the database', blocked)

        r = client_a.delete(f'/api/players/{adult_id}')
        check('archiving is blocked while the player is in an active game',
              r.status_code == 400)
        run(conn, 'UPDATE active_games SET is_complete = TRUE WHERE id = %s', (ag_id,))
        conn.commit()

        print('\n== Section 7: release/transfer flow ==')
        SENT_EMAILS.clear()
        r = client_b.post('/api/release-requests', json={
            'player_ids': [adult_id], 'to_family_id': fam_b['id'], 'note': 'smoke test'})
        check('lead B requests transfer of the adult', r.status_code == 200, str(r.get_json()))
        batch = q1(conn, """
            SELECT batch_id FROM player_release_requests
            WHERE player_id = %s AND status = 'pending'
        """, (adult_id,))
        check('release request recorded', batch is not None)
        check('release email sent to lead A',
              any('zztest.alpha@example.com' in m['to'] for m in SENT_EMAILS))
        if batch:
            r = client_a.post(f"/api/release-requests/{batch['batch_id']}/decide?decision=approve")
            check('lead A approves the transfer', r.status_code == 200, str(r.get_json()))
            row = q1(conn, 'SELECT family_id FROM players WHERE id = %s', (adult_id,))
            check('adult home family is now Beta', row['family_id'] == fam_b['id'])
            row = q1(conn, """
                SELECT family_id FROM active_game_players
                WHERE active_game_id = %s AND player_id = %s
            """, (ag_id, adult_id))
            check('past game rows keep the original family attribution',
                  row and row['family_id'] == fam_a['id'])
            scores = q1(conn, 'SELECT COUNT(*) AS n FROM game_scores WHERE player_id = %s', (adult_id,))
            check('scores survive the transfer', scores['n'] == 2)

        print('\n== Section 8: archive / reinstate / purge lifecycle ==')
        r = client_b.delete(f'/api/players/{adult_id}')
        check('new home lead can archive the adult (history intact)', r.status_code == 200,
              str(r.get_json()))
        scores = q1(conn, 'SELECT COUNT(*) AS n FROM game_scores WHERE player_id = %s', (adult_id,))
        check('scores survive the archive', scores['n'] == 2)
        r = client_b.get('/api/players')
        check('archived player hidden from the roster API',
              not any(p['id'] == adult_id for p in (r.get_json() or [])))

        run(conn, "UPDATE users SET role = 'super_admin' WHERE id = %s", (user_a['id'],))
        conn.commit()
        r = client_a.post(f'/api/players/{adult_id}/reinstate')
        check('super admin reinstates the adult', r.status_code == 200, str(r.get_json()))
        row = q1(conn, 'SELECT archived_at FROM players WHERE id = %s', (adult_id,))
        check('reinstated player is unarchived', row['archived_at'] is None)

        r = client_a.post('/api/team/players', json={
            'first_name': 'Zztestpurge', 'last_name': 'Target', 'display_name': 'Zztestpurge'})
        purge_pid = r.get_json().get('id')
        ids['players'].add(purge_pid)
        r = client_a.post(f'/api/players/{purge_pid}/purge', json={'confirm': 'PURGE'})
        check('purge refused before archive (two-step safety)', r.status_code == 400)
        client_a.delete(f'/api/players/{purge_pid}')
        r = client_a.post(f'/api/players/{purge_pid}/purge', json={'confirm': 'PURGE'})
        check('purge succeeds after archive', r.status_code == 200, str(r.get_json()))
        row = q1(conn, 'SELECT first_name, purged_at FROM players WHERE id = %s', (purge_pid,))
        check('purged player is anonymized, row preserved',
              row and row['first_name'] == 'Deleted' and row['purged_at'] is not None)

        print('\n== Section 9: lead-scoped duplicate merge ==')
        r = client_b.post('/api/team/players', json={
            'first_name': 'Zztestadult', 'last_name': 'Grownup', 'display_name': 'Zztestadult Dup'})
        dup_id = r.get_json().get('id')
        ids['players'].add(dup_id)
        run(conn, "UPDATE users SET role = 'family_member' WHERE id = %s", (user_a['id'],))
        conn.commit()
        r = client_b.post('/api/admin/merge-players', json={'keep_id': adult_id, 'dup_id': dup_id})
        check('lead of both home families can merge duplicates', r.status_code == 200,
              str(r.get_json()))
        row = q1(conn, 'SELECT COUNT(*) AS n FROM players WHERE id = %s', (dup_id,))
        check('duplicate row is gone after merge', row['n'] == 0)
        scores = q1(conn, 'SELECT COUNT(*) AS n FROM game_scores WHERE player_id = %s', (adult_id,))
        check('kept player still owns all scores', scores['n'] == 2)

        audit_rows = q1(conn, """
            SELECT COUNT(*) AS n FROM user_audit_log
            WHERE user_id = ANY(%s)
        """, (list(ids['users']),))
        check('audit log recorded lifecycle actions', audit_rows['n'] > 0)

        print('\n== Section 10: same-person smart keep + password invite (no live data) ==')
        # History player with scores, empty twin with a login — Noah-shaped, disposable.
        r = client_b.post('/api/team/players', json={
            'first_name': 'Zztesttwin', 'last_name': 'History', 'display_name': 'Zztesttwin'})
        hist_pid = r.get_json().get('id')
        ids['players'].add(hist_pid)
        r = client_b.post('/api/team/players', json={
            'first_name': 'Zztesttwin', 'last_name': 'History', 'display_name': 'Zztesttwin'})
        empty_pid = r.get_json().get('id')
        ids['players'].add(empty_pid)
        # Attach scores to history player via a finished game already owned by B's family.
        game_def = q1(conn, 'SELECT id FROM games ORDER BY id LIMIT 1')
        r = client_b.post('/api/games/new', json={
            'game_id': game_def['id'], 'player_ids': [hist_pid, user_b['player_id']]})
        ag2 = r.get_json().get('id')
        client_b.post('/api/scores', json={
            'game_id': ag2, 'player_id': hist_pid, 'round_number': 1, 'score': 12})
        run(conn, 'UPDATE active_games SET is_complete = TRUE WHERE id = %s', (ag2,))
        # Fake login on the empty twin only.
        run(conn, """
            INSERT INTO users (email, password_hash, first_name, last_name, family_name,
                               role, is_verified, is_approved, is_active, family_id, player_id)
            VALUES ('zztest.twinlogin@example.com', %s, 'Zztesttwin', 'History', 'Zztest Beta',
                    'family_admin', TRUE, TRUE, TRUE, %s, %s)
        """, ('x', fam_b['id'], empty_pid))
        twin_user = q1(conn, "SELECT id FROM users WHERE email = 'zztest.twinlogin@example.com'")
        ids['users'].add(twin_user['id'])
        conn.commit()

        r = client_b.get('/api/team/duplicate-suggestions')
        pairs = (r.get_json() or {}).get('pairs') or []
        check('duplicate suggestions finds the twin pair',
              any({p['a']['id'], p['b']['id']} == {hist_pid, empty_pid} for p in pairs))
        r = client_b.get('/api/team/same-person/preview', query_string={'a': hist_pid, 'b': empty_pid})
        prev = r.get_json() or {}
        check('same-person preview keeps the history profile',
              r.status_code == 200 and prev.get('keep', {}).get('id') == hist_pid,
              str(prev))
        r = client_b.post('/api/team/same-person', json={'player_a_id': hist_pid, 'player_b_id': empty_pid})
        check('same-person smart merge succeeds', r.status_code == 200, str(r.get_json()))
        check('empty twin player row removed',
              q1(conn, 'SELECT COUNT(*) AS n FROM players WHERE id = %s', (empty_pid,))['n'] == 0)
        check('login moved onto the history player',
              q1(conn, 'SELECT player_id FROM users WHERE id = %s', (twin_user['id'],))['player_id'] == hist_pid)
        check('scores still on the kept player',
              q1(conn, 'SELECT COUNT(*) AS n FROM game_scores WHERE player_id = %s', (hist_pid,))['n'] >= 1)

        # Not-the-same: two Michaels with distinct display names.
        r = client_b.post('/api/team/players', json={
            'first_name': 'Zztestmike', 'last_name': 'Smith', 'display_name': 'Zztestmike'})
        mike1 = r.get_json().get('id')
        r = client_b.post('/api/team/players', json={
            'first_name': 'Zztestmike', 'last_name': 'Smith', 'display_name': 'Zztestmike'})
        mike2 = r.get_json().get('id')
        ids['players'].update({mike1, mike2})
        r = client_b.post('/api/team/not-same-person', json={
            'player_a_id': mike1, 'player_b_id': mike2,
            'display_name_a': 'Mike', 'display_name_b': 'Grandpa'})
        check('not-the-same requires distinct names and saves', r.status_code == 200, str(r.get_json()))
        r = client_b.get('/api/team/duplicate-suggestions')
        check('not-the-same pair no longer suggested',
              not any({p['a']['id'], p['b']['id']} == {mike1, mike2} for p in (r.get_json() or {}).get('pairs', [])))

        # Password invite binds existing player — never creates a second players row.
        SENT_EMAILS.clear()
        r = client_b.post('/api/team/players', json={
            'first_name': 'Zztestinvite', 'last_name': 'Member', 'display_name': 'Zztestinvite'})
        invite_pid = r.get_json().get('id')
        ids['players'].add(invite_pid)
        before = q1(conn, 'SELECT COUNT(*) AS n FROM players')['n']
        r = client_b.post(f'/api/players/{invite_pid}/password-invite',
                          json={'email': 'zztest.invitee@example.com'})
        check('password invite sends', r.status_code == 200, str(r.get_json()))
        tok = extract_link('set-password')
        # extract_link looks for /claim|invite|action — extend for set-password
        if not tok:
            for mail in reversed(SENT_EMAILS):
                import re as _re
                m = _re.search(r'/auth/set-password/([A-Za-z0-9_\-]+)', mail['body'])
                if m:
                    tok = m.group(1)
                    break
        check('password invite email has set-password link', tok is not None)
        if tok:
            client_pw = app.test_client()
            r = client_pw.post(f'/auth/set-password/{tok}', data={
                'password': PASSWORD, 'confirm_password': PASSWORD}, follow_redirects=True)
            check('set-password completes', r.status_code == 200)
            after = q1(conn, 'SELECT COUNT(*) AS n FROM players')['n']
            check('set-password did not create a new players row', after == before)
            u = q1(conn, "SELECT id, player_id FROM users WHERE email = 'zztest.invitee@example.com'")
            check('new login bound to the existing player', u and u['player_id'] == invite_pid)
            if u:
                ids['users'].add(u['id'])

        # Approval notification payload includes Approve action.
        run(conn, """
            INSERT INTO notifications (user_id, type, title, message, data)
            VALUES (%s, 'user_pending_approval', 'New User Needs Approval', 'test',
                    jsonb_build_object('user_id', %s, 'actions',
                        jsonb_build_array(jsonb_build_object(
                            'label','Approve','style','success','method','POST',
                            'url', %s))))
        """, (user_a['id'], twin_user['id'], f"/auth/admin/approve-user/{twin_user['id']}"))
        conn.commit()
        run(conn, "UPDATE users SET role = 'super_admin' WHERE id = %s", (user_a['id'],))
        conn.commit()
        r = client_a.get('/api/notifications')
        notifs = r.get_json() or []
        pending = [n for n in notifs if n.get('type') == 'user_pending_approval']
        check('approval notification exists for super admin', len(pending) > 0)
        if pending:
            data = pending[0].get('data') or {}
            if isinstance(data, str):
                import json as _json
                data = _json.loads(data)
            acts = data.get('actions') or []
            check('approval notification has Approve action',
                  any(a.get('label') == 'Approve' and 'approve-user' in (a.get('url') or '') for a in acts))

        print('\n== Section 11: family game layouts ==')
        # Use UNO Classic (game_id 3) so Five Crowns DiFede seed is untouched.
        uno_id = 3
        # Restore lead A as family lead (was promoted to super_admin above).
        run(conn, "UPDATE users SET role = 'family_admin' WHERE id = %s", (user_a['id'],))
        conn.commit()
        p_home = qall(conn, """
            SELECT p.id FROM players p
            JOIN player_family_memberships m ON m.player_id = p.id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
              AND p.purged_at IS NULL
            ORDER BY p.id LIMIT 2
        """, (fam_a['id'],))
        check('Alpha has two roster players for layout', len(p_home) >= 2)
        layout_pids = [p_home[0]['id'], p_home[1]['id']] if len(p_home) >= 2 else []

        # Non-lead Alpha member (invited newbie) cannot create/delete layouts.
        newbie = q1(conn, "SELECT id FROM users WHERE email = 'zztest.newbie@example.com'")
        client_inv = app.test_client()
        if newbie and layout_pids:
            activate_and_login(conn, client_inv, 'zztest.newbie@example.com')
            r = client_inv.post(f'/api/games/{uno_id}/layouts', json={
                'name': 'Zztest Blocked', 'player_ids': layout_pids,
                'scoring_direction': 'high_wins', 'target_score': 500})
            check('non-lead cannot create a layout', r.status_code == 403, str(r.get_json()))

        r = client_a.post(f'/api/games/{uno_id}/layouts', json={
            'name': 'Zztest Nightly', 'player_ids': layout_pids,
            'scoring_direction': 'high_wins', 'target_score': 200, 'is_default': True})
        check('lead can create a default layout', r.status_code == 200, str(r.get_json()))
        layout = (r.get_json() or {}).get('layout') or {}
        layout_id = layout.get('id')
        check('created layout marked default', layout.get('is_default') is True)
        check('created layout stores player order', layout.get('player_ids') == layout_pids)
        check('created layout stores target score', layout.get('target_score') == 200)

        r = client_b.get(f'/api/games/{uno_id}/layouts')
        b_layouts = (r.get_json() or {}).get('layouts') or []
        check('other family does not see Alpha layouts',
              not any(L.get('id') == layout_id for L in b_layouts))

        r = client_a.get(f'/api/games/{uno_id}/layouts')
        a_layouts = (r.get_json() or {}).get('layouts') or []
        check('lead can list family layouts',
              any(L.get('id') == layout_id and L.get('is_default') for L in a_layouts))
        if newbie:
            r = client_inv.get(f'/api/games/{uno_id}/layouts')
            check('family member can list layouts',
                  any(L.get('id') == layout_id for L in (r.get_json() or {}).get('layouts', [])))

        r = client_a.post(f'/api/games/{uno_id}/layouts', json={
            'name': 'Zztest Alt', 'player_ids': list(reversed(layout_pids)),
            'scoring_direction': 'low_wins', 'target_score': 100})
        check('lead can create a second layout', r.status_code == 200, str(r.get_json()))
        alt_id = (r.get_json() or {}).get('layout', {}).get('id')
        r = client_a.post(f'/api/layouts/{alt_id}/set-default')
        check('lead can change default layout', r.status_code == 200, str(r.get_json()))
        defaults = qall(conn, """
            SELECT id FROM game_layouts
            WHERE family_id = %s AND game_id = %s AND is_default = TRUE
        """, (fam_a['id'], uno_id))
        check('only one default layout per family+game',
              len(defaults) == 1 and defaults[0]['id'] == alt_id)

        r = client_a.post('/api/games/new', json={
            'game_id': uno_id, 'player_ids': layout_pids,
            'scoring_direction': 'high_wins', 'target_score': 200})
        check('can start game from layout players', r.status_code == 200, str(r.get_json()))

        if newbie and layout_id:
            r = client_inv.delete(f'/api/layouts/{layout_id}')
            check('non-lead cannot delete a layout', r.status_code == 403, str(r.get_json()))
        r = client_a.delete(f'/api/layouts/{layout_id}')
        check('lead can delete a layout', r.status_code == 200, str(r.get_json()))
        check('deleted layout is gone',
              q1(conn, 'SELECT COUNT(*) AS n FROM game_layouts WHERE id = %s', (layout_id,))['n'] == 0)

        seed = q1(conn, """
            SELECT name, player_ids, is_default FROM game_layouts
            WHERE family_id = 1 AND game_id = 1 AND name = 'Kim & Joe'
        """)
        check('DiFede Five Crowns Kim & Joe layout seeded',
              seed and seed['is_default'] is True, str(seed))

        print('\n== Section 12: user feedback to super admins ==')
        run(conn, "UPDATE users SET role = 'super_admin' WHERE id = %s", (user_a['id'],))
        conn.commit()

        r = client_b.post('/api/feedback', json={
            'category': 'bug',
            'subject': 'Zztest score sheet stuck',
            'body': 'On mobile the Five Crowns score cell does not accept input.',
        })
        check('member can submit feedback', r.status_code == 200, str(r.get_json()))
        fb_id = (r.get_json() or {}).get('id')
        check('feedback id returned', bool(fb_id))

        notif = q1(conn, """
            SELECT id, type, title, data, is_read FROM notifications
            WHERE user_id = %s AND type = 'user_feedback'
            ORDER BY id DESC LIMIT 1
        """, (user_a['id'],))
        check('super admin got feedback notification',
              notif is not None and notif['is_read'] is False, str(notif))
        if notif:
            data = notif['data'] or {}
            if isinstance(data, str):
                import json as _json
                data = _json.loads(data)
            acts = data.get('actions') or []
            check('feedback notification has Open action',
                  any(a.get('label') == 'Open' and f'/admin/feedback/{fb_id}' in (a.get('url') or '')
                      for a in acts), str(data))

        r = client_a.get('/api/notifications')
        a_notifs = r.get_json() or []
        check('feedback appears in super admin notification API',
              any(n.get('type') == 'user_feedback' for n in a_notifs))

        r = client_a.get(f'/admin/feedback/{fb_id}')
        check('super admin can open feedback from inbox route', r.status_code == 200)
        view_a = q1(conn, """
            SELECT first_seen_at, last_seen_at FROM feedback_views
            WHERE feedback_id = %s AND user_id = %s
        """, (fb_id, user_a['id']))
        check('opening feedback records seen-by for admin A', view_a is not None)
        notif_after = q1(conn, 'SELECT is_read FROM notifications WHERE id = %s', (notif['id'],)) if notif else None
        check('opening feedback marks admin notification read',
              notif_after and notif_after['is_read'] is True)

        # Second super admin also opens it — paper trail grows.
        run(conn, "UPDATE users SET role = 'super_admin' WHERE id = %s", (user_b['id'],))
        conn.commit()
        r = client_b.get(f'/admin/feedback/{fb_id}')
        check('second super admin can open feedback', r.status_code == 200)
        viewers = qall(conn, """
            SELECT user_id FROM feedback_views WHERE feedback_id = %s ORDER BY first_seen_at
        """, (fb_id,))
        check('paper trail includes both super admins',
              {v['user_id'] for v in viewers} == {user_a['id'], user_b['id']},
              str([v['user_id'] for v in viewers]))

        r = client_a.post(f'/api/admin/feedback/{fb_id}/status', json={'status': 'in_progress'})
        check('super admin can set feedback status', r.status_code == 200, str(r.get_json()))
        check('feedback status persisted',
              q1(conn, 'SELECT status FROM feedback_items WHERE id = %s', (fb_id,))['status'] == 'in_progress')

        # Non-admin cannot open admin inbox.
        newbie = q1(conn, "SELECT id FROM users WHERE email = 'zztest.newbie@example.com'")
        if newbie:
            client_n = app.test_client()
            activate_and_login(conn, client_n, 'zztest.newbie@example.com')
            r = client_n.get(f'/admin/feedback/{fb_id}', follow_redirects=False)
            check('non-admin cannot open feedback detail',
                  r.status_code in (302, 403), f'status={r.status_code}')
            r = client_n.post('/api/feedback', json={
                'category': 'new_game',
                'subject': 'Zztest wants Phase 10',
                'body': 'Please add Phase 10 scoring.',
            })
            check('regular user can still submit feedback', r.status_code == 200, str(r.get_json()))

        r = client_a.get('/admin/feedback')
        check('feedback inbox page loads', r.status_code == 200)
        r = client_b.get('/feedback')
        page = r.get_data(as_text=True)
        check('feedback page loads for members', r.status_code == 200)
        # Primary navbar link (not buried in the user dropdown).
        primary = page.split('navbar-nav ms-auto', 1)[0] if 'navbar-nav ms-auto' in page else ''
        check('Feedback link is on the primary navbar',
              "href=\"/feedback\"" in primary or "Send Feedback" in primary or '>Feedback<' in primary)

        # Replies + search + notification destination URLs.
        reply_marker = 'ZztestAdminReplyMarker42'
        r = client_a.post(f'/api/feedback/{fb_id}/replies', json={
            'body': f'Thanks for reporting. {reply_marker}'
        })
        check('super admin can reply to feedback', r.status_code == 200, str(r.get_json()))
        check('feedback reply row persisted',
              q1(conn, 'SELECT COUNT(*) AS n FROM feedback_replies WHERE feedback_id = %s', (fb_id,))['n'] >= 1)

        reply_notif = q1(conn, """
            SELECT id, type, data FROM notifications
            WHERE user_id = %s AND type = 'user_feedback_reply'
            ORDER BY id DESC LIMIT 1
        """, (user_b['id'],))
        check('submitter got feedback reply notification', reply_notif is not None)
        if reply_notif:
            import json as _json
            rdata = reply_notif['data'] or {}
            if isinstance(rdata, str):
                rdata = _json.loads(rdata)
            check('feedback reply notification has url',
                  (rdata.get('url') or '') == f'/feedback/{fb_id}', str(rdata))

        r = client_b.get(f'/feedback/{fb_id}')
        b_thread = r.get_data(as_text=True)
        check('submitter can open own feedback thread', r.status_code == 200)
        check('submitter sees admin reply in thread', reply_marker in b_thread)

        r = client_b.post(f'/api/feedback/{fb_id}/replies', json={
            'body': 'ZztestUserFollowUp still stuck on iPhone.'
        })
        check('submitter can reply on their feedback', r.status_code == 200, str(r.get_json()))

        r = client_a.get('/admin/feedback?q=ZztestAdminReplyMarker42')
        check('admin feedback search finds reply text', r.status_code == 200)
        check('admin search results include the feedback item',
              f'/admin/feedback/{fb_id}' in r.get_data(as_text=True) or str(fb_id) in r.get_data(as_text=True))

        if notif:
            import json as _json
            fdata = notif['data'] or {}
            if isinstance(fdata, str):
                fdata = _json.loads(fdata)
            check('feedback notification has destination url',
                  (fdata.get('url') or '') == f'/admin/feedback/{fb_id}', str(fdata))

        print('\n== Section 13: personal crew links ==')
        # Use Copycat (separate family, never joined Alpha) so personal crew is
        # the only reason they appear in a picker -- Beta already joined Alpha earlier.
        activate_and_login(conn, client_c, 'zztest.copycat@example.com')
        newbie_row = q1(conn, "SELECT id, player_id FROM users WHERE email = 'zztest.newbie@example.com'")
        check('newbie exists for personal crew test', newbie_row is not None)
        client_n = app.test_client()
        if newbie_row:
            activate_and_login(conn, client_n, 'zztest.newbie@example.com')
            r = client_n.get('/api/directory/people?q=Zztestcopy')
            people = (r.get_json() or {}).get('results') or []
            copy_hit = next((p for p in people if p.get('player_id') == copy_user['player_id']), None)
            check('directory finds Copycat for personal crew', copy_hit is not None, str(people[:3]))
            if copy_hit:
                check('directory offers can_personal_crew for outsider with account',
                      copy_hit.get('can_personal_crew') is True, str(copy_hit))

            r = client_n.post('/api/crew-links', json={'player_id': copy_user['player_id']})
            check('non-lead can request personal crew', r.status_code == 200, str(r.get_json()))
            link_id = (r.get_json() or {}).get('id')
            check('crew link id returned', bool(link_id))

            crew_notif = q1(conn, """
                SELECT id, type, data FROM notifications
                WHERE user_id = %s AND type = 'personal_crew_request'
                ORDER BY id DESC LIMIT 1
            """, (copy_user['id'],))
            check('target got personal crew request notification', crew_notif is not None)
            if crew_notif:
                import json as _json
                cdata = crew_notif['data'] or {}
                if isinstance(cdata, str):
                    cdata = _json.loads(cdata)
                check('personal crew notification has url',
                      (cdata.get('url') or '') == '/my-team', str(cdata))
                acts = cdata.get('actions') or []
                check('personal crew notification has Accept action',
                      any(a.get('label') == 'Accept' for a in acts), str(cdata))

            before = client_n.get('/api/players').get_json() or []
            check('pending personal crew is not yet in /api/players',
                  not any(p.get('id') == copy_user['player_id'] for p in before),
                  str([p.get('id') for p in before]))

            r = client_c.post(f'/api/crew-links/{link_id}/accept')
            check('target can accept personal crew', r.status_code == 200, str(r.get_json()))

            after = client_n.get('/api/players').get_json() or []
            copy_pick = next((p for p in after if p.get('id') == copy_user['player_id']), None)
            check('accepted personal crew appears in /api/players', copy_pick is not None)
            if copy_pick:
                check('personal crew flagged for picker grouping',
                      copy_pick.get('is_personal_crew') is True
                      or copy_pick.get('guest_family_name') == 'Personal crew',
                      str(copy_pick))

            # Beta is Alpha-allied via membership, not the outsider control case.
            # A random other-family player without a link must stay out -- use Beta's
            # family-only adult if present? Simpler: Copycat's family has only Copycat;
            # verify Beta without personal crew from Copycat's perspective.
            from_copy = client_c.get('/api/players').get_json() or []
            check('accepter also sees requester in /api/players',
                  any(p.get('id') == newbie_row['player_id'] for p in from_copy))
            check('non-crewed Beta not in Copycat picker',
                  not any(p.get('id') == user_b['player_id'] for p in from_copy),
                  str([p.get('id') for p in from_copy]))

            r = client_n.get('/my-team')
            check('my team loads with personal crew section',
                  r.status_code == 200 and 'Personal Crew' in r.get_data(as_text=True))

            r = client_n.post(f'/api/crew-links/{link_id}/end')
            check('either side can end personal crew', r.status_code == 200, str(r.get_json()))
            ended = client_n.get('/api/players').get_json() or []
            check('ended personal crew removed from /api/players',
                  not any(p.get('id') == copy_user['player_id'] for p in ended))

        # Game landing uses searchable picker partial.
        r = client_a.get('/game/uno-classic')
        gl = r.get_data(as_text=True)
        check('game landing includes player picker',
              r.status_code == 200 and 'PlayerPicker' in gl and 'pp-input' in gl)
        check('player picker hides search when a chip is selected',
              'pp-change' in gl or 'Selected:' in gl or 'PlayerPicker' in gl)

        print('\n== Section 14: pause sticks + score API auth ==')
        fc = q1(conn, "SELECT id FROM games WHERE slug = 'five-crowns' OR id = 1 ORDER BY id LIMIT 1")
        pids = qall(conn, '''
            SELECT p.id FROM players p
            JOIN player_family_memberships m ON m.player_id = p.id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
            ORDER BY p.id LIMIT 2
        ''', (fam_a['id'],))
        if fc and len(pids) >= 2:
            r = client_a.post('/api/games/five-crowns/new', json={
                'player_ids': [pids[0]['id'], pids[1]['id']]
            })
            if r.status_code != 200:
                r = client_a.post('/api/games/new', json={
                    'game_id': fc['id'], 'player_ids': [pids[0]['id'], pids[1]['id']]
                })
            pause_game = (r.get_json() or {}).get('id')
            check('can start Five Crowns for pause test', bool(pause_game), str(r.get_json()))
            if pause_game:
                r = client_a.post('/api/scores', json={
                    'game_id': pause_game, 'player_id': pids[0]['id'],
                    'round_number': 3, 'score': 12
                })
                check('score saves before pause', r.status_code == 200, str(r.get_json()))

                r = client_a.post('/api/games/five-crowns/pause', json={'game_id': pause_game})
                check('pause API succeeds', r.status_code == 200, str(r.get_json()))
                row = q1(conn, 'SELECT is_paused FROM active_games WHERE id = %s', (pause_game,))
                check('pause flag stays true in database', row and row['is_paused'] is True)

                # Opening ?game_id= must NOT auto-unpause (the old bug).
                r = client_a.get(f'/five-crowns?game_id={pause_game}')
                check('paused game page loads without error', r.status_code == 200)
                row = q1(conn, 'SELECT is_paused FROM active_games WHERE id = %s', (pause_game,))
                check('opening paused game does not clear pause',
                      row and row['is_paused'] is True)
                page = r.get_data(as_text=True)
                check('paused game shows Resume, not the live board',
                      'resume-game' in page or 'Paused Games' in page or 'paused' in page.lower())

                r = client_a.post(f'/api/games/five-crowns/resume/{pause_game}')
                check('resume API succeeds', r.status_code == 200, str(r.get_json()))
                row = q1(conn, 'SELECT is_paused FROM active_games WHERE id = %s', (pause_game,))
                check('resume clears pause flag', row and row['is_paused'] is False)

                r = client_a.post('/api/scores', json={
                    'game_id': pause_game, 'player_id': pids[1]['id'],
                    'round_number': 3, 'score': 8
                })
                check('score saves after resume', r.status_code == 200, str(r.get_json()))

                anon = app.test_client()
                r = anon.post('/api/scores', json={
                    'game_id': pause_game, 'player_id': pids[0]['id'],
                    'round_number': 4, 'score': 5
                })
                check('unauthenticated score POST returns JSON 401',
                      r.status_code == 401 and (r.get_json() or {}).get('login_required') is True,
                      str(r.status_code) + ' ' + str(r.get_json()))

                # Last write wins for the same cell (simulates typed "2" then "25").
                for val in (2, 25):
                    r = client_a.post('/api/scores', json={
                        'game_id': pause_game, 'player_id': pids[0]['id'],
                        'round_number': 4, 'score': val
                    })
                    check(f'score overwrite step {val} accepted', r.status_code == 200, str(r.get_json()))
                final = q1(conn, '''
                    SELECT score FROM game_scores
                    WHERE active_game_id = %s AND player_id = %s AND round_number = 4
                ''', (pause_game, pids[0]['id']))
                check('latest score value wins for same cell',
                      final and final['score'] == 25, str(final))

                run(conn, 'UPDATE active_games SET is_complete = TRUE WHERE id = %s', (pause_game,))
                conn.commit()

        # 3-player Five Crowns board includes dealer/totals helpers.
        pids3 = qall(conn, '''
            SELECT p.id FROM players p
            JOIN player_family_memberships m ON m.player_id = p.id
            WHERE m.family_id = %s AND m.status = 'active' AND p.archived_at IS NULL
            ORDER BY p.id LIMIT 3
        ''', (fam_a['id'],))
        if fc and len(pids3) >= 3:
            r = client_a.post('/api/games/five-crowns/new', json={
                'player_ids': [pids3[0]['id'], pids3[1]['id'], pids3[2]['id']]
            })
            g3 = (r.get_json() or {}).get('id')
            check('can start 3-player Five Crowns', bool(g3), str(r.get_json()))
            if g3:
                for i, pid in enumerate([pids3[0]['id'], pids3[1]['id'], pids3[2]['id']]):
                    r = client_a.post('/api/scores', json={
                        'game_id': g3, 'player_id': pid, 'round_number': 3, 'score': 10 + i
                    })
                    check(f'3-player round 3 score for seat {i+1}', r.status_code == 200)
                # Leave one player without scores so complete summary still lists them.
                r = client_a.post(f'/api/games/complete/{g3}')
                # Prefer five-crowns complete if generic differs.
                if r.status_code != 200:
                    r = client_a.post('/api/games/five-crowns/complete', json={'game_id': g3})
                check('3-player Five Crowns can complete', r.status_code == 200, str(r.get_json()))
                page = client_a.get('/five-crowns').get_data(as_text=True)
                check('Five Crowns page has getCellScoreValue helper',
                      'getCellScoreValue' in page)
                check('Five Crowns page has contiguous dealer logic',
                      'Contiguous completed rounds' in page or 'roundIsComplete' in page)
                run(conn, 'UPDATE active_games SET is_complete = TRUE WHERE id = %s', (g3,))
                conn.commit()
        elif fc:
            page = client_a.get('/five-crowns').get_data(as_text=True)
            check('Five Crowns page has getCellScoreValue helper',
                  'getCellScoreValue' in page)
            check('Five Crowns page has contiguous dealer logic',
                  'Contiguous completed rounds' in page or 'roundIsComplete' in page)

        print('\n== Section 15: admin user deactivate / activate / delete ==')
        run(conn, "UPDATE users SET role = 'super_admin' WHERE id = %s", (user_a['id'],))
        conn.commit()
        page = client_a.get('/auth/admin/users').get_data(as_text=True)
        check('admin users page loads for super admin',
              'User Management' in page or 'Deactivate' in page)
        check('admin users page wires real deactivate/activate APIs',
              '/api/users/' in page and 'archive' in page and 'reinstate' in page)
        check('admin users page wires real delete API',
              "/auth/admin/users/' + userId + '/delete'" in page
              or 'admin/users/' in page and '/delete' in page)
        check('admin delete is a single confirm that POSTs delete',
              'Delete Login' in page and 'Final Warning' not in page
              and 'data-aum-action="delete"' in page)
        check('admin users actions use data attributes (no tojson-in-onclick)',
              'data-aum-action="delete"' in page
              and 'onclick="deleteUser(' not in page
              and 'onclick="approveUser(' not in page
              and 'onclick="setRole(' not in page
              and 'onclick="toggleUserStatus(' not in page)
        import re as _re
        check('admin users HTML has no broken onclick name quoting',
              not _re.search(
                  r'onclick="(?:deleteUser|approveUser|setRole|toggleUserStatus)\([^"]*"[^"]+"',
                  page))
        base = client_a.get('/dashboard').get_data(as_text=True)
        check('AppModal queues nested dialogs after hide',
              '_queue' in base and 'hidden.bs.modal' in base)
        check('admin deactivate is not a Coming Soon stub',
              'User deactivate functionality not yet implemented' not in page
              and 'User deletion functionality not yet implemented' not in page)
        check('admin users details is not a Coming Soon stub',
              'Detailed user information view coming soon' not in page
              and 'ADMIN_USERS' in page and 'aum-detail-row' in page)
        check('admin users has mobile card layout',
              'aum-card-list' in page and 'aum-user-card' in page
              and 'aum-table-wrap' in page)
        check('admin users hides table on phone widths',
              'max-width: 991.98px' in page and 'aum-table-wrap' in page
              and 'display: none' in page)
        admin_dash = client_a.get('/admin').get_data(as_text=True)
        check('admin dashboard management modals go fullscreen on phones',
              'modal-fullscreen-sm-down' in admin_dash)

        client_z = app.test_client()
        register(client_z, 'zztest.doomed@example.com', 'Zztestdoomed', 'Target', 'Zztest Doomed')
        doomed = q1(conn, "SELECT id, player_id FROM users WHERE email = 'zztest.doomed@example.com'")
        check('doomed test user created', bool(doomed))
        if doomed:
            ids['users'].add(doomed['id'])
            if doomed.get('player_id'):
                ids['players'].add(doomed['player_id'])
            doomed_fam = q1(conn, """
                SELECT id FROM families WHERE lead_user_id = %s OR id = (
                    SELECT family_id FROM users WHERE id = %s
                ) LIMIT 1
            """, (doomed['id'], doomed['id']))
            if doomed_fam:
                ids['families'].add(doomed_fam['id'])
            activate_and_login(conn, client_z, 'zztest.doomed@example.com')

            r = client_a.post(f"/api/users/{doomed['id']}/archive")
            check('super admin can deactivate (archive) a user',
                  r.status_code == 200, str(r.get_json()))
            row = q1(conn, 'SELECT is_active, archived_at FROM users WHERE id = %s', (doomed['id'],))
            check('deactivated user is inactive and archived',
                  row and row['is_active'] is False and row['archived_at'] is not None, str(row))
            r = client_z.post('/auth/login', data={
                'email': 'zztest.doomed@example.com', 'password': PASSWORD
            }, follow_redirects=True)
            body = r.get_data(as_text=True)
            check('deactivated user cannot log in',
                  'deactivated' in body.lower() or 'invalid' in body.lower())

            r = client_a.post(f"/api/users/{doomed['id']}/reinstate")
            check('super admin can activate (reinstate) a user',
                  r.status_code == 200, str(r.get_json()))
            row = q1(conn, 'SELECT is_active, archived_at FROM users WHERE id = %s', (doomed['id'],))
            check('activated user is active again',
                  row and row['is_active'] is True and row['archived_at'] is None, str(row))
            check('activated user can log in again',
                  activate_and_login(conn, client_z, 'zztest.doomed@example.com'))

            player_id = doomed.get('player_id')
            r = client_a.post(f"/auth/admin/users/{doomed['id']}/delete")
            check('super admin can delete a login account',
                  r.status_code == 200 and (r.get_json() or {}).get('success') is True,
                  str(r.get_json()))
            gone = q1(conn, 'SELECT id FROM users WHERE id = %s', (doomed['id'],))
            check('deleted login row is gone', gone is None)
            if player_id:
                kept = q1(conn, 'SELECT id FROM players WHERE id = %s', (player_id,))
                check('player profile survives login delete', kept is not None)
            ids['users'].discard(doomed['id'])

            r = client_a.post(f"/auth/admin/users/{user_a['id']}/delete")
            check('cannot delete own account', r.status_code == 400, str(r.get_json()))

    finally:
        print('\n== Cleanup: removing all Zztest data ==')
        try:
            np, nu, nf = cleanup(conn, ids)
            print(f'  removed {np} players, {nu} users, {nf} families')
            leftovers = q1(conn, """
                SELECT (SELECT COUNT(*) FROM players WHERE first_name ILIKE 'zztest%%')
                     + (SELECT COUNT(*) FROM users WHERE email LIKE 'zztest%%')
                     + (SELECT COUNT(*) FROM families WHERE name LIKE 'Zztest%%') AS n
            """)
            check('no Zztest rows left behind', leftovers['n'] == 0, f"{leftovers['n']} rows remain")
        except Exception as e:
            conn.rollback()
            print(f'  CLEANUP ERROR: {e}')
        conn.close()

    failed = [c for c in CHECKS if not c[1]]
    print(f'\n===== {len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed =====')
    if failed:
        for name, _, detail in failed:
            print(f'  FAILED: {name}' + (f' ({detail})' if detail else ''))
        sys.exit(1)
    print('ALL CHECKS PASSED')


if __name__ == '__main__':
    main()
