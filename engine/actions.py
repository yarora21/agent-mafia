"""The action vocabulary: the six things an agent can be asked to do.

These are pure data shapes. An `Action` is always a well-formed value; whether
it is *legal* in the current game state is a separate concern checked by the
referee (PR5), not encoded here. Likewise, which role may take which action is a
rule the state machine and referee own — not something these types constrain.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class ActionType(str, Enum):
    """What an agent is being asked to produce."""

    SPEECH = "speech"
    VOTE = "vote"
    MAFIA_CHAT = "mafia_chat"
    NIGHT_KILL = "night_kill"
    INVESTIGATE = "investigate"
    NARRATE = "narrate"


class _ActionBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_id: str


class Speech(_ActionBase):
    type: Literal[ActionType.SPEECH] = ActionType.SPEECH
    message: str


class Vote(_ActionBase):
    type: Literal[ActionType.VOTE] = ActionType.VOTE
    target_id: str


class MafiaChat(_ActionBase):
    type: Literal[ActionType.MAFIA_CHAT] = ActionType.MAFIA_CHAT
    message: str


class NightKill(_ActionBase):
    type: Literal[ActionType.NIGHT_KILL] = ActionType.NIGHT_KILL
    target_id: str


class Investigate(_ActionBase):
    type: Literal[ActionType.INVESTIGATE] = ActionType.INVESTIGATE
    target_id: str


class Narration(_ActionBase):
    type: Literal[ActionType.NARRATE] = ActionType.NARRATE
    message: str


Action = Annotated[
    Union[Speech, Vote, MafiaChat, NightKill, Investigate, Narration],
    Field(discriminator="type"),
]
"""Discriminated union over all action shapes, keyed by `type`."""


class ActionRequest(BaseModel):
    """What the engine needs from an agent right now.

    `legal_targets` is populated for target-based actions (VOTE, NIGHT_KILL,
    INVESTIGATE) and left empty for free-text actions (SPEECH, MAFIA_CHAT,
    NARRATE). It is a convenience for agents; the referee is the authority on
    legality.
    """

    model_config = ConfigDict(frozen=True)

    action_type: ActionType
    actor_id: str
    legal_targets: list[str] = Field(default_factory=list)
