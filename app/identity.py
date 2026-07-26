"""Shared identity helpers: naming, slugs, and audit logging.

A person (players row) is the permanent identity; a user (users row) is just
a login. users.player_id is the only identity link. players.created_by_user_id
is provenance only and must never be used to infer who someone is.
"""

import json
import re

from flask import request


def slugify_family(name):
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', name.strip()).strip('-').lower()
    return slug or 'family'


def unique_family_slug(conn, name):
    """Deterministic unique slug: bare slug if free, otherwise slug-N."""
    cursor = conn.cursor()
    base = slugify_family(name)
    cursor.execute('SELECT COUNT(*) AS n FROM families WHERE slug = %s OR slug ~ %s',
                   (base, f'^{re.escape(base)}-[0-9]+$'))
    row = cursor.fetchone()
    n = row['n'] if isinstance(row, dict) else row[0]
    cursor.close()
    return base if n == 0 else f'{base}-{n + 1}'


def public_person_name(player):
    """First name plus last initial unless the person opted into full name.

    Use for EVERY public-facing render of a person outside their own family.
    Never expose email through any public surface.
    """
    first = player.get('display_name') or player.get('first_name') or 'Player'
    last = (player.get('last_name') or '').strip()
    if not last:
        return first
    if player.get('show_full_last_name'):
        return f'{first} {last}'
    return f'{first} {last[0]}.'


def audit(conn, user_id, action, table_name=None, record_id=None, old=None, new=None):
    """Write an audit row inside the caller's transaction (no commit here)."""
    cursor = conn.cursor()
    try:
        ip = request.remote_addr if request else None
    except RuntimeError:
        ip = None
    cursor.execute('''
        INSERT INTO user_audit_log (user_id, action, table_name, record_id, old_values, new_values, ip_address)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    ''', (user_id, action, table_name, record_id,
          json.dumps(old, default=str) if old is not None else None,
          json.dumps(new, default=str) if new is not None else None,
          ip))
    cursor.close()


def allied_family_ids(conn, family_id):
    """Directly allied (accepted) families. Alliances never chain."""
    if not family_id:
        return []
    cursor = conn.cursor()
    cursor.execute('''
        SELECT CASE WHEN requesting_family_id = %s THEN target_family_id
                    ELSE requesting_family_id END AS ally_id
        FROM family_alliances
        WHERE status = 'accepted' AND (requesting_family_id = %s OR target_family_id = %s)
    ''', (family_id, family_id, family_id))
    rows = cursor.fetchall()
    cursor.close()
    return [r['ally_id'] if isinstance(r, dict) else r[0] for r in rows]
