"""Resolve the running dump3411 version once at process start.

Order of preference (see issue #8):
1. ``git describe --tags --always --dirty`` when a ``.git`` directory is present
2. ``.git_archival.txt`` filled by GitHub's ``export-subst`` on source archives
3. ``[project].version`` from ``pyproject.toml``
4. ``"unknown"``

Never raises — a version failure must not take the detector down.
"""

from __future__ import annotations

import logging
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("dump3411.version")

_ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return the process version string. Cached for the life of the process."""
    for resolver in (_from_git, _from_archival, _from_pyproject):
        try:
            value = resolver()
        except Exception:
            log.debug("version resolver %s failed", resolver.__name__, exc_info=True)
            continue
        if value:
            return value
    return "unknown"


def _from_git() -> str | None:
    if not (_ROOT / ".git").exists():
        return None
    result = subprocess.run(
        [
            "git",
            "-c", f"safe.directory={_ROOT}",
            "describe", "--tags", "--always", "--dirty",
        ],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _is_unexpanded(value: str) -> bool:
    # GitHub / git-archive leave "$Format:…$" when export-subst did not run.
    return value.startswith("$Format:") and value.endswith("$")


def _from_archival() -> str | None:
    path = _ROOT / ".git_archival.txt"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    # Prefer a line like "describe-name: v1.0.0-17-g9c1ee2e" if present,
    # otherwise take the whole file contents.
    for line in text.splitlines():
        if line.startswith("describe-name:"):
            value = line.split(":", 1)[1].strip()
            if value and not _is_unexpanded(value):
                return value
            return None
    first = text.splitlines()[0].strip()
    if first and not _is_unexpanded(first):
        return first
    return None


def _from_pyproject() -> str | None:
    path = _ROOT / "pyproject.toml"
    if not path.is_file():
        return None
    # Avoid adding a tomllib/toml dependency for a one-field read.
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            _, _, rhs = stripped.partition("=")
            value = rhs.strip().strip("\"'")
            if value:
                return value
    return None


# -- Standalone smoke test -----------------------------------------------------

if __name__ == "__main__":
    import importlib.util
    import shutil
    import tempfile

    def _load(root: Path):
        spec = importlib.util.spec_from_file_location(f"version_{root.name}", root / "version.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.get_version()

    print(f"this checkout: {get_version()}")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        # Source archive with export-subst applied.
        a = base / "archive"; a.mkdir()
        shutil.copy(__file__, a / "version.py")
        (a / ".git_archival.txt").write_text("describe-name: v1.2.3-4-gabcdef0\n")
        (a / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')
        assert _load(a) == "v1.2.3-4-gabcdef0"

        # Archive placeholder left unexpanded -> pyproject.
        b = base / "unexpanded"; b.mkdir()
        shutil.copy(__file__, b / "version.py")
        (b / ".git_archival.txt").write_text("describe-name: $Format:%(describe:tags)$\n")
        (b / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
        assert _load(b) == "1.0.0"

        # Broken .git directory -> pyproject, no exception.
        c = base / "brokengit"; c.mkdir(); (c / ".git").mkdir()
        shutil.copy(__file__, c / "version.py")
        (c / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
        assert _load(c) == "1.0.0"

        # Nothing at all -> "unknown".
        d = base / "bare"; d.mkdir()
        shutil.copy(__file__, d / "version.py")
        assert _load(d) == "unknown"

    print("smoke test OK")
