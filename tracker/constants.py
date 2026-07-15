from __future__ import annotations

from pathlib import Path
import re


def update_app_target_version(
    constants_file: Path,
    constant_name: str,
    new_version: str,
    version_code: str | None = None,
    apk_file_type: str | None = None,
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

    updated_prefix = match.group(1)
    type_changed = 0
    if apk_file_type:
        normalized_type = apk_file_type.strip().upper()
        if normalized_type not in {"APK", "APKM", "APKS", "XAPK"}:
            raise ValueError(f"Unsupported ApkFileType for {constant_name}: {apk_file_type}")
        updated_prefix, type_changed = update_apk_file_type(updated_prefix, normalized_type)

    if not version_changed:
        raise ValueError(f"Could not find AppTarget version for {constant_name}")
    if updated_body == target_body and not type_changed:
        return False
    updated = text[: match.start(1)] + updated_prefix + updated_body + text[match.end(2) :]
    if apk_file_type:
        updated = ensure_apk_file_type_import(updated)
    constants_file.write_text(updated, encoding="utf-8")
    return True


def update_apk_file_type(prefix: str, apk_file_type: str) -> tuple[str, int]:
    replacement = f"apkFileType = ApkFileType.{apk_file_type}"
    if re.search(r"apkFileType\s*=", prefix):
        updated, changed = re.subn(
            r"apkFileType\s*=\s*ApkFileType\.[A-Za-z0-9_]+",
            replacement,
            prefix,
            count=1,
        )
        return updated, changed if updated != prefix else 0

    target_match = re.search(r"(\n[ \t]*)targets\s*=", prefix)
    if not target_match:
        return prefix, 0
    indent = target_match.group(1)
    return prefix[: target_match.start()] + f"{indent}{replacement}," + prefix[target_match.start() :], 1


def ensure_apk_file_type_import(text: str) -> str:
    if "import app.morphe.patcher.patch.ApkFileType" in text:
        return text
    marker = "import app.morphe.patcher.patch.AppTarget"
    if marker in text:
        return text.replace(marker, "import app.morphe.patcher.patch.ApkFileType\n" + marker, 1)
    package_match = re.search(r"^package\s+[^\n]+\n", text, re.MULTILINE)
    if package_match:
        insert_at = package_match.end()
        return text[:insert_at] + "\nimport app.morphe.patcher.patch.ApkFileType\n" + text[insert_at:]
    return "import app.morphe.patcher.patch.ApkFileType\n" + text


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
