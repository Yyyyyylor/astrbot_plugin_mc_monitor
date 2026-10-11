"""Bounded binary tailing with persistent identity and content anchors."""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


def identity(stat):
    return f"{stat.st_dev}:{stat.st_ino}"


def anchor(file, offset):
    file.seek(max(0, offset - 128))
    return hashlib.sha256(file.read(min(offset, 128))).hexdigest()


@dataclass
class Batch:
    cursor: dict
    lines: list[tuple[str, str, float]]
    issues: list[str]


class Tailer:
    def read(self, cfg, saved):
        path = Path(cfg["path"])
        cursor = dict(saved or {})
        issues = []
        current = path.stat() if path.exists() else None
        active = path
        if not cursor:
            if current is None:
                raise FileNotFoundError("log_unavailable")
            cursor = {
                "identity": identity(current),
                "offset": 0,
                "generation": 0,
                "done": [],
                "mtime": current.st_mtime,
                "discard": False,
            }
            if cfg["initial_position"] == "end":
                cursor["offset"] = current.st_size
                # Don't parse a suffix of a partially written historical line.
                with path.open("rb") as file:
                    if current.st_size:
                        file.seek(current.st_size - 1)
                        cursor["discard"] = file.read(1) != b"\n"
                    cursor["anchor"] = anchor(file, cursor["offset"])
                    self._head(file, cursor)
                return Batch(cursor, [], [])
        if current is None or identity(current) != cursor["identity"]:
            candidates = []
            # Limit directory scans and compressed recovery work; never recurse.
            with os.scandir(path.parent) as entries:
                for i, entry in enumerate(entries):
                    if i >= 4096:
                        issues.append("rotation_scan_limit")
                        break
                    if fnmatch.fnmatch(entry.name, cfg["rotation_glob"]) and entry.is_file():
                        # DirEntry.stat on Windows 3.12 reports st_ino=0; Path.stat is stable.
                        stat = Path(entry.path).stat()
                        candidates.append((Path(entry.path), stat))
            old = next((x for x in candidates if identity(x[1]) == cursor["identity"]), None)
            if old is None:
                old = self._recover_compressed(candidates, cursor, cfg["recovery_max_bytes"])
                if old:
                    cursor["done"] = (cursor.get("done", []) + [cursor["identity"]])[-128:]
                    cursor["identity"] = identity(old[1])
            if old:
                active = old[0]
            else:
                if current is None:
                    raise FileNotFoundError("log_unavailable")
                issues.append("rotation_source_missing_possible_gap")
                cursor = self._switch(cursor, current)
        compressed = active.suffix == ".gz"
        if compressed and cursor["offset"] > cfg["recovery_max_bytes"]:
            if current is None:
                raise OSError("compressed_recovery_limit")
            return Batch(
                self._switch(cursor, current), [], ["compressed_recovery_limit_possible_gap"]
            )
        with gzip.open(active, "rb") if compressed else active.open("rb") as file:
            stat = os.fstat(file.fileno())
            if identity(stat) != cursor["identity"]:
                # Race between stat and open: retry without advancing anything.
                raise OSError("log_changed_during_open")
            offset = cursor["offset"]
            if (not compressed and stat.st_size < offset) or (
                cursor.get("anchor") is not None and anchor(file, offset) != cursor["anchor"]
            ):
                issues.append("log_truncated_or_rewritten")
                cursor = self._switch(cursor, stat)
                offset = 0
            file.seek(offset)
            raw = file.read(cfg["max_bytes"] + 1)
            file_end = len(raw) <= cfg["max_bytes"]
            raw = raw[: cfg["max_bytes"]]
            lines = []
            consumed = 0
            segments = raw.split(b"\n")
            parts = [part + b"\n" for part in segments[:-1]]
            if segments[-1]:
                parts.append(segments[-1])
            for part in parts:
                complete = part.endswith(b"\n")
                if not complete and (current is None or identity(stat) != identity(current)):
                    if file_end:
                        complete = True
                        issues.append("unterminated_rotated_line")
                if cursor.get("discard"):
                    consumed += len(part)
                    if complete:
                        cursor["discard"] = False
                    continue
                if len(part) > cfg["max_line_bytes"]:
                    issues.append("oversized_log_line_skipped")
                    consumed += len(part)
                    cursor["discard"] = not complete
                    continue
                if not complete:
                    # Hold until the writer finishes. Max line <= max bytes is validated.
                    break
                end = offset + consumed + len(part)
                origin = f"{cursor['identity']}:{cursor['generation']}:{offset + consumed}"
                lines.append(
                    (part.decode("utf-8", errors="replace").rstrip("\r\n"), origin, stat.st_mtime)
                )
                consumed = end - offset
            cursor["offset"] = offset + consumed
            cursor["anchor"] = anchor(file, cursor["offset"])
            self._head(file, cursor)
            cursor["mtime"] = stat.st_mtime
            eof = file_end and consumed == len(raw)
        if eof and (current is None or identity(stat) != identity(current)):
            # Drain old inode first; retained rotations between it and latest are read in order.
            done = (cursor.get("done", []) + [cursor["identity"]])[-128:]
            eligible = [
                (p, s)
                for p, s in candidates
                if identity(s) not in done and p != path and s.st_mtime >= cursor["mtime"]
            ]
            eligible.sort(key=lambda x: x[1].st_mtime)
            next_stat = eligible[0][1] if eligible else current
            if next_stat is not None:
                cursor["done"] = done
                cursor = self._switch(cursor, next_stat)
        return Batch(cursor, lines, list(dict.fromkeys(issues)))

    @staticmethod
    def _head(file, cursor):
        size = min(cursor["offset"], 128)
        file.seek(0)
        cursor["head_size"] = size
        cursor["head"] = hashlib.sha256(file.read(size)).hexdigest()

    @staticmethod
    def _recover_compressed(candidates, cursor, budget):
        offset = cursor["offset"]
        if not offset or offset > budget or not cursor.get("anchor"):
            return None
        archives = sorted(
            (x for x in candidates if x[0].suffix == ".gz"),
            key=lambda x: x[1].st_mtime,
            reverse=True,
        )[:16]
        for path, stat in archives:
            if offset > budget:
                break
            budget -= offset
            try:
                with gzip.open(path, "rb") as file:
                    head = file.read(cursor.get("head_size", 0))
                    if cursor.get("head") and hashlib.sha256(head).hexdigest() != cursor["head"]:
                        continue
                    if anchor(file, offset) == cursor["anchor"]:
                        return path, stat
            except (OSError, EOFError):
                continue
        return None

    @staticmethod
    def _switch(cursor, stat):
        return {
            "identity": identity(stat),
            "offset": 0,
            "generation": cursor.get("generation", 0) + 1,
            "done": cursor.get("done", []),
            "mtime": stat.st_mtime,
            "discard": False,
        }
