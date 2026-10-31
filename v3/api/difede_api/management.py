from datetime import timedelta
import hashlib
import json
import os
import secrets
import smtplib
import ssl
from email.message import EmailMessage

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, or_, select, text

from .catalog import CATALOG
from .db import utcnow
from .dependencies import authenticated
from .games import (GameError, accessible_game, finish, iso, permissions, put_cell, record,
                    seats, serialize)
from .management_schemas import (AccountPatch, AddMembership, Correction, GroupPatch, LeadTransfer,
    MembershipDecision, PasswordChange, PersonPatch, PreferencesPatch, ProfilePatch, Purge, Reason,
    ResetComplete, ResetRequest)
from .models import (ActionToken, Audit, Game, Group, GroupAdmin, Invite, Membership, Operation,
                     Person, Round, Score, Seat, User, WebSession)
from .schemas import FinishCommand
from .privacy import protected_minor, public_flag
from .security import (COOKIE_NAME, aware, check_origin, digest, hash_password, is_admin, is_member,
                       is_superadmin, preference_values, user_groups, verify_password)

router = APIRouter(prefix='/api')


def management_lock(db):
    # Cross-group identity changes are rare; one transaction lock keeps lead safeguards coherent.
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(74251123)'))


def require_super(user):
    if not is_superadmin(user):
        raise HTTPException(403, 'Site administrator privileges are required.')


def audit(db, user, action, payload, game_id=None):
    db.add(Audit(actor_id=user.id, action=action, payload=payload, game_id=game_id))


def group_row(db, gid, archived=False):
    group = db.get(Group, gid)
    if not group or (group.archived and not archived):
        raise HTTPException(404, 'Group not found.')
    return group


def manage_group(db, user, gid, archived=False):
    group = group_row(db, gid, archived)
    if not is_admin(db, user, gid):
        raise HTTPException(403, 'Only this group’s lead can manage its membership.')
    return group


def memberships(db, pid):
    return list(db.scalars(select(Membership).where(Membership.person_id == pid, Membership.status == 'active')))


def person_payload(db, person, private=False):
    account = db.scalar(select(User).where(User.person_id == person.id))
    result = {'id': person.id, 'name': 'Deleted Player' if person.purged else person.name,
              'has_account': account is not None,
              'account_id': account.id if account and account.active and account.approved and account.verified else None,
              'group_ids': [m.group_id for m in memberships(db, person.id)]}
    if private:
        details = person.details or {}
        privacy = {**(person.privacy or {}), 'is_discoverable': public_flag(person.privacy, 'is_discoverable', True),
                   'show_full_last_name': public_flag(person.privacy, 'show_full_last_name'),
                   'is_minor': protected_minor(person.privacy)}
        result.update(first_name=details.get('first_name', ''), last_name=details.get('last_name', ''),
                      email=details.get('email'), archived=person.archived, purged=person.purged,
                      privacy=privacy,
                      is_discoverable=privacy['is_discoverable'],
                      show_full_last_name=privacy['show_full_last_name'],
                      is_minor=privacy['is_minor'],
                      account=account_payload(db, account) if account else None)
    return result


def account_payload(db, user):
    return {'id': user.id, 'email': user.email, 'name': user.name, 'person_id': user.person_id,
            'site_role': user.site_role, 'is_owner': user.is_owner, 'active': user.active,
            'verified': user.verified, 'approved': user.approved,
            'group_ids': [m.group_id for m in memberships(db, user.person_id)] if user.person_id else []}


def member_payload(member):
    return {k: getattr(member, k) for k in ('id', 'person_id', 'group_id', 'role', 'status', 'is_home')}


def group_payload(db, group, user):
    return {'id': group.id, 'name': group.name, 'role': 'admin' if is_admin(db, user, group.id) else 'member',
            'archived': group.archived, 'settings': {**(group.settings or {}),
                'is_discoverable': public_flag(group.settings, 'is_discoverable', True),
                'show_roster': public_flag(group.settings, 'show_roster', True)},
            'member_count': db.scalar(select(func.count()).select_from(Membership).where(
                Membership.group_id == group.id, Membership.status == 'active'))}


