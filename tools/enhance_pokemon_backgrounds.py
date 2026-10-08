#!/usr/bin/env python3
"""
Safely enhance environment textures used by Google's Pokemon live wallpaper.

The script only touches a small allowlist of environment/effect Texture2D names.
Pokemon character/model textures are never selected. Each modified Unity bundle is
reloaded and validated; if validation fails, the original bytes are restored.

This deliberately performs subtle color/lighting work instead of replacing Google's
art direction: slightly richer clouds, warmer sun bloom, clearer stars and softer
weather particles. The Pokemon character artwork remains untouched.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, ImageFilter
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

# Explicit guard for known/model-like Pokemon texture names. This is intentionally
# broader than the current APK inventory so future character additions are protected.
PROTECTED_HINTS = (
    "pikachu", "eevee", "grookey", "scorbunny", "sobble",
    "pokemon", "pm0", "body", "head", "eye", "mouth", "fur", "skin",
)


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
        out = rgb.convert("RGBA")
        out = soft_bloom(out, max(2.0, min(out.size) / 180.0), 0.08)
    elif mode == "sun":
        rgb = ImageEnhance.Color(rgb).enhance(1.10)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.035)
        rgb = ImageEnhance.Brightness(rgb).enhance(1.045)
        out = rgb.convert("RGBA")
        out = soft_bloom(out, max(3.0, min(out.size) / 95.0), 0.13)
    elif mode == "stars":
        rgb = ImageEnhance.Contrast(rgb).enhance(1.16)
        rgb = ImageEnhance.Brightness(rgb).enhance(1.09)
        out = rgb.convert("RGBA")
        out = soft_bloom(out, max(1.2, min(out.size) / 300.0), 0.06)
    else:
        rgb = ImageEnhance.Color(rgb).enhance(1.06)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.05)
        out = rgb.convert("RGBA")
        out = soft_bloom(out, max(1.0, min(out.size) / 280.0), 0.05)

    out.putalpha(alpha)
    return out


def eligible(name: str) -> tuple[bool, str | None]:
    key = (name or "").strip().lower()
    mode = TARGETS.get(key)
    if not mode:
        return False, None

    # Exact-target names win, but still reject anything that looks like a model texture
    # unless it is the one known environment texture "Sun pokemon".
    if key != "sun pokemon" and any(h in key for h in PROTECTED_HINTS):
        return False, None
    return True, mode


def patch_bundle(path: Path, report: list[str]) -> int:
    original = path.read_bytes()
    try:
        env = UnityPy.load(str(path))
    except Exception as exc:
        report.append(f"SKIP {path}: load failed: {exc!r}")
        return 0

    changed: list[str] = []
    dimensions: dict[str, tuple[int, int]] = {}

    try:
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            data = obj.read()
            name = getattr(data, "m_Name", "") or ""
            ok, mode = eligible(name)
            if not ok or mode is None:
                continue

            image = data.image
            dimensions[name] = image.size
            data.image = enhance(image, mode)
            data.save()
            changed.append(name)

        if not changed:
            return 0

        rebuilt = env.file.save()
        if not rebuilt:
            raise RuntimeError("UnityPy returned an empty bundle")
        path.write_bytes(rebuilt)

        # Reload and verify every modified texture still decodes at its original dimensions.
        check = UnityPy.load(str(path))
        seen: dict[str, tuple[int, int]] = {}
        for obj in check.objects:
            if obj.type.name != "Texture2D":
                continue
            data = obj.read()
            name = getattr(data, "m_Name", "") or ""
            if name in dimensions:
                seen[name] = data.image.size

        for name, size in dimensions.items():
            if seen.get(name) != size:
                raise RuntimeError(
                    f"validation failed for {name}: expected {size}, got {seen.get(name)}"
                )

        report.append(
            f"PATCH {path}: {', '.join(sorted(changed))} "
            f"sha256 {sha256(original)[:12]} -> {sha256(path.read_bytes())[:12]}"
        )
        return len(changed)

    except Exception as exc:
        path.write_bytes(original)
        report.append(f"RESTORE {path}: {exc!r}")
        return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    report = [
        "Pokemon environment enhancement report",
        "Policy: exact allowlist only; Pokemon character/model textures are untouched.",
    ]

    bundle_paths = sorted(args.root.rglob("*.bundle"))
    report.append(f"Bundles scanned: {len(bundle_paths)}")

    changed = 0
    for bundle in bundle_paths:
        changed += patch_bundle(bundle, report)

    report.append(f"Environment textures enhanced: {changed}")
    if changed == 0:
        report.append(
            "NOTE: no allowlisted environment texture was writable in an addressable bundle; "
            "the original Google wallpaper remains intact."
        )

    text = "\n".join(report) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
