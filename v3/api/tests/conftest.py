import os
from pathlib import Path
import sys

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ['APP_BASE_URL'] = 'https://testserver'
os.environ['ALLOWED_ORIGINS'] = 'https://testserver'
os.environ['SESSION_COOKIE_SECURE'] = 'true'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from difede_api.db import Base, make_engine, make_sessions
from difede_api.main import create_app
from difede_api.models import Group, GroupAdmin, Membership, Person, User
from difede_api.security import hash_password


@pytest.fixture
def app(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path / "test.db"}')
    Base.metadata.create_all(engine)
    sessions = make_sessions(engine)
    password_hash = hash_password('ExamplePass!123')
    with sessions.begin() as db:
        db.add_all([Person(id=i, name=f'Test Person {i}') for i in range(1, 7)])
        db.add_all([Group(id=1, name='First Group'), Group(id=2, name='Second Group')])
        db.flush()
        db.add_all([User(id=i, email=f'person{i}@example.com', name=f'Person {i}', password_hash=password_hash, person_id=i)
                    for i in range(1, 6)])
        db.flush()
        for i in (1, 2, 3, 5, 6):
            db.add(Membership(person_id=i, group_id=1))
        db.add(Membership(person_id=4, group_id=2))
        db.add(Membership(person_id=1, group_id=2))
        db.add_all([GroupAdmin(group_id=1, user_id=1), GroupAdmin(group_id=2, user_id=4)])
    application = create_app(engine)
    yield application
    engine.dispose()


def signed_in(app, uid=1):
    client = TestClient(app, base_url='https://testserver', raise_server_exceptions=False)
    response = client.post('/api/auth/login', json={'email': f'person{uid}@example.com', 'password': 'ExamplePass!123'})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


@pytest.fixture
def client(app):
    return signed_in(app)


def new_game(client, slug='gin-rummy', **kwargs):
    response = client.post('/api/games', json={'slug': slug, 'player_ids': [1, 2], 'group_id': 1,
        'viewer_group_ids': [1], **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def save(client, game, pid=1, score=0, number=None, version=0, op=None):
    import uuid
    response = client.post(f'/api/games/{game["id"]}/scores', json={
        'operation_id': op or str(uuid.uuid4()), 'player_id': pid,
        'round_number': number or game['rounds'][0]['number'], 'score': score, 'expected_version': version})
    assert response.status_code == 200, response.text
    return response.json()
