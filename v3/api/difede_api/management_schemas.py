from typing import Annotated, Literal
from pydantic import EmailStr, Field, StringConstraints

from .schemas import Payload, Name, PositiveID, RevisionCommand, ScoreValue

Style = Literal['classic', 'arcade', 'scoreboard', 'clubhouse', 'playground']
Palette = Literal['difede-dark', 'classic-light', 'midnight-blue', 'espresso', 'emerald-forest',
                  'rose-wine', 'sunset-blaze', 'arctic-frost', 'royal-purple']
ReasonText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]


class PreferencesPatch(Payload):
    style: Style | None = None
    palette: Palette | None = None
    favorite_game_slugs: list[str] | None = Field(default=None, max_length=20)
    favorite_person_ids: list[PositiveID] | None = Field(default=None, max_length=200)


class Contact(Payload):
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=50)
    zipcode: str | None = Field(default=None, max_length=20)
    phone_number: str | None = Field(default=None, max_length=30)


class ProfilePatch(Payload):
    name: Name | None = None
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    person_email: EmailStr | None = None
    is_discoverable: bool | None = None
    show_full_last_name: bool | None = None
    contact: Contact | None = None


class PasswordChange(Payload):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=10, max_length=128)


class ResetRequest(Payload):
    email: EmailStr


class ResetComplete(Payload):
    password: str = Field(min_length=10, max_length=128)


class AccountPatch(Payload):
    name: Name | None = None
    email: EmailStr | None = None
    active: bool | None = None
    verified: bool | None = None
    approved: bool | None = None
    site_role: Literal['member', 'super_admin'] | None = None


class Reason(Payload):
    reason: ReasonText = 'Administrative action'


class GroupPatch(Payload):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    is_discoverable: bool | None = None
    show_roster: bool | None = None


class LeadTransfer(Payload):
    user_id: PositiveID


class PersonPatch(Payload):
    name: Name | None = None
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    is_minor: bool | None = None
    is_discoverable: bool | None = None
    show_full_last_name: bool | None = None


class Purge(Reason):
    confirm: Literal['PURGE']


class AddMembership(Payload):
    person_id: PositiveID


class MembershipDecision(Payload):
    decision: Literal['approve', 'deny', 'remove']


class CorrectionCell(Payload):
    player_id: PositiveID
    round_number: int = Field(strict=True, gt=0, le=1000)
    score: ScoreValue | None


class Correction(RevisionCommand):
    reason: ReasonText
    scores: list[CorrectionCell] | None = Field(default=None, max_length=10000)
    winner_id: PositiveID | None = None
    details: dict | None = None
