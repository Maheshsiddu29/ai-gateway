"""The supported subset of the OpenAI Chat Completions request.

Only fields listed here are accepted. Unknown fields are rejected instead of being
dropped, so a client never receives a response generated without an option it
asked for. Parsing is strict: JSON types must match exactly.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationInfo, field_validator

MAX_MESSAGES = 512
MAX_STOP_SEQUENCES = 4
MAX_OUTPUT_TOKENS = 1_000_000

# Chat Completions parameters that exist in the OpenAI API but are not implemented
# by the gateway. They are reported as unsupported capabilities rather than as
# unknown parameters.
KNOWN_UNSUPPORTED_PARAMETERS = frozenset(
    {
        "audio",
        "frequency_penalty",
        "function_call",
        "functions",
        "logit_bias",
        "logprobs",
        "max_completion_tokens",
        "metadata",
        "modalities",
        "n",
        "parallel_tool_calls",
        "prediction",
        "presence_penalty",
        "reasoning_effort",
        "response_format",
        "seed",
        "service_tier",
        "store",
        "tool_choice",
        "tools",
        "top_logprobs",
        "user",
        "web_search_options",
        # Message-level fields.
        "name",
        "refusal",
        "tool_call_id",
        "tool_calls",
    }
)


def _wrap_single_stop(value: object) -> object:
    return [value] if isinstance(value, str) else value


StopSequences = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=256)]],
    BeforeValidator(_wrap_single_stop),
    Field(min_length=1, max_length=MAX_STOP_SEQUENCES),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ChatMessage(_StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str


class StreamOptions(_StrictModel):
    include_usage: bool = False


class ChatCompletionRequest(_StrictModel):
    model: str = Field(min_length=1, max_length=64)
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    # A single string is accepted, as in the OpenAI API, and normalized to a list.
    stop: StopSequences | None = None

    @field_validator("stream_options")
    @classmethod
    def _require_streaming(
        cls, value: StreamOptions | None, info: ValidationInfo
    ) -> StreamOptions | None:
        if value is not None and not info.data.get("stream"):
            raise ValueError("stream_options is only allowed when stream is true")
        return value

    @property
    def include_usage(self) -> bool:
        return self.stream_options is not None and self.stream_options.include_usage
