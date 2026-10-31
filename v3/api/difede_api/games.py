import hashlib
import json

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import exists, or_, select, func

from .catalog import CATALOG
from .db import utcnow
from .models import Audit, Game, GameEvent, GroupAdmin, Membership, Operation, Person, Round, Score, Scorekeeper, Seat, User, ViewerGroup
from .schemas import PegDetails, PenaltyDetails
from .security import is_admin, is_member, user_groups


class GameError(Exception):
    def __init__(self, detail, status=422, code=None, missing=None):
        self.status = status
        self.body = {'detail': detail}
        if code:
            self.body['code'] = code
        if missing is not None:
            self.body['missing'] = missing


def seats(db, game_id):
    return db.scalars(select(Seat).where(Seat.game_id == game_id).order_by(Seat.position)).all()


def permissions(db, user, game):
    score = bool(user.person_id and db.get(Seat, (game.id, user.person_id))) or bool(db.get(Scorekeeper, (game.id, user.id)))
    manage = game.organizer_id == user.id or (game.group_id is not None and is_admin(db, user, game.group_id))
    return {'can_score': score, 'can_manage': manage}


def can_view(db, user, game):
    if not game or game.deleted:
        return False
    perms = permissions(db, user, game)
    if perms['can_score'] or perms['can_manage']:
        return True
    groups = {g['id'] for g in user_groups(db, user)}
    return bool(groups.intersection(db.scalars(select(ViewerGroup.group_id).where(ViewerGroup.game_id == game.id)).all()))


def visible_games_query(db, user):
    memberships = user_groups(db, user)
    groups = [g['id'] for g in memberships]
    admin_groups = [g['id'] for g in memberships if g['role'] == 'admin']
    return select(Game).where(Game.deleted.is_(False), or_(
        Game.organizer_id == user.id,
        exists().where(Seat.game_id == Game.id, Seat.person_id == (user.person_id or -1)),
        exists().where(Scorekeeper.game_id == Game.id, Scorekeeper.user_id == user.id),
        Game.group_id.in_(admin_groups),
        exists().where(ViewerGroup.game_id == Game.id, ViewerGroup.group_id.in_(groups)),
    ))


def summaries(db, games, user):
    """List cards omit board cells; bulk loads keep history independent of round count."""
    ids = [g.id for g in games]
    if not ids:
        return []
    groups = user_groups(db, user)
    admin_groups = {g['id'] for g in groups if g['role'] == 'admin'}
    roster_by_game = {gid: [] for gid in ids}
    names = {}
    for seat, person in db.execute(select(Seat, Person).join(Person, Person.id == Seat.person_id).where(
            Seat.game_id.in_(ids)).order_by(Seat.position)):
        roster_by_game[seat.game_id].append(seat)
        names[person.id] = person.name
    viewers = {gid: [] for gid in ids}
    for sharing in db.scalars(select(ViewerGroup).where(ViewerGroup.game_id.in_(ids))):
        viewers[sharing.game_id].append(sharing.group_id)
    keeper_games = set(db.scalars(select(Scorekeeper.game_id).where(Scorekeeper.game_id.in_(ids), Scorekeeper.user_id == user.id)))
    totals = {}
    for gid, pid, total in db.execute(select(Score.game_id, Score.person_id, func.sum(Score.value)).where(
            Score.game_id.in_([g.id for g in games if not g.result])).group_by(Score.game_id, Score.person_id)):
        totals[(gid, pid)] = total or 0
    output = []
    for game in games:
        roster = roster_by_game[game.id]
        if game.result:
            standings = [dict(r, name=names.get(r['player_id'], r.get('name', 'Player'))) for r in game.result.get('standings', [])]
        else:
            ordered = sorted(roster, key=lambda s: totals.get((game.id, s.person_id), 0), reverse=game.scoring_direction == 'high')
            standings = []
            rank, last = 0, None
            for i, seat in enumerate(ordered, 1):
                total = totals.get((game.id, seat.person_id), 0)
                if total != last:
                    rank, last = i, total
                standings.append(dict(player_id=seat.person_id, name=names[seat.person_id], total=total, rank=rank, winner=False))
        output.append(dict(id=game.id, slug=game.slug, title=game.title, status=game.status,
            revision=game.revision, mode=game.mode, scoring_direction=game.scoring_direction, target_score=game.target_score,
            group_id=game.group_id, viewer_group_ids=viewers[game.id],
            players=[dict(id=s.person_id, name=names[s.person_id], **({'color': s.color} if s.color else {})) for s in roster],
            rounds=[], scores=[], standings=standings,
            permissions={'can_score': game.id in keeper_games or any(s.person_id == user.person_id for s in roster),
                         'can_manage': game.organizer_id == user.id or game.group_id in admin_groups},
            created_at=iso(game.created_at), finished_at=iso(game.finished_at), imported=game.imported,
            **imported_result(game),
            rule_note=CATALOG.get(game.slug, {}).get('rule_note', 'Legacy history')))
    return output


