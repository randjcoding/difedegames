from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, func, select

from .db import utcnow
from .dependencies import authenticated
from .management import (account_payload, assert_lead_can_leave, assert_no_unfinished_games, audit,
    can_manage_person, group_payload, group_row, manage_group, management_lock, member_payload,
    memberships, person_payload, remove_admin_links, require_super, revoke_sessions)
from .management_schemas import (AddMembership, GroupPatch, LeadTransfer, MembershipDecision,
                                 PersonPatch, Purge, Reason)
from .models import ActionToken, Group, GroupAdmin, Invite, Membership, Person, User
from .security import is_admin, is_member, is_superadmin
from .privacy import public_flag

router = APIRouter(prefix='/api')


@router.get('/groups/{gid}')
def detail(gid: int, request: Request):
    with authenticated(request) as (db, user, _):
        group = group_row(db, gid, archived=is_superadmin(user))
        if not is_member(db, user, gid) and not is_superadmin(user):
            raise HTTPException(403, 'This group is not available to you.')
        lead = is_admin(db, user, gid)
        rows = list(db.scalars(select(Membership).where(Membership.group_id == gid)))
        people = [db.get(Person, m.person_id) for m in rows if m.status == 'active']
        visible_people = []
        for person in people:
            if not person or person.archived or person.purged:
                continue
            private = is_superadmin(user) or user.person_id == person.id or can_manage_person(db, user, person)
            shown = person_payload(db, person, private)
            if not is_superadmin(user) and user.person_id != person.id:
                shown['group_ids'] = [gid]
            visible_people.append(shown)
        return {'group': group_payload(db, group, user),
            'people': visible_people,
            'pending_people': [dict(id=p.id, name=p.name) for p in (db.get(Person, m.person_id) for m in rows
                if lead and m.status in ('requested', 'invited')) if p and not p.archived and not p.purged],
            'memberships': [member_payload(m) for m in rows if lead or m.status == 'active'],
            'admins': [{'id': u.id, 'name': db.get(Person, u.person_id).name if u.person_id else 'Group lead'} for u in db.scalars(select(User).join(GroupAdmin,
                GroupAdmin.user_id == User.id).where(GroupAdmin.group_id == gid, User.active.is_(True)))],
            'invites': invite_rows(db, gid) if lead else []}


