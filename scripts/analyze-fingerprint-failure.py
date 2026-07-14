#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile


@dataclass
class Fingerprint:
    name: str
    defining_class: str
    method_name: str
    return_type: str
    parameters: list[str]
    strings: list[str]
    opcodes: list[str]
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
    parser.add_argument("--app-id", default="", help="Tracker app id used to prefer same-app fingerprint declarations")
    parser.add_argument("--package-name", default="", help="Android package name used to prefer same-app fingerprint declarations")
    parser.add_argument("--out", default="")
    parser.add_argument("--work-dir", default=".work/fingerprint-analysis")
    args = parser.parse_args()

    apk = Path(args.apk)
    patches_src = Path(args.patches_src)
    work_dir = Path(args.work_dir)
    out = Path(args.out) if args.out else None
    log = read_text_or_literal(args.log)

    old_apk = Path(args.old_apk) if args.old_apk else None
    report = analyze(apk, patches_src, log, work_dir, old_apk=old_apk, app_id=args.app_id, package_name=args.package_name)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def analyze(
    apk: Path,
    patches_src: Path,
    log: str,
    work_dir: Path,
    old_apk: Path | None = None,
    app_id: str = "",
    package_name: str = "",
) -> dict:
    failed_names = failed_fingerprint_names(log)
    fingerprints = parse_fingerprints(patches_src)
    if not failed_names:
        failed_names = infer_fingerprints_from_stacktrace(log, patches_src, fingerprints)
    preferred_segments = preferred_source_segments(log, app_id=app_id, package_name=package_name)
    selected = select_fingerprints(failed_names, fingerprints, log, app_id=app_id, package_name=package_name)
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
        "preferred_source_segments": preferred_segments,
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
    completed, decoded_apk = decode_for_analysis(apk, decoded)
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
        completed, old_decoded_apk = decode_for_analysis(old_apk, old_decoded)
        if completed.returncode == 0:
            old_smali_files = list(old_decoded.glob("smali*/**/*.smali"))
            old_methods_by_file = {path: parse_smali_methods(path) for path in old_smali_files}
            old_text_cache = {path: path.read_text(encoding="utf-8", errors="ignore") for path in old_smali_files}
            if old_decoded_apk != old_apk:
                report["notes"].append(f"Decoded embedded old APK for analysis: {old_decoded_apk}")
        else:
            report["notes"].append("old APK decode failed.")
            report["notes"].append((completed.stdout + completed.stderr)[-2000:])
    if decoded_apk != apk:
        report["notes"].append(f"Decoded embedded APK for analysis: {decoded_apk}")
    for fp in selected:
        old_method = find_old_method(fp, old_methods_by_file)
        report["candidates"].append(analyze_fingerprint(fp, methods_by_file, text_cache, old_method))
    return report


def decode_for_analysis(apk: Path, decoded: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    completed = run_apktool_decode(apk, decoded)
    if completed.returncode != 0 or list(decoded.glob("smali*/**/*.smali")):
        return completed, apk

    embedded = extract_largest_embedded_apk(apk, decoded.parent / f"{decoded.name}-embedded.apk")
    if not embedded:
        return completed, apk

    if decoded.exists():
        shutil.rmtree(decoded)
    embedded_completed = run_apktool_decode(embedded, decoded)
    return embedded_completed, embedded


def run_apktool_decode(apk: Path, decoded: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["apktool", "d", "-f", "-r", "-o", str(decoded), str(apk)],
        text=True,
        capture_output=True,
        timeout=600,
    )


def extract_largest_embedded_apk(apk: Path, output: Path) -> Path | None:
    try:
        with zipfile.ZipFile(apk) as archive:
            apk_infos = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".apk")
            ]
            if not apk_infos:
                return None
            info = max(apk_infos, key=lambda item: item.file_size)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, output.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            return output
    except zipfile.BadZipFile:
        return None


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
            "opcodes": fp.opcodes,
            "oldTargetFound": old_method is not None,
        },
        "candidate_count": len(candidates),
        "top_candidates": candidates[:10],
    }


