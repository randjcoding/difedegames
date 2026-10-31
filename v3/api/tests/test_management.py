from datetime import datetime, timedelta
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from conftest import new_game, save, signed_in
from difede_api.bootstrap_owner import bootstrap
from difede_api.db import utcnow
from difede_api.models import ActionToken, Audit, Game, GroupAdmin, LegacyRecord, Membership, Person, Score, User, WebSession
from difede_api.security import digest
from test_api import action


@pytest.fixture
def owner(app):
    with app.state.sessions.begin() as db:
        db.get(User, 1).site_role = 'super_admin'
        db.get(User, 1).is_owner = True
        db.get(User, 5).site_role = 'super_admin'
    return signed_in(app, 1)


def test_superadmin_global_visibility_and_private_account_fields(app, owner):
    other = signed_in(app, 4)
    game = new_game(other, group_id=2, viewer_group_ids=[], player_ids=[1, 4])
    assert owner.get(f'/api/games/{game["id"]}').json()['permissions']['can_manage'] is True
    assert signed_in(app, 2).get(f'/api/games/{game["id"]}').status_code == 404
    assert owner.get('/api/games').json()['total'] == 1
    assert {g['id'] for g in owner.get('/api/groups').json()['groups']} == {1, 2}
    assert owner.get('/api/admin/overview').json()['counts']['people'] == 6
    accounts = owner.get('/api/admin/accounts').json()
    assert accounts['total'] == 5 and 'password_hash' not in accounts['accounts'][0]
    assert signed_in(app, 2).get('/api/admin/accounts').status_code == 403
    assert owner.get('/api/me').json()['user']['is_owner'] is True


def test_preferences_registry_partial_updates_and_account_isolation(app, client):
    assert client.get('/api/me').json()['preferences'] == {'style': 'classic', 'palette': 'midnight-blue', 'favorite_game_slugs': [], 'favorite_person_ids': []}
    assert client.patch('/api/preferences', json={'favorite_person_ids': [2], 'favorite_game_slugs': ['sevens']}).status_code == 200
    preferences = client.patch('/api/preferences', json={'style': 'arcade'}).json()['preferences']
    assert preferences['favorite_person_ids'] == [2] and preferences['favorite_game_slugs'] == ['sevens']
    assert signed_in(app, 2).get('/api/me').json()['preferences']['favorite_person_ids'] == []
    assert client.patch('/api/preferences', json={'style': None}).status_code == 422
    assert client.patch('/api/preferences', json={'style': 'arbitrary-css'}).status_code == 422
    assert client.patch('/api/preferences', json={'favorite_person_ids': [9999]}).status_code == 403
    assert client.patch('/api/preferences', json={'favorite_game_slugs': ['not-a-game']}).status_code == 422


def test_profile_preferred_name_private_contact_and_nullable_email(app, client):
    result = client.patch('/api/profile', json={'name': 'Chosen Display', 'first_name': 'Legal', 'last_name': 'Private',
        'person_email': 'person@example.com', 'contact': {'city': 'Example City'}, 'show_full_last_name': False})
    assert result.status_code == 200
    profile = result.json()
    assert profile['person']['name'] == 'Chosen Display' and profile['user']['name'] == 'Person 1'
    me = client.get('/api/me').json()['user']
    assert me['display_name'] == 'Chosen Display' and me['name'] == 'Person 1'
    assert profile['person']['first_name'] == 'Legal' and profile['contact']['city'] == 'Example City'
    assert client.patch('/api/profile', json={'person_email': None, 'contact': {'city': None}}).json()['person']['email'] is None
    other = signed_in(app, 2)
    assert other.get('/api/profile').json()['person']['name'] == 'Test Person 2'


def test_password_change_revokes_all_actual_sessions(app):
    first, second = signed_in(app, 2), signed_in(app, 2)
    assert first.post('/api/auth/password', json={'current_password': 'wrong', 'new_password': 'DifferentPassword!123'}).status_code == 403
    changed = first.post('/api/auth/password', json={'current_password': 'ExamplePass!123', 'new_password': 'DifferentPassword!123'})
    assert changed.status_code == 200
    assert first.get('/api/me').status_code == second.get('/api/me').status_code == 401
    anonymous = TestClient(app, base_url='https://testserver')
    assert anonymous.post('/api/auth/login', json={'email': 'person2@example.com', 'password': 'DifferentPassword!123'}).status_code == 200


