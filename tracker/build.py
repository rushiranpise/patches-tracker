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

from .config import AppConfig
from .constants import is_newer_version, update_app_target_version
from .github import known_patch_failure_exists


RESOLVER_RETRIES = int(os.environ.get("RESOLVER_RETRIES", "1"))
RESOLVER_TIMEOUT_SECONDS = int(os.environ.get("RESOLVER_TIMEOUT_SECONDS", "300"))
PATCHER_TIMEOUT_SECONDS = int(os.environ.get("PATCHER_TIMEOUT_SECONDS", "900"))
COMPARABLE_LATEST_SOURCES = {"direct", "github", "archive", "aoneroom", "apkmirror", "uptodown", "apkpure", "apkcombo"}
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
    repair_repo_path: Path | None = None
    repair_summary: str = ""


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
            repair = attempt_fingerprint_repair(
                app,
                combined_log,
                stock_apk,
                work_dir,
                patches_repo=patches_repo,
                target_branch=target_branch,
                constants_path=constants_path,
                candidate_version=candidate_version,
                version_code=version_code,
                apk_file_type=apk_file_type,
                failure_type=failure_type,
                app_dir=app_dir,
                cli_jar=cli_jar,
                continue_on_error=continue_on_error,
                old_stock_apk=download_current_version_apk(app, app_dir, resolver),
            )
            combined_log = repair.log
            if repair.output_apk:
                return BuildResult(
                    app,
                    True,
                    repair.output_apk,
                    combined_log,
                    candidate_version,
                    version_code,
                    status="patched",
                    downloaded=True,
                    source_name=source.source,
                    source_url=source.url,
                    apk_file_type=apk_file_type,
                    repair_repo_path=repair.repo_path,
                    repair_summary=repair.summary,
                )
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
        repair = attempt_fingerprint_repair(
            app,
            patch_context + log,
            stock_apk,
            work_dir,
            patches_repo=patches_repo,
            target_branch=target_branch,
            constants_path=constants_path,
            candidate_version=candidate_version,
            version_code=version_code,
            apk_file_type=apk_file_type,
            failure_type=failure_type,
            app_dir=app_dir,
            cli_jar=cli_jar,
            continue_on_error=continue_on_error,
            old_stock_apk=download_current_version_apk(app, app_dir, resolver),
        )
        failure_log = repair.log
        if repair.output_apk:
            return BuildResult(
                app,
                True,
                repair.output_apk,
                failure_log,
                candidate_version,
                version_code,
                status="patched",
                downloaded=True,
                source_name=source.source,
                source_url=source.url,
                apk_file_type=apk_file_type,
                repair_repo_path=repair.repo_path,
                repair_summary=repair.summary,
            )
        return BuildResult(
            app,
            False,
            None,
            failure_log,
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


def download_current_version_apk(app: AppConfig, app_dir: Path, resolver: Path) -> Path | None:
    if not app.current_version:
        return None
    current_stock_apk = app_dir / f"{app.id}-{app.current_version}-current.apk"
    if current_stock_apk.exists():
        print(f"[{app.id}] using cached current-version APK for repair: {current_stock_apk}", flush=True)
        return current_stock_apk
    for source in app.resolved_sources():
        clean_download_target(current_stock_apk)
        print(f"[{app.id}] downloading current version {app.current_version} via {source.source} for repair comparison", flush=True)
        resolved = run_resolver(
            app.id,
            "current-version download",
            [
                "bash",
                str(resolver),
                source.source,
                source.url,
                app.current_version,
                str(current_stock_apk),
                source.arch,
                source.dpi,
                " ".join(source.apk_types),
            ],
        )
        if resolved.returncode == 0 and current_stock_apk.exists():
            print(f"[{app.id}] downloaded current-version APK via {source.source}: {current_stock_apk}", flush=True)
            return current_stock_apk
        print(f"[{app.id}] current-version download did not work via {source.source}; trying next source", flush=True)
    print(f"[{app.id}] could not download current-version APK for repair comparison", flush=True)
    return None


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


@dataclass
class RepairResult:
    log: str
    output_apk: Path | None = None
    repo_path: Path | None = None
    summary: str = ""


def attempt_fingerprint_repair(
    app: AppConfig,
    log: str,
    stock_apk: Path,
    work_dir: Path,
    *,
    patches_repo: str,
    target_branch: str,
    constants_path: str,
    candidate_version: str,
    version_code: str | None,
    apk_file_type: str | None,
    failure_type: str,
    app_dir: Path,
    cli_jar: Path,
    continue_on_error: bool = False,
    old_stock_apk: Path | None = None,
) -> RepairResult:
    if failure_type != "fingerprint":
        return RepairResult(append_patch_failure_analysis(app, log, stock_apk, work_dir, failure_type))
    repo_dir = clone_repair_source(app, work_dir, patches_repo, target_branch)
    if repo_dir is None:
        return RepairResult(log + "\nAuto-repair skipped: patches source was not available.\n")

    constants_file = repo_dir / constants_path
    update_app_target_version(constants_file, app.constant, candidate_version, version_code, apk_file_type)

    repair_log = run_decompiled_patch_repair(app, repo_dir, stock_apk, old_stock_apk, work_dir)
    enriched_log = log + "\n\nDecompiled patch repair:\n" + repair_log + "\n"
    if not repo_has_patch_changes(repo_dir, constants_path):
        return RepairResult(enriched_log + "\nAuto-repair stopped: helper did not change patch files.\n")

    candidate_patches_file, build_log = build_patches_bundle_in_repo(repo_dir)
    enriched_log += "\nAuto-repair build:\n" + build_log + "\n"
    if candidate_patches_file is None:
        return RepairResult(enriched_log)

    verified_output, verify_log = verify_repaired_patch_with_runner(
        app,
        stock_apk,
        candidate_patches_file,
        cli_jar,
        work_dir,
        continue_on_error=continue_on_error,
    )
    enriched_log += "\nAuto-repair verification:\n" + verify_log + "\n"
    if verified_output:
        print(f"[{app.id}] decompiled auto-repair verified: {verified_output}", flush=True)
        summary = summarize_repo_changes(repo_dir) or "decompiled fingerprint repair"
        return RepairResult(enriched_log, verified_output, repo_dir, summary)
    return RepairResult(enriched_log + "\nAuto-repair stopped: repaired bundle did not patch successfully.\n")


def clone_repair_source(app: AppConfig, work_dir: Path, patches_repo: str, target_branch: str) -> Path | None:
    repo_dir = work_dir / "fingerprint-analysis-source" / app.id
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    clone_url = f"https://github.com/{patches_repo}.git"
    clone = run_plain_process(
        ["git", "clone", "--depth", "1", "--branch", target_branch, clone_url, str(repo_dir)],
        timeout_seconds=300,
    )
    if clone.returncode != 0:
        return None
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        run_plain_process(
            ["git", "remote", "set-url", "origin", f"https://x-access-token:{token}@github.com/{patches_repo}.git"],
            cwd=repo_dir,
            timeout_seconds=60,
        )
    return repo_dir


def run_decompiled_patch_repair(
    app: AppConfig,
    repo_dir: Path,
    stock_apk: Path,
    old_stock_apk: Path | None,
    work_dir: Path,
) -> str:
    repair_root = work_dir / "decompiled-repair" / app.id
    if repair_root.exists():
        shutil.rmtree(repair_root)
    repair_root.mkdir(parents=True, exist_ok=True)

    new_tree = repair_root / "new"
    old_tree = repair_root / "old"
    logs: list[str] = []
    logs.append(run_apktool_decode(app.id, stock_apk, new_tree, "new APK"))
    if old_stock_apk:
        logs.append(run_apktool_decode(app.id, old_stock_apk, old_tree, "old APK"))

    patch_dir = find_patch_dir(repo_dir, app)
    if patch_dir is None:
        return "\n".join(logs) + "\nCould not find patch directory for app.\n"

    report_dir = repair_root / "report"
    args = [
        "python",
        str(Path("scripts") / "update_patch_from_decompiled.py"),
        "--new-apktool",
        str(new_tree),
        "--patch-dir",
        str(patch_dir),
        "--out",
        str(report_dir),
        "--in-place",
        "--update-version",
        "--min-confidence",
        "medium",
    ]
    if old_stock_apk and old_tree.exists():
        args.extend(["--old-apktool", str(old_tree)])
    completed = run_plain_process(args, timeout_seconds=1200)
    logs.append("$ " + shell_join(args))
    logs.append(completed.stdout + completed.stderr)
    for report_name in ("patch_update_report.md", "patch_update_suggestions.json"):
        report = report_dir / report_name
        if report.exists():
            logs.append(f"\n{report_name}:\n{report.read_text(encoding='utf-8', errors='replace')[-12000:]}")
    return "\n".join(logs)


def run_apktool_decode(app_id: str, apk: Path, out_dir: Path, label: str) -> str:
    args = ["apktool", "d", "-f", "-o", str(out_dir), str(apk)]
    print(f"[{app_id}] decompiling {label}: {shell_join(args)}", flush=True)
    completed = run_plain_process(args, timeout_seconds=900)
    return f"$ {shell_join(args)}\n{completed.stdout}{completed.stderr}"


def find_patch_dir(repo_dir: Path, app: AppConfig) -> Path | None:
    patches_root = repo_dir / "patches" / "src" / "main" / "kotlin" / "app" / "template" / "patches"
    if not patches_root.exists():
        return None
    dirs = [path for path in patches_root.iterdir() if path.is_dir()]
    app_tokens = {
        normalize_for_match(app.id),
        normalize_for_match(app.name),
        normalize_for_match(app.package_name.rsplit(".", 1)[-1]),
    }
    for path in dirs:
        if normalize_for_match(path.name) in app_tokens:
            return path
    probes = [app.constant, app.package_name, *app.included_patches]
    scored: list[tuple[int, Path]] = []
    for path in dirs:
        score = 0
        for kt in path.rglob("*.kt"):
            try:
                text = kt.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for probe in probes:
                if probe and probe in text:
                    score += 1
        if score:
            scored.append((score, path))
    if scored:
        scored.sort(reverse=True, key=lambda item: item[0])
        return scored[0][1]
    return None


def normalize_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def repo_has_patch_changes(repo_dir: Path, constants_path: str) -> bool:
    diff = run_plain_process(["git", "diff", "--name-only"], cwd=repo_dir, timeout_seconds=60)
    constants = constants_path.replace("\\", "/")
    for line in diff.stdout.splitlines():
        path = line.strip().replace("\\", "/")
        if path and path != constants:
            return True
    return False


def verify_repaired_patch_with_runner(
    app: AppConfig,
    stock_apk: Path,
    patches_file: Path,
    cli_jar: Path,
    work_dir: Path,
    *,
    continue_on_error: bool,
) -> tuple[Path | None, str]:
    out_dir = work_dir / "morphe-cli-repair-runs" / app.id
    args = [
        "python",
        str(Path("scripts") / "run_morphe_cli_patch.py"),
        "--apk",
        str(stock_apk),
        "--patches-mpp",
        str(patches_file),
        "--cli-jar",
        str(cli_jar),
        "--out-dir",
        str(out_dir),
        "--force",
        "--no-build-patches",
        "--no-download-cli",
        "--no-doctor-check",
    ]
    for patch in app.included_patches:
        args.extend(["--patch", patch])
    for patch in app.excluded_patches:
        args.extend(["--disable", patch])
    if continue_on_error:
        args.append("--continue-on-error")
    completed = run_plain_process(args, timeout_seconds=PATCHER_TIMEOUT_SECONDS)
    log = "$ " + shell_join(args) + "\n" + completed.stdout + completed.stderr
    output = newest_file("*-patched.apk", out_dir)
    if completed.returncode == 0 and output and output.exists() and not patcher_failed_patch(log) and not patcher_skipped_incompatible_patch(log):
        return output, log
    return None, log


def summarize_repo_changes(repo_dir: Path) -> str:
    diff = run_plain_process(["git", "diff", "--stat"], cwd=repo_dir, timeout_seconds=60)
    return diff.stdout.strip()


def newest_file(pattern: str, root: Path) -> Path | None:
    files = [path for path in root.glob(pattern) if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def append_patch_failure_analysis(
    app: AppConfig,
    log: str,
    stock_apk: Path,
    work_dir: Path,
    failure_type: str,
) -> str:
    if failure_type != "patch":
        return log
    log_path = work_dir / "patch-failure-analysis" / app.id / "failure.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log[-12000:], encoding="utf-8")
    return log + "\n\nPatch analysis skipped: no generic patch analyzer is configured; raw log saved for artifacts.\n"


def build_patches_bundle_in_repo(repo_dir: Path) -> tuple[Path | None, str]:
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
    log = build.stdout + build.stderr
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