def score_method(fp: Fingerprint, method: Method, path: Path, text: str, class_matched: bool, old_method: Method | None = None) -> int:
    score = 0
    params, return_type = split_descriptor(method.descriptor)
    if old_method:
        old_params, old_return_type = split_descriptor(old_method.descriptor)
        if return_type != old_return_type:
            return 0
        if params != old_params:
            return 0
    else:
        if fp.method_name and is_obfuscated_method_name(fp.method_name) and not is_obfuscated_method_name(method.name):
            return 0
        if fp.defining_class and is_obfuscated_class_type(fp.defining_class) and not is_obfuscated_class_type(method.class_type):
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
    if fp.opcodes:
        opcodes = method_opcode_set(method.body)
        if not set(fp.opcodes).issubset(opcodes):
            return 0
        score += min(20, len(fp.opcodes) * 5)
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
        same_obfuscated_name = method.name == old_method.name and is_obfuscated_method_name(method.name)
        if similarity < 35 and not same_obfuscated_name:
            return 0
        if method.name == old_method.name:
            score += 30
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
        if method.name == old_method.name:
            bits.append("old method name still matches")
        if is_obfuscated_method_name(method.name):
            bits.append("obfuscated method fallback")
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
    for key, weight in (
        ("strings", 25),
        ("field_refs", 10),
        ("method_refs", 8),
        ("field_types", 12),
        ("method_protos", 15),
        ("opcodes", 10),
    ):
        score += int(weight * jaccard(old_features[key], new_features[key]))
    score += int(30 * SequenceMatcher(None, old_features["opcode_sequence"], new_features["opcode_sequence"]).ratio())
    score += int(40 * SequenceMatcher(None, old_features["normalized_instructions"], new_features["normalized_instructions"]).ratio())
    old_lines = old_features["body_lines"]
    new_lines = new_features["body_lines"]
    if old_lines and new_lines:
        ratio = min(old_lines, new_lines) / max(old_lines, new_lines)
        score += int(10 * ratio)
    return score


def method_features(body: str) -> dict[str, set[str] | list[str] | int]:
    instructions = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith((".", "#", ":"))
    ]
    field_refs = set(re.findall(r"\s[sp]?ut[^\s]*\s+[^,]+,\s+([^\s]+)", body))
    method_refs = set(re.findall(r"invoke-[^\s]+\s+\{[^}]*\},\s+([^\s]+)", body))
    return {
        "strings": set(re.findall(r'const-string(?:/jumbo)?\s+\S+,\s+"([^"]*)"', body)),
        "field_refs": field_refs,
        "method_refs": method_refs,
        "field_types": {ref.split(":", 1)[1] for ref in field_refs if ":" in ref},
        "method_protos": {"(" + ref.split("(", 1)[1] for ref in method_refs if "(" in ref},
        "opcodes": {line.split()[0] for line in instructions if line.split()},
        "opcode_sequence": [line.split()[0] for line in instructions if line.split()],
        "normalized_instructions": [normalize_instruction(line) for line in instructions],
        "body_lines": len(instructions),
}


def normalize_instruction(line: str) -> str:
    line = re.sub(r"\b[vp]\d+\b", "v#", line)
    line = re.sub(r"\+?-?[0-9a-fA-F]+h\b", "#h", line)
    line = re.sub(r"\b0x[0-9a-fA-F]+\b", "0x#", line)
    line = re.sub(r"L(?:[A-Za-z0-9_$]+/)*[A-Za-z0-9_$]+;", "L#;", line)
    line = re.sub(r"->([A-Za-z_$][A-Za-z0-9_$]{0,3})\(", "->m#(", line)
    line = re.sub(r"->([A-Za-z_$][A-Za-z0-9_$]{0,3})\s", "->f# ", line)
    return line


