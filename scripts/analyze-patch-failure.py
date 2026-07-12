#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze non-fingerprint patch failures.")
    parser.add_argument("--apk", required=True)
    parser.add_argument("--log", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--work-dir", default=".work/patch-failure-analysis")
    args = parser.parse_args()

    report = analyze(Path(args.apk), read_text_or_literal(args.log), Path(args.work_dir))
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def analyze(apk: Path, log: str, work_dir: Path) -> dict:
    report = {
        "schema": "patches-tracker/patch-analysis/v1",
        "apk": str(apk),
        "kind": "unknown",
        "candidates": [],
        "notes": [],
    }
    lower = log.lower()
    if "gma pcam loader" in lower or "sdk version changed" in lower:
        if not apk.exists():
            report.update({"kind": "classdef_by_strings"})
            report["notes"].append("APK file does not exist.")
            return report
        report.update(analyze_classdef_by_strings(apk, work_dir, [r"/\d+\.jar", r"pcam", r"DynamiteLoader"]))
        return report
    if "carbon " in lower and ("signature not found" in lower or "string id mismatch" in lower):
        if not apk.exists():
            report.update({"kind": "carbon_hermes_offsets"})
            report["notes"].append("APK file does not exist.")
            return report
        report.update(analyze_carbon_hermes(apk, log))
        return report

    if not apk.exists():
        report["notes"].append("APK file does not exist.")
        return report

    report["notes"].append("No specialized analyzer matched this failure.")
    return report


def analyze_classdef_by_strings(apk: Path, work_dir: Path, patterns: list[str]) -> dict:
    report = {"kind": "classdef_by_strings", "candidates": [], "notes": []}
    if shutil.which("apktool") is None:
        report["notes"].append("apktool is not installed; install apktool to analyze smali strings.")
        return report
    decoded = work_dir / safe_name(apk.stem)
    if decoded.exists():
        shutil.rmtree(decoded)
    decoded.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["apktool", "d", "-f", "-r", "-o", str(decoded), str(apk)],
        text=True,
        capture_output=True,
        timeout=600,
    )
    if completed.returncode != 0:
        report["notes"].append("apktool decode failed.")
        report["notes"].append((completed.stdout + completed.stderr)[-2000:])
        return report
    regexes = [re.compile(pattern) for pattern in patterns]
    for path in decoded.glob("smali*/**/*.smali"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        strings = sorted(set(re.findall(r'const-string(?:/jumbo)?\s+\S+,\s+"([^"]+)"', text)))
        matches = [string for string in strings if any(regex.search(string) for regex in regexes)]
        if not matches:
            continue
        class_name = smali_class_name(text)
        score = 50 + sum(25 for string in matches if string.endswith(".jar")) + (20 if "DexClassLoader" in text else 0)
        report["candidates"].append(
            {
                "score": score,
                "class": class_name,
                "file": str(path),
                "strings": matches[:20],
                "reason": "class contains candidate SDK/cache jar strings",
            }
        )
    report["candidates"].sort(key=lambda item: (-item["score"], item["class"]))
    report["candidates"] = report["candidates"][:10]
    return report


def analyze_carbon_hermes(apk: Path, log: str) -> dict:
    report = {"kind": "carbon_hermes_offsets", "candidates": [], "notes": []}
    bundle = read_zip_member(apk, "assets/index.android.bundle")
    if bundle is None:
        report["notes"].append("assets/index.android.bundle not found.")
        return report
    if len(bundle) < 12 or bundle[:4] != bytes([0xC6, 0x1F, 0xBC, 0x03]):
        report["notes"].append("Carbon bundle is not Hermes bytecode.")
        return report
    version = int.from_bytes(bundle[8:12], "little")
    report["hermes_version"] = version
    failed_labels = re.findall(r"Carbon ([^.]+?) (?:signature not found|string id mismatch)", log)
    for label in sorted(set(failed_labels)):
        report["candidates"].append(
            {
                "label": label,
                "nearby_const_string_ops": find_const_string_candidates(bundle),
                "reason": "candidate Hermes const-string opcodes; manual validation required before patch-code update",
            }
        )
    if not report["candidates"]:
        report["notes"].append("No Carbon offset labels were found in the log.")
    return report


def find_const_string_candidates(bundle: bytes) -> list[dict]:
    candidates = []
    for index in range(0, max(0, len(bundle) - 5)):
        if bundle[index] != 0x37:
            continue
        string_id = bundle[index + 4] | (bundle[index + 5] << 8)
        candidates.append({"offset": f"0x{index:X}", "string_id": string_id, "register": bundle[index + 1]})
    return candidates[:200]


def smali_class_name(text: str) -> str:
    match = re.search(r"^\.class\b.*\s(L[^;]+;)", text, flags=re.MULTILINE)
    return match.group(1) if match else ""


def read_zip_member(apk: Path, member: str) -> bytes | None:
    try:
        with zipfile.ZipFile(apk) as archive:
            return archive.read(member)
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def read_text_or_literal(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    return value


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


if __name__ == "__main__":
    raise SystemExit(main())
