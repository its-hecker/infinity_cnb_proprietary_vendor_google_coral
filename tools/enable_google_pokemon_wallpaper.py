#!/usr/bin/env python3
"""Register the bundled Google Pokemon wallpaper without changing its payload."""

from __future__ import annotations

import argparse
import copy
import hashlib
from pathlib import Path
import re
import shutil
import struct
import xml.etree.ElementTree as ET
import zipfile

from apk_resource_reader import chunks, pool, xml

ANDROID_URI = "http://schemas.android.com/apk/res/android"
ANDROID = "{" + ANDROID_URI + "}"
POKEMON = "com.google.pixel.livewallpaper.pokemon.wallpapers.PokemonWallpaper"
SHA256 = "0b2d73fe4698c41c267b56d98dd5a151e7b5acac5248ec0105157d13dbd61eda"
SPLIT_METADATA = {"com.google.pixel.livewallpaper.module", "action_delete_live_wallpaper"}
FUSED_MODULES = "com.android.dynamic.apk.fused.modules"
STATUS_BAR_PERMISSION = "android.permission.STATUS_BAR_SERVICE"
SIGNATURE = re.compile(r"META-INF/(?:MANIFEST\.MF|[^/]+\.(?:SF|RSA|DSA|EC))$", re.I)


def require_baseline(path: Path) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != SHA256:
        raise ValueError("Unexpected Google APK; review its manifest and payload before patching")


