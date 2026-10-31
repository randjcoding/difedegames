from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from conftest import signed_in
from difede_api.catalog import CATALOG
from difede_api.models import Game, Person, Score, Seat, User, ViewerGroup


def completed(app, gid, *, slug='gin-rummy', players=(1, 2), totals=None,
              winners=(1,), direction='high', mode='rounds', title=None,
              status='completed', deleted=False, group_id=1, organizer_id=1,
              shared=True, original=False, originals=None, legacy_originals=None):
    totals = totals or {pid: 10 if pid in winners else 3 for pid in players}
    standings = [{'player_id': pid, 'name': 'Snapshot name is not a privacy source',
                  'total': totals.get(pid, 0), 'rank': 1 if pid in winners else 2,
                  'winner': pid in winners} for pid in players]
    result = {'official': True, 'standings': standings}
    if original:
        result.update(policy='v2-original', original_results=originals or [])
    when = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=gid)
    with app.state.sessions.begin() as db:
        db.add(Game(id=gid, slug=slug, title=title or CATALOG[slug]['name'],
                    status=status, deleted=deleted, revision=1, mode=mode,
                    scoring_direction=direction, group_id=group_id, organizer_id=organizer_id,
                    created_at=when, finished_at=when, imported=original or bool(legacy_originals),
                    result=result, legacy={'original_results': legacy_originals or originals or []}))
        db.flush()
        db.add_all([Seat(game_id=gid, person_id=pid, position=index) for index, pid in enumerate(players)])
        if shared:
            db.add(ViewerGroup(game_id=gid, group_id=group_id))


def players(response):
    assert response.status_code == 200, response.text
    return {row['person_id']: row for row in response.json()['players']}


def test_tied_losers_are_losses_and_unknown_outcomes_do_not_guess(app, client):
    completed(app, 100, players=(1, 2, 3), totals={1: 0, 2: 0, 3: 9}, winners=(1, 2), direction='low')
    completed(app, 101, players=(1, 3), totals={1: 5, 3: 0}, winners=(3,), direction='low')
    # Snapshot totals/flags suggest a winner, but the original result was unknown.
    completed(app, 102, players=(1, 3), winners=(1,), original=True, originals=[])
    rows = players(client.get('/api/leaderboards'))
    assert rows[3]['wins'] == 1 and rows[3]['losses'] == 1 and rows[3]['ties'] == 0
    assert rows[1]['shared_wins'] == 1 and rows[1]['solo_wins'] == 0
    assert rows[1]['played'] == 3 and rows[1]['known_results'] == 2 and rows[1]['unknown_results'] == 1
    assert rows[1]['win_percentage'] == 50.0 and rows[1]['win_percentage_denominator'] == 2
    history = client.get('/api/statistics').json()
    assert history['summary']['unknown_results'] == 1
    unknown = next(game for game in history['history'] if game['id'] == 102)
    assert unknown['winners'] == [] and unknown['result_known'] is False


def test_two_unknown_imported_results_remain_unknown_and_percentage_null(app, client):
    for gid in (100, 101):
        completed(app, gid, original=True, originals=[], winners=(1,))
    rows = players(client.get('/api/leaderboards'))
    assert all(row['unknown_results'] == 2 and row['wins'] == 0 and row['losses'] == 0
               and row['win_percentage'] is None for row in rows.values())


def test_original_result_rows_authoritative_until_current_official_completion(app, client):
    completed(app, 100, winners=(1,), totals={1: 100, 2: 3}, original=True,
              originals=[{'winner_id': 2, 'winning_score': 700, 'is_tie': True}])
    completed(app, 101, winners=(1,), totals={1: 12, 2: 3},
              legacy_originals=[{'winner_id': 2, 'winning_score': 999, 'is_tie': True}])
    rows = players(client.get('/api/leaderboards'))
    assert rows[2]['shared_wins'] == 1 and rows[2]['solo_wins'] == 0
    assert rows[1]['solo_wins'] == 1 and rows[1]['shared_wins'] == 0
    assert rows[2]['winning_scores'][0]['average'] == 700
    latest = client.get('/api/hall-of-fame').json()['latest_game']
    assert latest['id'] == 101 and latest['winners'][0]['player_id'] == 1


def test_streaks_use_players_own_games_and_unknowns_interrupt(app, client):
    completed(app, 100, players=(1, 2), winners=(1,))
    completed(app, 101, players=(2, 3), winners=(2,))  # unrelated to person1
    completed(app, 102, players=(1, 3), winners=(1, 3))
    completed(app, 103, players=(1, 2), winners=(1,), slug='skyjo', direction='low')
    completed(app, 104, players=(1, 2), winners=(2,))
    completed(app, 105, players=(1, 2), winners=(1,))
    completed(app, 106, players=(1, 2), winners=(1,), original=True, originals=[])
    completed(app, 107, players=(1, 2), winners=(1,))
    completed(app, 108, players=(1, 2), winners=(2,), deleted=True)
    completed(app, 109, players=(1, 2), winners=(1,))
    response = client.get('/api/streaks')
    assert response.status_code == 200
    row = next(row for row in response.json()['streaks'] if row['person_id'] == 1)
    assert row['length'] == 3 and row['shared_wins'] == 1
    assert row['start_game_id'] == 100 and row['end_game_id'] == 103