def method_opcode_set(body: str) -> set[str]:
    return {
        line.split()[0]
        for line in (raw.strip() for raw in body.splitlines())
        if line and not line.startswith((".", "#", ":")) and line.split()
    }


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def is_obfuscated_class_type(value: str) -> bool:
    if not value:
        return False
    parts = value.removeprefix("L").removesuffix(";").split("/")
    if not parts:
        return False
    return all(re.fullmatch(r"[A-Za-z0-9_$]{1,5}", part) for part in parts)


def is_obfuscated_method_name(value: str) -> bool:
    if not value or value.startswith("<"):
        return False
    return bool(re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]{0,2}", value))


def failed_fingerprint_names(log: str) -> list[str]:
    focused_lines = [
        line
        for line in log.splitlines()
        if "Failed to match the fingerprint:" in line or "\tat app.template.patches." in line
    ]
    focused_log = "\n".join(focused_lines)
    patterns = [
        r"([A-Za-z0-9_]+Fingerprint)\b",
        r"fingerprint\s+['\"]?([A-Za-z0-9_]+)['\"]?",
    ]
    names = fingerprint_names_in_text(focused_log, patterns) if focused_log else []
    if not names:
        names = fingerprint_names_in_text(log, patterns)
    return sorted(set(names))


def fingerprint_names_in_text(text: str, patterns: list[str]) -> list[str]:
    names = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            name = match.group(1)
            normalized = re.sub(r"[^a-z0-9]+", "", name.lower())
            if normalized in {"fingerprint", "failed", "analysis", "failedfingerprint", "failedfingerprints", "declaration", "declarationfingerprint", "analysisfingerprint"}:
                continue
            if not name.endswith("Fingerprint"):
                name += "Fingerprint"
            names.append(name)
    return names


def select_fingerprints(
    failed_names: list[str],
    fingerprints: dict[str, list[Fingerprint]],
    log: str,
    app_id: str = "",
    package_name: str = "",
) -> list[Fingerprint]:
    selected = []
    preferred_segments = preferred_source_segments(log, app_id=app_id, package_name=package_name)
    for name in failed_names:
        entries = fingerprints.get(name, [])
        if not entries:
            continue
        entries = same_app_entries(entries, preferred_segments) or entries
        selected.append(best_fingerprint_entry(entries, preferred_segments))
    return selected


def same_app_entries(entries: list[Fingerprint], preferred_segments: list[str]) -> list[Fingerprint]:
    for segment in preferred_segments:
        segment = segment.lower()
        exact = [
            fp
            for fp in entries
            if f"/app/template/patches/{segment}/" in fp.source_file.replace("\\", "/").lower()
        ]
        if exact:
            return exact
    return []


def preferred_source_segments(log: str, app_id: str = "", package_name: str = "") -> list[str]:
    segments = []
    for hint in (app_id, app_id.replace("-", ""), package_name.split(".")[-1] if package_name else ""):
        hint = re.sub(r"[^a-z0-9_]+", "", hint.lower())
        if hint and hint not in segments:
            segments.append(hint)
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
    frames = []
    for line in log.splitlines():
        match = re.search(r"\tat app\.template\.patches\.([A-Za-z0-9_.]+)\.[A-Za-z0-9_$]+\(([A-Za-z0-9_]+\.kt):(\d+)\)", line)
        if match:
            frames.append((match.group(1), match.group(2), int(match.group(3))))
    for package, file_name, line_number in frames:
        package_path = Path(*[part for part in package.split(".") if part and part[0].islower()])
        search_root = root / "app" / "template" / "patches" / package_path
        if not search_root.exists():
            search_root = root
        for path in search_root.rglob(file_name):
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
                opcodes=opcode_filters(body),
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


def opcode_filters(body: str) -> list[str]:
    values = []
    for match in re.finditer(r"\bOpcode\.([A-Z0-9_]+)\b", body):
        values.append(match.group(1).lower().replace("_", "-"))
    return sorted(set(values))


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
