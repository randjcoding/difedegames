import json
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, insert, select

from difede_api.import_legacy import fingerprint


def test_additive_upgrade_preserves_every_old_column_and_game_606(tmp_path, monkeypatch):
    url = f'sqlite:///{tmp_path / "prior-image.db"}'
    monkeypatch.setenv('DATABASE_URL', url)
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    command.upgrade(config, '0002')
    engine = create_engine(url)
    old = MetaData()
    old.reflect(engine)
    with engine.begin() as db:
        db.execute(old.tables['people'].insert(), [{'id': 10, 'name': 'Preferred', 'created_by': None,
            'archived': False, 'purged': False, 'privacy': {'is_minor': False},
            'legacy': {'first_name': 'Legal', 'last_name': 'Private', 'email': None}}])
        db.execute(old.tables['accounts'].insert(), [{'id': 20, 'name': 'Account Legal', 'email': 'existing@example.com',
            'password_hash': 'unchanged-hash', 'person_id': 10, 'active': True, 'verified': True, 'approved': True,
            'legacy': {'id': 20, 'role': 'super_admin', 'city': 'Private City'}}])
        db.execute(old.tables['groups'].insert(), [{'id': 30, 'name': 'Existing group', 'archived': False,
            'legacy': {'show_roster': False, 'is_discoverable': False}}])
        db.execute(old.tables['legacy_records'].insert(), [{'source_table': 'users', 'source_key': '20',
            'payload': {'id': 20, 'role': 'super_admin', 'is_active': True, 'is_verified': True, 'is_approved': True}}])
        db.execute(old.tables['legacy_records'].insert(), [
            {'source_table': 'games', 'source_key': '7', 'payload': {'id': 7, 'slug': 'basic-other'}},
            {'source_table': 'game_layouts', 'source_key': '88', 'payload': {'id': 88, 'family_id': 30,
                'game_id': 7, 'name': 'Original saved setup', 'player_ids': [10], 'scoring_direction': 'high_wins',
                'target_score': 100, 'is_default': True, 'created_by_user_id': 20,
                'created_at': '2026-10-30T12:00:00', 'updated_at': '2026-10-30T12:00:00'}},
            {'source_table': 'games', 'source_key': '6', 'payload': {'id': 6, 'slug': 'trouble'}},
            {'source_table': 'game_layouts', 'source_key': '89', 'payload': {'id': 89, 'family_id': 30,
                'game_id': 6, 'name': 'Trouble pegs', 'player_ids': [10], 'scoring_direction': 'high_wins', 'is_default': True}},
            {'source_table': 'game_layouts', 'source_key': '90', 'payload': {'id': 90, 'family_id': 30,
                'game_id': 7, 'name': 'Manual winner', 'player_ids': [10], 'scoring_direction': 'manual'}},
            {'source_table': 'game_layouts', 'source_key': '91', 'payload': {'id': 91, 'family_id': 30,
                'game_id': 7, 'name': 'Explicit single', 'player_ids': [10], 'scoring_direction': 'single_round'}}])
        db.execute(old.tables['game_sessions'].insert(), [{'id': 606, 'slug': 'gin-rummy', 'title': 'Existing V3 game',
            'status': 'completed', 'revision': 17, 'mode': 'rounds', 'scoring_direction': 'high', 'target_score': 100,
            'group_id': 30, 'organizer_id': 20, 'created_at': datetime(2026, 10, 31), 'finished_at': datetime(2026, 10, 31),
            'deleted': False, 'imported': False, 'rules_version': 'v3-pilot-1',
            'result': {'standings': [{'player_id': 10, 'total': 90, 'rank': 1, 'winner': True}], 'official': True}, 'legacy': {}}])
        db.execute(old.tables['game_seats'].insert(), [{'game_id': 606, 'person_id': 10, 'position': 0, 'color': None, 'legacy': {}}])
        db.execute(old.tables['score_cells'].insert(), [{'game_id': 606, 'person_id': 10, 'round_number': 1,
            'value': 90, 'version': 7, 'metadata': {'retained': True}, 'legacy_id': None}])
        before = {name: fingerprint([dict(row) for row in db.execute(select(table)).mappings()]) for name, table in old.tables.items()
                  if name != 'alembic_version'}
    command.upgrade(config, 'head')
    with engine.begin() as db:
        after = {name: fingerprint([dict(row) for row in db.execute(select(table)).mappings()]) for name, table in old.tables.items()
                 if name != 'alembic_version'}
        assert before == after
        account = db.exec_driver_sql('SELECT site_role,is_owner,details FROM accounts WHERE id=20').one()
        assert account.site_role == 'super_admin' and not account.is_owner
        assert json.loads(account.details)['city'] == 'Private City'
        assert db.exec_driver_sql('SELECT revision FROM game_sessions WHERE id=606').scalar_one() == 17
        layout = db.exec_driver_sql('SELECT id,group_id,slug,player_ids,is_default FROM saved_game_layouts WHERE id=88').one()
        assert layout.id == 88 and layout.group_id == 30 and layout.slug == 'basic-other'
        assert json.loads(layout.player_ids) == [10] and layout.is_default
        assert dict(db.exec_driver_sql('SELECT id,mode FROM saved_game_layouts').all()) == {88: 'rounds', 89: 'single', 90: 'winner', 91: 'single'}
        # Previous image INSERTs omit new columns: persistent server defaults keep rollback usable.
        db.execute(old.tables['people'].insert(), {'id': 11, 'name': 'Old image guest', 'created_by': None,
            'archived': False, 'purged': False, 'privacy': {}, 'legacy': {}})
        db.execute(old.tables['accounts'].insert(), {'id': 21, 'name': 'Old image account', 'email': 'oldimage@example.com',
            'password_hash': 'oldimage-hash', 'person_id': 11, 'active': True, 'verified': True, 'approved': True, 'legacy': {}})
        inserted = db.exec_driver_sql('SELECT site_role,is_owner,preferences,details FROM accounts WHERE id=21').one()
        assert inserted.site_role == 'member' and not inserted.is_owner
        assert json.loads(inserted.preferences) == {} and json.loads(inserted.details) == {}
    engine.dispose()
