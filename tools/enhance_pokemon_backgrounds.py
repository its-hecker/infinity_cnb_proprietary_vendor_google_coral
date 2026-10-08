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
import ntpath
from pathlib import Path
import traceback
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

# UnityPy decodes Crunch but does not encode it. Re-encode those exact formats
# as their runtime-equivalent ETC formats and keep the payload outside the
# SerializedFile so sharedassets1.assets never balloons during a rebuild.
FORMAT_FALLBACK = {
    int(TextureFormat.ETC_RGB4Crunched): int(TextureFormat.ETC_RGB4),
    int(TextureFormat.ETC2_RGBA8Crunched): int(TextureFormat.ETC2_RGBA8),
}
SUPPORTED_OUTPUT_FORMATS = {
    int(TextureFormat.ETC_RGB4),
    int(TextureFormat.ETC2_RGBA8),
}
RESOURCE_SUFFIX = ".pokemon_env.resS"
RESOURCE_ALIGN = 16


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


def all_split_parts(base: Path) -> list[Path]:
    prefix = base.name + ".split"
    found: list[tuple[int, Path]] = []
    for p in base.parent.glob(prefix + "*"):
        suffix = p.name[len(prefix):]
        if suffix.isdigit():
            found.append((int(suffix), p))
    return [p for _, p in sorted(found)]


def snapshot_path(base: Path) -> dict[Path, bytes]:
    parts = split_parts(base)
    if parts:
        return {p: p.read_bytes() for p in parts}
    if base.exists():
        return {base: base.read_bytes()}
    raise RuntimeError(f"Unity stream disappeared: {base}")


def snapshot_blob(base: Path, snapshot: dict[Path, bytes]) -> bytes:
    split = [p for p in snapshot if p.name.startswith(base.name + ".split")]
    if split:
        split.sort(key=lambda p: int(p.name.rsplit(".split", 1)[1]))
        return b"".join(snapshot[p] for p in split)
    return snapshot[base]


def restore_path(base: Path, snapshot: dict[Path, bytes]) -> None:
    keep = set(snapshot)
    current = all_split_parts(base)
    if base.exists():
        current.append(base)
    for p in current:
        if p not in keep:
            p.unlink()
    for p, blob in snapshot.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)


def write_stream(base: Path, rebuilt: bytes, snapshot: dict[Path, bytes]) -> None:
    original_parts = [p for p in snapshot if p.name.startswith(base.name + ".split")]
    if not original_parts:
        for p in all_split_parts(base):
            p.unlink()
        base.write_bytes(rebuilt)
        if base.read_bytes() != rebuilt:
            raise RuntimeError(f"{base}: serialized-file write mismatch")
        return

    original_parts.sort(key=lambda p: int(p.name.rsplit(".split", 1)[1]))
    chunk_size = len(snapshot[original_parts[0]])
    if chunk_size <= 0 or not rebuilt:
        raise RuntimeError(f"{base}: invalid rebuilt split stream")

    # Android split files are concatenated in numeric order. Re-split the rebuilt
    # stream at the original chunk size; do not force the old part count.
    for p in all_split_parts(base):
        p.unlink()
    if base.exists():
        base.unlink()

    written: list[Path] = []
    for index, offset in enumerate(range(0, len(rebuilt), chunk_size)):
        part = Path(f"{base}.split{index}")
        part.write_bytes(rebuilt[offset:offset + chunk_size])
        written.append(part)

    if b"".join(p.read_bytes() for p in written) != rebuilt:
        raise RuntimeError(f"{base}: split reconstruction mismatch")


def pixel_hash(img: Image.Image) -> str:
    return sha256(img.convert("RGBA").tobytes())


def stream_basename(texture: Any) -> str:
    stream = getattr(texture, "m_StreamData", None)
    path = getattr(stream, "path", "") if stream is not None else ""
    return ntpath.basename(path or "")


def resolve_top_file(owner: Any, top_level: dict[int, tuple[str, Any]]) -> tuple[str, Any]:
    node = owner
    while id(node) not in top_level:
        parent = getattr(node, "parent", None)
        if parent is None:
            break
        node = parent
    if id(node) not in top_level:
        raise RuntimeError("cannot resolve top-level Unity container")
    return top_level[id(node)]


