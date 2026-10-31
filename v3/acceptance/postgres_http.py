"""Real HTTP, two-worker Uvicorn, and PostgreSQL acceptance; never use pilot DB.

Run inside the API image on the isolated V3 data network with private test.env,
ACCEPTANCE_RESET_DATABASE=difedegames_v3_test, and this directory on PYTHONPATH.
The exact isolated test database is reset. No real accounts/passwords are used.
"""
import concurrent.futures
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError

from common import reset_schema
from difede_api.models import Group, GroupAdmin, Membership, Person, User
from difede_api.security import hash_password

BASE = 'http://127.0.0.1:18763'
PASSWORD = 'AcceptanceOnly!2026'
RESULTS = []


def require(condition, label):
    if not condition:
        raise AssertionError(label)


class Client:
    def __init__(self, uid=None):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ''
        self.login_headers = None
        if uid is not None:
            code, body, headers = self.call('POST', '/api/auth/login', {
                'email': f'acceptance{uid}@example.com', 'password': PASSWORD})
            require(code == 200, 'fixture login')
            self.csrf = body['csrf_token']
            self.login_headers = headers

    def request(self, method, path, payload=None):
        headers = {'Origin': 'http://localhost', 'X-CSRF-Token': self.csrf}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        return urllib.request.Request(BASE + path, data=data, headers=headers, method=method)

    def call(self, method, path, payload=None):
        try:
            response = self.opener.open(self.request(method, path, payload), timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None, response.headers

    def get_game(self, game):
        code, body, _ = self.call('GET', f'/api/games/{game["id"]}')
        require(code == 200, 'read game')
        return body


def operation(**kwargs):
    return {'operation_id': str(uuid.uuid4()), **kwargs}


def new_game(client, slug='gin-rummy', **kwargs):
    code, body, _ = client.call('POST', '/api/games', {
        'slug': slug, 'group_id': 1, 'player_ids': [1, 2], 'viewer_group_ids': [1], **kwargs})
    require(code == 200, f'create {slug}')
    return body


def score_payload(pid=1, value=0, number=1, version=0, **kwargs):
    return operation(player_id=pid, round_number=number, score=value, expected_version=version, **kwargs)


def save(client, game, **kwargs):
    code, body, _ = client.call('POST', f'/api/games/{game["id"]}/scores', score_payload(**kwargs))
    require(code == 200, 'save fixture cell')
    return body


def action(client, game, action_name, **kwargs):
    return client.call('POST', f'/api/games/{game["id"]}/{action_name}',
                       operation(expected_revision=game['revision'], **kwargs))


def parallel(left, right):
    barrier = threading.Barrier(2)
    def run(fn):
        barrier.wait(timeout=10)
        return fn()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, fn) for fn in (left, right)]
        return [future.result(timeout=30) for future in futures]


def seed(engine):
    hashed = hash_password(PASSWORD)
    with Session(engine) as db, db.begin():
        db.add_all([Person(id=i, name=f'Acceptance Person {i}') for i in range(1, 9)])
        db.add_all([Group(id=1, name='Acceptance Main'), Group(id=2, name='Acceptance Other')])
        db.flush()
        db.add_all([User(id=i, email=f'acceptance{i}@example.com', name=f'Acceptance User {i}',
                         person_id=i, password_hash=hashed) for i in (1, 2, 3, 4, 5, 7, 8)])
        db.flush()
        db.add_all([Membership(person_id=i, group_id=1) for i in (1, 2, 3, 5, 6, 7)])
        db.add_all([Membership(person_id=i, group_id=2) for i in (1, 4, 8)])
        db.add_all([GroupAdmin(group_id=1, user_id=1), GroupAdmin(group_id=1, user_id=7)])
    with engine.begin() as connection:
        for table in ('people', 'accounts', 'groups', 'memberships'):
            connection.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}), true)"))


def check(name, fn):
    try:
        fn()
        RESULTS.append({'name': name, 'passed': True})
    except Exception as error:
        # Do not serialize response bodies, records, tokens, or DB exception text.
        RESULTS.append({'name': name, 'passed': False, 'error_type': type(error).__name__,
                        'assertion': str(error) if isinstance(error, AssertionError) else 'private failure'})


