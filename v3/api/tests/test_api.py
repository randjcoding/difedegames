import json
import uuid

import bcrypt
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from difede_api import games
from difede_api.catalog import CATALOG
from difede_api.models import Audit, Game, GameEvent, GroupAdmin, Membership, Operation, Score, User, WebSession
from difede_api.security import verify_password
from conftest import new_game, save, signed_in


def action(client, game, name, **extra):
    return client.post(f'/api/games/{game["id"]}/{name}', json={
        'operation_id': str(uuid.uuid4()), 'expected_revision': game['revision'], **extra})


def test_catalog_and_auth_headers(app, client):
    anonymous = TestClient(app, base_url='https://testserver')
    assert len(anonymous.get('/api/catalog').json()['games']) == 10
    assert anonymous.get('/api/people').status_code == 401
    response = client.get('/api/me')
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.json()['user']['person_id'] == 1
    login = anonymous.post('/api/auth/login', json={'email': 'person1@example.com', 'password': 'ExamplePass!123'})
    cookie = login.headers['set-cookie']
    assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=lax' in cookie
    assert anonymous.post('/api/groups', json={'name': 'No CSRF'}).status_code == 403
    assert client.post('/api/groups', json={'name': 'Bad origin'}, headers={'Origin': 'https://bad.example'}).status_code == 403


def test_mixed_hashes_and_account_revocation(app, client):
    hashed = bcrypt.hashpw(b'ExamplePass!123', bcrypt.gensalt(rounds=4)).decode()
    assert verify_password('ExamplePass!123', hashed)
    assert not verify_password('wrong', hashed)
    with app.state.sessions.begin() as db:
        db.get(User, 2).password_hash = hashed
    assert signed_in(app, 2).get('/api/me').status_code == 200
    with app.state.sessions.begin() as db:
        db.get(User, 1).active = False
    assert client.get('/api/me').status_code == 401


def test_login_rate_limit(app):
    c = TestClient(app, base_url='https://testserver')
    for _ in range(10):
        assert c.post('/api/auth/login', json={'email': 'person1@example.com', 'password': 'wrong'}).status_code == 401
    assert c.post('/api/auth/login', json={'email': 'person1@example.com', 'password': 'wrong'}).status_code == 429


def test_participant_scorekeeper_watcher_and_explicit_sharing(app, client):
    game = new_game(client, scorekeeper_ids=[5])
    watcher = signed_in(app, 3)
    outsider = signed_in(app, 4)
    keeper = signed_in(app, 5)
    assert watcher.get(f'/api/games/{game["id"]}').status_code == 200
    assert watcher.get(f'/api/games/{game["id"]}').json()['permissions']['can_score'] is False
    assert outsider.get(f'/api/games/{game["id"]}').status_code == 404
    payload = {'operation_id': str(uuid.uuid4()), 'player_id': 1, 'round_number': 1, 'score': 2, 'expected_version': 0}
    assert watcher.post(f'/api/games/{game["id"]}/scores', json=payload).status_code == 403
    assert keeper.post(f'/api/games/{game["id"]}/scores', json=payload).status_code == 200
    private = new_game(client, viewer_group_ids=[])
    assert watcher.get(f'/api/games/{private["id"]}').status_code == 404
    participant = signed_in(app, 2)
    with app.state.sessions.begin() as db:
        db.scalar(select(Membership).where(Membership.person_id == 2)).status = 'removed'
    assert participant.get(f'/api/games/{private["id"]}').status_code == 200


def test_versions_idempotency_and_clear_tombstone(client):
    game = new_game(client)
    game = save(client, game, score=0, op='same-operation-001')
    first = game
    # A different cell changes the game revision but not the first cell's version.
    game = save(client, game, pid=2, score=3)
    replay = client.post(f'/api/games/{game["id"]}/scores', json={'operation_id': 'same-operation-001',
        'player_id': 1, 'round_number': 1, 'score': 0, 'expected_version': 0})
    assert replay.json() == first
    conflict = client.post(f'/api/games/{game["id"]}/scores', json={'operation_id': 'same-operation-001',
        'player_id': 1, 'round_number': 1, 'score': 9, 'expected_version': 0})
    assert conflict.status_code == 409
    game = save(client, game, score=None, version=1)
    cell = next(c for c in game['scores'] if c['player_id'] == 1)
    assert cell['value'] is None and cell['version'] == 2
    stale = client.post(f'/api/games/{game["id"]}/scores', json={'operation_id': str(uuid.uuid4()),
        'player_id': 1, 'round_number': 1, 'score': 8, 'expected_version': 1})
    assert stale.status_code == 409 and stale.json()['code'] == 'cell_conflict'


