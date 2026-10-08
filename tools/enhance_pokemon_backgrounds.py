#!/usr/bin/env python3
"""
Safely enhance environment textures used by Google's Pokemon live wallpaper.

Only an exact allowlist of environment/effect Texture2D names is touched. Pokemon
character/model textures are never selected.

PixelLiveWallpaper stores Unity SerializedFiles as Android .assets.splitN chunks.
UnityPy can decode Unity-Crunch ETC textures, but it cannot re-encode valid Crunch
payloads for these assets. Crunched ETC inputs are therefore rewritten as the
equivalent non-crunched ETC/ETC2 formats while preserving the complete mip chain.
The rebuilt SerializedFile is then split back into the original fixed chunk size.

Every first-time write is validated by reloading the complete decoded APK tree and
decoding all modified textures. Any failure restores the original split files.
A marker inside the APK makes later runs idempotent and is itself validated.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter
import UnityPy
from UnityPy.enums import TextureFormat
from UnityPy.export import Texture2DConverter

TARGETS = {
    "cloudc2": "cloud",
    "cloudc_light": "cloud",
    "sun pokemon": "sun",
    "stars_twinkly": "stars",
    "rainstar": "weather",
    "waterdropparticle": "weather",
    "watermistparticle": "weather",
}

MARKER_REL = Path("assets") / "pokemon_environment_enhanced_v2.txt"

CRUNCHED_TO_PLAIN = {
    TextureFormat.ETC_RGB4Crunched: TextureFormat.ETC_RGB4,
    TextureFormat.ETC2_RGBA8Crunched: TextureFormat.ETC2_RGBA8,
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
    return 2 if getattr(texture, "m_MipMap", False) else 1


def writable_format(fmt: Any) -> TextureFormat:
    parsed = TextureFormat(int(fmt))
    return CRUNCHED_TO_PLAIN.get(parsed, parsed)


def encode_texture(texture: Any, image: Image.Image, target: TextureFormat, mips: int) -> tuple[TextureFormat, int]:
    """Encode every mip explicitly, including 2x2 and 1x1 levels."""
    platform = texture.object_reader.platform if texture.object_reader is not None else 0
    platform_blob = getattr(texture, "m_PlatformBlob", None)

    current = image.convert("RGBA")
    payload = bytearray()
    encoded_format: TextureFormat | None = None
    actual_mips = 0

    for level in range(max(1, mips)):
        data, fmt = Texture2DConverter.image_to_texture2d(
            current, target, platform, platform_blob
        )
        fmt = TextureFormat(int(fmt))
        if encoded_format is None:
            encoded_format = fmt
        elif fmt != encoded_format:
            raise RuntimeError(
                f"mip {level}: format changed from {encoded_format.name} to {fmt.name}"
            )
        payload.extend(data)
        actual_mips += 1

        if current.width == 1 and current.height == 1:
            break
        current = current.resize(
            (max(1, current.width // 2), max(1, current.height // 2)),
            Image.Resampling.BICUBIC,
        )

    if encoded_format is None:
        raise RuntimeError("texture encoder produced no data")
    if encoded_format != target:
        raise RuntimeError(
            f"texture encoder returned {encoded_format.name}, expected {target.name}"
        )
    if actual_mips != max(1, mips):
        raise RuntimeError(
            f"could not preserve mip count {mips}; encoded {actual_mips}"
        )

    texture.m_Width = image.width
    texture.m_Height = image.height
    if getattr(texture, "m_MipMap", None) is not None:
        texture.m_MipMap = actual_mips > 1
    if getattr(texture, "m_MipCount", None) is not None:
        texture.m_MipCount = actual_mips
    texture.image_data = bytes(payload)
    texture.m_CompleteImageSize = len(payload)
    texture.m_TextureFormat = encoded_format

    stream = getattr(texture, "m_StreamData", None)
    if stream is not None:
        stream.path = ""
        stream.offset = 0
        stream.size = 0

    return encoded_format, actual_mips


def path_for_stream(root: Path, stream_name: str) -> Path:
    p = Path(stream_name)
    if p.exists() or Path(str(p) + ".split0").exists():
        return p

    candidates = list(root.rglob(p.name))
    if len(candidates) == 1:
        return candidates[0]
    split_candidates = list(root.rglob(p.name + ".split0"))
    if len(split_candidates) == 1:
        return Path(str(split_candidates[0])[:-7])
    raise RuntimeError(f"cannot resolve Unity stream path {stream_name!r}")


def split_parts(base: Path) -> list[Path]:
    out: list[Path] = []
    for i in range(999):
        p = Path(f"{base}.split{i}")
        if p.exists():
            out.append(p)
        elif i:
            break
    return out


def snapshot_path(base: Path) -> dict[Path, bytes]:
    parts = split_parts(base)
    if parts:
        return {p: p.read_bytes() for p in parts}
    if base.exists():
        return {base: base.read_bytes()}
    raise RuntimeError(f"Unity stream disappeared: {base}")


def snapshot_bytes(base: Path, snapshot: dict[Path, bytes]) -> bytes:
    if Path(f"{base}.split0") in snapshot:
        return b"".join(
            snapshot[Path(f"{base}.split{i}")]
            for i in range(len(snapshot))
        )
    return snapshot[base]


def restore(base: Path, snapshot: dict[Path, bytes]) -> None:
    if Path(f"{base}.split0") in snapshot:
        for i in range(999):
            p = Path(f"{base}.split{i}")
            if p.exists():
                p.unlink()
            elif i:
                break
    for p, data in snapshot.items():
        p.write_bytes(data)


def write_stream(base: Path, rebuilt: bytes, snapshot: dict[Path, bytes]) -> int:
    parts = split_parts(base)
    if not parts:
        if not base.exists():
            raise RuntimeError(f"no destination for rebuilt stream {base}")
        base.write_bytes(rebuilt)
        return 1

    original = [snapshot[p] for p in parts]
    chunk_size = len(original[0])
    if chunk_size <= 0:
        raise RuntimeError(f"{base}: zero-sized split chunk")
    if any(len(blob) != chunk_size for blob in original[:-1]):
        raise RuntimeError(f"{base}: original split layout is not fixed-size")

    needed = max(1, (len(rebuilt) + chunk_size - 1) // chunk_size)
    for i in range(needed):
        start = i * chunk_size
        Path(f"{base}.split{i}").write_bytes(rebuilt[start:start + chunk_size])
    for i in range(needed, len(parts)):
        p = Path(f"{base}.split{i}")
        if p.exists():
            p.unlink()

    joined = b"".join(
        Path(f"{base}.split{i}").read_bytes() for i in range(needed)
    )
    if joined != rebuilt:
        raise RuntimeError(f"{base}: split reconstruction mismatch")
    if any(
        Path(f"{base}.split{i}").stat().st_size != chunk_size
        for i in range(max(0, needed - 1))
    ):
        raise RuntimeError(f"{base}: rebuilt split chunk size mismatch")

    return needed


def write_report(path: Path | None, report: list[str]) -> None:
    text = "\n".join(report) + "\n"
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text, end="")


def validate_existing(root: Path, env: Any, report: list[str]) -> bool:
    marker = root / MARKER_REL
    if not marker.exists():
        return False

    seen = set()
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        name = (obj.peek_name() or "").strip()
        key = name.lower()
        if key not in TARGETS:
            continue

        tex = obj.read()
        decoded = tex.image.convert("RGBA")
        fmt = TextureFormat(int(getattr(tex, "m_TextureFormat")))
        if fmt in CRUNCHED_TO_PLAIN:
            raise RuntimeError(f"{name}: marker exists but texture is still {fmt.name}")
        seen.add(key)
        report.append(
            f"VERIFY_EXISTING {name} path_id={obj.path_id} "
            f"format={fmt.name}({int(fmt)}) mips={mip_count(tex)} "
            f"size={decoded.size[0]}x{decoded.size[1]} "
            f"pixels={sha256(decoded.tobytes())[:12]}"
        )

    if seen != set(TARGETS):
        raise RuntimeError(
            f"marker validation missing targets: {sorted(set(TARGETS) - seen)}"
        )

    report.append(f"Environment textures enhanced: {len(seen)} (already present)")
    report.append("Validation: PASS (marker present and all enhanced textures decode)")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    root = args.root
    report = [
        "Pokemon environment enhancement report",
        "Policy: exact environment allowlist only; Pokemon character/model textures untouched.",
        "Encoding: Unity-Crunch ETC -> plain ETC/ETC2; complete mip chain preserved.",
        "Split policy: fixed Android chunk size; add/remove .splitN pieces as needed.",
    ]

    env = UnityPy.load(str(root))

    try:
        if validate_existing(root, env, report):
            write_report(args.report, report)
            return
    except Exception as exc:
        report.append(f"Validation: FAIL; invalid existing marker: {exc!r}")
        write_report(args.report, report)
        raise SystemExit(1)

    top_level = {id(item): (name, item) for name, item in env.files.items()}
    modified: list[dict[str, Any]] = []

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue

        name = (obj.peek_name() or "").strip()
        mode = TARGETS.get(name.lower())
        if mode is None:
            continue

        texture = obj.read()
        original = texture.image.convert("RGBA")
        source_format = TextureFormat(int(getattr(texture, "m_TextureFormat")))
        target_format = writable_format(source_format)
        mips = mip_count(texture)

        encoded_format, encoded_mips = encode_texture(
            texture, enhance(original, mode), target_format, mips
        )
        texture.save()

        owner = obj.assets_file
        if owner is None:
            raise RuntimeError(f"{name}: no owning SerializedFile")
        top = owner
        while getattr(top, "parent", None) is not None and id(top) not in top_level:
            top = top.parent
        if id(top) not in top_level:
            raise RuntimeError(f"{name}: cannot resolve top-level Unity container")

        modified.append(
            {
                "name": name,
                "path_id": obj.path_id,
                "size": original.size,
                "format": int(encoded_format),
                "mips": encoded_mips,
                "original_pixels": sha256(original.tobytes()),
            }
        )
        report.append(
            f"TARGET {name} path_id={obj.path_id} owner={getattr(owner, 'name', '?')} "
            f"format={source_format.name}({int(source_format)})->"
            f"{encoded_format.name}({int(encoded_format)}) "
            f"mips={encoded_mips} size={original.size[0]}x{original.size[1]}"
        )

    found = {item["name"].lower() for item in modified}
    if found != set(TARGETS):
        report.append(
            f"Validation: FAIL; missing allowlisted textures "
            f"{sorted(set(TARGETS) - found)}"
        )
        write_report(args.report, report)
        raise SystemExit(1)

    snapshots: dict[Path, dict[Path, bytes]] = {}
    failed = False

    try:
        changed_streams = 0
        for stream_name, item in env.files.items():
            if not getattr(item, "is_changed", False):
                continue

            base = path_for_stream(root, stream_name)
            snap = snapshot_path(base)
            snapshots[base] = snap
            before = snapshot_bytes(base, snap)
            rebuilt = item.save()
            if not rebuilt:
                raise RuntimeError(f"{stream_name}: UnityPy returned empty output")

            parts = write_stream(base, rebuilt, snap)
            changed_streams += 1
            report.append(
                f"WRITE {base} bytes={len(rebuilt)} parts={parts} "
                f"sha256 {sha256(before)[:12]}->{sha256(rebuilt)[:12]}"
            )

        if not changed_streams:
            raise RuntimeError("textures changed but no Unity top-level file was marked changed")

        check = UnityPy.load(str(root))
        expected = {(x["name"], x["path_id"]): x for x in modified}
        seen: dict[tuple[str, int], dict[str, Any]] = {}

        for obj in check.objects:
            if obj.type.name != "Texture2D":
                continue
            name = (obj.peek_name() or "").strip()
            key = (name, obj.path_id)
            if key not in expected:
                continue

            tex = obj.read()
            decoded = tex.image.convert("RGBA")
            state = {
                "size": decoded.size,
                "format": int(getattr(tex, "m_TextureFormat")),
                "mips": mip_count(tex),
                "pixels": sha256(decoded.tobytes()),
            }
            seen[key] = state
            report.append(
                f"VERIFY {name} path_id={obj.path_id} "
                f"format={TextureFormat(state['format']).name}({state['format']}) "
                f"mips={state['mips']} size={decoded.size[0]}x{decoded.size[1]} "
                f"pixels={state['pixels'][:12]}"
            )

        missing = [k for k in expected if k not in seen]
        bad = [
            k for k in expected
            if k in seen and (
                seen[k]["size"] != expected[k]["size"]
                or seen[k]["format"] != expected[k]["format"]
                or seen[k]["mips"] != expected[k]["mips"]
            )
        ]
        changed_pixels = [
            k for k in expected
            if k in seen and seen[k]["pixels"] != expected[k]["original_pixels"]
        ]
        if missing or bad:
            raise RuntimeError(f"post-write validation failed: missing={missing}, bad={bad}")
        if not changed_pixels:
            raise RuntimeError("decoded pixels did not change")

        marker = root / MARKER_REL
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            "pokemon-environment-enhanced-v2\n"
            f"textures={len(modified)}\n"
            f"serialized_files={changed_streams}\n",
            encoding="utf-8",
        )

        report.append(f"Environment textures enhanced: {len(modified)}")
        report.append(f"Decoded textures with changed pixel content: {len(changed_pixels)}")
        report.append(f"Unity top-level files rewritten: {changed_streams}")
        report.append(f"Marker: {MARKER_REL.as_posix()}")
        report.append("Validation: PASS (split tree reloaded and every modified texture decoded)")

    except Exception as exc:
        for base, snap in snapshots.items():
            restore(base, snap)
        report.append(f"ROLLBACK: {exc!r}")
        report.append("Environment textures enhanced: 0")
        report.append("Validation: FAIL; original Unity bytes restored")
        failed = True

    write_report(args.report, report)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
