#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess


@dataclass
class Fingerprint:
    name: str
    defining_class: str
    method_name: str
    return_type: str
    parameters: list[str]
    strings: list[str]
    source_file: str


@dataclass
class Method:
    class_type: str
    name: str
    descriptor: str
    file: Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Find likely moved/renamed fingerprint targets in an APK.")
    parser.add_argument("--apk", required=True)
    parser.add_argument("--patches-src", required=True, help="Path to patches/src/main/kotlin")
    parser.add_argument("--log", default="", help="Patch failure log text or path")
    parser.add_argument("--out", default="")
    parser.add_argument("--work-dir", default=".work/fingerprint-analysis")
    args = parser.parse_args()

    apk = Path(args.apk)
    patches_src = Path(args.patches_src)
    work_dir = Path(args.work_dir)
    out = Path(args.out) if args.out else None
    log = read_text_or_literal(args.log)

    report = analyze(apk, patches_src, log, work_dir)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def analyze(apk: Path, patches_src: Path, log: str, work_dir: Path) -> dict:
    failed_names = failed_fingerprint_names(log)
    fingerprints = parse_fingerprints(patches_src)
    selected = [fingerprints[name] for name in failed_names if name in fingerprints]
    if not selected and len(failed_names) == 1:
        selected = [fp for name, fp in fingerprints.items() if failed_names[0].lower() in name.lower()]

    report = {
        "schema": "patches-tracker/fingerprint-analysis/v1",
        "apk": str(apk),
        "failed_fingerprints": failed_names,
        "analyzed_fingerprints": [fp.name for fp in selected],
        "candidates": [],
        "notes": [],
    }
    if not selected:
        report["notes"].append("No matching Fingerprint declaration was found in patches source.")
        return report
    if not apk.exists():
        report["notes"].append("APK file does not exist.")
        return report
    if shutil.which("apktool") is None:
        report["notes"].append("apktool is not installed; install apktool to analyze smali candidates.")
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

    smali_files = list(decoded.glob("smali*/**/*.smali"))
    methods_by_file = {path: parse_smali_methods(path) for path in smali_files}
    text_cache = {path: path.read_text(encoding="utf-8", errors="ignore") for path in smali_files}
    for fp in selected:
        report["candidates"].append(analyze_fingerprint(fp, methods_by_file, text_cache))
    return report


def analyze_fingerprint(
    fp: Fingerprint,
    methods_by_file: dict[Path, list[Method]],
    text_cache: dict[Path, str],
) -> dict:
    class_files = []
    if fp.defining_class:
        suffix = fp.defining_class.removeprefix("L").removesuffix(";") + ".smali"
        class_files = [path for path in methods_by_file if str(path).replace("\\", "/").endswith(suffix)]

    if not class_files and fp.strings:
        class_files = [
            path
            for path, text in text_cache.items()
            if all(string in text for string in fp.strings)
        ]

    candidates = []
    search_files = class_files or list(methods_by_file)
    for path in search_files:
        for method in methods_by_file[path]:
            score = score_method(fp, method, path, text_cache[path], bool(class_files))
            if score <= 0:
                continue
            candidates.append(
                {
                    "score": score,
                    "class": method.class_type,
                    "method": method.name,
                    "descriptor": method.descriptor,
                    "file": str(path),
                    "reason": candidate_reason(fp, method, bool(class_files)),
                }
            )

    candidates.sort(key=lambda item: (-item["score"], item["class"], item["method"]))
    return {
        "fingerprint": fp.name,
        "source_file": fp.source_file,
        "current": {
            "definingClass": fp.defining_class,
            "name": fp.method_name,
            "returnType": fp.return_type,
            "parameters": fp.parameters,
            "strings": fp.strings,
        },
        "candidate_count": len(candidates),
        "top_candidates": candidates[:10],
    }


def score_method(fp: Fingerprint, method: Method, path: Path, text: str, class_matched: bool) -> int:
    score = 0
    params, return_type = split_descriptor(method.descriptor)
    if fp.return_type and return_type == fp.return_type:
        score += 25
    elif fp.return_type:
        return 0
    if fp.parameters and params == fp.parameters:
        score += 25
    elif fp.parameters == [] and params == []:
        score += 20
    elif fp.parameters:
        return 0
    if class_matched:
        score += 30
    if fp.method_name and method.name == fp.method_name:
        score += 15
    if fp.strings and all(string in text for string in fp.strings):
        score += 20
    if fp.defining_class and method.class_type == fp.defining_class:
        score += 20
    return score