def test_zero_missing_ties_early_reopen_delete(client, app):
    game = new_game(client)
    rejected = action(client, game, 'finish')
    assert rejected.status_code == 422 and len(rejected.json()['missing']) == 2
    game = save(client, game)
    game = save(client, game, pid=2)
    assert action(client, game, 'rounds').status_code == 200
    game = client.get(f'/api/games/{game["id"]}').json()
    # The wholly empty trailing hand is excluded, while explicit zeros count.
    complete = action(client, game, 'finish')
    assert complete.status_code == 200, complete.text
    game = complete.json()
    assert all(r['winner'] for r in game['standings'])
    assert action(client, game, 'finish').status_code == 409
    reopened = action(client, game, 'reopen')
    assert reopened.status_code == 200
    game = reopened.json()
    early = action(client, game, 'finish', early=True)
    assert early.status_code == 200 and early.json()['status'] == 'incomplete'
    assert not any(r['winner'] for r in early.json()['standings'])
    game = early.json()
    body = {'operation_id': str(uuid.uuid4()), 'expected_revision': game['revision']}
    assert client.request('DELETE', f'/api/games/{game["id"]}', json=body).status_code == 200
    assert client.request('DELETE', f'/api/games/{game["id"]}', json=body).status_code == 200
    assert client.get(f'/api/games/{game["id"]}').status_code == 404


@pytest.mark.parametrize('slug', list(CATALOG))
def test_all_game_creation_and_policies(client, slug):
    game = new_game(client, slug)
    assert game['slug'] == slug
    assert game['revision'] == 1
    if game['mode'] != 'rounds':
        assert client.post(f'/api/games/{game["id"]}/scores', json={'operation_id': str(uuid.uuid4()),
            'player_id': 1, 'round_number': 1, 'score': 1, 'expected_version': 0}).status_code == 422
        return
    for number in [r['number'] for r in game['rounds']]:
        game = save(client, game, pid=1, score=0, number=number)
        game = save(client, game, pid=2, score=1, number=number)
    result = action(client, game, 'finish')
    assert result.status_code == 200, result.text
    winner = next(r for r in result.json()['standings'] if r['winner'])
    assert winner['player_id'] == (1 if game['scoring_direction'] == 'low' else 2)


def test_negative_policy_roster_round_validation_and_flip(client):
    game = new_game(client, 'skyjo')
    assert save(client, game, score=-20)['scores'][0]['value'] == -20
    gin = new_game(client)
    for pid, num, val in [(1, 1, -1), (6, 1, 5), (1, 2, 5)]:
        assert client.post(f'/api/games/{gin["id"]}/scores', json={'operation_id': str(uuid.uuid4()),
            'player_id': pid, 'round_number': num, 'score': val, 'expected_version': 0}).status_code == 422
    flip = new_game(client, 'uno-flip')
    response = client.patch(f'/api/games/{flip["id"]}/rounds/1', json={'operation_id': str(uuid.uuid4()),
        'expected_revision': flip['revision'], 'side': 'dark'})
    assert response.status_code == 200 and response.json()['rounds'][0]['side'] == 'dark'
    assert action(client, flip, 'rounds').status_code == 409


def test_trouble_exact_peg_formula_and_metadata(client):
    game = new_game(client, 'trouble')
    pegs = [{'player_id': 1, 'color': 'red', 'finish': 4, 'board': 0, 'home': 0},
            {'player_id': 2, 'color': 'blue', 'finish': 1, 'board': 1, 'home': 2}]
    bad = action(client, game, 'finish', winner_id=1, details={'pegs': pegs[:1]})
    assert bad.status_code == 422
    result = action(client, game, 'finish', winner_id=1, details={'pegs': pegs})
    assert result.status_code == 200, result.text
    assert next(c for c in result.json()['scores'] if c['player_id'] == 1)['value'] == 50
    assert result.json()['players'][0]['color'] == 'red'
    assert result.json()['scores'][1]['metadata']['pegs']['home'] == 2


def test_kings_single_and_basic_winner(client):
    game = new_game(client, 'kings-corner', mode='single', player_ids=[1, 2, 3])
    result = action(client, game, 'finish', winner_id=1,
                    details={'penalties': [{'player_id': 2, 'score': 3}, {'player_id': 3, 'score': 7}]})
    assert result.status_code == 200, result.text
    assert next(c for c in result.json()['scores'] if c['player_id'] == 1)['value'] == 10
    assert result.json()['scoring_direction'] == 'high'
    assert result.json()['standings'][0]['player_id'] == 1
    basic = new_game(client, 'basic-other', mode='winner', title='Family custom game')
    result = action(client, basic, 'finish', winner_id=2)
    assert result.status_code == 200 and result.json()['scores'] == []
    assert next(r for r in result.json()['standings'] if r['winner'])['total'] == 0
    assert result.json()['standings'][0]['player_id'] == 2


