#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote as url_quote
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
    "apkmirror-dlurl",
    "uptodown-dlurl",
    "apkpure-dlurl",
    "apkcombo-dlurl",
    "gplay-dlurl",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--constants", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--patches-repo", default="rushiranpise/morphe-patches")
    parser.add_argument("--constants-path", default="patches/src/main/kotlin/app/template/patches/shared/Constants.kt")
    parser.add_argument("--target-branch", default="dev")
    parser.add_argument("--source-workers", type=int, default=2)
    parser.add_argument("--source-timeout", type=int, default=45)
    parser.add_argument("--max-source-checks", type=int, default=30)
    parser.add_argument("--no-resolve-source-urls", action="store_true")
    args = parser.parse_args()

    apps = parse_constants(args.constants.read_text(encoding="utf-8"))
    existing = read_existing_config(args.output)
    if args.no_resolve_source_urls:
        seed_existing_source_urls(apps, existing)
    else:
        resolve_source_urls(apps, existing, args.source_workers, args.source_timeout, args.max_source_checks)
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
        apk_types = apk_types_from_apk_file_type(read_apk_file_type(body))
        if not name or not package_name or not version:
            continue
        apps.append(
            {
                "id": slugify(name, package_name),
                "name": name,
                "package_name": package_name,
                "constant": constant,
                "current_version": version,
                "apk_types": apk_types,
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


def read_apk_file_type(body: str) -> str:
    match = re.search(r"apkFileType\s*=\s*ApkFileType\.([A-Za-z0-9_]+)", body)
    return match.group(1).upper() if match else ""


def apk_types_from_apk_file_type(apk_file_type: str) -> str:
    return {
        "APK": "apk",
        "XAPK": "xapk",
        "APKS": "apks",
        "APKM": "apkm",
        "BUNDLE": "xapk apks apkm",
        "SPLIT": "xapk apks apkm",
        "SPLITS": "xapk apks apkm",
    }.get(apk_file_type, "")


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
        "parallel_jobs = 4",
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
                f"enabled = {toml_value(existing_app_enabled(existing.get(app['id'], {})))}",
                f"app-name = {quote(app['name'])}",
                f"package-name = {quote(app['package_name'])}",
                f"constant = {quote(app['constant'])}",
                f"current-version = {quote(app['current_version'])}",
                'version = "latest"',
                'arch = "all"',
                'dpi = "nodpi anydpi auto"',
                f"apk-types = {quote(app.get('apk_types') or 'apk xapk apks')}",
            ]
        )
        for key in ("apkmirror-dlurl", "uptodown-dlurl", "apkpure-dlurl"):
            if app.get(key):
                lines.append(f"{key} = {quote(app[key])}")
        apkcombo_url = app.get("apkcombo-dlurl") or "https://apkcombo.com/search/" + app["package_name"] + "/"
        lines.append(f"apkcombo-dlurl = {quote(apkcombo_url)}")
        gplay_url = app.get("gplay-dlurl") or "https://play.google.com/store/apps/details?id=" + app["package_name"]
        lines.append(f"gplay-dlurl = {quote(gplay_url)}")
        for key, value in preserved:
            lines.append(f"{key} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def preserved_app_items(existing_app: object) -> list[tuple[str, object]]:
    if not isinstance(existing_app, dict):
        return []
    return [(key, value) for key, value in existing_app.items() if key not in GENERATED_APP_KEYS]


def existing_app_enabled(existing_app: object) -> bool:
    if not isinstance(existing_app, dict):
        return True
    value = existing_app.get("enabled", True)
    return value if isinstance(value, bool) else True


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return quote(str(value))


def resolve_source_urls(apps: list[dict[str, str]], existing: dict, workers: int, timeout: int, max_checks: int) -> None:
    workers = max(1, workers)
    seed_existing_source_urls(apps, existing)
    pending_checks = source_checks_to_run(apps, existing, max_checks)
    print(f"Source discovery checks this run: {len(pending_checks)}")
    if not pending_checks:
        return
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_app = {
            executor.submit(resolve_app_source_urls, app, existing.get(app["id"], {}), timeout, keys): app
            for app, keys in pending_checks
        }
        for future in as_completed(future_to_app):
            app = future_to_app[future]
            try:
                app.update(future.result())
            except Exception as error:
                print(f"[{app['id']}] source discovery failed: {error}")


def seed_existing_source_urls(apps: list[dict[str, str]], existing: dict, *, final_only: bool = True) -> None:
    for app in apps:
        existing_app = existing.get(app["id"], {})
        if not isinstance(existing_app, dict):
            continue
        for key in ("apkmirror-dlurl", "uptodown-dlurl", "apkpure-dlurl", "apkcombo-dlurl", "gplay-dlurl"):
            existing_url = existing_app.get(key, "")
            if isinstance(existing_url, str) and existing_url and (not final_only or is_final_source_url(key, existing_url, app.get("package_name", ""))):
                app[key] = str(existing_url)


def source_checks_to_run(apps: list[dict[str, str]], existing: dict, max_checks: int) -> list[tuple[dict[str, str], list[str]]]:
    unlimited = max_checks <= 0
    pending = []
    remaining = max_checks
    source_keys = ("apkmirror-dlurl", "uptodown-dlurl", "apkpure-dlurl")
    for app in apps:
        existing_app = existing.get(app["id"], {})
        if not isinstance(existing_app, dict):
            existing_app = {}
        keys = []
        for key in source_keys:
            if is_final_source_url(key, existing_app.get(key, ""), app.get("package_name", "")):
                if not app.get("apk_types") and not has_specific_apk_types(existing_app.get("apk-types", "")):
                    keys.append(key)
                continue
            keys.append(key)
        if not keys:
            continue
        if not unlimited:
            keys = keys[:remaining]
        pending.append((app, keys))
        if unlimited:
            continue
        remaining -= len(keys)
        if remaining <= 0:
            break
    return pending


def resolve_app_source_urls(app: dict[str, str], existing_app: object, timeout: int, source_keys: list[str]) -> dict[str, str]:
    import requests

    package_name = app["package_name"]
    existing = existing_app if isinstance(existing_app, dict) else {}
    resolved = {}
    sources = {
        "apkmirror-dlurl": resolve_apkmirror_url,
        "uptodown-dlurl": resolve_uptodown_url,
        "apkpure-dlurl": resolve_apkpure_url,
    }
    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:108.0) Gecko/20100101 Firefox/108.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        for key in source_keys:
            resolver = sources[key]
            existing_url = existing.get(key, "")
            if is_final_source_url(key, existing_url, package_name):
                valid, apk_types = validate_existing_source_url(key, str(existing_url), package_name, session, timeout)
                if valid:
                    resolved[key] = str(existing_url)
                    if apk_types and not app.get("apk_types") and not has_specific_apk_types(existing.get("apk-types", "")):
                        resolved["apk_types"] = apk_types
                    print(f"[{app['id']}] kept existing {key}: {resolved[key]}")
                    break
                print(f"[{app['id']}] existing {key} did not match package {package_name}; resolving again: {existing_url}")
            elif isinstance(existing_url, str) and existing_url:
                print(f"[{app['id']}] dropping non-final {key}: {existing_url}")
            try:
                url, apk_types = resolver(package_name, session, timeout)
            except requests.RequestException as error:
                print(f"[{app['id']}] could not check {key}: {error}")
                url = ""
                apk_types = ""
            if url:
                resolved[key] = url
                if apk_types and not app.get("apk_types"):
                    resolved["apk_types"] = apk_types
                print(f"[{app['id']}] found {key}: {url}")
                break
            else:
                print(f"[{app['id']}] no usable {key} found")
    return resolved


