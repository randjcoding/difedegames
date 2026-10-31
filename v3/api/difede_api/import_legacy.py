"""Import an explicitly restored, read-only V2 snapshot into an empty V3 DB.

No V2 config imports, environment discovery, identity inference, or source writes.
The full source dump must also remain backed up independently of this database.
"""
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url

from .catalog import CATALOG
from .db import Base, make_engine, make_sessions, utcnow
from .games import active_standings
from .models import (Game, GameEvent, Group, GroupAdmin, ImportRun, LegacyRecord, Membership,
                     Person, Round, Score, Seat, User, ViewerGroup)


def json_value(value):
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def canonical(value):
    return json.dumps(json_value(value), sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def fingerprint(rows):
    encoded = sorted(canonical(row) for row in rows)
    return hashlib.sha256('\n'.join(encoded).encode()).hexdigest()


def source_time(value):
    if not value:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def validate_source(source_url, target_url, confirmed):
    if not confirmed:
        raise ValueError('--confirm-isolated is required for a restored source snapshot')
    source = make_url(source_url)
    target = make_url(target_url)
    if source.get_backend_name() != 'postgresql':
        raise ValueError('CLI source must be a restored PostgreSQL snapshot')
    if not re.search(r'(snapshot|restore)', source.database or '', re.I):
        raise ValueError('Source database name must explicitly identify a snapshot or restore')
    if (source.host, source.port, source.database) == (target.host, target.port, target.database):
        raise ValueError('Source and target databases must differ')
    if not re.search(r'v3', target.database or '', re.I):
        raise ValueError('Target database must explicitly identify V3')


def read_snapshot(engine):
    source = {}
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as conn:
        with conn.begin():
            conn.execute(text('SET TRANSACTION READ ONLY'))
            inspector = inspect(conn)
            for name in sorted(inspector.get_table_names(schema='public')):
                table = Table(name, MetaData(), autoload_with=conn, schema='public')
                source[name] = [dict(r) for r in conn.execute(select(table)).mappings()]
    return source


def import_rows(db, source, source_name):
    # Refuse every kind of occupied target, including prior provenance-only imports.
    for table in Base.metadata.sorted_tables:
        if db.scalar(select(func.count()).select_from(table)):
            raise ValueError('V3 target is not empty; import refused without changing it')
    required = {'users', 'players', 'families', 'games', 'active_games', 'active_game_players', 'game_scores', 'game_stats'}
    if not required <= set(source):
        raise ValueError('Snapshot is missing required V2 tables')
    report = {'source_name': source_name, 'source_tables': {}, 'normalized': {}, 'checks': {}, 'warnings': {}}
    for table, rows in source.items():
        report['source_tables'][table] = {'rows': len(rows), 'sha256': fingerprint(rows)}
        for index, row in enumerate(rows):
            key = str(row.get('id', index))
            db.add(LegacyRecord(source_table=table, source_key=key, payload=json_value(row)))
    people = {r['id']: r for r in source['players']}
    accounts = {r['id']: r for r in source['users']}
    families = {r['id']: r for r in source['families']}
    definitions = {r['id']: r for r in source['games']}
    for row in people.values():
        display_name = row.get('display_name') or row.get('first_name') or 'Player'
        db.add(Person(id=row['id'], name=display_name,
                      created_by=row.get('created_by_user_id', row.get('user_id')),
                      archived=bool(row.get('archived_at')), purged=bool(row.get('purged_at')),
                      privacy={key: row.get(key) for key in ('is_discoverable', 'show_full_last_name', 'is_minor', 'email_verified')},
                      legacy=json_value(row)))
    db.flush()
    for row in accounts.values():
        db.add(User(id=row['id'], email=row['email'].strip().lower(),
                    name=' '.join(s for s in [row.get('first_name'), row.get('last_name')] if s) or 'Player',
                    password_hash=row['password_hash'], person_id=row.get('player_id'),
                    active=bool(row.get('is_active')) and not bool(row.get('archived_at')),
                    verified=bool(row.get('is_verified')), approved=bool(row.get('is_approved')),
                    legacy=json_value(row)))
    db.flush()
    for row in families.values():
        db.add(Group(id=row['id'], name=row['name'], archived=bool(row.get('archived_at')), legacy=json_value(row)))
    db.flush()
    for row in source.get('player_family_memberships', []):
        db.add(Membership(id=row['id'], person_id=row['player_id'], group_id=row['family_id'],
                          role='admin' if row.get('role') == 'lead' else 'member',
                          status=row.get('status') or 'active', is_home=bool(row.get('is_primary'))))
    db.flush()
    # Preserve actual account/person links; never match by names or creator fields.
    for row in families.values():
        lead = row.get('lead_user_id')
        if lead in accounts:
            db.add(GroupAdmin(group_id=row['id'], user_id=lead))
    db.flush()
    stats = defaultdict(list)
    for row in source['game_stats']:
        stats[row['game_id']].append(row)
    source_seats = defaultdict(list)
    for row in source['active_game_players']:
        source_seats[row['active_game_id']].append(row)
    source_scores = defaultdict(list)
    # Unified table is authoritative; legacy rows are fallback only, never summed twice.
    cells = {(r['active_game_id'], r['player_id'], r['round_number']): r for r in source['game_scores']}
    fallback = 0
    for row in source.get('five_crowns_scores', []):
        key = (row['active_game_id'], row['player_id'], row['round_number'])
        if key not in cells:
            cells[key] = row
            fallback += 1
    for key, row in cells.items():
        source_scores[key[0]].append(row)
    missing_seats = 0
    missing_winners = 0
    for row in sorted(source['active_games'], key=lambda r: r['id']):
        definition = definitions[row['game_id']]
        slug = definition.get('slug') or f'legacy-{row["game_id"]}'
        raw_direction = row.get('scoring_direction') or definition.get('scoring_direction') or 'low_wins'
        mode = 'winner' if raw_direction == 'manual' else 'single' if raw_direction == 'single_round' or slug == 'trouble' else 'rounds'
        game = Game(id=row['id'], slug=slug, title=row.get('custom_game_name') or definition['name'],
                    status='completed' if row.get('is_complete') else 'active', revision=1, mode=mode,
                    scoring_direction='high' if raw_direction == 'high_wins' or mode == 'single' else 'low',
                    target_score=row.get('target_score'), group_id=row.get('family_id'), organizer_id=row.get('user_id'),
                    created_at=source_time(row.get('start_time')) or utcnow(),
                    finished_at=source_time(row.get('completion_time')), imported=True, rules_version='v2-original',
                    legacy=json_value(row) | {'definition': json_value(definition), 'original_results': json_value(stats[row['id']])})
        db.add(game)
        db.flush()
        seen = set()
        for index, seat in enumerate(sorted(source_seats[game.id], key=lambda s: s['id'])):
            if seat['player_id'] in seen:
                continue
            seen.add(seat['player_id'])
            db.add(Seat(game_id=game.id, person_id=seat['player_id'], position=index, legacy=json_value(seat)))
        # Preserve orphaned historical score identities explicitly and report repairs.
        for score in source_scores[game.id]:
            if score['player_id'] not in seen:
                db.add(Seat(game_id=game.id, person_id=score['player_id'], position=len(seen),
                            legacy={'import_note': 'Score existed without a source seat'}))
                seen.add(score['player_id'])
                missing_seats += 1
        for result in stats[game.id]:
            pid = result.get('winner_id')
            if pid and pid in people and pid not in seen:
                db.add(Seat(game_id=game.id, person_id=pid, position=len(seen),
                            legacy={'import_note': 'Original winner existed without a source seat'}))
                seen.add(pid)
                missing_winners += 1
        if game.group_id is not None:
            db.add(ViewerGroup(game_id=game.id, group_id=game.group_id))
        numbers = {s['round_number'] for s in source_scores[game.id]}
        if mode == 'rounds':
            numbers |= set(CATALOG.get(slug, {}).get('fixed_rounds') or [])
        if not numbers and mode != 'winner':
            numbers = {1}
        for number in sorted(numbers):
            db.add(Round(game_id=game.id, number=number, label={11: 'J', 12: 'Q', 13: 'K'}.get(number, str(number))
                         if slug == 'five-crowns' else str(number), side='light' if slug == 'uno-flip' else None))
        db.flush()
        for cell in source_scores[game.id]:
            db.add(Score(game_id=game.id, person_id=cell['player_id'], round_number=cell['round_number'],
                         value=cell['score'], version=1, extra=cell.get('metadata') or {}, legacy_id=cell['id']))
        db.flush()
        if game.status == 'completed':
            standings = active_standings(db, game)
            winners = {s['winner_id'] for s in stats[game.id] if s.get('winner_id')}
            for standing in standings:
                standing['winner'] = standing['player_id'] in winners
                if mode == 'winner':
                    standing['rank'] = 1 if standing['winner'] else 2
            game.result = {'standings': standings, 'official': True, 'original_results': json_value(stats[game.id]),
                           'policy': 'v2-original'}
        db.add(GameEvent(game_id=game.id, revision=1, action='legacy_imported', payload={'source_name': source_name}))
    db.flush()
    report['normalized'] = {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                            for model in (Person, User, Group, Membership, Game, Seat, Score, LegacyRecord)}
    report['warnings'] = {'legacy_fallback_cells': fallback, 'scores_missing_source_seat': missing_seats,
                          'winners_missing_source_seat': missing_winners,
                          'accounts_without_person': sum(not r.get('player_id') for r in accounts.values()),
                          'completed_without_original_result': sum(bool(r.get('is_complete')) and not stats[r['id']] for r in source['active_games'])}
    report['checks'] = {
        'person_ids': set(db.scalars(select(Person.id))) == set(people),
        'account_ids': set(db.scalars(select(User.id))) == set(accounts),
        'game_ids': set(db.scalars(select(Game.id))) == {r['id'] for r in source['active_games']},
        'password_hashes': all(db.get(User, uid).password_hash == r['password_hash'] for uid, r in accounts.items()),
        'account_identity_links': all(db.get(User, uid).person_id == r.get('player_id') for uid, r in accounts.items()),
        'score_values_metadata': all((cell := db.get(Score, key)).value == row['score'] and cell.extra == (row.get('metadata') or {})
                                     for key, row in cells.items()),
        'original_results': all(db.get(Game, r['id']).legacy['original_results'] == json_value(stats[r['id']]) for r in source['active_games']),
        'all_raw_rows': report['normalized']['legacy_records'] == sum(len(rows) for rows in source.values()),
        'person_display_privacy_lifecycle': all((person := db.get(Person, pid)).name == (row.get('display_name') or row.get('first_name') or 'Player')
            and person.archived == bool(row.get('archived_at')) and person.purged == bool(row.get('purged_at'))
            and person.privacy == {key: row.get(key) for key in ('is_discoverable', 'show_full_last_name', 'is_minor', 'email_verified')}
            for pid, row in people.items()),
        'cell_count': report['normalized']['score_cells'] == len(cells),
        'score_total': db.scalar(select(func.sum(Score.value))) == sum(r['score'] for r in cells.values()) if cells else True,
    }
    for table, original in source.items():
        imported_rows = list(db.scalars(select(LegacyRecord.payload).where(LegacyRecord.source_table == table)))
        report['checks'][f'raw_{table}'] = fingerprint(imported_rows) == fingerprint(original)
    if not all(report['checks'].values()):
        raise ValueError('Import reconciliation failed; all target writes rolled back')
    db.add(ImportRun(source_name=source_name, report=report))
    db.flush()
    if db.bind.dialect.name == 'postgresql':
        for table in Base.metadata.sorted_tables:
            if 'id' in table.c and table.c.id.primary_key and isinstance(table.c.id.type.python_type(), int):
                # Bound relation names avoid interpolating external input.
                sequence = db.scalar(text('SELECT pg_get_serial_sequence(:table, :column)'), {'table': table.name, 'column': 'id'})
                if sequence:
                    maximum = db.scalar(select(func.max(table.c.id)))
                    db.execute(text('SELECT setval(CAST(:seq AS regclass), :value, :called)'),
                               {'seq': sequence, 'value': maximum or 1, 'called': maximum is not None})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-url', default=os.environ.get('LEGACY_SOURCE_URL'))
    parser.add_argument('--source-name', required=True)
    parser.add_argument('--confirm-isolated', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    target_url = os.environ.get('DATABASE_URL')
    if not args.source_url or not target_url:
        raise SystemExit('LEGACY_SOURCE_URL and DATABASE_URL must be supplied privately')
    validate_source(args.source_url, target_url, args.confirm_isolated)
    source_engine = create_engine(args.source_url, pool_pre_ping=True)
    source = read_snapshot(source_engine)
    source_engine.dispose()
    engine = make_engine(target_url)
    try:
        with make_sessions(engine).begin() as db:
            report = import_rows(db, source, args.source_name)
    except Exception:
        # Database exceptions may contain personal rows/password hashes; never dump them.
        raise SystemExit('Import failed and rolled back. Inspect schema/reconciliation privately; no source data was changed.') from None
    if args.report:
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
