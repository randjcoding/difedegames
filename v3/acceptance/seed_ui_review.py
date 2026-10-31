"""Create disposable, clearly synthetic fixtures for the parent UI review.

Run after Alembic in the API working directory with DATABASE_URL pointing to a
new local SQLite file named parity-review.db. Never accepts a server database,
another filename, or an existing populated database.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from difede_api.db import make_engine, make_sessions
from difede_api.games import create_game, finish, put_cell
from difede_api.models import Group, GroupAdmin, Membership, Person, User
from difede_api.schemas import FinishCommand, GameCreate
from difede_api.security import hash_password


def main():
    url = make_url(os.environ.get('DATABASE_URL', ''))
    if url.get_backend_name() != 'sqlite' or Path(url.database or '').name != 'parity-review.db':
        raise SystemExit('Refusing seed outside the exact local parity-review.db fixture.')
    engine = make_engine()
    with make_sessions(engine).begin() as db:
        if db.scalar(select(func.count()).select_from(User)):
            raise SystemExit('Fixture already populated; no existing records will be changed.')
        names = ['Avery', 'Morgan', 'Taylor', 'Jordan', 'Riley', 'Casey', 'Alexandria', 'Drew']
        db.add_all([Person(id=i, name=name, details={'first_name': name}) for i, name in enumerate(names, 1)])
        db.add_all([Group(id=1, name='Demo Game Night'), Group(id=2, name='Demo Other Group')])
        db.flush()
        password = hash_password('ExamplePass!123')
        for i in range(1, 4):
            db.add(User(id=i, email=f'person{i}@example.com', name=names[i-1],
                        password_hash=password, person_id=i,
                        site_role='super_admin' if i == 1 else 'member', is_owner=i == 1))
        db.flush()
        for i in range(1, 8):
            db.add(Membership(person_id=i, group_id=1, role='admin' if i == 1 else 'member'))
        db.add(Membership(person_id=3, group_id=2, role='admin'))
        db.add(Membership(person_id=8, group_id=2))
        db.add_all([GroupAdmin(group_id=1, user_id=1), GroupAdmin(group_id=2, user_id=3)])
        db.flush()
        actor = db.get(User, 1)
        boards = []
        for players in ([1, 2], list(range(1, 8))):
            game = create_game(db, actor, GameCreate(slug='five-crowns', player_ids=players,
                               group_id=1, viewer_group_ids=[1]))
            # create_game returns a serialized board; look up its persistent row.
            from difede_api.models import Game
            row = db.get(Game, game['id'])
            for number in range(3, 14):
                for pid in players:
                    put_cell(db, row, pid, number, 0 if pid == 2 else (pid * number) % 31)
            db.flush()
            finish(db, row, FinishCommand(operation_id=f'demo-finish-{row.id}', expected_revision=row.revision))
            boards.append(row.id)
        create_game(db, actor, GameCreate(slug='uno-flip', player_ids=[1, 2, 3], group_id=1, viewer_group_ids=[1]))
        create_game(db, actor, GameCreate(slug='trouble', mode='single', player_ids=[1, 2], group_id=1, viewer_group_ids=[1]))
    print({'synthetic_only': True, 'completed_five_crowns_boards': boards, 'players': 8})


if __name__ == '__main__':
    main()
