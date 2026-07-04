from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class ToolConfig:
    url: str = ""
    repo: str = ""
    asset_regex: str = ""
    prerelease: bool = False


@dataclass(frozen=True)
class TrackerConfig:
    patches_repo: str
    constants_path: str
    release_prefix: str = "tracker"
    work_dir: str = ".work"


@dataclass(frozen=True)
class SourceConfig:
    source: str
    url: str
    arch: str = "all"
    dpi: str = "nodpi anydpi auto"


@dataclass(frozen=True)
class AppConfig:
    id: str
    name: str
    package_name: str
    constant: str
    current_version: str
    candidate_version: str = "latest"
    url: str = ""
    source: str = "direct"
    apk_url: str = ""
    arch: str = "all"
    dpi: str = "nodpi anydpi auto"
    included_patches: list[str] = field(default_factory=list)
    excluded_patches: list[str] = field(default_factory=list)
    patcher_args: list[str] = field(default_factory=list)
    sources: list[SourceConfig] = field(default_factory=list)

    def resolved_sources(self) -> list[SourceConfig]:
        if self.sources:
            return self.sources
        source_url = self.apk_url or self.url
        if not source_url:
            return []
        return [SourceConfig(self.source, source_url, self.arch, self.dpi)]


@dataclass(frozen=True)
class Config:
    tracker: TrackerConfig
    cli: ToolConfig
    patches: ToolConfig
    apps: list[AppConfig]


def load_config(path: str | Path) -> Config:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    apps = []
    for app in data.get("apps", []):
        app = dict(app)
        app["sources"] = [SourceConfig(**source) for source in app.get("sources", [])]
        apps.append(AppConfig(**app))
    return Config(
        tracker=TrackerConfig(**data["tracker"]),
        cli=ToolConfig(**data["cli"]),
        patches=ToolConfig(**data["patches"]),
        apps=apps,
    )
