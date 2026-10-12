#!/usr/bin/env python3
"""Check the compiled picker catalog, preview and preservation of Google content."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from apk_resource_reader import resources, xml
from sidekick_dex import SIDEKICK, dex_index, inspect_apk, validate
from unify_pixel_live_wallpaper import CATALOG, CHARACTERS, EXPECTED_PACKAGE, SERVICE, a, component, owned_service


def require(condition, message):
    if not condition:
        raise ValueError(message)


def resource_id(value):
    require(value and value.startswith("@ref/0x"), f"Expected compiled resource reference: {value}")
    return int(value[5:], 16)


def scalar(table, value):
    kind, data = value
    if kind == 1:
        return scalar(table, table[data][2])
    require(kind == 3, "Expected a string resource")
    return data


def named(table, name, kind):
    matches = [(ident, values) for ident, (typ, key, values) in table.items()
               if typ == kind and key == name]
    require(len(matches) == 1, f"Expected one {kind}/{name}")
    return matches[0]


def array(table, name):
    _, values = named(table, name, "array")
    return [scalar(table, value) for _, value in values]


def signature(node):
    return node.tag, sorted(node.attrib.items()), [signature(child) for child in node]


def hashes(apk, names):
    result = {}
    for name in names:
        digest = hashlib.sha256()
        with apk.open(name) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        result[name] = digest.hexdigest()
    return result


def check(path: Path, baseline: Path, allow_payload_update=False) -> str:
    dex_report = validate(path)
    before_dex, _ = inspect_apk(baseline)
    after_dex, after_classes = inspect_apk(path)
    with zipfile.ZipFile(path) as apk, zipfile.ZipFile(baseline) as old:
        manifest = xml(apk.read("AndroidManifest.xml"))
        previous = xml(old.read("AndroidManifest.xml"))
        table = resources(apk.read("resources.arsc"))
        all_configs = resources(apk.read("resources.arsc"), default_only=False)
        old_table = resources(old.read("resources.arsc"))
        require(manifest.get("package") == EXPECTED_PACKAGE, "Unexpected wallpaper package")
        app = manifest.find("application")
        require(app is not None, "Application missing")
        services = {s.get(a("name")): s for s in app.findall("service")}
        required = [(component(cls), key, title) for key, title, cls in CHARACTERS]
        required.append((SERVICE, "unified", "Sidekick collection"))
        thumbnail_hashes = set()
        for name, key, title in required:
            require(sum(n.get(a("name")) == name for n in app.findall("service")) == 1,
                    "Missing or duplicated wallpaper component: " + name)
            service = services[name]
            for flag in ("enabled", "exported"):
                require(service.get(a(flag)) == "true", "Sidekick must be " + flag)
            require(service.get(a("permission")) == "android.permission.BIND_WALLPAPER", "Wrong wallpaper permission")
            require(any(n.get(a("name")) == "android.service.wallpaper.WallpaperService"
                        for n in service.findall("intent-filter/action")), "Wallpaper intent missing")
            require(scalar(table, (1, resource_id(service.get(a("label"))))) == title, "Wrong character label")
            require("L" + name.replace(".", "/") + ";" in after_classes,
                    "Picker component is not present in DEX: " + name)
            metadata = [m for m in service.findall("meta-data")
                        if m.get(a("name")) == "android.service.wallpaper"]
            require(len(metadata) == 1, "Wallpaper metadata missing")
            preview_file = scalar(table, (1, resource_id(metadata[0].get(a("resource")))))
            preview = xml(apk.read(preview_file))
            require(preview.tag == "wallpaper", "Wrong preview XML")
            for field in ("description", "author"):
                require(scalar(table, (1, resource_id(preview.get(a(field))))).strip(), "Preview " + field + " missing")
            thumbnail = scalar(all_configs, (1, resource_id(preview.get(a("thumbnail")))))
            require(apk.read(thumbnail).startswith(b"\x89PNG\r\n\x1a\n"), "Preview PNG missing")
            if key != "unified":
                require("assets/sidekick/wallpaper_" + key + ".json" in apk.namelist(), "Character asset missing")
                thumbnail_hashes.add(hashlib.sha256(apk.read(thumbnail)).hexdigest())
        require(len(thumbnail_hashes) == len(CHARACTERS), "Each character needs its own rendered thumbnail")
        categories = [m for m in app.findall("meta-data") if m.get(a("name")) == "live_categories"]
        require(len(categories) == 1, "Category metadata must be on application")
        require(not any(m.get(a("name")) == "live_categories" for m in manifest.findall("meta-data")), "Invalid manifest-level categories")
        category_id = resource_id(categories[0].get(a("resource")))
        require(category_id == named(table, "pixel_live_categories", "array")[0], "Wrong category resource")
        require("come_alive" in array(table, "pixel_live_categories"), "Come Alive category missing")
        expected = [component(cls) for _, _, cls in CHARACTERS]
        catalog = array(table, CATALOG)
        require([name for name in catalog if owned_service(name)] == expected,
                "Come Alive must list each Sidekick exactly once and omit the legacy duplicate")
        require(array(table, "pixel_live_categories") == array(old_table, "pixel_live_categories"), "Google categories changed")
        for name in (CATALOG, "living_universe_service_names"):
            require([s for s in array(table, name) if not owned_service(s)]
                    == [s for s in array(old_table, name) if not owned_service(s)], f"Google catalog changed: {name}")
        before_services = {s.get(a("name")): signature(s) for s in previous.find("application").findall("service")
                           if not owned_service(s.get(a("name"), ""))}
        after_services = {s.get(a("name")): signature(s) for s in app.findall("service")
                          if not owned_service(s.get(a("name"), ""))}
        require(before_services == after_services, "Existing Google service declarations changed")
        keep = lambda name: (name.startswith(("assets/", "lib/")) and not name.endswith("/")
                             and not (allow_payload_update and name.startswith("assets/sidekick/")))
        before_names = {n for n in old.namelist() if keep(n)}
        after_names = {n for n in apk.namelist() if keep(n)}
        require(before_names == after_names, "Existing asset/native file inventory changed")
        require(hashes(old, before_names) == hashes(apk, after_names), "Existing wallpaper asset/native bytes changed")
        if allow_payload_update:
            def host_files(dex):
                payload = [dex_index(n) for n, definitions in dex.items() if SIDEKICK in definitions]
                first = min(payload) if payload else float("inf")
                return {n for n in dex if dex_index(n) < first}
            old_code = hashes(old, host_files(before_dex))
            new_code = hashes(apk, host_files(after_dex))
        else:
            old_code = hashes(old, before_dex)
            new_code = hashes(apk, after_dex)
        require(old_code == new_code, "Retained wallpaper DEX bytes changed")
        sidekick_assets = {n for n in apk.namelist() if n.startswith("assets/sidekick/wallpaper_") and n.endswith(".json")}
        require(len(sidekick_assets) == len(CHARACTERS), "Expected exactly 16 Sidekick characters")
        for asset in sorted(sidekick_assets):
            animation = json.loads(apk.read(asset))
            require(all(layer.get("bm", 0) != 1 for layer in animation["layers"]),
                    "Alpha-modulating multiply blend would erase the scene: " + asset)
    require(path.stat().st_size < 100 * 1024 * 1024, "APK exceeds GitHub file limit")
    return ("Picker category: Come Alive\n"
            f"Sidekick catalog entries: {len(CHARACTERS)}\n"
            "Preview thumbnail, description and author: PASS\n"
            "Scene lighting preserves background alpha: PASS\n"
            "Existing Google services and catalog entries: unchanged\n"
            f"Existing asset/native files with identical bytes: {len(before_names)}\n"
            f"Retained DEX files with identical bytes: {len(old_code)}\n"
            f"Sidekick character assets: {len(sidekick_assets)}\n" + dex_report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--allow-payload-update", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = check(args.apk, args.baseline, args.allow_payload_update)
    except (ValueError, KeyError) as error:
        if args.report:
            args.report.write_text(f"Validation: FAIL\n{error}\n", encoding="utf-8")
        raise
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
