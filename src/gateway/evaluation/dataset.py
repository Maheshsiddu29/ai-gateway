"""Versioned JSONL evaluation datasets.

Each line is one self-contained case. Every case in a file must name the same
dataset, version, task, evaluation method, and partition, so a file is one
partition of one versioned dataset. The file's SHA-256 is recorded with every
evaluation run, which makes a result traceable to the exact bytes that produced it.

A dataset version has a development partition, whose results may qualify models
for routing, and optionally a holdout partition, used only to measure a routing
policy after its evidence is fixed. ``check_partitions`` verifies that the two
share a task definition and no cases.
"""

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gateway.routing.config import DatasetName, DatasetVersion
from gateway.schemas.completions import ChatMessage

# Evaluation workloads are bounded so that a mistaken dataset cannot start an
# unbounded number of paid provider calls.
MAX_CASES = 5000
MAX_LINE_BYTES = 64 * 1024


class TaskType(StrEnum):
    CLASSIFICATION = "classification"
    EXTRACTION = "extraction"


class EvaluationMethod(StrEnum):
    # The response must be exactly one of the allowed labels.
    EXACT_LABEL = "exact_label"
    # The response must be a JSON object; scored by F1 over (field, value) pairs.
    FIELD_F1 = "field_f1"


class Partition(StrEnum):
    # Results may become routing evidence.
    DEVELOPMENT = "development"
    # Results are never routing evidence; they measure policies independently.
    HOLDOUT = "holdout"


_METHOD_FOR_TASK = {
    TaskType.CLASSIFICATION: EvaluationMethod.EXACT_LABEL,
    TaskType.EXTRACTION: EvaluationMethod.FIELD_F1,
}

