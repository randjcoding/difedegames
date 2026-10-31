import asyncio
from contextlib import contextmanager
from datetime import timedelta
import json
import os
import secrets

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from .catalog import CATALOG
from .db import make_engine, make_sessions, utcnow
from .games import GameError, accessible_game, can_view, command, create_game, iso, serialize, summaries, visible_games_query
from .models import Audit, Game, Group, GroupAdmin, Invite, LoginThrottle, Membership, Person, User, WebSession
from .schemas import (FinishCommand, GameCreate, GroupCreate, InviteAccept, InviteCreate, Login,
                      PersonCreate, RevisionCommand, RoundCommand, RoundPatch, ScoreCommand)
from .security import (COOKIE_NAME, aware, check_csrf, check_origin, digest, hash_password,
                       is_admin, is_member, issue_session, me_payload, session_user,
                       user_groups, verify_password)


@contextmanager
def authenticated(request, mutation=False):
    with request.app.state.sessions.begin() as db:
        user, sess = session_user(db, request.cookies.get(COOKIE_NAME))
        if mutation:
            check_csrf(request, sess)
        yield db, user, sess


def people_payload(db, user):
    group_ids = {g['id'] for g in user_groups(db, user)}
    visible_ids = set(db.scalars(select(Membership.person_id).where(
        Membership.group_id.in_(group_ids), Membership.status == 'active')))
    if user.person_id:
        visible_ids.add(user.person_id)
    output = []
    for person in db.scalars(select(Person).where(Person.id.in_(visible_ids), Person.archived.is_(False),
                                                   Person.purged.is_(False)).order_by(Person.name)):
        account_id = db.scalar(select(User.id).where(User.person_id == person.id, User.active.is_(True),
                                                    User.approved.is_(True), User.verified.is_(True)))
        output.append({'id': person.id, 'name': person.name,
                       'has_account': db.scalar(select(User.id).where(User.person_id == person.id)) is not None,
                       'account_id': account_id,
                       'group_ids': list(db.scalars(select(Membership.group_id).where(
                           Membership.person_id == person.id, Membership.status == 'active',
                           Membership.group_id.in_(group_ids))))})
    return output


def throttle_login(db, email, ip):
    now = utcnow()
    allowed = True
    # Independent buckets prevent switching either addresses or IPs to reset a budget.
    buckets = sorted([(digest('email:' + email.lower()), 10), (digest('ip:' + ip), 50)])
    for key, maximum in buckets:
        if db.bind.dialect.name == 'postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(key[:15], 16)})
        row = db.scalar(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())
        if not row:
            row = LoginThrottle(key=key, count=0, window_start=now)
            db.add(row)
        if aware(row.window_start) < now - timedelta(minutes=15):
            row.count = 0
            row.window_start = now
        if row.count >= maximum:
            allowed = False
        else:
            row.count += 1
    return allowed


