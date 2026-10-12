#!/usr/bin/env python3
"""Register each Sidekick in the unified PixelLiveWallpaper picker catalog."""
from __future__ import annotations
import argparse
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

ANDROID = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID)
SERVICE = "com.hecker.motionsense.wallpapers.UnifiedSidekickWallpaper"
PERMISSION = "android.permission.STATUS_BAR_SERVICE"
EXPECTED_PACKAGE = "com.google.pixel.livewallpaper"
CATALOG = "come_alive_service_names"
CHARACTERS = (
    ("hecker", "Hecker", "Hecker"), ("mochi", "Mochi", "Mochi"),
    ("biscuit", "Biscuit", "Biscuit"), ("whisk_crumb", "Whisk & Crumb", "WhiskCrumb"),
    ("pip", "Pip", "Pip"), ("drift", "Drift", "Drift"),
    ("orbit", "Orbit", "Orbit"), ("aurora", "Aurora", "Aurora"),
    ("doraemon", "Doraemon", "Doraemon"), ("ben10", "Ben 10", "Ben10"),
    ("batman", "Batman", "Batman"), ("tom_jerry", "Tom & Jerry", "TomJerry"),
    ("nova", "Nova", "Nova"), ("kumo", "Kumo", "Kumo"),
    ("ember", "Ember", "Ember"), ("byte", "Byte", "Byte"),
)

def a(name: str) -> str:
    return "{%s}%s" % (ANDROID, name)

def component(class_name: str) -> str:
    return SERVICE + "$" + class_name

def owned_service(name: str) -> bool:
    return name == SERVICE or name.startswith(SERVICE + "$")

