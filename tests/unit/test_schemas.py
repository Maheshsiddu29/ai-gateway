import json

import pytest
from pydantic import ValidationError

from gateway.schemas.completions import MAX_MESSAGES, ChatCompletionRequest


def parse(**fields: object) -> ChatCompletionRequest:
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], **fields}
    return ChatCompletionRequest.model_validate_json(json.dumps(body))


def test_minimal_request_defaults() -> None:
    request = parse()

    assert request.stream is False
    assert request.max_tokens is None
    assert request.stop is None
    assert request.include_usage is False


def test_accepts_supported_generation_parameters() -> None:
    request = parse(max_tokens=10, temperature=1, top_p=0.5, stop=["END"])

    assert request.max_tokens == 10
    assert request.temperature == 1.0
    assert request.stop == ["END"]


def test_single_stop_string_is_normalized_to_a_list() -> None:
    assert parse(stop="END").stop == ["END"]


@pytest.mark.parametrize(
    "fields",
    [
        {"temperature": 2.5},
        {"temperature": -0.1},
        {"top_p": 1.5},
        {"max_tokens": 0},
        {"max_tokens": "10"},
        {"stream": "true"},
        {"temperature": "0.5"},
        {"stop": []},
        {"stop": ["a", "b", "c", "d", "e"]},
        {"stop": [""]},
        {"stream_options": {"include_usage": True}},
        {"stream": True, "stream_options": {"include_usage": True, "extra": 1}},
        {"messages": []},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}]},
        {"messages": [{"role": "user", "content": None}]},
        {"messages": [{"role": "user"}]},
        {"messages": [{"role": "user", "content": "x"}] * (MAX_MESSAGES + 1)},
        {"model": ""},
        {"tools": []},
    ],
)
def test_rejects_invalid_or_unsupported_fields(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse(**fields)


def test_stream_options_with_streaming() -> None:
    request = parse(stream=True, stream_options={"include_usage": True})

    assert request.include_usage is True
