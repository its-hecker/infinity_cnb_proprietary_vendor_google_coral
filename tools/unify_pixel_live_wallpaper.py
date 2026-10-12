#!/usr/bin/env python3
"""Merge the Sidekick payload into the decoded Google PixelLiveWallpaper APK."""

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


def a(name: str) -> str:
    return "{%s}%s" % (ANDROID, name)


def register_picker(decoded: Path, thumbnail: Path) -> None:
    """Expose the existing payload through Pixel's catalog and Android previews."""
    manifest_path = decoded / "AndroidManifest.xml"
    tree = ET.parse(manifest_path)
    root = tree.getroot()
    if root.get("package") != EXPECTED_PACKAGE:
        raise RuntimeError("Refusing to patch an unexpected wallpaper package")
    app = root.find("application")
    if app is None:
        raise RuntimeError("PixelLiveWallpaper has no <application>")
    if not thumbnail.is_file() or not thumbnail.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Sidekick preview must be an existing PNG")

    # Validate the upstream layout before changing any decoded files.
    legacy = [node for node in root.findall("meta-data") if node.get(a("name")) == "live_categories"]
    existing = [node for node in app.findall("meta-data") if node.get(a("name")) == "live_categories"]
    references = {node.get(a("resource")) for node in legacy + existing}
    if len(references) > 1 or None in references:
        raise RuntimeError("Conflicting or invalid Pixel category metadata")

    catalogs = []
    for path in sorted((decoded / "res").glob("values*/*.xml")):
        resources = ET.parse(path)
        arrays = [node for node in resources.getroot()
                  if node.tag in ("array", "string-array") and node.get("name") == CATALOG]
        if not arrays:
            continue
        catalogs.append((path, resources, arrays))
    if not any(path.parent.name == "values" for path, _, _ in catalogs):
        raise RuntimeError("Pixel Come Alive catalog is missing; upstream layout needs review")
    for path, resources, arrays in catalogs:
        for node in arrays:
            # Keep Google's existing service order and remove only duplicate Sidekick entries.
            for item in list(node):
                if (item.text or "").strip() == SERVICE:
                    node.remove(item)
            ET.SubElement(node, "item").text = SERVICE
        resources.write(path, encoding="utf-8", xml_declaration=True)

    # ApplicationInfo metadata is how the picker discovers Pixel's category names.
    # Earlier packages put live_categories at manifest level, where it is not app metadata.
    for node in legacy:
        root.remove(node)
    for node in existing:
        app.remove(node)
    metadata = ET.SubElement(app, "meta-data")
    metadata.set(a("name"), "live_categories")
    metadata.set(a("resource"), next(iter(references), "@array/pixel_live_categories"))

    for old in list(app.findall("service")):
        if old.get(a("name")) == SERVICE:
            app.remove(old)
    service = ET.SubElement(app, "service", {
        a("name"): SERVICE,
        a("exported"): "true",
        a("enabled"): "true",
        a("label"): "@string/sidekick_unified_title",
        a("permission"): "android.permission.BIND_WALLPAPER",
    })
    intent_filter = ET.SubElement(service, "intent-filter")
    ET.SubElement(intent_filter, "action", {a("name"): "android.service.wallpaper.WallpaperService"})
    ET.SubElement(service, "meta-data", {
        a("name"): "android.service.wallpaper",
        a("resource"): "@xml/sidekick_unified",
    })

    values_dir = decoded / "res" / "values"
    values_dir.mkdir(parents=True, exist_ok=True)
    strings = ET.Element("resources")
    for name, value in (
        ("sidekick_unified_title", "Sidekick • Motion Sense"),
        ("sidekick_unified_description", "Interactive Sidekicks. Double-tap a character to switch."),
        ("sidekick_unified_author", "Hecker"),
    ):
        ET.SubElement(strings, "string", {"name": name}).text = value
    ET.ElementTree(strings).write(values_dir / "sidekick_picker.xml", encoding="utf-8", xml_declaration=True)
    xml_dir = decoded / "res" / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    wallpaper = ET.Element("wallpaper", {
        a("description"): "@string/sidekick_unified_description",
        a("thumbnail"): "@drawable/sidekick_unified_thumbnail",
        a("author"): "@string/sidekick_unified_author",
        a("showMetadataInPreview"): "true",
    })
    ET.ElementTree(wallpaper).write(xml_dir / "sidekick_unified.xml", encoding="utf-8", xml_declaration=True)
    drawables = decoded / "res" / "drawable-nodpi"
    drawables.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(thumbnail, drawables / "sidekick_unified_thumbnail.png")
    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--decoded", required=True, type=Path)
    p.add_argument("--assets", type=Path)
    p.add_argument("--thumbnail", required=True, type=Path)
    p.add_argument("--registration-only", action="store_true")
    args = p.parse_args()

    if args.registration_only:
        register_picker(args.decoded, args.thumbnail)
        return
    if args.assets is None:
        p.error("--assets is required unless --registration-only is used")

    manifest_path = args.decoded / "AndroidManifest.xml"
    tree = ET.parse(manifest_path)
    root = tree.getroot()

    package_name = root.get("package")
    if package_name != EXPECTED_PACKAGE:
        raise RuntimeError(
            "Unexpected PixelLiveWallpaper package %r (expected %r); "
            "refusing to inject into an unknown upstream layout"
            % (package_name, EXPECTED_PACKAGE)
        )

    if not any(x.get(a("name")) == PERMISSION for x in root.findall("uses-permission")):
        perm = ET.Element("uses-permission")
        perm.set(a("name"), PERMISSION)
        root.insert(0, perm)

    app = root.find("application")
    if app is None:
        raise RuntimeError("PixelLiveWallpaper has no <application>")

    dest = args.decoded / "assets" / "sidekick"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(args.assets, dest)

    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
    register_picker(args.decoded, args.thumbnail)


if __name__ == "__main__":
    main()

