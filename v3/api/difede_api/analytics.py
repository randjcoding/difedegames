"""Authorized completed-game analytics; outcomes and point units stay explicit.

No board serialization or per-person queries: select game result snapshots and
batch roster metadata. Only the explicit hand-record endpoint reads score cells,
as a bounded streaming SQL projection rather than loading complete boards.
"""
from collections import defaultdict
from datetime import timezone
import hashlib
import json
import math

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import select

from .catalog import CATALOG
from .dependencies import authenticated
from .games import iso, visible_games_query
from .models import Game, Group, Membership, Person, Score, Seat
from .security import is_member, is_superadmin, user_groups

router = APIRouter(prefix='/api', tags=['statistics'])


def parameters(scope: str = Query('visible', pattern='^(visible|site)$'),
               selected_group_id: int | None = Query(None, alias='group_id', ge=1, le=2147483647),
               selected_person_id: int | None = Query(None, alias='person_id', ge=1, le=2147483647),
               slug: str | None = Query(None, min_length=1, max_length=60),
               title: str | None = Query(None, min_length=1, max_length=160),
               mode: str | None = Query(None, pattern='^(rounds|single|winner)$'),
               direction: str | None = Query(None, pattern='^(low|high)$'),
               q: str | None = Query(None, max_length=120),
               page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    return dict(scope=scope, group_id=selected_group_id, person_id=selected_person_id, slug=slug,
                title=title, mode=mode, direction=direction, q=q, page=page, page_size=page_size)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def chronology(game):
    value = game['finished_at'] or game['created_at']
    if value is None:
        return (0, game['id'])
    return ((value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp(), game['id'])


def pagination(items, options):
    start = (options['page'] - 1) * options['page_size']
    return items[start:start + options['page_size']], {
        'page': options['page'], 'page_size': options['page_size'], 'total': len(items),
        'pages': math.ceil(len(items) / options['page_size'])}


def category(game):
    title = game['title'] if game['slug'] == 'basic-other' else None
    metric = ('outcome_only' if game['mode'] == 'winner' else
              'strength_of_win' if game['mode'] == 'single' else 'round_total')
    key = [game['slug'], title, game['mode'], game['scoring_direction'], metric]
    name = CATALOG.get(game['slug'], {}).get('name', game['slug'])
    metric_label = {'outcome_only': 'winner only', 'strength_of_win': 'winning strength', 'round_total': 'round total'}[metric]
    return {'id': hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:24],
            'slug': game['slug'], 'name': name, 'title': title, 'mode': game['mode'],
            'scoring_direction': game['scoring_direction'], 'score_metric': metric,
            'label': f"{title or name} · {game['mode']} · {game['scoring_direction']} · {metric_label}"}


class Dataset:
    def __init__(self, db, user, options):
        self.db, self.user, self.options = db, user, dict(options)
        self.superadmin = is_superadmin(user)
        if options['scope'] == 'site' and not self.superadmin:
            raise HTTPException(403, 'Site-wide statistics require a site administrator.')
        if options['title'] is not None and options['slug'] not in (None, 'basic-other'):
            raise HTTPException(422, 'An exact custom title applies only to Basic / Other.')
        statement = visible_games_query(db, user).where(Game.status == 'completed')
        if options['group_id'] is not None:
            statement = statement.where(Game.group_id == options['group_id'])
        if options['person_id'] is not None:
            statement = statement.where(Game.id.in_(select(Seat.game_id).where(Seat.person_id == options['person_id'])))
        if options['slug']:
            statement = statement.where(Game.slug == options['slug'])
        if options['title'] is not None:
            statement = statement.where(Game.slug == 'basic-other', Game.title == options['title'])
        if options['mode']:
            statement = statement.where(Game.mode == options['mode'])
        if options['direction']:
            statement = statement.where(Game.scoring_direction == options['direction'])
        if options['q'] and options['q'].strip():
            statement = statement.where(Game.title.icontains(options['q'].strip(), autoescape=True))
        self.ids_query = statement.with_only_columns(Game.id)
        fields = (Game.id, Game.slug, Game.title, Game.mode, Game.scoring_direction,
                  Game.group_id, Game.created_at, Game.finished_at, Game.imported, Game.result)
        self.games = {row['id']: dict(row) for row in db.execute(statement.with_only_columns(*fields)).mappings()}
        self.rosters, self.people = defaultdict(list), {}
        # Only identity/name/privacy columns; no accounts, email, password or full person legacy payload.
        roster_query = select(Seat.game_id, Seat.person_id, Person.name, Person.privacy, Person.purged).join(
            Person, Person.id == Seat.person_id).where(Seat.game_id.in_(self.ids_query)).order_by(Seat.game_id, Seat.position)
        for gid, pid, name, privacy, purged in db.execute(roster_query):
            self.rosters[gid].append(pid)
            self.people[pid] = {'id': pid, 'name': name, 'privacy': privacy or {}, 'purged': purged}
        self.categories, self.game_categories, self.outcomes = {}, {}, {}
        for gid, game in self.games.items():
            cat = category(game)
            self.categories[cat['id']] = cat
            self.game_categories[gid] = cat['id']
            self.outcomes[gid] = self.outcome(game)

    def outcome(self, game):
        result = game['result'] or {}
        rows = result.get('standings', [])
        roster = set(self.rosters[game['id']])
        totals = {row.get('player_id'): row['total'] for row in rows
                  if row.get('player_id') in roster and number(row.get('total'))}
        winners = {}
        if result.get('official') is not False:
            if result.get('policy') == 'v2-original':
                # Missing original winners are unknown, even if the scores suggest an answer.
                for row in result.get('original_results', []):
                    pid = row.get('winner_id')
                    if pid in roster:
                        score = row.get('winning_score')
                        winners[pid] = {'winning_score': score if number(score) else None,
                                        'is_tie': bool(row.get('is_tie'))}
            else:
                for row in rows:
                    pid = row.get('player_id')
                    if row.get('winner') and pid in roster:
                        score = row.get('total')
                        winners[pid] = {'winning_score': score if number(score) else None, 'is_tie': False}
        if len(winners) > 1:
            for winner in winners.values():
                winner['is_tie'] = True
        return {'known': bool(winners), 'winners': winners, 'totals': totals}

    def name(self, pid):
        person = self.people.get(pid)
        if not person or person['purged']:
            return 'Deleted player'
        name = person['name'] or 'Player'
        if self.superadmin or pid == self.user.person_id or person['privacy'].get('show_full_last_name'):
            return name
        pieces = name.split()
        return pieces[0] + (' ' + pieces[-1][0] + '.' if len(pieces) > 1 else '')

    def context(self):
        return {'scope': 'site' if self.superadmin else 'visible',
                'filters': {key: value for key, value in self.options.items()
                            if key not in ('page', 'page_size', 'scope') and value is not None},
                'labels': {'scope': 'All site completed games' if self.superadmin else 'Authorized completed games only',
                           'win_percentage': 'Wins divided by decided completed games; unknown outcomes excluded',
                           'score_comparisons': 'Points compared only within one game, title, mode, direction and scoring metric'}}

    def brief(self, game):
        result = self.outcomes[game['id']]
        return {key: game[key] for key in ('id', 'title', 'slug', 'mode', 'scoring_direction', 'group_id')} | {
            'category_id': self.game_categories[game['id']], 'finished_at': iso(game['finished_at']),
            'created_at': iso(game['created_at']), 'player_count': len(self.rosters[game['id']]),
            'result_known': result['known'], 'winners': [dict(player_id=pid, name=self.name(pid), **winner)
                for pid, winner in result['winners'].items()]}

    def history(self):
        ordered = sorted(self.games.values(), key=chronology, reverse=True)
        page, meta = pagination(ordered, self.options)
        return [self.brief(game) for game in page], meta

    def category_sort(self, cat):
        order = {slug: index for index, slug in enumerate(CATALOG)}
        return (order.get(cat['slug'], 999), cat['label'])

    def category_counts(self):
        counts = {key: {'category': cat, 'games': 0, 'known_results': 0, 'unknown_results': 0}
                  for key, cat in self.categories.items()}
        for gid in self.games:
            row = counts[self.game_categories[gid]]
            row['games'] += 1
            row['known_results' if self.outcomes[gid]['known'] else 'unknown_results'] += 1
        return sorted(counts.values(), key=lambda row: self.category_sort(row['category']))

    def summary(self):
        known = sum(result['known'] for result in self.outcomes.values())
        return {'games': len(self.games), 'known_results': known, 'unknown_results': len(self.games) - known,
                'players': len(self.people), 'player_appearances': sum(len(row) for row in self.rosters.values()),
                'solo_wins': sum(not winner['is_tie'] for result in self.outcomes.values() for winner in result['winners'].values()),
                'shared_wins': sum(winner['is_tie'] for result in self.outcomes.values() for winner in result['winners'].values())}

    def player_statistics(self, category_id=None):
        if hasattr(self, '_player_statistics'):
            return self._player_statistics.get(category_id, [])
        # Overall and every category are accumulated in one pass, not one pass
        # over all history for every custom-title category in Hall of Fame.
        buckets, scores = defaultdict(dict), defaultdict(list)
        for gid, game in self.games.items():
            cid = self.game_categories[gid]
            outcome = self.outcomes[gid]
            for pid in self.rosters[gid]:
                for bucket in (None, cid):
                    row = buckets[bucket].setdefault(pid, self.empty_player(pid))
                    row['played'] += 1
                    row['known_results' if outcome['known'] else 'unknown_results'] += 1
                    winner = outcome['winners'].get(pid)
                    if winner:
                        row['shared_wins' if winner['is_tie'] else 'solo_wins'] += 1
                        if number(winner['winning_score']) and self.categories[cid]['score_metric'] != 'outcome_only':
                            scores[(bucket, pid, cid)].append(winner['winning_score'])
                    elif outcome['known']:
                        row['losses'] += 1
        for players in buckets.values():
            for row in players.values():
                row['wins'] = row['solo_wins'] + row['shared_wins']
                row['ties'] = row['shared_wins']
                row['win_percentage_denominator'] = row['known_results']
                row['win_percentage'] = round(100 * row['wins'] / row['known_results'], 1) if row['known_results'] else None
        for (bucket, pid, cid), values in scores.items():
            low = self.categories[cid]['scoring_direction'] == 'low'
            buckets[bucket][pid]['winning_scores'].append({'category_id': cid, 'scored_wins': len(values),
                'average': round(sum(values) / len(values), 1), 'best': min(values) if low else max(values)})
        self._player_statistics = {key: sorted(players.values(), key=lambda row: (
            -row['wins'], -(row['win_percentage'] or 0), row['name'], row['person_id'])) for key, players in buckets.items()}
        return self._player_statistics.get(category_id, [])

    def empty_player(self, pid):
        return {'person_id': pid, 'name': self.name(pid), 'played': 0, 'known_results': 0, 'unknown_results': 0,
                'solo_wins': 0, 'shared_wins': 0, 'wins': 0, 'losses': 0, 'ties': 0,
                'win_percentage': None, 'win_percentage_denominator': 0, 'winning_scores': []}

    def streaks(self):
        current, best = {}, {}
        for game in sorted(self.games.values(), key=chronology):
            gid = game['id']
            outcome = self.outcomes[gid]
            for pid in self.rosters[gid]:
                winner = outcome['winners'].get(pid)
                if not winner:
                    current.pop(pid, None)
                    continue
                row = current.setdefault(pid, {'person_id': pid, 'name': self.name(pid), 'length': 0, 'shared_wins': 0,
                    'start_at': iso(game['finished_at'] or game['created_at']), 'start_game_id': gid})
                row['length'] += 1
                row['shared_wins'] += int(winner['is_tie'])
                row.update(end_at=iso(game['finished_at'] or game['created_at']), end_game_id=gid)
                if row['length'] >= 2 and (pid not in best or row['length'] >= best[pid]['length']):
                    best[pid] = dict(row)
        return sorted(best.values(), key=lambda row: (-row['length'], row['name'], row['person_id']))

    def hall(self, limit):
        spreads, best, worst, trouble = {}, {}, {}, {}
        for gid, game in self.games.items():
            outcome, cid = self.outcomes[gid], self.game_categories[gid]
            cat = self.categories[cid]
            if not outcome['known'] or cat['score_metric'] == 'outcome_only':
                continue
            if len(self.rosters[gid]) < 2:
                continue
            totals = outcome['totals']
            # Winning strength and round totals are different units even within one slug.
            if cat['score_metric'] == 'round_total' and len(totals) >= 2:
                low, high = min(totals.values()), max(totals.values())
                spread = high - low
                if spread > 0 and (cid not in spreads or spread > spreads[cid]['value']):
                    spreads[cid] = {'category': cat, 'game': self.brief(game), 'value': spread, 'low_total': low, 'high_total': high}
            values = ({pid: win['winning_score'] for pid, win in outcome['winners'].items() if number(win['winning_score'])}
                      if cat['score_metric'] == 'strength_of_win' else totals)
            for pid, value in values.items():
                record = {'category': cat, 'game': self.brief(game), 'person_id': pid, 'name': self.name(pid), 'value': value}
                if game['slug'] == 'trouble':
                    if cid not in trouble or value > trouble[cid]['value']:
                        trouble[cid] = record
                    continue
                low_wins = cat['scoring_direction'] == 'low'
                if cid not in best or (value < best[cid]['value'] if low_wins else value > best[cid]['value']):
                    best[cid] = record
                if cid not in worst or (value > worst[cid]['value'] if low_wins else value < worst[cid]['value']):
                    worst[cid] = record
        def records(values):
            return sorted(values.values(), key=lambda row: self.category_sort(row['category']))[:limit]
        leaders = []
        for cid, cat in sorted(self.categories.items(), key=lambda row: self.category_sort(row[1])):
            stats = self.player_statistics(cid)
            if stats and stats[0]['wins']:
                leaders.append({'category': cat, 'leaders': [row for row in stats if row['wins'] == stats[0]['wins']]})
        hands = []
        query = select(Score.game_id, Score.person_id, Score.round_number, Score.value).join(
            Game, Game.id == Score.game_id).where(Game.id.in_(self.ids_query), Game.slug == 'five-crowns',
                Game.mode == 'rounds', Score.value.is_not(None)).order_by(Score.value.desc(), Game.finished_at.desc(),
                    Score.game_id.desc(), Score.person_id, Score.round_number).execution_options(yield_per=100)
        result = self.db.execute(query)
        try:
            for gid, pid, hand, value in result:
                if self.outcomes[gid]['known'] and pid in self.people:
                    hands.append({'category': self.categories[self.game_categories[gid]], 'game': self.brief(self.games[gid]),
                                  'person_id': pid, 'name': self.name(pid), 'value': value, 'round_number': hand})
                    if len(hands) == limit:
                        break
        finally:
            result.close()
        latest = max(self.games.values(), key=chronology) if self.games else None
        return self.context() | {'latest_game': self.brief(latest) if latest else None,
            'biggest_beatdowns': records(spreads), 'worst_five_crowns_hands': hands,
            'win_leaders': leaders[:limit], 'best_scores': records(best), 'worst_scores': records(worst),
            'trouble_strength': records(trouble)}


@router.get('/statistics')
def statistics(request: Request, options=Depends(parameters)):
    with authenticated(request) as (db, user, _):
        data = Dataset(db, user, options)
        history, meta = data.history()
        return data.context() | {'summary': data.summary(), 'categories': data.category_counts(), 'history': history, 'pagination': meta}


@router.get('/leaderboards')
def leaderboards(request: Request, options=Depends(parameters)):
    with authenticated(request) as (db, user, _):
        data = Dataset(db, user, options)
        players, meta = pagination(data.player_statistics(), options)
        return data.context() | {'summary': data.summary(), 'categories': data.category_counts(), 'players': players, 'pagination': meta}


@router.get('/streaks')
def streaks(request: Request, options=Depends(parameters)):
    with authenticated(request) as (db, user, _):
        data = Dataset(db, user, options)
        rows, meta = pagination(data.streaks(), options)
        return data.context() | {'streaks': rows, 'pagination': meta}


@router.get('/hall-of-fame')
def hall_of_fame(request: Request, options=Depends(parameters), limit: int = Query(5, ge=1, le=20)):
    with authenticated(request) as (db, user, _):
        return Dataset(db, user, options).hall(limit)


@router.get('/people/{pid}/statistics')
def person_statistics(request: Request, pid: int = Path(ge=1, le=2147483647), options=Depends(parameters)):
    with authenticated(request) as (db, user, _):
        person_id = pid
        if person_id <= 0:
            raise HTTPException(404, 'Person not found.')
        data = Dataset(db, user, options | {'person_id': person_id})
        if person_id not in data.people:
            profile = db.execute(select(Person.id, Person.name, Person.privacy, Person.purged).where(Person.id == person_id)).first()
            groups = [g['id'] for g in user_groups(db, user)]
            shared = db.scalar(select(Membership.id).where(Membership.person_id == person_id,
                Membership.status == 'active', Membership.group_id.in_(groups)).limit(1))
            if profile is None or not (is_superadmin(user) or user.person_id == person_id or shared is not None):
                raise HTTPException(404, 'Person not found.')
            data.people[person_id] = dict(id=profile.id, name=profile.name, privacy=profile.privacy or {}, purged=profile.purged)
        row = next((row for row in data.player_statistics() if row['person_id'] == person_id), data.empty_player(person_id))
        history, meta = data.history()
        return data.context() | {'person': {'id': person_id, 'name': data.name(person_id)}, 'summary': row,
            'categories': data.category_counts(), 'history': history, 'pagination': meta}


@router.get('/groups/{gid}/statistics')
def group_statistics(request: Request, gid: int = Path(ge=1, le=2147483647), options=Depends(parameters)):
    with authenticated(request) as (db, user, _):
        group_id = gid
        if group_id <= 0:
            raise HTTPException(404, 'Group not found.')
        data = Dataset(db, user, options | {'group_id': group_id})
        group = db.execute(select(Group.id, Group.name).where(Group.id == group_id)).first()
        if group is None or not (is_superadmin(user) or is_member(db, user, group_id) or bool(data.games)):
            raise HTTPException(404, 'Group not found.')
        history, meta = data.history()
        players, player_meta = pagination(data.player_statistics(), options)
        return data.context() | {'group': {'id': group.id, 'name': group.name}, 'summary': data.summary(),
            'categories': data.category_counts(), 'players': players, 'history': history, 'pagination': meta,
            'player_pagination': player_meta}
