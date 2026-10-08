#!/usr/bin/env python3
"""
Safely enhance environment textures used by Google's Pokemon live wallpaper.

Only an exact allowlist of environment/effect Texture2D names is touched. Pokemon
character/model textures are never selected. The script supports both normal Unity
asset bundles and Unity's sharedassets*.assets.splitN layout used by PixelLiveWallpaper.

Every rebuilt Unity file is reloaded and validated before it replaces the APK asset.
If anything fails, the original bytes stay in place.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import tempfile
from pathlib import Path

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


def edit_environment(env) -> tuple[list[str], dict[str, tuple[int, int]]]:
    changed: list[str] = []
    dimensions: dict[str, tuple[int, int]] = {}

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        name = (getattr(data, "m_Name", "") or "").strip()
        mode = TARGETS.get(name.lower())
        if mode is None:
            continue

        image = data.image
        dimensions[name] = image.size
        data.image = enhance(image, mode)
        data.save()
        changed.append(name)

    return changed, dimensions


def rebuild_unity_blob(blob: bytes, label: str) -> tuple[bytes | None, list[str]]:
    """Return rebuilt bytes and changed texture names; None means no target texture."""
    with tempfile.TemporaryDirectory(prefix="pokemon-bg-") as td:
        src = Path(td) / "asset"
        src.write_bytes(blob)
        env = UnityPy.load(str(src))
        changed, dimensions = edit_environment(env)
        if not changed:
            return None, []

        rebuilt = env.file.save()
        if not rebuilt:
            raise RuntimeError(f"{label}: UnityPy returned empty output")

        check_path = Path(td) / "check"
        check_path.write_bytes(rebuilt)
        check = UnityPy.load(str(check_path))
        seen: dict[str, tuple[int, int]] = {}
        for obj in check.objects:
            if obj.type.name != "Texture2D":
                continue
            data = obj.read()
            name = (getattr(data, "m_Name", "") or "").strip()
            if name in dimensions:
                seen[name] = data.image.size

        for name, size in dimensions.items():
            if seen.get(name) != size:
                raise RuntimeError(
                    f"{label}: validation failed for {name}: "
                    f"expected {size}, got {seen.get(name)}"
                )

        return rebuilt, changed


def patch_regular(path: Path, report: list[str]) -> int:
    original = path.read_bytes()
    try:
        rebuilt, changed = rebuild_unity_blob(original, str(path))
        if rebuilt is None:
            return 0
        path.write_bytes(rebuilt)
        report.append(
            f"PATCH {path}: {', '.join(sorted(changed))} "
            f"sha256 {sha256(original)[:12]} -> {sha256(rebuilt)[:12]}"
        )
        return len(changed)
    except Exception as exc:
        path.write_bytes(original)
        report.append(f"RESTORE {path}: {exc!r}")
        return 0


SPLIT_RE = re.compile(r"^(?P<base>.+\.assets)\.split(?P<index>\d+)$")


def find_split_groups(root: Path) -> list[list[Path]]:
    groups: dict[Path, list[tuple[int, Path]]] = {}
    for p in root.rglob("*.assets.split*"):
        m = SPLIT_RE.match(p.name)
        if not m:
            continue
        base = p.with_name(m.group("base"))
        groups.setdefault(base, []).append((int(m.group("index")), p))

    out: list[list[Path]] = []
    for _, parts in sorted(groups.items(), key=lambda item: str(item[0])):
        parts.sort(key=lambda item: item[0])
        # Only accept contiguous split numbering. Anything odd is left untouched.
        if [i for i, _ in parts] != list(range(len(parts))):
            continue
        out.append([p for _, p in parts])
    return out


def patch_split(parts: list[Path], report: list[str]) -> int:
    originals = [p.read_bytes() for p in parts]
    blob = b"".join(originals)
    label = parts[0].with_name(parts[0].name.rsplit(".split", 1)[0])

    try:
        rebuilt, changed = rebuild_unity_blob(blob, str(label))
        if rebuilt is None:
            return 0

        # Keep every original boundary except the final chunk. This preserves Unity's
        # expected split ordering while allowing the re-encoded final segment to vary.
        prefix_size = sum(len(x) for x in originals[:-1])
        if len(rebuilt) <= prefix_size:
            raise RuntimeError(
                f"{label}: rebuilt asset shrank across a split boundary "
                f"({len(rebuilt)} <= {prefix_size})"
            )

        offset = 0
        for i, (path, original) in enumerate(zip(parts, originals)):
            if i < len(parts) - 1:
                chunk = rebuilt[offset:offset + len(original)]
                if len(chunk) != len(original):
                    raise RuntimeError(f"{label}: short split {i}")
                path.write_bytes(chunk)
                offset += len(original)
            else:
                path.write_bytes(rebuilt[offset:])

        # Byte-for-byte concatenation must reconstruct exactly what UnityPy validated.
        if b"".join(p.read_bytes() for p in parts) != rebuilt:
            raise RuntimeError(f"{label}: split reconstruction mismatch")

        report.append(
            f"PATCH {label} ({len(parts)} splits): {', '.join(sorted(changed))} "
            f"sha256 {sha256(blob)[:12]} -> {sha256(rebuilt)[:12]}"
        )
        return len(changed)

    except Exception as exc:
        for path, original in zip(parts, originals):
            path.write_bytes(original)
        report.append(f"RESTORE {label}: {exc!r}")
        return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    report = [
        "Pokemon environment enhancement report",
        "Policy: exact environment allowlist only; Pokemon character/model textures untouched.",
    ]

    changed = 0

    bundles = sorted(args.root.rglob("*.bundle"))
    report.append(f"AssetBundles scanned: {len(bundles)}")
    for path in bundles:
        changed += patch_regular(path, report)

    # Normal unsplit SerializedFiles, if a future PixelLiveWallpaper build uses them.
    split_parts = {p for group in find_split_groups(args.root) for p in group}
    assets = [
        p for p in sorted(args.root.rglob("*.assets"))
        if p not in split_parts
    ]
    report.append(f"Unsplit .assets scanned: {len(assets)}")
    for path in assets:
        changed += patch_regular(path, report)

    split_groups = find_split_groups(args.root)
    report.append(f"Split sharedassets groups scanned: {len(split_groups)}")
    for parts in split_groups:
        changed += patch_split(parts, report)

    report.append(f"Environment textures enhanced: {changed}")
    if changed == 0:
        report.append(
            "NOTE: no allowlisted environment texture could be safely rebuilt; "
            "the original Google Pokemon wallpaper assets remain intact."
        )

    text = "\n".join(report) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