def can_manage_person(db, actor, person, privacy=False):
    if is_superadmin(actor) or actor.person_id == person.id:
        return True
    if db.scalar(select(User.id).where(User.person_id == person.id)) is not None:
        return False
    groups = memberships(db, person.id)
    return len(groups) == 1 and is_admin(db, actor, groups[0].group_id)


def assert_no_unfinished_games(db, person_id=None, group_id=None):
    query = select(Game.id).where(Game.status == 'active', Game.deleted.is_(False))
    if person_id is not None:
        query = query.join(Seat, Seat.game_id == Game.id).where(Seat.person_id == person_id)
    if group_id is not None:
        query = query.where(Game.group_id == group_id)
    if db.scalar(query.limit(1)):
        raise HTTPException(409, 'Finish or remove unfinished games before this lifecycle change.')


def assert_lead_can_leave(db, user_id, group_id=None):
    groups = list(db.scalars(select(GroupAdmin.group_id).where(GroupAdmin.user_id == user_id)))
    for gid in groups:
        if group_id is not None and gid != group_id:
            continue
        group = db.get(Group, gid)
        if not group or group.archived:
            continue
        other = db.scalar(select(GroupAdmin.user_id).join(User, User.id == GroupAdmin.user_id).join(
            Membership, (Membership.group_id == GroupAdmin.group_id) & (Membership.person_id == User.person_id)).where(
                GroupAdmin.group_id == gid, GroupAdmin.user_id != user_id, User.active.is_(True),
                User.verified.is_(True), User.approved.is_(True), Membership.status == 'active').limit(1))
        if other is None:
            raise HTTPException(409, 'Transfer group leadership before removing its last active lead.')


def remove_admin_links(db, user_id, group_id=None):
    query = select(GroupAdmin).where(GroupAdmin.user_id == user_id)
    if group_id is not None:
        query = query.where(GroupAdmin.group_id == group_id)
    user = db.get(User, user_id)
    for link in db.scalars(query):
        member = db.scalar(select(Membership).where(Membership.group_id == link.group_id,
            Membership.person_id == user.person_id)) if user.person_id else None
        if member:
            member.role = 'member'
        db.delete(link)


def revoke_sessions(db, uid):
    db.execute(delete(WebSession).where(WebSession.user_id == uid))


def profile_payload(db, user):
    person = db.get(Person, user.person_id) if user.person_id else None
    return {'user': account_payload(db, user), 'person': person_payload(db, person, True) if person else None,
            'contact': {k: (user.details or {}).get(k, '') for k in ('address', 'city', 'state', 'zipcode', 'phone_number')},
            'groups': user_groups(db, user)}


@router.get('/profile')
def profile(request: Request):
    with authenticated(request) as (db, user, _):
        return profile_payload(db, user)


