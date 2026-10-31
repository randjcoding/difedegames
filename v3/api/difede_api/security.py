import hashlib
import hmac
import os
import secrets
from datetime import timedelta, timezone

import bcrypt
from fastapi import HTTPException
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from .db import utcnow
from .models import Group, GroupAdmin, Membership, Person, User, WebSession

COOKIE_NAME = 'difede_v3_session'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def verify_password(password, stored):
    try:
        if stored.startswith(('$2a$', '$2b$', '$2y$')):
            # V2 bcrypt 4 truncated UTF-8 inputs at 72 bytes.
            return bcrypt.checkpw(password.encode()[:72], stored.encode())
        return check_password_hash(stored, password)
    except (ValueError, TypeError):
        return False


def hash_password(password):
    return generate_password_hash(password, method='scrypt')


def session_user(db, raw_token):
    if not raw_token:
        raise HTTPException(401, 'Please sign in.')
    sess = db.get(WebSession, digest(raw_token))
    if not sess or aware(sess.expires_at) <= utcnow():
        raise HTTPException(401, 'Your session has expired. Please sign in.')
    user = db.get(User, sess.user_id)
    if not user or not user.active or not user.approved or not user.verified:
        raise HTTPException(401, 'This account is not available.')
    return user, sess


def issue_session(db, user, response):
    raw_token = secrets.token_urlsafe(48)
    sess = WebSession(token_hash=digest(raw_token), user_id=user.id,
                      csrf_token=secrets.token_urlsafe(32), expires_at=utcnow() + timedelta(hours=24))
    db.add(sess)
    secure = os.environ.get('SESSION_COOKIE_SECURE', 'true').lower() == 'true'
    response.set_cookie(COOKIE_NAME, raw_token, httponly=True, secure=secure,
                        samesite='lax', max_age=86400, path='/')
    return sess


def check_origin(request):
    origin = request.headers.get('origin')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Cross-site requests are not allowed.')
    allowed = {s.strip().rstrip('/') for s in os.environ.get(
        'ALLOWED_ORIGINS', os.environ.get('APP_BASE_URL', 'http://localhost:5173')).split(',')}
    if origin and origin.rstrip('/') not in allowed:
        raise HTTPException(403, 'Origin is not allowed.')


def check_csrf(request, sess):
    check_origin(request)
    supplied = request.headers.get('x-csrf-token', '')
    if not supplied or not hmac.compare_digest(supplied, sess.csrf_token):
        raise HTTPException(403, 'Please refresh your sign-in before saving.')


def user_groups(db, user):
    ids = set(db.scalars(select(Membership.group_id).where(
        Membership.person_id == user.person_id, Membership.status == 'active')).all()) if user.person_id else set()
    admin_ids = set(db.scalars(select(GroupAdmin.group_id).where(GroupAdmin.user_id == user.id)).all())
    ids |= admin_ids
    groups = db.scalars(select(Group).where(Group.id.in_(ids), Group.archived.is_(False)).order_by(Group.name)).all()
    return [{'id': g.id, 'name': g.name, 'role': 'admin' if g.id in admin_ids else 'member'} for g in groups]


def is_member(db, user, group_id):
    return any(g['id'] == group_id for g in user_groups(db, user))


def is_admin(db, user, group_id):
    return any(g['id'] == group_id and g['role'] == 'admin' for g in user_groups(db, user))


def me_payload(db, user, sess):
    return {'user': {'id': user.id, 'email': user.email, 'name': user.name, 'person_id': user.person_id},
            'groups': user_groups(db, user), 'csrf_token': sess.csrf_token}
