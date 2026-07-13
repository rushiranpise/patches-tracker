from __future__ import annotations

import argparse
import html
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
from .github import close_resolved_failure_issue, create_or_update_failure_issue, create_pull_request
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
        print(f"push access is configured for {repo}", flush=True)
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
        f"tracker batch {args.shard_index + 1}/{args.shard_total}: "
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
    print(f"running {parallel_jobs} app check(s) at a time", flush=True)
    with ThreadPoolExecutor(max_workers=parallel_jobs) as executor:
        future_to_index = {
            executor.submit(
                build_app,
                app,
                cli_jar,
                patches_file,
                work_dir,
                patches_repo=cfg.tracker.patches_repo,
                target_branch=cfg.tracker.target_branch,
                constants_path=cfg.tracker.constants_path,
                dry_run=args.dry_run,
            ): index
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
                "status": r.status,
                "failure_type": r.failure_type,
                "downloaded": r.downloaded,
                "source": r.source_name,
                "source_url": r.source_url,
                "repair": bool(r.repair_repo_path),
                "repair_summary": r.repair_summary,
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
    successful_results = [result for result in results if result.ok and result.output and not result.repair_repo_path]
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
        if result.downloaded:
            close_resolved_source_issues(app, result)
        if result.ok:
            if result.output:
                close_resolved_issues_for_success(app, result, cfg.tracker.patches_repo)
            if result.repair_repo_path:
                create_repair_pull_request(result, cfg.tracker.patches_repo, cfg.tracker.target_branch)
                continue
            if not result.output:
                print(f"[{app.id}] {result.status}; no patched APK artifact, leaving constants unchanged", flush=True)
                continue
            if constants_file is None:
                continue
            if not is_newer_version(result.candidate_version, app.current_version):
                print(
                    f"[{app.id}] tested version {result.candidate_version} is not newer than "
                    f"current {app.current_version}; leaving constants unchanged",
                    flush=True,
                )
                continue
            if update_app_target_version(constants_file, app.constant, result.candidate_version, result.version_code):
                changed.append(app)
            continue

        target_repo = issue_repo_for_failure(cfg.tracker.patches_repo, result.failure_type)
        issue_title = issue_title_for_failure(app, result, cfg.tracker.patches_repo)
        body = issue_body_for_failure(app, result, target_repo, cfg.tracker.patches_repo, cfg.cli.repo, cfg.patches.repo)
        create_or_update_failure_issue(
            target_repo,
            issue_title,
            body,
            issue_labels_for_failure(result.failure_type),
        )

    if changed:
        git(["config", "user.name", git_author_name()], patches_repo_path)
        git(["config", "user.email", git_author_email()], patches_repo_path)
        git(["add", cfg.tracker.constants_path], patches_repo_path)
        git(["commit", "-m", "chore: update verified app versions"], patches_repo_path)
        changed_by_id = {result.app.id: result for result in results}
        body = "\n".join(
            f"- `{app.name}`: `{app.current_version}` -> `{changed_by_id[app.id].candidate_version}`"
            + (f" (`versionCode {changed_by_id[app.id].version_code}`)" if changed_by_id[app.id].version_code else "")
            for app in changed
        )
        pr_title = "chore: update verified app versions"
        try:
            git(["push", "origin", branch], patches_repo_path)
            create_pull_request(
                patches_repo_path,
                cfg.tracker.patches_repo,
                branch,
                cfg.tracker.target_branch,
                pr_title,
                body,
            )
        except subprocess.CalledProcessError:
            print("warning: could not push or open the morphe-patches PR; check PATCHES_REPO_TOKEN contents write access", flush=True)

    return 0


def create_repair_pull_request(result, patches_repo: str, target_branch: str) -> str:
    repo_path = result.repair_repo_path
    if repo_path is None:
        return ""
    app = result.app
    branch = slug_branch(f"tracker/repair-{app.id}-{result.candidate_version}")
    body = (
        f"- `{app.name}`: verified `{result.candidate_version}`"
        + (f" (`versionCode {result.version_code}`)" if result.version_code else "")
        + "\n"
        + f"- Auto-repair: {result.repair_summary or 'fingerprint target update'}\n"
    )
    try:
        git(["config", "user.name", git_author_name()], repo_path)
        git(["config", "user.email", git_author_email()], repo_path)
        git(["checkout", "-B", branch], repo_path)
        git(["add", "."], repo_path)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_path)
        if staged.returncode == 0:
            print(f"[{app.id}] auto-repair produced no git changes; skipping PR", flush=True)
            return ""
        git(["commit", "-m", f"fix: repair {app.name} for {result.candidate_version}"], repo_path)
        git(["push", "origin", branch, "--force-with-lease"], repo_path)
        return create_pull_request(
            repo_path,
            patches_repo,
            branch,
            target_branch,
            f"fix: repair {app.name} for {result.candidate_version}",
            body,
        )
    except subprocess.CalledProcessError as error:
        print(f"warning: could not push or open auto-repair PR for {app.name}: {error}", flush=True)
        return ""


