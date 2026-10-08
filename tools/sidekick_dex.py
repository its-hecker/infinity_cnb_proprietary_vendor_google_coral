#!/usr/bin/env python3
"""Replace the Sidekick payload without retaining earlier DEX generations."""

from __future__ import annotations

import argparse
from collections import defaultdict
import os
from pathlib import Path
import re
import struct
import tempfile
import zipfile

DEX_NAME = re.compile(r"classes(\d*)\.dex")
SIDEKICK = "Lcom/hecker/motionsense/wallpapers/UnifiedSidekickWallpaper;"
OSLO = "Lcom/hecker/motionsense/wallpapers/OsloGestureClient;"
OWNED_PREFIX = "Lcom/hecker/motionsense/wallpapers/"
RELOCATIONS = {
    "androidx": "com.hecker.sidekick.shaded.androidx",
    "android.support": "com.hecker.sidekick.shaded.android.support",
}


def relocated_descriptor(descriptor: str) -> str:
    for original, isolated in RELOCATIONS.items():
        old = "L" + original.replace(".", "/") + "/"
        if descriptor.startswith(old):
            return "L" + isolated.replace(".", "/") + "/" + descriptor[len(old):]
    return descriptor


def relocate(decoded: Path) -> int:
    """Isolate AndroidX definitions, references and reflective class-name strings.

    Only the separately built payload's smali is rewritten. Google's DEX and
    resources stay untouched. Apktool then assembles valid DEX offsets/checksums.
    """
    paths = [path for directory in decoded.glob("smali*") if directory.is_dir()
             for path in directory.rglob("*.smali")]
    if not paths:
        raise ValueError("No decoded payload smali found")
    changed = 0
    for path in paths:
        original = path.read_text(encoding="utf-8")
        text = original
        for source, target in RELOCATIONS.items():
            text = text.replace("L" + source.replace(".", "/") + "/",
                                "L" + target.replace(".", "/") + "/")
            # Match complete class-name prefixes, not an already-isolated name.
            text = re.sub(r"(?<![\w.$/])" + re.escape(source) + r"\.",
                          target + ".", text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed += 1
    if not changed:
        raise ValueError("Payload contains no AndroidX definitions/references to isolate")
    return changed


def uleb128(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if offset >= len(data):
            raise ValueError("Truncated DEX ULEB128")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, offset
    raise ValueError("Invalid DEX ULEB128")


def defined_classes(data: bytes) -> list[str]:
    """Read class_defs, rather than confusing string references with definitions."""
    if len(data) < 112 or not re.fullmatch(rb"dex\n0[0-9]{2}\x00", data[:8]):
        raise ValueError("Expected a standard DEX file")
    if struct.unpack_from("<I", data, 40)[0] != 0x12345678:
        raise ValueError("Unsupported DEX byte order")
    strings_count, strings_offset = struct.unpack_from("<II", data, 56)
    types_count, types_offset = struct.unpack_from("<II", data, 64)
    classes_count, classes_offset = struct.unpack_from("<II", data, 96)
    for count, offset, width in [
        (strings_count, strings_offset, 4),
        (types_count, types_offset, 4),
        (classes_count, classes_offset, 32),
    ]:
        if offset + count * width > len(data):
            raise ValueError("DEX table is out of bounds")

    result = []
    for index in range(classes_count):
        type_index = struct.unpack_from("<I", data, classes_offset + 32 * index)[0]
        if type_index >= types_count:
            raise ValueError("DEX class type is out of bounds")
        string_index = struct.unpack_from("<I", data, types_offset + 4 * type_index)[0]
        if string_index >= strings_count:
            raise ValueError("DEX type string is out of bounds")
        offset = struct.unpack_from("<I", data, strings_offset + 4 * string_index)[0]
        _, offset = uleb128(data, offset)
        end = data.find(b"\x00", offset)
        if end < 0:
            raise ValueError("Unterminated DEX class descriptor")
        # Class descriptors are ASCII in this payload; MUTF-8 strings unrelated to
        # class_defs do not need decoding.
        result.append(data[offset:end].decode("utf-8"))
    return result


def dex_index(name: str) -> int:
    match = DEX_NAME.fullmatch(name)
    if match is None:
        raise ValueError(f"Not a DEX filename: {name}")
    return int(match.group(1) or "1")


def inspect_apk(path: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    by_dex = {}
    by_class = defaultdict(list)
    with zipfile.ZipFile(path) as apk:
        names = apk.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"{path.name}: duplicate ZIP entries")
        for name in sorted((n for n in names if DEX_NAME.fullmatch(n)), key=dex_index):
            classes = defined_classes(apk.read(name))
            by_dex[name] = classes
            for descriptor in classes:
                by_class[descriptor].append(name)
    return by_dex, dict(by_class)


def validate(path: Path) -> str:
    by_dex, by_class = inspect_apk(path)
    if not by_dex:
        raise ValueError(f"{path.name}: no DEX files")
    duplicates = {name: files for name, files in by_class.items() if len(files) > 1}
    if duplicates:
        details = "\n".join(f"  {name}: {', '.join(files)}"
                            for name, files in sorted(duplicates.items())[:20])
        raise ValueError(f"Duplicate class definitions: {len(duplicates)}\n{details}")
    for descriptor in (SIDEKICK, OSLO):
        if descriptor not in by_class:
            raise ValueError(f"Missing required payload class: {descriptor}")
    return (f"DEX files: {len(by_dex)}\n"
            f"Class definitions: {len(by_class)}\n"
            "Duplicate class definitions: 0\n"
            f"Sidekick service: {by_class[SIDEKICK][0]}\n"
            f"Oslo client: {by_class[OSLO][0]}\n"
            f"APK bytes: {path.stat().st_size}\n"
            "Validation: PASS\n")


def inject(staged: Path, payload: Path) -> list[str]:
    payload_dex, payload_classes = inspect_apk(payload)
    validate(payload)
    host_dex, _ = inspect_apk(staged)
    payload_types = set(payload_classes)
    generations = [dex_index(name) for name, classes in host_dex.items()
                   if SIDEKICK in classes]
    first_payload_index = min(generations) if generations else None
    removed = []
    for name, classes in host_dex.items():
        class_set = set(classes)
        replacement_types = {relocated_descriptor(c) for c in class_set}
        owns_service = SIDEKICK in class_set
        # Additional multidex payload parts can consist only of dependencies.
        # Remove those only when every definition belongs to the new payload.
        is_payload_part = (first_payload_index is not None
                           and dex_index(name) >= first_payload_index
                           and bool(class_set) and replacement_types <= payload_types)
        if not owns_service and not is_payload_part:
            continue
        foreign = {c for c in class_set if relocated_descriptor(c) not in payload_types
                   and not c.startswith(OWNED_PREFIX)}
        if foreign:
            raise ValueError(f"Refusing to remove mixed host/payload DEX {name}: "
                             f"{len(foreign)} unrelated class definitions")
        removed.append(name)

    # Write a fresh archive with a contiguous DEX sequence. This supports safe
    # repeat builds and preserves the bytes of every retained Google DEX/asset.
    with tempfile.TemporaryDirectory(prefix="sidekick-dex-", dir=staged.parent) as temp:
        cleaned = Path(temp) / "cleaned.apk"
        with zipfile.ZipFile(staged) as host, zipfile.ZipFile(payload) as source, \
                zipfile.ZipFile(cleaned, "w") as output:
            kept = sorted((n for n in host_dex if n not in removed), key=dex_index)
            renamed = {name: ("classes.dex" if i == 1 else f"classes{i}.dex")
                       for i, name in enumerate(kept, 1)}
            for entry in host.infolist():
                if entry.filename in removed:
                    continue
                data = host.read(entry.filename)
                if entry.filename in renamed:
                    entry.filename = renamed[entry.filename]
                output.writestr(entry, data)
            for index, name in enumerate(sorted(payload_dex, key=dex_index), len(kept) + 1):
                output.writestr("classes.dex" if index == 1 else f"classes{index}.dex",
                                source.read(name), compress_type=zipfile.ZIP_DEFLATED)
        validate(cleaned)
        os.replace(cleaned, staged)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    injection = commands.add_parser("inject")
    injection.add_argument("--staged", required=True, type=Path)
    injection.add_argument("--payload", required=True, type=Path)
    validation = commands.add_parser("validate")
    validation.add_argument("apk", type=Path)
    validation.add_argument("--report", type=Path)
    relocation = commands.add_parser("relocate")
    relocation.add_argument("--decoded", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "inject":
        removed = inject(args.staged, args.payload)
        print("Removed previous payload DEX:", ", ".join(removed) or "none")
        print(validate(args.staged), end="")
    elif args.command == "relocate":
        print("Payload smali files isolated:", relocate(args.decoded))
    else:
        try:
            report = validate(args.apk)
        except ValueError as error:
            if args.report:
                args.report.write_text(f"Validation: FAIL\n{error}\n", encoding="utf-8")
            raise
        if args.report:
            args.report.write_text(report, encoding="utf-8")
        print(report, end="")


if __name__ == "__main__":
    main()