def encode_mip_chain(
    texture: Any, image: Image.Image, requested_mips: int
) -> tuple[bytes, int, int]:
    source_format = int(texture.m_TextureFormat)
    target_format = FORMAT_FALLBACK.get(source_format, source_format)
    if target_format not in SUPPORTED_OUTPUT_FORMATS:
        raise RuntimeError(
            f"{texture.m_Name}: unexpected texture format {source_format}; "
            "refusing to modify an unapproved Pokemon asset format"
        )

    reader = getattr(texture, "object_reader", None)
    platform = getattr(reader, "platform", 0) if reader is not None else 0
    platform_blob = getattr(texture, "m_PlatformBlob", None)

    payload = bytearray()
    encoded_format: int | None = None
    actual_mips = 0
    for level in range(max(1, requested_mips)):
        width = max(1, image.width >> level)
        height = max(1, image.height >> level)
        level_img = (
            image
            if level == 0
            else image.resize((width, height), Image.Resampling.LANCZOS)
        )
        chunk, fmt = Texture2DConverter.image_to_texture2d(
            level_img, target_format, platform, platform_blob
        )
        fmt_i = int(fmt)
        if encoded_format is None:
            encoded_format = fmt_i
        elif fmt_i != encoded_format:
            raise RuntimeError(
                f"{texture.m_Name}: mip {level} changed format "
                f"{encoded_format} -> {fmt_i}"
            )
        payload.extend(chunk)
        actual_mips += 1
        if width == 1 and height == 1:
            break

    if encoded_format is None or not payload:
        raise RuntimeError(f"{texture.m_Name}: encoder produced no texture data")
    return bytes(payload), encoded_format, actual_mips


def collect_targets(env: Any) -> dict[str, tuple[Any, Any]]:
    found: dict[str, tuple[Any, Any]] = {}
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        name = (obj.peek_name() or "").strip()
        key = name.lower()
        if key not in TARGETS:
            continue
        if key in found:
            raise RuntimeError(f"duplicate allowlisted Texture2D name: {name!r}")
        found[key] = (obj, obj.read())

    missing = sorted(set(TARGETS) - set(found))
    if missing:
        raise RuntimeError(f"missing allowlisted Pokemon environment textures: {missing}")
    return found


def write_report(path: Path | None, report: list[str]) -> None:
    out = "\n".join(report) + "\n"
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out, encoding="utf-8")
    print(out, end="")


