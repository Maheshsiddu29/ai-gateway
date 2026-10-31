import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gateway.evaluation.dataset import EvaluationCase
from gateway.evaluation.scoring import Score, score_output
from gateway.evaluation.stats import percentile, wilson_interval


def label_case() -> EvaluationCase:
    return EvaluationCase.model_validate_json(
        json.dumps(
            {
                "id": "t1",
                "dataset": "tickets",
                "dataset_version": "1",
                "task": "classification",
                "method": "exact_label",
                "messages": [{"role": "user", "content": "charged twice"}],
                "expected": {"label": "billing", "labels": ["account", "billing", "shipping"]},
            }
        )
    )


def fields_case(min_f1: float = 1.0) -> EvaluationCase:
    return EvaluationCase.model_validate_json(
        json.dumps(
            {
                "id": "i1",
                "dataset": "invoices",
                "dataset_version": "1",
                "task": "extraction",
                "method": "field_f1",
                "messages": [{"role": "user", "content": "Invoice A-1 ..."}],
                "expected": {
                    "fields": {"invoice_number": "A-1", "total": "10.00", "currency": "EUR"},
                    "min_f1": min_f1,
                },
            }
        )
    )


@pytest.mark.parametrize("output", ["billing", " Billing.\n", "BILLING"])
def test_label_normalization_accepts_trivial_variations(output: str) -> None:
    assert score_output(label_case(), output) == Score(1.0, passed=True, invalid_response=False)


def test_wrong_label_is_a_valid_but_failed_response() -> None:
    expected = Score(0.0, passed=False, invalid_response=False)

    assert score_output(label_case(), "shipping") == expected


@pytest.mark.parametrize("output", ["", "The category is billing.", "billing, shipping", "refund"])
def test_anything_but_one_label_is_invalid(output: str) -> None:
    assert score_output(label_case(), output) == Score(0.0, passed=False, invalid_response=True)


def test_all_fields_correct_pass() -> None:
    output = json.dumps({"invoice_number": "a-1", "total": " 10.00 ", "currency": "eur"})

    assert score_output(fields_case(), output) == Score(1.0, passed=True, invalid_response=False)


def test_fenced_json_is_accepted() -> None:
    output = '```json\n{"invoice_number": "A-1", "total": "10.00", "currency": "EUR"}\n```'

    assert score_output(fields_case(), output).passed


def test_partial_extraction_scores_f1() -> None:
    # Two of three expected fields right, one wrong prediction, one extra key:
    # precision 2/4, recall 2/3, F1 = 4/7.
    output = json.dumps(
        {"invoice_number": "A-1", "total": "10.00", "currency": "USD", "due_date": "2026-01-01"}
    )

    score = score_output(fields_case(), output)

    assert score.value == pytest.approx(4 / 7)
    assert not score.passed
    assert score_output(fields_case(min_f1=0.5), output).passed


def test_null_values_are_not_predictions() -> None:
    output = json.dumps(
        {"invoice_number": "A-1", "total": "10.00", "currency": "EUR", "due_date": None}
    )

    assert score_output(fields_case(), output).value == 1.0


def test_non_string_values_cannot_match() -> None:
    output = json.dumps({"invoice_number": "A-1", "total": 10.0, "currency": "EUR"})

    score = score_output(fields_case(), output)

    assert score.value == pytest.approx(2 * (2 / 3) * (2 / 3) / (4 / 3))
    assert not score.passed


@pytest.mark.parametrize("output", ["not json", "[1, 2]", '"A-1"', "", "```\n{}"])
def test_non_object_output_is_invalid(output: str) -> None:
    assert score_output(fields_case(), output) == Score(0.0, passed=False, invalid_response=True)


def test_no_correct_fields_scores_zero() -> None:
    assert score_output(fields_case(), "{}") == Score(0.0, passed=False, invalid_response=False)


@given(st.text(max_size=200))
def test_scoring_is_deterministic_and_bounded(output: str) -> None:
    for case in (label_case(), fields_case()):
        first = score_output(case, output)
        assert first == score_output(case, output)
        assert 0.0 <= first.value <= 1.0
        assert not (first.passed and first.invalid_response)


def test_wilson_interval_reflects_sample_size() -> None:
    small_low, _ = wilson_interval(10, 10)
    large_low, _ = wilson_interval(1000, 1000)

    assert small_low == pytest.approx(0.7225, abs=1e-4)
    assert large_low == pytest.approx(0.99617, abs=1e-5)
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(45, 50)
    assert low < 0.9 < high


def test_wilson_interval_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="between"):
        wilson_interval(3, 2)


def test_nearest_rank_percentile() -> None:
    samples = [float(value) for value in range(1, 101)]

    assert percentile(samples, 0.5) == 50.0
    assert percentile(samples, 0.95) == 95.0
    assert percentile(samples, 0.99) == 99.0
    assert percentile([7.0], 0.95) == 7.0
    with pytest.raises(ValueError, match="no samples"):
        percentile([], 0.5)
