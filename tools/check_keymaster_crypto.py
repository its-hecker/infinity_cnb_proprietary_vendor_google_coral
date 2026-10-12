#!/usr/bin/env python3
"""Verify explicit legacy crypto linking without changing Keymaster code."""

import argparse
import hashlib
from pathlib import Path
import re

from keymaster_elf import Elf

LEGACY = "libcrypto-v33.so"
ORIGINAL_HASHES = {
    "service": "c0a54b5c860088f8689934aaca9bace01b958109",
    "impl": "9222e4cb05ed23597690ae30b3faf0e2b592c07a",
    "crypto": "e99fe843b8f7b1aa3937d85b1bdb91cc84cae951",
}
CRYPTO_NAME = re.compile(r"^(?:BN_|EC_|EVP_|RSA_|CBB_|CBS_|OPENSSL_|CRYPTO_|SHA|HMAC_)")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def blob_hash(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def pinned(path, kind):
    data = path.read_bytes()
    require(blob_hash(data) == ORIGINAL_HASHES[kind], f"Unexpected {kind} baseline")
    return Elf(data)


def normalized_symbols(elf):
    # Adding a dependency may reorder section headers. Section names and symbol
    # addresses/types matter; the bookkeeping section number does not.
    sections = {index: name for name, (index, section) in elf.sections.items()}
    return [(name, sections.get(index, index), info, value, size)
            for name, index, info, value, size in elf.symbols()]


def preserved(before, after, name):
    require(before.data[24:32] == after.data[24:32], f"{name}: entry point changed")
    for section_name in (".text", ".rodata", ".plt", ".eh_frame", ".note.gnu.build-id"):
        if section_name not in before.sections:
            continue
        a = before.sections[section_name][1]
        b = after.sections[section_name][1]
        # Patchelf can relocate ELF notes while preserving the build ID.
        if section_name != ".note.gnu.build-id":
            require(a[3] == b[3], f"{name}: {section_name} address changed")
        require(before.data[a[4]:a[4] + a[5]] == after.data[b[4]:b[4] + b[5]],
                f"{name}: {section_name} bytes changed")
    require(normalized_symbols(before) == normalized_symbols(after), f"{name}: dynamic symbol semantics changed")
    for old_load in before.ph:
        if old_load[0] != 1 or not old_load[1] & 1:
            continue
        matching = [p for p in after.ph if p[0] == 1 and p[1] & 1 and p[3] == old_load[3]]
        require(len(matching) == 1, f"{name}: executable mapping changed")
        new_load = matching[0]
        require(new_load[5] == old_load[5] and
                before.data[old_load[2]:old_load[2] + old_load[5]] ==
                after.data[new_load[2]:new_load[2] + new_load[5]], f"{name}: executable load bytes changed")


def check(service_path, impl_path, original_service, original_impl, crypto_path,
          vendor_root, device_root):
    before_service = pinned(original_service, "service")
    before_impl = pinned(original_impl, "impl")
    crypto = pinned(crypto_path, "crypto")
    service = Elf(service_path.read_bytes())
    impl = Elf(impl_path.read_bytes())
    require(service.needed() == [LEGACY] + before_service.needed(), "Service must load v33 crypto first")
    expected_impl = [LEGACY if lib == "libcrypto.so" else lib
                     for lib in before_impl.needed() if lib != "libcrypto_shim.so"]
    require(impl.needed() == expected_impl, "Unexpected implementation dependency changes")
    require([crypto.string(value) for _, tag, value in crypto.dyn if tag == 14] == [LEGACY],
            "Wrong compatibility library SONAME")
    imports = {name for name, index, info, value, size in impl.symbols()
               if not index and CRYPTO_NAME.match(name)}
    exports = {name for name, index, info, value, size in crypto.symbols()
               if index and info >> 4 in (1, 2)}
    require(imports and imports <= exports, f"Legacy crypto lacks imports: {sorted(imports - exports)}")
    preserved(before_service, service, "service")
    preserved(before_impl, impl, "implementation")
    bp = (vendor_root / "Android.bp").read_text()
    for module in ("android.hardware.keymaster@4.1-service.citadel", "android.hardware.keymaster@4.1-impl.nos"):
        start = bp.index('name: "' + module + '"')
        body = bp[start:bp.index("\n}\n", start)]
        require('"libcrypto-v33"' in body, f"{module}: build dependency missing")
        require('"libcrypto",' not in body and '"libcrypto_shim",' not in body,
                f"{module}: old crypto build dependency remains")
    rc = (vendor_root / "proprietary/vendor/etc/init/android.hardware.keymaster@4.1-service.citadel.rc").read_text()
    require("LD_PRELOAD" not in rc, "Service still relies on an environment preload")
    require("    user hsm\n" in rc and "    group hsm drmrpc\n" in rc, "Service identity changed")
    device = (device_root / "coral/device.mk").read_text()
    require("    libcrypto-v33" in device, "Compatibility library install entry missing")
    extraction = (device_root / "coral/extract-files.py").read_text()
    require(".replace_needed('libcrypto.so', 'libcrypto-v33.so')" in extraction and
            ".add_needed('libcrypto-v33.so')" in extraction and
            ".remove_needed('libcrypto_shim.so')" in extraction,
            "Future stock extraction does not retain the direct bindings")
    return ("Service first dependency: libcrypto-v33.so\n"
            "NOS implementation crypto dependency: libcrypto-v33.so\n"
            "NOS modern crypto/shim dependencies: removed\n"
            f"Crypto imports provided by v33: {len(imports)}/{len(imports)}\n"
            f"Compatibility Git blob: {ORIGINAL_HASHES['crypto']}\n"
            "Service/implementation executable mappings: unchanged\n"
            "Code, constants, call stubs, unwind data and build IDs: unchanged\n"
            "Dynamic symbol names, types and addresses: unchanged\n"
            "Init service identity and groups: unchanged\n"
            "Build/install/extraction entries: PASS\n"
            f"Service SHA256: {hashlib.sha256(service.data).hexdigest()}\n"
            f"Implementation SHA256: {hashlib.sha256(impl.data).hexdigest()}\n"
            "Validation: PASS (ELF/source checks; phone runtime verification required)\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, type=Path)
    parser.add_argument("--impl", required=True, type=Path)
    parser.add_argument("--before-service", required=True, type=Path)
    parser.add_argument("--before-impl", required=True, type=Path)
    parser.add_argument("--crypto", required=True, type=Path)
    parser.add_argument("--vendor-root", default=Path("."), type=Path)
    parser.add_argument("--device-root", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = check(args.service, args.impl, args.before_service, args.before_impl,
                   args.crypto, args.vendor_root, args.device_root)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
