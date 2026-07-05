from __future__ import annotations

from pathlib import Path
import re


def update_app_target_version(
    constants_file: Path,
    constant_name: str,
    new_version: str,
    version_code: str | None = None,
) -> bool:
    text = constants_file.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(val\s+{re.escape(constant_name)}\s*=\s*Compatibility\([\s\S]*?targets\s*=\s*listOf\(\s*AppTarget\()([\s\S]*?)(\)\s*\))",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        raise ValueError(f"Could not find AppTarget version for {constant_name}")
    target_body = match.group(2)
    updated_body, version_changed = re.subn(
        r'version\s*=\s*"[^"]+"',
        f'version = "{new_version}"',
        target_body,
        count=1,
    )
    if version_code:
        if re.search(r"versionCode\s*=", updated_body):
            updated_body, code_changed = re.subn(
                r"versionCode\s*=\s*\d+",
                f"versionCode = {version_code}",
                updated_body,
                count=1,
            )
        else:
            updated_body = updated_body.rstrip() + f", versionCode = {version_code}"
            code_changed = 1
    else:
        code_changed = 0

    if not version_changed:
        raise ValueError(f"Could not find AppTarget version for {constant_name}")
    if updated_body == target_body:
        return False
    updated = text[: match.start(2)] + updated_body + text[match.end(2) :]
    constants_file.write_text(updated, encoding="utf-8")
    return True


def is_newer_version(candidate: str, current: str) -> bool:
    candidate_key = version_key(candidate)
    current_key = version_key(current)
    if candidate_key != current_key:
        return candidate_key > current_key
    return normalize_suffix(candidate) > normalize_suffix(current)


def version_key(version: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", version)]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def normalize_suffix(version: str) -> int:
    lower = version.lower()
    if any(marker in lower for marker in ("alpha", "beta", "rc", "preview")):
        return -1
    return 0
