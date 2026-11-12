"""Opt-in Langfuse tracing. Never send agents the omniscient game state."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

if TYPE_CHECKING:
    from langfuse import Langfuse
    from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult

_log = logging.getLogger(__name__)
_client: Langfuse | None = None
_handler: ContextVar[Any] = ContextVar("langfuse_handler", default=None)
_SECRET = re.compile(r"\b(?:sk|pk)-(?:ant-|lf-|proj-)?[A-Za-z0-9_-]{8,}")


def mask_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult:
    """Redact API-key patterns and configured secrets before exporting attributes."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    secrets = [os.environ[k] for k in (
        "ANTHROPIC_API_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY"
    ) if os.environ.get(k)]

    def redact(value: str) -> str:
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return _SECRET.sub("[REDACTED]", value)

    patches = {}
    for identifier, span in params.spans.items():
        attributes: dict[str, Any] = {}
        for key, value in span.attributes.items():
            if isinstance(value, str):
                attributes[key] = redact(value)
            elif isinstance(value, (tuple, list)):
                attributes[key] = tuple(redact(v) if isinstance(v, str) else v for v in value)
        if attributes:
            patches[identifier] = OtelSpanPatch(set_attributes=attributes)
    return MaskOtelSpansResult(span_patches=patches)


def configure_tracing() -> None:
    """Called by entry points after loading the project .env; shell values win."""
    global _client
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    if _client is not None or os.getenv("LANGFUSE_TRACING_ENABLED", "false").lower() != "true":
        return
    required = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
    if not all(os.getenv(key) for key in required):
        _log.warning("Langfuse disabled: set public key, secret key, and base URL.")
        return
    from langfuse import Langfuse

    try:
        _client = Langfuse(mask_otel_spans=mask_spans)
    except Exception:
        _log.warning("Langfuse initialization failed; continuing without tracing.")


def enabled() -> bool:
    return _client is not None


def flush_tracing() -> None:
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            _log.warning("Langfuse flush failed; game results are unaffected.")


def callback_config() -> RunnableConfig:
    handler = _handler.get()
    return {"callbacks": [handler]} if handler is not None else {}


class Observation:
    """No-op when disabled; explicitly selected inputs avoid capturing credentials."""
    def __init__(self, span: Any = None) -> None:
        self._span = span

    def update(self, **values: Any) -> None:
        if self._span is not None:
            self._span.update(**values)


@contextmanager
def observation(name: str, *, as_type: Literal["span", "agent"] = "span", **values: Any) -> Iterator[Observation]:
    if _client is None:
        yield Observation()
        return
    with _client.start_as_current_observation(name=name, as_type=as_type, **values) as span:
        try:
            yield Observation(span)
        except BaseException as exc:
            span.update(level="ERROR", status_message=type(exc).__name__)
            raise


@contextmanager
def player_turn(*, session_id: str | None, metadata: dict[str, Any], input: Any) -> Iterator[Observation]:
    if _client is None:
        yield Observation()
        return
    from langfuse import propagate_attributes
    from langfuse.langchain import CallbackHandler

    # A distinct handler per concurrent turn; contextvars isolate asyncio tasks.
    token = _handler.set(CallbackHandler(public_key=os.environ.get("LANGFUSE_PUBLIC_KEY")))
    try:
        with propagate_attributes(session_id=session_id, metadata=metadata, tags=["mafia"]):
            with observation("play-player-turn", as_type="agent", input=input, metadata=metadata) as span:
                yield span
    finally:
        _handler.reset(token)


def record_game_summary(session_id: str | None, output: dict[str, Any]) -> None:
    if _client is None:
        return
    from langfuse import propagate_attributes

    with propagate_attributes(session_id=session_id, tags=["mafia"]):
        with observation("finish-game", input={"session_id": session_id}) as span:
            span.update(output=output, level="WARNING" if output.get("aborted") else "DEFAULT")
