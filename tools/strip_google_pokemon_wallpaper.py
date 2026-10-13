#!/usr/bin/env python3
"""Remove Pokemon Sidekick registration and its Unity payload from the pinned Google APK."""

from __future__ import annotations

import argparse
import copy
import hashlib
from pathlib import Path
import re
import shutil
import struct
import zipfile

from apk_resource_reader import chunks, pool, resources, xml

ANDROID = "{http://schemas.android.com/apk/res/android}"
POKEMON = "com.google.pixel.livewallpaper.pokemon.wallpapers.PokemonWallpaper"
SHA256 = "0b2d73fe4698c41c267b56d98dd5a151e7b5acac5248ec0105157d13dbd61eda"
UNITY_METADATA = {"unity.build-id", "unity.splash-mode", "unity.splash-enable", "pokemon.version"}
UNITY_LIBRARIES = {"libil2cpp.so", "libmain.so", "libswappy.so", "libswappyVk.so", "libunity.so"}
SIGNATURE = re.compile(r"META-INF/(?:MANIFEST\.MF|[^/]+\.(?:SF|RSA|DSA|EC))$", re.I)


def removed_payload(name: str) -> bool:
    return (name.startswith("assets/bin/Data/")
            or name.startswith("lib/arm64-v8a/") and Path(name).name in UNITY_LIBRARIES)


def require_baseline(path: Path) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != SHA256:
        raise ValueError("Unexpected Google APK; review the wallpaper and native dependencies first")


def stripped_manifest(original: bytes) -> bytes:
    kind, header, size = struct.unpack_from("<HHI", original)
    if kind != 3 or size != len(original):
        raise ValueError("Invalid compiled manifest")
    strings, stack, nodes, output = [], [], [], []
    for offset, kind, _, total in chunks(original, header, size):
        part = original[offset:offset + total]
        if kind == 1:
            strings = pool(original, offset)
        elif kind == 258:
            name = strings[struct.unpack_from("<I", part, 20)[0]]
            start, step, count = struct.unpack_from("<HHH", part, 24)
            attrs = {}
            for index in range(count):
                pos = 16 + start + index * step
                ns, key, raw, _, _, typ, value = struct.unpack_from("<IIIHBBI", part, pos)
                text = strings[raw] if raw != 0xffffffff else strings[value] if typ == 3 else value
                attrs[("" if ns == 0xffffffff else strings[ns], strings[key])] = text
            node = {"tag": name, "start": len(output), "attrs": attrs,
                    "parent": stack[-1] if stack else None}
            nodes.append(node)
            stack.append(node)
        elif kind == 259:
            node = stack.pop()
            if strings[struct.unpack_from("<I", part, 20)[0]] != node["tag"]:
                raise ValueError("Unbalanced compiled manifest")
            node["end"] = len(output) + 1
        output.append(part)
    if stack:
        raise ValueError("Unclosed compiled manifest")
    def name(node):
        return node["attrs"].get((ANDROID[1:-1], "name"))
    apps = [n for n in nodes if n["tag"] == "application"]
    pokemon = [n for n in nodes if n["tag"] == "service" and name(n) == POKEMON]
    categories = [n for n in nodes if n["tag"] == "meta-data" and name(n) == "live_categories"]
    if len(apps) != 1 or len(pokemon) != 1 or len(categories) != 1:
        raise ValueError("Unexpected original Google component layout")
    app, service, category = apps[0], pokemon[0], categories[0]
    if service["parent"] is not app or category["parent"]["tag"] != "manifest":
        raise ValueError("Unexpected Google service/category placement")
    removed = [service, category] + [n for n in nodes if n["tag"] == "meta-data"
                                     and n["parent"] is app and name(n) in UNITY_METADATA]
    excluded = {i for node in removed for i in range(node["start"], node["end"])}
    result = []
    for index, part in enumerate(output):
        if index not in excluded:
            result.append(part)
        if index == app["start"]:
            result.extend(output[category["start"]:category["end"]])
    data = bytearray(original[:header]) + b"".join(result)
    struct.pack_into("<I", data, 4, len(data))
    validate_manifest(original, data)
    return bytes(data)


def validate_manifest(original: bytes, stripped: bytes) -> None:
    expected = xml(original)
    app = expected.find("application")
    service = next(s for s in app.findall("service") if s.get(ANDROID + "name") == POKEMON)
    app.remove(service)
    for node in list(app):
        if node.tag == "meta-data" and node.get(ANDROID + "name") in UNITY_METADATA:
            app.remove(node)
    category = next(n for n in expected.findall("meta-data")
                    if n.get(ANDROID + "name") == "live_categories")
    expected.remove(category)
    app.insert(0, category)
    def content(node):
        return node.tag, sorted(node.attrib.items()), [content(child) for child in node]
    if content(xml(stripped)) != content(expected):
        raise ValueError("Unexpected manifest change outside Pokemon removal/category placement")


