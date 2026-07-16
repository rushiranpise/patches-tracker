#!/usr/bin/env python3
"""
Assist Morphe patch updates after an app update.

Input:
  - old decompiled APK tree that the patch was built against
  - new decompiled APK tree
  - old working Kotlin patch file(s)

Output:
  - Markdown/JSON reports with class and method remap suggestions
  - optional safe rewritten patch copies with updated class descriptors/AppTarget

This is a static assistant, not a magic patcher. It is strongest for apktool smali
trees because Morphe fingerprints usually target smali descriptors. It still
reports manifest/version/string info for jadx-style source trees.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


DESC_RE = re.compile(r"L[A-Za-z0-9_/$.-]+;")
CLASS_RE = re.compile(r"^\.class\b.*\s(?P<desc>L[^;]+;)", re.MULTILINE)
SUPER_RE = re.compile(r"^\.super\s+(?P<desc>L[^;]+;)", re.MULTILINE)
IMPLEMENTS_RE = re.compile(r"^\.implements\s+(?P<desc>L[^;]+;)", re.MULTILINE)
METHOD_RE = re.compile(r"(?ms)^\.method\b(?P<decl>.*?)\n(?P<body>.*?)^\.end method")
METHOD_DECL_LINE_RE = re.compile(r"^\.method\b(?P<decl>.*)$", re.MULTILINE)
METHOD_SIG_RE = re.compile(r"(?P<name>[^\s(]+)\((?P<params>[^)]*)\)(?P<ret>\S+)\s*$")
CONST_STRING_RE = re.compile(r'const-string(?:/jumbo)?\s+\S+,\s+"((?:\\.|[^"\\])*)"')
INVOKE_RE = re.compile(r"invoke-\S+\s+\{[^}]*\},\s+(L[^;]+;)->([^\s(]+)\(([^)]*)\)(\S+)")
FIELD_REF_RE = re.compile(r"(?:[is][gp]et|[is][gp]ut)(?:-\S+)?\s+[^,]+,\s+(L[^;]+;)->([^:\s]+):(\S+)")
LITERAL_RE = re.compile(r"\b(?:const(?:/\d+)?|const-wide(?:/\d+)?)\s+\S+,\s+(-?0x[0-9A-Fa-f]+|-?\d+)")
KOTLIN_STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
FP_CLASS_RE = re.compile(r'definingClass\s*=\s*"([^"]+)"')
FP_NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')
FP_STRINGS_RE = re.compile(r"strings\s*=\s*listOf\s*\((.*?)\)", re.DOTALL)
METHOD_CALL_RE = re.compile(
    r'methodCall\s*\((?P<body>.*?)\)',
    re.DOTALL,
)
APP_TARGET_RE = re.compile(r'AppTarget\s*\(\s*"([^"]+)"')


@dataclass
class MethodInfo:
    class_desc: str
    name: str
    params: str
    ret: str
    decl: str
    strings: set[str] = field(default_factory=set)
    invokes: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)
    literals: set[str] = field(default_factory=set)

    @property
    def proto(self) -> str:
        return f"({self.params}){self.ret}"

    @property
    def full(self) -> str:
        return f"{self.class_desc}->{self.name}{self.proto}"


@dataclass
class ClassInfo:
    desc: str
    path: Path
    super_desc: str = ""
    interfaces: tuple[str, ...] = ()
    methods: list[MethodInfo] = field(default_factory=list)
    strings: set[str] = field(default_factory=set)
    invokes: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)

    @property
    def simple_name(self) -> str:
        return self.desc.rstrip(";").split("/")[-1]

    @property
    def package(self) -> str:
        parts = self.desc.rstrip(";").split("/")
        return "/".join(parts[:-1])


@dataclass
class ManifestInfo:
    package_name: str = ""
    version_name: str = ""
    version_code: str = ""


@dataclass
class DecompiledIndex:
    root: Path
    manifest: ManifestInfo
    classes: dict[str, ClassInfo]
    string_to_classes: dict[str, set[str]]
    proto_to_classes: dict[str, set[str]]
    global_strings: set[str]
    source_text_files: list[Path]
    deep_cache: dict[str, ClassInfo] = field(default_factory=dict)


@dataclass
class ClassMap:
    old_desc: str
    new_desc: str
    score: float
    confidence: str
    reason: str


@dataclass
class MethodMap:
    old_method: str
    new_method: str
    score: float
    confidence: str
    reason: str


@dataclass
class FingerprintRef:
    patch_file: Path
    class_desc: str
    method_name: str | None
    strings: tuple[str, ...]
    kind: str
    offset: int
    return_type: str = ""
    parameters: tuple[str, ...] = ()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def decode_literal(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            result.append(char)
            index += 1
            continue

        escaped = value[index + 1]
        if escaped == "u" and index + 5 < len(value):
            hex_value = value[index + 2:index + 6]
            try:
                result.append(chr(int(hex_value, 16)))
                index += 6
                continue
            except ValueError:
                pass

        result.append({
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "b": "\b",
            "\"": "\"",
            "'": "'",
            "\\": "\\",
            "$": "$",
        }.get(escaped, escaped))
        index += 2

    return "".join(result)


def norm_string(value: str) -> str:
    return value.strip()


def useful_string(value: str) -> bool:
    value = value.strip()
    if len(value) < 3:
        return False
    if value.startswith("L") and value.endswith(";"):
        return False
    if value in {"true", "false", "null", "0", "1"}:
        return False
    return True


def parse_apktool_yml(root: Path) -> tuple[str, str]:
    apktool_yml = root / "apktool.yml"
    if not apktool_yml.exists():
        return "", ""

    text = read_text(apktool_yml)
    version_name = re.search(r"(?m)^\s*versionName:\s*['\"]?([^'\"\r\n]+)", text)
    version_code = re.search(r"(?m)^\s*versionCode:\s*['\"]?([^'\"\r\n]+)", text)
    return (
        version_name.group(1).strip() if version_name else "",
        version_code.group(1).strip() if version_code else "",
    )


def parse_manifest(root: Path) -> ManifestInfo:
    yml_version_name, yml_version_code = parse_apktool_yml(root)
    manifest = root / "AndroidManifest.xml"
    if not manifest.exists():
        return ManifestInfo(version_name=yml_version_name, version_code=yml_version_code)

    text = read_text(manifest)
    try:
        xml = ET.fromstring(text)
    except ET.ParseError:
        package = re.search(r'\bpackage="([^"]+)"', text)
        version_name = re.search(r'\bandroid:versionName="([^"]+)"', text)
        version_code = re.search(r'\bandroid:versionCode="([^"]+)"', text)
        return ManifestInfo(
            package.group(1) if package else "",
            version_name.group(1) if version_name else yml_version_name,
            version_code.group(1) if version_code else yml_version_code,
        )

    android_ns = "{http://schemas.android.com/apk/res/android}"
    return ManifestInfo(
        xml.attrib.get("package", ""),
        xml.attrib.get(android_ns + "versionName", "") or yml_version_name,
        xml.attrib.get(android_ns + "versionCode", "") or yml_version_code,
    )


def parse_method(class_desc: str, decl: str, body: str) -> MethodInfo | None:
    compact_decl = " ".join(decl.split())
    match = METHOD_SIG_RE.search(compact_decl)
    if not match:
        return None

    strings = {norm_string(decode_literal(s)) for s in CONST_STRING_RE.findall(body)}
    strings = {s for s in strings if useful_string(s)}
    invokes = {f"{owner}->{name}({params}){ret}" for owner, name, params, ret in INVOKE_RE.findall(body)}
    fields = {f"{owner}->{name}:{typ}" for owner, name, typ in FIELD_REF_RE.findall(body)}
    literals = set(LITERAL_RE.findall(body))
    return MethodInfo(
        class_desc=class_desc,
        name=match.group("name"),
        params=match.group("params"),
        ret=match.group("ret"),
        decl=compact_decl,
        strings=strings,
        invokes=invokes,
        fields=fields,
        literals=literals,
    )


def parse_smali(path: Path, deep: bool = False) -> ClassInfo | None:
    text = read_text(path)
    class_match = CLASS_RE.search(text)
    if not class_match:
        return None

    desc = class_match.group("desc")
    super_match = SUPER_RE.search(text)
    interfaces = tuple(IMPLEMENTS_RE.findall(text))
    info = ClassInfo(
        desc=desc,
        path=path,
        super_desc=super_match.group("desc") if super_match else "",
        interfaces=interfaces,
    )

    class_strings = {norm_string(decode_literal(s)) for s in CONST_STRING_RE.findall(text)}
    info.strings.update(s for s in class_strings if useful_string(s))

    if deep:
        for method_match in METHOD_RE.finditer(text):
            method = parse_method(desc, method_match.group("decl"), method_match.group("body"))
            if method:
                info.methods.append(method)
                info.invokes.update(method.invokes)
                info.fields.update(method.fields)
    else:
        for method_match in METHOD_DECL_LINE_RE.finditer(text):
            method = parse_method(desc, method_match.group("decl"), "")
            if method:
                info.methods.append(method)
    return info


def smali_path_for_desc(root: Path, desc: str) -> Path | None:
    if not desc.startswith("L") or not desc.endswith(";"):
        return None
    rel = Path(*desc[1:-1].split("/")).with_suffix(".smali")
    for smali_root in root.glob("smali*"):
        candidate = smali_root / rel
        if candidate.exists():
            return candidate
    return None


def rg_files_containing(root: Path, value: str, limit: int) -> list[Path]:
    rg = shutil.which("rg")
    if not rg or not value:
        return []
    try:
        result = subprocess.run(
            [rg, "-l", "-F", "--glob", "*.smali", "--", value, str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    files = []
    for line in result.stdout.splitlines():
        path = Path(line.strip())
        if path.exists():
            files.append(path)
        if len(files) >= limit:
            break
    return files


def rg_files_containing_any(root: Path, values: Iterable[str], limit: int) -> list[Path]:
    rg = shutil.which("rg")
    patterns = [value for value in values if value]
    if not rg or not patterns:
        return []
    args = [rg, "-l", "-F", "--glob", "*.smali"]
    for value in patterns:
        args.extend(["-e", value])
    args.extend(["--", str(root)])
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    files = []
    for line in result.stdout.splitlines():
        path = Path(line.strip())
        if path.exists():
            files.append(path)
        if len(files) >= limit:
            break
    return files


def fallback_files_containing(root: Path, value: str, limit: int) -> list[Path]:
    files = []
    for path in root.rglob("*.smali"):
        try:
            if value in read_text(path):
                files.append(path)
        except OSError:
            continue
        if len(files) >= limit:
            break
    return files


def files_containing(root: Path, value: str, limit: int = 50) -> list[Path]:
    files = rg_files_containing(root, value, limit)
    return files if files else fallback_files_containing(root, value, limit)


def build_index(
    root: Path,
    wanted_descs: set[str] | None = None,
    probe_strings: set[str] | None = None,
    candidate_strings: set[str] | None = None,
) -> DecompiledIndex:
    classes: dict[str, ClassInfo] = {}
    string_to_classes: dict[str, set[str]] = defaultdict(set)
    proto_to_classes: dict[str, set[str]] = defaultdict(set)
    global_strings: set[str] = set()

    smali_files: list[Path]
    if wanted_descs is None:
        smali_files = list(root.rglob("*.smali"))
    else:
        smali_files = []
        seen: set[Path] = set()
        for desc in wanted_descs:
            path = smali_path_for_desc(root, desc)
            if path and path not in seen:
                smali_files.append(path)
                seen.add(path)
        candidates = sorted(candidate_strings or (), key=len, reverse=True)[:80]
        for path in rg_files_containing_any(root, candidates, limit=800):
            if path not in seen:
                smali_files.append(path)
                seen.add(path)

    for path in smali_files:
        info = parse_smali(path, deep=False)
        if not info:
            continue
        classes[info.desc] = info
        for value in info.strings:
            string_to_classes[value].add(info.desc)
            global_strings.add(value)
        for method in info.methods:
            proto_to_classes[method.proto].add(info.desc)

    for value in probe_strings or ():
        if value in global_strings:
            continue
        if files_containing(root, value, limit=1):
            global_strings.add(value)

    source_text_files = []
    if wanted_descs is None and not smali_files:
        for suffix in ("*.java", "*.kt"):
            source_text_files.extend(root.rglob(suffix))
        for path in source_text_files:
            for value in KOTLIN_STRING_RE.findall(read_text(path)):
                decoded = norm_string(decode_literal(value))
                if useful_string(decoded):
                    global_strings.add(decoded)

    return DecompiledIndex(
        root=root,
        manifest=parse_manifest(root),
        classes=classes,
        string_to_classes=string_to_classes,
        proto_to_classes=proto_to_classes,
        global_strings=global_strings,
        source_text_files=source_text_files,
    )


def deep_class(index: DecompiledIndex, desc: str) -> ClassInfo | None:
    if desc in index.deep_cache:
        return index.deep_cache[desc]
    info = index.classes.get(desc)
    if not info:
        return None
    parsed = parse_smali(info.path, deep=True) or info
    index.deep_cache[desc] = parsed
    return parsed


def weighted_string_score(strings: Iterable[str], new_index: DecompiledIndex) -> float:
    score = 0.0
    for value in strings:
        df = max(1, len(new_index.string_to_classes.get(value, ())))
        length_bonus = min(3.0, len(value) / 18.0)
        score += (8.0 + length_bonus) / math.sqrt(df)
    return score


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def confidence(score: float, margin: float) -> str:
    if score >= 65 and margin >= 15:
        return "high"
    if score >= 35 and margin >= 8:
        return "medium"
    if score >= 18:
        return "low"
    return "none"


def rank_conf(value: str) -> int:
    return {"exact": 4, "high": 3, "medium": 2, "low": 1, "none": 0}.get(value, 0)


def class_score(old: ClassInfo, new: ClassInfo, new_index: DecompiledIndex) -> tuple[float, str]:
    common_strings = old.strings & new.strings
    common_invokes = old.invokes & new.invokes
    common_fields = old.fields & new.fields
    old_protos = {m.proto for m in old.methods}
    new_protos = {m.proto for m in new.methods}

    score = weighted_string_score(common_strings, new_index)
    score += 12.0 * jaccard(old_protos, new_protos)
    score += 8.0 * jaccard(old.invokes, new.invokes)
    score += 5.0 * jaccard(old.fields, new.fields)
    if old.simple_name == new.simple_name:
        score += 7.0
    if old.super_desc and old.super_desc == new.super_desc:
        score += 4.0
    if old.interfaces and set(old.interfaces) == set(new.interfaces):
        score += 4.0
    if old.package and old.package == new.package:
        score += 2.0

    reason = (
        f"strings={len(common_strings)} protos={len(old_protos & new_protos)} "
        f"invokes={len(common_invokes)} fields={len(common_fields)}"
    )
    return score, reason


def class_candidates(old: ClassInfo, old_index: DecompiledIndex, new_index: DecompiledIndex, limit: int) -> list[str]:
    candidates: Counter[str] = Counter()
    rare_strings = sorted(
        (s for s in old.strings if s in new_index.string_to_classes),
        key=lambda s: (len(new_index.string_to_classes[s]), -len(s)),
    )[:80]
    for value in rare_strings:
        df = len(new_index.string_to_classes[value])
        if df <= 40:
            for desc in new_index.string_to_classes[value]:
                candidates[desc] += max(1, 40 - df)

    if not candidates:
        for method in old.methods[:80]:
            for desc in new_index.proto_to_classes.get(method.proto, ()):
                candidates[desc] += 1

    if old.desc in new_index.classes:
        candidates[old.desc] += 999

    return [desc for desc, _ in candidates.most_common(limit)]


def map_class(old_desc: str, old_index: DecompiledIndex, new_index: DecompiledIndex, limit: int = 80) -> ClassMap | None:
    old = old_index.classes.get(old_desc)
    if not old:
        return None

    scored: list[tuple[float, str, str]] = []
    for new_desc in class_candidates(old, old_index, new_index, limit):
        new = new_index.classes[new_desc]
        score, reason = class_score(old, new, new_index)
        scored.append((score, new_desc, reason))
    if not scored:
        if old_desc in new_index.classes:
            return ClassMap(old_desc, old_desc, 999.0, "exact", "descriptor still exists")
        return ClassMap(old_desc, "", 0.0, "none", "no candidate")

    scored.sort(reverse=True, key=lambda item: item[0])
    best_score, best_desc, reason = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if old_desc in new_index.classes:
        exact = new_index.classes[old_desc]
        exact_score, exact_reason = class_score(old, exact, new_index)
        if best_desc != old_desc and best_score >= max(18.0, exact_score + 10.0):
            conf = confidence(best_score, best_score - max(second_score, exact_score))
            return ClassMap(
                old_desc,
                best_desc,
                best_score,
                conf,
                reason + f" stale-exact={exact_score:.1f} ({exact_reason})",
            )
        return ClassMap(old_desc, old_desc, 999.0, "exact", "descriptor still exists")

    conf = confidence(best_score, best_score - second_score)
    return ClassMap(old_desc, best_desc, best_score, conf, reason + f" margin={best_score - second_score:.1f}")


def method_score(old: MethodInfo, new: MethodInfo) -> tuple[float, str]:
    score = 0.0
    if old.proto == new.proto:
        score += 35.0
    if old.name == new.name:
        score += 20.0
    score += 20.0 * jaccard(old.strings, new.strings)
    score += 12.0 * jaccard(old.invokes, new.invokes)
    score += 8.0 * jaccard(old.fields, new.fields)
    score += 5.0 * jaccard(old.literals, new.literals)
    reason = (
        f"proto={old.proto == new.proto} strings={len(old.strings & new.strings)} "
        f"invokes={len(old.invokes & new.invokes)} literals={len(old.literals & new.literals)}"
    )
    return score, reason


def map_method(old: MethodInfo, new_class: ClassInfo) -> MethodMap:
    for method in new_class.methods:
        if method.name == old.name and method.proto == old.proto:
            return MethodMap(old.full, method.full, 999.0, "exact", "same name and proto")

    candidates = [m for m in new_class.methods if m.proto == old.proto] or new_class.methods
    scored = []
    for method in candidates:
        score, reason = method_score(old, method)
        scored.append((score, method, reason))
    if not scored:
        return MethodMap(old.full, "", 0.0, "none", "no candidate")

    scored.sort(reverse=True, key=lambda item: item[0])
    best_score, best_method, reason = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    conf = confidence(best_score, best_score - second_score)
    return MethodMap(old.full, best_method.full, best_score, conf, reason + f" margin={best_score - second_score:.1f}")


def kotlin_strings(text: str) -> list[str]:
    return [decode_literal(value) for value in KOTLIN_STRING_RE.findall(text)]


def extract_strings_list(window: str) -> tuple[str, ...]:
    match = FP_STRINGS_RE.search(window)
    if not match:
        return ()
    return tuple(s for s in kotlin_strings(match.group(1)) if useful_string(s))


def extract_return_type(window: str) -> str:
    match = re.search(r'returnType\s*=\s*"([^"]+)"', window)
    return match.group(1) if match else ""


def extract_parameters(window: str) -> tuple[str, ...]:
    if re.search(r"parameters\s*=\s*emptyList\s*\(", window):
        return ()
    match = re.search(r"parameters\s*=\s*listOf\s*\((.*?)\)", window, re.DOTALL)
    if not match:
        return ()
    return tuple(kotlin_strings(match.group(1)))


def extract_refs(path: Path) -> list[FingerprintRef]:
    text = read_text(path)
    refs: list[FingerprintRef] = []

    for match in FP_CLASS_RE.finditer(text):
        window = text[match.start(): match.start() + 1800]
        name_match = FP_NAME_RE.search(window)
        refs.append(
            FingerprintRef(
                patch_file=path,
                class_desc=match.group(1),
                method_name=name_match.group(1) if name_match else None,
                strings=extract_strings_list(window),
                kind="Fingerprint",
                offset=match.start(),
                return_type=extract_return_type(window),
                parameters=extract_parameters(window),
            )
        )

    for match in METHOD_CALL_RE.finditer(text):
        body = match.group("body")
        cls = re.search(r'definingClass\s*=\s*"([^"]+)"', body)
        name = re.search(r'name\s*=\s*"([^"]+)"', body)
        if cls:
            refs.append(
                FingerprintRef(
                    patch_file=path,
                    class_desc=cls.group(1),
                    method_name=name.group(1) if name else None,
                    strings=(),
                    kind="methodCall",
                    offset=match.start(),
                )
            )

    return refs


def patch_files(paths: list[str], patch_dir: str | None) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            out.extend(sorted(path.rglob("*.kt")))
        elif path.exists():
            out.append(path)
    if patch_dir:
        out.extend(sorted(Path(patch_dir).rglob("*.kt")))
    return sorted(set(out))


def useful_descriptor(desc: str) -> bool:
    if "..." in desc:
        return False
    ignored_prefixes = (
        "Landroid/",
        "Landroidx/",
        "Ldalvik/",
        "Ljava/",
        "Ljavax/",
        "Lj$/",
        "Lkotlin/",
        "Lkotlinx/",
        "Lapp/morphe/",
        "Lapp/template/extension/",
    )
    return not desc.startswith(ignored_prefixes)


def collect_patch_descriptors(files: list[Path]) -> set[str]:
    descs: set[str] = set()
    for path in files:
        text = read_text(path)
        for desc in DESC_RE.findall(text):
            if useful_descriptor(desc):
                descs.add(desc)
    return descs


def line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def method_by_name(cls: ClassInfo, name: str) -> list[MethodInfo]:
    return [m for m in cls.methods if m.name == name]


def ref_params(ref: FingerprintRef) -> str:
    return "".join(ref.parameters)


def ref_method_score(ref: FingerprintRef, cls: ClassInfo, method: MethodInfo) -> float:
    score = 0.0
    if ref.method_name and method.name == ref.method_name:
        score += 24.0
    elif ref.method_name:
        return 0.0

    if ref.return_type and method.ret == ref.return_type:
        score += 10.0
    params = ref_params(ref)
    if ref.parameters and method.params == params:
        score += 10.0
    elif not ref.parameters and method.params == "":
        score += 6.0

    if ref.strings:
        common = set(ref.strings) & cls.strings
        score += 8.0 * len(common)
    return score


def cross_class_method_suggestion(ref: FingerprintRef, new_index: DecompiledIndex) -> MethodMap | None:
    if not ref.method_name:
        return None

    scored: list[tuple[float, MethodInfo]] = []
    old_package = "/".join(ref.class_desc.rstrip(";").split("/")[:-1])
    params = ref_params(ref)
    for cls in new_index.classes.values():
        for method in cls.methods:
            score = ref_method_score(ref, cls, method)
            if score <= 0:
                continue
            if ref.method_name == "<init>" and ref.parameters and method.params == params:
                score += 50.0
            if old_package and cls.package == old_package:
                score += 8.0
            scored.append((score, method))

    if not scored:
        return None

    scored.sort(reverse=True, key=lambda item: item[0])
    best_score, best_method = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    return MethodMap(
        old_method=f"{ref.class_desc}->{ref.method_name}",
        new_method=best_method.full,
        score=best_score,
        confidence=confidence(best_score, best_score - second_score),
        reason=f"cross-class method hint margin={best_score - second_score:.1f}",
    )


def map_class_from_new_refs(
    old_desc: str,
    refs: list[FingerprintRef],
    new_index: DecompiledIndex,
    allow_exact: bool = True,
) -> ClassMap | None:
    if allow_exact and old_desc in new_index.classes:
        return ClassMap(old_desc, old_desc, 999.0, "exact", "descriptor still exists")

    scored: list[tuple[float, str, str]] = []
    useful_refs = [ref for ref in refs if ref.method_name or ref.strings]
    if not useful_refs:
        return ClassMap(old_desc, "", 0.0, "none", "no old tree and no method/string hints")

    for desc, cls in new_index.classes.items():
        score = 0.0
        matched = 0
        method_hits = []
        for ref in useful_refs:
            best = 0.0
            for method in cls.methods:
                best = max(best, ref_method_score(ref, cls, method))
            if best > 0:
                matched += 1
                method_hits.append(ref.method_name or "<strings>")
            score += best

        if matched:
            score += 12.0 * matched
            if matched == len(useful_refs):
                score += 24.0
            scored.append((score, desc, f"matched={matched}/{len(useful_refs)} methods={','.join(method_hits[:6])}"))

    if not scored:
        return ClassMap(old_desc, "", 0.0, "none", "no new-only candidate")

    scored.sort(reverse=True, key=lambda item: item[0])
    best_score, best_desc, reason = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    conf = confidence(best_score, best_score - second_score)
    return ClassMap(old_desc, best_desc, best_score, conf, reason + f" margin={best_score - second_score:.1f}")


def analyze_refs(
    refs: list[FingerprintRef],
    old_index: DecompiledIndex,
    new_index: DecompiledIndex,
    class_maps: dict[str, ClassMap],
    min_conf: str,
) -> list[dict]:
    rows = []
    for ref in refs:
        cmap = class_maps.get(ref.class_desc)
        new_desc = cmap.new_desc if cmap and rank_conf(cmap.confidence) >= rank_conf(min_conf) else ref.class_desc
        old_cls = old_index.classes.get(ref.class_desc)
        new_cls = new_index.classes.get(new_desc)
        status = "ok" if new_cls else "missing-class"
        method_suggestion = None

        if ref.method_name and old_cls and new_cls:
            old_deep = deep_class(old_index, ref.class_desc) or old_cls
            new_deep = deep_class(new_index, new_desc) or new_cls
            old_methods = method_by_name(old_deep, ref.method_name)
            if old_methods:
                mapped_methods = [map_method(old_method, new_deep) for old_method in old_methods]
                mapped_methods.sort(reverse=True, key=lambda m: (rank_conf(m.confidence), m.score))
                best = mapped_methods[0]
                method_suggestion = dataclasses.asdict(best)
                if best.confidence not in {"exact", "high", "medium"}:
                    status = "check-method"
                elif best.new_method and f"->{ref.method_name}(" not in best.new_method:
                    status = "renamed-method"
            elif ref.method_name not in {m.name for m in new_deep.methods}:
                status = "method-not-in-old"
        elif ref.method_name and new_cls:
            new_deep = deep_class(new_index, new_desc) or new_cls
            scored = []
            for method in new_deep.methods:
                score = ref_method_score(ref, new_deep, method)
                if score > 0:
                    scored.append((score, method))
            if scored:
                scored.sort(reverse=True, key=lambda item: item[0])
                best_score, best_method = scored[0]
                second_score = scored[1][0] if len(scored) > 1 else 0.0
                conf = confidence(best_score, best_score - second_score)
                method_suggestion = dataclasses.asdict(
                    MethodMap(
                        old_method=f"{ref.class_desc}->{ref.method_name}",
                        new_method=best_method.full,
                        score=best_score,
                        confidence=conf,
                        reason=f"new-only method hint margin={best_score - second_score:.1f}",
                    )
                )
                if best_method.name != ref.method_name:
                    status = "candidate-method"
            elif status == "ok":
                cross_class_suggestion = cross_class_method_suggestion(ref, new_index)
                method_suggestion = dataclasses.asdict(cross_class_suggestion) if cross_class_suggestion else None
                status = "candidate-class-method" if method_suggestion else "missing-method"

        missing_strings = [s for s in ref.strings if s not in new_index.global_strings]
        if missing_strings and status == "ok":
            status = "missing-string"

        rows.append(
            {
                "file": str(ref.patch_file),
                "line": line_for_offset(read_text(ref.patch_file), ref.offset),
                "kind": ref.kind,
                "old_class": ref.class_desc,
                "new_class": new_desc if new_cls else "",
                "class_confidence": cmap.confidence if cmap else ("exact" if ref.class_desc in new_index.classes else "none"),
                "method_name": ref.method_name or "",
                "status": status,
                "missing_strings": missing_strings,
                "method_suggestion": method_suggestion,
            }
        )
    return rows


def apply_safe_updates(
    files: list[Path],
    class_maps: dict[str, ClassMap],
    new_manifest: ManifestInfo,
    out_dir: Path,
    in_place: bool,
    min_conf: str,
    update_version: bool,
) -> list[dict]:
    changed = []
    for path in files:
        original = read_text(path)
        text = original
        replacements = []

        for old_desc, cmap in class_maps.items():
            if not cmap.new_desc or cmap.old_desc == cmap.new_desc:
                continue
            if rank_conf(cmap.confidence) < rank_conf(min_conf):
                continue
            if old_desc in text:
                text = text.replace(old_desc, cmap.new_desc)
                replacements.append({"old": old_desc, "new": cmap.new_desc, "confidence": cmap.confidence})

        if update_version and new_manifest.version_name:
            text, count = APP_TARGET_RE.subn(f'AppTarget("{new_manifest.version_name}"', text)
            if count:
                replacements.append({"old": "AppTarget(...)", "new": new_manifest.version_name, "confidence": "manifest"})

        if text == original:
            continue

        target = path if in_place else out_dir / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        changed.append({"source": str(path), "target": str(target), "replacements": replacements})

    return changed


def write_report(
    out_dir: Path,
    old_index: DecompiledIndex,
    new_index: DecompiledIndex,
    class_maps: dict[str, ClassMap],
    ref_rows: list[dict],
    writes: list[dict],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "old_manifest": dataclasses.asdict(old_index.manifest),
        "new_manifest": dataclasses.asdict(new_index.manifest),
        "classes": [dataclasses.asdict(v) for v in class_maps.values()],
        "fingerprints": ref_rows,
        "writes": writes,
    }
    (out_dir / "patch_update_suggestions.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# Patch Update Suggestions",
        "",
        f"- Old package: `{old_index.manifest.package_name}` version `{old_index.manifest.version_name}` code `{old_index.manifest.version_code}`",
        f"- New package: `{new_index.manifest.package_name}` version `{new_index.manifest.version_name}` code `{new_index.manifest.version_code}`",
        f"- Old smali classes: `{len(old_index.classes)}`",
        f"- New smali classes: `{len(new_index.classes)}`",
        f"- Class refs analyzed: `{len(class_maps)}`",
        f"- Fingerprints analyzed: `{len(ref_rows)}`",
        "",
        "## Class Remaps",
        "",
        "| Confidence | Score | Old | New | Reason |",
        "|---|---:|---|---|---|",
    ]
    for cmap in sorted(class_maps.values(), key=lambda x: (rank_conf(x.confidence), x.score), reverse=True):
        if cmap.confidence == "exact":
            continue
        lines.append(
            f"| {cmap.confidence} | {cmap.score:.1f} | `{cmap.old_desc}` | `{cmap.new_desc}` | {cmap.reason} |"
        )

    lines.extend(["", "## Fingerprints To Check", "", "| Status | File | Line | Class | Method | Note |", "|---|---|---:|---|---|---|"])
    for row in ref_rows:
        note = ""
        if row["missing_strings"]:
            note = "missing strings: " + ", ".join(f"`{s}`" for s in row["missing_strings"][:5])
        elif row["method_suggestion"]:
            suggestion = row["method_suggestion"]
            if suggestion["confidence"] != "exact":
                note = f"{suggestion['confidence']} -> `{suggestion['new_method']}` ({suggestion['reason']})"
        lines.append(
            f"| {row['status']} | `{Path(row['file']).name}` | {row['line']} | `{row['old_class']}` -> `{row['new_class']}` | `{row['method_name']}` | {note} |"
        )

    if writes:
        lines.extend(["", "## Written Files", ""])
        for item in writes:
            lines.append(f"- `{item['target']}` from `{item['source']}`")

    (out_dir / "patch_update_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update Morphe Kotlin patch files using old/new decompiled APK trees.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/update_patch_from_decompiled.py --old-apktool old_apktool --new-apktool new_apktool --patch-dir patches/src/main/kotlin/app/template/patches/waze
  python tools/update_patch_from_decompiled.py --new-apktool new_apktool --patch-dir patches/src/main/kotlin/app/template/patches/waze
  python tools/update_patch_from_decompiled.py --old-apktool old --new-apktool new --patch MyPatch.kt --write-dir out/updated
  python tools/update_patch_from_decompiled.py --old-apktool old --new-apktool new --patch MyPatch.kt --in-place --update-version
""",
    )
    parser.add_argument("--old-apktool", help="Old working decompiled APK root")
    parser.add_argument("--new-apktool", required=True, help="New decompiled APK root")
    parser.add_argument("--patch", action="append", default=[], help="Old working patch file or folder")
    parser.add_argument("--patch-dir", help="Patch folder to scan")
    parser.add_argument("--out", default="patch_update_report", help="Report output directory")
    parser.add_argument("--write-dir", help="Write safe updated patch copies here")
    parser.add_argument("--in-place", action="store_true", help="Modify patch files in place")
    parser.add_argument("--min-confidence", default="high", choices=["high", "medium", "low"], help="Minimum confidence for auto replacement")
    parser.add_argument("--candidate-limit", type=int, default=80, help="Max class candidates scored per old class")
    parser.add_argument("--update-version", action="store_true", help="Update AppTarget version strings from new manifest")
    args = parser.parse_args()

    old_root = Path(args.old_apktool) if args.old_apktool else None
    new_root = Path(args.new_apktool)
    if old_root and not old_root.exists():
        print("old decompiled path missing", file=sys.stderr)
        return 2
    if not new_root.exists():
        print("new decompiled path missing", file=sys.stderr)
        return 2

    files = patch_files(args.patch, args.patch_dir)
    if not files:
        print("no patch files found", file=sys.stderr)
        return 2

    descriptors = collect_patch_descriptors(files)
    refs: list[FingerprintRef] = []
    for file in files:
        refs.extend(extract_refs(file))
    required_strings = {s for ref in refs for s in ref.strings}

    refs_by_desc: dict[str, list[FingerprintRef]] = defaultdict(list)
    for ref in refs:
        refs_by_desc[ref.class_desc].append(ref)

    if old_root:
        old_index = build_index(old_root, wanted_descs=descriptors)
        candidate_strings: set[str] = set(required_strings)
        for desc in descriptors:
            old_cls = old_index.classes.get(desc)
            if old_cls:
                candidate_strings.update(sorted(old_cls.strings, key=len, reverse=True)[:12])
        new_index = build_index(
            new_root,
            wanted_descs=descriptors,
            probe_strings=required_strings,
            candidate_strings=candidate_strings,
        )
    else:
        old_index = DecompiledIndex(
            root=Path(),
            manifest=ManifestInfo(),
            classes={},
            string_to_classes=defaultdict(set),
            proto_to_classes=defaultdict(set),
            global_strings=set(),
            source_text_files=[],
        )
        new_index = build_index(
            new_root,
            wanted_descs=descriptors,
            probe_strings=required_strings,
            candidate_strings=required_strings,
        )

    class_maps: dict[str, ClassMap] = {}
    for desc in sorted(descriptors):
        if old_root and (desc in old_index.classes or desc in new_index.classes):
            mapped = map_class(desc, old_index, new_index, args.candidate_limit)
            hint_mapped = map_class_from_new_refs(
                desc,
                refs_by_desc.get(desc, []),
                new_index,
                allow_exact=False,
            )
            if (
                mapped
                and mapped.confidence == "exact"
                and hint_mapped
                and hint_mapped.new_desc
                and hint_mapped.new_desc != desc
                and rank_conf(hint_mapped.confidence) >= rank_conf("medium")
            ):
                hint_mapped.reason = "fingerprint hints over stale exact: " + hint_mapped.reason
                class_maps[desc] = hint_mapped
            elif mapped:
                class_maps[desc] = mapped
            elif desc in new_index.classes:
                class_maps[desc] = ClassMap(desc, desc, 999.0, "exact", "descriptor exists only in new tree")
        elif not old_root:
            mapped = map_class_from_new_refs(desc, refs_by_desc.get(desc, []), new_index)
            if mapped:
                class_maps[desc] = mapped

    ref_rows = analyze_refs(refs, old_index, new_index, class_maps, args.min_confidence)

    writes: list[dict] = []
    if args.in_place or args.write_dir:
        write_dir = Path(args.write_dir or "patch_update_report/updated_patches")
        writes = apply_safe_updates(
            files=files,
            class_maps=class_maps,
            new_manifest=new_index.manifest,
            out_dir=write_dir,
            in_place=args.in_place,
            min_conf=args.min_confidence,
            update_version=args.update_version,
        )

    out_dir = Path(args.out)
    write_report(out_dir, old_index, new_index, class_maps, ref_rows, writes)

    non_exact = [m for m in class_maps.values() if m.confidence != "exact"]
    bad_refs = [r for r in ref_rows if r["status"] != "ok"]
    print(f"reports={out_dir}")
    print(f"patch_files={len(files)} class_refs={len(class_maps)} remap_suggestions={len(non_exact)} fingerprint_checks={len(bad_refs)}")
    if writes:
        print(f"written={len(writes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