@router.patch('/profile')
def patch_profile(data: ProfilePatch, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        db.refresh(user, with_for_update=True)
        if not user.active or not user.approved or not user.verified:
            raise HTTPException(401, 'This account is no longer available.')
        patch = data.model_dump(exclude_unset=True, mode='json')
        if data.contact:
            user.details = {**(user.details or {}), **data.contact.model_dump(exclude_unset=True)}
        if user.person_id:
            person = db.get(Person, user.person_id)
            for key in ('first_name', 'last_name'):
                if key in patch:
                    person.details = {**person.details, key: patch[key] or ''}
            if 'person_email' in patch:
                person.details = {**person.details, 'email': patch['person_email']}
                person.privacy = {**person.privacy, 'email_verified': False}
            if patch.get('name'):
                person.name = patch['name']
            person.privacy = {**person.privacy, **{k: patch[k] for k in ('is_discoverable', 'show_full_last_name') if patch.get(k) is not None}}
        audit(db, user, 'profile_updated', {'fields': sorted(patch)})
        db.flush()
        return profile_payload(db, user)


@router.patch('/preferences')
def patch_preferences(data: PreferencesPatch, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        db.refresh(user, with_for_update=True)
        if not user.active or not user.approved or not user.verified:
            raise HTTPException(401, 'This account is no longer available.')
        if any(getattr(data, key) is None for key in data.model_fields_set):
            raise HTTPException(422, 'Preference values cannot be null.')
        patch = data.model_dump(exclude_unset=True, exclude_none=True)
        if 'favorite_game_slugs' in patch:
            if any(slug not in CATALOG for slug in patch['favorite_game_slugs']):
                raise HTTPException(422, 'Choose games from the current catalog.')
            patch['favorite_game_slugs'] = list(dict.fromkeys(patch['favorite_game_slugs']))
        if 'favorite_person_ids' in patch:
            groups = [g['id'] for g in user_groups(db, user)]
            query = select(Person.id).join(Membership, Membership.person_id == Person.id).where(
                Membership.group_id.in_(groups), Membership.status == 'active', Person.archived.is_(False), Person.purged.is_(False))
            if is_superadmin(user):
                query = select(Person.id).where(Person.archived.is_(False), Person.purged.is_(False))
            allowed = set(db.scalars(query))
            if user.person_id:
                allowed.add(user.person_id)
            if not set(patch['favorite_person_ids']) <= allowed:
                raise HTTPException(403, 'A favorite person is not visible in your groups.')
            patch['favorite_person_ids'] = list(dict.fromkeys(patch['favorite_person_ids']))
        user.preferences = {**(user.preferences or {}), **patch}
        return {'preferences': preference_values(user)}


@router.post('/auth/password')
def change_password(data: PasswordChange, request: Request, response: Response):
    with authenticated(request, True) as (db, user, _):
        db.refresh(user, with_for_update=True)
        if not user.active or not user.approved or not user.verified:
            raise HTTPException(401, 'This account is no longer available.')
        if not verify_password(data.current_password, user.password_hash):
            raise HTTPException(403, 'The current password is incorrect.')
        user.password_hash = hash_password(data.new_password)
        revoke_sessions(db, user.id)
        db.execute(delete(ActionToken).where(ActionToken.user_id == user.id))
        audit(db, user, 'password_changed', {'account_id': user.id})
    response.delete_cookie(COOKIE_NAME, path='/')
    return {'ok': True}


def reset_link(db, target):
    db.refresh(target, with_for_update=True)
    if not target.active or not target.approved or not target.verified:
        raise HTTPException(404, 'Active approved account not found.')
    for old in db.scalars(select(ActionToken).where(ActionToken.user_id == target.id, ActionToken.purpose == 'password_reset', ActionToken.used_at.is_(None))):
        old.used_at = utcnow()
    raw = secrets.token_urlsafe(40)
    expires = utcnow() + timedelta(hours=1)
    db.add(ActionToken(token_hash=digest(raw), user_id=target.id, purpose='password_reset', expires_at=expires))
    return {'url': os.environ.get('APP_BASE_URL', 'http://localhost:5173').rstrip('/') + '/reset-password/' + raw,
            'expires_at': iso(expires)}


def send_reset(recipient, url):
    if not os.environ.get('SMTP_HOST') or not os.environ.get('SMTP_FROM'):
        return
    message = EmailMessage()
    message['From'] = os.environ['SMTP_FROM']
    message['To'] = recipient
    message['Subject'] = 'Reset your DiFede Games password'
    message.set_content('Use this single-use link within one hour to reset your password:\n' + url)
    try:
        with smtplib.SMTP(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '587')), timeout=15) as smtp:
            if os.environ.get('SMTP_STARTTLS', 'true').lower() == 'true':
                smtp.starttls(context=ssl.create_default_context())
            if os.environ.get('SMTP_USERNAME'):
                smtp.login(os.environ['SMTP_USERNAME'], os.environ.get('SMTP_PASSWORD', ''))
            smtp.send_message(message)
    except Exception:
        # Do not put addresses, token links, provider responses or credentials in logs.
        return


