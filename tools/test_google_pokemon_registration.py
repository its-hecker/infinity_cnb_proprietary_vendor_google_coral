#!/usr/bin/env python3
"""Regressions for the manifest issues found in the Pixel 4 XL crash report."""

from pathlib import Path
import struct
import unittest
import zipfile

from apk_resource_reader import chunks, pool, xml
from enable_google_pokemon_wallpaper import (
    ANDROID, FUSED_MODULES, POKEMON, STATUS_BAR_PERMISSION,
    enabled_manifest, manifest_pool, validate_manifest,
)

APK = Path(__file__).resolve().parents[1] / (
        "proprietary/product/priv-app/PixelLiveWallpaper/PixelLiveWallpaper.apk")


def named_element(data, tag, value):
    strings = []
    for offset, kind, _, size in chunks(data, 8):
        if kind == 1:
            strings = pool(data, offset)
        elif kind == 258 and strings[struct.unpack_from("<I", data, offset + 20)[0]] == tag:
            start, step, count = struct.unpack_from("<HHH", data, offset + 24)
            attributes = {}
            for index in range(count):
                position = offset + 16 + start + index * step
                _, key, raw, _, _, typ, typed = struct.unpack_from("<IIIHBBI", data, position)
                if typ == 3:
                    attributes[strings[key]] = (strings[raw if raw != 0xffffffff else typed], position)
            if attributes.get("name", (None,))[0] == value:
                return offset, size, attributes, strings
    raise AssertionError("Manifest element missing: " + value)


class PokemonRegistrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with zipfile.ZipFile(APK) as package:
            cls.original = package.read("AndroidManifest.xml")
        cls.enabled = enabled_manifest(cls.original)

    def test_original_google_identifier_and_fused_module_survive(self):
        app = xml(self.enabled).find("application")
        pokemon = next(s for s in app.findall("service") if s.get(ANDROID + "name") == POKEMON)
        metadata = {m.get(ANDROID + "name"): m.get(ANDROID + "value")
                    for m in pokemon.findall("meta-data")}
        self.assertEqual(metadata["com.google.pixel.livewallpaper.module"], "pokemon")
        fused = next(m.get(ANDROID + "value") for m in app.findall("meta-data")
                     if m.get(ANDROID + "name") == FUSED_MODULES)
        # This is the comma-separated declaration consumed by the APK's Play Core code.
        self.assertIn(metadata["com.google.pixel.livewallpaper.module"], fused.split(","))
        self.assertNotIn("action_delete_live_wallpaper", metadata)
        self.assertIn(STATUS_BAR_PERMISSION, [p.get(ANDROID + "name")
                      for p in xml(self.enabled).findall("uses-permission")])

    def test_rejects_the_removed_identifier_regression(self):
        offset, size, _, _ = named_element(
                self.enabled, "meta-data", "com.google.pixel.livewallpaper.module")
        end_size = struct.unpack_from("<I", self.enabled, offset + size + 4)[0]
        changed = bytearray(self.enabled[:offset] + self.enabled[offset + size + end_size:])
        struct.pack_into("<I", changed, 4, len(changed))
        pokemon = next(s for s in xml(changed).find("application").findall("service")
                       if s.get(ANDROID + "name") == POKEMON)
        self.assertFalse(any(m.get(ANDROID + "name") == "com.google.pixel.livewallpaper.module"
                             for m in pokemon.findall("meta-data")))
        with self.assertRaises(ValueError):
            validate_manifest(self.original, changed)

    def test_rejects_a_fused_list_without_pokemon(self):
        _, _, attributes, strings = named_element(self.enabled, "meta-data", FUSED_MODULES)
        changed = bytearray(self.enabled)
        position = attributes["value"][1]
        for field in (8, 16):
            struct.pack_into("<I", changed, position + field, strings.index("delight"))
        with self.assertRaises(ValueError):
            validate_manifest(self.original, changed)

    def test_rejects_the_missing_oslo_permission(self):
        _, _, attributes, strings = named_element(
                self.enabled, "uses-permission", STATUS_BAR_PERMISSION)
        changed = bytearray(self.enabled)
        position = attributes["name"][1]
        for field in (8, 16):
            struct.pack_into("<I", changed, position + field,
                             strings.index("android.permission.INTERNET"))
        with self.assertRaises(ValueError):
            validate_manifest(self.original, changed)

    def test_string_pool_keeps_indices_and_utf16_character_lengths(self):
        values = ["short", "x" * 160, "Pokemon \U0001f31f", "base,pokemon"]
        for flags in (0, 0x100):
            template = struct.pack("<HHIIIIII", 1, 28, 28, 0, 0, flags, 28, 0)
            self.assertEqual(pool(manifest_pool(values, template), 0), values)


if __name__ == "__main__":
    unittest.main()
