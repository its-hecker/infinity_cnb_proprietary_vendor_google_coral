#!/usr/bin/env python3
"""Require the original Google wallpaper APK, without custom Sidekick injection."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys
import zipfile

from apk_resource_reader import xml
from strip_google_pokemon_wallpaper import check_stripped_apk

ROOT = Path(__file__).resolve().parents[1]
APK = ROOT / "proprietary/product/priv-app/PixelLiveWallpaper/PixelLiveWallpaper.apk"
# Original Google APK imported before any custom Sidekick/Unity asset changes.
SOURCE_COMMIT = "460f71da894d2a8435a20a761a82c351e1d29587"
SHA256 = "0b2d73fe4698c41c267b56d98dd5a151e7b5acac5248ec0105157d13dbd61eda"
ANDROID = "{http://schemas.android.com/apk/res/android}"
POKEMON = "com.google.pixel.livewallpaper.pokemon.wallpapers.PokemonWallpaper"


def check(apk: Path) -> str:
    fingerprint = hashlib.sha256()
    with apk.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            fingerprint.update(chunk)
    digest = fingerprint.hexdigest()
    if digest != SHA256:
        raise ValueError(f"Wallpaper APK differs from the original Google baseline: {digest}")
    with zipfile.ZipFile(apk) as package:
        names = package.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate APK ZIP entries")
        if any(name.startswith("assets/sidekick/") for name in names):
            raise ValueError("Custom Sidekick assets remain")
        manifest = xml(package.read("AndroidManifest.xml"))
        if manifest.get("package") != "com.google.pixel.livewallpaper":
            raise ValueError("Unexpected wallpaper package")
        application = manifest.find("application")
        if application is None:
            raise ValueError("Wallpaper application missing")
        services = application.findall("service")
        if any(not service.get(ANDROID + "name", "").startswith("com.google.pixel.livewallpaper.")
               for service in services):
            raise ValueError("Custom wallpaper service remains")
        wallpapers = [service for service in services if any(
            action.get(ANDROID + "name") == "android.service.wallpaper.WallpaperService"
            for action in service.findall("intent-filter/action"))]
        if POKEMON not in {service.get(ANDROID + "name") for service in wallpapers}:
            raise ValueError("Google Pokemon Wave Hello service missing")
        if any(permission.get(ANDROID + "name") == "android.permission.STATUS_BAR_SERVICE"
               for permission in manifest.findall("uses-permission")):
            raise ValueError("Custom Sidekick SystemUI permission remains")
        dex_names = [name for name in names if re.fullmatch(r"classes\d*\.dex", name)]
        for name in dex_names:
            data = package.read(name)
            if any(prefix in data for prefix in (b"Lcom/hecker/motionsense/wallpapers/",
                                                 b"Lcom/hecker/sidekick/")):
                raise ValueError(f"Custom Sidekick code remains in {name}")
    return (
        f"Original Google import: {SOURCE_COMMIT}\n"
        f"APK SHA-256: {digest}\n"
        f"Google wallpaper services: {len(wallpapers)}\n"
        f"Original Google DEX files: {len(dex_names)}\n"
        "Custom Sidekick assets, services, code and SystemUI permission: absent\n"
        "Google Pokemon Wave Hello: present\n"
        "Validation: PASS\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", nargs="?", type=Path, default=APK)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--without-pokemon", type=Path,
                        help="Also verify a generated APK with Pokemon removed")
    args = parser.parse_args()
    try:
        report = check(args.apk)
        if args.without_pokemon:
            report += check_stripped_apk(args.apk, args.without_pokemon)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        print(f"Validation: FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
