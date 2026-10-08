"""Regression coverage for repeated payload injection and class ownership."""

import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from sidekick_dex import (SIDEKICK, OSLO, defined_classes, inject, inspect_apk,
                         validate, relocate)


def dex(classes, references=()):
    strings = list(classes) + list(references)
    strings_offset = 112
    types_offset = strings_offset + len(strings) * 4
    classes_offset = types_offset + len(classes) * 4
    data_offset = classes_offset + len(classes) * 32
    data = bytearray(data_offset)
    data[:8] = b"dex\n035\x00"
    struct.pack_into("<I", data, 40, 0x12345678)
    struct.pack_into("<II", data, 56, len(strings), strings_offset)
    struct.pack_into("<II", data, 64, len(classes), types_offset)
    struct.pack_into("<II", data, 96, len(classes), classes_offset)
    for index, string in enumerate(strings):
        assert len(string) < 128
        struct.pack_into("<I", data, strings_offset + index * 4, len(data))
        data.extend(bytes([len(string)]) + string.encode() + b"\x00")
    for index in range(len(classes)):
        struct.pack_into("<I", data, types_offset + index * 4, index)
        struct.pack_into("<I", data, classes_offset + index * 32, index)
    return bytes(data)


def apk(path, entries):
    with zipfile.ZipFile(path, "w") as output:
        for name, data in entries.items():
            output.writestr(name, data)


class SidekickDexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = self.root / "host.apk"
        self.payload = self.root / "payload.apk"
        self.google = dex(["Lcom/google/pixel/Wallpaper;"], references=[SIDEKICK])
        self.sidekick = dex([SIDEKICK, OSLO, "Lcom/airbnb/lottie/LottieDrawable;"])
        apk(self.payload, {"classes.dex": self.sidekick})

    def test_repeated_injection_removes_all_old_generations(self):
        apk(self.host, {"classes.dex": self.google, "assets/bin/Data/keep": b"original",
                        **{f"classes{i}.dex": self.sidekick for i in range(2, 7)}})
        self.assertEqual(len(inject(self.host, self.payload)), 5)
        self.assertIn("Duplicate class definitions: 0", validate(self.host))
        self.assertEqual(list(inspect_apk(self.host)[0]), ["classes.dex", "classes2.dex"])
        with zipfile.ZipFile(self.host) as output:
            self.assertEqual(output.read("classes.dex"), self.google)
            self.assertEqual(output.read("assets/bin/Data/keep"), b"original")
        inject(self.host, self.payload)
        self.assertEqual(list(inspect_apk(self.host)[0]), ["classes.dex", "classes2.dex"])

    def test_class_reference_does_not_own_host_dex(self):
        self.assertNotIn(SIDEKICK, defined_classes(self.google))
        apk(self.host, {"classes.dex": self.google})
        self.assertEqual(inject(self.host, self.payload), [])

    def test_multidex_payload_replaced_and_sequence_compacted(self):
        dependency = dex(["Lcom/airbnb/lottie/LottieDrawable;"])
        apk(self.payload, {"classes.dex": dex([SIDEKICK, OSLO]), "classes2.dex": dependency})
        apk(self.host, {"classes.dex": self.google, "classes4.dex": dex([SIDEKICK, OSLO]),
                        "classes5.dex": dependency})
        inject(self.host, self.payload)
        self.assertEqual(list(inspect_apk(self.host)[0]),
                         ["classes.dex", "classes2.dex", "classes3.dex"])
        validate(self.host)

    def test_duplicate_classes_rejected(self):
        apk(self.host, {"classes.dex": self.sidekick, "classes2.dex": self.sidekick})
        with self.assertRaisesRegex(ValueError, "Duplicate class definitions: 3"):
            validate(self.host)

    def test_mixed_dex_not_deleted(self):
        apk(self.host, {"classes.dex": dex([SIDEKICK, OSLO, "Lhost/Unrelated;"])})
        original = hashlib.sha256(self.host.read_bytes()).digest()
        with self.assertRaisesRegex(ValueError, "Refusing to remove mixed"):
            inject(self.host, self.payload)
        self.assertEqual(hashlib.sha256(self.host.read_bytes()).digest(), original)

    def test_collision_preserves_original_archive(self):
        apk(self.host, {"classes.dex": dex(["Lcom/airbnb/lottie/LottieDrawable;", "Lhost/Main;"])})
        original = self.host.read_bytes()
        with self.assertRaisesRegex(ValueError, "Duplicate class definitions"):
            inject(self.host, self.payload)
        self.assertEqual(self.host.read_bytes(), original)

    def test_relocated_library_preserves_google_copy(self):
        old = dex([SIDEKICK, OSLO, "Landroidx/core/Original;"])
        apk(self.host, {"classes.dex": dex(["Landroidx/core/Original;"]),
                        "classes2.dex": old})
        apk(self.payload, {"classes.dex": dex([SIDEKICK, OSLO,
            "Lcom/hecker/sidekick/shaded/androidx/core/Original;"])})
        inject(self.host, self.payload)
        _, classes = inspect_apk(self.host)
        self.assertEqual(classes["Landroidx/core/Original;"], ["classes.dex"])
        self.assertEqual(classes["Lcom/hecker/sidekick/shaded/androidx/core/Original;"],
                         ["classes2.dex"])
        validate(self.host)

    def test_relocation_updates_descriptors_and_reflection_strings(self):
        smali = self.root / "decoded" / "smali" / "Example.smali"
        smali.parent.mkdir(parents=True)
        smali.write_text('.class Landroidx/core/Example;\n'
                         '.field other:Landroid/support/v4/Example;\n'
                         'const-string v0, "androidx.core.Example"\n'
                         'const-string v1, "com.google.oslo.Service"\n')
        self.assertEqual(relocate(self.root / "decoded"), 1)
        text = smali.read_text()
        self.assertIn('Lcom/hecker/sidekick/shaded/androidx/core/Example;', text)
        self.assertIn('Lcom/hecker/sidekick/shaded/android/support/v4/Example;', text)
        self.assertIn('"com.hecker.sidekick.shaded.androidx.core.Example"', text)
        self.assertIn('"com.google.oslo.Service"', text)
        # An accidental second relocation must not recursively lengthen names.
        with self.assertRaisesRegex(ValueError, "no AndroidX"):
            relocate(self.root / "decoded")
        self.assertEqual(smali.read_text(), text)


if __name__ == "__main__":
    unittest.main()
