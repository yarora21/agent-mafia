"""PR9a — the LLM-facing data shapes.

Two kinds of shape live here, both pure frozen models:

- `BeliefState` — an agent's *private* scratchpad. It is not part of `GameState`
  and no other player ever sees it; it rides on the agent instance (PR11) and
  persists across turns. Its `cursor` records the last event the agent has
  folded into its beliefs, so the agent knows what is new since it last thought
  (the hook the `beliefs_only` memory strategy builds on).
- `TargetOutput` / `TextOutput` — the shapes the model is *constrained* to emit.
  They carry only the payload the model chooses; identity (`actor_id`) is never
  model-supplied — the orchestrator stamps it from the request in PR9b. This is
  why neither output shape has an `actor_id` field.

The adapters that translate these to/from engine `Action`s, and pick the right
output schema per request, land in PR9b (`output_schema_for`,
`to_engine_action`, `parse_belief_state`).
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.actions import (
    Action,
    ActionRequest,
    ActionType,
    Investigate,
    MafiaChat,
    Narration,
    NightKill,
    Speech,
    Vote,
)
from engine.state import Role


class BeliefState(BaseModel):
    """An agent's private, cross-turn memory.

    Immutable like everything else in the codebase: a belief update produces a
    new `BeliefState` (via `model_copy(update=...)`), it does not mutate in
    place. `cursor` is the `event_id` of the newest event already reflected
    here; `None` means the agent has not yet processed any event.
    """

    model_config = ConfigDict(frozen=True)

    cursor: int | None = None
    suspicions: dict[str, float] = Field(default_factory=dict)
    confirmed_roles: dict[str, Role] = Field(default_factory=dict)
    notes: str = ""


class TargetOutput(BaseModel):
    """Model output for a target-based action (VOTE / NIGHT_KILL / INVESTIGATE).

    Only the chosen target; `actor_id` is stamped from the request in PR9b.
    """

    model_config = ConfigDict(frozen=True)

    target_id: str


class TextOutput(BaseModel):
    """Model output for a free-text action (SPEECH / MAFIA_CHAT / NARRATE).

    Only the produced line; `actor_id` is stamped from the request in PR9b.
    """

    model_config = ConfigDict(frozen=True)

    message: str


# --------------------------------------------------------------------------- #
# PR9b — adapters (never raise; return ``(value, reason)``)
# --------------------------------------------------------------------------- #

# Each action type maps to the concrete engine `Action` class the model's output
# becomes. The output *schema* (target vs text) follows from which map it is in.
_TARGET_ACTIONS: dict[ActionType, type[Vote | NightKill | Investigate]] = {
    ActionType.VOTE: Vote,
    ActionType.NIGHT_KILL: NightKill,
    ActionType.INVESTIGATE: Investigate,
}
_TEXT_ACTIONS: dict[ActionType, type[Speech | MafiaChat | Narration]] = {
    ActionType.SPEECH: Speech,
    ActionType.MAFIA_CHAT: MafiaChat,
    ActionType.NARRATE: Narration,
}


def output_schema_for(request: ActionRequest) -> type[TargetOutput | TextOutput]:
    """Return the output schema the model must satisfy for this request.

    Target-based actions (VOTE / NIGHT_KILL / INVESTIGATE) → `TargetOutput`;
    free-text actions (SPEECH / MAFIA_CHAT / NARRATE) → `TextOutput`. This is
    the response format handed to the structured-output call in PR11.
    """

    if request.action_type in _TARGET_ACTIONS:
        return TargetOutput
    return TextOutput


def to_engine_action(
    raw: Mapping[str, object], request: ActionRequest
) -> tuple[Action | None, str | None]:
    """Map a raw model output dict to a well-formed engine `Action`.

    Never raises. Returns ``(action, None)`` on success or ``(None, reason)``
    when the dict does not satisfy the schema for this request. The action's
    `actor_id` is stamped from the request — never taken from the model — and
    `request.action_type` selects the concrete `Action` class. Legality
    (a *real* target, a non-empty message) is the referee's concern, not this
    adapter's: it guarantees a well-formed action, not a legal one.
    """

    schema = output_schema_for(request)
    try:
        output = schema.model_validate(raw)
    except ValidationError as exc:
        return None, f"malformed {schema.__name__}: {exc.errors()[0]['msg']}"

    if isinstance(output, TargetOutput):
        target_cls = _TARGET_ACTIONS[request.action_type]
        return target_cls(actor_id=request.actor_id, target_id=output.target_id), None
    text_cls = _TEXT_ACTIONS[request.action_type]
    return text_cls(actor_id=request.actor_id, message=output.message), None


def parse_belief_state(
    raw: Mapping[str, object],
) -> tuple[BeliefState | None, str | None]:
    """Parse a raw model dict into a `BeliefState`.

    Never raises. Returns ``(belief_state, None)`` on success or
    ``(None, reason)`` when the dict is malformed, so a bad belief update feeds
    the retry path instead of crashing the game loop.
    """

    try:
        return BeliefState.model_validate(raw), None
    except ValidationError as exc:
        return None, f"malformed BeliefState: {exc.errors()[0]['msg']}"