def accessible_game(db, user, game_id, lock=False, allow_deleted=False):
    statement = select(Game).where(Game.id == game_id)
    if lock:
        statement = statement.with_for_update()
    game = db.scalar(statement)
    visible = bool(game and allow_deleted and permissions(db, user, game)['can_manage']) or can_view(db, user, game)
    if not visible:
        raise GameError('Game not found or not shared with you.', 404)
    return game


def active_standings(db, game):
    roster = seats(db, game.id)
    totals = {s.person_id: 0 for s in roster}
    for cell in db.scalars(select(Score).where(Score.game_id == game.id)):
        if cell.person_id in totals and cell.value is not None:
            totals[cell.person_id] += cell.value
    ordered = sorted(roster, key=lambda s: totals[s.person_id], reverse=game.scoring_direction == 'high')
    rows = []
    last_total = None
    rank = 0
    for i, seat in enumerate(ordered, 1):
        total = totals[seat.person_id]
        if total != last_total:
            rank = i
            last_total = total
        rows.append({'player_id': seat.person_id, 'name': db.get(Person, seat.person_id).name,
                     'total': total, 'rank': rank, 'winner': False})
    return rows


def iso(value):
    if not value:
        return None
    return value.isoformat() + ('Z' if not value.tzinfo else '')


def imported_result(game):
    if not game.imported:
        return {}
    return {'imported_result': {'winners': [
        {'player_id': row.get('winner_id'), 'winning_score': row.get('winning_score'), 'is_tie': row.get('is_tie')}
        for row in game.legacy.get('original_results', [])]}}


def serialize(db, game, user):
    roster = seats(db, game.id)
    policy = CATALOG.get(game.slug)
    standings = game.result.get('standings', []) if game.result else active_standings(db, game)
    # Names can change without rewriting the immutable result's identity and numbers.
    standings = [dict(r, name=db.get(Person, r['player_id']).name) for r in standings]
    return {'id': game.id, 'slug': game.slug, 'title': game.title, 'status': game.status,
            'revision': game.revision, 'mode': game.mode, 'scoring_direction': game.scoring_direction,
            'target_score': game.target_score, 'group_id': game.group_id,
            'viewer_group_ids': list(db.scalars(select(ViewerGroup.group_id).where(ViewerGroup.game_id == game.id))),
            'players': [dict(id=s.person_id, name=db.get(Person, s.person_id).name,
                             **({'color': s.color} if s.color else {})) for s in roster],
            'rounds': [dict(number=r.number, label=r.label, **({'side': r.side} if r.side else {}))
                       for r in db.scalars(select(Round).where(Round.game_id == game.id).order_by(Round.number))],
            'scores': [dict(player_id=c.person_id, round_number=c.round_number, value=c.value,
                            version=c.version, metadata=c.extra or {}) for c in db.scalars(select(Score).where(
                                Score.game_id == game.id).order_by(Score.round_number, Score.person_id))],
            'standings': standings, 'permissions': permissions(db, user, game),
            'created_at': iso(game.created_at), 'finished_at': iso(game.finished_at),
            'imported': game.imported,
            **imported_result(game),
            'rule_note': ('Imported snapshot: original results are retained. Reopening applies V3 pilot scoring. '
                          if game.imported else '') + (policy['rule_note'] if policy else 'Legacy variant retained as imported history.')}


