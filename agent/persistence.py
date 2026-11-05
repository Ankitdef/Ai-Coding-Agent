from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any
from client.response import TokenUsage
from config.loader import get_data_dir


@dataclass
class SessionSnapshot:
    session_id: str
    created_at: datetime
    updated_at: datetime
    turn_count: int
    messages: list[dict[str, Any]]
    total_usage: TokenUsage
    tool_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "turn_count": self.turn_count,
            "messages": self.messages,
            "total_usage": self.total_usage.__dict__,
            "tool_state": self.tool_state,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionSnapshot:
        return cls(
            session_id=data["session_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            turn_count=data["turn_count"],
            messages=data["messages"],
            total_usage=TokenUsage(**data["total_usage"]),
            tool_state=data.get("tool_state") or {},
        )


class PersistenceManager:
    def __init__(self):
        self.data_dir = get_data_dir()
        self.sessions_dir = self.data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir = self.data_dir / "checkpoints"
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.sessions_dir, 0o700)
        os.chmod(self.checkpoints_dir, 0o700)

    def _write_json(self, file_path: Path, payload: dict[str, Any]) -> None:
        tmp_path = file_path.with_suffix(".json.tmp")

        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2)

        os.replace(tmp_path, file_path)
        os.chmod(file_path, 0o600)

    def save_session(self, snapshot: SessionSnapshot) -> None:
        file_path = self.sessions_dir / f"{snapshot.session_id}.json"
        self._write_json(file_path, snapshot.to_dict())

    def load_session(self, session_id: str) -> SessionSnapshot | None:
        file_path = self.sessions_dir / f"{session_id}.json"

        if not file_path.exists():
            return None

        with open(file_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        return SessionSnapshot.from_dict(data)

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = []
        for file_path in self.sessions_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
            except (OSError, ValueError, KeyError):
                continue

            sessions.append(
                {
                    "session_id": data["session_id"],
                    "created_at": data["created_at"],
                    "updated_at": data["updated_at"],
                    "turn_count": data["turn_count"],
                }
            )

        sessions.sort(key=lambda x: x["updated_at"], reverse=True)
        return sessions

    def latest_session(self) -> SessionSnapshot | None:
        sessions = self.list_sessions()

        if not sessions:
            return None

        return self.load_session(sessions[0]["session_id"])

    def save_checkpoint(self, snapshot: SessionSnapshot) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        checkpoint_id = f"{snapshot.session_id}_{timestamp}"
        file_path = self.checkpoints_dir / f"{checkpoint_id}.json"
        self._write_json(file_path, snapshot.to_dict())

        return checkpoint_id

    def load_checkpoint(self, checkpoint_id: str) -> SessionSnapshot | None:
        file_path = self.checkpoints_dir / f"{checkpoint_id}.json"

        if not file_path.exists():
            return None

        with open(file_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        return SessionSnapshot.from_dict(data)

    def list_checkpoints(self) -> list[dict[str, Any]]:
        checkpoints = []
        for file_path in self.checkpoints_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
            except (OSError, ValueError, KeyError):
                continue

            checkpoints.append(
                {
                    "checkpoint_id": file_path.stem,
                    "session_id": data["session_id"],
                    "created_at": data["created_at"],
                    "turn_count": data["turn_count"],
                }
            )

        checkpoints.sort(key=lambda x: x["checkpoint_id"], reverse=True)
        return checkpoints
