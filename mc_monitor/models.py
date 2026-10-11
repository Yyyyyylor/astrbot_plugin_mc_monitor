from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Event:
    kind: str
    at: str
    facts: dict = field(default_factory=dict)
    id: str = ""

    def identify(self, origin):
        self.id = hashlib.sha256(origin.encode("utf-8")).hexdigest()
        return self

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**value)

    @property
    def subject(self):
        return str(
            self.facts.get("player")
            or self.facts.get("metric")
            or self.facts.get("source")
            or self.kind
        )


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
