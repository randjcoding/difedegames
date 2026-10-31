from datetime import timedelta
from itertools import islice
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import exists, func, literal, or_, select, text, update

from .catalog import CATALOG
from .db import utcnow
from .dependencies import authenticated
from .games import create_game
from .management import audit, group_row, manage_group, management_lock
from .models import GameLayout, Group, LoginThrottle, Membership, Person, User
from .schemas import GameCreate, Name, Payload, PositiveID
from .security import aware, digest, is_member, is_superadmin, user_groups
from .privacy import public_flag, sql_flag

router = APIRouter(prefix='/api')


class LayoutSave(Payload):
    group_id: PositiveID
    slug: str = Field(max_length=60)
    name: str = Field(min_length=1, max_length=80)
    player_ids: list[PositiveID] = Field(min_length=1, max_length=10)
    mode: Literal['rounds', 'single', 'winner'] | None = None
    scoring_direction: Literal['low', 'high'] | None = None
    target_score: PositiveID | None = None
    title: Name | None = None
    is_default: bool = False


class LayoutStart(Payload):
    viewer_group_ids: list[PositiveID] = Field(default_factory=list, max_length=30)
    scorekeeper_ids: list[PositiveID] = Field(default_factory=list, max_length=10)


def layout_payload(row):
    return {key: getattr(row, key) for key in ('id', 'group_id', 'slug', 'name', 'player_ids', 'mode',
        'scoring_direction', 'target_score', 'title', 'is_default', 'created_by')}


def layout_row(db, user, lid, manage=False):
    row = db.get(GameLayout, lid)
    if not row:
        raise HTTPException(404, 'Saved setup not found.')
    group_row(db, row.group_id)
    if manage:
        manage_group(db, user, row.group_id)
    elif not is_member(db, user, row.group_id):
        raise HTTPException(403, 'This saved setup is not shared with your group.')
    return row


def validate_layout(db, user, data):
    manage_group(db, user, data.group_id)
    policy = CATALOG.get(data.slug)
    if not policy:
        raise HTTPException(422, 'Choose an available game type.')
    mode = data.mode or policy['modes'][0]
    if mode not in policy['modes']:
        raise HTTPException(422, 'This game does not support that mode.')
    if len(data.player_ids) != len(set(data.player_ids)) or not policy['min_players'] <= len(data.player_ids) <= policy['max_players']:
        raise HTTPException(422, 'Select a valid distinct roster within this game’s player limits.')
    group_ids = [g['id'] for g in user_groups(db, user)]
    visible = set(db.scalars(select(Person.id).join(Membership, Membership.person_id == Person.id).where(
        Membership.group_id.in_(group_ids), Membership.status == 'active', Person.archived.is_(False), Person.purged.is_(False))))
    if not set(data.player_ids) <= visible:
        raise HTTPException(422, 'One or more saved players are not available in your groups.')
    direction = data.scoring_direction or policy['scoring_direction']
    if mode == 'single':
        direction = 'high'
    elif data.slug not in ('basic-other', 'uno-classic', 'uno-flip', 'dutch-blitz') and direction != policy['scoring_direction']:
        raise HTTPException(422, 'This game has a fixed scoring direction.')
    if data.target_score and data.target_score > 1000000:
        raise HTTPException(422, 'Target is too large.')
    return mode, direction


def clear_default(db, gid, slug):
    db.execute(update(GameLayout).where(GameLayout.group_id == gid, GameLayout.slug == slug).values(is_default=False))
    db.flush()


@router.get('/layouts')
def list_layouts(request: Request, group_id: int | None = None, slug: str | None = None):
    with authenticated(request) as (db, user, _):
        groups = [g['id'] for g in user_groups(db, user)]
        if group_id is not None and group_id not in groups:
            raise HTTPException(403, 'Group membership is required.')
        query = select(GameLayout).where(GameLayout.group_id.in_(groups))
        if group_id is not None:
            query = query.where(GameLayout.group_id == group_id)
        if slug:
            query = query.where(GameLayout.slug == slug)
        return {'layouts': [layout_payload(row) for row in db.scalars(query.order_by(GameLayout.is_default.desc(), GameLayout.name))]}