def test_all_time_is_authorized_not_public_private_group_history(app, client):
    completed(app, 100)
    completed(app, 101, players=(4, 6), organizer_id=4, group_id=2,
              shared=False, title='Secret private game')
    watcher = signed_in(app, 3)
    assert watcher.get('/api/statistics').json()['summary']['games'] == 1
    assert 'Secret private game' not in watcher.get('/api/hall-of-fame').text
    assert watcher.get('/api/statistics?scope=site').status_code == 403
    assert watcher.get('/api/groups/2/statistics').status_code == 404
    assert watcher.get('/api/people/4/statistics').status_code == 404
    with app.state.sessions.begin() as db:
        db.get(User, 3).site_role = 'super_admin'
    admin = signed_in(app, 3)
    data = admin.get('/api/statistics?scope=site').json()
    assert data['summary']['games'] == 2 and data['scope'] == 'site'
    assert admin.get('/api/groups/2/statistics').status_code == 200


def test_deleted_active_incomplete_games_excluded_and_group_host_scope(app, client):
    completed(app, 100)
    completed(app, 101, group_id=2)
    completed(app, 102, deleted=True)
    completed(app, 103, status='active')
    completed(app, 104, status='incomplete')
    assert client.get('/api/statistics').json()['summary']['games'] == 2
    group = client.get('/api/groups/1/statistics').json()
    assert group['summary']['games'] == 1
    assert group['pagination']['total'] == 1 and group['player_pagination']['total'] == 2
    person = client.get('/api/people/1/statistics').json()
    assert person['summary']['played'] == 2  # person lifetime spans both authorized host groups


def test_exact_basic_title_and_literal_q_filters_with_pagination(app, client):
    completed(app, 100, slug='basic-other', title='Family % cards', direction='high')
    completed(app, 101, slug='basic-other', title='Family + cards', direction='low')
    completed(app, 102, slug='basic-other', title='Family % cards', direction='low')
    completed(app, 103, slug='uno-classic', title='Family other', direction='low')
    exact = client.get('/api/statistics', params={'title': 'Family % cards', 'page_size': 1}).json()
    assert exact['summary']['games'] == 2 and len(exact['history']) == 1
    assert exact['pagination']['total'] == 2 and exact['pagination']['pages'] == 2
    assert len(exact['categories']) == 2
    literal = client.get('/api/statistics', params={'q': '%'}).json()
    assert literal['summary']['games'] == 2
    narrowed = client.get('/api/statistics?slug=basic-other&mode=rounds&direction=low').json()
    assert narrowed['summary']['games'] == 2
    assert client.get('/api/statistics?slug=uno-classic&title=anything').status_code == 422


def test_hall_comparisons_partition_points_modes_directions_and_custom_titles(app, client):
    completed(app, 100, slug='uno-classic', totals={1: 1000, 2: 10}, winners=(1,), direction='high')
    completed(app, 101, slug='uno-classic', totals={1: 3, 2: 100}, winners=(1,), direction='low')
    completed(app, 102, slug='kings-corner', totals={1: 10, 2: 40}, winners=(1,), direction='low')
    completed(app, 103, slug='kings-corner', totals={1: 600, 2: 0}, winners=(1,), mode='single', direction='high')
    completed(app, 104, slug='basic-other', title='Tiny contest', totals={1: 2, 2: 0})
    completed(app, 105, slug='basic-other', title='Large contest', totals={1: 20000, 2: 0})
    hall = client.get('/api/hall-of-fame?limit=20').json()
    uno = [record for record in hall['best_scores'] if record['category']['slug'] == 'uno-classic']
    assert {(row['category']['scoring_direction'], row['value']) for row in uno} == {('high', 1000), ('low', 3)}
    kings = [record for record in hall['best_scores'] if record['category']['slug'] == 'kings-corner']
    assert {(row['category']['score_metric'], row['value']) for row in kings} == {('round_total', 10), ('strength_of_win', 600)}
    basics = [record for record in hall['biggest_beatdowns'] if record['category']['slug'] == 'basic-other']
    assert len(basics) == 2 and len({row['category']['id'] for row in basics}) == 2
    # No single mixed-type strongest-score field or rank is fabricated.
    assert 'biggest_beatdown' not in hall
    rows = players(client.get('/api/leaderboards?slug=kings-corner'))
    assert len(rows[1]['winning_scores']) == 2


