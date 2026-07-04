from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

from .build import build_app
from .config import load_config
from .constants import update_app_target_version
from .github import create_or_update_failure_issue, create_pull_request
from .releases import resolve_tool


def git(args: list[str], cwd: Path, *, dry_run: bool = False) -> str:
    cmd = ["git", *args]
    if dry_run:
        print("+", " ".join(cmd))
        return ""
    completed = subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)
    return completed.stdout.strip()


def clone_patches_repo(repo: str, dest: Path, *, dry_run: bool = False) -> Path:
    if dry_run:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run(["gh", "repo", "clone", repo, str(dest), "--", "--depth", "1"], check=True)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    root = Path.cwd()
    work_dir = root / cfg.tracker.work_dir
    artifacts_dir = root / "artifacts"
    logs_dir = root / "logs"
    work_dir.mkdir(exist_ok=True)
    artifacts_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)

    cli_jar = resolve_tool(cfg.cli, work_dir / "tools" / "cli.jar", dry_run=args.dry_run)
    patches_file = resolve_tool(cfg.patches, work_dir / "tools" / "patches.mpp", dry_run=args.dry_run)

    results = []
    for app in cfg.apps:
        result = build_app(app, cli_jar, patches_file, work_dir, dry_run=args.dry_run)
        log_path = logs_dir / f"{app.id}.log"
        log_path.write_text(result.log, encoding="utf-8")
        if result.output:
            shutil.copy2(result.output, artifacts_dir / result.output.name)
        results.append(result)

    status = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
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
                "artifact": str(r.output) if r.output else None,
            }
            for r in results
        ],
    }
    (root / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    (root / "STATUS.md").write_text(render_status_table(results), encoding="utf-8")

    if args.dry_run:
        print(json.dumps(status, indent=2))
        return 0

    patches_repo_path = clone_patches_repo(cfg.tracker.patches_repo, work_dir / "morphe-patches")
    branch = f"tracker/update-working-versions-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    git(["checkout", "-b", branch], patches_repo_path)

    changed = []
    constants_file = patches_repo_path / cfg.tracker.constants_path
    for result in results:
        app = result.app
        if result.ok:
            if update_app_target_version(constants_file, app.constant, result.candidate_version, result.version_code):
                changed.append(app)
            continue

        issue_title = f"tracker: {app.name} failed on {result.candidate_version}"
        body = (
            f"Automated tracker failed to patch `{app.name}` (`{app.package_name}`).\n\n"
            f"- Current known working: `{app.current_version}`\n"
            f"- Candidate tested: `{result.candidate_version}`\n"
            f"- Failure type: `{result.failure_type or 'unknown'}`\n"
            f"- Workflow: {os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}\n\n"
            "Last log excerpt:\n\n"
            "```text\n"
            f"{result.log[-6000:]}\n"
            "```"
        )
        create_or_update_failure_issue(
            cfg.tracker.patches_repo,
            issue_title,
            body,
            ["bug", "tracker", "patch-broken-after-update"],
        )

    if changed:
        git(["config", "user.name", "patches-tracker"], patches_repo_path)
        git(["config", "user.email", "patches-tracker@users.noreply.github.com"], patches_repo_path)
        git(["add", cfg.tracker.constants_path], patches_repo_path)
        git(["commit", "-m", "chore: update tracker verified app versions"], patches_repo_path)
        git(["push", "origin", branch], patches_repo_path)
        changed_by_id = {result.app.id: result for result in results}
        body = "\n".join(
            f"- `{app.name}`: `{app.current_version}` -> `{changed_by_id[app.id].candidate_version}`"
            + (f" (`versionCode {changed_by_id[app.id].version_code}`)" if changed_by_id[app.id].version_code else "")
            for app in changed
        )
        create_pull_request(
            patches_repo_path,
            cfg.tracker.patches_repo,
            branch,
            "chore: update tracker verified app versions",
            body,
        )

    return 0 if all(result.ok for result in results) else 1


def render_status_table(results) -> str:
    lines = [
        "# Patch Tracker Status",
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
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
