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
    body: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Find likely moved/renamed fingerprint targets in an APK.")
    parser.add_argument("--apk", required=True)
    parser.add_argument("--old-apk", default="", help="Known-working APK for the current Constants.kt version")
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

    old_apk = Path(args.old_apk) if args.old_apk else None
    report = analyze(apk, patches_src, log, work_dir, old_apk=old_apk)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def analyze(apk: Path, patches_src: Path, log: str, work_dir: Path, old_apk: Path | None = None) -> dict:
    failed_names = failed_fingerprint_names(log)
    fingerprints = parse_fingerprints(patches_src)
    if not failed_names:
        failed_names = infer_fingerprints_from_stacktrace(log, patches_src, fingerprints)
    selected = select_fingerprints(failed_names, fingerprints, log)
    if not selected and len(failed_names) == 1:
        selected = [
            fp
            for name, entries in fingerprints.items()
            for fp in entries
            if failed_names[0].lower() in name.lower()
        ]

    report = {
        "schema": "patches-tracker/fingerprint-analysis/v1",
        "apk": str(apk),
        "old_apk": str(old_apk) if old_apk else "",
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
    old_methods_by_file = {}
    old_text_cache = {}
    if old_apk and old_apk.exists():
        old_decoded = work_dir / safe_name(old_apk.stem)
        if old_decoded.exists():
            shutil.rmtree(old_decoded)
        completed = subprocess.run(
            ["apktool", "d", "-f", "-r", "-o", str(old_decoded), str(old_apk)],
            text=True,
            capture_output=True,
            timeout=600,
        )
        if completed.returncode == 0:
            old_smali_files = list(old_decoded.glob("smali*/**/*.smali"))
            old_methods_by_file = {path: parse_smali_methods(path) for path in old_smali_files}
            old_text_cache = {path: path.read_text(encoding="utf-8", errors="ignore") for path in old_smali_files}
        else:
            report["notes"].append("old APK decode failed.")
            report["notes"].append((completed.stdout + completed.stderr)[-2000:])
    for fp in selected:
        old_method = find_old_method(fp, old_methods_by_file)
        report["candidates"].append(analyze_fingerprint(fp, methods_by_file, text_cache, old_method))
    return report


def analyze_fingerprint(
    fp: Fingerprint,
    methods_by_file: dict[Path, list[Method]],
    text_cache: dict[Path, str],
    old_method: Method | None = None,
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
            score = score_method(fp, method, path, text_cache[path], bool(class_files), old_method)
            if score <= 0:
                continue
            candidates.append(
                {
                    "score": score,
                    "class": method.class_type,
                    "method": method.name,
                    "descriptor": method.descriptor,
                    "file": str(path),
                    "reason": candidate_reason(fp, method, bool(class_files), old_method),
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
            "oldTargetFound": old_method is not None,
        },
        "candidate_count": len(candidates),
        "top_candidates": candidates[:10],
    }


def score_method(fp: Fingerprint, method: Method, path: Path, text: str, class_matched: bool, old_method: Method | None = None) -> int:
    score = 0
    params, return_type = split_descriptor(method.descriptor)
    if not old_method and fp.method_name and is_obfuscated_method_name(fp.method_name) and not is_obfuscated_method_name(method.name):
        return 0
    if not old_method and fp.defining_class and is_obfuscated_class_type(fp.defining_class) and not is_obfuscated_class_type(method.class_type):
        return 0
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
    elif fp.defining_class and is_obfuscated_class_type(fp.defining_class) and is_obfuscated_class_type(method.class_type):
        score += 10
    if fp.method_name and is_obfuscated_method_name(fp.method_name) and is_obfuscated_method_name(method.name):
        score += 10
    if old_method:
        similarity = bytecode_similarity_score(old_method, method)
        if similarity < 20:
            return 0
        score += similarity
    return score


def candidate_reason(fp: Fingerprint, method: Method, class_matched: bool, old_method: Method | None = None) -> str:
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
    elif fp.method_name and is_obfuscated_method_name(fp.method_name) and is_obfuscated_method_name(method.name):
        bits.append("obfuscated method shape matches")
    if fp.defining_class and method.class_type != fp.defining_class and is_obfuscated_class_type(fp.defining_class) and is_obfuscated_class_type(method.class_type):
        bits.append("obfuscated class shape matches")
    if old_method:
        bits.append(f"old bytecode similarity {bytecode_similarity_score(old_method, method)}")
    return ", ".join(bits) or "shape match"


def find_old_method(fp: Fingerprint, methods_by_file: dict[Path, list[Method]]) -> Method | None:
    if not methods_by_file or not fp.defining_class or not fp.method_name:
        return None
    wanted_descriptor = "(" + "".join(fp.parameters) + ")" + fp.return_type if fp.return_type else ""
    suffix = fp.defining_class.removeprefix("L").removesuffix(";") + ".smali"
    for path, methods in methods_by_file.items():
        if not str(path).replace("\\", "/").endswith(suffix):
            continue
        for method in methods:
            if method.name == fp.method_name and (not wanted_descriptor or method.descriptor == wanted_descriptor):
                return method
    return None


def bytecode_similarity_score(old_method: Method, new_method: Method) -> int:
    old_features = method_features(old_method.body)
    new_features = method_features(new_method.body)
    score = 0
    for key, weight in (("strings", 25), ("field_refs", 20), ("method_refs", 15), ("opcodes", 10)):
        score += int(weight * jaccard(old_features[key], new_features[key]))
    old_lines = old_features["body_lines"]
    new_lines = new_features["body_lines"]
    if old_lines and new_lines:
        ratio = min(old_lines, new_lines) / max(old_lines, new_lines)
        score += int(10 * ratio)
    return score


def method_features(body: str) -> dict[str, set[str] | int]:
    instructions = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith((".", "#", ":"))
    ]
    return {
        "strings": set(re.findall(r'const-string(?:/jumbo)?\s+\S+,\s+"([^"]*)"', body)),
        "field_refs": set(re.findall(r"\s[sp]?ut[^\s]*\s+[^,]+,\s+([^\s]+)", body)),
        "method_refs": set(re.findall(r"invoke-[^\s]+\s+\{[^}]*\},\s+([^\s]+)", body)),
        "opcodes": {line.split()[0] for line in instructions if line.split()},
        "body_lines": len(instructions),
    }


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def is_obfuscated_class_type(value: str) -> bool:
    return bool(re.fullmatch(r"L[A-Za-z0-9_$]{1,5};", value or ""))


def is_obfuscated_method_name(value: str) -> bool:
    if not value or value.startswith("<"):
        return False
    return bool(re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]{0,2}", value))


def failed_fingerprint_names(log: str) -> list[str]:
    patterns = [
        r"([A-Za-z0-9_]+Fingerprint)\b",
        r"fingerprint\s+['\"]?([A-Za-z0-9_]+)['\"]?",
    ]
    names = []
    for pattern in patterns:
        for match in re.finditer(pattern, log, flags=re.IGNORECASE):
            name = match.group(1)
            normalized = re.sub(r"[^a-z0-9]+", "", name.lower())
            if normalized in {"fingerprint", "failed", "failedfingerprint", "failedfingerprints", "declaration", "declarationfingerprint"}:
                continue
            if not name.endswith("Fingerprint"):
                name += "Fingerprint"
            names.append(name)
    return sorted(set(names))


def select_fingerprints(failed_names: list[str], fingerprints: dict[str, list[Fingerprint]], log: str) -> list[Fingerprint]:
    selected = []
    preferred_segments = preferred_source_segments(log)
    for name in failed_names:
        entries = fingerprints.get(name, [])
        if not entries:
            continue
        selected.append(best_fingerprint_entry(entries, preferred_segments))
    return selected


def preferred_source_segments(log: str) -> list[str]:
    segments = []
    focused_lines = []
    for line in log.splitlines():
        if "Failed to match the fingerprint:" in line or "\tat app.template.patches." in line:
            focused_lines.append(line)
    focused_log = "\n".join(focused_lines) or log
    for package in re.findall(r"\bapp\.template\.patches\.([A-Za-z0-9_.]+)", focused_log):
        bits = [bit for bit in package.split(".") if bit and bit[0].islower()]
        for index in range(len(bits), 0, -1):
            segment = "/".join(bits[:index])
            if segment and segment not in segments:
                segments.append(segment)
    return segments


def best_fingerprint_entry(entries: list[Fingerprint], preferred_segments: list[str]) -> Fingerprint:
    def score(fp: Fingerprint) -> tuple[int, int]:
        source = fp.source_file.replace("\\", "/").lower()
        for index, segment in enumerate(preferred_segments):
            if f"/{segment.lower()}/" in source:
                return (100 - index, -len(source))
        return (0, -len(source))

    return max(entries, key=score)


def infer_fingerprints_from_stacktrace(log: str, root: Path, fingerprints: dict[str, list[Fingerprint]]) -> list[str]:
    inferred = []
    for file_name, line_text in re.findall(r"\(([A-Za-z0-9_]+\.kt):(\d+)\)", log):
        line_number = int(line_text)
        for path in root.rglob(file_name):
            names = fingerprint_names_near_line(path, line_number, set(fingerprints))
            inferred.extend(names)
    return sorted(set(inferred))


def fingerprint_names_near_line(path: Path, line_number: int, known_names: set[str]) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    if 1 <= line_number <= len(lines):
        exact_names = re.findall(r"\b([A-Za-z0-9_]+Fingerprint)\b", lines[line_number - 1])
        exact_matches = [name for name in exact_names if name in known_names]
        if exact_matches:
            return exact_matches
    start = max(0, line_number - 6)
    end = min(len(lines), line_number + 5)
    window = "\n".join(lines[start:end])
    names = re.findall(r"\b([A-Za-z0-9_]+Fingerprint)\b", window)
    return [name for name in names if name in known_names]


def parse_fingerprints(root: Path) -> dict[str, list[Fingerprint]]:
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
            defining_class = first_string_arg(body, "definingClass") or custom_class_check(body)
            method_name = first_string_arg(body, "name") or custom_method_check(body)
            fingerprints.setdefault(name, []).append(Fingerprint(
                name=name,
                defining_class=defining_class,
                method_name=method_name,
                return_type=first_string_arg(body, "returnType"),
                parameters=list_arg(body, "parameters"),
                strings=list_arg(body, "strings"),
                source_file=str(path),
            ))
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


def custom_class_check(body: str) -> str:
    match = re.search(r"\bclassDef\.type\s*==\s*\"([^\"]*)\"", body)
    return match.group(1) if match else ""


def custom_method_check(body: str) -> str:
    match = re.search(r"\bmethod\.name\s*==\s*\"([^\"]*)\"", body)
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
    for match in re.finditer(r"^\.method\b.*?\s([^\s(]+)(\([^)]*\).+?)$\n([\s\S]*?)^\.end method$", text, flags=re.MULTILINE):
        methods.append(Method(class_type, match.group(1), match.group(2).strip(), path, match.group(3)))
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
