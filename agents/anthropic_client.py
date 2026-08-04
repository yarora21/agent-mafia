"""PR14a — the real `ModelClient`: a language model behind the seam at last.

Everything up to here ran against stubs; this is where a live Anthropic model
plugs in. It implements the `ModelClient` protocol (PR11a) over `langchain-anthropic`,
and its whole job is to honour that protocol's two failure classes faithfully:

- **Malformed / empty → `None` (never an exception).** Structured calls use
  `with_structured_output(..., include_raw=True)`, so a reply the model failed to
  shape into the schema comes back as `parsed=None` instead of raising. The agent
  turns that `None` into a referee retry — exactly as it did against the stub.
- **Outage → raised.** A transport error, auth failure, or timeout from the API is
  *not* swallowed: it propagates so the orchestrator's backoff path decides what to
  do. This client never retries the network itself.

Token usage is read from each response's `usage_metadata` and returned alongside
the value, so the shared `CostLedger` can be charged at real `MODEL_PRICING`.

The concrete model is injectable (`llm=`) so tests drive a fake chat model with no
network; the default builds a `ChatAnthropic` from the model name and API key.
"""

from __future__ import annotations

from typing import Any, cast

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from agents.llm_agent import TokenUsage
from config import DEFAULT_MODEL

_DEFAULT_MAX_TOKENS = 1024


class AnthropicModelClient:
    """A live Anthropic model as a `ModelClient` (PR11a).

    `complete_structured` validates into a PR9 output schema (malformed → `None`);
    `complete_text` returns free-form reasoning. Both report `TokenUsage`. Outages
    raise; nothing here retries the network.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        llm: BaseChatModel | None = None,
    ) -> None:
        self._model = model
        if llm is not None:
            self._llm: BaseChatModel = llm
        else:
            # api_key passed through; ChatAnthropic also reads ANTHROPIC_API_KEY.
            self._llm = ChatAnthropic(
                model_name=model,
                api_key=api_key,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens_to_sample=max_tokens,
                timeout=None,
                stop=None,
            )

    @staticmethod
    def _messages(system: str, user: str) -> list[BaseMessage]:
        return [SystemMessage(content=system), HumanMessage(content=user)]

    @staticmethod
    def _usage(message: AIMessage) -> TokenUsage:
        meta = message.usage_metadata
        if not meta:
            return TokenUsage()
        return TokenUsage(
            input_tokens=meta.get("input_tokens", 0),
            output_tokens=meta.get("output_tokens", 0),
        )

    @staticmethod
    def _text_of(message: AIMessage) -> str:
        """Flatten an AIMessage's content to a plain string.

        Anthropic content can be a string or a list of blocks; join the text of
        any block-shaped content so reasoning is always a single string.
        """

        content = message.content
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)

    async def complete_structured(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> tuple[Any, TokenUsage]:
        structured = self._llm.with_structured_output(schema, include_raw=True)
        # A transport/auth error raises here by design (outage). A *parse* failure
        # does not, because include_raw=True routes it to parsed=None.
        result = cast(dict[str, Any], await structured.ainvoke(self._messages(system, user)))
        raw = result.get("raw")
        usage = self._usage(raw) if isinstance(raw, AIMessage) else TokenUsage()
        parsed = result.get("parsed")  # None when the model's reply didn't validate
        return parsed, usage

    async def complete_text(
        self, *, system: str, user: str
    ) -> tuple[str | None, TokenUsage]:
        message = await self._llm.ainvoke(self._messages(system, user))
        assert isinstance(message, AIMessage)
        text = self._text_of(message)
        usage = self._usage(message)
        # Empty output is "nothing usable" — surface it as None (the retry path).
        return (text if text.strip() else None), usage
