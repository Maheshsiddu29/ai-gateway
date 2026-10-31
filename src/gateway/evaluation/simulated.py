"""A deterministic stand-in provider for exercising the evaluation pipeline.

It answers each case with the expected output, except for a fixed fraction of
cases chosen by hashing the model name and case ID, where it gives a well-formed
wrong answer. Its token counts come from the gateway's input estimator and the
output length. Its results measure nothing about any real model, so runs that use
it are marked simulated and are never accepted as routing evidence.
"""

import hashlib
import json
import math
from collections.abc import AsyncGenerator, Mapping, Sequence

from gateway.admission.tokens import ESTIMATED_BYTES_PER_TOKEN, estimate_input_tokens
from gateway.errors import InvalidRequestError
from gateway.evaluation.dataset import Dataset, EvaluationCase, FieldsExpectation, LabelExpectation
from gateway.providers.base import (
    Completion,
    ProviderRequest,
    StreamEnd,
    StreamEvent,
    TextDelta,
    Usage,
)
from gateway.schemas.completions import ChatMessage


def _key(messages: Sequence[ChatMessage]) -> tuple[tuple[str, str], ...]:
    return tuple((message.role, message.content) for message in messages)


def _fraction(model: str, case_id: str) -> float:
    digest = hashlib.sha256(f"{model}\0{case_id}".encode()).digest()
    return int.from_bytes(digest[:8]) / 2**64


def simulated_answer(case: EvaluationCase, *, correct: bool) -> str:
    expected = case.expected
    if isinstance(expected, LabelExpectation):
        if correct:
            return expected.label
        labels = expected.labels
        return labels[(labels.index(expected.label) + 1) % len(labels)]
    if isinstance(expected, FieldsExpectation):
        fields = dict(sorted(expected.fields.items()))
        if not correct:
            # Dropping one field lowers recall below 1, so the case fails.
            fields.popitem()
        return json.dumps(fields)
    raise TypeError(f"no simulated answer for method {case.method}")


class SimulatedProvider:
    def __init__(self, name: str, dataset: Dataset, accuracy: Mapping[str, float]) -> None:
        if any(not 0 <= value <= 1 for value in accuracy.values()):
            raise ValueError("simulated accuracy must be between 0 and 1")
        self._name = name
        self._cases = {_key(case.messages): case for case in dataset.cases}
        self._accuracy = dict(accuracy)

    @property
    def name(self) -> str:
        return self._name

    def validate(self, request: ProviderRequest) -> None:
        if request.model not in self._accuracy:
            raise InvalidRequestError("No simulated accuracy is configured for this model.")

    async def complete(self, request: ProviderRequest) -> Completion:
        self.validate(request)
        case = self._cases.get(_key(request.messages))
        if case is None:
            raise InvalidRequestError("The simulated provider only answers dataset cases.")
        correct = _fraction(request.model, case.id) < self._accuracy[request.model]
        content = simulated_answer(case, correct=correct)
        prompt = estimate_input_tokens(request.messages)
        completion = max(1, math.ceil(len(content.encode()) / ESTIMATED_BYTES_PER_TOKEN))
        return Completion(
            content=content,
            finish_reason="stop",
            usage=Usage(prompt, completion, prompt + completion),
        )

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent]:
        completion = await self.complete(request)
        yield TextDelta(completion.content)
        yield StreamEnd(completion.finish_reason, completion.usage)

    async def aclose(self) -> None:
        return
