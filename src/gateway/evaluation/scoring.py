"""Deterministic scoring of model outputs against case expectations.

A scorer is a pure function of the case and the output text. Changing how any
scorer behaves must change its entry in ``EVALUATOR_VERSIONS``, because routing
only uses evidence produced by the current evaluator version.

Normalization is deliberately small and documented in docs/evaluation.md:
whitespace is trimmed, letter case is ignored, and for labels one trailing period
is dropped. A label response with any other text is invalid, not partially right.
"""

import json
import re
from dataclasses import dataclass
from fractions import Fraction

from gateway.evaluation.dataset import (
    EvaluationCase,
    EvaluationMethod,
    FieldsExpectation,
    LabelExpectation,
)

EVALUATOR_VERSIONS: dict[EvaluationMethod, str] = {
    EvaluationMethod.EXACT_LABEL: "exact_label-1",
    EvaluationMethod.FIELD_F1: "field_f1-1",
}

_WHITESPACE = re.compile(r"\s+")
# A whole response wrapped in one Markdown code fence, optionally tagged json.
_FENCED = re.compile(r"\A```(?:json)?\s*\n(?P<body>.*)\n```\Z", re.DOTALL)


@dataclass(frozen=True)
class Score:
    # 1 or 0 for labels; the F1 score for fields.
    value: float
    passed: bool
    # The output did not have the required form (an unknown label, not a JSON object).
    invalid_response: bool
    # Field scoring only: matching (field, value) pairs, non-null predicted fields,
    # and expected fields. They are reported for micro-averaged precision and recall
    # and do not change the score.
    fields_correct: int | None = None
    fields_predicted: int | None = None
    fields_expected: int | None = None


def score_output(case: EvaluationCase, output: str) -> Score:
    if isinstance(case.expected, LabelExpectation):
        return _score_label(case.expected, output)
    if isinstance(case.expected, FieldsExpectation):
        return _score_fields(case.expected, output)
    raise TypeError(f"no scorer for method {case.method}")


def unanswered(case: EvaluationCase) -> Score:
    """The score of a case that got no response (a provider error or timeout)."""
    if isinstance(case.expected, FieldsExpectation):
        return Score(0.0, False, False, 0, 0, len(case.expected.fields))
    return Score(0.0, passed=False, invalid_response=False)


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip()).casefold()


def _score_label(expected: LabelExpectation, output: str) -> Score:
    answer = _normalize(output).removesuffix(".")
    allowed = {_normalize(label) for label in expected.labels}
    if answer not in allowed:
        return Score(0.0, passed=False, invalid_response=True)
    correct = answer == _normalize(expected.label)
    return Score(1.0 if correct else 0.0, passed=correct, invalid_response=False)


def _score_fields(expected: FieldsExpectation, output: str) -> Score:
    invalid = Score(0.0, False, True, 0, 0, len(expected.fields))
    text = output.strip()
    if fenced := _FENCED.match(text):
        text = fenced["body"]
    try:
        parsed = json.loads(text)
    except ValueError:
        return invalid
    if not isinstance(parsed, dict):
        return invalid
    # A null value means the model found no value; it is not a prediction. Non-string
    # values are predictions that cannot match, since every expected value is text.
    predicted = {_normalize(key): value for key, value in parsed.items() if value is not None}
    wanted = {_normalize(key): _normalize(value) for key, value in expected.fields.items()}
    correct = sum(
        1
        for key, value in wanted.items()
        if isinstance(predicted.get(key), str) and _normalize(predicted[key]) == value
    )
    counts = (correct, len(predicted), len(wanted))
    if correct == 0:
        return Score(0.0, False, False, *counts)
    precision = Fraction(correct, len(predicted))
    recall = Fraction(correct, len(wanted))
    f1 = 2 * precision * recall / (precision + recall)
    return Score(float(f1), f1 >= Fraction(expected.min_f1), False, *counts)