def register_picker(decoded: Path, thumbnail_dir: Path) -> None:
    manifest_path = decoded / "AndroidManifest.xml"
    tree = ET.parse(manifest_path)
    root = tree.getroot()
    if root.get("package") != EXPECTED_PACKAGE:
        raise RuntimeError("Refusing to patch an unexpected wallpaper package")
    app = root.find("application")
    if app is None:
        raise RuntimeError("PixelLiveWallpaper has no <application>")
    for key, _, _ in CHARACTERS:
        preview = thumbnail_dir / ("sidekick_" + key + "_thumbnail.png")
        if not preview.is_file() or not preview.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Sidekick preview must be an existing PNG: " + key)
        if not (decoded / "assets/sidekick" / ("wallpaper_" + key + ".json")).is_file():
            raise RuntimeError("Sidekick character asset is missing: " + key)

    legacy = [n for n in root.findall("meta-data") if n.get(a("name")) == "live_categories"]
    existing = [n for n in app.findall("meta-data") if n.get(a("name")) == "live_categories"]
    references = {n.get(a("resource")) for n in legacy + existing}
    if len(references) > 1 or None in references:
        raise RuntimeError("Conflicting or invalid Pixel category metadata")
    catalogs = []
    for path in sorted((decoded / "res").glob("values*/*.xml")):
        resources = ET.parse(path)
        arrays = [n for n in resources.getroot()
                  if n.tag in ("array", "string-array") and n.get("name") == CATALOG]
        if arrays:
            catalogs.append((path, resources, arrays))
    if not any(path.parent.name == "values" for path, _, _ in catalogs):
        raise RuntimeError("Pixel Come Alive catalog is missing; upstream layout needs review")

    # Only remove resources we own. This also supports decoding an already-updated APK,
    # where apktool merges the generated strings into strings.xml.
    string_names = {"sidekick_unified_title", "sidekick_unified_description", "sidekick_unified_author"}
    string_names |= {"sidekick_" + key + suffix for key, _, _ in CHARACTERS
                     for suffix in ("_title", "_description")}
    for path in sorted((decoded / "res").glob("values*/*.xml")):
        resources = ET.parse(path)
        changed = False
        for node in list(resources.getroot()):
            if node.tag == "string" and node.get("name") in string_names:
                resources.getroot().remove(node)
                changed = True
        if changed:
            resources.write(path, encoding="utf-8", xml_declaration=True)

    for path, _, _ in catalogs:
        # Re-read after removing owned strings in case the arrays share that file.
        resources = ET.parse(path)
        for node in resources.getroot():
            if node.tag in ("array", "string-array") and node.get("name") == CATALOG:
                for item in list(node):
                    if owned_service((item.text or "").strip()):
                        node.remove(item)
                for _, _, class_name in CHARACTERS:
                    ET.SubElement(node, "item").text = component(class_name)
        resources.write(path, encoding="utf-8", xml_declaration=True)

    for node in legacy:
        root.remove(node)
    for node in existing:
        app.remove(node)
    ET.SubElement(app, "meta-data", {
        a("name"): "live_categories",
        a("resource"): next(iter(references), "@array/pixel_live_categories"),
    })
    for old in list(app.findall("service")):
        if owned_service(old.get(a("name"), "")):
            app.remove(old)

    strings = ET.Element("resources")
    for name, value in (
        ("sidekick_unified_title", "Sidekick collection"),
        ("sidekick_unified_description", "Interactive Sidekicks. Double-tap a character to switch."),
        ("sidekick_unified_author", "Hecker"),
    ):
        ET.SubElement(strings, "string", {"name": name}).text = value
    xml_dir = decoded / "res/xml"
    drawables = decoded / "res/drawable-nodpi"
    xml_dir.mkdir(parents=True, exist_ok=True)
    drawables.mkdir(parents=True, exist_ok=True)

    def declare(name, resource):
        service = ET.SubElement(app, "service", {
            a("name"): name, a("exported"): "true", a("enabled"): "true",
            a("label"): "@string/" + resource + "_title",
            a("permission"): "android.permission.BIND_WALLPAPER",
        })
        intent = ET.SubElement(service, "intent-filter")
        ET.SubElement(intent, "action", {a("name"): "android.service.wallpaper.WallpaperService"})
        ET.SubElement(service, "meta-data", {
            a("name"): "android.service.wallpaper", a("resource"): "@xml/" + resource,
        })
        wallpaper = ET.Element("wallpaper", {
            a("description"): "@string/" + resource + "_description",
            a("thumbnail"): "@drawable/" + resource + "_thumbnail",
            a("author"): "@string/sidekick_unified_author",
            a("showMetadataInPreview"): "true",
        })
        ET.ElementTree(wallpaper).write(xml_dir / (resource + ".xml"),
                                      encoding="utf-8", xml_declaration=True)

    for key, title, class_name in CHARACTERS:
        resource = "sidekick_" + key
        ET.SubElement(strings, "string", {"name": resource + "_title"}).text = title
        ET.SubElement(strings, "string", {"name": resource + "_description"}).text = (
            title + " reacts to touch, Motion Sense and home-screen movement.")
        source = thumbnail_dir / (resource + "_thumbnail.png")
        target = drawables / source.name
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        declare(component(class_name), resource)

    # Keep the old applied component valid, but omit its duplicate tile from the catalog.
    shutil.copyfile(drawables / "sidekick_hecker_thumbnail.png",
                    drawables / "sidekick_unified_thumbnail.png")
    declare(SERVICE, "sidekick_unified")
    ET.ElementTree(strings).write(decoded / "res/values/sidekick_picker.xml",
                                 encoding="utf-8", xml_declaration=True)
    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decoded", required=True, type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--thumbnail-dir", required=True, type=Path)
    parser.add_argument("--registration-only", action="store_true")
    args = parser.parse_args()
    if not args.registration_only:
        if args.assets is None:
            parser.error("--assets is required unless --registration-only is used")
        manifest_path = args.decoded / "AndroidManifest.xml"
        tree = ET.parse(manifest_path)
        root = tree.getroot()
        if root.get("package") != EXPECTED_PACKAGE:
            raise RuntimeError("Unexpected PixelLiveWallpaper package")
        if not any(n.get(a("name")) == PERMISSION for n in root.findall("uses-permission")):
            root.insert(0, ET.Element("uses-permission", {a("name"): PERMISSION}))
        dest = args.decoded / "assets/sidekick"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(args.assets, dest)
        tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
    register_picker(args.decoded, args.thumbnail_dir)

if __name__ == "__main__":
    main()
