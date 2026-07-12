from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import quote


LABEL_COLORS = {
    "bug": "d73a4a",
    "tracker": "5319e7",
    "source-failure": "fbca04",
    "patch-broken-after-update": "d93f0b",
}

LABEL_DESCRIPTIONS = {
    "bug": "Something is not working",
    "tracker": "Created or updated by patches-tracker",
    "source-failure": "APK source lookup or download problem",
    "patch-broken-after-update": "A newer app version no longer patches cleanly",
}


def run_gh(
    args: list[str],
    *,
    cwd: Path | None = None,
    dry_run: bool = False,
    token: str | None = None,
) -> str:
    cmd = ["gh", *args]
    if dry_run:
        print("+", " ".join(cmd))
        return ""
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    completed = subprocess.run(cmd, cwd=cwd, env=env, check=True, text=True, capture_output=True)
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
    token = token_for_repo(repo)
    query = f'repo:{repo} is:issue is:open in:title "{title}"'
    try:
        existing = run_gh(
            ["issue", "list", "--repo", repo, "--search", query, "--json", "number", "--jq", ".[0].number // empty"],
            dry_run=dry_run,
            token=token,
        )
    except subprocess.CalledProcessError as error:
        print(f"warning: could not look for existing issues: {error.stderr}", flush=True)
        existing = ""
    try:
        if existing:
            print(f"adding an update to issue {repo}#{existing}: {title}", flush=True)
            run_gh(
                ["api", f"repos/{repo}/issues/{existing}/comments", "-f", f"body={body}"],
                dry_run=dry_run,
                token=token,
            )
            add_issue_labels(repo, existing, labels, dry_run=dry_run, token=token)
            return
        print(f"opening issue in {repo}: {title}", flush=True)
        created = run_gh(
            ["api", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}", "--jq", ".number"],
            dry_run=dry_run,
            token=token,
        )
        if created:
            add_issue_labels(repo, created, labels, dry_run=dry_run, token=token)
    except subprocess.CalledProcessError as error:
        print(f"warning: could not open or update the issue: {error.stderr}", flush=True)


def close_resolved_failure_issue(
    repo: str,
    title: str,
    comment: str,
    *,
    dry_run: bool = False,
) -> None:
    token = token_for_repo(repo)
    query = f'repo:{repo} is:issue is:open in:title "{title}"'
    try:
        issues = run_gh(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--search",
                query,
                "--json",
                "number,title,body,labels",
            ],
            dry_run=dry_run,
            token=token,
        )
    except subprocess.CalledProcessError as error:
        print(f"warning: could not look for resolved issues: {error.stderr}", flush=True)
        return

    import json

    for issue in json.loads(issues or "[]"):
        if issue.get("title") != title:
            continue
        body = issue.get("body") or ""
        labels = {label.get("name") for label in issue.get("labels", [])}
        if "Automated report from patches-tracker" not in body and not labels.intersection({"tracker", "source-failure", "patch-broken-after-update"}):
            continue
        issue_number = str(issue["number"])
        try:
            print(f"closing resolved issue {repo}#{issue_number}: {title}", flush=True)
            run_gh(
                ["api", f"repos/{repo}/issues/{issue_number}/comments", "-f", f"body={comment}"],
                dry_run=dry_run,
                token=token,
            )
            run_gh(
                ["issue", "close", issue_number, "--repo", repo, "--reason", "completed"],
                dry_run=dry_run,
                token=token,
            )
        except subprocess.CalledProcessError as error:
            print(f"warning: could not close resolved issue {repo}#{issue_number}: {error.stderr}", flush=True)


def token_for_repo(repo: str) -> str | None:
    if repo == os.environ.get("GITHUB_REPOSITORY"):
        return os.environ.get("TRACKER_REPO_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def add_issue_labels(
    repo: str,
    issue_number: str,
    labels: list[str],
    *,
    dry_run: bool = False,
    token: str | None = None,
) -> None:
    if not labels:
        return
    ensure_issue_labels(repo, labels, dry_run=dry_run, token=token)
    try:
        run_gh(
            ["issue", "edit", issue_number, "--repo", repo, "--add-label", ",".join(labels)],
            dry_run=dry_run,
            token=token,
        )
    except subprocess.CalledProcessError as error:
        print(f"warning: could not add labels to {repo}#{issue_number}: {error.stderr}", flush=True)


def ensure_issue_labels(
    repo: str,
    labels: list[str],
    *,
    dry_run: bool = False,
    token: str | None = None,
) -> None:
    for label in labels:
        try:
            run_gh(
                [
                    "api",
                    f"repos/{repo}/labels/{quote(label, safe='')}",
                ],
                dry_run=dry_run,
                token=token,
            )
            continue
        except subprocess.CalledProcessError:
            pass
        try:
            run_gh(
                [
                    "api",
                    f"repos/{repo}/labels",
                    "-f",
                    f"name={label}",
                    "-f",
                    f"color={LABEL_COLORS.get(label, 'ededed')}",
                    "-f",
                    f"description={LABEL_DESCRIPTIONS.get(label, '')}",
                ],
                dry_run=dry_run,
                token=token,
            )
            print(f"created issue label {repo}:{label}", flush=True)
        except subprocess.CalledProcessError as error:
            print(f"warning: could not create issue label {repo}:{label}: {error.stderr}", flush=True)


def create_pull_request(
    repo_path: Path,
    repo: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    *,
    dry_run: bool = False,
) -> None:
    run_gh(
        ["pr", "create", "--repo", repo, "--head", branch, "--base", base, "--title", title, "--body", body],
        cwd=repo_path,
        dry_run=dry_run,
    )
