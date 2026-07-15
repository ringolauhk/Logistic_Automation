"""Atomic output writing for the operator-facing CLI (M7).

A run's outputs are treated as ONE artifact set: every temporary file is
written and closed successfully BEFORE any existing final artifact is
replaced. If any temp write fails (or the run is interrupted), no final
artifact is touched and every temp is cleaned up - old outputs remain exactly
as they were.

Each temp lives in the SAME directory as its final path so os.replace() is a
true atomic rename (a cross-directory replace is not atomic). The multi-file
commit() replaces each final in turn: os.replace of a single file is atomic,
but the SET of replacements is not transactional - if the process is killed
between two replaces, some finals may be new and some old. We minimize that
window by doing ALL temp generation first and only then the (fast, back-to-
back) replaces; see commit()'s note. This is a deliberate, documented
limitation, not a bug.
"""

import os
from pathlib import Path

_TEMP_SUFFIX = ".tmp-"


class StagedArtifacts:
    """Stage N artifacts, then commit them together.

    Usage:
        with StagedArtifacts() as stage:
            stage.stage(workbook_path, lambda p: export_workbook(results, p))
            stage.stage(usage_path, lambda p: write_usage_csv(records, p))
            stage.commit()          # replaces all finals only after all temps OK

    If stage()/commit() is not reached (exception, KeyboardInterrupt), __exit__
    removes any temps not yet committed and leaves all finals untouched.
    """

    def __init__(self):
        self._pending: list[tuple[Path, Path]] = []  # (final, temp) not yet committed

    def stage(self, final_path, writer) -> Path:
        """Write one artifact to a temp beside its final path. `writer(temp)`
        performs the actual write; it may raise, in which case the temp is
        removed and no final is affected."""
        final_path = Path(final_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp = final_path.parent / f"{final_path.name}{_TEMP_SUFFIX}{os.getpid()}-{len(self._pending)}"
        try:
            writer(temp)
        except BaseException:
            _silent_unlink(temp)
            raise
        self._pending.append((final_path, temp))
        return final_path

    def commit(self) -> None:
        """Replace every final with its temp. Only reached once all temps were
        written successfully. Replaces run back-to-back to shrink the (non-
        transactional) multi-file window - see module docstring."""
        while self._pending:
            final_path, temp = self._pending.pop(0)
            os.replace(temp, final_path)

    def cleanup(self) -> None:
        while self._pending:
            _, temp = self._pending.pop(0)
            _silent_unlink(temp)

    def __enter__(self) -> "StagedArtifacts":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Anything left in _pending was never committed (early error / interrupt
        # / caller forgot commit) - remove those temps. Finals are untouched.
        self.cleanup()
        return False


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
