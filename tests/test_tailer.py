import gzip
import os

from mc_monitor.config import defaults
from mc_monitor.tailer import Tailer


def config(path):
    result = defaults()["logs"]
    result.update(path=str(path), initial_position="beginning")
    return result


def test_partial_lines_and_restart(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"one\ntw")
    cfg, tail = config(path), Tailer()
    batch = tail.read(cfg, None)
    assert [x[0] for x in batch.lines] == ["one"]
    with path.open("ab") as file:
        file.write(b"o\n")
    after = Tailer().read(cfg, batch.cursor)
    assert [x[0] for x in after.lines] == ["two"]
    assert Tailer().read(cfg, after.cursor).lines == []


def test_first_end_skips_partial_historical_line(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"oldpartial")
    cfg = config(path)
    cfg["initial_position"] = "end"
    first = Tailer().read(cfg, None)
    with path.open("ab") as file:
        file.write(b"suffix\nnew\n")
    assert [x[0] for x in Tailer().read(cfg, first.cursor).lines] == ["new"]


def test_rotation_drains_old_inode_after_restart(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"one\n")
    cfg = config(path)
    first = Tailer().read(cfg, None)
    with path.open("ab") as file:
        file.write(b"two\n")
    path.rename(tmp_path / "old.log")
    path.write_bytes(b"three\n")
    second = Tailer().read(cfg, first.cursor)
    third = Tailer().read(cfg, second.cursor)
    assert [x[0] for x in second.lines + third.lines] == ["two", "three"]
    assert Tailer().read(cfg, third.cursor).lines == []


def test_copytruncate_even_when_regrown_larger(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"old\n")
    cfg = config(path)
    first = Tailer().read(cfg, None)
    path.write_bytes(b"new-longer\n")
    second = Tailer().read(cfg, first.cursor)
    assert [x[0] for x in second.lines] == ["new-longer"]
    assert "log_truncated_or_rewritten" in second.issues


def test_missing_rotation_reports_gap_and_reconnects(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"old\n")
    cfg = config(path)
    first = Tailer().read(cfg, None)
    # A different inode simulates old compressed/removed rotation, outside the matching glob.
    path.rename(tmp_path / "unavailable.gz")
    path.write_bytes(b"new\n")
    second = Tailer().read(cfg, first.cursor)
    assert second.lines[0][0] == "new"
    assert "rotation_source_missing_possible_gap" in second.issues


def test_bounded_oversized_line(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"x" * 20000 + b"\nok\n")
    cfg = config(path)
    cfg.update(max_bytes=4096, max_line_bytes=1024)
    cursor, seen, issues = None, [], []
    for _ in range(8):
        batch = Tailer().read(cfg, cursor)
        cursor = batch.cursor
        seen.extend(x[0] for x in batch.lines)
        issues.extend(batch.issues)
    assert seen == ["ok"]
    assert "oversized_log_line_skipped" in issues
    assert cursor["offset"] == os.stat(path).st_size


def test_compressed_rotation_resume_unread_suffix_after_restart(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"already-read\n")
    cfg = config(path)
    cfg["rotation_glob"] = "*.log*"
    first = Tailer().read(cfg, None)
    with gzip.open(tmp_path / "2026-10-11-1.log.gz", "wb") as file:
        file.write(b"already-read\nunread\nlast-without-newline")
    path.rename(tmp_path / "outside-scan.data")
    path.write_bytes(b"new-generation\n")
    second = Tailer().read(cfg, first.cursor)
    third = Tailer().read(cfg, second.cursor)
    assert [x[0] for x in second.lines + third.lines] == [
        "unread",
        "last-without-newline",
        "new-generation",
    ]
    assert "rotation_source_missing_possible_gap" not in second.issues


def test_multiple_retained_rotations_are_drained_in_order(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"one\n")
    cfg = config(path)
    first = Tailer().read(cfg, None)
    path.rename(tmp_path / "first.log")
    middle = tmp_path / "middle.log"
    middle.write_bytes(b"two\n")
    os.utime(middle, (os.stat(middle).st_atime, first.cursor["mtime"] + 1))
    path.write_bytes(b"three\n")
    os.utime(path, (os.stat(path).st_atime, first.cursor["mtime"] + 2))
    events, cursor = [], first.cursor
    for _ in range(4):
        batch = Tailer().read(cfg, cursor)
        cursor = batch.cursor
        events.extend(x[0] for x in batch.lines)
    assert events == ["two", "three"]
