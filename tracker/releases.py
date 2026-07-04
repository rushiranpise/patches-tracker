from __future__ import annotations

from pathlib import Path
import re

import requests

from .build import download
from .config import ToolConfig


def resolve_tool(config: ToolConfig, path: Path, *, dry_run: bool = False) -> Path:
    if config.url:
        return download_or_skip(config.url, path, dry_run=dry_run)
    if not config.repo:
        raise ValueError("tool config needs either url or repo")
    if dry_run:
        return path

    releases_url = f"https://api.github.com/repos/{config.repo}/releases"
    response = requests.get(releases_url, timeout=60)
    response.raise_for_status()
    releases = response.json()
    asset_pattern = re.compile(config.asset_regex or r".*")

    for release in releases:
        if release.get("draft"):
            continue
        if bool(release.get("prerelease")) != config.prerelease:
            continue
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            if asset_pattern.fullmatch(name) or asset_pattern.search(name):
                return download_or_skip(asset["browser_download_url"], path, dry_run=False)
    raise RuntimeError(f"could not resolve release asset for {config.repo}")


def download_or_skip(url: str, path: Path, *, dry_run: bool = False) -> Path:
    if dry_run:
        return path
    if path.exists():
        return path
    if url.startswith("file://"):
        source = Path(url.removeprefix("file://"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.read_bytes())
        return path
    return download(url, path)
