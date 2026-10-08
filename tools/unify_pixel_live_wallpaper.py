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


def a(name: str) -> str:
    return "{%s}%s" % (ANDROID, name)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--decoded", required=True, type=Path)
    p.add_argument("--assets", required=True, type=Path)
    args = p.parse_args()

    manifest_path = args.decoded / "AndroidManifest.xml"
    tree = ET.parse(manifest_path)
    root = tree.getroot()

    if not any(x.get(a("name")) == PERMISSION for x in root.findall("uses-permission")):
        perm = ET.Element("uses-permission")
        perm.set(a("name"), PERMISSION)
        root.insert(0, perm)

    app = root.find("application")
    if app is None:
        raise RuntimeError("PixelLiveWallpaper has no <application>")

    for old in list(app.findall("service")):
        if old.get(a("name")) == SERVICE:
            app.remove(old)

    service = ET.SubElement(app, "service")
    service.set(a("name"), SERVICE)
    service.set(a("exported"), "true")
    service.set(a("label"), "Sidekick • Motion Sense")
    service.set(a("permission"), "android.permission.BIND_WALLPAPER")

    intent_filter = ET.SubElement(service, "intent-filter")
    action = ET.SubElement(intent_filter, "action")
    action.set(a("name"), "android.service.wallpaper.WallpaperService")

    metadata = ET.SubElement(service, "meta-data")
    metadata.set(a("name"), "android.service.wallpaper")
    metadata.set(a("resource"), "@xml/sidekick_unified")

    xml_dir = args.decoded / "res" / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    (xml_dir / "sidekick_unified.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<wallpaper xmlns:android="http://schemas.android.com/apk/res/android" />\n',
        encoding="utf-8",
    )

    dest = args.decoded / "assets" / "sidekick"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(args.assets, dest)

    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    main()