def create_app(engine=None):
    app = FastAPI(title='DiFede Games V3 pilot', docs_url=None, redoc_url=None)
    app.state.engine = engine or make_engine()
    app.state.sessions = make_sessions(app.state.engine)

    @app.middleware('http')
    async def security_headers(request, call_next):
        if request.method in ('POST', 'PATCH', 'PUT', 'DELETE'):
            length = request.headers.get('content-length')
            if length and (not length.isdigit() or int(length) > 65536):
                return JSONResponse({'detail': 'Request body too large.'}, status_code=413,
                                    headers={'Cache-Control': 'private, no-store'})
            if len(await request.body()) > 65536:
                return JSONResponse({'detail': 'Request body too large.'}, status_code=413,
                                    headers={'Cache-Control': 'private, no-store'})
        response = await call_next(request)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    @app.exception_handler(GameError)
    async def game_error(_, exc):
        return JSONResponse(exc.body, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        fields = ', '.join('.'.join(str(p) for p in e['loc'][1:]) for e in exc.errors()[:5])
        return JSONResponse({'detail': f'Check the supplied fields: {fields}.', 'code': 'validation_error'}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def integrity_error(_, exc):
        return JSONResponse({'detail': 'That change conflicts with an existing record. Refresh and try again.',
                             'code': 'record_conflict'}, status_code=409)

    @app.exception_handler(Exception)
    async def unexpected_error(_, exc):
        return JSONResponse({'detail': 'The change could not be completed. Refresh and try again.',
                             'code': 'server_error'}, status_code=500,
                            headers={'Cache-Control': 'private, no-store'})

    @app.get('/api/health')
    def health():
        try:
            with app.state.engine.connect() as conn:
                conn.execute(text('SELECT 1'))
            return {'status': 'ok', 'database': True}
        except Exception:
            return JSONResponse({'status': 'unavailable', 'database': False}, status_code=503)

    @app.get('/api/catalog')
    def catalog():
        return {'games': list(CATALOG.values())}

    @app.post('/api/auth/login')
    def login(data: Login, request: Request, response: Response):
        check_origin(request)
        email = str(data.email).lower()
        ip = request.client.host if request.client else 'unknown'
        with app.state.sessions.begin() as db:
            allowed = throttle_login(db, email, ip)
        if not allowed:
            raise HTTPException(429, 'Too many sign-in attempts. Try again in 15 minutes.')
        with app.state.sessions.begin() as db:
            user = db.scalar(select(User).where(User.email == email))
            # An expensive dummy check prevents the cheapest account-enumeration timing path.
            stored = user.password_hash if user else app.state.dummy_hash
            valid = verify_password(data.password, stored)
            if not valid or not user or not user.active or not user.approved or not user.verified:
                raise HTTPException(401, 'Email or password is incorrect, or the account is unavailable.')
            old = request.cookies.get(COOKIE_NAME)
            if old:
                old_session = db.get(WebSession, digest(old))
                if old_session:
                    db.delete(old_session)
            sess = issue_session(db, user, response)
            return me_payload(db, user, sess)

    @app.get('/api/me')
    def me(request: Request):
        with authenticated(request) as (db, user, sess):
            return me_payload(db, user, sess)

    @app.post('/api/auth/logout')
    def logout(request: Request, response: Response):
        with authenticated(request, True) as (db, _, sess):
            db.delete(sess)
        response.delete_cookie(COOKIE_NAME, path='/')
        return {'ok': True}

    @app.get('/api/people')
    def list_people(request: Request):
        with authenticated(request) as (db, user, _):
            return {'people': people_payload(db, user)}

    @app.post('/api/people')
    def add_person(data: PersonCreate, request: Request):
        with authenticated(request, True) as (db, user, _):
            if not is_admin(db, user, data.group_id):
                raise HTTPException(403, 'Only a group admin may add guests.')
            person = Person(name=data.name, created_by=user.id)
            db.add(person)
            db.flush()
            db.add(Membership(person_id=person.id, group_id=data.group_id))
            db.add(Audit(actor_id=user.id, action='guest_created', payload={'person_id': person.id, 'group_id': data.group_id}))
            return {'id': person.id, 'name': person.name, 'has_account': False, 'account_id': None, 'group_ids': [data.group_id]}

    @app.get('/api/groups')
    def groups(request: Request):
        with authenticated(request) as (db, user, _):
            return {'groups': user_groups(db, user)}

    @app.post('/api/groups')
    def add_group(data: GroupCreate, request: Request):
        with authenticated(request, True) as (db, user, _):
            if not user.person_id:
                raise HTTPException(422, 'Resolve your existing player identity before creating a group.')
            group = Group(name=data.name)
            db.add(group)
            db.flush()
            db.add(Membership(person_id=user.person_id, group_id=group.id, role='admin'))
            db.add(GroupAdmin(group_id=group.id, user_id=user.id))
            db.add(Audit(actor_id=user.id, action='group_created', payload={'group_id': group.id}))
            return {'id': group.id, 'name': group.name, 'role': 'admin'}

    @app.post('/api/groups/{group_id}/invites')
    def create_invite(group_id: int, data: InviteCreate, request: Request):
        with authenticated(request, True) as (db, user, _):
            if not is_admin(db, user, group_id):
                raise HTTPException(403, 'Only a group admin may invite people.')
            if data.person_id is not None:
                person = db.get(Person, data.person_id)
                member = db.scalar(select(Membership).where(Membership.person_id == data.person_id,
                    Membership.group_id == group_id, Membership.status == 'active'))
                claimed = db.scalar(select(User.id).where(User.person_id == data.person_id))
                if not person or person.archived or person.purged or not member or claimed:
                    raise HTTPException(422, 'Choose an unclaimed active guest in this group.')
            token = secrets.token_urlsafe(40)
            expires = utcnow() + timedelta(days=7)
            db.add(Invite(token_hash=digest(token), group_id=group_id, person_id=data.person_id,
                          creator_id=user.id, expires_at=expires))
            db.add(Audit(actor_id=user.id, action='invite_created', payload={'group_id': group_id, 'person_id': data.person_id}))
            base = os.environ.get('APP_BASE_URL', 'http://localhost:5173').rstrip('/')
            return {'url': f'{base}/invite/{token}', 'expires_at': iso(expires)}

    def valid_invite(db, token, lock=False):
        if len(token) > 100:
            raise HTTPException(404, 'Invitation not found.')
        query = select(Invite).where(Invite.token_hash == digest(token))
        invite = db.scalar(query.with_for_update() if lock else query)
        if not invite or invite.used_at or aware(invite.expires_at) <= utcnow():
            raise HTTPException(404, 'This invitation is expired or already used.')
        group = db.get(Group, invite.group_id)
        creator = db.get(User, invite.creator_id)
        if not group or group.archived or not creator or not creator.active or not creator.approved or not creator.verified or not is_admin(db, creator, group.id):
            raise HTTPException(404, 'This invitation is no longer available.')
        if invite.person_id:
            person = db.get(Person, invite.person_id)
            membership = db.scalar(select(Membership.id).where(Membership.person_id == invite.person_id,
                Membership.group_id == group.id, Membership.status == 'active'))
            if not person or person.archived or person.purged or not membership:
                raise HTTPException(404, 'This invitation is no longer available.')
        return invite, group

    @app.get('/api/invites/{token}')
    def view_invite(token: str):
        with app.state.sessions.begin() as db:
            invite, group = valid_invite(db, token)
            return {'group': {'id': group.id, 'name': group.name},
                    'invited_name': db.get(Person, invite.person_id).name if invite.person_id else None,
                    'expires_at': iso(invite.expires_at)}

    @app.post('/api/invites/{token}/accept')
    def accept_invite(token: str, data: InviteAccept, request: Request, response: Response):
        check_origin(request)
        with app.state.sessions.begin() as db:
            invite, group = valid_invite(db, token, lock=True)
            raw_cookie = request.cookies.get(COOKIE_NAME)
            if raw_cookie:
                user, sess = session_user(db, raw_cookie)
                check_csrf(request, sess)
                if data.email or data.password:
                    raise HTTPException(422, 'Sign out before creating a different account.')
                if invite.person_id and user.person_id and user.person_id != invite.person_id:
                    raise HTTPException(409, 'Your account already has a different person profile. Ask an admin to review the identities.')
                if not user.person_id:
                    if not invite.person_id:
                        raise HTTPException(422, 'An invitation to your existing profile is required.')
                    claimed = db.scalar(select(User.id).where(User.person_id == invite.person_id))
                    if claimed:
                        raise HTTPException(409, 'This guest already has an account.')
                    user.person_id = invite.person_id
            else:
                if not data.email or not data.password or (not data.name and not invite.person_id):
                    raise HTTPException(422, 'Name, email, and a password of at least 10 characters are required.')
                if db.scalar(select(User.id).where(User.email == str(data.email).lower())):
                    raise HTTPException(409, 'An account already uses this email. Sign in to accept.')
                person = db.get(Person, invite.person_id) if invite.person_id else Person(name=data.name)
                if invite.person_id and db.scalar(select(User.id).where(User.person_id == invite.person_id)):
                    raise HTTPException(409, 'This guest already has an account.')
                if not invite.person_id:
                    db.add(person)
                    db.flush()
                user = User(email=str(data.email).lower(), name=data.name or person.name,
                            password_hash=hash_password(data.password), person_id=person.id)
                db.add(user)
                db.flush()
                sess = issue_session(db, user, response)
            membership = db.scalar(select(Membership).where(Membership.person_id == user.person_id, Membership.group_id == group.id))
            if membership:
                membership.status = 'active'
            else:
                db.add(Membership(person_id=user.person_id, group_id=group.id))
            invite.used_at = utcnow()
            db.add(Audit(actor_id=user.id, action='invite_accepted', payload={'group_id': group.id, 'person_id': user.person_id}))
            db.flush()
            return me_payload(db, user, sess)

    @app.get('/api/games')
    def list_games(request: Request, status: str | None = None, slug: str | None = None,
                   q: str | None = Query(default=None, max_length=160),
                   limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        with authenticated(request) as (db, user, _):
            query = visible_games_query(db, user)
            if status:
                query = query.where(Game.status == status)
            if slug:
                query = query.where(Game.slug == slug)
            if q and q.strip():
                query = query.where(Game.title.icontains(q.strip(), autoescape=True))
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            rows = list(db.scalars(query.order_by(Game.created_at.desc(), Game.id.desc()).limit(limit).offset(offset)))
            return {'games': summaries(db, rows, user), 'total': total}

    @app.get('/api/dashboard')
    def dashboard(request: Request):
        with authenticated(request) as (db, user, _):
            query = visible_games_query(db, user)
            active_rows = list(db.scalars(query.where(Game.status == 'active').order_by(Game.created_at.desc())))
            recent_rows = list(db.scalars(query.where(Game.status != 'active').order_by(Game.created_at.desc()).limit(20)))
            active = summaries(db, active_rows, user)
            recent = summaries(db, recent_rows, user)
            played = [g for g in db.scalars(query.where(Game.status == 'completed')) if g.result and any(
                r['player_id'] == user.person_id for r in g.result.get('standings', []))]
            wins = sum(any(r['player_id'] == user.person_id and r['winner'] for r in g.result.get('standings', [])) for g in played)
            return {'active': active, 'recent': recent, 'stats': {'games': len(played), 'wins': wins},
                    'people': people_payload(db, user), 'groups': user_groups(db, user)}

    @app.post('/api/games')
    def new_game(data: GameCreate, request: Request):
        with authenticated(request, True) as (db, user, _):
            return create_game(db, user, data)

    @app.get('/api/games/{game_id}')
    def game_detail(game_id: int, request: Request):
        with authenticated(request) as (db, user, _):
            return serialize(db, accessible_game(db, user, game_id), user)

    def run_command(request, game_id, action, data, round_number=None):
        with authenticated(request, True) as (db, user, _):
            return command(db, user, game_id, action, data, round_number)

    @app.post('/api/games/{game_id}/scores')
    def score(game_id: int, data: ScoreCommand, request: Request):
        return run_command(request, game_id, 'score', data)

    @app.post('/api/games/{game_id}/rounds')
    def add_round(game_id: int, data: RoundCommand, request: Request):
        return run_command(request, game_id, 'round', data)

    @app.patch('/api/games/{game_id}/rounds/{number}')
    def change_side(game_id: int, number: int, data: RoundPatch, request: Request):
        return run_command(request, game_id, 'side', data, number)

    @app.post('/api/games/{game_id}/finish')
    def finish_game(game_id: int, data: FinishCommand, request: Request):
        return run_command(request, game_id, 'finish', data)

    @app.post('/api/games/{game_id}/reopen')
    def reopen(game_id: int, data: RevisionCommand, request: Request):
        return run_command(request, game_id, 'reopen', data)

    @app.delete('/api/games/{game_id}')
    def delete(game_id: int, data: RevisionCommand, request: Request):
        return run_command(request, game_id, 'delete', data)

    def stream_snapshot(raw_cookie, game_id):
        with app.state.sessions.begin() as db:
            user, _ = session_user(db, raw_cookie)
            game = accessible_game(db, user, game_id)
            return serialize(db, game, user)

    @app.get('/api/games/{game_id}/events')
    async def events(game_id: int, request: Request):
        raw_cookie = request.cookies.get(COOKIE_NAME)
        check_origin(request)
        first = await run_in_threadpool(stream_snapshot, raw_cookie, game_id)

        async def snapshots():
            previous = None
            payload = first
            while not await request.is_disconnected():
                encoded = json.dumps(payload, separators=(',', ':'))
                if encoded != previous:
                    yield f'id: {payload["revision"]}\nevent: snapshot\ndata: {encoded}\n\n'
                    previous = encoded
                else:
                    yield ': heartbeat\n\n'
                await asyncio.sleep(2)
                try:
                    payload = await run_in_threadpool(stream_snapshot, raw_cookie, game_id)
                except (HTTPException, GameError):
                    yield 'event: access_revoked\ndata: {"detail":"Access changed. Please reload."}\n\n'
                    break
        return StreamingResponse(snapshots(), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'private, no-store'})

    app.state.dummy_hash = hash_password(secrets.token_urlsafe(30))
    return app


app = create_app()