@router.post('/auth/password-reset')
def request_reset(data: ResetRequest, request: Request, tasks: BackgroundTasks):
    check_origin(request)
    from .main import throttle_login
    with request.app.state.sessions.begin() as db:
        allowed = throttle_login(db, str(data.email), 'reset:' + (request.client.host if request.client else 'unknown'))
    if allowed and os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'):
        with request.app.state.sessions.begin() as db:
            target = db.scalar(select(User).where(User.email == str(data.email).lower(), User.active.is_(True),
                User.verified.is_(True), User.approved.is_(True)))
            if target:
                link = reset_link(db, target)
                tasks.add_task(send_reset, target.email, link['url'])
    return {'ok': True, 'message': 'If an eligible account exists and email delivery is configured, a reset link will be sent. Otherwise contact an administrator.'}


def reset_target(db, token):
    if len(token) > 100:
        raise HTTPException(404, 'This reset link is invalid or expired.')
    row = db.get(ActionToken, digest(token))
    if not row:
        raise HTTPException(404, 'This reset link is invalid or expired.')
    target = db.scalar(select(User).where(User.id == row.user_id).with_for_update())
    row = db.scalar(select(ActionToken).where(ActionToken.token_hash == digest(token)).with_for_update().execution_options(populate_existing=True))
    if not row or row.purpose != 'password_reset' or row.used_at or aware(row.expires_at) <= utcnow() or not target or not target.active or not target.verified or not target.approved:
        raise HTTPException(404, 'This reset link is invalid or expired.')
    return row, target


@router.get('/auth/password-reset/{token}')
def inspect_reset(token: str, request: Request):
    with request.app.state.sessions.begin() as db:
        reset_target(db, token)
        return {'valid': True}


@router.post('/auth/password-reset/{token}')
def complete_reset(token: str, data: ResetComplete, request: Request, response: Response):
    check_origin(request)
    with request.app.state.sessions.begin() as db:
        row, target = reset_target(db, token)
        target.password_hash = hash_password(data.password)
        row.used_at = utcnow()
        revoke_sessions(db, target.id)
        audit(db, target, 'password_reset', {'account_id': target.id})
    response.delete_cookie(COOKIE_NAME, path='/')
    return {'ok': True}


@router.get('/admin/overview')
def overview(request: Request):
    with authenticated(request) as (db, user, _):
        require_super(user)
        count = lambda cls, *where: db.scalar(select(func.count()).select_from(cls).where(*where))
        return {'counts': {'people': count(Person, Person.purged.is_(False)), 'accounts': count(User),
            'groups': count(Group, Group.archived.is_(False)), 'games': count(Game, Game.deleted.is_(False)),
            **{name + '_games': count(Game, Game.deleted.is_(False), Game.status == state) for name, state in
               [('active', 'active'), ('completed', 'completed'), ('incomplete', 'incomplete')]}},
            'pending_accounts': count(User, User.active.is_(True), or_(User.verified.is_(False), User.approved.is_(False)))}


