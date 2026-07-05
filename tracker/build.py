from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import time

import requests

from .config import AppConfig


RESOLVER_RETRIES = int(os.environ.get("RESOLVER_RETRIES", "3"))


@dataclass
class BuildResult:
    app: AppConfig
    ok: bool
    output: Path | None
    log: str
    candidate_version: str
    version_code: str | None = None
    failure_type: str | None = None


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    return dest


def build_app(app: AppConfig, cli_jar: Path, patches_file: Path, work_dir: Path, *, dry_run: bool = False) -> BuildResult:
    app_dir = work_dir / app.id
    app_dir.mkdir(parents=True, exist_ok=True)
    candidate_version = app.candidate_version
    sources = app.resolved_sources()
    if not sources:
        return BuildResult(app, False, None, "No app url configured", candidate_version, failure_type="config")

    resolver = Path("scripts") / "resolve-apk.sh"
    source_used = None
    resolve_logs = []
    if candidate_version == "latest" and not dry_run:
        for source in sources:
            print(f"[{app.id}] resolving latest via {source.source}: {source.url}", flush=True)
            latest = run_resolver(
                app.id,
                "latest resolve",
                ["bash", str(resolver), "latest", source.source, source.url],
            )
            if latest.returncode == 0 and latest.stdout.strip():
                candidate_version = latest.stdout.strip().splitlines()[0]
                source_used = source
                print(f"[{app.id}] latest version from {source.source}: {candidate_version}", flush=True)
                break
            source_log = latest.stdout + latest.stderr
            print(f"[{app.id}] latest resolve failed via {source.source}", flush=True)
            resolve_logs.append(f"[{source.source}] {source_log}")
        if source_used is None:
            return BuildResult(app, False, None, "\n".join(resolve_logs), candidate_version, failure_type="version_resolve")
    elif sources:
        source_used = sources[0]

    stock_apk = app_dir / f"{app.id}-{candidate_version}.apk"
    output_apk = app_dir / f"{app.id}-patched-{candidate_version}.apk"

    if dry_run:
        return BuildResult(app, True, None, "dry-run: build skipped", candidate_version)

    download_logs = []
    download_sources = [source_used] if source_used else sources
    if source_used and len(sources) > 1:
        download_sources.extend(source for source in sources if source != source_used)
    for source in download_sources:
        if source is None:
            continue
        print(f"[{app.id}] downloading {candidate_version} via {source.source}: {source.url}", flush=True)
        resolved = run_resolver(
            app.id,
            "download",
            [
                "bash",
                str(resolver),
                source.source,
                source.url,
                candidate_version,
                str(stock_apk),
                source.arch,
                source.dpi,
                " ".join(source.apk_types),
            ],
        )
        if resolved.returncode == 0 and stock_apk.exists():
            source_used = source
            print(f"[{app.id}] downloaded APK via {source.source}: {stock_apk}", flush=True)
            break
        source_log = resolved.stdout + resolved.stderr
        print(f"[{app.id}] download failed via {source.source}", flush=True)
        download_logs.append(f"[{source.source}] {source_log}")
    if not stock_apk.exists():
        return BuildResult(app, False, None, "\n".join(download_logs), candidate_version, failure_type="download")

    version_code = read_version_code(stock_apk)
    print(f"[{app.id}] versionCode: {version_code or 'unknown'}", flush=True)

    args = ["java", "-jar", str(cli_jar), "patch", str(stock_apk), "-o", str(output_apk), "--patches", str(patches_file)]
    for patch in app.included_patches:
        args.extend(["-e", patch])
    for patch in app.excluded_patches:
        args.extend(["-d", patch])
    args.extend(app.patcher_args)

    completed = subprocess.run(args, text=True, capture_output=True)
    log = completed.stdout + completed.stderr
    print_process_log(app.id, "patch", completed.stdout, completed.stderr)
    if completed.returncode != 0 or not output_apk.exists():
        print(f"[{app.id}] patch failed: {log[-1000:]}", flush=True)
        return BuildResult(app, False, None, log, candidate_version, version_code, classify_failure(log, "patch"))
    print(f"[{app.id}] patch succeeded: {output_apk}", flush=True)
    return BuildResult(app, True, output_apk, log, candidate_version, version_code)


def run_resolver(app_id: str, phase: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    attempts = max(1, RESOLVER_RETRIES)
    last = subprocess.CompletedProcess(args, 1, "", "")
    for attempt in range(1, attempts + 1):
        print(f"[{app_id}] {phase} attempt {attempt}/{attempts}: {shell_join(args)}", flush=True)
        completed = subprocess.run(args, text=True, capture_output=True)
        print_process_log(app_id, f"{phase} attempt {attempt}", completed.stdout, completed.stderr)
        if completed.returncode == 0:
            return completed
        last = completed
        if attempt < attempts and looks_transient_block(completed.stdout + completed.stderr):
            wait_seconds = attempt * 15
            print(f"[{app_id}] {phase} looks blocked/transient; retrying in {wait_seconds}s", flush=True)
            time.sleep(wait_seconds)
            continue
        return completed
    return last


def print_process_log(app_id: str, phase: str, stdout: str, stderr: str) -> None:
    if stdout:
        print(f"[{app_id}] {phase} stdout:\n{stdout.rstrip()}", flush=True)
    if stderr:
        print(f"[{app_id}] {phase} stderr:\n{stderr.rstrip()}", flush=True)


def looks_transient_block(log: str) -> bool:
    lower = log.lower()
    markers = (
        "captcha",
        "cloudflare",
        "cf-chl",
        "just a moment",
        "attention required",
        "checking your browser",
        "access denied",
        "forbidden",
        "blocked page",
        "could not find apk link",
        "could not find download link",
        "did not expose",
        "request failed",
    )
    return any(marker in lower for marker in markers)


def shell_join(args: list[str]) -> str:
    return " ".join(subprocess.list2cmdline([arg]) for arg in args)


def prepare_tool(url: str, path: Path, *, dry_run: bool = False) -> Path:
    if dry_run:
        return path
    if path.exists():
        return path
    if url.startswith("file://"):
        src = Path(url.removeprefix("file://"))
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, path)
        return path
    return download(url, path)


def read_version_code(apk: Path) -> str | None:
    completed = subprocess.run(["aapt", "dump", "badging", str(apk)], text=True, capture_output=True)
    if completed.returncode != 0:
        return None
    first = completed.stdout.splitlines()[0] if completed.stdout else ""
    marker = "versionCode='"
    if marker not in first:
        return None
    return first.split(marker, 1)[1].split("'", 1)[0]


def classify_failure(log: str, default: str) -> str:
    lower = log.lower()
    if "fingerprint" in lower or "failed to resolve" in lower or ("not found" in lower and "patch" in lower):
        return "fingerprint"
    if "sign" in lower or "keystore" in lower or "apksigner" in lower:
        return "signing"
    if "download" in lower or "http" in lower or "cloudflare" in lower:
        return "download"
    return default