def git_author_name() -> str:
    return os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("GITHUB_ACTOR") or "Rushi Ranpise"


def git_author_email() -> str:
    return os.environ.get("GIT_AUTHOR_EMAIL") or "rushiranpise17@gmail.com"


def slug_branch(value: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9._/-]+", "-", value).strip("-")


def close_resolved_source_issues(app, result) -> None:
    workflow_url = (
        f"{os.environ.get('GITHUB_SERVER_URL', '')}/"
        f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    source = result.source_name or "configured source"
    comment = (
        f"Closing automatically: patches-tracker downloaded `{app.name}` "
        f"`{result.candidate_version}` from `{source}` in {workflow_url}."
    )
    tracker_repo = os.environ.get("GITHUB_REPOSITORY") or "rushiranpise/patches-tracker"
    titles = {
        f"tracker: {app.name} needs attention for {result.candidate_version}",
        f"tracker: {app.name} needs attention for latest",
    }
    if result.candidate_version != app.current_version:
        titles.add(f"tracker: {app.name} needs attention for {app.current_version}")
    for title in titles:
        close_resolved_failure_issue(tracker_repo, title, comment)


def close_resolved_issues_for_success(app, result, patches_repo: str) -> None:
    version = result.candidate_version
    workflow_url = (
        f"{os.environ.get('GITHUB_SERVER_URL', '')}/"
        f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    comment = (
        f"Closing automatically: patches-tracker verified `{app.name}` "
        f"at `{version}` successfully in {workflow_url}."
    )
    tracker_repo = os.environ.get("GITHUB_REPOSITORY") or "rushiranpise/patches-tracker"
    tracker_titles = {
        f"tracker: {app.name} needs attention for {version}",
        f"tracker: {app.name} needs attention for latest",
    }
    if version != app.current_version:
        tracker_titles.add(f"tracker: {app.name} needs attention for {app.current_version}")
    for title in tracker_titles:
        close_resolved_failure_issue(tracker_repo, title, comment)

    close_resolved_failure_issue(
        patches_repo,
        f"bug: patch broken after app update - {app.name}",
        comment,
    )


def issue_repo_for_failure(patches_repo: str, failure_type: str | None) -> str:
    if failure_type in {"download", "version_resolve", "config"}:
        return os.environ.get("GITHUB_REPOSITORY") or "rushiranpise/patches-tracker"
    return patches_repo


def issue_labels_for_failure(failure_type: str | None) -> list[str]:
    if failure_type in {"download", "version_resolve", "config"}:
        return ["bug", "tracker", "source-failure"]
    return ["bug", "patch-broken-after-update"]


def issue_title_for_failure(app, result, patches_repo: str) -> str:
    if issue_repo_for_failure(patches_repo, result.failure_type) == patches_repo:
        return f"bug: patch broken after app update - {app.name}"
    return f"tracker: {app.name} needs attention for {result.candidate_version}"


def issue_body_for_failure(app, result, target_repo: str, patches_repo: str, cli_repo: str, patches_release_repo: str) -> str:
    workflow_url = (
        f"{os.environ.get('GITHUB_SERVER_URL', '')}/"
        f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    if target_repo == patches_repo:
        broken_patches = "\n".join(f"- {patch}" for patch in skipped_patch_names(result.log)) or "- Unknown; see logs."
        metadata = failure_metadata(app, result, target_repo)
        return (
            "### App name\n\n"
            f"{app.name} (`{app.package_name}`)\n\n"
            "### Broken app version\n\n"
            f"{result.candidate_version}"
            + (f" (`versionCode {result.version_code}`)" if result.version_code else "")
            + "\n\n"
            "### Last working app version\n\n"
            f"{app.current_version}\n\n"
            "### Patch source release\n\n"
            f"Automated tracker run using `{patches_release_repo}` release asset for `{patches_repo}`.\n\n"
            "### Manager or CLI version\n\n"
            f"Morphe CLI resolved by patches-tracker from `{cli_repo}`.\n\n"
            "### APK source and type\n\n"
            f"{apk_source_summary(result.log)}\n\n"
            "### Broken patch or patches\n\n"
            f"{broken_patches}\n\n"
            "### Tracker metadata\n\n"
            "```json\n"
            f"{metadata}\n"
            "```\n\n"
            "### Error logs\n\n"
            "```shell\n"
            f"{result.log[-6000:]}\n"
            "```\n\n"
            "### Additional context\n\n"
            f"Automated report from patches-tracker: {workflow_url}\n\n"
            "### Acknowledgements\n\n"
            "- [x] I have checked all open and closed bug reports and this is not a duplicate.\n"
            "- [x] I followed the README log capture instructions and included the relevant logs.\n"
            "- [x] All requested information has been provided properly.\n"
        )
    metadata = failure_metadata(app, result, target_repo)
    return (
        f"The tracker could not finish `{app.name}` (`{app.package_name}`).\n\n"
        f"- Current known working: `{app.current_version}`\n"
        f"- Version checked: `{result.candidate_version}`\n"
        f"- Problem area: `{friendly_failure_type(result.failure_type)}`\n"
        f"- Issue repo: `{target_repo}`\n"
        f"- Workflow: {workflow_url}\n\n"
        "Tracker metadata:\n\n"
        "```json\n"
        f"{metadata}\n"
        "```\n\n"
        "Last log excerpt:\n\n"
        "```text\n"
        f"{result.log[-6000:]}\n"
        "```"
    )


def failure_metadata(app, result, target_repo: str) -> str:
    return json.dumps(
        {
            "schema": "patches-tracker/failure/v1",
            "issue_repo": target_repo,
            "app_id": app.id,
            "app_name": app.name,
            "package_name": app.package_name,
            "constant": app.constant,
            "current_version": app.current_version,
            "candidate_version": result.candidate_version,
            "version_code": result.version_code,
            "status": result.status,
            "failure_type": result.failure_type,
            "downloaded": result.downloaded,
            "source": result.source_name,
            "source_url": result.source_url,
            "apk_source": apk_source_summary(result.log),
            "skipped_patches": skipped_patch_names(result.log),
            "analysis_reports": analysis_report_types(result.log),
        },
        indent=2,
        sort_keys=True,
    )


def skipped_patch_names(log: str) -> list[str]:
    import re

    return sorted(set(re.findall(r'WARNING: Skipping "([^"]+)"', log)))


def analysis_report_types(log: str) -> list[str]:
    reports = []
    if "Fingerprint analysis JSON:" in log:
        reports.append("fingerprint")
    if "Patch analysis JSON:" in log:
        reports.append("patch")
    return reports


def apk_source_summary(log: str) -> str:
    for line in log.splitlines():
        if line.startswith("APK source and type: "):
            return line.removeprefix("APK source and type: ")
    for line in log.splitlines():
        if line.startswith("Downloaded APK via "):
            return line
    return "Unknown; see logs."


def friendly_failure_type(failure_type: str | None) -> str:
    return {
        "config": "tracker config",
        "version_resolve": "latest version lookup",
        "download": "APK download",
        "fingerprint": "patch fingerprint",
        "signing": "APK signing",
        "patch": "patching",
    }.get(failure_type or "", failure_type or "unknown")


def render_status_table(results, shard_index: int = 0, shard_total: int = 1) -> str:
    patched_count = sum(1 for result in results if result.status == "patched")
    skipped_count = sum(1 for result in results if result.status == "skipped_known_broken")
    no_update_count = sum(1 for result in results if result.status == "no_update")
    failed_count = sum(1 for result in results if not result.ok)
    lines = [
        "# Patch Tracker Status",
        "",
        f"Shard: {shard_index + 1}/{shard_total}",
        f"Checked apps: {len(results)}",
        f"Patched: {patched_count}",
        f"No update: {no_update_count}",
        f"Skipped known broken: {skipped_count}",
        f"Failed: {failed_count}",
        "",
        "| App | Package | Known working | Tested | Version code | Status | Failure |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        status = result.status.upper()
        lines.append(
            "| "
            + " | ".join(
                [
                    md_table_cell(result.app.name),
                    md_table_cell(f"`{result.app.package_name}`"),
                    md_table_cell(f"`{result.app.current_version}`"),
                    md_table_cell(f"`{result.candidate_version}`"),
                    md_table_cell(f"`{result.version_code}`" if result.version_code else ""),
                    md_table_cell(status),
                    md_table_cell(result.failure_type or ""),
                ]
            )
            + " |"
        )

    failed_results = [result for result in results if not result.ok and result.log]
    if failed_results:
        lines.extend(["", "## Failure Logs"])
    for result in failed_results:
        summary = html.escape(f"{result.app.name} log excerpt", quote=False)
        lines.extend(
            [
                "",
                f"<details><summary>{summary}</summary>",
                "",
                "````text",
                result.log[-2000:],
                "````",
                "",
                "</details>",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def md_table_cell(value: str) -> str:
    return value.replace("\r", "").replace("\n", "<br>").replace("|", "\\|")


if __name__ == "__main__":
    raise SystemExit(main())