def test_worst_five_crowns_is_saved_hand_and_trouble_original_strength(app, client):
    completed(app, 100, slug='five-crowns', direction='low', totals={1: 10, 2: 150}, winners=(1,))
    completed(app, 101, slug='five-crowns', direction='low', totals={1: 40, 2: 1000}, winners=(1,))
    completed(app, 102, slug='trouble', mode='single', totals={1: 25, 2: 0}, original=True,
              originals=[{'winner_id': 1, 'winning_score': 95, 'is_tie': False}])
    with app.state.sessions.begin() as db:
        db.add_all([Score(game_id=100, person_id=2, round_number=4, value=110, version=1),
                    Score(game_id=101, person_id=2, round_number=3, value=20, version=1)])
    hall = client.get('/api/hall-of-fame').json()
    hand = hall['worst_five_crowns_hands'][0]
    assert hand['game']['id'] == 100 and hand['round_number'] == 4 and hand['value'] == 110
    assert hall['trouble_strength'][0]['value'] == 95
    assert all(record['category']['slug'] != 'trouble' for record in hall['best_scores'] + hall['worst_scores'])


def test_per_type_win_leaders_and_directional_best_worst_winning_averages(app, client):
    completed(app, 100, totals={1: 10, 2: 3}, winners=(1,))
    completed(app, 101, totals={1: 20, 2: 3}, winners=(1,))
    completed(app, 102, totals={1: 3, 2: 5}, winners=(2,))
    completed(app, 103, slug='skyjo', direction='low', totals={1: 0, 2: -5}, winners=(2,))
    completed(app, 104, slug='skyjo', direction='low', totals={1: -10, 2: 0}, winners=(1,))
    hall = client.get('/api/hall-of-fame').json()
    leaders = {row['category']['slug']: row['leaders'] for row in hall['win_leaders']}
    assert [(row['person_id'], row['wins']) for row in leaders['gin-rummy']] == [(1, 2)]
    assert {row['person_id'] for row in leaders['skyjo']} == {1, 2}
    best = {row['category']['slug']: row['value'] for row in hall['best_scores']}
    worst = {row['category']['slug']: row['value'] for row in hall['worst_scores']}
    assert best == {'gin-rummy': 20, 'skyjo': -10}
    assert worst == {'gin-rummy': 3, 'skyjo': 0}
    gin = players(client.get('/api/leaderboards?slug=gin-rummy'))[1]['winning_scores'][0]
    assert gin['scored_wins'] == 2 and gin['average'] == 15 and gin['best'] == 20


def test_single_player_basic_and_winner_only_do_not_create_point_records(app, client):
    completed(app, 100, slug='basic-other', players=(1,), totals={1: 99999}, winners=(1,))
    completed(app, 101, slug='basic-other', players=(1, 2), mode='winner', totals={1: 0, 2: 0}, winners=(1,))
    hall = client.get('/api/hall-of-fame').json()
    assert not hall['best_scores'] and not hall['worst_scores'] and not hall['biggest_beatdowns']
    assert client.get('/api/leaderboards').json()['players'][0]['wins'] == 2


def test_privacy_masks_other_surnames_and_purged_people(app, client):
    with app.state.sessions.begin() as db:
        person = db.get(Person, 2)
        person.name = 'Private Surname'
        person.privacy = {'show_full_last_name': False}
        db.get(Person, 3).name = 'Former Secret'
        db.get(Person, 3).purged = True
    completed(app, 100, players=(1, 2, 3))
    rows = players(client.get('/api/leaderboards'))
    assert rows[2]['name'] == 'Private S.' and rows[3]['name'] == 'Deleted player'
    assert 'Surname' not in client.get('/api/hall-of-fame').text
    assert 'Snapshot name' not in client.get('/api/statistics').text
    own = signed_in(app, 2).get('/api/people/2/statistics').json()
    assert own['person']['name'] == 'Private Surname'


@pytest.mark.parametrize('query', ['page_size=101', 'page=0', 'q=' + 'x' * 121,
                                  'group_id=999999999999999999999', 'person_id=-1', 'direction=sideways'])
def test_query_inputs_bounded(client, query):
    assert client.get('/api/statistics?' + query).status_code == 422


@pytest.mark.parametrize('path', ['/api/statistics', '/api/leaderboards', '/api/streaks', '/api/hall-of-fame',
                                  '/api/people/1/statistics', '/api/groups/1/statistics'])
def test_analytics_requires_session(app, path):
    assert TestClient(app, base_url='https://testserver').get(path).status_code == 401


@pytest.mark.parametrize('endpoint', ['/api/statistics', '/api/leaderboards'])
def test_batch_query_budget_and_no_board_score_loading(app, client, endpoint):
    for gid in range(200, 280):
        completed(app, gid)
    statements = []
    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(app.state.engine, 'before_cursor_execute', capture)
    try:
        response = client.get(endpoint + '?page_size=5')
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', capture)
    assert response.status_code == 200, response.text
    assert len(statements) <= 12
    assert not any('score_cells' in statement for statement in statements)
    assert 'scores' not in response.json().get('history', [{}])[0]