@router.post('/layouts')
def save_layout(data: LayoutSave, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        mode, direction = validate_layout(db, user, data)
        if not data.name.strip():
            raise HTTPException(422, 'Give the saved setup a name.')
        if data.is_default:
            clear_default(db, data.group_id, data.slug)
        row = GameLayout(group_id=data.group_id, slug=data.slug, name=data.name.strip(), player_ids=data.player_ids,
            mode=mode, scoring_direction=direction, target_score=data.target_score, title=data.title,
            is_default=data.is_default, created_by=user.id)
        db.add(row)
        db.flush()
        audit(db, user, 'saved_setup_created', {'layout_id': row.id, 'setup': layout_payload(row)})
        return layout_payload(row)


@router.put('/layouts/{lid}')
def update_layout(lid: int, data: LayoutSave, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        row = layout_row(db, user, lid, True)
        if row.group_id != data.group_id or row.slug != data.slug:
            raise HTTPException(422, 'A saved setup cannot change its host group or game type.')
        mode, direction = validate_layout(db, user, data)
        if not data.name.strip():
            raise HTTPException(422, 'Give the saved setup a name.')
        before = layout_payload(row)
        if data.is_default:
            clear_default(db, data.group_id, data.slug)
        for key, value in data.model_dump().items():
            setattr(row, key, value)
        row.name, row.mode, row.scoring_direction, row.updated_at = data.name.strip(), mode, direction, utcnow()
        db.flush()
        audit(db, user, 'saved_setup_updated', {'layout_id': lid, 'before': before, 'after': layout_payload(row)})
        return layout_payload(row)


@router.delete('/layouts/{lid}')
def delete_layout(lid: int, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        row = layout_row(db, user, lid, True)
        audit(db, user, 'saved_setup_deleted', {'layout_id': lid, 'setup': layout_payload(row)})
        db.delete(row)
        return {'ok': True}


@router.post('/layouts/{lid}/set-default')
def default_layout(lid: int, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        row = layout_row(db, user, lid, True)
        clear_default(db, row.group_id, row.slug)
        row.is_default, row.updated_at = True, utcnow()
        audit(db, user, 'saved_setup_default_changed', {'layout_id': lid, 'group_id': row.group_id})
        return layout_payload(row)


@router.post('/layouts/{lid}/start')
def start_layout(lid: int, data: LayoutStart, request: Request):
    with authenticated(request, True) as (db, user, _):
        row = layout_row(db, user, lid)
        return create_game(db, user, GameCreate(slug=row.slug, group_id=row.group_id, player_ids=row.player_ids,
            mode=row.mode, scoring_direction=row.scoring_direction, target_score=row.target_score, title=row.title,
            viewer_group_ids=data.viewer_group_ids, scorekeeper_ids=data.scorekeeper_ids))


def directory_budget(db, uid):
    key = digest('directory:' + str(uid))
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(key[:15], 16)})
    row = db.scalar(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())
    now = utcnow()
    if not row:
        row = LoginThrottle(key=key, count=0, window_start=now)
        db.add(row)
    if aware(row.window_start) < now - timedelta(minutes=1):
        row.count, row.window_start = 0, now
    if row.count >= 30:
        return False
    row.count += 1
    return True


def public_name(person):
    name = person.name.strip()
    last = (person.details.get('last_name') or '').strip()
    if last:
        if person.privacy.get('show_full_last_name'):
            return name if name.casefold().endswith(last.casefold()) else name + ' ' + last
        name = re.sub(r'(?<!\w)' + re.escape(last) + r'(?!\w)', '', name, flags=re.IGNORECASE)
        name = ' '.join(name.split()) or 'Player'
        name += ' ' + last[0] + '.'
    elif not person.privacy.get('show_full_last_name'):
        pieces = name.split()
        name = pieces[0] + (' ' + pieces[-1][0] + '.' if len(pieces) > 1 else '')
    return name


@router.get('/directory/groups')
def directory_groups(request: Request, q: str = Query(min_length=2, max_length=100)):
    if len(q.strip()) < 2:
        raise HTTPException(422, 'Enter at least two name characters.')
    with authenticated(request) as (db, user, _):
        if not directory_budget(db, user.id):
            raise HTTPException(429, 'Please wait a minute before searching again.')
        superadmin = is_superadmin(user)
        own = {g['id'] for g in user_groups(db, user)} if not superadmin else set()
        output = []
        rows = list(db.scalars(select(Group).where(Group.archived.is_(False), Group.name.icontains(q.strip(), autoescape=True),
            or_(literal(superadmin), Group.id.in_(own), sql_flag(db, Group.settings, 'is_discoverable', True)))
            .order_by(Group.name).limit(30)))
        ids = [g.id for g in rows]
        counts = dict(db.execute(select(Membership.group_id, func.count()).where(Membership.group_id.in_(ids),
            Membership.status == 'active').group_by(Membership.group_id)).all())
        states = dict(db.execute(select(Membership.group_id, Membership.status).where(Membership.group_id.in_(ids),
            Membership.person_id == user.person_id)).all()) if user.person_id else {}
        for group in rows:
            trusted = group.id in own or superadmin
            show_roster = public_flag(group.settings, 'show_roster', True)
            output.append({'id': group.id, 'name': group.name, 'show_roster': show_roster,
                'member_count': counts.get(group.id, 0) if trusted or show_roster else None,
                'membership_status': states.get(group.id)})
        return {'groups': output}


@router.get('/directory/people')
def directory_people(request: Request, q: str = Query(min_length=2, max_length=100)):
    if len(q.strip()) < 2:
        raise HTTPException(422, 'Enter at least two name characters.')
    with authenticated(request) as (db, user, _):
        if not directory_budget(db, user.id):
            raise HTTPException(429, 'Please wait a minute before searching again.')
        superadmin = is_superadmin(user)
        own = {g['id'] for g in user_groups(db, user)} if not superadmin else set()
        trusted_sql = or_(literal(superadmin), Person.id == user.person_id,
            exists().where(Membership.person_id == Person.id, Membership.status == 'active', Membership.group_id.in_(own)))
        public_group = sql_flag(db, Group.settings, 'is_discoverable', True) & sql_flag(db, Group.settings, 'show_roster', True)
        public_roster = exists(select(Membership.id).join(Group, Group.id == Membership.group_id).where(
            Membership.person_id == Person.id, Membership.status == 'active', Group.archived.is_(False), public_group))
        query = select(Person.id, Person.name, Person.privacy,
            Person.details['last_name'].as_string().label('last_name')).where(Person.archived.is_(False), Person.purged.is_(False),
            Person.name.icontains(q.strip(), autoescape=True), or_(trusted_sql,
                sql_flag(db, Person.privacy, 'is_discoverable', True)
                & ~sql_flag(db, Person.privacy, 'is_minor', False, True) & public_roster)).order_by(Person.name)
        output = []
        stream = iter(db.execute(query).yield_per(50))
        from types import SimpleNamespace
        while batch := list(islice(stream, 50)):
            ids = [p.id for p in batch]
            visible_groups = {}
            for pid, gid in db.execute(select(Membership.person_id, Group.id).join(Group, Group.id == Membership.group_id).where(
                Membership.person_id.in_(ids), Membership.status == 'active', Group.archived.is_(False),
                or_(literal(superadmin), Membership.person_id == user.person_id, Group.id.in_(own), public_group))):
                visible_groups.setdefault(pid, set()).add(gid)
            for person in batch:
                gids = visible_groups.get(person.id, set())
                trusted = superadmin or person.id == user.person_id or bool(gids & own)
                shown_name = person.name if trusted else public_name(SimpleNamespace(name=person.name,
                    details={'last_name': person.last_name}, privacy=person.privacy or {}))
                if q.strip().casefold() not in shown_name.casefold():
                    continue
                output.append({'id': person.id, 'name': shown_name, 'group_ids': sorted(gids)})
                if len(output) >= 30:
                    break
            if len(output) >= 30:
                break
        account_people = set(db.scalars(select(User.person_id).where(User.person_id.in_([p['id'] for p in output]))))
        for person in output:
            person['has_account'] = person['id'] in account_people
        return {'people': output}
