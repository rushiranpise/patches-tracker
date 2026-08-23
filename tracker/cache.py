"""APK download cache — avoids re-downloading the same stock APK across runs.

Cache key is derived from (package, version, source, url, arch, dpi, apk_types).
Only the stock APK is cached; the patched APK is always rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


def cache_key(
    package: str,
    version: str,
    source: str,
    url: str,
    arch: str,
    dpi: str,
    apk_types: list[str],
) -> str:
    """Deterministic short hash from the resolver inputs."""
    raw = f"{package}|{version}|{source}|{url}|{arch}|{dpi}|{' '.join(apk_types)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _entry_dir(cache_base: Path, key: str) -> Path:
    return cache_base / key


def try_restore(
    cache_base: Path,
    key: str,
    dest: Path,
    package_name: str,
    version_code: str | None = None,
) -> bool:
    """Try to restore a cached APK. Returns True on hit.

    Validates with aapt if available: package name must match.
    Version code check is optional (some sources don't expose it).
    A stale/corrupt cache is silently discarded.
    """
    entry = _entry_dir(cache_base, key)
    cached_apk = entry / "stock.apk"
    if not cached_apk.exists():
        return False

    # Basic integrity: file must be non-empty and a valid zip
    if cached_apk.stat().st_size == 0:
        cached_apk.unlink(missing_ok=True)
        return False
    if not _is_valid_apk(cached_apk):
        cached_apk.unlink(missing_ok=True)
        return False

    # Validate package name via aapt if available
    actual_pkg = _aapt_package_name(cached_apk)
    if actual_pkg and actual_pkg != package_name:
        print(f"  cache: package mismatch ({actual_pkg} != {package_name}), discarding", flush=True)
        cached_apk.unlink(missing_ok=True)
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cached_apk, dest)
    print(f"  cache: restored {cached_apk.stat().st_size} bytes from cache (key={key})", flush=True)
    return True


def store(
    cache_base: Path,
    key: str,
    src: Path,
    meta: dict,
) -> None:
    """Store a downloaded APK in the cache."""
    entry = _entry_dir(cache_base, key)
    entry.mkdir(parents=True, exist_ok=True)
    dest = entry / "stock.apk"
    shutil.copy2(src, dest)
    (entry / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _is_valid_apk(path: Path) -> bool:
    """Check if file starts with PK zip signature."""
    try:
        with path.open("rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


def _aapt_package_name(apk: Path) -> str | None:
    """Extract package name via aapt if available."""
    try:
        import subprocess

        result = subprocess.run(
            ["aapt", "dump", "badging", str(apk)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("package: name='"):
                return line.split("'")[1]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None