def test_hashed_single_use_reset_and_session_revocation(app, owner):
    target = signed_in(app, 2)
    link = owner.post('/api/admin/accounts/2/reset-link', json={})
    assert link.status_code == 200
    raw = link.json()['url'].rsplit('/', 1)[1]
    with app.state.sessions.begin() as db:
        assert db.get(ActionToken, digest(raw)).user_id == 2
        assert db.get(ActionToken, raw) is None
    anonymous = TestClient(app, base_url='https://testserver')
    assert anonymous.get('/api/auth/password-reset/' + raw).status_code == 200
    reset = anonymous.post('/api/auth/password-reset/' + raw, json={'password': 'ResetPassword!123'})
    assert reset.status_code == 200 and target.get('/api/me').status_code == 401
    assert anonymous.post('/api/auth/password-reset/' + raw, json={'password': 'AnotherPassword!123'}).status_code == 404
    assert 'ResetPassword' not in owner.get('/api/admin/audit').text and raw not in owner.get('/api/admin/audit').text
    fresh = owner.post('/api/admin/accounts/2/reset-link', json={}).json()['url'].rsplit('/', 1)[1]
    with app.state.sessions.begin() as db:
        db.get(ActionToken, digest(fresh)).expires_at = utcnow() - timedelta(seconds=1)
    assert anonymous.get('/api/auth/password-reset/' + fresh).status_code == 404


def test_unconfigured_reset_is_generic_and_does_not_invalidate_manual_link(app, owner, monkeypatch):
    monkeypatch.delenv('SMTP_HOST', raising=False)
    manual = owner.post('/api/admin/accounts/2/reset-link', json={}).json()['url'].rsplit('/', 1)[1]
    anonymous = TestClient(app, base_url='https://testserver')
    known = anonymous.post('/api/auth/password-reset', json={'email': 'person2@example.com'})
    unknown = anonymous.post('/api/auth/password-reset', json={'email': 'absent@example.com'})
    assert known.json() == unknown.json()
    assert anonymous.get('/api/auth/password-reset/' + manual).status_code == 200


def test_owner_role_and_last_lead_lockout_boundaries(app, owner):
    other_admin = signed_in(app, 5)
    assert other_admin.patch('/api/admin/accounts/2', json={'site_role': 'super_admin'}).status_code == 403
    assert other_admin.patch('/api/admin/accounts/1', json={'active': False}).status_code == 403
    assert other_admin.post('/api/admin/accounts/1/reset-link', json={}).status_code == 403
    assert owner.patch('/api/admin/accounts/1', json={'site_role': 'member'}).status_code == 403
    assert owner.request('DELETE', '/api/admin/accounts/1', json={'reason': 'Cannot delete owner'}).status_code == 403
    assert owner.patch('/api/admin/accounts/4', json={'active': False}).status_code == 409
    assert owner.patch('/api/admin/accounts/2', json={'site_role': 'super_admin'}).status_code == 200


def test_explicit_owner_bootstrap_uses_immutable_trusted_role(app):
    with app.state.sessions.begin() as db:
        user = db.get(User, 1)
        user.site_role = 'super_admin'
        db.add(LegacyRecord(source_table='users', source_key='1', payload={'role': 'super_admin'}))
        db.flush()
        bootstrap(db, 'PERSON1@EXAMPLE.COM')
        assert user.is_owner is True
    with app.state.sessions.begin() as db:
        db.get(User, 5).site_role = 'super_admin'
        db.get(User, 5).legacy = {'role': 'super_admin'}
        with pytest.raises(ValueError):
            bootstrap(db, 'person5@example.com')


def test_group_lead_cannot_mutate_shared_canonical_person(app, client):
    assert client.patch('/api/people/6', json={'name': 'Guest Preferred'}).status_code == 200
    with app.state.sessions.begin() as db:
        db.add(Membership(person_id=6, group_id=2))
    assert client.patch('/api/people/6', json={'name': 'Wrong Global Rename'}).status_code == 403
    assert client.post('/api/people/6/archive', json={'reason': 'Wrong global archive'}).status_code == 403
    assert client.patch('/api/people/2', json={'name': 'Account-owned'}).status_code == 403
    assert signed_in(app, 4).patch('/api/groups/1', json={'name': 'Cross-group'}).status_code == 403


