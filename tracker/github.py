from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_gh(args: list[str], *, cwd: Path | None = None, dry_run: bool = False) -> str:
    cmd = ["gh", *args]
    if dry_run:
        print("+", " ".join(cmd))
        return ""
    completed = subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)
    return completed.stdout.strip()


def ensure_gh_auth() -> None:
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN must be set")


def create_or_update_failure_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    *,
    dry_run: bool = False,
) -> None:
    query = f'repo:{repo} is:issue is:open in:title "{title}"'
    existing = run_gh(
        ["issue", "list", "--repo", repo, "--search", query, "--json", "number", "--jq", ".[0].number // empty"],
        dry_run=dry_run,
    )
    label_arg = ",".join(labels)
    if existing:
        run_gh(["issue", "comment", existing, "--repo", repo, "--body", body], dry_run=dry_run)
        return
    run_gh(["issue", "create", "--repo", repo, "--title", title, "--body", body, "--label", label_arg], dry_run=dry_run)


def create_pull_request(repo_path: Path, repo: str, branch: str, title: str, body: str, *, dry_run: bool = False) -> None:
    run_gh(["pr", "create", "--repo", repo, "--head", branch, "--title", title, "--body", body], cwd=repo_path, dry_run=dry_run)