def stripped_resources(original: bytes) -> bytes:
    """Remove Pokemon from the picker category's array without shifting any resource offsets."""
    targets = [rid for rid, (_, name, _) in resources(original).items()
               if name == "come_alive_service_names"]
    if len(targets) != 1:
        raise ValueError("Unexpected Come Alive service array")
    target = targets[0]
    _, header, size = struct.unpack_from("<HHI", original)
    strings = []
    data = bytearray(original)
    changed = 0
    for offset, kind, head, total in chunks(original, header, size):
        if kind == 1:
            strings = pool(original, offset)
        if kind != 512 or struct.unpack_from("<I", original, offset + 8)[0] != target >> 24:
            continue
        for pos, typ, h, length in chunks(original, offset + head, offset + total):
            if typ != 513:
                continue
            type_id, flags, _, count, base = struct.unpack_from("<BBHII", original, pos + 8)
            if type_id != (target >> 16) & 0xff:
                continue
            index = target & 0xffff
            if flags & 1:
                rel = next((rel * 4 for idx, rel in (
                    struct.unpack_from("<HH", original, pos + h + i * 4)
                    for i in range(count)) if idx == index), None)
            elif index >= count:
                rel = None
            elif flags & 2:
                value = struct.unpack_from("<H", original, pos + h + index * 2)[0]
                rel = None if value == 0xffff else value * 4
            else:
                value = struct.unpack_from("<I", original, pos + h + index * 4)[0]
                rel = None if value == 0xffffffff else value
            if rel is None:
                continue
            entry = pos + base + rel
            entry_size, entry_flags, _ = struct.unpack_from("<HHI", original, entry)
            if entry_size != 16 or not entry_flags & 1:
                raise ValueError("Expected a complex service array")
            parent, count = struct.unpack_from("<II", original, entry + 8)
            start = entry + entry_size
            if parent or start + count * 12 > pos + length:
                raise ValueError("Unexpected service array bounds/inheritance")
            items = [original[start + i * 12:start + (i + 1) * 12] for i in range(count)]
            first_name = struct.unpack_from("<I", items[0])[0]
            kept = []
            removed = 0
            for i, item in enumerate(items):
                name, _, _, value_type, value = struct.unpack("<IHBBI", item)
                if name != first_name + i or value_type != 3:
                    raise ValueError("Expected a sequential string array")
                if strings[value] == POKEMON:
                    removed += 1
                else:
                    kept.append(item)
            if removed != 1:
                raise ValueError("Expected exactly one Pokemon category entry")
            struct.pack_into("<I", data, entry + 12, len(kept))
            for i, item in enumerate(kept):
                data[start + i * 12:start + (i + 1) * 12] = item
                struct.pack_into("<I", data, start + i * 12, first_name + i)
            data[start + len(kept) * 12:start + count * 12] = b"\0" * (removed * 12)
            changed += 1
    if not changed:
        raise ValueError("Pokemon service array was not patched")
    validate_resources(original, data)
    return bytes(data)


def validate_resources(original: bytes, stripped: bytes) -> None:
    expected = resources(original, default_only=False)
    for rid, (typ, name, values) in list(expected.items()):
        if name == "come_alive_service_names":
            kept = [value for _, value in values if value != (3, POKEMON)]
            if len(kept) != len(values) - 1:
                raise ValueError("Unexpected original Pokemon array")
            expected[rid] = (typ, name, [(values[0][0] + i, value) for i, value in enumerate(kept)])
    if resources(stripped, default_only=False) != expected:
        raise ValueError("Unexpected resource change outside Pokemon category removal")


def check_stripped_apk(original: Path, stripped: Path) -> str:
    require_baseline(original)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(stripped) as target:
        names = [n for n in source.namelist() if not SIGNATURE.fullmatch(n) and not removed_payload(n)]
        actual = [n for n in target.namelist() if not SIGNATURE.fullmatch(n)]
        if len(target.namelist()) != len(set(target.namelist())) or set(actual) != set(names):
            raise ValueError("Unexpected APK entries after Pokemon removal")
        validate_manifest(source.read("AndroidManifest.xml"), target.read("AndroidManifest.xml"))
        validate_resources(source.read("resources.arsc"), target.read("resources.arsc"))
        if target.read("resources.arsc") != stripped_resources(source.read("resources.arsc")):
            raise ValueError("Unexpected resource bytes outside the service array")
        for name in names:
            if name in {"AndroidManifest.xml", "resources.arsc"}:
                continue
            with source.open(name) as a, target.open(name) as b:
                while True:
                    block = a.read(1024 * 1024)
                    if block != b.read(1024 * 1024):
                        raise ValueError("Other Google payload changed: " + name)
                    if not block:
                        break
    return ("Google Pokemon Sidekick service and category entry: removed\n"
            "Pokemon Unity assets and five native libraries: removed\n"
            "Other Google DEX, assets, native libraries and resource values: preserved\n"
            "Google live category metadata: under application\n"
            "Google wallpaper removal validation: PASS\n")


def build(original: Path, stripped: Path) -> None:
    if original.resolve() == stripped.resolve():
        raise ValueError("Output must not overwrite the pinned original Google APK")
    require_baseline(original)
    stripped.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(stripped, "w") as target:
        for info in source.infolist():
            if SIGNATURE.fullmatch(info.filename) or removed_payload(info.filename):
                continue
            entry = copy.copy(info)
            if info.filename == "AndroidManifest.xml":
                target.writestr(entry, stripped_manifest(source.read(info.filename)))
            elif info.filename == "resources.arsc":
                target.writestr(entry, stripped_resources(source.read(info.filename)))
            else:
                with source.open(info) as src, target.open(entry, "w") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
    print(check_stripped_apk(original, stripped), end="")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.input, args.output)


if __name__ == "__main__":
    main()
