from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time

import requests

from .cache import cache_key, store, try_restore
from .config import AppConfig
from .constants import is_newer_version, update_app_target_version
from .github import known_patch_failure_exists


RESOLVER_RETRIES = int(os.environ.get("RESOLVER_RETRIES", "1"))
RESOLVER_TIMEOUT_SECONDS = int(os.environ.get("RESOLVER_TIMEOUT_SECONDS", "300"))
PATCHER_TIMEOUT_SECONDS = int(os.environ.get("PATCHER_TIMEOUT_SECONDS", "900"))
COMPARABLE_LATEST_SOURCES = {"direct", "github", "github-release", "archive", "aoneroom", "apkmirror", "uptodown", "apkpure", "apkcombo"}
DOWNLOAD_FALLBACK_SOURCES = {"gplay"}


@dataclass
class BuildResult:
    app: AppConfig
    ok: bool
    output: Path | None
    log: str
    candidate_version: str
    version_code: str | None = None
    failure_type: str | None = None
    status: str = "failed"
    downloaded: bool = False
    source_name: str | None = None
    source_url: str | None = None
    apk_file_type: str | None = None


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    return dest


def build_app(
    app: AppConfig,
    cli_jar: Path,
    patches_file: Path,
    work_dir: Path,
    *,
    patches_repo: str,
    target_branch: str,
    constants_path: str,
    dry_run: bool = False,
    ignore_known_failures: bool = False,
    continue_on_error: bool = False,
    cache_dir: Path | None = None,
) -> BuildResult:
    app_dir = work_dir / app.id
    app_dir.mkdir(parents=True, exist_ok=True)
    candidate_version = app.candidate_version
    sources = app.resolved_sources()
    if not sources:
        return BuildResult(app, False, None, "No download source is configured for this app", candidate_version, failure_type="config")

    resolver = Path("scripts") / "resolve-apk.sh"
    resolve_logs = []
    download_logs = []
    resolved_latest_count = 0
    stock_apk = None
    output_apk = None
    source = None
    apk_file_type = None
    highest_candidate_version = candidate_version

    if candidate_version == "latest" and not dry_run:
        latest_candidates: list[tuple[str, SourceConfig]] = []
        comparable_sources = [source for source in sources if source.source in COMPARABLE_LATEST_SOURCES]
        fallback_sources = [source for source in sources if source.source in DOWNLOAD_FALLBACK_SOURCES]
        skipped_latest_sources = [source for source in sources if source.source not in COMPARABLE_LATEST_SOURCES]
        for skipped in skipped_latest_sources:
            print(
                f"[{app.id}] skipping latest resolve via {skipped.source}; source is download-only for known versions",
                flush=True,
            )
        for source in comparable_sources:
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
                    latest_candidates.append((latest_version, source))
                    if highest_candidate_version == "latest" or is_newer_version(latest_version, highest_candidate_version):
                        highest_candidate_version = latest_version
                else:
                    print(
                        f"[{app.id}] {source.source} is not newer than current {app.current_version}; skipping {latest_version}",
                        flush=True,
                    )
                continue
            source_log = latest.stdout + latest.stderr
            print(f"[{app.id}] could not get latest version from {source.source}", flush=True)
            resolve_logs.append(f"[{source.source}] {source_log}")

        if latest_candidates:
            candidate_version = highest_candidate_version
            if not ignore_known_failures and known_patch_failure_exists(patches_repo, app.name, candidate_version):
                log = f"Skipping {candidate_version}; already reported as patch-broken in {patches_repo}"
                print(f"[{app.id}] {log}", flush=True)
                return BuildResult(app, True, None, log, candidate_version, status="skipped_known_broken")
            print(
                f"[{app.id}] selected highest newer version {candidate_version}; "
                f"sources: {', '.join(src.source for version, src in latest_candidates if version == candidate_version)}",
                flush=True,
            )
            download_sources = [src for version, src in latest_candidates if version == candidate_version]
            download_sources.extend(fallback_sources)
            seen_download_sources = set()
            unique_download_sources = []
            for candidate_source in download_sources:
                source_key = (candidate_source.source, candidate_source.url)
                if source_key in seen_download_sources:
                    continue
                seen_download_sources.add(source_key)
                unique_download_sources.append(candidate_source)
            for source in unique_download_sources:
                if source.source in DOWNLOAD_FALLBACK_SOURCES:
                    print(
                        f"[{app.id}] trying download-only fallback {source.source} for selected version {candidate_version}",
                        flush=True,
                    )
                candidate_stock_apk = app_dir / f"{app.id}-{candidate_version}.apk"
                candidate_output_apk = app_dir / f"{app.id}-patched-{candidate_version}.apk"
                clean_download_target(candidate_stock_apk)
                # Try cache restore before downloading
                ckey = cache_key(app.package_name, candidate_version, source.source, source.url, source.arch, source.dpi, source.apk_types)
                cache_hit = False
                if cache_dir is not None and not dry_run:
                    cache_hit = try_restore(cache_dir, ckey, candidate_stock_apk, app.package_name)
                if cache_hit:
                    stock_apk = candidate_stock_apk
                    output_apk = candidate_output_apk
                    apk_file_type = "APK"  # cached APK is already merged
                    print(f"[{app.id}] restored from cache via {source.source}; skipping lower-priority sources", flush=True)
                    break
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
                    apk_file_type = apk_file_type_from_resolver_log(resolved.stdout + resolved.stderr, source.source, source.apk_types)
                    # Store in cache for future runs
                    if cache_dir is not None:
                        store(cache_dir, ckey, candidate_stock_apk, {
                            "package": app.package_name,
                            "version": candidate_version,
                            "source": source.source,
                            "url": source.url,
                            "arch": source.arch,
                            "dpi": source.dpi,
                            "apk_types": source.apk_types,
                        })
                    print(f"[{app.id}] downloaded APK via {source.source}: {stock_apk}; skipping lower-priority sources", flush=True)
                    break
                source_log = resolved.stdout + resolved.stderr
                print(f"[{app.id}] download did not work via {source.source}; trying next source", flush=True)
                download_logs.append(f"[{source.source} {candidate_version}] {source_log}")
        if stock_apk is None or output_apk is None:
            if download_logs:
                return BuildResult(app, False, None, "\n".join(download_logs), highest_candidate_version, failure_type="download")
            if resolved_latest_count == 0 and resolve_logs:
                return BuildResult(app, False, None, "\n".join(resolve_logs), candidate_version, failure_type="version_resolve")
            log = f"No configured source reported a version newer than {app.current_version}"
            print(f"[{app.id}] {log}", flush=True)
            return BuildResult(app, True, None, log, app.current_version, status="no_update")
    else:
        if not is_newer_version(candidate_version, app.current_version):
            log = f"Configured version {candidate_version} is not newer than current {app.current_version}"
            print(f"[{app.id}] {log}", flush=True)
            return BuildResult(app, True, None, log, app.current_version, status="no_update")

        if dry_run:
            return BuildResult(app, True, None, "dry-run: build skipped", candidate_version, status="dry_run")

        highest_candidate_version = candidate_version
        if not ignore_known_failures and known_patch_failure_exists(patches_repo, app.name, candidate_version):
            log = f"Skipping {candidate_version}; already reported as patch-broken in {patches_repo}"
            print(f"[{app.id}] {log}", flush=True)
            return BuildResult(app, True, None, log, candidate_version, status="skipped_known_broken")
        for source in sources:
            candidate_stock_apk = app_dir / f"{app.id}-{candidate_version}.apk"
            candidate_output_apk = app_dir / f"{app.id}-patched-{candidate_version}.apk"
            clean_download_target(candidate_stock_apk)
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
                apk_file_type = apk_file_type_from_resolver_log(resolved.stdout + resolved.stderr, source.source, source.apk_types)
                print(f"[{app.id}] downloaded APK via {source.source}: {stock_apk}; skipping lower-priority sources", flush=True)
                break
            source_log = resolved.stdout + resolved.stderr
            print(f"[{app.id}] download did not work via {source.source}; trying next source", flush=True)
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
    add_force_compatibility(args)
    if continue_on_error:
        args.append("--continue-on-error")

    print(f"[{app.id}] patch command: {shell_join(args)}", flush=True)
    completed = run_streamed_process(app.id, "patch", args, timeout_seconds=PATCHER_TIMEOUT_SECONDS)
    print(f"[{app.id}] patch return code: {completed.returncode}", flush=True)
    log = completed.stdout + completed.stderr
    patch_context = (
        f"Downloaded APK via {source.source}: {stock_apk}\n"
        f"APK source URL: {source.url}\n"
        f"APK source and type: {source.source} arch={source.arch} dpi={source.dpi} apk-types={' '.join(source.apk_types)}\n\n"
        f"Tested ApkFileType: {apk_file_type or 'unknown'}\n\n"
    )
    if patcher_skipped_incompatible_patch(log):
        print(f"[{app.id}] released patch bundle skipped incompatible patches; rebuilding with {candidate_version}", flush=True)
        candidate_patches_file, rebuild_log = build_candidate_patches_bundle(
            app,
            candidate_version,
            version_code,
            work_dir,
            patches_repo=patches_repo,
            target_branch=target_branch,
            constants_path=constants_path,
            apk_file_type=apk_file_type,
        )
        if candidate_patches_file is None:
            return BuildResult(
                app,
                False,
                None,
                patch_context + log + "\n\n" + rebuild_log,
                candidate_version,
                version_code,
                "patch",
                downloaded=True,
                source_name=source.source,
                source_url=source.url,
                apk_file_type=apk_file_type,
            )
        candidate_output_apk = app_dir / f"{app.id}-patched-{candidate_version}-candidate.apk"
        retry_args = ["java", "-jar", str(cli_jar), "patch", str(stock_apk), "-o", str(candidate_output_apk), "--patches", str(candidate_patches_file)]
        for patch in app.included_patches:
            retry_args.extend(["-e", patch])
        for patch in app.excluded_patches:
            retry_args.extend(["-d", patch])
        retry_args.extend(app.patcher_args)
        add_force_compatibility(retry_args)
        if continue_on_error:
            retry_args.append("--continue-on-error")
        print(f"[{app.id}] retry patch command: {shell_join(retry_args)}", flush=True)
        retry = run_streamed_process(app.id, "patch retry", retry_args, timeout_seconds=PATCHER_TIMEOUT_SECONDS)
        print(f"[{app.id}] patch retry return code: {retry.returncode}", flush=True)
        retry_log = retry.stdout + retry.stderr
        combined_log = patch_context + log + "\n\nCandidate patch bundle rebuild:\n" + rebuild_log + "\n\nPatch retry:\n" + retry_log
        if (
            retry.returncode != 0
            or not candidate_output_apk.exists()
            or patcher_skipped_incompatible_patch(retry_log)
            or patcher_failed_patch(retry_log)
        ):
            print(f"[{app.id}] candidate-version patch did not finish successfully: {retry_log[-1000:]}", flush=True)
            failure_type = classify_failure(retry_log, "patch")
            return BuildResult(
                app,
                False,
                None,
                combined_log,
                candidate_version,
                version_code,
                failure_type,
                downloaded=True,
                source_name=source.source,
                source_url=source.url,
                apk_file_type=apk_file_type,
            )
        print(f"[{app.id}] patched APK ready: {candidate_output_apk}", flush=True)
        return BuildResult(
            app,
            True,
            candidate_output_apk,
            combined_log,
            candidate_version,
            version_code,
            status="patched",
            downloaded=True,
            source_name=source.source,
            source_url=source.url,
            apk_file_type=apk_file_type,
        )
    if completed.returncode != 0 or not output_apk.exists() or patcher_failed_patch(log):
        print(f"[{app.id}] patch did not finish successfully: {log[-1000:]}", flush=True)
        failure_type = classify_failure(log, "patch")
        return BuildResult(
            app,
            False,
            None,
            patch_context + log,
            candidate_version,
            version_code,
            failure_type,
            downloaded=True,
            source_name=source.source,
            source_url=source.url,
            apk_file_type=apk_file_type,
        )
    print(f"[{app.id}] patched APK ready: {output_apk}", flush=True)
    return BuildResult(
        app,
        True,
        output_apk,
        log,
        candidate_version,
        version_code,
        status="patched",
        downloaded=True,
        source_name=source.source,
        source_url=source.url,
        apk_file_type=apk_file_type,
    )


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


