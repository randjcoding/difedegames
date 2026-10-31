"""Explicit operator-only bootstrap; no runtime email-based privileges."""
import os

from sqlalchemy import select, func, text

from .db import make_engine, make_sessions
from .models import Audit, LegacyRecord, User
from .security import is_superadmin


def bootstrap(db, owner_email):
    if not owner_email:
        raise ValueError('OWNER_EMAIL must be supplied in private operator configuration')
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(74251123)'))
    user = db.scalar(select(User).where(func.lower(User.email) == owner_email.strip().lower()).with_for_update())
    source = db.get(LegacyRecord, ('users', str(user.id))) if user else None
    if not user or not is_superadmin(user) or not source or source.payload.get('role') != 'super_admin':
        raise ValueError('Owner must match an existing active verified approved legacy superadmin')
    existing = db.scalar(select(User.id).where(User.is_owner.is_(True), User.id != user.id))
    if existing:
        raise ValueError('A different owner already exists; explicit reviewed recovery is required')
    if not user.is_owner:
        user.is_owner = True
        db.add(Audit(actor_id=user.id, action='owner_bootstrapped', payload={'account_id': user.id}))


def main():
    with make_sessions(make_engine()).begin() as db:
        bootstrap(db, os.environ.get('OWNER_EMAIL'))
    print('Owner capability verified for the matching preserved legacy administrator.')


if __name__ == '__main__':
    main()