def resolve_apkmirror_url(package_name: str, session, timeout: int) -> tuple[str, str]:
    import requests

    search_url = apkmirror_search_url(package_name)
    html = fetch_text(session, search_url, timeout)
    results = re.search(r'"arch_results":"(\d+)"', html)
    if results and results.group(1) == "0":
        return "", ""
    for path in unique(re.findall(r'href=["\'](/apk/[^"\']+?/[^"\']+?/)', html)):
        app_url = "https://www.apkmirror.com" + path
        try:
            app_html = fetch_text(session, app_url, timeout)
        except requests.RequestException:
            continue
        found = re.search(r'id=([^"\s]+)" class="accent_color', app_html)
        if found and found.group(1) == package_name:
            return app_url, infer_apkmirror_apk_types(app_html)
    return "", ""


def resolve_uptodown_url(package_name: str, session, timeout: int) -> tuple[str, str]:
    import requests

    search_url = uptodown_search_url(package_name)
    html = fetch_text(session, search_url, timeout)
    for app_url in unique(re.findall(r'https://[a-z0-9-]+\.en\.uptodown\.com/android', html)):
        try:
            download_html = fetch_text(session, app_url.rstrip("/") + "/download", timeout)
        except requests.RequestException:
            continue
        if re.search(rf">\s*{re.escape(package_name)}\s*<", download_html) or package_name in download_html:
            return app_url.rstrip("/"), infer_uptodown_apk_types(download_html)
    return "", ""


def resolve_apkpure_url(package_name: str, session, timeout: int) -> tuple[str, str]:
    info_url = apkpure_info_url(package_name)
    response = fetch_response(session, info_url, timeout)
    final_url = response.url.rstrip("/")
    if "/apk-info/" in final_url or package_name not in final_url:
        final_url = apkpure_app_url_from_html(response.text, package_name) or final_url
    if "/apk-info/" in final_url or package_name not in final_url:
        return "", ""
    return final_url, infer_apkpure_apk_types(response.text)


