#!/usr/bin/env python3
"""Verify the stock calibration dependency before packaging Pixel 4 Face ID."""

import argparse
import hashlib
from pathlib import Path
import re
import struct
import subprocess

EXPECTED_BLOB = "c2fdf209a1c63f7c8c69bbca85c0b0b7f2fd253c"
EXPECTED_BUILD_ID = "c71b3e23670c561613fd4aa655bcdfdc"
EXPECTED_NEEDED = {"libm.so", "liblog.so", "libz.so", "libdl.so", "libc.so"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def readelf(path, option):
    return subprocess.check_output(["readelf", option, str(path)], text=True)


def text_section(path):
    data = path.read_bytes()
    require(data[:6] == b"\x7fELF\x02\x01", "Expected ELF64 little endian")
    require(struct.unpack_from("<H", data, 18)[0] == 183, "Expected AArch64")
    offset = struct.unpack_from("<Q", data, 40)[0]
    width, count, names_index = struct.unpack_from("<3H", data, 58)
    require(width == 64 and names_index < count, "Invalid section table")
    require(offset + width * count <= len(data), "Truncated section table")
    sections = [struct.unpack_from("<II4QII2Q", data, offset + i * width) for i in range(count)]
    names = sections[names_index]
    strings = data[names[4]:names[4] + names[5]]
    for section in sections:
        if strings[section[0]:].split(b"\0", 1)[0] == b".text":
            require(section[4] + section[5] <= len(data), "Truncated executable section")
            return data[section[4]:section[4] + section[5]]
    raise ValueError("No executable section")


def check(library, stock_face, face, vendor_root, device_root):
    data = library.read_bytes()
    digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    require(digest == EXPECTED_BLOB, "Calibration blob differs from the pinned stock file")
    require("AArch64" in readelf(library, "-Wh"), "Wrong calibration architecture")
    dynamic = readelf(library, "-Wd")
    needed = set(re.findall(r"\(NEEDED\).*?\[([^]]+)\]", dynamic))
    require(needed == EXPECTED_NEEDED, "Unexpected calibration dependencies")
    require("Library soname: [libautocalsolvers.so-Wl]" in dynamic, "Stock SONAME changed")
    require(re.search(r"FUNC\s+GLOBAL\s+DEFAULT\s+\d+\s+Autocalibrate@@VERS_1\.0", readelf(library, "-Ws")),
            "The required Autocalibrate entry point is missing")
    for binary in (stock_face, face):
        require(f"Build ID: {EXPECTED_BUILD_ID}" in readelf(binary, "-Wn"), "Unexpected Face HAL version")
    require(text_section(face) == text_section(stock_face), "Face HAL executable bytes changed")
    face_dynamic = readelf(face, "-Wd")
    require("[libcamera2ndk_face.so]" in face_dynamic, "Legacy camera binding missing")
    require("[libstagefright_foundation-v33.so]" in face_dynamic, "Legacy callback binding missing")
    require("[libcamera2ndk_vendor.so]" not in face_dynamic, "Face HAL binds the modern camera ABI")
    bp = (vendor_root / "Android.bp").read_text()
    require('name: "libautocalsolvers"' in bp, "Calibration prebuilt module missing")
    require('"proprietary/vendor/lib64/libautocalsolvers.so"' in bp, "Calibration prebuilt source missing")
    require("    libautocalsolvers" in (vendor_root / "coral-vendor.mk").read_text(), "Calibration install entry missing")
    require("vendor/lib64/libautocalsolvers.so;DISABLE_CHECKELF" in
            (device_root / "coral/proprietary-files-vendor.txt").read_text(), "Calibration extraction entry missing")
    camera_bp = (device_root / "faceunlock/camera2ndk_v33/Android.bp").read_text()
    require('"libstagefright_foundation-v33"' in camera_bp, "Camera callbacks use the modern handler ABI")
    header = (device_root / "faceunlock/camera2ndk_v33/compat/AHandler.h").read_text()
    require("sizeof(android::AHandler) == 96" in header, "Handler layout assertion missing")
    return (f"Stock build: coral TP1A.221005.002.B2 / 9382335\n"
            f"Calibration Git blob: {digest}\n"
            f"Calibration SHA256: {hashlib.sha256(data).hexdigest()}\n"
            f"Calibration bytes: {len(data)}\n"
            "Autocalibrate default export: PASS\n"
            "Calibration dependencies: libc, libdl, liblog, libm, libz\n"
            "Stock SONAME preserved: libautocalsolvers.so-Wl\n"
            f"Face HAL stock/packaged build ID: {EXPECTED_BUILD_ID}\n"
            "Face HAL executable bytes: identical to stock\n"
            "Legacy camera and callback ABI bindings: PASS\n"
            "Vendor install and device extraction entries: PASS\n"
            "Validation: PASS (packaging/ELF checks; phone enrollment still required)\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("--stock-face", required=True, type=Path)
    parser.add_argument("--face", required=True, type=Path)
    parser.add_argument("--vendor-root", default=Path("."), type=Path)
    parser.add_argument("--device-root", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = check(args.library, args.stock_face, args.face, args.vendor_root, args.device_root)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
