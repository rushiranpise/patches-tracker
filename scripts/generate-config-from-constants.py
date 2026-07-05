#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path
import tomllib


GENERATED_APP_KEYS = {
    "enabled",
    "app-name",
    "package-name",
    "constant",
    "current-version",
    "version",
    "arch",
    "dpi",
    "apk-types",
    "apkcombo-dlurl",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--constants", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--patches-repo", default="rushiranpise/morphe-patches")
    parser.add_argument("--constants-path", default="patches/src/main/kotlin/app/template/patches/shared/Constants.kt")
    parser.add_argument("--target-branch", default="dev")
    args = parser.parse_args()

    apps = parse_constants(args.constants.read_text(encoding="utf-8"))
    existing = read_existing_config(args.output)
    args.output.write_text(render_config(apps, args, existing), encoding="utf-8")
    print(f"Wrote {len(apps)} apps to {args.output}")
    return 0


def parse_constants(text: str) -> list[dict[str, str]]:
    apps = []
    for match in re.finditer(r"val\s+([A-Z0-9_]+_COMPATIBILITY)\s*=\s*Compatibility\s*\(", text):
        constant = match.group(1)
        body = read_call_body(text, match.end() - 1)
        name = read_string_arg(body, "name")
        package_name = read_string_arg(body, "packageName")
        version = read_target_version(body)
        if not name or not package_name or not version:
            continue
        apps.append(
            {
                "id": slugify(name, package_name),
                "name": name,
                "package_name": package_name,
                "constant": constant,
                "current_version": version,
            }
        )
    return dedupe_ids(apps)


def read_call_body(text: str, open_paren: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    for index in range(open_paren, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : index]
    return ""


def read_string_arg(body: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}\s*=\s*\"([^\"]+)\"", body)
    return match.group(1) if match else ""


def read_target_version(body: str) -> str:
    match = re.search(r"AppTarget\s*\([^)]*\bversion\s*=\s*\"([^\"]+)\"", body, re.DOTALL)
    return match.group(1) if match else ""


def slugify(name: str, package_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or package_name.rsplit(".", 1)[-1].lower()


def dedupe_ids(apps: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = {}
    for app in apps:
        base = app["id"]
        if base not in seen:
            seen[base] = 1
            continue
        seen[base] += 1
        app["id"] = f"{base}-{seen[base]}"
    return apps


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_existing_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def render_config(apps: list[dict[str, str]], args: argparse.Namespace, existing: dict | None = None) -> str:
    existing = existing or {}
    lines = [
        "# Generated from Constants.kt. Do not hand-edit this file.",
        "",
        "[tracker]",
        f"patches_repo = {quote(args.patches_repo)}",
        f"constants_path = {quote(args.constants_path)}",
        'release_prefix = "tracker"',
        'work_dir = ".work"',
        f"target_branch = {quote(args.target_branch)}",
        "",
        "[cli]",
        'repo = "MorpheApp/morphe-cli"',
        'asset_regex = ".*\\\\.jar$"',
        "prerelease = false",
        "",
        "[patches]",
        f"repo = {quote(args.patches_repo)}",
        'asset_regex = ".*\\\\.(mpp|rvp|jar)$"',
        "prerelease = false",
    ]

    for app in apps:
        preserved = preserved_app_items(existing.get(app["id"], {}))
        lines.extend(
            [
                "",
                f"[{app['id']}]",
                "enabled = true",
                f"app-name = {quote(app['name'])}",
                f"package-name = {quote(app['package_name'])}",
                f"constant = {quote(app['constant'])}",
                f"current-version = {quote(app['current_version'])}",
                'version = "latest"',
                'arch = "all"',
                'dpi = "nodpi anydpi auto"',
                'apk-types = "apk xapk apks"',
                f"apkcombo-dlurl = {quote('https://apkcombo.com/search/' + app['package_name'] + '/')}",
            ]
        )
        for key, value in preserved:
            lines.append(f"{key} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def preserved_app_items(existing_app: object) -> list[tuple[str, object]]:
    if not isinstance(existing_app, dict):
        return []
    return [(key, value) for key, value in existing_app.items() if key not in GENERATED_APP_KEYS]


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return quote(str(value))


if __name__ == "__main__":
    raise SystemExit(main())