def record(db, user, game, action, payload):
    db.add(Audit(game_id=game.id, actor_id=user.id, action=action, payload=payload))
    db.add(GameEvent(game_id=game.id, revision=game.revision, action=action, payload=payload))


def create_game(db, user, data):
    policy = CATALOG.get(data.slug)
    if not policy:
        raise GameError('Unknown game type.')
    mode = data.mode or policy['modes'][0]
    if mode not in policy['modes']:
        raise GameError('This game does not support that scoring mode.')
    if len(set(data.player_ids)) != len(data.player_ids):
        raise GameError('Each person may appear only once.')
    if not policy['min_players'] <= len(data.player_ids) <= policy['max_players']:
        raise GameError(f"Select {policy['min_players']} to {policy['max_players']} players.")
    selected_groups = set(data.viewer_group_ids) | {data.group_id}
    if any(not is_member(db, user, g) for g in selected_groups):
        raise GameError('You must belong to each selected group.', 403)
    requester_groups = {g['id'] for g in user_groups(db, user)}
    available_people = set(db.scalars(select(Membership.person_id).where(
        Membership.group_id.in_(requester_groups), Membership.status == 'active')))
    for pid in data.player_ids:
        person = db.get(Person, pid)
        if pid not in available_people or not person or person.archived or person.purged:
            raise GameError('Select active people from your groups.')
    for uid in set(data.scorekeeper_ids):
        keeper = db.get(User, uid)
        if not keeper or not keeper.active or not keeper.approved or not keeper.verified or not any(
                is_member(db, keeper, g) for g in requester_groups):
            raise GameError('Scorekeepers must be active accounts sharing one of your groups.')
    participant_account = db.scalar(select(User.id).where(User.person_id.in_(data.player_ids),
        User.active.is_(True), User.approved.is_(True), User.verified.is_(True)).limit(1))
    if participant_account is None and not data.scorekeeper_ids:
        raise GameError('Assign a signed-in scorekeeper for a table of guests.', code='scorer_required')
    direction = 'high' if mode == 'single' else (data.scoring_direction or policy['scoring_direction'])
    if mode != 'single' and data.slug not in ('basic-other', 'uno-classic', 'uno-flip', 'dutch-blitz') and direction != policy['scoring_direction']:
        raise GameError('This game has a fixed scoring direction.')
    target = data.target_score if 'target_score' in data.model_fields_set else policy['default_target']
    if target is not None and target > 1000000:
        raise GameError('Target is too large.')
    if policy['fixed_rounds'] or mode != 'rounds':
        target = None
    game = Game(slug=data.slug, title=data.title or policy['name'], mode=mode,
                scoring_direction=direction, target_score=target, group_id=data.group_id,
                organizer_id=user.id)
    db.add(game)
    db.flush()
    for i, pid in enumerate(data.player_ids):
        db.add(Seat(game_id=game.id, person_id=pid, position=i))
    for gid in set(data.viewer_group_ids):
        db.add(ViewerGroup(game_id=game.id, group_id=gid))
    for uid in set(data.scorekeeper_ids):
        db.add(Scorekeeper(game_id=game.id, user_id=uid))
    numbers = policy['fixed_rounds'] or ([1] if mode != 'winner' else [])
    for number in numbers:
        db.add(Round(game_id=game.id, number=number,
                     label=policy['round_labels'].get(str(number), str(number)),
                     side='light' if data.slug == 'uno-flip' else None))
    db.flush()
    record(db, user, game, 'created', data.model_dump(mode='json'))
    return serialize(db, game, user)


