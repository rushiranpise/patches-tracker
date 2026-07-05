from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .build import build_app
from .config import load_config
from .constants import is_newer_version, update_app_target_version
from .github import create_or_update_failure_issue, create_pull_request
from .releases import resolve_tool


def git(args: list[str], cwd: Path, *, dry_run: bool = False) -> str:
    cmd = ["git", *args]
    if dry_run:
        print("+", " ".join(cmd))
        return ""
    completed = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if completed.returncode != 0:
        print(f"git command failed: {' '.join(cmd)}", flush=True)
        print(completed.stdout, flush=True)
        print(completed.stderr, flush=True)
        completed.check_returncode()
    return completed.stdout.strip()


def clone_patches_repo(repo: str, dest: Path, branch: str, *, dry_run: bool = False) -> Path:
    if dry_run:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run(["gh", "repo", "clone", repo, str(dest), "--", "--branch", branch, "--depth", "1"], check=True)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        subprocess.run(
            ["git", "remote", "set-url", "origin", f"https://x-access-token:{token}@github.com/{repo}.git"],
            cwd=dest,
            check=True,
        )
        print(f"configured authenticated push remote for {repo}", flush=True)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-total", type=int, default=1)
    args = parser.parse_args()
    if args.shard_total < 1:
        parser.error("--shard-total must be at least 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_total:
        parser.error("--shard-index must be between 0 and --shard-total - 1")

    cfg = load_config(args.config)
    all_apps = cfg.apps
    cfg_apps = [app for index, app in enumerate(all_apps) if index % args.shard_total == args.shard_index]
    print(
        f"tracker shard {args.shard_index + 1}/{args.shard_total}: "
        f"{len(cfg_apps)} of {len(all_apps)} apps",
        flush=True,
    )
    root = Path.cwd()
    work_dir = root / cfg.tracker.work_dir
    artifacts_dir = root / "artifacts"
    logs_dir = root / "logs"
    work_dir.mkdir(exist_ok=True)
    artifacts_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)

    cli_jar = resolve_tool(cfg.cli, work_dir / "tools" / "cli.jar", dry_run=args.dry_run)
    patches_file = resolve_tool(cfg.patches, work_dir / "tools" / "patches.mpp", dry_run=args.dry_run)

    results_by_index = {}
    parallel_jobs = max(1, cfg.tracker.parallel_jobs)
    print(f"tracker parallel jobs: {parallel_jobs}", flush=True)
    with ThreadPoolExecutor(max_workers=parallel_jobs) as executor:
        future_to_index = {
            executor.submit(build_app, app, cli_jar, patches_file, work_dir, dry_run=args.dry_run): index
            for index, app in enumerate(cfg_apps)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            results_by_index[index] = future.result()

    results = []
    for index, app in enumerate(cfg_apps):
        result = results_by_index[index]
        log_path = logs_dir / f"{app.id}.log"
        log_path.write_text(result.log, encoding="utf-8")
        if result.output:
            shutil.copy2(result.output, artifacts_dir / result.output.name)
        results.append(result)

    status = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "shard_index": args.shard_index,
        "shard_total": args.shard_total,
        "app_count": len(cfg_apps),
        "total_app_count": len(all_apps),
        "results": [
            {
                "id": r.app.id,
                "name": r.app.name,
                "package_name": r.app.package_name,
                "current_version": r.app.current_version,
                "candidate_version": r.candidate_version,
                "version_code": r.version_code,
                "ok": r.ok,
                "failure_type": r.failure_type,
                "log_excerpt": r.log[-2000:] if not r.ok else "",
                "artifact": str(r.output) if r.output else None,
            }
            for r in results
        ],
    }
    (root / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    (root / "STATUS.md").write_text(render_status_table(results, args.shard_index, args.shard_total), encoding="utf-8")

    if args.dry_run:
        print(json.dumps(status, indent=2))
        return 0

    changed = []
    successful_results = [result for result in results if result.ok]
    patches_repo_path = None
    branch = ""
    constants_file = None
    if successful_results:
        patches_repo_path = clone_patches_repo(cfg.tracker.patches_repo, work_dir / "morphe-patches", cfg.tracker.target_branch)
        branch = f"tracker/update-working-versions-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        git(["checkout", "-b", branch], patches_repo_path)
        constants_file = patches_repo_path / cfg.tracker.constants_path

    for result in results:
        app = result.app
        if result.ok:
            if constants_file is None:
                continue
            if not is_newer_version(result.candidate_version, app.current_version):
                print(
                    f"[{app.id}] patched candidate {result.candidate_version} is not newer than "
                    f"current {app.current_version}; skipping constants update",
                    flush=True,
                )
                continue
            if update_app_target_version(constants_file, app.constant, result.candidate_version, result.version_code):
                changed.append(app)
            continue

        target_repo = issue_repo_for_failure(cfg.tracker.patches_repo, result.failure_type)
        issue_title = f"tracker: {app.name} failed on {result.candidate_version}"
        body = (
            f"Automated tracker failed to patch `{app.name}` (`{app.package_name}`).\n\n"
            f"- Current known working: `{app.current_version}`\n"
            f"- Candidate tested: `{result.candidate_version}`\n"
            f"- Failure type: `{result.failure_type or 'unknown'}`\n"
            f"- Issue repo: `{target_repo}`\n"
            f"- Workflow: {os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}\n\n"
            "Last log excerpt:\n\n"
            "```text\n"
            f"{result.log[-6000:]}\n"
            "```"
        )
        create_or_update_failure_issue(
            target_repo,
            issue_title,
            body,
            issue_labels_for_failure(result.failure_type),
        )

    if changed:
        git(["config", "user.name", "patches-tracker"], patches_repo_path)
        git(["config", "user.email", "patches-tracker@users.noreply.github.com"], patches_repo_path)
        git(["add", cfg.tracker.constants_path], patches_repo_path)
        git(["commit", "-m", "chore: update tracker verified app versions"], patches_repo_path)
        changed_by_id = {result.app.id: result for result in results}
        body = "\n".join(
            f"- `{app.name}`: `{app.current_version}` -> `{changed_by_id[app.id].candidate_version}`"
            + (f" (`versionCode {changed_by_id[app.id].version_code}`)" if changed_by_id[app.id].version_code else "")
            for app in changed
        )
        try:
            git(["push", "origin", branch], patches_repo_path)
            create_pull_request(
                patches_repo_path,
                cfg.tracker.patches_repo,
                branch,
                cfg.tracker.target_branch,
                "chore: update tracker verified app versions",
                body,
            )
        except subprocess.CalledProcessError:
            print("warning: could not push/open PR for morphe-patches; check PATCHES_REPO_TOKEN contents write access", flush=True)

    return 0


def issue_repo_for_failure(patches_repo: str, failure_type: str | None) -> str:
    if failure_type in {"download", "version_resolve", "config"}:
        return os.environ.get("GITHUB_REPOSITORY") or "rushiranpise/patches-tracker"
    return patches_repo


def issue_labels_for_failure(failure_type: str | None) -> list[str]:
    if failure_type in {"download", "version_resolve", "config"}:
        return ["bug", "tracker", "source-failure"]
    return ["bug", "tracker", "patch-broken-after-update"]


def render_status_table(results, shard_index: int = 0, shard_total: int = 1) -> str:
    lines = [
        "# Patch Tracker Status",
        "",
        f"Shard: {shard_index + 1}/{shard_total}",
        "",
        "| App | Package | Known working | Tested | Version code | Status | Failure |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        status = "ok" if result.ok else "failed"
        lines.append(
            "| "
            + " | ".join(
                [
                    result.app.name,
                    f"`{result.app.package_name}`",
                    f"`{result.app.current_version}`",
                    f"`{result.candidate_version}`",
                    f"`{result.version_code}`" if result.version_code else "",
                    status,
                    result.failure_type or "",
                ]
            )
            + " |"
        )
        if not result.ok and result.log:
            lines.extend(
                [
                    "",
                    f"<details><summary>{result.app.name} log excerpt</summary>",
                    "",
                    "```text",
                    result.log[-2000:],
                    "```",
                    "",
                    "</details>",
                ]
            )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