def test_lead_handover_and_removed_lead_access(app, client):
    member = signed_in(app, 2)
    with app.state.sessions.begin() as db:
        mid = db.scalar(select(Membership.id).where(Membership.group_id == 1, Membership.person_id == 1))
    assert client.post(f'/api/memberships/{mid}/decide', json={'decision': 'remove'}).status_code == 409
    assert client.post('/api/groups/1/transfer-lead', json={'user_id': 2}).status_code == 200
    assert client.patch('/api/groups/1', json={'name': 'Old lead cannot change'}).status_code == 403
    assert member.patch('/api/groups/1', json={'name': 'New lead may change'}).status_code == 200
    assert member.post(f'/api/memberships/{mid}/decide', json={'decision': 'remove'}).status_code == 200
    with app.state.sessions.begin() as db:
        assert db.get(GroupAdmin, (1, 1)) is None


def test_active_game_blocks_archive_and_admin_reason_is_audited(app, owner):
    game = new_game(owner, player_ids=[1, 6])
    assert owner.post('/api/people/6/archive', json={'reason': 'Unfinished guest'}).status_code == 409
    assert owner.post('/api/groups/1/archive', json={'reason': 'Unfinished group'}).status_code == 409
    assert owner.request('DELETE', '/api/admin/accounts/2', json={'reason': 'Example lifecycle reason'}).status_code == 200
    assert 'Example lifecycle reason' in owner.get('/api/admin/audit').text


def test_corrections_atomic_versioned_preserve_original_and_date(app, client, monkeypatch):
    game = new_game(client)
    game = save(client, game, pid=1, score=1)
    game = save(client, game, pid=2, score=3)
    game = action(client, game, 'finish').json()
    with app.state.sessions.begin() as db:
        stored = db.get(Game, game['id'])
        stored.imported = True
        stored.legacy = {'original_results': [{'winner_id': 2, 'winning_score': 3, 'is_tie': False}]}
    command = {'operation_id': str(uuid.uuid4()), 'expected_revision': game['revision'], 'reason': 'Correct a transcribed score',
        'scores': [{'player_id': 1, 'round_number': 1, 'score': 9}]}
    ordinary = signed_in(app, 2)
    assert ordinary.post(f'/api/admin/games/{game["id"]}/corrections', json=command).status_code == 403
    result = client.post(f'/api/admin/games/{game["id"]}/corrections', json=command)
    assert result.status_code == 200, result.text
    fixed = result.json()
    assert datetime.fromisoformat(fixed['finished_at'].replace('Z', '+00:00')) == datetime.fromisoformat(game['finished_at'].replace('Z', '+00:00'))
    assert fixed['standings'][0]['player_id'] == 1
    assert fixed['imported_result']['winners'][0]['player_id'] == 2
    assert fixed['result_source'] == 'v3-correction'
    assert client.post(f'/api/admin/games/{game["id"]}/corrections', json=command).json() == fixed
    assert client.post(f'/api/admin/games/{game["id"]}/corrections', json=command | {'reason': 'Changed payload'}).status_code == 409
    assert client.post(f'/api/admin/games/{game["id"]}/corrections', json=command | {'operation_id': str(uuid.uuid4())}).status_code == 409
    from difede_api import management
    monkeypatch.setattr(management, 'record', lambda *args: (_ for _ in ()).throw(RuntimeError('isolated audit failure')))
    failed = client.post(f'/api/admin/games/{game["id"]}/corrections', json=command | {
        'operation_id': str(uuid.uuid4()), 'expected_revision': fixed['revision'],
        'scores': [{'player_id': 1, 'round_number': 1, 'score': 90}]})
    assert failed.status_code == 500
    current = client.get(f'/api/games/{game["id"]}').json()
    assert current['revision'] == fixed['revision']
    assert next(c for c in current['scores'] if c['player_id'] == 1)['value'] == 9


def test_group_detail_does_not_leak_canonical_private_profile_or_other_groups(app, client):
    with app.state.sessions.begin() as db:
        person = db.get(Person, 2)
        person.details = {'first_name': 'Private', 'last_name': 'Legal', 'email': 'private@example.com'}
        db.add(Membership(person_id=2, group_id=2))
    detail = client.get('/api/groups/1').json()
    person = next(p for p in detail['people'] if p['id'] == 2)
    assert person['group_ids'] == [1]
    assert 'account' not in person and 'email' not in person and 'first_name' not in person
