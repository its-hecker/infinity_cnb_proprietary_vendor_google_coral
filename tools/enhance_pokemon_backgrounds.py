#!/usr/bin/env python3
"""
Safely enhance environment textures used by Google's Pokemon live wallpaper.

The Pixel 4 APK stores its Unity player data as Android .assets.splitN files.
UnityPy understands that layout when the *whole decoded APK tree* is loaded, because
it can resolve the split SerializedFile and any external resource data together.

Only an exact allowlist of environment/effect Texture2D names is touched. Pokemon
character/model textures are never selected. Modified textures keep their original
texture format and mip-count, are inlined into the owning SerializedFile, and the
result is written back using the original Android split boundaries.

Every write is validated by reloading the complete decoded tree and decoding every
modified texture. If anything fails, all touched Unity files are restored byte-for-byte.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter
import UnityPy

TARGETS = {
    "cloudc2": "cloud",
    "cloudc_light": "cloud",
    "sun pokemon": "sun",
    "stars_twinkly": "stars",
    "rainstar": "weather",
    "waterdropparticle": "weather",
    "watermistparticle": "weather",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def soft_bloom(img: Image.Image, radius: float, opacity: float) -> Image.Image:
    rgba = img.convert("RGBA")
    glow = rgba.filter(ImageFilter.GaussianBlur(radius))
    return Image.blend(rgba, glow, opacity)


def enhance(img: Image.Image, mode: str) -> Image.Image:
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    rgb = rgba.convert("RGB")

    if mode == "cloud":
        rgb = ImageEnhance.Color(rgb).enhance(1.08)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.07)
        rgb = ImageEnhance.Brightness(rgb).enhance(1.025)
        out = soft_bloom(rgb.convert("RGBA"), max(2.0, min(rgba.size) / 180.0), 0.08)
    elif mode == "sun":
        rgb = ImageEnhance.Color(rgb).enhance(1.10)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.035)
        rgb = ImageEnhance.Brightness(rgb).enhance(1.045)
        out = soft_bloom(rgb.convert("RGBA"), max(3.0, min(rgba.size) / 95.0), 0.13)
    elif mode == "stars":
        rgb = ImageEnhance.Contrast(rgb).enhance(1.16)
        rgb = ImageEnhance.Brightness(rgb).enhance(1.09)
        out = soft_bloom(rgb.convert("RGBA"), max(1.2, min(rgba.size) / 300.0), 0.06)
    else:
        rgb = ImageEnhance.Color(rgb).enhance(1.06)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.05)
        out = soft_bloom(rgb.convert("RGBA"), max(1.0, min(rgba.size) / 280.0), 0.05)

    out.putalpha(alpha)
    return out


def mip_count(texture: Any) -> int:
    count = getattr(texture, "m_MipCount", None)
    if isinstance(count, int) and count > 0:
        return count
    old_flag = getattr(texture, "m_MipMap", None)
    return 2 if old_flag else 1


def path_for_stream(root: Path, stream_name: str) -> Path:
    p = Path(stream_name)
    if p.exists() or Path(str(p) + ".split0").exists():
        return p

    # UnityPy normally keeps the path supplied by load_folder(), but tolerate versions
    # that reduce it to a basename.
    candidates = list(root.rglob(p.name))
    if len(candidates) == 1:
        return candidates[0]
    split_candidates = list(root.rglob(p.name + ".split0"))
    if len(split_candidates) == 1:
        return Path(str(split_candidates[0])[:-7])
    raise RuntimeError(f"cannot resolve Unity stream path {stream_name!r}")


def split_parts(base: Path) -> list[Path]:
    parts: list[Path] = []
    i = 0
    while True:
        p = Path(f"{base}.split{i}")
        if p.exists():
            parts.append(p)
            i += 1
            continue
        break
    return parts


def snapshot_path(base: Path) -> dict[Path, bytes]:
    parts = split_parts(base)
    if parts:
        return {p: p.read_bytes() for p in parts}
    if base.exists():
        return {base: base.read_bytes()}
    raise RuntimeError(f"Unity stream disappeared: {base}")


def restore(snapshot: dict[Path, bytes]) -> None:
    for p, blob in snapshot.items():
        p.write_bytes(blob)


def write_stream(base: Path, rebuilt: bytes, snapshot: dict[Path, bytes]) -> None:
    parts = split_parts(base)
    if not parts:
        if not base.exists():
            raise RuntimeError(f"no destination for rebuilt stream {base}")
        base.write_bytes(rebuilt)
        return

    originals = [snapshot[p] for p in parts]
    prefix_size = sum(len(x) for x in originals[:-1])
    if len(rebuilt) <= prefix_size:
        raise RuntimeError(
            f"{base}: rebuilt file is too small for original split boundaries "
            f"({len(rebuilt)} <= {prefix_size})"
        )

    offset = 0
    for i, (part, old) in enumerate(zip(parts, originals)):
        if i < len(parts) - 1:
            chunk = rebuilt[offset:offset + len(old)]
            if len(chunk) != len(old):
                raise RuntimeError(f"{base}: short split chunk {i}")
            part.write_bytes(chunk)
            offset += len(old)
        else:
            part.write_bytes(rebuilt[offset:])

    if b"".join(p.read_bytes() for p in parts) != rebuilt:
        raise RuntimeError(f"{base}: split reconstruction mismatch")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    root = args.root
    report = [
        "Pokemon environment enhancement report",
        "Policy: exact environment allowlist only; Pokemon character/model textures untouched.",
        "Method: load complete decoded APK tree; preserve texture format/mips; validate and rollback.",
    ]

    env = UnityPy.load(str(root))

    # Map top-level UnityPy file objects back to their on-disk stream names. mark_changed()
    # propagates from a SerializedFile inside a bundle to its top-level file.
    top_level = {id(item): (name, item) for name, item in env.files.items()}

    modified: list[tuple[str, int, tuple[int, int]]] = []
    owners: dict[int, Any] = {}

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue

        name = (obj.peek_name() or "").strip()
        mode = TARGETS.get(name.lower())
        if mode is None:
            continue

        texture = obj.read()
        image = texture.image
        size = image.size
        fmt = getattr(texture, "m_TextureFormat", None)
        mips = mip_count(texture)

        # Use set_image rather than assigning .image so the original mip count is retained.
        texture.set_image(enhance(image, mode), target_format=fmt, mipmap_count=mips)
        texture.save()

        owner = obj.assets_file
        if owner is None:
            raise RuntimeError(f"{name}: no owning SerializedFile")

        # Walk up until the direct child of Environment. File.mark_changed() propagates
        # along this same parent chain.
        top = owner
        while getattr(top, "parent", None) is not None and id(top) not in top_level:
            top = top.parent
        if id(top) not in top_level:
            # Some UnityPy versions use Environment as parent without exposing it as File.
            # Resolve by identity from env.files before giving up.
            match = next((item for item in env.files.values() if item is top), None)
            if match is None:
                raise RuntimeError(f"{name}: cannot resolve top-level Unity container")

        owners[id(top)] = top
        modified.append((name, obj.path_id, size))
        report.append(
            f"TARGET {name} path_id={obj.path_id} owner={getattr(owner, 'name', '?')} "
            f"format={fmt} mips={mips} size={size[0]}x{size[1]}"
        )

    if not modified:
        report.append("Environment textures enhanced: 0")
        report.append("NOTE: no allowlisted environment textures were found; original assets kept.")
        out = "\n".join(report) + "\n"
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(out, encoding="utf-8")
        print(out, end="")
        return

    snapshots: dict[Path, dict[Path, bytes]] = {}
    hashes_before: dict[Path, str] = {}

    try:
        changed_streams = []
        for stream_name, item in env.files.items():
            if not getattr(item, "is_changed", False):
                continue
            base = path_for_stream(root, stream_name)
            snap = snapshot_path(base)
            snapshots[base] = snap
            old_blob = b"".join(snap[p] for p in sorted(snap, key=lambda x: str(x)))
            hashes_before[base] = sha256(old_blob)

            rebuilt = item.save()
            if not rebuilt:
                raise RuntimeError(f"{stream_name}: UnityPy returned empty output")
            write_stream(base, rebuilt, snap)
            changed_streams.append((base, rebuilt))
            report.append(
                f"WRITE {base} bytes={len(rebuilt)} "
                f"sha256 {hashes_before[base][:12]} -> {sha256(rebuilt)[:12]}"
            )

        if not changed_streams:
            raise RuntimeError("textures were modified but UnityPy marked no top-level file changed")

        # Full-tree validation is crucial: it confirms Android split reconstruction,
        # external resource resolution and texture decoding all still work together.
        check = UnityPy.load(str(root))
        expected = {(name, path_id): size for name, path_id, size in modified}
        seen: dict[tuple[str, int], tuple[int, int]] = {}

        for obj in check.objects:
            if obj.type.name != "Texture2D":
                continue
            name = (obj.peek_name() or "").strip()
            key = (name, obj.path_id)
            if key not in expected:
                continue
            tex = obj.read()
            # Force decode, not merely metadata parsing.
            decoded = tex.image
            seen[key] = decoded.size

        missing = [key for key in expected if key not in seen]
        wrong = [
            (key, expected[key], seen.get(key))
            for key in expected
            if key in seen and seen[key] != expected[key]
        ]
        if missing or wrong:
            raise RuntimeError(f"post-write validation failed: missing={missing}, wrong={wrong}")

        report.append(f"Environment textures enhanced: {len(modified)}")
        report.append(f"Unity top-level files rewritten: {len(changed_streams)}")
        report.append("Validation: PASS (complete APK tree reloaded and every modified texture decoded)")

    except Exception as exc:
        for snap in snapshots.values():
            restore(snap)
        report.append(f"ROLLBACK: {exc!r}")
        report.append("Environment textures enhanced: 0")
        report.append("Validation: FAIL; original Unity bytes restored")

    out = "\n".join(report) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(out, encoding="utf-8")
    print(out, end="")


if __name__ == "__main__":
    main()
