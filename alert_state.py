"""Small durable store for subscriptions and successful S-rank deliveries."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


class AlertState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data.get("subscriptions"), dict) or not isinstance(data.get("sent"), dict):
                raise ValueError("S播报状态文件格式错误；请保留备份后检查文件")
            self.subscriptions = data["subscriptions"]
            self.sent = data["sent"]
        else:
            self.subscriptions = {}
            self.sent = {}

    def save(self):
        cutoff = time.time() - 14 * 86400
        self.sent = {k: v for k, v in self.sent.items() if v["at"] > cutoff}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"subscriptions": self.subscriptions, "sent": self.sent},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def subscribe(self, umo: str, group: str):
        if umo not in self.subscriptions:
            self.subscriptions[umo] = {"group": group, "since": time.time()}
            self.save()

    def unsubscribe(self, umo: str):
        self.subscriptions.pop(umo, None)
        self.save()

    @staticmethod
    def key(spawn_key: str, umo: str) -> str:
        return hashlib.sha256((spawn_key + "\0" + umo).encode()).hexdigest()

    def delivery(self, spawn_key: str, umo: str):
        return self.sent.get(self.key(spawn_key, umo))

    def mark_sent(self, spawn_key: str, umo: str, has_map: bool):
        self.sent[self.key(spawn_key, umo)] = {"at": time.time(), "has_map": has_map}
        self.save()
