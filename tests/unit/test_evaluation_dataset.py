import json
from pathlib import Path
from typing import Any

import pytest

from gateway.evaluation.dataset import (
    MAX_LINE_BYTES,
    DatasetError,
    EvaluationMethod,
    TaskType,
    load_dataset,
)

REPOSITORY_DATASETS = sorted((Path(__file__).parents[2] / "evaluations" / "datasets").glob("*"))


def label_case(case_id: str = "c1", **overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": case_id,
        "dataset": "tickets",
        "dataset_version": "1",
        "task": "classification",
        "method": "exact_label",
        "messages": [{"role": "user", "content": "My card was charged twice."}],
        "expected": {"label": "billing", "labels": ["billing", "shipping"]},
    }
    case.update(overrides)
    return case


def write_cases(tmp_path: Path, *cases: dict[str, Any]) -> Path:
    path = tmp_path / "dataset.jsonl"
    path.write_text("".join(json.dumps(case) + "\n" for case in cases))
    return path


@pytest.mark.parametrize("path", REPOSITORY_DATASETS, ids=lambda path: path.name)
def test_repository_datasets_are_valid(path: Path) -> None:
    dataset = load_dataset(path)

    assert len(dataset.cases) >= 40
    assert path.name == f"{dataset.name}-v{dataset.version}.jsonl"


def test_parses_a_dataset_and_records_its_digest(tmp_path: Path) -> None:
    path = write_cases(tmp_path, label_case("a"), label_case("b"))

    dataset = load_dataset(path)

    assert (dataset.name, dataset.version) == ("tickets", "1")
    assert dataset.task is TaskType.CLASSIFICATION
    assert dataset.method is EvaluationMethod.EXACT_LABEL
    assert [case.id for case in dataset.cases] == ["a", "b"]
    assert len(dataset.sha256) == 64


def test_digest_changes_with_any_byte(tmp_path: Path) -> None:
    first = load_dataset(write_cases(tmp_path, label_case())).sha256
    edited = label_case(messages=[{"role": "user", "content": "My card was charged twice!"}])
    second = load_dataset(write_cases(tmp_path, edited)).sha256

    assert first != second


@pytest.mark.parametrize(
    ("case", "message"),
    [
        (label_case(expected=None), "expected"),
        (label_case(expected={"labels": ["billing", "shipping"]}), "expected"),
        (label_case(expected={"label": "refunds", "labels": ["billing", "x"]}), "one of labels"),
        (label_case(expected={"label": "a", "labels": ["a", "A"]}), "distinct"),
        (label_case(method="field_f1"), "evaluated with"),
        (label_case(task="extraction", method="field_f1"), "needs a FieldsExpectation"),
        (label_case(messages=[]), "messages"),
        (label_case(extra="field"), "extra"),
        (label_case(id="has space"), "id"),
        (label_case(dataset_version=1), "dataset_version"),
    ],
)
def test_rejects_invalid_cases_with_line_number(
    tmp_path: Path, case: dict[str, Any], message: str
) -> None:
    path = write_cases(tmp_path, label_case("first"), case)

    with pytest.raises(DatasetError, match=rf"dataset.jsonl:2: .*{message}"):
        load_dataset(path)


def test_rejects_duplicate_ids_and_mixed_versions(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="duplicate case id a"):
        load_dataset(write_cases(tmp_path, label_case("a"), label_case("a")))
    with pytest.raises(DatasetError, match="does not match"):
        load_dataset(write_cases(tmp_path, label_case("a"), label_case("b", dataset_version="2")))


def test_rejects_empty_and_oversized_input(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n")
    with pytest.raises(DatasetError, match="no cases"):
        load_dataset(empty)

    huge = label_case(messages=[{"role": "user", "content": "x" * MAX_LINE_BYTES}])
    with pytest.raises(DatasetError, match="exceeds"):
        load_dataset(write_cases(tmp_path, huge))


def test_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "a",\n')

    with pytest.raises(DatasetError, match=r"bad\.jsonl:1"):
        load_dataset(path)