def run_cases(engine):
    a, b, viewer, outsider, keeper, manager = [Client(i) for i in (1, 2, 3, 4, 5, 7)]

    def permissions():
        game = new_game(a, scorekeeper_ids=[5])
        path = f'/api/games/{game["id"]}'
        for client, expected, can_score, can_manage in (
                (a, 200, True, True), (b, 200, True, False), (viewer, 200, False, False),
                (outsider, 404, False, False), (keeper, 200, True, False), (manager, 200, False, True)):
            status, data, _ = client.call('GET', path)
            require(status == expected, 'role read boundary')
            if status == 200:
                require(data['permissions'] == {'can_score': can_score, 'can_manage': can_manage}, 'role capability matrix')
        for client in (viewer, manager):
            require(client.call('POST', path + '/scores', score_payload())[0] == 403, 'nonplaying view/manage cannot score')
        require(outsider.call('POST', path + '/scores', score_payload())[0] == 404, 'outsider cannot score')
        game = save(keeper, game)
        game = save(b, game, pid=2, value=1)
        require(action(b, game, 'finish')[0] == 200, 'participant can finish')
        game = a.get_game(game)
        require(action(b, game, 'reopen')[0] == 403, 'participant cannot manage')
        require(action(manager, game, 'reopen')[0] == 200, 'nonplaying group admin can manage')
    check('HTTP permission matrix', permissions)

    def same_cell_race():
        game = new_game(a)
        path = f'/api/games/{game["id"]}/scores'
        results = parallel(lambda: a.call('POST', path, score_payload(value=4)),
                           lambda: b.call('POST', path, score_payload(value=9)))
        require(sorted(r[0] for r in results) == [200, 409], 'same-cell race one winner')
        body = a.get_game(game)
        require(body['revision'] == game['revision'] + 1, 'race single revision')
        require(body['scores'][0]['version'] == 1, 'race single cell version')
    check('PostgreSQL same-cell simultaneous writes', same_cell_race)

    def unrelated_cells():
        game = new_game(a)
        path = f'/api/games/{game["id"]}/scores'
        results = parallel(lambda: a.call('POST', path, score_payload(pid=1, value=4)),
                           lambda: b.call('POST', path, score_payload(pid=2, value=9)))
        require([r[0] for r in results] == [200, 200], 'independent cell writes both commit')
        body = a.get_game(game)
        require(body['revision'] == game['revision'] + 2 and len(body['scores']) == 2, 'independent revisions retained')
    check('PostgreSQL simultaneous unrelated-cell writes', unrelated_cells)

    def retry_race():
        game = new_game(a)
        other_session = Client(1)
        path = f'/api/games/{game["id"]}/scores'
        payload = score_payload(value=17)
        results = parallel(lambda: a.call('POST', path, payload), lambda: other_session.call('POST', path, payload))
        require([r[0] for r in results] == [200, 200], 'duplicate requests succeed')
        require(results[0][1] == results[1][1], 'duplicate requests same durable response')
        # Treat the original response as lost, modify another cell, then retry it.
        save(b, game, pid=2, value=2)
        code, replay, _ = a.call('POST', path, payload)
        require(code == 200 and replay == results[0][1], 'lost-response retry returns original result')
        require(a.call('POST', path, {**payload, 'score': 18})[0] == 409, 'reused id different payload conflicts')
        with engine.connect() as connection:
            require(connection.scalar(text('SELECT count(*) FROM operations WHERE game_id=:id AND operation_id=:op'),
                                      {'id': game['id'], 'op': payload['operation_id']}) == 1, 'one durable operation row')
    check('PostgreSQL duplicate request and timeout retry', retry_race)

    def finish_race():
        game = save(a, new_game(a))
        game = save(a, game, pid=2)
        path = f'/api/games/{game["id"]}'
        results = parallel(lambda: a.call('POST', path + '/finish', operation(expected_revision=game['revision'])),
                           lambda: b.call('POST', path + '/scores', score_payload(value=9, version=1)))
        require(sorted(r[0] for r in results) == [200, 409], 'finish/save race serializes')
        body = a.get_game(game)
        cell = next(cell for cell in body['scores'] if cell['player_id'] == 1)
        if body['status'] == 'completed':
            require(cell['value'] == 0 and cell['version'] == 1, 'completion froze original revision')
        else:
            require(cell['value'] == 9 and cell['version'] == 2, 'edit committed; stale completion rejected')
    check('PostgreSQL finish-versus-save race', finish_race)

    def completion():
        game = new_game(a)
        response = action(a, game, 'finish')
        require(response[0] == 422 and len(response[1]['missing']) == 2, 'missing differs from zero')
        game = save(a, game)
        game = save(a, game, pid=2)
        added = action(a, game, 'rounds')
        require(added[0] == 200, 'append blank hand')
        finished = action(a, added[1], 'finish')
        require(finished[0] == 200 and all(row['winner'] for row in finished[1]['standings']), 'zeros tie with trailing blank excluded')
        game = new_game(a)
        cleared = save(a, save(a, game), value=None, version=1)
        require(cleared['scores'][0]['version'] == 2 and cleared['scores'][0]['value'] is None, 'clear keeps tombstone')
        require(action(a, cleared, 'finish')[0] == 422, 'cleared score still missing')
    check('HTTP zero/null/tie/trailing-blank completion', completion)

    def invitations():
        code, invitation, _ = a.call('POST', '/api/groups/1/invites', {'person_id': 6})
        require(code == 200, 'bound guest invitation')
        token = invitation['url'].rsplit('/', 1)[1]
        require(b.call('POST', f'/api/invites/{token}/accept', {})[0] == 409, 'unrelated account cannot claim identity')
        guest = Client()
        accepted = guest.call('POST', f'/api/invites/{token}/accept', {
            'email': 'acceptance-guest@example.com', 'password': PASSWORD})
        require(accepted[0] == 200 and accepted[1]['user']['person_id'] == 6, 'explicit invitation preserves guest identity')
        guest.csrf = accepted[1]['csrf_token']
        require(guest.call('POST', f'/api/invites/{token}/accept', {})[0] == 404, 'invitation single use')
    check('HTTP explicit guest claim without identity merge', invitations)

    def guest_only():
        code, guest, _ = a.call('POST', '/api/people', {'name': 'Acceptance Additional Guest', 'group_id': 1})
        require(code == 200, 'create synthetic guest')
        code, second_guest, _ = a.call('POST', '/api/people', {'name': 'Acceptance Second Guest', 'group_id': 1})
        require(code == 200, 'create second synthetic guest')
        payload = {'slug': 'gin-rummy', 'group_id': 1, 'player_ids': [guest['id'], second_guest['id']], 'viewer_group_ids': [1]}
        require(a.call('POST', '/api/games', payload)[0] == 422, 'guest-only table requires scorekeeper')
        code, game, _ = a.call('POST', '/api/games', {**payload, 'scorekeeper_ids': [1]})
        require(code == 200 and game['permissions']['can_score'], 'explicit nonplaying keeper enables guest table')
        require(save(a, game, pid=guest['id'])['scores'][0]['value'] == 0, 'nonplaying keeper can score guest')
    check('Guest-only table requires explicit nonplaying scorekeeper', guest_only)

    def stream_revocation():
        stream_client = Client(2)
        game = new_game(a)
        with stream_client.opener.open(stream_client.request('GET', f'/api/games/{game["id"]}/events'), timeout=8) as stream:
            require('text/event-stream' in stream.headers.get('Content-Type', ''), 'real SSE content type')
            first = []
            while True:
                line = stream.readline().decode().strip()
                first.append(line)
                if not line:
                    break
            require('event: snapshot' in first, 'real SSE initial snapshot')
            require(stream_client.call('POST', '/api/auth/logout', {})[0] == 200, 'logout while SSE open')
            revoked = False
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                line = stream.readline().decode().strip()
                if line == 'event: access_revoked':
                    revoked = True
                    break
            require(revoked, 'open stream revokes after logout')
        require(stream_client.call('GET', '/api/me')[0] == 401, 'logged-out HTTP session revoked')
    check('Real SSE logout revocation', stream_revocation)

    code, catalog, _ = a.call('GET', '/api/catalog')
    require(code == 200 and len(catalog['games']) == 10, 'ten-game catalog')
    for definition in catalog['games']:
        for mode in definition['modes']:
            def game_mode(definition=definition, mode=mode):
                game = new_game(a, definition['slug'], mode=mode)
                if mode == 'rounds':
                    for hand in game['rounds']:
                        game = save(a, game, number=hand['number'])
                        game = save(a, game, pid=2, value=1, number=hand['number'])
                    code, completed, _ = action(a, game, 'finish')
                    require(code == 200 and completed['status'] == 'completed', 'round policy completes')
                    expected = 1 if game['scoring_direction'] == 'low' else 2
                    require([row['player_id'] for row in completed['standings'] if row['winner']] == [expected], 'directional winner correct')
                elif definition['slug'] == 'trouble':
                    code, completed, _ = action(a, game, 'finish', winner_id=1, details={'pegs': [
                        {'player_id': 1, 'color': 'red', 'finish': 4, 'board': 0, 'home': 0},
                        {'player_id': 2, 'color': 'blue', 'finish': 1, 'board': 1, 'home': 2}]})
                    require(code == 200 and next(c for c in completed['scores'] if c['player_id'] == 1)['value'] == 50, 'Trouble derived result')
                elif definition['slug'] == 'kings-corner':
                    code, completed, _ = action(a, game, 'finish', winner_id=1, details={'penalties': [{'player_id': 2, 'score': 13}]})
                    require(code == 200 and next(c for c in completed['scores'] if c['player_id'] == 1)['value'] == 13, 'Kings single result')
                else:
                    code, completed, _ = action(a, game, 'finish', winner_id=1)
                    require(code == 200 and not completed['scores'], 'winner-only has no fake points')
            check(f'PostgreSQL game policy {definition["slug"]}/{mode}', game_mode)

    def immutable_audit():
        for table in ('audit_events', 'game_events', 'operations'):
            with engine.connect() as connection:
                require(connection.scalar(text(f'SELECT count(*) FROM {table}')) > 0, 'immutable table has actual accepted events')
                blocked = False
                try:
                    connection.execute(text(f'DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM {table} LIMIT 1)'))
                except DBAPIError as error:
                    blocked = getattr(error.orig, 'sqlstate', None) == 'P0001'
                finally:
                    connection.rollback()
                require(blocked, 'runtime owner cannot delete immutable history')
    check('PostgreSQL audit/events/operations immutable triggers', immutable_audit)


