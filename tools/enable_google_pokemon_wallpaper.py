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
import zipfile

from apk_resource_reader import chunks, pool, xml

ANDROID_URI = "http://schemas.android.com/apk/res/android"
ANDROID = "{" + ANDROID_URI + "}"
POKEMON = "com.google.pixel.livewallpaper.pokemon.wallpapers.PokemonWallpaper"
SHA256 = "0b2d73fe4698c41c267b56d98dd5a151e7b5acac5248ec0105157d13dbd61eda"
SPLIT_METADATA = {"com.google.pixel.livewallpaper.module", "action_delete_live_wallpaper"}
SIGNATURE = re.compile(r"META-INF/(?:MANIFEST\.MF|[^/]+\.(?:SF|RSA|DSA|EC))$", re.I)


def require_baseline(path: Path) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != SHA256:
        raise ValueError("Unexpected Google APK; review its manifest and payload before patching")


def enabled_manifest(original: bytes) -> bytes:
    """Change one typed boolean and move/remove complete binary XML elements."""
    kind, header, size = struct.unpack_from("<HHI", original)
    if kind != 3 or size != len(original):
        raise ValueError("Invalid compiled Android manifest")
    strings = []
    stack = []
    nodes = []
    output = []
    for offset, kind, _, total in chunks(original, header, size):
        part = bytearray(original[offset:offset + total])
        if kind == 1:
            strings = pool(original, offset)
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
    removed = [n for n in nodes if n["tag"] == "meta-data"
               and n["parent"] is pokemon and name(n) in SPLIT_METADATA]
    if {name(n) for n in removed} != SPLIT_METADATA:
        raise ValueError("Unexpected Pokemon split-module metadata")
    excluded = set()
    for node in removed + [category]:
        if node["end"] - node["start"] != 2:
            raise ValueError("Expected a metadata element without children")
        excluded.update(range(node["start"], node["end"]))
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


def validate_manifest(original: bytes, enabled: bytes) -> None:
    """Independently limit semantic changes to Pokemon registration and category placement."""
    expected = xml(original)
    app = expected.find("application")
    pokemon = next(s for s in app.findall("service") if s.get(ANDROID + "name") == POKEMON)
    pokemon.set(ANDROID + "enabled", "true")
    for child in list(pokemon):
        if child.tag == "meta-data" and child.get(ANDROID + "name") in SPLIT_METADATA:
            pokemon.remove(child)
    category = next(n for n in expected.findall("meta-data")
                    if n.get(ANDROID + "name") == "live_categories")
    expected.remove(category)
    app.insert(0, category)

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
            "Download/delete module metadata: removed for Pokemon only\n"
            "Google live category metadata: under application\n"
            "All Google DEX, Unity assets, native libraries and resources: unchanged\n"
            "Enabled APK validation: PASS\n")


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
