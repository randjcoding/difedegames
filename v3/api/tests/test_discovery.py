from sqlalchemy import select

from conftest import signed_in
from difede_api.models import Group, Membership, Person, User


def test_saved_setups_default_and_no_implicit_sharing(app, client):
    body = {'group_id': 1, 'slug': 'gin-rummy', 'name': 'Family setup', 'player_ids': [1, 2], 'is_default': True}
    first = client.post('/api/layouts', json=body)
    assert first.status_code == 200, first.text
    first = first.json()
    second = client.post('/api/layouts', json=body | {'name': 'Second setup'}).json()
    rows = client.get('/api/layouts?group_id=1&slug=gin-rummy').json()['layouts']
    assert len(rows) == 2 and sum(r['is_default'] for r in rows) == 1
    assert next(r for r in rows if r['is_default'])['id'] == second['id']
    assert client.post(f'/api/layouts/{first["id"]}/set-default', json={}).status_code == 200
    game = client.post(f'/api/layouts/{first["id"]}/start', json={})
    assert game.status_code == 200 and game.json()['viewer_group_ids'] == []
    assert signed_in(app, 3).get(f'/api/games/{game.json()["id"]}').status_code == 404
    assert signed_in(app, 2).post('/api/layouts', json=body | {'name': 'No lead'}).status_code == 403
    assert signed_in(app, 4).get('/api/layouts?group_id=1').status_code == 403
    assert client.request('DELETE', f'/api/layouts/{second["id"]}', json={}).status_code == 200


def test_saved_setup_revalidates_person_membership_at_use(app, client):
    setup = client.post('/api/layouts', json={'group_id': 1, 'slug': 'gin-rummy', 'name': 'Roster', 'player_ids': [1, 6]}).json()
    with app.state.sessions.begin() as db:
        db.scalar(select(Membership).where(Membership.person_id == 6)).status = 'removed'
    assert client.post(f'/api/layouts/{setup["id"]}/start', json={}).status_code == 422


def test_directory_honors_discovery_minors_roster_and_private_fields(app, client):
    stranger = signed_in(app, 4)
    with app.state.sessions.begin() as db:
        person = db.get(Person, 6)
        person.name = 'Findable Guest'
        person.details = {'first_name': 'Legal', 'last_name': 'PrivateSurname', 'email': 'private@example.com'}
        person.privacy = {'is_discoverable': True, 'show_full_last_name': False, 'is_minor': True}
        db.get(Group, 1).name = 'Findable Group'
        db.get(Group, 1).settings = {'is_discoverable': True, 'show_roster': True}
    assert stranger.get('/api/directory/people?q=Findable').json()['people'] == []
    assert len(client.get('/api/directory/people?q=Findable').json()['people']) == 1
    with app.state.sessions.begin() as db:
        db.get(Person, 6).privacy = {'is_discoverable': True, 'show_full_last_name': False, 'is_minor': False}
    found = stranger.get('/api/directory/people?q=Findable').json()['people'][0]
    assert found['name'] == 'Findable Guest P.'
    assert 'email' not in found and 'account_id' not in found and 'PrivateSurname' not in found['name']
    with app.state.sessions.begin() as db:
        db.get(Group, 1).settings = {'is_discoverable': False, 'show_roster': False}
    assert stranger.get('/api/directory/people?q=Findable').json()['people'] == []
    assert stranger.get('/api/directory/groups?q=Findable').json()['groups'] == []
    assert len(client.get('/api/directory/groups?q=Findable').json()['groups']) == 1
    assert stranger.post('/api/groups/1/request-join', json={}).status_code == 404


def test_directory_bounds_and_shared_per_account_budget(client):
    assert client.get('/api/directory/people?q=x').status_code == 422
    assert client.get('/api/directory/groups?q=%20%20').status_code == 422
    for _ in range(30):
        assert client.get('/api/directory/groups?q=First').status_code == 200
    assert client.get('/api/directory/people?q=Test').status_code == 429


