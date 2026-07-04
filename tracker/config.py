from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shlex
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
    target_branch: str = "dev"


@dataclass(frozen=True)
class SourceConfig:
    source: str
    url: str
    arch: str = "all"
    dpi: str = "nodpi anydpi auto"
    apk_types: list[str] = field(default_factory=list)


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
    apk_types: list[str] = field(default_factory=list)
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
        return [SourceConfig(self.source, source_url, self.arch, self.dpi, self.apk_types)]


@dataclass(frozen=True)
class Config:
    tracker: TrackerConfig
    cli: ToolConfig
    patches: ToolConfig
    apps: list[AppConfig]


def load_config(path: str | Path) -> Config:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    apps = [_load_legacy_app(app) for app in data.get("apps", [])]
    reserved_tables = {"tracker", "cli", "patches", "apps"}
    for app_id, app in data.items():
        if app_id in reserved_tables or not isinstance(app, dict):
            continue
        if app.get("enabled", True) is False:
            continue
        apps.append(_load_rvb_style_app(app_id, app))
    return Config(
        tracker=TrackerConfig(**data["tracker"]),
        cli=ToolConfig(**data["cli"]),
        patches=ToolConfig(**data["patches"]),
        apps=apps,
    )


def _load_legacy_app(raw: dict) -> AppConfig:
    app = _normalize_keys(raw)
    app["apk_types"] = _list_value(app.get("apk_types", []))
    app["sources"] = [
        _load_source(source, app.get("arch", "all"), app.get("dpi", "nodpi anydpi auto"), app["apk_types"])
        for source in app.get("sources", [])
    ]
    return AppConfig(**app)


def _load_rvb_style_app(app_id: str, raw: dict) -> AppConfig:
    app = _normalize_keys(raw)
    apk_types = _list_value(app.get("apk_types", []))
    sources = []
    for source in ("direct", "github", "archive", "apkmirror", "uptodown", "apkpure", "apkcombo"):
        url = app.pop(f"{source}_dlurl", "")
        if url:
            sources.append(SourceConfig(source, url, app.get("arch", "all"), app.get("dpi", "nodpi anydpi auto"), apk_types))

    return AppConfig(
        id=app_id,
        name=app.get("app_name") or app_id,
        package_name=app["package_name"],
        constant=app["constant"],
        current_version=app["current_version"],
        candidate_version=app.get("version") or app.get("candidate_version", "latest"),
        arch=app.get("arch", "all"),
        dpi=app.get("dpi", "nodpi anydpi auto"),
        apk_types=apk_types,
        included_patches=_list_value(app.get("included_patches", [])),
        excluded_patches=_list_value(app.get("excluded_patches", [])),
        patcher_args=_list_value(app.get("patcher_args", [])),
        sources=sources,
    )


def _load_source(raw: dict, default_arch: str, default_dpi: str, default_apk_types: list[str]) -> SourceConfig:
    source = _normalize_keys(raw)
    return SourceConfig(
        source=source["source"],
        url=source["url"],
        arch=source.get("arch", default_arch),
        dpi=source.get("dpi", default_dpi),
        apk_types=_list_value(source.get("apk_types", default_apk_types)),
    )


def _normalize_keys(raw: dict) -> dict:
    return {key.replace("-", "_"): value for key, value in raw.items()}


def _list_value(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return shlex.split(value)
    return [str(value)]