def put_cell(db, game, pid, number, value, metadata=None):
    cell = db.get(Score, (game.id, pid, number))
    if not cell:
        cell = Score(game_id=game.id, person_id=pid, round_number=number, version=0)
        db.add(cell)
    cell.value = value
    cell.version += 1
    if metadata is not None:
        cell.extra = metadata
    return cell


def finish(db, game, data):
    roster = seats(db, game.id)
    ids = {s.person_id for s in roster}
    if data.early:
        game.status = 'incomplete'
        game.finished_at = utcnow()
        db.flush()
        game.result = {'standings': active_standings(db, game), 'official': False}
        return
    if game.mode in ('single', 'winner'):
        if data.winner_id not in ids:
            raise GameError('Choose a winner from this game.')
        if game.mode == 'winner':
            if data.details:
                raise GameError('Winner-only games do not accept score details.')
            rows = [{'player_id': s.person_id, 'name': db.get(Person, s.person_id).name, 'total': 0,
                     'rank': 1 if s.person_id == data.winner_id else 2, 'winner': s.person_id == data.winner_id} for s in roster]
        else:
            try:
                if game.slug == 'trouble':
                    pegs = PegDetails.model_validate(data.details or {}).pegs
                    if len(pegs) != len(ids) or {p.player_id for p in pegs} != ids:
                        raise GameError('Enter peg counts for exactly every player.')
                    if len({p.color for p in pegs}) != len(pegs):
                        raise GameError('Each player needs a different color.')
                    for p in pegs:
                        if p.finish + p.board + p.home != 4:
                            raise GameError('Each player must have exactly four pegs.')
                        if p.player_id == data.winner_id and p.finish != 4:
                            raise GameError('The winner must have all four pegs finished.')
                        if p.player_id != data.winner_id and p.finish == 4:
                            raise GameError('Only the selected winner may have all pegs finished.')
                    strength = 25 + sum(5 * p.board + 10 * p.home for p in pegs if p.player_id != data.winner_id)
                    for p in pegs:
                        put_cell(db, game, p.player_id, 1, strength if p.player_id == data.winner_id else 0,
                                 {'pegs': p.model_dump(), 'sow': strength if p.player_id == data.winner_id else 0})
                        db.get(Seat, (game.id, p.player_id)).color = p.color
                elif game.slug == 'kings-corner':
                    penalties = PenaltyDetails.model_validate(data.details or {}).penalties
                    if len(penalties) != len(ids) - 1 or {p.player_id for p in penalties} != ids - {data.winner_id}:
                        raise GameError('Enter penalties for exactly every opponent.')
                    strength = sum(p.score for p in penalties)
                    put_cell(db, game, data.winner_id, 1, strength, {'sow': strength, 'type': 'single_round'})
                    for p in penalties:
                        put_cell(db, game, p.player_id, 1, 0, {'penalty': p.score, 'type': 'loser'})
                else:
                    raise GameError('This game has no single-result policy.')
            except ValidationError:
                raise GameError('Invalid result details. Check every count and player.') from None
            db.flush()
            rows = active_standings(db, game)
            for row in rows:
                row['winner'] = row['player_id'] == data.winner_id
                row['rank'] = 1 if row['winner'] else 2
    else:
        if data.winner_id is not None or data.details:
            raise GameError('Round game winners are calculated from saved scores.')
        numbers = list(db.scalars(select(Round.number).where(Round.game_id == game.id).order_by(Round.number)))
        cells = {(c.person_id, c.round_number): c.value for c in db.scalars(select(Score).where(Score.game_id == game.id))}
        if not CATALOG.get(game.slug, {}).get('fixed_rounds'):
            while len(numbers) > 1 and all(cells.get((pid, numbers[-1])) is None for pid in ids):
                numbers.pop()
        missing = [{'player_id': pid, 'round_number': number} for number in numbers for pid in sorted(ids)
                   if cells.get((pid, number)) is None]
        if not numbers or missing:
            raise GameError('Save a score for every required player and round.', missing=missing)
        rows = active_standings(db, game)
        for row in rows:
            row['winner'] = row['rank'] == 1
    game.status = 'completed'
    game.finished_at = utcnow()
    rows.sort(key=lambda row: row['rank'])
    game.result = {'standings': rows, 'official': True, 'policy': game.rules_version}


