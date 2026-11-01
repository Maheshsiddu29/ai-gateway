import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from gateway.evaluation.cli import main
from gateway.evaluation.dataset import (
    MAX_LINE_BYTES,
    DatasetError,
    EvaluationMethod,
    Partition,
    TaskType,
    check_partitions,
    load_dataset,
)

EVALUATIONS = Path(__file__).parents[2] / "evaluations"
REPOSITORY_DATASETS = sorted((EVALUATIONS / "datasets").glob("*"))
PARTITION_PAIRS = [
    (
        EVALUATIONS / "datasets" / f"{name}-v1.jsonl",
        EVALUATIONS / "datasets" / f"{name}-v1-holdout.jsonl",
    )
    for name in ("support-ticket-category", "invoice-fields")
]


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


def write_cases(tmp_path: Path, *cases: dict[str, Any], name: str = "dataset.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(case) + "\n" for case in cases))
    return path


@pytest.mark.parametrize("path", REPOSITORY_DATASETS, ids=lambda path: path.name)
def test_repository_datasets_are_valid(path: Path) -> None:
    dataset = load_dataset(path)

    assert len(dataset.cases) >= 40
    suffix = "-holdout" if dataset.partition is Partition.HOLDOUT else ""
    assert path.name == f"{dataset.name}-v{dataset.version}{suffix}.jsonl"


def test_released_datasets_match_the_manifest() -> None:
    manifest = tomllib.loads((EVALUATIONS / "manifest.toml").read_text())["datasets"]

    assert sorted(entry["file"] for entry in manifest) == [
        path.name for path in REPOSITORY_DATASETS
    ]
    for entry in manifest:
        dataset = load_dataset(EVALUATIONS / "datasets" / entry["file"])
        assert entry == {
            "file": entry["file"],
            "dataset": dataset.name,
            "version": dataset.version,
            "partition": dataset.partition.value,
            "cases": len(dataset.cases),
            "sha256": dataset.sha256,
        }


@pytest.mark.parametrize(("development", "holdout"), PARTITION_PAIRS, ids=lambda path: path.name)
def test_repository_holdouts_are_independent_of_development_data(
    development: Path, holdout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--development", str(development), "--holdout", str(holdout)]) == 0
    assert "no shared or near-duplicate cases" in capsys.readouterr().out


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


def test_partition_defaults_to_development_and_is_uniform(tmp_path: Path) -> None:
    assert load_dataset(write_cases(tmp_path, label_case())).partition is Partition.DEVELOPMENT
    holdout = load_dataset(write_cases(tmp_path, label_case(partition="holdout")))
    assert holdout.partition is Partition.HOLDOUT

    with pytest.raises(DatasetError, match="partition of the first case"):
        load_dataset(write_cases(tmp_path, label_case("a"), label_case("b", partition="holdout")))


def check(tmp_path: Path, development: list[dict[str, Any]], holdout: list[dict[str, Any]]) -> None:
    check_partitions(
        load_dataset(write_cases(tmp_path, *development, name="development.jsonl")),
        load_dataset(write_cases(tmp_path, *holdout, name="holdout.jsonl")),
    )


def holdout_case(case_id: str = "h1", **overrides: Any) -> dict[str, Any]:
    case = label_case(
        case_id,
        partition="holdout",
        messages=[{"role": "user", "content": "Where is the parcel I ordered on Monday?"}],
        expected={"label": "shipping", "labels": ["billing", "shipping"]},
    )
    case.update(overrides)
    return case


def test_independent_partitions_pass(tmp_path: Path) -> None:
    check(tmp_path, [label_case("d1")], [holdout_case("h1")])


@pytest.mark.parametrize(
    ("development", "holdout", "message"),
    [
        ([label_case("same")], [holdout_case("same")], "share case ids: same"),
        (
            [label_case("d1")],
            [holdout_case(messages=label_case()["messages"])],
            "h1 duplicates development case d1",
        ),
        (
            [
                label_case(
                    "d1", messages=[{"role": "user", "content": "Order 1234 arrived on May 2."}]
                )
            ],
            [holdout_case(messages=[{"role": "user", "content": "ORDER 98 arrived on May 30!"}])],
            "duplicates development case d1",
        ),
        (
            [
                label_case(
                    "d1", messages=[{"role": "system", "content": "A"}, *label_case()["messages"]]
                )
            ],
            [holdout_case()],
            "different system prompts or label sets",
        ),
        (
            [label_case("d1")],
            [holdout_case(expected={"label": "shipping", "labels": ["shipping", "account"]})],
            "different system prompts or label sets",
        ),
        ([label_case("d1")], [holdout_case(dataset_version="2")], "different dataset versions"),
        ([label_case("d1")], [holdout_case(partition="development")], "not a holdout partition"),
        (
            [label_case("d1", partition="holdout")],
            [holdout_case()],
            "not a development partition",
        ),
    ],
    ids=[
        "shared-id",
        "exact-duplicate",
        "numbers-changed",
        "system-prompt",
        "label-set",
        "version",
        "two-development",
        "two-holdout",
    ],
)
def test_contaminated_or_mismatched_partitions_are_rejected(
    tmp_path: Path,
    development: list[dict[str, Any]],
    holdout: list[dict[str, Any]],
    message: str,
) -> None:
    with pytest.raises(DatasetError, match=message):
        check(tmp_path, development, holdout)


def test_check_command_reports_contamination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    development = write_cases(tmp_path, label_case("d1"), name="development.jsonl")
    holdout = write_cases(
        tmp_path, holdout_case(messages=label_case()["messages"]), name="holdout.jsonl"
    )

    code = main(["check", "--development", str(development), "--holdout", str(holdout)])

    assert code == 2
    assert "duplicates development case d1" in capsys.readouterr().err
