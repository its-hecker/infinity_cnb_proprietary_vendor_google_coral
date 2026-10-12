#!/usr/bin/env python3
"""Check the compiled picker catalog, preview and preservation of Google content."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import zipfile

from apk_resource_reader import resources, xml
from sidekick_dex import SIDEKICK, dex_index, inspect_apk, validate
from unify_pixel_live_wallpaper import CATALOG, EXPECTED_PACKAGE, SERVICE, a


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
    after_dex, _ = inspect_apk(path)
    with zipfile.ZipFile(path) as apk, zipfile.ZipFile(baseline) as old:
        manifest = xml(apk.read("AndroidManifest.xml"))
        previous = xml(old.read("AndroidManifest.xml"))
        table = resources(apk.read("resources.arsc"))
        all_configs = resources(apk.read("resources.arsc"), default_only=False)
        old_table = resources(old.read("resources.arsc"))
        require(manifest.get("package") == EXPECTED_PACKAGE, "Unexpected wallpaper package")
        app = manifest.find("application")
        require(app is not None, "Application missing")
        matches = [s for s in app.findall("service") if s.get(a("name")) == SERVICE]
        require(len(matches) == 1, "Expected exactly one Sidekick service")
        service = matches[0]
        for key in ("enabled", "exported"):
            require(service.get(a(key)) == "true", f"Sidekick must be {key}")
        require(service.get(a("permission")) == "android.permission.BIND_WALLPAPER", "Wrong wallpaper permission")
        require(any(action.get(a("name")) == "android.service.wallpaper.WallpaperService"
                    for action in service.findall("intent-filter/action")), "Wallpaper intent missing")
        require(scalar(table, (1, resource_id(service.get(a("label"))))).startswith("Sidekick"), "Sidekick label missing")
        categories = [m for m in app.findall("meta-data") if m.get(a("name")) == "live_categories"]
        require(len(categories) == 1, "Category metadata must be on application")
        require(not any(m.get(a("name")) == "live_categories" for m in manifest.findall("meta-data")), "Invalid manifest-level categories")
        category_id = resource_id(categories[0].get(a("resource")))
        require(category_id == named(table, "pixel_live_categories", "array")[0], "Wrong category resource")
        require("come_alive" in array(table, "pixel_live_categories"), "Come Alive category missing")
        require(array(table, CATALOG).count(SERVICE) == 1, "Sidekick missing or duplicated in Come Alive")
        require(array(table, "pixel_live_categories") == array(old_table, "pixel_live_categories"), "Google categories changed")
        for name in (CATALOG, "living_universe_service_names"):
            require([s for s in array(table, name) if s != SERVICE]
                    == [s for s in array(old_table, name) if s != SERVICE], f"Google catalog changed: {name}")
        metadata = [m for m in service.findall("meta-data") if m.get(a("name")) == "android.service.wallpaper"]
        require(len(metadata) == 1, "Wallpaper metadata missing")
        preview_file = scalar(table, (1, resource_id(metadata[0].get(a("resource")))))
        preview = xml(apk.read(preview_file))
        require(preview.tag == "wallpaper", "Wrong preview XML")
        for key in ("description", "author"):
            require(scalar(table, (1, resource_id(preview.get(a(key))))).strip(), f"Preview {key} missing")
        thumbnail = scalar(all_configs, (1, resource_id(preview.get(a("thumbnail")))))
        require(apk.read(thumbnail).startswith(b"\x89PNG\r\n\x1a\n"), "Preview PNG missing")

        before_services = {s.get(a("name")): signature(s) for s in previous.find("application").findall("service")
                           if s.get(a("name")) != SERVICE}
        after_services = {s.get(a("name")): signature(s) for s in app.findall("service")
                          if s.get(a("name")) != SERVICE}
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
        require(len(sidekick_assets) >= 16, "Expected all 16 Sidekick characters")
    require(path.stat().st_size < 100 * 1024 * 1024, "APK exceeds GitHub file limit")
    return ("Picker category: Come Alive\n"
            "Sidekick catalog entries: 1\n"
            "Preview thumbnail, description and author: PASS\n"
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
