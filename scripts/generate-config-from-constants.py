#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


SOURCE_ORDER = ("apkcombo",)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--constants", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--patches-repo", default="rushiranpise/morphe-patches")
    parser.add_argument("--constants-path", default="patches/src/main/kotlin/app/template/patches/shared/Constants.kt")
    parser.add_argument("--target-branch", default="dev")
    args = parser.parse_args()

    apps = parse_constants(args.constants.read_text(encoding="utf-8"))
    args.output.write_text(render_config(apps, args), encoding="utf-8")
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


def render_config(apps: list[dict[str, str]], args: argparse.Namespace) -> str:
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
                f"apkcombo-dlurl = {quote('https://apkcombo.com/search/' + app['package_name'] + '/')}",
            ]
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