def candidate_reason(fp: Fingerprint, method: Method, class_matched: bool) -> str:
    bits = []
    params, return_type = split_descriptor(method.descriptor)
    if fp.return_type and return_type == fp.return_type:
        bits.append("return type matches")
    if fp.parameters == params:
        bits.append("parameters match")
    if class_matched:
        bits.append("class/string context matches")
    if fp.method_name and method.name == fp.method_name:
        bits.append("method name still matches")
    return ", ".join(bits) or "shape match"


def failed_fingerprint_names(log: str) -> list[str]:
    patterns = [
        r"([A-Za-z0-9_]+Fingerprint)\b",
        r"fingerprint\s+['\"]?([A-Za-z0-9_]+)['\"]?",
    ]
    names = []
    for pattern in patterns:
        for match in re.finditer(pattern, log, flags=re.IGNORECASE):
            name = match.group(1)
            if name.lower() in {"fingerprint"}:
                continue
            if not name.endswith("Fingerprint"):
                name += "Fingerprint"
            names.append(name)
    return sorted(set(names))


def parse_fingerprints(root: Path) -> dict[str, Fingerprint]:
    fingerprints = {}
    for path in root.rglob("*.kt"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        pattern = (
            r"(?:object\s+([A-Za-z0-9_]+Fingerprint)\s*:\s*Fingerprint\s*\("
            r"|(?:internal\s+|private\s+)?val\s+([A-Za-z0-9_]+Fingerprint)\s*(?::\s*Fingerprint)?\s*=\s*Fingerprint\()"
        )
        for match in re.finditer(pattern, text):
            name = match.group(1) or match.group(2)
            body = balanced_call_body(text, match.end() - 1)
            if not body:
                continue
            fingerprints[name] = Fingerprint(
                name=name,
                defining_class=first_string_arg(body, "definingClass"),
                method_name=first_string_arg(body, "name"),
                return_type=first_string_arg(body, "returnType"),
                parameters=list_arg(body, "parameters"),
                strings=list_arg(body, "strings"),
                source_file=str(path),
            )
    return fingerprints


def balanced_call_body(text: str, open_paren_index: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    for index in range(open_paren_index, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_index + 1 : index]
    return ""


def first_string_arg(body: str, name: str) -> str:
    match = re.search(rf"\b{name}\s*=\s*\"([^\"]*)\"", body)
    return match.group(1) if match else ""


def list_arg(body: str, name: str) -> list[str]:
    empty = re.search(rf"\b{name}\s*=\s*emptyList\s*\(", body)
    if empty:
        return []
    match = re.search(rf"\b{name}\s*=\s*listOf\s*\(([\s\S]*?)\)", body)
    if not match:
        return []
    return re.findall(r'"([^"]*)"', match.group(1))


def parse_smali_methods(path: Path) -> list[Method]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    class_match = re.search(r"^\.class\b.*\s(L[^;]+;)", text, flags=re.MULTILINE)
    class_type = class_match.group(1) if class_match else ""
    methods = []
    for match in re.finditer(r"^\.method\b.*?\s([^\s(]+)(\([^)]*\).+)$", text, flags=re.MULTILINE):
        methods.append(Method(class_type, match.group(1), match.group(2).strip(), path))
    return methods


def split_descriptor(descriptor: str) -> tuple[list[str], str]:
    match = re.match(r"\((.*?)\)(.+)", descriptor)
    if not match:
        return [], ""
    return parse_types(match.group(1)), match.group(2)


def parse_types(raw: str) -> list[str]:
    types = []
    index = 0
    while index < len(raw):
        start = index
        while index < len(raw) and raw[index] == "[":
            index += 1
        if index >= len(raw):
            break
        if raw[index] == "L":
            end = raw.find(";", index)
            if end == -1:
                break
            types.append(raw[start : end + 1])
            index = end + 1
        else:
            types.append(raw[start : index + 1])
            index += 1
    return types


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
