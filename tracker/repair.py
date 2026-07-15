from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from datetime import datetime, timezone

from .build import build_app
from .cli import (
    clone_patches_repo,
    create_repair_pull_request,
    git,
    git_author_email,
    git_author_name,
    render_status_table,
)
from .config import AppConfig, load_config
from .constants import is_newer_version, update_app_target_version
from .github import comment_issue, create_pull_request, run_gh, token_for_repo, tracker_metadata_from_issue_body
from .releases import resolve_tool


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry and repair apps from open morphe-patches failure issues.")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--issue", action="append", default=[], help="Specific morphe-patches issue number to repair")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    root = Path.cwd()
    work_dir = root / cfg.tracker.work_dir
    artifacts_dir = root / "repair-artifacts"
    logs_dir = root / "repair-logs"
    work_dir.mkdir(exist_ok=True)
    artifacts_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)

    issues = fetch_patch_failure_issues(cfg.tracker.patches_repo, args.issue, args.limit)
    targets = issue_targets(issues, cfg.apps)
    print(f"repair targets: {len(targets)} app(s) from {len(issues)} issue(s)", flush=True)
    for target in targets:
        print(f"- {target.app.id}: {target.version} from {target.issue_url}", flush=True)

    cli_jar = resolve_tool(cfg.cli, work_dir / "tools" / "cli.jar", dry_run=args.dry_run)
    patches_file = resolve_tool(cfg.patches, work_dir / "tools" / "patches.mpp", dry_run=args.dry_run)

    results = []
    for target in targets:
        app = replace(target.app, candidate_version=target.version)
        result = build_app(
            app,
            cli_jar,
            patches_file,
            work_dir,
            patches_repo=cfg.tracker.patches_repo,
            target_branch=cfg.tracker.target_branch,
            constants_path=cfg.tracker.constants_path,
            dry_run=args.dry_run,
            ignore_known_failures=True,
        )
        log_path = logs_dir / f"{app.id}.log"
        log_path.write_text(result.log, encoding="utf-8")
        if result.output:
            shutil.copy2(result.output, artifacts_dir / result.output.name)
        results.append((target, result))

    status_results = [result for _, result in results]
    status = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "repair",
        "app_count": len(status_results),
        "results": [
            {
                "issue": target.issue_number,
                "issue_url": target.issue_url,
                "id": result.app.id,
                "name": result.app.name,
                "package_name": result.app.package_name,
                "current_version": result.app.current_version,
                "candidate_version": result.candidate_version,
                "version_code": result.version_code,
                "ok": result.ok,
                "status": result.status,
                "failure_type": result.failure_type,
                "repair": bool(result.repair_repo_path),
                "repair_summary": result.repair_summary,
                "artifact": str(result.output) if result.output else None,
                "log_excerpt": result.log[-2000:] if not result.ok else "",
            }
            for target, result in results
        ],
    }
    (root / "repair-status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    (root / "REPAIR_STATUS.md").write_text(render_status_table(status_results), encoding="utf-8")

    if args.dry_run:
        print(json.dumps(status, indent=2))
        return 0

    changed = []
    constants_repo = None
    constants_file = None
    for target, result in results:
        if result.repair_repo_path:
            pr_url = create_repair_pull_request(result, cfg.tracker.patches_repo, cfg.tracker.target_branch)
            if pr_url:
                comment_repair_issue(cfg.tracker.patches_repo, target, result, pr_url)
            continue
        if result.ok and result.output and is_newer_version(result.candidate_version, result.app.current_version):
            if constants_repo is None:
                constants_repo = clone_patches_repo(cfg.tracker.patches_repo, work_dir / "repair-version-updates", cfg.tracker.target_branch)
                branch = f"tracker/repair-verified-versions-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                git(["checkout", "-b", branch], constants_repo)
                constants_file = constants_repo / cfg.tracker.constants_path
            if update_app_target_version(
                constants_file,
                result.app.constant,
                result.candidate_version,
                result.version_code,
                result.apk_file_type,
            ):
                changed.append((target, result))

    if changed and constants_repo and constants_file:
        git(["config", "user.name", git_author_name()], constants_repo)
        git(["config", "user.email", git_author_email()], constants_repo)
        git(["add", cfg.tracker.constants_path], constants_repo)
        git(["commit", "-m", "chore: update repaired app versions"], constants_repo)
        body = "\n".join(
            f"- `{result.app.name}`: `{result.app.current_version}` -> `{result.candidate_version}`"
            + (f" (`versionCode {result.version_code}`)" if result.version_code else "")
            + (f", `ApkFileType.{result.apk_file_type}`" if result.apk_file_type else "")
            for _, result in changed
        )
        branch = git(["branch", "--show-current"], constants_repo)
        git(["push", "origin", branch], constants_repo)
        pr_url = create_pull_request(
            constants_repo,
            cfg.tracker.patches_repo,
            branch,
            cfg.tracker.target_branch,
            "chore: update repaired app versions",
            body,
        )
        if pr_url:
            for target, result in changed:
                comment_repair_issue(cfg.tracker.patches_repo, target, result, pr_url)

    return 0


def comment_repair_issue(repo: str, target: "RepairTarget", result, pr_url: str) -> None:
    summary = result.repair_summary or "verified with a rebuilt patch bundle; no patch-file repair was needed"
    body = (
        f"Repair PR opened: {pr_url}\n\n"
        f"- App: `{result.app.name}`\n"
        f"- Version: `{result.candidate_version}`"
        + (f" (`versionCode {result.version_code}`)" if result.version_code else "")
        + "\n"
        f"- Result: patched APK verified\n"
        f"- Repair: {summary}\n\n"
        "Leaving this issue open for review until the repair PR is merged."
    )
    comment_issue(repo, target.issue_number, body)


class RepairTarget:
    def __init__(self, app: AppConfig, version: str, issue_number: int, issue_url: str) -> None:
        self.app = app
        self.version = version
        self.issue_number = issue_number
        self.issue_url = issue_url


def fetch_patch_failure_issues(repo: str, issue_numbers: list[str], limit: int) -> list[dict]:
    token = token_for_repo(repo)
    if issue_numbers:
        issues = []
        for number in issue_numbers:
            raw = run_gh(
                ["api", f"repos/{repo}/issues/{number}", "--jq", "{number,title,body,html_url}"],
                token=token,
            )
            issues.append(json.loads(raw))
        return issues
    raw = run_gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            'is:issue is:open "patch broken after app update"',
            "--limit",
            str(limit),
            "--json",
            "number,title,body,url",
        ],
        token=token,
    )
    return json.loads(raw or "[]")