def validate_existing_source_url(key: str, url: str, package_name: str, session, timeout: int) -> tuple[bool, str]:
    try:
        html = fetch_text(session, url.rstrip("/") + "/download" if key == "uptodown-dlurl" else url, timeout)
    except Exception as error:
        print(f"Could not validate {url}: {error}")
        return True, ""
    if key == "apkmirror-dlurl":
        return apkmirror_page_matches_package(html, package_name), infer_apkmirror_apk_types(html)
    if key == "uptodown-dlurl":
        return package_name in html, infer_uptodown_apk_types(html)
    if key == "apkpure-dlurl":
        return apkpure_url_matches_package(url, package_name) or package_name in html, infer_apkpure_apk_types(html)
    return True, ""


def apkmirror_page_matches_package(html: str, package_name: str) -> bool:
    found = re.search(r'id=([^"\s]+)" class="accent_color', html)
    return bool(found and found.group(1) == package_name)


def has_specific_apk_types(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.split()).lower()
    return bool(normalized and normalized != "apk xapk apks")


def infer_apkmirror_apk_types(html: str) -> str:
    if re.search(r"\bAPK\s+BUNDLE\b|\bBUNDLE\b", html, re.I):
        return "xapk apks apkm"
    if re.search(r">\s*APK\s*<|\bAPK\b", html, re.I):
        return "apk"
    return ""


def infer_uptodown_apk_types(html: str) -> str:
    if re.search(r"\bxapk\b", html, re.I):
        return "xapk"
    if re.search(r"\bapk\b", html, re.I):
        return "apk"
    return ""


def infer_apkpure_apk_types(html: str) -> str:
    if re.search(r"\bxapk\b", html, re.I):
        return "xapk"
    if re.search(r"\bapk\b", html, re.I):
        return "apk"
    return ""


def apkpure_app_url_from_html(html: str, package_name: str) -> str:
    patterns = [
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)',
        rf'https://apkpure\.com/[^"\'<>\s]+/{re.escape(package_name)}',
        rf'href=["\']([^"\']+/{re.escape(package_name)})["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if not match:
            continue
        url = match.group(1)
        if url.startswith("/"):
            url = "https://apkpure.com" + url
        if "/apk-info/" not in url:
            return url.rstrip("/")
    return ""


def fetch_response(session, url: str, timeout: int):
    response = session.get(url, timeout=timeout, allow_redirects=True)
    if response.status_code in {403, 429, 503}:
        flaresolverr = fetch_with_flaresolverr(url, timeout)
        if flaresolverr:
            return StaticResponse(flaresolverr[0], flaresolverr[1])
    response.raise_for_status()
    return response


def fetch_text(session, url: str, timeout: int) -> str:
    return fetch_response(session, url, timeout).text


def fetch_with_flaresolverr(url: str, timeout: int) -> tuple[str, str] | None:
    import requests

    flaresolverr_url = os.environ.get("FLARESOLVERR_URL")
    if not flaresolverr_url:
        return None
    for attempt in range(1, 3):
        try:
            response = requests.post(
                flaresolverr_url.rstrip("/") + "/v1",
                json={"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000},
                timeout=timeout + 15,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "ok":
                print(f"FlareSolverr returned status {payload.get('status')!r} for {url} on attempt {attempt}/2")
                continue
            solution = payload.get("solution") or {}
            html = solution.get("response") or ""
            final_url = solution.get("url") or url
            if looks_blocked_page(html):
                print(f"FlareSolverr returned blocked page for {url} on attempt {attempt}/2")
                continue
            return final_url, html
        except requests.RequestException as error:
            print(f"FlareSolverr request failed for {url} on attempt {attempt}/2: {error}")
    return None


def looks_blocked_page(html: str) -> bool:
    return bool(re.search(r"cf-chl|just a moment|checking your browser|access denied|error 1020", html, re.I))


class StaticResponse:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text


def unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def is_final_source_url(key: str, url: object, package_name: str = "") -> bool:
    if not isinstance(url, str) or not url:
        return False
    if key == "apkmirror-dlurl":
        return "apkmirror.com/apk/" in url and "?" not in url
    if key == "uptodown-dlurl":
        return ".en.uptodown.com/android" in url and "/search" not in url
    if key == "apkpure-dlurl":
        return "apkpure.com/" in url and "/apk-info/" not in url and apkpure_url_matches_package(url, package_name)
    return False


def apkpure_url_matches_package(url: str, package_name: str) -> bool:
    if not package_name:
        return True
    return url.rstrip("/").endswith("/" + package_name)


def apkmirror_search_url(package_name: str) -> str:
    return (
        "https://www.apkmirror.com/?post_type=app_release&searchtype=app&sortby=date&sort=desc&s="
        + url_quote(package_name)
    )


def uptodown_search_url(package_name: str) -> str:
    return "https://en.uptodown.com/android/search?query=" + url_quote(package_name)


def apkpure_info_url(package_name: str) -> str:
    return "https://apkpure.com/apk-info/" + url_quote(package_name)


if __name__ == "__main__":
    raise SystemExit(main())