Label = Annotated[str, Field(min_length=1, max_length=64)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LabelExpectation(_Strict):
    label: Label
    labels: list[Label] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def _check_label(self) -> "LabelExpectation":
        if self.label not in self.labels:
            raise ValueError("label must be one of labels")
        if len({label.casefold() for label in self.labels}) != len(self.labels):
            raise ValueError("labels must be distinct ignoring case")
        return self


class FieldsExpectation(_Strict):
    fields: dict[Annotated[str, Field(min_length=1, max_length=64)], str] = Field(
        min_length=1, max_length=64
    )
    # A case passes when the F1 score reaches this value.
    min_f1: float = Field(default=1.0, gt=0, le=1)


class EvaluationCase(_Strict):
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    dataset: DatasetName
    dataset_version: DatasetVersion
    task: TaskType
    method: EvaluationMethod
    # Files released before partitions existed are development data.
    partition: Partition = Partition.DEVELOPMENT
    messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    expected: LabelExpectation | FieldsExpectation

    @model_validator(mode="after")
    def _check_method(self) -> "EvaluationCase":
        method = _METHOD_FOR_TASK[self.task]
        if method is not self.method:
            raise ValueError(f"task {self.task.value} is evaluated with {method.value}")
        expected_type = (
            LabelExpectation if self.method is EvaluationMethod.EXACT_LABEL else FieldsExpectation
        )
        if not isinstance(self.expected, expected_type):
            raise ValueError(f"method {self.method.value} needs a {expected_type.__name__}")
        return self


@dataclass(frozen=True)
class Dataset:
    name: str
    version: str
    task: TaskType
    method: EvaluationMethod
    partition: Partition
    sha256: str
    cases: tuple[EvaluationCase, ...]


class DatasetError(Exception):
    """A dataset file that cannot be used. The message names the line."""


def load_dataset(path: Path) -> Dataset:
    data = path.read_bytes()
    cases: list[EvaluationCase] = []
    for number, line in enumerate(data.splitlines(), start=1):
        if not line.strip():
            continue
        if len(line) > MAX_LINE_BYTES:
            raise DatasetError(f"{path}:{number}: line exceeds {MAX_LINE_BYTES} bytes")
        try:
            cases.append(EvaluationCase.model_validate_json(line))
        except ValidationError as exc:
            error = exc.errors(include_url=False, include_input=False)[0]
            location = ".".join(str(part) for part in error["loc"]) or "case"
            raise DatasetError(f"{path}:{number}: {location}: {error['msg']}") from None
        if len(cases) > MAX_CASES:
            raise DatasetError(f"{path}: more than {MAX_CASES} cases")
    if not cases:
        raise DatasetError(f"{path}: no cases")
    first = cases[0]
    identity = _identity(first)
    seen: set[str] = set()
    for case in cases:
        if _identity(case) != identity:
            raise DatasetError(
                f"{path}: case {case.id} does not match the dataset, version, task, method,"
                " and partition of the first case"
            )
        if case.id in seen:
            raise DatasetError(f"{path}: duplicate case id {case.id}")
        seen.add(case.id)
    return Dataset(
        name=first.dataset,
        version=first.dataset_version,
        task=first.task,
        method=first.method,
        partition=first.partition,
        sha256=hashlib.sha256(data).hexdigest(),
        cases=tuple(cases),
    )


def _identity(case: EvaluationCase) -> tuple[str, str, TaskType, EvaluationMethod, Partition]:
    return case.dataset, case.dataset_version, case.task, case.method, case.partition


# Two cases whose non-system text shares at least this fraction of distinct words
# (Jaccard similarity, digits ignored) are treated as near-duplicates.
NEAR_DUPLICATE_SIMILARITY = 0.6
_WORD = re.compile(r"[^\W_]+")
_DIGITS = re.compile(r"\d+")


def check_partitions(development: Dataset, holdout: Dataset) -> None:
    """Raise DatasetError unless ``holdout`` can measure what ``development`` qualified.

    Both must be partitions of the same dataset version with the same task
    definition (system prompts and label sets), and they must not share a case:
    neither an ID, nor the same or nearly the same non-system text.
    """
    if development.partition is not Partition.DEVELOPMENT:
        raise DatasetError(f"{development.name}: the first dataset is not a development partition")
    if holdout.partition is not Partition.HOLDOUT:
        raise DatasetError(f"{holdout.name}: the second dataset is not a holdout partition")
    for attribute in ("name", "version", "task", "method"):
        if getattr(development, attribute) != getattr(holdout, attribute):
            raise DatasetError(f"the partitions have different dataset {attribute}s")
    if _task_definition(development) != _task_definition(holdout):
        raise DatasetError("the partitions have different system prompts or label sets")
    shared = {case.id for case in development.cases} & {case.id for case in holdout.cases}
    if shared:
        raise DatasetError(f"the partitions share case ids: {', '.join(sorted(shared))}")
    words = [(case.id, _words(case)) for case in development.cases]
    for case in holdout.cases:
        candidate = _words(case)
        for other_id, other in words:
            union = candidate | other
            similarity = len(candidate & other) / len(union) if union else 1.0
            if similarity >= NEAR_DUPLICATE_SIMILARITY:
                raise DatasetError(
                    f"holdout case {case.id} duplicates development case {other_id}"
                    f" (word similarity {similarity:.2f})"
                )


def _task_definition(dataset: Dataset) -> tuple[frozenset[str], frozenset[tuple[str, ...]]]:
    prompts = frozenset(
        message.content
        for case in dataset.cases
        for message in case.messages
        if message.role == "system"
    )
    label_sets = frozenset(
        tuple(case.expected.labels)
        for case in dataset.cases
        if isinstance(case.expected, LabelExpectation)
    )
    return prompts, label_sets


def _words(case: EvaluationCase) -> frozenset[str]:
    text = " ".join(message.content for message in case.messages if message.role != "system")
    # A copied case with only its numbers changed is still the same case.
    return frozenset(_DIGITS.sub("0", word) for word in _WORD.findall(text.casefold()))
