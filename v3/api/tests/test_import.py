from copy import deepcopy
from datetime import datetime

import pytest
from sqlalchemy import select

from difede_api.db import Base, make_engine, make_sessions
from difede_api.import_legacy import import_rows, validate_source
from difede_api.models import Game, LegacyRecord, Membership, Person, Score, User


def snapshot():
    return {
        'players': [
            {'id': 10, 'first_name': 'First', 'last_name': 'HiddenSurname', 'display_name': 'Chosen Name',
             'created_by_user_id': 4, 'family_id': 2, 'show_full_last_name': False, 'is_minor': True,
             'is_discoverable': False, 'email_verified': False, 'archived_at': None, 'purged_at': None},
            {'id': 20, 'first_name': 'Second', 'last_name': 'AlsoHidden', 'display_name': None,
             'created_by_user_id': 4, 'family_id': 2, 'archived_at': datetime(2026, 1, 1), 'purged_at': None},
        ],
        'users': [{'id': 4, 'email': 'old@example.com', 'password_hash': '$2b$old-hash-bytes-unchanged',
                   'first_name': 'First', 'last_name': 'HiddenSurname', 'player_id': 10,
                   'is_active': True, 'is_verified': True, 'is_approved': True, 'family_id': 2}],
        'families': [{'id': 2, 'name': 'Original Family', 'lead_user_id': 4, 'archived_at': None}],
        'player_family_memberships': [{'id': 7, 'player_id': 10, 'family_id': 2, 'role': 'lead', 'status': 'active', 'is_primary': True},
                                      {'id': 8, 'player_id': 20, 'family_id': 2, 'role': 'member', 'status': 'active', 'is_primary': False}],
        'games': [{'id': 1, 'slug': 'five-crowns', 'name': 'Five Crowns', 'scoring_direction': 'low_wins'}],
        'active_games': [{'id': 50, 'game_id': 1, 'user_id': 4, 'family_id': 2, 'is_complete': True,
                          'is_paused': False, 'start_time': datetime(2026, 2, 1), 'completion_time': datetime(2026, 2, 2)}],
        'active_game_players': [{'id': 100, 'active_game_id': 50, 'player_id': 20},
                                {'id': 101, 'active_game_id': 50, 'player_id': 10}],
        'game_scores': [{'id': 200, 'active_game_id': 50, 'player_id': 10, 'round_number': 3, 'score': 8,
                         'metadata': {'original': True}}],
        'five_crowns_scores': [{'id': 300, 'active_game_id': 50, 'player_id': 10, 'round_number': 3, 'score': 999},
                               {'id': 301, 'active_game_id': 50, 'player_id': 20, 'round_number': 3, 'score': 0}],
        'game_stats': [{'id': 400, 'game_id': 50, 'winner_id': 10, 'winning_score': 5, 'player_count': 2, 'is_tie': True}],
        'player_not_duplicates': [{'player_a_id': 10, 'player_b_id': 20, 'decided_by_user_id': 4}],
    }


def test_import_preserves_identity_score_precedence_original_result_and_privacy(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path / "import.db"}')
    Base.metadata.create_all(engine)
    sessions = make_sessions(engine)
    source = snapshot()
    before = deepcopy(source)
    with sessions.begin() as db:
        report = import_rows(db, source, 'isolated-test-snapshot')
    assert source == before
    assert all(report['checks'].values())
    assert report['normalized']['score_cells'] == 2
    assert report['warnings']['legacy_fallback_cells'] == 1
    with sessions.begin() as db:
        assert db.get(Person, 10).name == 'Chosen Name'
        assert 'HiddenSurname' not in db.get(Person, 10).name
        assert db.get(Person, 20).name == 'Second'
        assert db.get(Person, 20).archived is True
        assert db.get(Person, 10).privacy['is_minor'] is True
        assert db.get(Person, 10).privacy['is_discoverable'] is False
        assert db.get(Person, 10).privacy['show_full_last_name'] is False
        assert db.get(User, 4).person_id == 10
        assert db.get(User, 4).password_hash == source['users'][0]['password_hash']
        assert db.get(Score, (50, 10, 3)).value == 8
        assert db.get(Score, (50, 10, 3)).extra == {'original': True}
        result = db.get(Game, 50).result
        assert next(r for r in result['standings'] if r['winner'])['player_id'] == 10
        assert result['original_results'][0]['winning_score'] == 5
        assert result['original_results'][0]['is_tie'] is True
        assert db.get(Membership, 7).is_home is True
        assert db.get(LegacyRecord, ('player_not_duplicates', '0')).payload['player_a_id'] == 10
    with pytest.raises(ValueError, match='not empty'):
        with sessions.begin() as db:
            import_rows(db, source, 'refuse-repeat')


def test_import_rejects_live_source_same_database_and_unconfirmed():
    with pytest.raises(ValueError, match='snapshot or restore'):
        validate_source('postgresql://reader@db/difedeappv2', 'postgresql://writer@db/difede_v3', True)
    with pytest.raises(ValueError, match='must differ'):
        validate_source('postgresql://reader@db/v3_snapshot', 'postgresql://writer@db/v3_snapshot', True)
    with pytest.raises(ValueError, match='confirm-isolated'):
        validate_source('postgresql://reader@db/v3_snapshot', 'postgresql://writer@db/difede_v3', False)
