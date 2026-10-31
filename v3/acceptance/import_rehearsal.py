"""Independent reconciliation of the retained snapshot into exact test DB only."""
from collections import defaultdict
import json
import os
from pathlib import Path
import subprocess
import tempfile

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from common import reset_schema


def main():
    source_url = os.environ.get('LEGACY_SOURCE_URL', '')
    parsed = make_url(source_url)
    if parsed.database != 'difedegames_v3_legacy_snapshot_20261031' or parsed.username != 'v3_legacy_reader':
        raise RuntimeError('Only the retained isolated snapshot with read-only reader is permitted')
    target = reset_schema()
    with tempfile.TemporaryDirectory() as directory:
        report_path = Path(directory) / 'report.json'
        command = ['python', '-m', 'difede_api.import_legacy', '--source-name', parsed.database,
                   '--confirm-isolated', '--report', str(report_path)]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError('Importer failed; inspect importer privately without printing rows')
        importer = json.loads(report_path.read_text())
        source = create_engine(source_url)
        checks = {}
        with source.connect() as legacy, target.connect() as normalized:
            legacy.execute(text('BEGIN TRANSACTION READ ONLY'))
            names = inspect(legacy).get_table_names(schema='public')
            counts = {name: legacy.scalar(text('SELECT count(*) FROM public.' + legacy.dialect.identifier_preparer.quote(name))) for name in names}
            preserved = dict(normalized.execute(text('SELECT source_table,count(*) FROM legacy_records GROUP BY source_table')).all())
            checks['each_source_table_row_count_preserved'] = all(preserved.get(name, 0) == count for name, count in counts.items())
            checks['all_source_rows_preserved'] = normalized.scalar(text('SELECT count(*) FROM legacy_records')) == sum(counts.values())
            for old, new in [('players', 'people'), ('users', 'accounts'), ('active_games', 'game_sessions')]:
                checks[f'{old}_identity_count_preserved'] = counts[old] == normalized.scalar(text(f'SELECT count(*) FROM {new}'))
            legacy_seats = defaultdict(list)
            for gid, pid in legacy.execute(text('SELECT active_game_id,player_id FROM active_game_players ORDER BY id')):
                if pid not in legacy_seats[gid]:
                    legacy_seats[gid].append(pid)
            target_seats = defaultdict(list)
            for gid, pid in normalized.execute(text('SELECT game_id,person_id FROM game_seats ORDER BY game_id,position')):
                target_seats[gid].append(pid)
            checks['legacy_board_seat_order_preserved'] = all(target_seats[gid][:len(pids)] == pids for gid, pids in legacy_seats.items())
            hashes = dict(legacy.execute(text('SELECT id,password_hash FROM users')).all())
            checks['password_hashes_preserved_without_display'] = hashes == dict(normalized.execute(text('SELECT id,password_hash FROM accounts')).all())
            source_states = dict(legacy.execute(text('SELECT id,is_complete FROM active_games')).all())
            checks['completion_states_preserved'] = all((status == 'completed') == bool(source_states[gid]) for gid, status in normalized.execute(text('SELECT id,status FROM game_sessions')))
            checks['all_importer_reconciliations_pass'] = all(importer['checks'].values())
            legacy.rollback()
        # A second import must refuse the nonempty target without writing more rows.
        repeated = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        checks['nonempty_target_reimport_refused'] = repeated.returncode != 0
        with target.connect() as normalized:
            checks['single_import_marker'] = normalized.scalar(text('SELECT count(*) FROM legacy_imports')) == 1
        report = {'database': 'difedegames_v3_test', 'source': parsed.database,
                  'source_tables': len(counts), 'source_rows': sum(counts.values()),
                  'normalized_counts': importer['normalized'], 'checks': checks,
                  'passed': sum(checks.values()), 'total': len(checks)}
        print(json.dumps(report, indent=2))
        if os.environ.get('ACCEPTANCE_REPORT'):
            Path(os.environ['ACCEPTANCE_REPORT']).write_text(json.dumps(report, indent=2), encoding='utf-8')
        source.dispose()
        target.dispose()
        raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == '__main__':
    main()