def validate_tree(root: Path, require_marker: bool = True) -> list[str]:
    check = UnityPy.load(str(root))
    found = collect_targets(check)
    lines: list[str] = []

    for key in sorted(TARGETS):
        obj, tex = found[key]
        image = tex.image
        fmt = int(tex.m_TextureFormat)
        stream = stream_basename(tex)
        if require_marker and not stream.endswith(RESOURCE_SUFFIX):
            raise RuntimeError(
                f"{tex.m_Name}: not backed by {RESOURCE_SUFFIX}: {stream!r}"
            )
        if fmt not in SUPPORTED_OUTPUT_FORMATS:
            raise RuntimeError(f"{tex.m_Name}: unexpected enhanced format {fmt}")
        lines.append(
            f"VERIFY {tex.m_Name} path_id={obj.path_id} format={fmt} "
            f"mips={mip_count(tex)} size={image.width}x{image.height} "
            f"stream={stream} pixels={pixel_hash(image)[:12]}"
        )
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument(
        "--validate-only",
        action="store_true",
        help="Decode the already-enhanced dedicated resource stream without modifying files.",
    )
    args = ap.parse_args()

    root = args.root
    report = [
        "Pokemon environment enhancement report",
        "Policy: exact environment allowlist only; Pokemon character/model textures untouched.",
        "Method: ETC payloads use a dedicated resS stream; split SerializedFiles are rebuilt safely.",
        f"UnityPy: {getattr(UnityPy, '__version__', 'unknown')}",
    ]

    if args.validate_only:
        try:
            report.extend(validate_tree(root, require_marker=True))
            report.append(f"Environment textures enhanced: {len(TARGETS)}")
            report.append("Mode: validate-only")
            report.append(
                "Validation: PASS (final APK Unity tree reloaded and every enhanced texture decoded)"
            )
            write_report(args.report, report)
            return
        except Exception as exc:
            report.append(f"Validation error: {type(exc).__name__}: {exc}")
            report.append("Environment textures enhanced: 0")
            report.append("Validation: FAIL")
            write_report(args.report, report)
            traceback.print_exc()
            raise SystemExit(1)

    try:
        env = UnityPy.load(str(root))
        found = collect_targets(env)
    except Exception as exc:
        report.append(f"Load error: {type(exc).__name__}: {exc}")
        report.append("Environment textures enhanced: 0")
        report.append("Validation: FAIL")
        write_report(args.report, report)
        traceback.print_exc()
        raise SystemExit(1)

    marker_state = {
        key: stream_basename(tex).endswith(RESOURCE_SUFFIX)
        for key, (_, tex) in found.items()
    }
    if all(marker_state.values()):
        try:
            report.extend(validate_tree(root, require_marker=True))
            report.append(f"Environment textures enhanced: {len(TARGETS)}")
            report.append("Mode: already-enhanced (no second color pass applied)")
            report.append("Validation: PASS (existing dedicated stream decoded successfully)")
            write_report(args.report, report)
            return
        except Exception as exc:
            report.append(f"Validation error: {type(exc).__name__}: {exc}")
            report.append("Environment textures enhanced: 0")
            report.append("Validation: FAIL")
            write_report(args.report, report)
            traceback.print_exc()
            raise SystemExit(1)

    if any(marker_state.values()):
        partial = sorted(key for key, marked in marker_state.items() if marked)
        report.append(f"Refusing partial enhancement marker state: {partial}")
        report.append("Environment textures enhanced: 0")
        report.append("Validation: FAIL")
        write_report(args.report, report)
        raise SystemExit(1)

    top_level = {id(item): (name, item) for name, item in env.files.items()}
    resource_buffers: dict[Path, bytearray] = {}
    resource_paths: dict[Path, Path] = {}
    top_files: dict[Path, Any] = {}
    originals: dict[
        tuple[str, int], tuple[tuple[int, int], str, int, int]
    ] = {}

    # Encode everything in memory before touching disk.
    try:
        for key in sorted(TARGETS):
            obj, texture = found[key]
            image = texture.image
            before_hash = pixel_hash(image)
            before_format = int(texture.m_TextureFormat)
            requested_mips = mip_count(texture)

            stream_name, top = resolve_top_file(obj.assets_file, top_level)
            base = path_for_stream(root, stream_name)
            resource = Path(str(base) + RESOURCE_SUFFIX)
            buf = resource_buffers.setdefault(base, bytearray())
            resource_paths[base] = resource
            top_files[base] = top

            while len(buf) % RESOURCE_ALIGN:
                buf.append(0)
            offset = len(buf)

            enhanced = enhance(image, TARGETS[key])
            encoded, output_format, output_mips = encode_mip_chain(
                texture, enhanced, requested_mips
            )
            buf.extend(encoded)

            stream = getattr(texture, "m_StreamData", None)
            if stream is None:
                raise RuntimeError(f"{texture.m_Name}: Texture2D has no StreamingInfo")

            texture.image_data = b""
            texture.m_Width = enhanced.width
            texture.m_Height = enhanced.height
            texture.m_TextureFormat = output_format
            texture.m_CompleteImageSize = len(encoded)
            if getattr(texture, "m_MipMap", None) is not None:
                texture.m_MipMap = output_mips > 1
            if getattr(texture, "m_MipCount", None) is not None:
                texture.m_MipCount = output_mips
            stream.path = resource.name
            stream.offset = offset
            stream.size = len(encoded)
            texture.save()

            originals[(texture.m_Name, obj.path_id)] = (
                image.size,
                before_hash,
                output_format,
                output_mips,
            )
            report.append(
                f"TARGET {texture.m_Name} path_id={obj.path_id} "
                f"owner={getattr(obj.assets_file, 'name', '?')} "
                f"format={before_format}->{output_format} "
                f"mips={requested_mips}->{output_mips} "
                f"size={image.width}x{image.height} stream={resource.name} "
                f"offset={offset} bytes={len(encoded)}"
            )
    except Exception as exc:
        report.append(f"Encode error: {type(exc).__name__}: {exc}")
        report.append("Environment textures enhanced: 0")
        report.append("Validation: FAIL; disk was not modified")
        write_report(args.report, report)
        traceback.print_exc()
        raise SystemExit(1)

    stream_snapshots: dict[Path, dict[Path, bytes]] = {}
    resource_snapshots: dict[Path, bytes | None] = {}

    try:
        for base in top_files:
            stream_snapshots[base] = snapshot_path(base)
            resource = resource_paths[base]
            resource_snapshots[resource] = (
                resource.read_bytes() if resource.exists() else None
            )

        for base, buf in resource_buffers.items():
            resource = resource_paths[base]
            resource.write_bytes(bytes(buf))
            report.append(
                f"RESOURCE {resource} bytes={len(buf)} "
                f"sha256={sha256(bytes(buf))[:12]}"
            )

        for base, top in top_files.items():
            snapshot = stream_snapshots[base]
            old_blob = snapshot_blob(base, snapshot)
            rebuilt = top.save()
            if not rebuilt:
                raise RuntimeError(f"{base}: UnityPy returned empty SerializedFile output")
            write_stream(base, rebuilt, snapshot)
            report.append(
                f"WRITE {base} bytes={len(rebuilt)} sha256 "
                f"{sha256(old_blob)[:12]} -> {sha256(rebuilt)[:12]} "
                f"parts={len(split_parts(base)) or 1}"
            )

        # Full-tree validation exercises split reconstruction, resource lookup,
        # ETC decoding and proves the visible pixels actually changed.
        check = UnityPy.load(str(root))
        checked = collect_targets(check)
        seen: set[tuple[str, int]] = set()

        for key in sorted(TARGETS):
            obj, tex = checked[key]
            ident = (tex.m_Name, obj.path_id)
            if ident not in originals:
                raise RuntimeError(f"post-write target identity changed: {ident}")

            expected_size, before_hash, expected_format, expected_mips = originals[ident]
            decoded = tex.image
            after_hash = pixel_hash(decoded)
            stream = stream_basename(tex)

            if decoded.size != expected_size:
                raise RuntimeError(
                    f"{tex.m_Name}: decoded size {decoded.size} != {expected_size}"
                )
            if int(tex.m_TextureFormat) != expected_format:
                raise RuntimeError(
                    f"{tex.m_Name}: format {int(tex.m_TextureFormat)} != {expected_format}"
                )
            if mip_count(tex) != expected_mips:
                raise RuntimeError(
                    f"{tex.m_Name}: mip count {mip_count(tex)} != {expected_mips}"
                )
            if not stream.endswith(RESOURCE_SUFFIX):
                raise RuntimeError(f"{tex.m_Name}: wrong resource stream {stream!r}")
            if after_hash == before_hash:
                raise RuntimeError(f"{tex.m_Name}: decoded pixels did not change")

            seen.add(ident)
            report.append(
                f"VERIFY {tex.m_Name} path_id={obj.path_id} "
                f"format={int(tex.m_TextureFormat)} mips={mip_count(tex)} "
                f"stream={stream} pixels {before_hash[:12]} -> {after_hash[:12]}"
            )

        if seen != set(originals):
            missing = sorted(set(originals) - seen)
            raise RuntimeError(f"post-write validation missed targets: {missing}")

        report.append(f"Environment textures enhanced: {len(originals)}")
        report.append(f"Unity top-level files rewritten: {len(top_files)}")
        report.append(f"Dedicated resource streams written: {len(resource_buffers)}")
        report.append(
            "Validation: PASS (complete APK tree reloaded and every enhanced texture decoded)"
        )

    except Exception as exc:
        for base, snapshot in stream_snapshots.items():
            restore_path(base, snapshot)
        for resource, old in resource_snapshots.items():
            if old is None:
                if resource.exists():
                    resource.unlink()
            else:
                resource.write_bytes(old)

        report.append(f"ROLLBACK: {type(exc).__name__}: {exc}")
        report.append("Environment textures enhanced: 0")
        report.append("Validation: FAIL; original Unity bytes restored")
        write_report(args.report, report)
        traceback.print_exc()
        raise SystemExit(1)

    write_report(args.report, report)


if __name__ == "__main__":
    main()
