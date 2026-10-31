from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class Person(Base):
    __tablename__ = 'people'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(Integer)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    purged: Mapped[bool] = mapped_column(Boolean, default=False)
    privacy: Mapped[dict] = mapped_column(JSON, default=dict)
    legacy: Mapped[dict] = mapped_column(JSON, default=dict)
    details: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')


class User(Base):
    __tablename__ = 'accounts'
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    person_id: Mapped[int | None] = mapped_column(ForeignKey('people.id'), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=True)
    legacy: Mapped[dict] = mapped_column(JSON, default=dict)
    site_role: Mapped[str] = mapped_column(String(16), default='member', server_default='member')
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    preferences: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')
    details: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')


class Group(Base):
    __tablename__ = 'groups'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    legacy: Mapped[dict] = mapped_column(JSON, default=dict)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')


class Membership(Base):
    __tablename__ = 'memberships'
    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey('people.id'))
    group_id: Mapped[int] = mapped_column(ForeignKey('groups.id'))
    role: Mapped[str] = mapped_column(String(12), default='member')
    status: Mapped[str] = mapped_column(String(16), default='active')
    is_home: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint('person_id', 'group_id'),)


class GroupAdmin(Base):
    __tablename__ = 'group_admins'
    group_id: Mapped[int] = mapped_column(ForeignKey('groups.id'), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), primary_key=True)


class Game(Base):
    __tablename__ = 'game_sessions'
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16), default='active', index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    mode: Mapped[str] = mapped_column(String(16), default='rounds')
    scoring_direction: Mapped[str] = mapped_column(String(8))
    target_score: Mapped[int | None] = mapped_column(Integer)
    group_id: Mapped[int | None] = mapped_column(ForeignKey('groups.id'))
    organizer_id: Mapped[int | None] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    rules_version: Mapped[str] = mapped_column(String(40), default='v3-pilot-1')
    result: Mapped[dict | None] = mapped_column(JSON)
    legacy: Mapped[dict] = mapped_column(JSON, default=dict)


class Seat(Base):
    __tablename__ = 'game_seats'
    game_id: Mapped[int] = mapped_column(ForeignKey('game_sessions.id'), primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey('people.id'), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    color: Mapped[str | None] = mapped_column(String(20))
    legacy: Mapped[dict] = mapped_column(JSON, default=dict)


class ViewerGroup(Base):
    __tablename__ = 'game_viewer_groups'
    game_id: Mapped[int] = mapped_column(ForeignKey('game_sessions.id'), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey('groups.id'), primary_key=True)


class Scorekeeper(Base):
    __tablename__ = 'game_scorekeepers'
    game_id: Mapped[int] = mapped_column(ForeignKey('game_sessions.id'), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), primary_key=True)


class Round(Base):
    __tablename__ = 'game_rounds'
    game_id: Mapped[int] = mapped_column(ForeignKey('game_sessions.id'), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(12))
    side: Mapped[str | None] = mapped_column(String(8))


class Score(Base):
    __tablename__ = 'score_cells'
    game_id: Mapped[int] = mapped_column(ForeignKey('game_sessions.id'), primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey('people.id'), primary_key=True)
    round_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    extra: Mapped[dict] = mapped_column('metadata', JSON, default=dict)
    legacy_id: Mapped[int | None] = mapped_column(Integer)


class Operation(Base):
    __tablename__ = 'operations'
    game_id: Mapped[int] = mapped_column(ForeignKey('game_sessions.id'), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Audit(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int | None] = mapped_column(ForeignKey('game_sessions.id'))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey('accounts.id'))
    action: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GameEvent(Base):
    __tablename__ = 'game_events'
    game_id: Mapped[int] = mapped_column(ForeignKey('game_sessions.id'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebSession(Base):
    __tablename__ = 'web_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), index=True)
    csrf_token: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginThrottle(Base):
    __tablename__ = 'login_throttles'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invite(Base):
    __tablename__ = 'invites'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey('groups.id'))
    person_id: Mapped[int | None] = mapped_column(ForeignKey('people.id'))
    creator_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportRun(Base):
    __tablename__ = 'legacy_imports'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_name: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    report: Mapped[dict] = mapped_column(JSON)


class LegacyRecord(Base):
    __tablename__ = 'legacy_records'
    source_table: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class ActionToken(Base):
    __tablename__ = 'account_action_tokens'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(30))
    user_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GameLayout(Base):
    __tablename__ = 'saved_game_layouts'
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey('groups.id'))
    slug: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(80))
    player_ids: Mapped[list] = mapped_column(JSON)
    mode: Mapped[str] = mapped_column(String(16), default='rounds')
    scoring_direction: Mapped[str | None] = mapped_column(String(8))
    target_score: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(160))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_by: Mapped[int | None] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    legacy: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint('group_id', 'slug', 'name'),
        Index('one_default_saved_layout', 'group_id', 'slug', unique=True,
            postgresql_where=text('is_default = true'), sqlite_where=text('is_default = 1')))
