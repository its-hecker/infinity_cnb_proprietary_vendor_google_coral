"""Regression checks for picker catalog exclusion and registration stability."""

from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from unify_pixel_live_wallpaper import CATALOG, SERVICE, a, register_picker


class PickerRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "res/values").mkdir(parents=True)
        self.preview = self.root / "preview.png"
        self.preview.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.manifest = self.root / "AndroidManifest.xml"
        self.manifest.write_text('''<manifest xmlns:android="http://schemas.android.com/apk/res/android"
            package="com.google.pixel.livewallpaper">
            <meta-data android:name="live_categories" android:resource="@array/pixel_live_categories" />
            <application><service android:name="com.google.wallpapers.Pokemon"
                android:enabled="false" android:permission="android.permission.BIND_WALLPAPER">
                <meta-data android:name="module" android:value="pokemon" />
            </service></application></manifest>''')
        self.catalog = self.root / "res/values/arrays.xml"
        self.catalog.write_text('''<resources>
            <string-array name="come_alive_service_names"><item>com.google.wallpapers.Pokemon</item>
            <item>com.google.wallpapers.Delights</item></string-array>
            <string-array name="living_universe_service_names"><item>com.google.wallpapers.Earth</item></string-array>
            </resources>''')
        (self.root / "assets").mkdir()
        (self.root / "assets/google.bin").write_bytes(b"existing environment")
        (self.root / "classes.dex").write_bytes(b"existing code")

    def items(self, path, name=CATALOG):
        return [n.text for n in ET.parse(path).getroot().find(f"*[@name='{name}']")]

    def test_catalog_preview_and_google_content_are_preserved(self):
        google_before = ET.tostring(ET.parse(self.manifest).getroot().find("application/service"))
        register_picker(self.root, self.preview)
        root = ET.parse(self.manifest).getroot()
        app = root.find("application")
        self.assertEqual(self.items(self.catalog), ["com.google.wallpapers.Pokemon", "com.google.wallpapers.Delights", SERVICE])
        self.assertEqual(self.items(self.catalog, "living_universe_service_names"), ["com.google.wallpapers.Earth"])
        self.assertEqual(ET.tostring(app.find("service")), google_before)
        self.assertFalse(root.findall("meta-data"))
        self.assertEqual(app.find("meta-data").get(a("resource")), "@array/pixel_live_categories")
        service = next(s for s in app.findall("service") if s.get(a("name")) == SERVICE)
        self.assertEqual(service.get(a("enabled")), "true")
        self.assertEqual(service.get(a("permission")), "android.permission.BIND_WALLPAPER")
        wallpaper = ET.parse(self.root / "res/xml/sidekick_unified.xml").getroot()
        self.assertEqual(wallpaper.get(a("thumbnail")), "@drawable/sidekick_unified_thumbnail")
        self.assertTrue(wallpaper.get(a("description")))
        self.assertEqual((self.root / "res/drawable-nodpi/sidekick_unified_thumbnail.png").read_bytes(), self.preview.read_bytes())
        self.assertEqual((self.root / "classes.dex").read_bytes(), b"existing code")
        self.assertEqual((self.root / "assets/google.bin").read_bytes(), b"existing environment")

    def test_repeat_build_and_localized_catalog_have_one_entry(self):
        localized = self.root / "res/values-en/arrays.xml"
        localized.parent.mkdir()
        localized.write_bytes(self.catalog.read_bytes())
        register_picker(self.root, self.preview)
        snapshots = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        register_picker(self.root, self.preview)
        self.assertEqual(snapshots, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(self.items(localized).count(SERVICE), 1)
        app = ET.parse(self.manifest).getroot().find("application")
        self.assertEqual(sum(s.get(a("name")) == SERVICE for s in app.findall("service")), 1)
        self.assertEqual(sum(m.get(a("name")) == "live_categories" for m in app.findall("meta-data")), 1)

    def test_unknown_layout_is_rejected_before_writing(self):
        self.catalog.write_text("<resources />")
        before = self.manifest.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "catalog is missing"):
            register_picker(self.root, self.preview)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertFalse((self.root / "res/xml").exists())

    def test_invalid_preview_is_rejected_before_writing(self):
        self.preview.write_bytes(b"not a PNG")
        before = self.catalog.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "existing PNG"):
            register_picker(self.root, self.preview)
        self.assertEqual(self.catalog.read_bytes(), before)

    def test_conflicting_categories_are_rejected(self):
        tree = ET.parse(self.manifest)
        ET.SubElement(tree.getroot().find("application"), "meta-data", {
            a("name"): "live_categories", a("resource"): "@array/other_categories"})
        tree.write(self.manifest)
        before = self.catalog.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Conflicting"):
            register_picker(self.root, self.preview)
        self.assertEqual(self.catalog.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
