from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

import requests

from .config import AppConfig, SourceConfig
from .constants import is_newer_version, normalize_suffix, version_key


RESOLVER_RETRIES = int(os.environ.get("RESOLVER_RETRIES", "1"))
RESOLVER_TIMEOUT_SECONDS = int(os.environ.get("RESOLVER_TIMEOUT_SECONDS", "120"))
PATCHER_TIMEOUT_SECONDS = int(os.environ.get("PATCHER_TIMEOUT_SECONDS", "900"))


@dataclass
class BuildResult:
    app: AppConfig
    ok: bool
    output: Path | None
    log: str
    candidate_version: str
    version_code: str | None = None
    failure_type: str | None = None


@dataclass(frozen=True)
class VersionCandidate:
    version: str
    source: SourceConfig
    source_index: int


def version_candidate_sort_key(candidate: VersionCandidate) -> tuple[tuple[int, ...], int, int]:
    return (version_key(candidate.version), normalize_suffix(candidate.version), -candidate.source_index)


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
        return BuildResult(app, False, None, "No download source is configured for this app", candidate_version, failure_type="config")

    resolver = Path("scripts") / "resolve-apk.sh"
    resolve_logs = []
    candidates = []
    resolved_latest_count = 0
    if candidate_version == "latest" and not dry_run:
        for source_index, source in enumerate(sources):
            print(f"[{app.id}] resolving latest via {source.source}: {source.url}", flush=True)
            latest = run_resolver(
                app.id,
                "latest resolve",
                ["bash", str(resolver), "latest", source.source, source.url],
            )
            if latest.stdout.strip() and (latest.returncode == 0 or looks_stdout_with_noisy_exit_usable(latest.stdout, latest.stderr)):
                resolved_latest_count += 1
                latest_version = latest.stdout.strip().splitlines()[0]
                print(f"[{app.id}] latest version from {source.source}: {latest_version}", flush=True)
                if is_newer_version(latest_version, app.current_version):
                    candidates.append(VersionCandidate(latest_version, source, source_index))
                else:
                    print(
                        f"[{app.id}] {source.source} is not newer than current {app.current_version}; skipping {latest_version}",
                        flush=True,
                    )
                continue
            source_log = latest.stdout + latest.stderr
            print(f"[{app.id}] could not get latest version from {source.source}", flush=True)
            resolve_logs.append(f"[{source.source}] {source_log}")
        if not candidates and resolved_latest_count == 0 and resolve_logs:
            return BuildResult(app, False, None, "\n".join(resolve_logs), candidate_version, failure_type="version_resolve")
        if not candidates:
            log = f"No configured source reported a version newer than {app.current_version}"
            print(f"[{app.id}] {log}", flush=True)
            return BuildResult(app, True, None, log, app.current_version)
        candidates.sort(key=version_candidate_sort_key, reverse=True)
        print(
            f"[{app.id}] newer versions to try: "
            + ", ".join(f"{candidate.version} via {candidate.source.source}" for candidate in candidates),
            flush=True,
        )
    else:
        if not is_newer_version(candidate_version, app.current_version):
            log = f"Configured version {candidate_version} is not newer than current {app.current_version}"
            print(f"[{app.id}] {log}", flush=True)
            return BuildResult(app, True, None, log, app.current_version)
        candidates = [VersionCandidate(candidate_version, source, source_index) for source_index, source in enumerate(sources)]

    candidate_version = candidates[0].version
    highest_candidate_version = candidate_version

    if dry_run:
        return BuildResult(app, True, None, "dry-run: build skipped", candidate_version)

    download_logs = []
    stock_apk = None
    output_apk = None
    for candidate in candidates:
        candidate_version = candidate.version
        source = candidate.source
        candidate_stock_apk = app_dir / f"{app.id}-{candidate_version}.apk"
        candidate_output_apk = app_dir / f"{app.id}-patched-{candidate_version}.apk"
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
                str(candidate_stock_apk),
                source.arch,
                source.dpi,
                " ".join(source.apk_types),
            ],
        )
        if resolved.returncode == 0 and candidate_stock_apk.exists():
            stock_apk = candidate_stock_apk
            output_apk = candidate_output_apk
            print(f"[{app.id}] downloaded APK via {source.source}: {stock_apk}", flush=True)
            break
        source_log = resolved.stdout + resolved.stderr
        print(f"[{app.id}] download did not work via {source.source}", flush=True)
        download_logs.append(f"[{source.source} {candidate_version}] {source_log}")
    if stock_apk is None or output_apk is None:
        return BuildResult(app, False, None, "\n".join(download_logs), highest_candidate_version, failure_type="download")

    version_code = read_version_code(stock_apk)
    print(f"[{app.id}] versionCode: {version_code or 'unknown'}", flush=True)

    args = ["java", "-jar", str(cli_jar), "patch", str(stock_apk), "-o", str(output_apk), "--patches", str(patches_file)]
    for patch in app.included_patches:
        args.extend(["-e", patch])
    for patch in app.excluded_patches:
        args.extend(["-d", patch])
    args.extend(app.patcher_args)

    print(f"[{app.id}] patch command: {shell_join(args)}", flush=True)
    completed = run_streamed_process(app.id, "patch", args, timeout_seconds=PATCHER_TIMEOUT_SECONDS)
    print(f"[{app.id}] patch return code: {completed.returncode}", flush=True)
    log = completed.stdout + completed.stderr
    if patcher_skipped_incompatible_patch(log):
        print(f"[{app.id}] patch skipped incompatible patches; treating as patch failure: {log[-1000:]}", flush=True)
        return BuildResult(app, False, None, log, candidate_version, version_code, "patch")
    if completed.returncode != 0 or not output_apk.exists():
        print(f"[{app.id}] patch did not finish successfully: {log[-1000:]}", flush=True)
        return BuildResult(app, False, None, log, candidate_version, version_code, classify_failure(log, "patch"))
    print(f"[{app.id}] patched APK ready: {output_apk}", flush=True)
    return BuildResult(app, True, output_apk, log, candidate_version, version_code)