def command(db, user, game_id, action, data, round_number=None):
    game = accessible_game(db, user, game_id, lock=True, allow_deleted=action == 'delete')
    perms = permissions(db, user, game)
    needs_manage = action in ('reopen', 'delete')
    if not perms['can_manage' if needs_manage else 'can_score']:
        raise GameError('You are watching this game. Only players or assigned scorekeepers can save.', 403)
    payload = data.model_dump(mode='json') | {'action': action, 'round_number_path': round_number}
    hashed = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    previous = db.get(Operation, (game.id, user.id, data.operation_id))
    if previous:
        if previous.payload_hash != hashed:
            raise GameError('That operation ID was already used for a different change.', 409, 'idempotency_conflict')
        return previous.result
    if game.deleted:
        raise GameError('This game has been deleted.', 409)
    if action not in ('reopen', 'delete') and game.status != 'active':
        raise GameError('This game has finished. A manager must reopen it before editing.', 409, 'game_finished')
    if hasattr(data, 'expected_revision') and data.expected_revision != game.revision:
        raise GameError('The game changed. Review the latest saved state and try again.', 409, 'revision_conflict')
    policy = CATALOG.get(game.slug)
    if not policy:
        raise GameError('This historical variant is read-only.')
    if action == 'score':
        if game.mode != 'rounds':
            raise GameError('Use this game’s result form to save its scores.')
        if not db.get(Seat, (game.id, data.player_id)) or not db.get(Round, (game.id, data.round_number)):
            raise GameError('Unknown player or round.')
        if data.score is not None and data.score < 0 and not policy['allow_negative']:
            raise GameError('This game does not accept negative scores.')
        cell = db.get(Score, (game.id, data.player_id, data.round_number))
        if (cell.version if cell else 0) != data.expected_version:
            raise GameError('This score changed on another screen. Review both values.', 409, 'cell_conflict')
        payload['previous_value'] = cell.value if cell else None
        payload['previous_version'] = cell.version if cell else 0
        put_cell(db, game, data.player_id, data.round_number, data.score)
    elif action == 'round':
        if game.mode != 'rounds' or policy['fixed_rounds']:
            raise GameError('This game does not support adding rounds.')
        if data.side and game.slug != 'uno-flip':
            raise GameError('Only UNO Flip has light and dark sides.')
        existing = list(db.scalars(select(Round.number).where(Round.game_id == game.id)))
        number = max(existing, default=0) + 1
        if number > 1000:
            raise GameError('Round limit reached.')
        db.add(Round(game_id=game.id, number=number, label=str(number),
                     side=(data.side or 'light') if game.slug == 'uno-flip' else None))
    elif action == 'side':
        row = db.get(Round, (game.id, round_number))
        if game.slug != 'uno-flip' or not row:
            raise GameError('Only an existing UNO Flip round has a side.')
        row.side = data.side
    elif action == 'finish':
        finish(db, game, data)
    elif action == 'reopen':
        if game.status == 'active':
            raise GameError('This game is already active.', 409)
        payload['previous_result'] = game.result
        payload['previous_status'] = game.status
        payload['previous_rules_version'] = game.rules_version
        game.status = 'active'
        game.finished_at = None
        game.result = None
        game.rules_version = 'v3-pilot-1'
    elif action == 'delete':
        game.deleted = True
    else:
        raise GameError('Unknown game command.')
    game.revision += 1
    db.flush()
    record(db, user, game, action, payload)
    result = {'ok': True} if action == 'delete' else serialize(db, game, user)
    db.add(Operation(game_id=game.id, user_id=user.id, operation_id=data.operation_id,
                     payload_hash=hashed, result=result))
    db.flush()
    return result