def test_linked_account_membership_requires_consent_unless_site_admin(app, client):
    with app.state.sessions.begin() as db:
        db.add(Membership(person_id=4, group_id=1))
    leader = signed_in(app, 4)
    assert leader.post('/api/groups/2/memberships', json={'person_id': 2}).status_code == 409
    requested = client.post('/api/groups/2/request-join', json={})
    # Already active membership cannot be used as a new recruitment request.
    assert requested.status_code == 200
    with app.state.sessions.begin() as db:
        db.scalar(select(Membership).where(Membership.person_id == 2, Membership.group_id == 1)).status = 'active'
        db.add(Membership(person_id=2, group_id=2, status='requested'))
    assert leader.post('/api/groups/2/memberships', json={'person_id': 2}).status_code == 200


def test_lead_cannot_reapprove_removed_linked_membership(app, client):
    with app.state.sessions.begin() as db:
        row = db.scalar(select(Membership).where(Membership.person_id == 2, Membership.group_id == 1))
        row.status = 'removed'
        mid = row.id
    assert client.post(f'/api/memberships/{mid}/decide', json={'decision': 'approve'}).status_code == 409
    with app.state.sessions.begin() as db:
        assert db.get(Membership, mid).status == 'removed'


def test_hidden_early_matches_do_not_crowd_out_visible_directory_results(app, client):
    stranger = signed_in(app, 4)
    with app.state.sessions.begin() as db:
        for i in range(210):
            p = Person(name=f'Crowd AA {i:03}', privacy={'is_discoverable': False})
            db.add(p)
            db.flush()
            db.add(Membership(person_id=p.id, group_id=1))
        visible = Person(name='Crowd ZZ', details={'last_name': 'Secret'}, privacy={'is_discoverable': True})
        db.add(visible)
        db.flush()
        db.add(Membership(person_id=visible.id, group_id=1))
        db.get(Person, 6).name = 'Riley Secret'
        db.get(Person, 6).details = {'last_name': 'Secret'}
        db.get(Person, 6).privacy = {'is_discoverable': True, 'show_full_last_name': False}
    rows = stranger.get('/api/directory/people?q=Crowd').json()['people']
    assert len(rows) == 1 and rows[0]['name'] == 'Crowd ZZ S.'
    result = stranger.get('/api/directory/people?q=Riley').json()['people'][0]
    assert result['name'] == 'Riley S.'
    assert stranger.get('/api/directory/people?q=Secret').json()['people'] == []


def test_explicit_null_privacy_is_closed_but_missing_defaults_remain_consistent(app, client):
    stranger = signed_in(app, 4)
    with app.state.sessions.begin() as db:
        person = db.get(Person, 6)
        person.name = 'Nullable Person'
        person.privacy = {'is_discoverable': None, 'is_minor': False}
        db.get(Group, 1).name = 'Nullable Group'
        db.get(Group, 1).settings = {'is_discoverable': None, 'show_roster': None}
    assert stranger.get('/api/directory/groups?q=Nullable').json()['groups'] == []
    assert stranger.get('/api/directory/people?q=Nullable').json()['people'] == []
    group = client.get('/api/groups/1').json()['group']
    assert group['settings']['is_discoverable'] is False and group['settings']['show_roster'] is False
    with app.state.sessions.begin() as db:
        db.get(Group, 1).settings = {}
        db.get(Person, 6).privacy = {}
    assert stranger.get('/api/directory/groups?q=Nullable').json()['groups'][0]['show_roster'] is True
    assert len(stranger.get('/api/directory/people?q=Nullable').json()['people']) == 1
    with app.state.sessions.begin() as db:
        db.get(Person, 6).privacy = {'is_discoverable': True, 'is_minor': None}
    assert stranger.get('/api/directory/people?q=Nullable').json()['people'] == []
