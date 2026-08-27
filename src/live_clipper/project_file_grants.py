from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock


class FileSelectionGrantError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FileSelectionGrant:
    token: str
    issue_id: str
    kind: str
    selected_path: Path
    expires_at: float
    consumed: bool = False


class FileSelectionGrantStore:
    """Process-local, single-use authorization for one user-selected path."""

    def __init__(self, *, ttl_seconds: int = 300, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._grants: dict[str, FileSelectionGrant] = {}
        self._lock = Lock()

    def issue(self, *, issue_id: str, kind: str, selected_path: str | Path) -> str:
        if kind not in {"source", "recovery_output"}:
            raise FileSelectionGrantError("selection_token_invalid")
        raw_path = Path(selected_path).expanduser()
        if not raw_path.is_absolute():
            raise FileSelectionGrantError("selection_token_invalid")
        normalized = raw_path.resolve(strict=False)
        token = secrets.token_urlsafe(32)
        grant = FileSelectionGrant(
            token=token,
            issue_id=issue_id,
            kind=kind,
            selected_path=normalized,
            expires_at=self._clock() + self.ttl_seconds,
        )
        with self._lock:
            self._grants[token] = grant
        return token

    def consume(self, token: str, *, issue_id: str, kind: str) -> Path:
        with self._lock:
            grant = self._grants.get(token)
            if grant is None or grant.issue_id != issue_id or grant.kind != kind:
                raise FileSelectionGrantError("selection_token_invalid")
            if grant.consumed:
                raise FileSelectionGrantError("selection_token_already_used")
            if self._clock() > grant.expires_at:
                raise FileSelectionGrantError("selection_token_expired")
            self._grants[token] = FileSelectionGrant(**{**grant.__dict__, "consumed": True})
            return grant.selected_path


_PROCESS_GRANTS = FileSelectionGrantStore()


def process_file_selection_grants() -> FileSelectionGrantStore:
    return _PROCESS_GRANTS
