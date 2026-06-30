from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class JsonStore:
    MAX_LOG_BYTES = 2_000_000  # rotate the jsonl log past ~2 MB so it stays bounded

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.data_dir / "agent_log.jsonl"
        self.approval_path = self.data_dir / "approvals.json"
        self.positions_path = self.data_dir / "positions.json"
        self.conversations_path = self.data_dir / "conversations.json"
        self.config_path = self.data_dir / "runtime_config.json"
        # FastAPI runs sync endpoints in a threadpool, so guard the read-modify-write
        # of the json docs (and log appends) against concurrent requests.
        self._lock = threading.Lock()

    def append_log(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "type": event_type,
            "payload": payload,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            self._rotate_log_if_needed()
            with self.log_path.open("a", encoding="utf-8") as file:
                file.write(line)
        return entry

    def _rotate_log_if_needed(self) -> None:
        try:
            if self.log_path.stat().st_size < self.MAX_LOG_BYTES:
                return
        except FileNotFoundError:
            return
        # Keep a single rotated backup (agent_log.jsonl.1) and start fresh.
        os.replace(self.log_path, self.log_path.with_suffix(".jsonl.1"))

    def read_logs(self, limit: int = 80) -> List[Dict[str, Any]]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()[-limit:]
        output = []
        for line in lines:
            try:
                output.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(output))

    def save_approval(self, approval: Dict[str, Any]) -> None:
        with self._lock:
            approvals = self._read_json(self.approval_path)
            approvals[approval["id"]] = approval
            self._write_json(self.approval_path, approvals)

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read_json(self.approval_path).get(approval_id)

    def update_approval(self, approval_id: str, **changes: Any) -> Dict[str, Any]:
        with self._lock:
            approvals = self._read_json(self.approval_path)
            if approval_id not in approvals:
                raise KeyError(approval_id)
            approvals[approval_id].update(changes)
            self._write_json(self.approval_path, approvals)
            return approvals[approval_id]

    def mutate_approval(
        self, approval_id: str, mutate: Callable[[Dict[str, Any]], Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Atomically read-modify-write a single approval under the store lock.

        ``mutate`` receives the current approval and returns the changed fields to
        persist. Used for the execute transition so the pending->executed check and
        the write happen as one critical section (no double-execute race).
        """
        with self._lock:
            approvals = self._read_json(self.approval_path)
            if approval_id not in approvals:
                raise KeyError(approval_id)
            changes = mutate(approvals[approval_id])
            approvals[approval_id].update(changes)
            self._write_json(self.approval_path, approvals)
            return approvals[approval_id]

    def read_positions(self) -> Dict[str, Any]:
        with self._lock:
            return self._read_json(self.positions_path)

    def mutate_positions(
        self, mutate: Callable[[Dict[str, Any]], None]
    ) -> Dict[str, Any]:
        """Atomically read-modify-write the positions doc under the store lock."""
        with self._lock:
            doc = self._read_json(self.positions_path)
            mutate(doc)
            self._write_json(self.positions_path, doc)
            return doc

    # --- Conversations (multi-turn chat persistence) -----------------------

    MAX_CONVERSATION_MESSAGES = 60

    def append_message(self, session_id: str, role: str, content: str) -> Dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        captured: Dict[str, Any] = {}

        def mutate(doc: Dict[str, Any]) -> None:
            conv = doc.get(session_id)
            if conv is None:
                conv = {"id": session_id, "created_at": now, "messages": []}
                doc[session_id] = conv
            conv["messages"].append({"role": role, "content": content, "ts": now})
            conv["messages"] = conv["messages"][-self.MAX_CONVERSATION_MESSAGES:]
            conv["updated_at"] = now
            captured["conv"] = conv

        with self._lock:
            doc = self._read_json(self.conversations_path)
            mutate(doc)
            self._write_json(self.conversations_path, doc)
        return captured["conv"]

    def get_conversation(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read_json(self.conversations_path).get(session_id)

    def list_conversations(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            doc = self._read_json(self.conversations_path)
        items = []
        for conv in doc.values():
            messages = conv.get("messages", [])
            first_user = next((m["content"] for m in messages if m.get("role") == "user"), "")
            items.append(
                {
                    "id": conv.get("id"),
                    "updated_at": conv.get("updated_at", conv.get("created_at", "")),
                    "message_count": len(messages),
                    "preview": first_user[:40],
                }
            )
        items.sort(key=lambda c: c["updated_at"], reverse=True)
        return items[:limit]

    # --- Runtime config (UI-settable model / thinking overrides) -----------

    def read_config(self) -> Dict[str, Any]:
        with self._lock:
            return self._read_json(self.config_path)

    def update_config(self, **changes: Any) -> Dict[str, Any]:
        with self._lock:
            doc = self._read_json(self.config_path)
            doc.update({k: v for k, v in changes.items() if v is not None})
            self._write_json(self.config_path, doc)
            return doc

    def _read_json(self, path: Path) -> Dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            # A corrupt/torn file should not silently erase history: preserve it for
            # inspection and start fresh rather than overwriting good data on top of
            # a misread.
            path.replace(path.with_suffix(path.suffix + ".corrupt"))
            return {}

    def _write_json(self, path: Path, data: Dict[str, Any]) -> None:
        # Atomic write: temp file in the same dir + os.replace (atomic on POSIX) so a
        # crash mid-write cannot truncate the file and wipe its contents.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