@router.get('/admin/accounts')
def accounts(request: Request, q: str = Query(default='', max_length=160), include_archived: bool = False,
             limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    with authenticated(request) as (db, user, _):
        require_super(user)
        query = select(User)
        if not include_archived:
            query = query.where(User.active.is_(True))
        if q:
            query = query.where(or_(User.name.icontains(q, autoescape=True), User.email.icontains(q, autoescape=True)))
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        return {'accounts': [account_payload(db, row) for row in db.scalars(query.order_by(User.id).limit(limit).offset(offset))], 'total': total}


@router.patch('/admin/accounts/{uid}')
def patch_account(uid: int, data: AccountPatch, request: Request):
    with authenticated(request, True) as (db, actor, _):
        require_super(actor)
        management_lock(db)
        target = db.scalar(select(User).where(User.id == uid).with_for_update())
        if not target:
            raise HTTPException(404, 'Account not found.')
        patch = data.model_dump(exclude_unset=True, exclude_none=True, mode='json')
        if 'site_role' in patch and patch['site_role'] != target.site_role and not actor.is_owner:
            raise HTTPException(403, 'Only the site owner may grant or revoke administrator privileges.')
        disabling = any(patch.get(k) is False for k in ('active', 'verified', 'approved'))
        owner_change = disabling or patch.get('site_role', target.site_role) != 'super_admin'
        if target.is_owner and (actor.id != target.id or owner_change):
            raise HTTPException(403, 'The site owner cannot be changed or locked out through this account action.')
        if uid == actor.id and disabling:
            raise HTTPException(409, 'You cannot disable your own sign-in.')
        if disabling:
            assert_lead_can_leave(db, uid)
            remove_admin_links(db, uid)
        old = {key: getattr(target, key) for key in patch if key != 'email'}
        for key, value in patch.items():
            setattr(target, key, value.lower() if key == 'email' else value)
        if disabling or 'site_role' in patch or 'email' in patch:
            revoke_sessions(db, uid)
        audit(db, actor, 'account_updated', {'account_id': uid, 'fields': sorted(patch), 'previous': old})
        db.flush()
        return account_payload(db, target)


@router.post('/admin/accounts/{uid}/reset-link')
def admin_reset(uid: int, request: Request):
    with authenticated(request, True) as (db, actor, _):
        require_super(actor)
        target = db.get(User, uid)
        if not target or not target.active or not target.approved or not target.verified:
            raise HTTPException(404, 'Active approved account not found.')
        if target.is_owner and actor.id != target.id:
            raise HTTPException(403, 'Only the owner may request an owner reset link.')
        result = reset_link(db, target)
        audit(db, actor, 'password_reset_link_created', {'account_id': uid})
        return result


@router.delete('/admin/accounts/{uid}')
def archive_account(uid: int, data: Reason, request: Request):
    with authenticated(request, True) as (db, actor, _):
        require_super(actor)
        management_lock(db)
        target = db.scalar(select(User).where(User.id == uid).with_for_update())
        if not target:
            raise HTTPException(404, 'Account not found.')
        if target.is_owner or target.id == actor.id:
            raise HTTPException(403, 'Your own or the owner’s account cannot be deactivated here.')
        assert_lead_can_leave(db, uid)
        remove_admin_links(db, uid)
        target.active = False
        revoke_sessions(db, uid)
        audit(db, actor, 'account_deactivated', {'account_id': uid, 'reason': data.reason})
        return {'ok': True}


@router.get('/admin/people')
def admin_people(request: Request, q: str = Query(default='', max_length=160), include_archived: bool = False,
                 limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    with authenticated(request) as (db, user, _):
        require_super(user)
        query = select(Person).where(Person.purged.is_(False))
        if not include_archived:
            query = query.where(Person.archived.is_(False))
        if q:
            query = query.where(Person.name.icontains(q, autoescape=True))
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        return {'people': [person_payload(db, p, True) for p in db.scalars(query.order_by(Person.id).limit(limit).offset(offset))], 'total': total}


@router.get('/admin/groups')
def admin_groups(request: Request, q: str = Query(default='', max_length=160), include_archived: bool = False,
                 limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    with authenticated(request) as (db, user, _):
        require_super(user)
        query = select(Group)
        if not include_archived:
            query = query.where(Group.archived.is_(False))
        if q:
            query = query.where(Group.name.icontains(q, autoescape=True))
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        return {'groups': [group_payload(db, g, user) for g in db.scalars(query.order_by(Group.id).limit(limit).offset(offset))], 'total': total}


def audit_rows(db, query, limit, offset):
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {'events': [{'id': row.id, 'game_id': row.game_id, 'actor_id': row.actor_id, 'action': row.action,
                        'payload': row.payload, 'created_at': iso(row.created_at)}
                       for row in db.scalars(query.order_by(Audit.id.desc()).limit(limit).offset(offset))], 'total': total}


@router.get('/admin/audit')
def admin_audit(request: Request, game_id: int | None = None, actor_id: int | None = None,
                limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    with authenticated(request) as (db, user, _):
        require_super(user)
        query = select(Audit)
        if game_id:
            query = query.where(Audit.game_id == game_id)
        if actor_id:
            query = query.where(Audit.actor_id == actor_id)
        return audit_rows(db, query, limit, offset)


@router.get('/games/{gid}/audit')
def game_audit(gid: int, request: Request):
    with authenticated(request) as (db, user, _):
        game = accessible_game(db, user, gid)
        if not permissions(db, user, game)['can_manage']:
            raise HTTPException(403, 'Only game managers may inspect the audit trail.')
        return audit_rows(db, select(Audit).where(Audit.game_id == gid), 100, 0)


@router.get('/admin/games/{gid}')
def inspect_game(gid: int, request: Request):
    with authenticated(request) as (db, user, _):
        require_super(user)
        game = db.get(Game, gid)
        if not game:
            raise HTTPException(404, 'Game not found.')
        return serialize(db, game, user) | {'deleted': game.deleted, 'organizer_id': game.organizer_id, 'rules_version': game.rules_version}


@router.post('/admin/games/{gid}/corrections')
def correct_game(gid: int, data: Correction, request: Request):
    with authenticated(request, True) as (db, user, _):
        game = accessible_game(db, user, gid, lock=True)
        if not permissions(db, user, game)['can_manage']:
            raise HTTPException(403, 'Only game managers may correct a result.')
        payload = data.model_dump(mode='json')
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        previous = db.get(Operation, (gid, user.id, data.operation_id))
        if previous:
            if previous.payload_hash != fingerprint:
                raise GameError('Operation ID was already used for another change.', 409, 'idempotency_conflict')
            return previous.result
        if game.revision != data.expected_revision:
            raise GameError('The game changed. Review the latest saved result.', 409, 'revision_conflict')
        if game.status not in ('completed', 'incomplete'):
            raise HTTPException(422, 'Use normal score entry for an active game.')
        if game.slug not in CATALOG:
            raise HTTPException(422, 'This legacy variant needs an explicit scoring policy before correction.')
        previous_finished_at = game.finished_at
        before = {'result': game.result, 'status': game.status, 'rules_version': game.rules_version, 'finished_at': iso(game.finished_at),
                  'scores': serialize(db, game, user)['scores']}
        if game.mode == 'rounds':
            roster = {s.person_id for s in seats(db, gid)}
            seen = set()
            for cell in data.scores or []:
                key = (cell.player_id, cell.round_number)
                if key in seen or cell.player_id not in roster or not db.get(Round, (gid, cell.round_number)):
                    raise HTTPException(422, 'Correction contains duplicate cells or an unknown player/round.')
                seen.add(key)
                if cell.score is not None and cell.score < 0 and not CATALOG[game.slug]['allow_negative']:
                    raise HTTPException(422, 'This game does not permit negative scores.')
                put_cell(db, game, cell.player_id, cell.round_number, cell.score)
        elif data.scores:
            raise HTTPException(422, 'Use winner/result details to correct this game type.')
        db.flush()
        game.rules_version = 'v3-corrected-1'
        finish(db, game, FinishCommand(operation_id=data.operation_id, expected_revision=data.expected_revision,
            winner_id=data.winner_id, details=data.details))
        if before['status'] == 'completed':
            game.finished_at = previous_finished_at
        game.result = {**game.result, 'corrected': True, 'policy': 'v3-corrected-1'}
        game.revision += 1
        db.flush()
        record(db, user, game, 'historical_correction', {'reason': data.reason, 'before': before,
            'after': {'result': game.result, 'scores': serialize(db, game, user)['scores']}})
        result = serialize(db, game, user)
        db.add(Operation(game_id=gid, user_id=user.id, operation_id=data.operation_id, payload_hash=fingerprint, result=result))
        return result