def test_transaction_rolls_back_score_audit_event_and_operation(app, client, monkeypatch):
    game = new_game(client)
    def fail(*args):
        raise RuntimeError('isolated failure before durable event')
    monkeypatch.setattr(games, 'record', fail)
    response = client.post(f'/api/games/{game["id"]}/scores', json={'operation_id': str(uuid.uuid4()),
        'player_id': 1, 'round_number': 1, 'score': 7, 'expected_version': 0})
    assert response.status_code == 500
    with app.state.sessions.begin() as db:
        assert db.get(Game, game['id']).revision == 1
        assert db.scalar(select(func.count()).select_from(Score)) == 0
        assert db.scalar(select(func.count()).select_from(Operation)) == 0
        assert db.scalar(select(func.count()).select_from(GameEvent)) == 1


def test_guest_claim_preserves_history_and_multi_group_identity(app, client):
    game = new_game(client, player_ids=[1, 6])
    invite = client.post('/api/groups/1/invites', json={'person_id': 6})
    assert invite.status_code == 200
    token = invite.json()['url'].rsplit('/', 1)[1]
    c = TestClient(app, base_url='https://testserver')
    accepted = c.post(f'/api/invites/{token}/accept', json={'email': 'guest@example.com', 'password': 'NewGuestPass!123'})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['user']['person_id'] == 6
    c.headers['X-CSRF-Token'] = accepted.json()['csrf_token']
    assert c.get(f'/api/games/{game["id"]}').json()['permissions']['can_score'] is True
    assert c.post(f'/api/invites/{token}/accept', json={}).status_code == 404
    # Ordinary signed-in join adds membership, never another person identity.
    second_group = client.post('/api/groups', json={'name': 'Friends'}).json()
    second = client.post(f'/api/groups/{second_group["id"]}/invites', json={}).json()
    token2 = second['url'].rsplit('/', 1)[1]
    joined = c.post(f'/api/invites/{token2}/accept', json={})
    assert joined.status_code == 200
    assert joined.json()['user']['person_id'] == 6 and len(joined.json()['groups']) == 2


def test_bound_invite_does_not_merge_existing_identity(app, client):
    invite = client.post('/api/groups/1/invites', json={'person_id': 6}).json()
    token = invite['url'].rsplit('/', 1)[1]
    other = signed_in(app, 2)
    assert other.post(f'/api/invites/{token}/accept', json={}).status_code == 409


def test_no_public_registration_or_unscoped_people(app, client):
    outsider = signed_in(app, 4)
    assert {p['id'] for p in outsider.get('/api/people').json()['people']} == {1, 4}
    assert outsider.post('/api/people', json={'name': 'Unauthorized', 'group_id': 1}).status_code == 403
    assert client.post('/api/auth/register', json={}).status_code == 404


def test_cross_group_participant_does_not_share_entire_group(app, client):
    with app.state.sessions.begin() as db:
        member = db.scalar(select(Membership).where(Membership.person_id == 3))
        member.group_id = 2
    game = new_game(client, player_ids=[1, 4], viewer_group_ids=[])
    participant = signed_in(app, 4)
    other_group_member = signed_in(app, 3)
    assert participant.get(f'/api/games/{game["id"]}').json()['permissions']['can_score'] is True
    assert other_group_member.get(f'/api/games/{game["id"]}').status_code == 404
    # An assigned nonplaying scorekeeper also needs no implicit group audience.
    keeper_game = new_game(client, viewer_group_ids=[], scorekeeper_ids=[4])
    assert participant.get(f'/api/games/{keeper_game["id"]}').json()['permissions']['can_score'] is True
    assert other_group_member.get(f'/api/games/{keeper_game["id"]}').status_code == 404


def test_list_pagination_summaries_and_dashboard(app, client):
    game = new_game(client)
    game2 = new_game(client)
    page = client.get('/api/games?limit=1&offset=0').json()
    assert page['total'] == 2 and len(page['games']) == 1
    assert page['games'][0]['rounds'] == [] and page['games'][0]['scores'] == []
    assert len(client.get(f'/api/games/{game["id"]}').json()['rounds']) == 1
    assert client.get('/api/games?limit=0').status_code == 422
    assert len(client.get('/api/dashboard').json()['active']) == 2


def test_invite_revoked_by_guest_membership_removal(app, client):
    invite = client.post('/api/groups/1/invites', json={'person_id': 6}).json()
    token = invite['url'].rsplit('/', 1)[1]
    with app.state.sessions.begin() as db:
        db.scalar(select(Membership).where(Membership.person_id == 6)).status = 'removed'
    anonymous = TestClient(app, base_url='https://testserver')
    assert anonymous.get(f'/api/invites/{token}').status_code == 404
    assert anonymous.post(f'/api/invites/{token}/accept', json={}).status_code == 404


def test_guest_only_table_requires_explicit_scorekeeper(client):
    body = {'slug': 'basic-other', 'player_ids': [6], 'group_id': 1, 'viewer_group_ids': []}
    response = client.post('/api/games', json=body)
    assert response.status_code == 422 and response.json()['code'] == 'scorer_required'
    response = client.post('/api/games', json=body | {'scorekeeper_ids': [1]})
    assert response.status_code == 200 and response.json()['permissions']['can_score'] is True