@router.patch('/groups/{gid}')
def patch_group(gid: int, data: GroupPatch, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        group = manage_group(db, user, gid)
        patch = data.model_dump(exclude_unset=True, exclude_none=True)
        if 'name' in patch:
            group.name = patch.pop('name')
        group.settings = {**group.settings, **patch}
        audit(db, user, 'group_updated', {'group_id': gid, 'fields': sorted(data.model_fields_set)})
        return group_payload(db, group, user)


@router.post('/groups/{gid}/archive')
def archive_group(gid: int, data: Reason, request: Request):
    with authenticated(request, True) as (db, user, _):
        require_super(user)
        management_lock(db)
        group = db.scalar(select(Group).where(Group.id == gid).with_for_update().execution_options(populate_existing=True))
        if not group or group.archived:
            raise HTTPException(404, 'Active group not found.')
        assert_no_unfinished_games(db, group_id=gid)
        group.archived = True
        audit(db, user, 'group_archived', {'group_id': gid, 'reason': data.reason})
        return group_payload(db, group, user)


@router.post('/groups/{gid}/reinstate')
def reinstate_group(gid: int, data: Reason, request: Request):
    with authenticated(request, True) as (db, user, _):
        require_super(user)
        management_lock(db)
        group = group_row(db, gid, True)
        # Restoring a group without a usable lead requires an explicit handover first.
        active_lead = db.scalar(select(User.id).join(GroupAdmin, GroupAdmin.user_id == User.id).join(
            Membership, (Membership.group_id == GroupAdmin.group_id) & (Membership.person_id == User.person_id)).where(
                GroupAdmin.group_id == gid, User.active.is_(True), User.verified.is_(True), User.approved.is_(True), Membership.status == 'active'))
        if active_lead is None:
            raise HTTPException(409, 'Assign an active member as lead before restoring this group.')
        group.archived = False
        audit(db, user, 'group_reinstated', {'group_id': gid, 'reason': data.reason})
        return group_payload(db, group, user)


@router.post('/groups/{gid}/transfer-lead')
def transfer_lead(gid: int, data: LeadTransfer, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        group = manage_group(db, user, gid, archived=is_superadmin(user))
        target = db.get(User, data.user_id)
        if not target or not target.active or not target.approved or not target.verified or not target.person_id:
            raise HTTPException(422, 'Choose an active approved account with a person identity.')
        member = db.scalar(select(Membership).where(Membership.person_id == target.person_id,
            Membership.group_id == gid, Membership.status == 'active'))
        if not member:
            raise HTTPException(422, 'The new lead must be an active member of this group.')
        previous = list(db.scalars(select(GroupAdmin.user_id).where(GroupAdmin.group_id == gid)))
        for uid in previous:
            remove_admin_links(db, uid, gid)
        db.flush()
        db.add(GroupAdmin(group_id=gid, user_id=target.id))
        member.role = 'admin'
        audit(db, user, 'group_lead_transferred', {'group_id': gid, 'previous_leads': previous, 'new_lead': target.id})
        db.flush()
        return group_payload(db, group, user)


@router.patch('/people/{pid}')
def patch_person(pid: int, data: PersonPatch, request: Request):
    with authenticated(request, True) as (db, actor, _):
        management_lock(db)
        person = db.get(Person, pid)
        if not person or person.purged:
            raise HTTPException(404, 'Person not found.')
        if not can_manage_person(db, actor, person):
            raise HTTPException(403, 'Shared or account-owned profiles require the person or a site administrator. Manage only your group membership.')
        patch = data.model_dump(exclude_unset=True, mode='json')
        if patch.get('is_minor') is False and person.privacy.get('is_minor') and not is_superadmin(actor):
            raise HTTPException(403, 'Only a site administrator can remove minor protection.')
        if patch.get('name'):
            person.name = patch['name']
        person.details = {**person.details, **{k: patch[k] for k in ('first_name', 'last_name', 'email') if k in patch}}
        person.privacy = {**person.privacy, **{k: patch[k] for k in ('is_minor', 'is_discoverable', 'show_full_last_name') if patch.get(k) is not None}}
        if 'email' in patch:
            person.privacy = {**person.privacy, 'email_verified': False}
        audit(db, actor, 'person_updated', {'person_id': pid, 'fields': sorted(patch)})
        return person_payload(db, person, True)


@router.post('/people/{pid}/archive')
def archive_person(pid: int, data: Reason, request: Request):
    with authenticated(request, True) as (db, actor, _):
        management_lock(db)
        person = db.scalar(select(Person).where(Person.id == pid).with_for_update())
        if not person or person.purged:
            raise HTTPException(404, 'Person not found.')
        if not can_manage_person(db, actor, person):
            raise HTTPException(403, 'Remove this person’s membership in your group instead of archiving a shared identity.')
        account = db.scalar(select(User).where(User.person_id == pid))
        if account:
            if account.is_owner:
                raise HTTPException(403, 'The owner profile cannot be archived.')
            assert_lead_can_leave(db, account.id)
        assert_no_unfinished_games(db, person_id=pid)
        person.archived = True
        if account:
            remove_admin_links(db, account.id)
        audit(db, actor, 'person_archived', {'person_id': pid, 'reason': data.reason})
        return person_payload(db, person, True)


@router.post('/people/{pid}/reinstate')
def reinstate_person(pid: int, data: Reason, request: Request):
    with authenticated(request, True) as (db, actor, _):
        require_super(actor)
        management_lock(db)
        person = db.get(Person, pid)
        if not person or person.purged:
            raise HTTPException(404, 'Person not found or irreversibly anonymized.')
        person.archived = False
        audit(db, actor, 'person_reinstated', {'person_id': pid, 'reason': data.reason})
        return person_payload(db, person, True)


@router.post('/people/{pid}/purge')
def purge_person(pid: int, data: Purge, request: Request):
    with authenticated(request, True) as (db, actor, _):
        require_super(actor)
        management_lock(db)
        person = db.scalar(select(Person).where(Person.id == pid).with_for_update())
        if not person or person.purged:
            raise HTTPException(404, 'Person not found.')
        account = db.scalar(select(User).where(User.person_id == pid))
        if account and (account.is_owner or account.id == actor.id):
            raise HTTPException(403, 'Your own or the owner’s identity cannot be anonymized here.')
        if account:
            assert_lead_can_leave(db, account.id)
        assert_no_unfinished_games(db, person_id=pid)
        person.name, person.details, person.privacy = 'Deleted Player', {}, {'is_discoverable': False}
        person.archived = person.purged = True
        if account:
            account.active = False
            remove_admin_links(db, account.id)
            revoke_sessions(db, account.id)
        audit(db, actor, 'active_profile_anonymized', {'person_id': pid, 'reason': data.reason,
            'provenance_retained': True})
        return person_payload(db, person, True)


@router.post('/groups/{gid}/memberships')
def add_membership(gid: int, data: AddMembership, request: Request):
    with authenticated(request, True) as (db, actor, _):
        management_lock(db)
        manage_group(db, actor, gid)
        person = db.get(Person, data.person_id)
        if not person or person.archived or person.purged:
            raise HTTPException(404, 'Active person not found.')
        if not is_superadmin(actor) and not any(is_member(db, actor, m.group_id) for m in memberships(db, person.id)):
            raise HTTPException(403, 'Invite a person who is not already in one of your groups.')
        row = db.scalar(select(Membership).where(Membership.group_id == gid, Membership.person_id == person.id))
        linked = db.scalar(select(User.id).where(User.person_id == person.id))
        if not is_superadmin(actor) and (not row or row.status != 'active'):
            if linked and (not row or row.status != 'requested'):
                raise HTTPException(409, 'Invite this account to join, or approve its own join request first.')
            if not linked and not can_manage_person(db, actor, person):
                raise HTTPException(403, 'Only a manageable guest may be assigned directly to another group.')
        if not row:
            row = Membership(group_id=gid, person_id=person.id)
            db.add(row)
        else:
            row.status = 'active'
        audit(db, actor, 'membership_added', {'group_id': gid, 'person_id': person.id})
        db.flush()
        return member_payload(row)


@router.post('/groups/{gid}/request-join')
def request_join(gid: int, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        group_row(db, gid)
        group = db.get(Group, gid)
        if not public_flag(group.settings, 'is_discoverable', True) and not is_member(db, user, gid) and not is_superadmin(user):
            raise HTTPException(404, 'Group not available. Ask its lead for an invitation.')
        if not user.person_id:
            raise HTTPException(422, 'Resolve your player identity before requesting membership.')
        row = db.scalar(select(Membership).where(Membership.group_id == gid, Membership.person_id == user.person_id))
        if row and row.status == 'active':
            return member_payload(row)
        if row:
            row.status = 'requested'
        else:
            row = Membership(group_id=gid, person_id=user.person_id, status='requested')
            db.add(row)
        db.flush()
        audit(db, user, 'membership_requested', {'group_id': gid, 'person_id': user.person_id})
        return member_payload(row)


@router.post('/memberships/{mid}/decide')
def decide_membership(mid: int, data: MembershipDecision, request: Request):
    with authenticated(request, True) as (db, actor, _):
        management_lock(db)
        row = db.get(Membership, mid)
        if not row:
            raise HTTPException(404, 'Membership not found.')
        self_leaving = row.person_id == actor.person_id and data.decision == 'remove'
        if not self_leaving:
            manage_group(db, actor, row.group_id)
        if data.decision == 'approve' and row.status not in ('requested', 'active'):
            raise HTTPException(409, 'A new join request or self-accepted invitation is required.')
        account = db.scalar(select(User).where(User.person_id == row.person_id))
        if data.decision != 'approve':
            if account:
                assert_lead_can_leave(db, account.id, row.group_id)
                remove_admin_links(db, account.id, row.group_id)
            row.status, row.role, row.is_home = 'removed', 'member', False
        else:
            person = db.get(Person, row.person_id)
            if person.archived or person.purged:
                raise HTTPException(422, 'Restore the person before approving membership.')
            row.status = 'active'
        audit(db, actor, 'membership_' + data.decision, {'membership_id': mid, 'group_id': row.group_id, 'person_id': row.person_id})
        return member_payload(row)


@router.post('/memberships/{mid}/home')
def set_home(mid: int, request: Request):
    with authenticated(request, True) as (db, actor, _):
        management_lock(db)
        row = db.get(Membership, mid)
        if not row or row.status != 'active':
            raise HTTPException(404, 'Active membership not found.')
        group_row(db, row.group_id)
        if actor.person_id != row.person_id and not is_superadmin(actor):
            raise HTTPException(403, 'Only this person or a site administrator may choose their home group.')
        for other in db.scalars(select(Membership).where(Membership.person_id == row.person_id)):
            other.is_home = other.id == row.id
        audit(db, actor, 'home_group_changed', {'person_id': row.person_id, 'group_id': row.group_id})
        return member_payload(row)


def invite_rows(db, gid):
    from .games import iso
    return [{'id': i.token_hash, 'person_id': i.person_id, 'expires_at': iso(i.expires_at), 'used_at': iso(i.used_at)}
            for i in db.scalars(select(Invite).where(Invite.group_id == gid).order_by(Invite.expires_at.desc()))]


@router.get('/groups/{gid}/invites')
def list_invites(gid: int, request: Request):
    with authenticated(request) as (db, user, _):
        manage_group(db, user, gid)
        return {'invites': invite_rows(db, gid)}


@router.post('/invites/{identifier}/revoke')
def revoke_invite(identifier: str, request: Request):
    with authenticated(request, True) as (db, user, _):
        management_lock(db)
        row = db.get(Invite, identifier)
        if not row:
            raise HTTPException(404, 'Invitation not found.')
        manage_group(db, user, row.group_id)
        row.expires_at = utcnow()
        audit(db, user, 'invite_revoked', {'group_id': row.group_id})
        return {'ok': True}
