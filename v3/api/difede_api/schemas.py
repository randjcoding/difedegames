from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
OperationID = Annotated[str, StringConstraints(min_length=8, max_length=120, pattern=r'^[A-Za-z0-9_.:-]+$')]
PositiveID = Annotated[int, Field(strict=True, gt=0)]
ScoreValue = Annotated[int, Field(strict=True, ge=-1000000, le=1000000)]


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Payload):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class PersonCreate(Payload):
    name: Name
    group_id: PositiveID


class GroupCreate(Payload):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class InviteCreate(Payload):
    person_id: PositiveID | None = None


class InviteAccept(Payload):
    name: Name | None = None
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=10, max_length=128)


class GameCreate(Payload):
    slug: str = Field(max_length=60)
    title: Name | None = None
    mode: Literal['rounds', 'single', 'winner'] | None = None
    player_ids: list[PositiveID] = Field(min_length=1, max_length=10)
    group_id: PositiveID
    viewer_group_ids: list[PositiveID] = Field(default_factory=list, max_length=30)
    scorekeeper_ids: list[PositiveID] = Field(default_factory=list, max_length=10)
    scoring_direction: Literal['low', 'high'] | None = None
    target_score: PositiveID | None = None


class OperationPayload(Payload):
    operation_id: OperationID


class ScoreCommand(OperationPayload):
    player_id: PositiveID
    round_number: Annotated[int, Field(strict=True, gt=0, le=1000)]
    score: ScoreValue | None
    expected_version: Annotated[int, Field(strict=True, ge=0)]


class RevisionCommand(OperationPayload):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class RoundCommand(RevisionCommand):
    side: Literal['light', 'dark'] | None = None


class RoundPatch(RevisionCommand):
    side: Literal['light', 'dark']


class FinishCommand(RevisionCommand):
    early: bool = False
    winner_id: PositiveID | None = None
    details: dict | None = None


class Peg(Payload):
    player_id: PositiveID
    color: Literal['red', 'blue', 'green', 'yellow']
    finish: Annotated[int, Field(strict=True, ge=0, le=4)]
    board: Annotated[int, Field(strict=True, ge=0, le=4)]
    home: Annotated[int, Field(strict=True, ge=0, le=4)]


class PegDetails(Payload):
    pegs: list[Peg] = Field(min_length=2, max_length=4)


class Penalty(Payload):
    player_id: PositiveID
    score: Annotated[int, Field(strict=True, ge=0, le=1000000)]


class PenaltyDetails(Payload):
    penalties: list[Penalty] = Field(min_length=1, max_length=5)