def manifest_pool(strings: list[str], original: bytes) -> bytes:
    """Preserve string indices while adding manifest-only compatibility strings."""
    kind, header, _, _, styles, flags, _, _ = struct.unpack_from("<HHIIIIII", original)
    if kind != 1 or header != 28 or styles:
        raise ValueError("Unexpected manifest string pool")

    def length(value, utf8):
        if utf8:
            if value > 0x7fff:
                raise ValueError("Manifest string is too long")
            return bytes([value]) if value < 0x80 else bytes([0x80 | value >> 8, value & 0xff])
        return struct.pack("<H", value) if value < 0x8000 else struct.pack(
                "<HH", 0x8000 | value >> 16, value & 0xffff)

    data = bytearray()
    offsets = []
    for value in strings:
        offsets.append(len(data))
        utf16 = value.encode("utf-16le")
        data.extend(length(len(utf16) // 2, bool(flags & 0x100)))
        if flags & 0x100:
            utf8 = value.encode("utf-8")
            data.extend(length(len(utf8), True))
            data.extend(utf8 + b"\0")
        else:
            data.extend(utf16 + b"\0\0")
    data.extend(b"\0" * (-len(data) % 4))
    start = 28 + len(offsets) * 4
    # New strings are appended, so this pool can no longer claim lexical sorting.
    return (struct.pack("<HHIIIIII", 1, 28, start + len(data), len(offsets), 0,
                        flags & ~1, start, 0)
            + struct.pack("<" + "I" * len(offsets), *offsets) + data)


def enabled_manifest(original: bytes) -> bytes:
    """Register the bundled module while preserving Google's runtime identifier."""
    kind, header, size = struct.unpack_from("<HHI", original)
    if kind != 3 or size != len(original):
        raise ValueError("Invalid compiled Android manifest")
    strings = []
    stack = []
    nodes = []
    output = []
    pool_index = None
    for offset, kind, _, total in chunks(original, header, size):
        part = bytearray(original[offset:offset + total])
        if kind == 1:
            strings = pool(original, offset)
            pool_index = len(output)
        elif kind == 258:
            name = strings[struct.unpack_from("<I", part, 20)[0]]
            start, step, count = struct.unpack_from("<HHH", part, 24)
            attributes = {}
            for index in range(count):
                position = 16 + start + index * step
                ns, key, raw, _, _, typ, value = struct.unpack_from("<IIIHBBI", part, position)
                namespace = "" if ns == 0xffffffff else strings[ns]
                key = (namespace, strings[key])
                text = strings[raw] if raw != 0xffffffff else strings[value] if typ == 3 else value
                attributes[key] = (text, position, typ)
            node = {"tag": name, "start": len(output), "parent": stack[-1] if stack else None,
                    "attributes": attributes}
            nodes.append(node)
            stack.append(node)
        elif kind == 259:
            node = stack.pop()
            if strings[struct.unpack_from("<I", part, 20)[0]] != node["tag"]:
                raise ValueError("Unbalanced compiled XML")
            node["end"] = len(output) + 1
        output.append(part)
    if stack:
        raise ValueError("Unclosed compiled XML")

    def name(node):
        return node["attributes"].get((ANDROID_URI, "name"), (None,))[0]

    apps = [n for n in nodes if n["tag"] == "application"]
    services = [n for n in nodes if n["tag"] == "service" and name(n) == POKEMON]
    categories = [n for n in nodes if n["tag"] == "meta-data" and name(n) == "live_categories"]
    if len(apps) != 1 or len(services) != 1 or len(categories) != 1:
        raise ValueError("Unexpected Google wallpaper manifest layout")
    app, pokemon, category = apps[0], services[0], categories[0]
    if pokemon["parent"] is not app or category["parent"]["tag"] != "manifest":
        raise ValueError("Unexpected component/category placement")
    value, position, typ = pokemon["attributes"][(ANDROID_URI, "enabled")]
    if typ != 18 or value != 0:
        raise ValueError("Pokemon's original enabled attribute is not a false boolean")
    # Discard any textual raw value so Android reads the new typed boolean.
    struct.pack_into("<I", output[pokemon["start"]], position + 8, 0xffffffff)
    struct.pack_into("<I", output[pokemon["start"]], position + 16, 0xffffffff)
    metadata = [n for n in nodes if n["tag"] == "meta-data"
                and n["parent"] is pokemon and name(n) in SPLIT_METADATA]
    if {name(n) for n in metadata} != SPLIT_METADATA:
        raise ValueError("Unexpected Pokemon split-module metadata")
    module = next(n for n in metadata if name(n) == "com.google.pixel.livewallpaper.module")
    if module["attributes"].get((ANDROID_URI, "value"), (None,))[0] != "pokemon":
        raise ValueError("Unexpected Pokemon module identifier")
    if any(n["tag"] == "meta-data" and n["parent"] is app
           and name(n) == FUSED_MODULES for n in nodes):
        raise ValueError("Unexpected existing fused module declaration")
    if pool_index is None:
        raise ValueError("Manifest string pool missing")

    def string_index(value):
        if value not in strings:
            strings.append(value)
        return strings.index(value)

    def string_attribute(part, node, attribute, value):
        _, position, typ = node["attributes"][(ANDROID_URI, attribute)]
        if typ != 3:
            raise ValueError("Expected a string manifest attribute")
        index = string_index(value)
        struct.pack_into("<I", part, position + 8, index)
        struct.pack_into("<I", part, position + 16, index)

    if module["end"] - module["start"] != 2:
        raise ValueError("Expected metadata without children")
    fused = [bytearray(part) for part in output[module["start"]:module["end"]]]
    string_attribute(fused[0], module, "name", FUSED_MODULES)
    string_attribute(fused[0], module, "value", "base,pokemon")
    permissions = [n for n in nodes if n["tag"] == "uses-permission"
                   and len(n["attributes"]) == 1]
    if not permissions or any(name(n) == STATUS_BAR_PERMISSION for n in permissions):
        raise ValueError("Unexpected original wallpaper permissions")
    template = permissions[0]
    if template["end"] - template["start"] != 2:
        raise ValueError("Expected permission without children")
    permission = [bytearray(part) for part in output[template["start"]:template["end"]]]
    string_attribute(permission[0], template, "name", STATUS_BAR_PERMISSION)
    output[pool_index] = manifest_pool(strings, output[pool_index])
    removed = [n for n in metadata if name(n) == "action_delete_live_wallpaper"]
    excluded = set()
    for node in removed + [category]:
        if node["end"] - node["start"] != 2:
            raise ValueError("Expected a metadata element without children")
        excluded.update(range(node["start"], node["end"]))
    result = []
    for index, part in enumerate(output):
        if index == app["start"]:
            result.extend(permission)
        if index not in excluded:
            result.append(part)
        if index == app["start"]:
            result.extend(output[category["start"]:category["end"]])
            result.extend(fused)
    data = bytearray(original[:header]) + b"".join(result)
    struct.pack_into("<I", data, 4, len(data))
    validate_manifest(original, data)
    return bytes(data)


def validate_manifest(original: bytes, enabled: bytes) -> None:
    """Limit changes to bundled registration and the original Pokemon Oslo client's permission."""
    expected = xml(original)
    app = expected.find("application")
    pokemon = next(s for s in app.findall("service") if s.get(ANDROID + "name") == POKEMON)
    pokemon.set(ANDROID + "enabled", "true")
    for child in list(pokemon):
        if (child.tag == "meta-data"
                and child.get(ANDROID + "name") == "action_delete_live_wallpaper"):
            pokemon.remove(child)
    category = next(n for n in expected.findall("meta-data")
                    if n.get(ANDROID + "name") == "live_categories")
    expected.remove(category)
    app.insert(0, category)
    app.insert(1, ET.Element("meta-data", {ANDROID + "name": FUSED_MODULES,
                                         ANDROID + "value": "base,pokemon"}))
    expected.insert(list(expected).index(app), ET.Element(
            "uses-permission", {ANDROID + "name": STATUS_BAR_PERMISSION}))

    def content(node):
        return node.tag, sorted(node.attrib.items()), [content(child) for child in node]

    if content(xml(enabled)) != content(expected):
        raise ValueError("Unexpected manifest change outside Google Pokemon registration")


def check_enabled_apk(original: Path, enabled: Path) -> str:
    require_baseline(original)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(enabled) as target:
        names = [n for n in source.namelist() if not SIGNATURE.fullmatch(n)]
        actual = [n for n in target.namelist() if not SIGNATURE.fullmatch(n)]
        if len(target.namelist()) != len(set(target.namelist())) or set(actual) != set(names):
            raise ValueError("Enabled APK entries differ from the original Google payload")
        validate_manifest(source.read("AndroidManifest.xml"), target.read("AndroidManifest.xml"))
        for name in names:
            if name == "AndroidManifest.xml":
                continue
            with source.open(name) as a, target.open(name) as b:
                while True:
                    block = a.read(1024 * 1024)
                    if block != b.read(1024 * 1024):
                        raise ValueError("Google payload changed: " + name)
                    if not block:
                        break
    return ("Google Pokemon Sidekick: enabled as a bundled wallpaper\n"
            "Google Pokemon module identifier: preserved for wallpaper-change logging\n"
            "Play Core fused modules: base,pokemon\n"
            "Delete module metadata: removed for Pokemon only\n"
            "Original Google Pokemon Oslo client: requests STATUS_BAR_SERVICE\n"
            "Google live category metadata: under application\n"
            "All Google DEX, Unity assets, native libraries and resources: unchanged\n"
            "Enabled APK registration validation: PASS\n"
            "Native Unity renderer: unchanged; reported allocator crash remains unresolved\n")


def build(original: Path, enabled: Path) -> None:
    if original.resolve() == enabled.resolve():
        raise ValueError("Output must not overwrite the original Google APK")
    require_baseline(original)
    enabled.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(enabled, "w") as target:
        manifest = enabled_manifest(source.read("AndroidManifest.xml"))
        for info in source.infolist():
            if SIGNATURE.fullmatch(info.filename):
                continue  # Soong supplies the ROM platform signature after the manifest changes.
            entry = copy.copy(info)
            if info.filename == "AndroidManifest.xml":
                target.writestr(entry, manifest)
            else:
                with source.open(info) as src, target.open(entry, "w") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
    print(check_enabled_apk(original, enabled), end="")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.input, args.output)


if __name__ == "__main__":
    main()