def clean_download_target(path: Path) -> None:
    for candidate in [path, *path.parent.glob(path.name + ".*"), path.with_suffix(path.suffix + ".gplay")]:
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        except FileNotFoundError:
            continue


def apk_file_type_from_resolver_log(log: str, source_name: str, configured_types: list[str]) -> str:
    matches = re.findall(r"APK file type:\s*(APK|APKM|APKS|XAPK)\b", log, re.IGNORECASE)
    if matches:
        return matches[-1].upper()
    configured = [item.upper() for item in configured_types]
    if len(configured) == 1 and configured[0] in {"APK", "APKM", "APKS", "XAPK"}:
        return configured[0]
    if source_name == "apkmirror" and any(item in configured for item in ("APKM", "APKS", "XAPK")):
        return "APKM"
    return "APK"


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


def build_candidate_patches_bundle(
    app: AppConfig,
    candidate_version: str,
    version_code: str | None,
    work_dir: Path,
    *,
    patches_repo: str,
    target_branch: str,
    constants_path: str,
    apk_file_type: str | None,
) -> tuple[Path | None, str]:
    repo_dir = work_dir / "candidate-patches" / app.id
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    clone_url = f"https://github.com/{patches_repo}.git"
    clone = run_plain_process(
        ["git", "clone", "--depth", "1", "--branch", target_branch, clone_url, str(repo_dir)],
        timeout_seconds=300,
    )
    log = "Clone candidate patches repo:\n" + clone.stdout + clone.stderr
    if clone.returncode != 0:
        return None, log

    constants_file = repo_dir / constants_path
    if not update_app_target_version(constants_file, app.constant, candidate_version, version_code, apk_file_type):
        return None, log + f"\nCould not update {app.constant} to {candidate_version} in {constants_path}\n"

    gradlew = repo_dir / "gradlew"
    try:
        gradlew.chmod(gradlew.stat().st_mode | 0o111)
    except OSError:
        pass
    build = run_plain_process(
        [str(gradlew), ":patches:buildAndroid", "--no-daemon"],
        cwd=repo_dir,
        timeout_seconds=900,
    )
    log += "\nBuild candidate patches bundle:\n" + build.stdout + build.stderr
    if build.returncode != 0:
        return None, log
    candidates = [
        path
        for path in (repo_dir / "patches" / "build" / "libs").glob("*.mpp")
        if "-sources" not in path.name and "-javadoc" not in path.name
    ]
    if not candidates:
        return None, log + "\nCould not find built .mpp in patches/build/libs\n"
    return max(candidates, key=lambda path: path.stat().st_mtime), log


def run_plain_process(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else (error.stdout or b"").decode(errors="replace")
        stderr = error.stderr if isinstance(error.stderr, str) else (error.stderr or b"").decode(errors="replace")
        return subprocess.CompletedProcess(args, 124, stdout, stderr + f"\nTimed out after {timeout_seconds}s\n")


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


def patcher_failed_patch(log: str) -> bool:
    return bool(re.search(r"(?im)^\s*(?:severe:\s*)?failed:\s+", log))


def add_force_compatibility(args: list[str]) -> None:
    if "-f" not in args and "--force" not in args:
        args.append("--force")


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
    if "failed to match the fingerprint" in lower or "failed to resolve" in lower or "fingerprint" in lower:
        return "fingerprint"
    if "keystore" in lower or "apksigner" in lower or "failed to sign" in lower or "signing" in lower:
        return "signing"
    if "download" in lower or "http" in lower or "cloudflare" in lower:
        return "download"
    return default