def run_resolver(app_id: str, phase: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    attempts = max(1, RESOLVER_RETRIES)
    last = subprocess.CompletedProcess(args, 1, "", "")
    env = os.environ.copy()
    env["TEMP_DIR"] = str(Path(".work") / "resolver" / app_id)
    for attempt in range(1, attempts + 1):
        print(f"[{app_id}] {phase} attempt {attempt}/{attempts}: {shell_join(args)}", flush=True)
        completed = run_streamed_process(
            app_id,
            f"{phase} attempt {attempt}",
            args,
            timeout_seconds=RESOLVER_TIMEOUT_SECONDS,
            env=env,
        )
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


def run_streamed_process(
    app_id: str,
    phase: str,
    args: list[str],
    *,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        env=env,
    )
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def reader(stream, name: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                output_queue.put((name, line))
        finally:
            stream.close()
            output_queue.put((name, None))

    threads = [
        threading.Thread(target=reader, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=reader, args=(process.stderr, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout_seconds
    open_streams = {"stdout", "stderr"}
    timed_out = False
    while open_streams:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            print(f"[{app_id}] {phase} timed out after {timeout_seconds}s; killing process", flush=True)
            process.kill()
            break
        try:
            name, line = output_queue.get(timeout=min(1.0, remaining))
        except queue.Empty:
            continue
        if line is None:
            open_streams.discard(name)
            continue
        if name == "stdout":
            stdout_lines.append(line)
        else:
            stderr_lines.append(line)
        print(f"[{app_id}] {phase} {name}: {line.rstrip()}", flush=True)

    return_code = process.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=1)
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    if timed_out:
        stderr += f"\nTimed out after {timeout_seconds}s\n"
        return_code = return_code if return_code != 0 else 124
    return subprocess.CompletedProcess(args, return_code, stdout, stderr)


def looks_transient_block(log: str) -> bool:
    lower = log.lower()
    markers = (
        "captcha",
        "cf-chl",
        "cf-browser-verification",
        "just a moment",
        "attention required",
        "checking your browser",
        "access denied",
        "error 1020",
        "blocked page",
        "request failed",
        "timed out after",
        "turnstile",
    )
    return any(marker in lower for marker in markers)


def looks_stdout_with_noisy_exit_usable(stdout: str, stderr: str) -> bool:
    """Some HTML tools print the value, then exit nonzero when a pipe closes early."""
    first_line = stdout.strip().splitlines()[0] if stdout.strip() else ""
    if not first_line:
        return False
    lower_stderr = stderr.lower()
    return ("broken pipe" in lower_stderr or "brokenpipe" in lower_stderr) and not looks_transient_block(stderr)


def patcher_skipped_incompatible_patch(log: str) -> bool:
    lower = log.lower()
    return "warning: skipping" in lower and "incompatible with" in lower


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
