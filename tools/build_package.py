#!/usr/bin/env python3
"""Build a source-complete local AstrBot plugin ZIP from an explicit file manifest."""

import ast
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
NAME = "astrbot_plugin_mc_monitor"
ROOT_FILES = [
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
]
COMPONENTS = [
    "__init__.py",
    "config.py",
    "models.py",
    "storage.py",
    "tailer.py",
    "parser.py",
    "probes.py",
    "engine.py",
    "notifier.py",
]
DOCS = ["CONFIGURATION.md", "DEPLOYMENT.md", "ARCHITECTURE.md", "VALIDATION.md"]
TOOLS = [
    "docker_readonly_bridge.py",
    "mc-monitor-bridge.service.example",
    "build_package.py",
    "check_astrbot_compat.py",
]


def main():
    version = re.search(
        r"^version: ([0-9]+\.[0-9]+\.[0-9]+)$", (ROOT / "metadata.yaml").read_text("utf-8"), re.M
    )[1]
    files = [ROOT / x for x in ROOT_FILES]
    files += [ROOT / "mc_monitor" / x for x in COMPONENTS]
    files += [ROOT / "docs" / x for x in DOCS]
    files += [ROOT / "tools" / x for x in TOOLS]
    files += sorted((ROOT / "tests").glob("*.py"))
    for file in files:
        if not file.is_file() or file.is_symlink():
            raise RuntimeError("missing_or_symlink_artifact: " + str(file.relative_to(ROOT)))
        if file.suffix == ".py":
            ast.parse(file.read_text("utf-8"), filename=str(file))
    json.loads((ROOT / "_conf_schema.json").read_text("utf-8"))
    output = ROOT / "dist" / f"{NAME}-v{version}.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for file in sorted(files):
            archive.write(file, NAME + "/" + file.relative_to(ROOT).as_posix())
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert NAME + "/main.py" in names and NAME + "/metadata.yaml" in names
        assert not any(
            x in name.split("/")
            for name in names
            for x in (".venv", ".del", "__pycache__", ".env", "dist", ".pytest_cache")
        )
        assert output.stat().st_size < 16 * 1024 * 1024
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "path": str(output),
                "files": len(files),
                "bytes": output.stat().st_size,
                "sha256": digest,
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
