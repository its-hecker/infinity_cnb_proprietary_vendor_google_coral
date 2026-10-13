#!/usr/bin/env python3
"""Verify the wallpaper and renderer are absent without changing other Google walls."""

from pathlib import Path
import unittest
import zipfile

from apk_resource_reader import resources, xml
from strip_google_pokemon_wallpaper import (
    ANDROID, POKEMON, stripped_manifest, stripped_resources,
    validate_manifest, validate_resources,
)

APK = Path(__file__).resolve().parents[1] / (
        "proprietary/product/priv-app/PixelLiveWallpaper/PixelLiveWallpaper.apk")


class PokemonRemovalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with zipfile.ZipFile(APK) as package:
            cls.manifest = package.read("AndroidManifest.xml")
            cls.table = package.read("resources.arsc")

    def test_wallpaper_service_is_absent_and_other_services_remain(self):
        original = xml(self.manifest).find("application")
        changed = xml(stripped_manifest(self.manifest)).find("application")
        names = lambda app: {s.get(ANDROID + "name") for s in app.findall("service")}
        self.assertEqual(names(changed), names(original) - {POKEMON})
        self.assertIsNotNone(next((m for m in changed.findall("meta-data")
                             if m.get(ANDROID + "name") == "live_categories"), None))
        self.assertFalse(any(m.get(ANDROID + "name", "").startswith("unity.")
                             for m in changed.findall("meta-data")))

    def test_category_keeps_all_other_wallpapers_in_order(self):
        before = resources(self.table)
        after = resources(stripped_resources(self.table))
        rid = next(rid for rid, (_, name, _) in before.items() if name == "come_alive_service_names")
        expected = [value for _, value in before[rid][2] if value != (3, POKEMON)]
        self.assertEqual([value for _, value in after[rid][2]], expected)
        self.assertEqual(len(expected), 8)
        self.assertEqual({k: v for k, v in after.items() if k != rid},
                         {k: v for k, v in before.items() if k != rid})

    def test_rejects_a_manifest_that_can_still_register_pokemon(self):
        with self.assertRaises(ValueError):
            validate_manifest(self.manifest, self.manifest)

    def test_rejects_a_category_that_can_still_offer_pokemon(self):
        with self.assertRaises(ValueError):
            validate_resources(self.table, self.table)


if __name__ == "__main__":
    unittest.main()
