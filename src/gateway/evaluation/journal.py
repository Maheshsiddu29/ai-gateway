"""An append-only progress journal for one evaluation run.

The journal records each case when it is sent and when its result is known,
flushing and syncing every line, so that the results of an interrupted or
crashed run survive. A case sent but never finished was possibly charged for.
``run`` never reads journals: an interrupted evaluation is not resumed.
"""

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gateway.evaluation.runner import CaseResult

JOURNAL_FORMAT_VERSION = 1


class RunJournal:
    def __init__(self, path: Path, header: dict[str, Any]) -> None:
        self.path = path
        # Exclusive creation: a journal is never overwritten or appended to by
        # another run.
        self._file = path.open("x", encoding="utf-8")
        self._write({"journal_format": JOURNAL_FORMAT_VERSION, "run": header})

    def dispatched(self, case_id: str) -> None:
        self._write({"dispatched": case_id})

    def finished(self, result: CaseResult) -> None:
        self._write({"result": asdict(result)})

    def close(self) -> None:
        self._file.close()

    def discard(self) -> None:
        """Remove the journal once the complete results file has been written."""
        self.close()
        self.path.unlink()

    def _write(self, record: dict[str, Any]) -> None:
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