def main():
    os.environ['SESSION_COOKIE_SECURE'] = 'false'  # Only this no-published-port acceptance server.
    os.environ['APP_BASE_URL'] = 'http://localhost'
    os.environ['ALLOWED_ORIGINS'] = 'http://localhost'
    engine = reset_schema()
    seed(engine)
    with tempfile.TemporaryFile(mode='w+') as server_log:
        process = subprocess.Popen(['uvicorn', 'difede_api.main:app', '--host', '127.0.0.1', '--port', '18763',
                                    '--workers', '2', '--no-access-log', '--log-level', 'error'],
                                   stdout=server_log, stderr=server_log)
        try:
            for _ in range(100):
                try:
                    if Client().call('GET', '/api/health')[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError('Isolated acceptance server did not start')
            run_cases(engine)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            engine.dispose()
        # Verify deployed cookie configuration against a real server as well.
        # The Secure cookie intentionally is not sent back over this HTTP probe.
        secure_process = subprocess.Popen(['uvicorn', 'difede_api.main:app', '--host', '127.0.0.1', '--port', '18763',
                                           '--no-access-log', '--log-level', 'error'],
                                          env={**os.environ, 'SESSION_COOKIE_SECURE': 'true'},
                                          stdout=server_log, stderr=server_log)
        try:
            for _ in range(100):
                try:
                    if Client().call('GET', '/api/health')[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            def cookie_attributes():
                cookie = Client(1).login_headers.get('Set-Cookie', '')
                require(all(part in cookie.lower() for part in ('secure', 'httponly', 'samesite=lax', 'path=/')), 'secure cookie attributes')
                require('domain=' not in cookie.lower() and 'difede_v3_session=' in cookie, 'V3 host-only separate cookie')
            check('Real HTTP production cookie attributes', cookie_attributes)
        finally:
            secure_process.terminate()
            secure_process.wait(timeout=10)
    report = {'database': 'difedegames_v3_test', 'transport': 'real HTTP / two Uvicorn workers / PostgreSQL',
              'checks': len(RESULTS), 'passed': sum(r['passed'] for r in RESULTS), 'results': RESULTS}
    print(json.dumps(report, indent=2))
    if os.environ.get('ACCEPTANCE_REPORT'):
        Path(os.environ['ACCEPTANCE_REPORT']).write_text(json.dumps(report, indent=2), encoding='utf-8')
    raise SystemExit(0 if all(r['passed'] for r in RESULTS) else 1)


if __name__ == '__main__':
    main()