def issue_targets(issues: list[dict], apps: list[AppConfig]) -> list[RepairTarget]:
    by_name = {normalize(app.name): app for app in apps}
    by_package = {app.package_name: app for app in apps}
    targets = []
    for issue in issues:
        body = issue.get("body") or ""
        metadata = tracker_metadata_from_issue_body(body)
        app = None
        version = ""
        if metadata:
            app = by_package.get(metadata.get("package_name", "")) or by_name.get(normalize(metadata.get("app_name", "")))
            version = metadata.get("candidate_version", "")
        if app is None:
            title_name = app_name_from_issue_title(issue.get("title", ""))
            app = by_name.get(normalize(title_name))
        if not version:
            version = version_from_issue_body(body)
        if app is None or not version:
            print(f"warning: could not map issue #{issue.get('number')}: {issue.get('title')}", flush=True)
            continue
        targets.append(RepairTarget(app, version, int(issue["number"]), issue.get("url") or issue.get("html_url", "")))
    return targets


def app_name_from_issue_title(title: str) -> str:
    return title.split("bug: patch broken after app update - ", 1)[-1].strip()


def version_from_issue_body(body: str) -> str:
    match = re.search(r"### Broken app version\s+`?([^`\n(]+)`?", body)
    if match:
        return match.group(1).strip()
    match = re.search(r"Broken app version[\s\S]{0,80}?([0-9][A-Za-z0-9_.-]*)", body)
    return match.group(1).strip() if match else ""


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


if __name__ == "__main__":
    raise SystemExit(main())
