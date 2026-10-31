"""Synthetic local UI fixture; refuses PostgreSQL and every nonempty database."""
from sqlalchemy import func, select

from .db import make_engine, make_sessions
from .games import create_game
from .models import Group, GroupAdmin, Membership, Person, User
from .schemas import GameCreate
from .security import hash_password


def main():
    engine = make_engine()
    if engine.dialect.name != 'sqlite':
        raise SystemExit('Synthetic fixtures are only allowed in a local SQLite database.')
    with make_sessions(engine).begin() as db:
        if db.scalar(select(func.count()).select_from(User)):
            raise SystemExit('Database already contains accounts; seed refused.')
        db.add_all([Person(id=i, name=name) for i, name in enumerate(['Avery', 'Morgan', 'Taylor', 'Guest'], 1)])
        db.add(Group(id=1, name='Synthetic Test Group'))
        db.flush()
        for i in range(1, 4):
            db.add(User(id=i, email=f'person{i}@example.com', name=['Avery', 'Morgan', 'Taylor'][i-1],
                        password_hash=hash_password('ExamplePass!123'), person_id=i))
        db.flush()
        for i in range(1, 5):
            db.add(Membership(person_id=i, group_id=1))
        db.add(GroupAdmin(group_id=1, user_id=1))
        db.flush()
        user = db.get(User, 1)
        create_game(db, user, GameCreate(slug='five-crowns', player_ids=[1, 2, 4], group_id=1, viewer_group_ids=[1]))
        create_game(db, user, GameCreate(slug='uno-flip', player_ids=[1, 2], group_id=1, viewer_group_ids=[1]))
    print('Synthetic local UI fixture created; no real user data imported.')


if __name__ == '__main__':
    main()
