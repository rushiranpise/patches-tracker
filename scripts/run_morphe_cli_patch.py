#!/usr/bin/env python3
"""Run Morphe CLI against an APK/APKS and capture patcher logs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / "allaboutmorphe" / "MorpheApp" / "morphe-cli"
DEFAULT_RUN_DIR = REPO_ROOT / "build" / "morphe-cli-runs"
DEFAULT_CLI_CACHE_DIR = REPO_ROOT / "build" / "morphe-cli-runner"
CORE_COMMANDS = ("java", "git")
APK_TOOL_COMMANDS = ("apktool", "jadx", "rg", "adb")
WINGET_PACKAGES = {
    "java": "EclipseAdoptium.Temurin.21.JDK",
    "git": "Git.Git",
    "rg": "BurntSushi.ripgrep.MSVC",
    "apktool": "iBotPeaches.Apktool",
    "jadx": "JADX.JADX",
}
PYTHON_APK_PACKAGES = (
    "androguard",
    "capstone",
    "apkutils2",
    "apksigcopier",
    "pysmali",
    "smali",
    "droidlysis",
)


def newest_file(pattern: str, root: Path) -> Path | None:
    files = [path for path in root.glob(pattern) if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def run_streamed(command: list[str], cwd: Path, log_file: Path) -> int:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8", errors="replace") as log:
        log.write(f"$ {format_command(command)}\n\n")
        log.flush()

        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            log.write(line)
            log.flush()
        return process.wait()


def format_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return " ".join(shlex.quote(part) for part in command)


def run_quiet(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(command, cwd=str(cwd), text=True)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def winget_install(command: str) -> bool:
    package = WINGET_PACKAGES.get(command)
    if not package or not command_exists("winget"):
        return False

    completed = subprocess.run(
        [
            "winget",
            "install",
            "--id",
            package,
            "--exact",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ],
        text=True,
    )
    return completed.returncode == 0


def install_python_apk_packages() -> bool:
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", *PYTHON_APK_PACKAGES],
        text=True,
    )
    return completed.returncode == 0


def doctor(bootstrap_tools: bool, bootstrap_python_tools: bool) -> int:
    missing_core = []
    missing_optional = []

    for command in CORE_COMMANDS:
        if not command_exists(command):
            if bootstrap_tools and os.name == "nt" and winget_install(command):
                continue
            missing_core.append(command)

    for command in APK_TOOL_COMMANDS:
        if not command_exists(command):
            if bootstrap_tools and os.name == "nt" and winget_install(command):
                continue
            missing_optional.append(command)

    gradlew = REPO_ROOT / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if not gradlew.exists():
        missing_core.append(str(gradlew))

    if bootstrap_python_tools:
        install_python_apk_packages()

    print("core:", "ok" if not missing_core else "missing " + ", ".join(missing_core))
    print("apk tools:", "ok" if not missing_optional else "missing " + ", ".join(missing_optional))
    print("python apk packages:", "install attempted" if bootstrap_python_tools else "skipped")
    return 1 if missing_core else 0


def build_patches() -> None:
    gradlew = REPO_ROOT / ("gradlew.bat" if os.name == "nt" else "gradlew")
    run_quiet([str(gradlew), "buildandroid"], REPO_ROOT)


def build_cli() -> None:
    gradlew = CLI_DIR / ("gradlew.bat" if os.name == "nt" else "gradlew")
    run_quiet([str(gradlew), "shadowJar"], CLI_DIR)


def download_cli() -> Path:
    DEFAULT_CLI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        "https://api.github.com/repos/MorpheApp/morphe-cli/releases/latest",
        headers={"User-Agent": "morphe-cli-runner"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.loads(response.read().decode("utf-8"))

    assets = release.get("assets", [])
    jar_asset = next(
        (
            asset for asset in assets
            if asset.get("name", "").endswith("-all.jar") or asset.get("name", "").endswith(".jar")
        ),
        None,
    )
    if jar_asset is None:
        raise SystemExit("latest Morphe CLI release has no jar asset")

    jar_name = jar_asset["name"]
    jar_url = jar_asset["browser_download_url"]
    target = DEFAULT_CLI_CACHE_DIR / jar_name
    if target.exists() and target.stat().st_size > 0:
        return target

    print(f"downloading cli: {jar_url}")
    with urllib.request.urlopen(jar_url, timeout=300) as response, target.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    return target


def resolve_patches_mpp(explicit: str | None, build: bool) -> Path:
    if build:
        build_patches()

    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"patches mpp not found: {path}")
        return path

    path = newest_file("patches/build/libs/*.mpp", REPO_ROOT)
    if path is None:
        raise SystemExit("patches mpp not found; run with --build-patches")
    return path


def resolve_cli_jar(explicit: str | None, auto_build: bool, auto_download: bool) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"cli jar not found: {path}")
        return path

    path = newest_file("build/libs/*-all.jar", CLI_DIR) or newest_file("*.jar", DEFAULT_CLI_CACHE_DIR)
    if path is None and auto_download:
        path = download_cli()
    if path is None and auto_build:
        build_cli()
        path = newest_file("build/libs/*-all.jar", CLI_DIR)

    if path is None:
        raise SystemExit("morphe-cli jar not found; run with --download-cli or --build-cli")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Morphe CLI with the local patch bundle and capture output.",
    )
    parser.add_argument("--apk", help="Input .apk/.apks/.apkm/.xapk")
    parser.add_argument("--patch", action="append", default=[], help="Patch name to enable; repeatable")
    parser.add_argument("--disable", action="append", default=[], help="Patch name to disable; repeatable")
    parser.add_argument("--options-file", help="Morphe options JSON")
    parser.add_argument("--patches-mpp", help="Patch bundle path; defaults to newest patches/build/libs/*.mpp")
    parser.add_argument("--cli-jar", help="Morphe CLI jar path; defaults to newest morphe-cli build/libs/*-all.jar")
    parser.add_argument("--out-dir", default=str(DEFAULT_RUN_DIR), help="Run log/output directory")
    parser.add_argument("--exclusive", action="store_true", help="Only run patches passed with --patch")
    parser.add_argument("--force", action="store_true", help="Ignore version compatibility")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue after failed patches")
    parser.add_argument("--bytecode-mode", choices=["FULL", "STRIP_SAFE", "STRIP_FAST"])
    parser.add_argument("--build-patches", action="store_true", default=True, help="Build patch bundle first")
    parser.add_argument("--no-build-patches", action="store_false", dest="build_patches")
    parser.add_argument("--build-cli", action="store_true", help="Build CLI jar if missing")
    parser.add_argument("--download-cli", action="store_true", default=True, help="Download latest public CLI jar if missing")
    parser.add_argument("--no-download-cli", action="store_false", dest="download_cli")
    parser.add_argument("--sign", action="store_true", help="Sign output APK; default is unsigned")
    parser.add_argument("--doctor", action="store_true", help="Check local tools and exit")
    parser.add_argument("--no-doctor-check", action="store_true", help="Skip environment doctor before patching")
    parser.add_argument("--bootstrap-tools", action="store_true", help="Attempt Windows winget installs for missing core APK tools")
    parser.add_argument("--bootstrap-python-tools", action="store_true", help="Install common Python APK libraries with pip --user")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.doctor:
        return doctor(args.bootstrap_tools, args.bootstrap_python_tools)

    if not args.apk:
        raise SystemExit("--apk is required unless --doctor is used")

    if not args.no_doctor_check and doctor(args.bootstrap_tools, False) != 0:
        raise SystemExit("missing required core tools; rerun with --doctor or --bootstrap-tools")

    apk = Path(args.apk).expanduser().resolve()
    if not apk.exists():
        raise SystemExit(f"apk not found: {apk}")

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in apk.stem)
    log_file = out_dir / f"{safe_name}-{stamp}.log"
    result_file = out_dir / f"{safe_name}-{stamp}.result.json"
    output_apk = out_dir / f"{safe_name}-{stamp}-patched.apk"

    patches_mpp = resolve_patches_mpp(args.patches_mpp, args.build_patches)
    cli_jar = resolve_cli_jar(args.cli_jar, args.build_cli, args.download_cli)

    command = [
        "java",
        "-jar",
        str(cli_jar),
        "patch",
        "--patches",
        str(patches_mpp),
        "--out",
        str(output_apk),
        "--result-file",
        str(result_file),
    ]
    if not args.sign:
        command.append("--unsigned")
    if args.exclusive:
        command.append("--exclusive")
    if args.force:
        command.append("--force")
    if args.continue_on_error:
        command.append("--continue-on-error")
    if args.bytecode_mode:
        command.extend(["--bytecode-mode", args.bytecode_mode])
    if args.options_file:
        command.extend(["--options-file", str(Path(args.options_file).expanduser().resolve())])
    for patch in args.patch:
        command.extend(["--enable", patch])
    for patch in args.disable:
        command.extend(["--disable", patch])
    command.append(str(apk))

    print(f"log: {log_file}")
    print(f"result: {result_file}")
    print(f"out: {output_apk}")
    return run_streamed(command, REPO_ROOT, log_file)


if __name__ == "__main__":
    raise SystemExit(main())
