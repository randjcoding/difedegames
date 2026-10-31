"""Real two-worker HTTP/PG parity security checks, synthetic test database only.

Uses common.py's exact difedegames_v3_test reset guard. Never load pilot,
restored-data clone, owner, import, or SMTP env files into this process.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlencode
import uuid

from sqlalchemy import JSON, literal, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from common import reset_schema
from postgres_http import Client, PASSWORD, action, parallel, require, save
from difede_api.models import Game, Group, GroupAdmin, Membership, Person, User
from difede_api.privacy import sql_flag
from difede_api.security import hash_password, verify_password

RESULTS = []
SHARED_HASH = None
OWNER = None


def fixture(engine):
    """Fresh actors per case prevent password/role changes coupling cases."""
    global SHARED_HASH, OWNER
    SHARED_HASH = SHARED_HASH or hash_password(PASSWORD)
    nonce = uuid.uuid4().hex[:10]
    roles = ('owner', 'lead1', 'lead2', 'member', 'outsider', 'super')
    actors = {}
    with Session(engine) as db, db.begin():
        groups = [Group(name='Discoverable_' + nonce, settings={'is_discoverable': True, 'show_roster': True}),
                  Group(name='Other_' + nonce, settings={'is_discoverable': False, 'show_roster': False})]
        db.add_all(groups)
        db.flush()
        for index, role in enumerate(roles):
            if role == 'owner' and OWNER is not None:
                membership = Membership(person_id=OWNER.pid, group_id=groups[0].id, status='active')
                db.add(membership); db.flush()
                actors[role] = SimpleNamespace(uid=OWNER.uid, pid=OWNER.pid, mid=membership.id)
                continue
            person = Person(name=f'Parity synthetic {nonce} {role}', privacy={'is_discoverable': True, 'is_minor': False})
            db.add(person)
            db.flush()
            account = User(email=f'acceptance{person.id}@example.com', name='Synthetic parity actor',
                           person_id=person.id, password_hash=SHARED_HASH,
                           site_role='super_admin' if role in ('owner', 'super') else 'member',
                           is_owner=role == 'owner')
            db.add(account)
            db.flush()
            # Fresh DB sequences normally agree; login uses account ID explicitly.
            account.email = f'acceptance{account.id}@example.com'
            member = Membership(person_id=person.id, group_id=groups[1 if role == 'outsider' else 0].id,
                                role='admin' if role in ('lead1', 'lead2') else 'member', status='active')
            db.add(member)
            db.flush()
            actors[role] = SimpleNamespace(uid=account.id, pid=person.id, mid=member.id)
            if role in ('lead1', 'lead2'):
                db.add(GroupAdmin(group_id=groups[0].id, user_id=account.id))
        group, other = groups[0].id, groups[1].id
    OWNER = actors['owner']
    return SimpleNamespace(**actors, group=group, other=other, nonce=nonce)


def check(name, function):
    try:
        function()
        RESULTS.append({'name': name, 'passed': True})
    except Exception as error:
        RESULTS.append({'name': name, 'passed': False, 'error_type': type(error).__name__,
                        'assertion': str(error) if isinstance(error, AssertionError) else 'private failure'})


def create_game(client, f, players=None):
    code, game, _ = client.call('POST', '/api/games', {'slug': 'gin-rummy', 'group_id': f.group,
        'player_ids': players or [f.owner.pid, f.member.pid], 'viewer_group_ids': [f.group]})
    require(code == 200, 'synthetic game creation')
    return game


def active_leads(engine, gid):
    with engine.connect() as db:
        return db.scalar(text('''SELECT count(*) FROM group_admins ga JOIN accounts a ON a.id=ga.user_id
          JOIN memberships m ON m.person_id=a.person_id AND m.group_id=ga.group_id
          WHERE ga.group_id=:gid AND m.status='active' AND a.active AND a.verified AND a.approved'''), {'gid': gid})


def run_cases(engine):
    def role_boundaries():
        f = fixture(engine)
        owner, admin, member = Client(f.owner.uid), Client(f.super.uid), Client(f.member.uid)
        require(member.call('GET', '/api/admin/overview')[0] == 403, 'member cannot access site administration')
        require(member.call('GET', '/api/statistics?scope=site')[0] == 403, 'member cannot request private site statistics')
        require(member.call('PATCH', '/api/preferences', {'site_role': 'super_admin'})[0] == 422, 'preferences cannot mass assign privilege')
        require(admin.call('PATCH', f'/api/admin/accounts/{f.member.uid}', {'site_role': 'super_admin'})[0] == 403, 'nonowner admin cannot grant admin')
        require(admin.call('PATCH', f'/api/admin/accounts/{f.owner.uid}', {'active': False})[0] == 403, 'nonowner cannot disable owner')
        require(owner.call('PATCH', f'/api/admin/accounts/{f.member.uid}', {'site_role': 'super_admin'})[0] == 200, 'persisted owner may grant role')
        require(member.call('GET', '/api/me')[0] == 401, 'role change revokes existing session')
        signed_again = Client(f.member.uid)
        require(signed_again.call('GET', '/api/admin/overview')[0] == 200, 'new session uses persisted privilege')
        require(owner.call('PATCH', f'/api/admin/accounts/{f.member.uid}', {'active': False})[0] == 200, 'owner disables synthetic account')
        require(signed_again.call('GET', '/api/me')[0] == 401, 'disabled account sessions immediately revoked')
    check('Persisted owner/admin boundaries and account/session revocation', role_boundaries)

    def corrections():
        f = fixture(engine)
        owner, admin = Client(f.owner.uid), Client(f.super.uid)
        game = create_game(owner, f)
        game = save(owner, game, pid=f.owner.pid, value=0)
        game = save(owner, game, pid=f.member.pid, value=1)
        code, game, _ = action(owner, game, 'finish')
        require(code == 200, 'finish synthetic historic game')
        original = [{'winner_id': f.member.pid, 'winning_score': 1, 'is_tie': False}]
        with Session(engine) as db, db.begin():
            row = db.get(Game, game['id'])
            row.imported = True
            row.legacy = {'original_results': original}
            row.result = {**row.result, 'policy': 'v2-original', 'original_results': original}
        path = f'/api/admin/games/{game["id"]}/corrections'
        def payload(score):
            return {'operation_id': str(uuid.uuid4()), 'expected_revision': game['revision'],
                    'reason': 'Synthetic concurrent correction', 'scores': [
                        {'player_id': f.owner.pid, 'round_number': 1, 'score': score}]}
        results = parallel(lambda: owner.call('POST', path, payload(20)),
                           lambda: admin.call('POST', path, payload(30)))
        require(sorted(result[0] for result in results) == [200, 409], 'same-revision corrections serialize')
        current = owner.get_game(game)
        require(current['revision'] == game['revision'] + 1, 'one correction revision committed')
        require(current['finished_at'] == game['finished_at'], 'historical correction preserves finished chronology')
        require(current['imported_result']['winners'][0]['player_id'] == f.member.pid, 'original imported winner retained separately')
        with engine.connect() as db:
            require(db.scalar(text("SELECT count(*) FROM audit_events WHERE game_id=:gid AND action='historical_correction'"),
                              {'gid': game['id']}) == 1, 'one durable correction audit')
            blocked = False
            try:
                db.execute(text("DELETE FROM audit_events WHERE game_id=:gid AND action='historical_correction'"), {'gid': game['id']})
            except DBAPIError as error:
                blocked = getattr(error.orig, 'sqlstate', None) == 'P0001'
            finally:
                db.rollback()
            require(blocked, 'correction audit immutable under runtime owner')
    check('Concurrent historical correction commits once with immutable provenance/audit', corrections)

    def double_reset():
        f = fixture(engine)
        owner, first, second = Client(f.owner.uid), Client(f.member.uid), Client(f.member.uid)
        status, link, _ = owner.call('POST', f'/api/admin/accounts/{f.member.uid}/reset-link', {})
        require(status == 200, 'synthetic administrative reset link')
        token = link['url'].rsplit('/', 1)[1]
        path = '/api/auth/password-reset/' + token
        results = parallel(lambda: Client().call('POST', path, {'password': 'SyntheticResetOnly!2026'}),
                           lambda: Client().call('POST', path, {'password': 'SyntheticResetOnly!2026'}))
        require(sorted(result[0] for result in results) == [200, 404], 'single-use reset simultaneous consumption')
        require(first.call('GET', '/api/me')[0] == second.call('GET', '/api/me')[0] == 401, 'reset revokes every prior session')
        require(Client().call('GET', path)[0] == 404, 'consumed reset no longer valid')
        fresh = Client()
        require(fresh.call('POST', '/api/auth/login', {'email': f'acceptance{f.member.uid}@example.com',
                'password': 'SyntheticResetOnly!2026'})[0] == 200, 'new synthetic password works')
    check('Concurrent reset redemption is single-use and revokes sessions', double_reset)

    def reset_change_race():
        f = fixture(engine)
        owner, member = Client(f.owner.uid), Client(f.member.uid)
        status, link, _ = owner.call('POST', f'/api/admin/accounts/{f.member.uid}/reset-link', {})
        require(status == 200, 'race reset token created')
        path = '/api/auth/password-reset/' + link['url'].rsplit('/', 1)[1]
        results = parallel(lambda: member.call('POST', '/api/auth/password', {
                            'current_password': PASSWORD, 'new_password': 'SyntheticChangeOnly!2026'}),
                           lambda: Client().call('POST', path, {'password': 'SyntheticResetRace!2026'}))
        codes = [row[0] for row in results]
        require(codes.count(200) == 1 and all(code in (200, 401, 403, 404) for code in codes), 'password/reset race fails safely without500')
        require(member.call('GET', '/api/me')[0] == 401, 'race revokes original session')
        with Session(engine) as db:
            hashed = db.get(User, f.member.uid).password_hash
            winners = sum(verify_password(password, hashed) for password in ('SyntheticChangeOnly!2026', 'SyntheticResetRace!2026'))
            require(winners == 1 and not verify_password(PASSWORD, hashed), 'exactly one new synthetic credential committed')
    check('Password change versus reset race has one outcome and no deleted-token500', reset_change_race)

    def preferences():
        f = fixture(engine)
        first, second = Client(f.member.uid), Client(f.member.uid)
        results = parallel(lambda: first.call('PATCH', '/api/preferences', {'style': 'arcade'}),
                           lambda: second.call('PATCH', '/api/preferences', {'palette': 'royal-purple'}))
        require([result[0] for result in results] == [200, 200], 'independent preference patches both accepted')
        _, me, _ = first.call('GET', '/api/me')
        require(me['preferences']['style'] == 'arcade' and me['preferences']['palette'] == 'royal-purple', 'concurrent partial preferences preserve both fields')
    check('Concurrent different-field preferences do not lose updates', preferences)

    def lead_races():
        f = fixture(engine)
        first, second = Client(f.lead1.uid), Client(f.lead2.uid)
        results = parallel(lambda: first.call('POST', f'/api/groups/{f.group}/transfer-lead', {'user_id': f.lead2.uid}),
                           lambda: second.call('POST', f'/api/memberships/{f.lead2.mid}/decide', {'decision': 'remove'}))
        codes = [row[0] for row in results]
        require(codes.count(200) == 1 and all(code in (200, 409, 422) for code in codes), 'lead transfer/removal contention serializes')
        require(active_leads(engine, f.group) >= 1, 'transfer/removal leaves active lead')
        f = fixture(engine)
        first, second = Client(f.lead1.uid), Client(f.lead2.uid)
        results = parallel(lambda: first.call('POST', f'/api/memberships/{f.lead1.mid}/decide', {'decision': 'remove'}),
                           lambda: second.call('POST', f'/api/memberships/{f.lead2.mid}/decide', {'decision': 'remove'}))
        require(sorted(row[0] for row in results) == [200, 409], 'two leads cannot concurrently both leave')
        require(active_leads(engine, f.group) == 1, 'exactly one usable lead retained')
    check('Lead transfer/removal and last-lead races retain active leadership', lead_races)

    def archive_create():
        f = fixture(engine)
        owner, lead = Client(f.owner.uid), Client(f.lead1.uid)
        results = parallel(lambda: owner.call('POST', f'/api/groups/{f.group}/archive', {'reason': 'Synthetic archive race'}),
                           lambda: lead.call('POST', '/api/games', {'slug': 'gin-rummy', 'group_id': f.group,
                               'player_ids': [f.lead1.pid, f.member.pid], 'viewer_group_ids': [f.group]}))
        codes = [row[0] for row in results]
        require(codes.count(200) == 1 and all(code in (200, 403, 409, 422) for code in codes), 'archive/newgame race has one valid winner')
        with engine.connect() as db:
            archived = db.scalar(text('SELECT archived FROM groups WHERE id=:gid'), {'gid': f.group})
            active = db.scalar(text("SELECT count(*) FROM game_sessions WHERE group_id=:gid AND status='active' AND NOT deleted"), {'gid': f.group})
            require(not archived or active == 0, 'no unfinished game hosted by archived group')
    check('Concurrent group archive/new game preserves group lifecycle invariant', archive_create)

    def directory_privacy():
        f = fixture(engine)
        public_name, minor_name, hidden_name = 'Public' + f.nonce, 'Minor' + f.nonce, 'Hidden' + f.nonce
        with Session(engine) as db, db.begin():
            for actor, name, privacy in ((f.member, public_name, {'is_discoverable': True, 'is_minor': False}),
                    (f.lead1, minor_name, {'is_discoverable': True, 'is_minor': True}),
                    (f.lead2, hidden_name, {'is_discoverable': False, 'is_minor': False})):
                person = db.get(Person, actor.pid)
                person.name, person.details = name + ' SecretSurname', {'last_name': 'SecretSurname'}
                person.privacy = {**privacy, 'show_full_last_name': False}
        outsider, lead = Client(f.outsider.uid), Client(f.lead1.uid)
        code, body, _ = outsider.call('GET', '/api/directory/people?' + urlencode({'q': public_name}))
        require(code == 200 and [row['id'] for row in body['people']] == [f.member.pid], 'public adult discoverable outside group')
        require(body['people'][0]['name'] == public_name + ' S.', 'public surname consent enforced in JSON')
        require(set(body['people'][0]) == {'id', 'name', 'group_ids', 'has_account'}, 'directory omits contact and raw profile metadata')
        for name in (minor_name, hidden_name):
            require(outsider.call('GET', '/api/directory/people?' + urlencode({'q': name}))[1]['people'] == [], 'minor/hidden identity not publicly exposed')
        require(lead.call('PATCH', f'/api/groups/{f.group}', {'show_roster': False})[0] == 200, 'group roster visibility changed')
        require(outsider.call('GET', '/api/directory/people?' + urlencode({'q': public_name}))[1]['people'] == [], 'hidden group roster closes public discovery')
    check('PostgreSQL JSON privacy protects minors, surnames and hidden rosters', directory_privacy)

    def explicit_json_null():
        with Session(engine) as db:
            db.execute(text('SET TRANSACTION READ ONLY'))
            null = literal({'is_discoverable': None, 'is_minor': None}, type_=JSON)
            missing = literal({}, type_=JSON)
            values = db.execute(select(sql_flag(db, null, 'is_discoverable', True),
                                       sql_flag(db, missing, 'is_discoverable', True),
                                       sql_flag(db, null, 'is_minor', False, True))).one()
            require(tuple(values) == (False, True, True), 'PostgreSQL JSON null fails closed; missing defaults preserved')
            db.rollback()
    check('Read-only PostgreSQL JSON null/missing privacy helper', explicit_json_null)

    def layouts():
        f = fixture(engine)
        lead, member, outsider = Client(f.lead1.uid), Client(f.member.uid), Client(f.outsider.uid)
        common = {'group_id': f.group, 'slug': 'gin-rummy', 'player_ids': [f.lead1.pid, f.member.pid],
                  'mode': 'rounds', 'scoring_direction': 'high', 'is_default': True}
        results = parallel(lambda: lead.call('POST', '/api/layouts', {**common, 'name': 'Synthetic saved one'}),
                           lambda: Client(f.lead2.uid).call('POST', '/api/layouts', {**common, 'name': 'Synthetic saved two'}))
        require([row[0] for row in results] == [200, 200], 'concurrent default setup writes accepted')
        with engine.connect() as db:
            require(db.scalar(text("SELECT count(*) FROM saved_game_layouts WHERE group_id=:gid AND slug='gin-rummy' AND is_default"), {'gid': f.group}) == 1, 'one database-enforced default per group/type')
        layout = results[0][1]
        require(member.call('GET', f'/api/layouts?group_id={f.group}')[0] == 200, 'members may read shared setup')
        require(member.call('DELETE', f'/api/layouts/{layout["id"]}')[0] == 403, 'ordinary member cannot delete managed setup')
        require(outsider.call('GET', f'/api/layouts?group_id={f.group}')[0] == 403, 'outsider cannot read setup')
        require(lead.call('PUT', f'/api/layouts/{layout["id"]}', {**common, 'group_id': f.other, 'name': layout['name']})[0] == 422, 'saved setup cannot be moved to another group')
        require(lead.call('POST', '/api/layouts', {**common, 'name': layout['name']})[0] == 409, 'duplicate setup name conflicts without deleting default')
        code, game, _ = member.call('POST', f'/api/layouts/{layout["id"]}/start', {'viewer_group_ids': [f.group]})
        require(code == 200 and [row['id'] for row in game['players']] == common['player_ids'], 'start saved setup retains roster order')
    check('Saved setup ownership/default uniqueness and start semantics', layouts)


def main():
    require(not any(os.environ.get(key) for key in ('SMTP_HOST', 'SMTP_FROM', 'SMTP_USERNAME', 'SMTP_PASSWORD')),
            'SMTP environment must be absent from acceptance runtime')
    os.environ.update(SESSION_COOKIE_SECURE='false', APP_BASE_URL='http://localhost', ALLOWED_ORIGINS='http://localhost')
    engine = reset_schema()
    with tempfile.TemporaryFile(mode='w+') as log:
        process = subprocess.Popen(['uvicorn', 'difede_api.main:app', '--host', '127.0.0.1', '--port', '18763',
                                    '--workers', '2', '--no-access-log', '--log-level', 'error'], stdout=log, stderr=log)
        try:
            for _ in range(100):
                try:
                    if Client().call('GET', '/api/health')[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError('Synthetic acceptance API did not start')
            run_cases(engine)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            engine.dispose()
    report = {'database': 'difedegames_v3_test', 'synthetic_only': True, 'smtp_environment_absent': True,
              'transport': 'real HTTP / two workers / PostgreSQL', 'checks': len(RESULTS),
              'passed': sum(row['passed'] for row in RESULTS), 'results': RESULTS}
    print(json.dumps(report, indent=2))
    if os.environ.get('ACCEPTANCE_REPORT'):
        Path(os.environ['ACCEPTANCE_REPORT']).write_text(json.dumps(report, indent=2))
    raise SystemExit(0 if all(row['passed'] for row in RESULTS) else 1)


if __name__ == '__main__':
    main()
