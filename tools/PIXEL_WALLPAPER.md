# Google live wallpapers on features-experimental

The custom Infinity Sidekick wallpapers were removed on 12 October 2026 at the
user's request after reports of stutter, missing Motion Sense reactions and a
glow regression during the same build period. That timing does not establish the
cause of the glow failure. The user confirmed Motion Sense and glow working after
the dedicated SystemUI overlay fix.

The source `PixelLiveWallpaper.apk` remains byte for byte from the original
Google import at commit `460f71da894d2a8435a20a761a82c351e1d29587`. Google's
Pokemon Sidekick renderer, Unity assets, backgrounds, native libraries and
resource table remain unchanged. Custom Sidekick content remains absent.

That original manifest disables Pokemon and marks it as a downloadable `pokemon`
split. Its `RestoreStateJobIntentService` disables module-tagged services when
Play Core does not report their split as installed. This monolithic APK already
contains the Pokemon renderer and Unity payload, so waiting for a downloadable
split hides a wallpaper that is already bundled. Infinity's WallpaperPicker2
queries enabled wallpaper services with `GET_META_DATA`, excluding disabled ones.

The `coral_google_pokemon_wallpaper_apk` Soong genrule now builds the imported
wallpaper from a manifest-only compatibility patch. It enables the existing
Google Pokemon service, preserves its `com.google.pixel.livewallpaper.module=pokemon`
identifier, removes only its deletion metadata, and moves the existing
`live_categories` element under `application`. The application declares
`com.android.dynamic.apk.fused.modules=base,pokemon`. The bundled Play Core
implementation (`bmd.d()` in the original APK) adds this comma-separated list to
its installed module set, so the restore job retains the bundled Pokemon service.
Google's wallpaper-change logging (`bvs.a(Context)` and `bqf.a(String)`) still reads
the original `pokemon` identifier and resolves its integer wallpaper ID.

The generated manifest also requests `android.permission.STATUS_BAR_SERVICE` for
the original Google Pokemon client's calls to `com.google.oslo.service.OsloService`.
The existing platform signing supplies the signature permission. It is needed by
Google's original client even though the custom Sidekick implementation is removed.
Other wallpaper services and all payload entries retain their original contents.
Stale APK signatures are removed from the generated intermediate;
`android_app_import` retains platform signing, JNI processing and normal installation.
No APK, Unity or DEX rebuilding tools are required; the host Python tool uses only
the standard library and rejects any input outside the pinned original import.

The paired device change in GoogleParts clears a saved disabled component state
once at boot after it detects the preserved module ID and fused registration.
The migration marker is version 2 so devices that ran the earlier metadata-removal
build can also migrate. It resets only the
Pokemon component to the manifest default, does not select a wallpaper, and
does not override later user changes. Without the new vendor registration it
waits instead of enabling the old downloadable module.

The device branch removes the standalone Sidekick module, animation packs,
renderer and Oslo listeners. This vendor branch removes its injection workflow
and tools. The `pixel-wallpaper.yml` workflow checks the source, generates the
manifest patch, compares every non-manifest payload entry byte for byte, and
parses the output with Android AAPT2. Regression checks reject missing Pokemon
identifiers, missing fused registration and missing Oslo permission. It has no
write permission.

Run from the ROM source root before building:

```bash
python3 vendor/google/coral/tools/check_pixel_wallpaper.py
python3 vendor/google/coral/tools/enable_google_pokemon_wallpaper.py \
  --input vendor/google/coral/proprietary/product/priv-app/PixelLiveWallpaper/PixelLiveWallpaper.apk \
  --output /tmp/PixelLiveWallpaper-google-pokemon.apk
python3 vendor/google/coral/tools/check_pixel_wallpaper.py \
  --enabled /tmp/PixelLiveWallpaper-google-pokemon.apk
```

The baseline check pins the imported APK's SHA-256 and checks that Google wallpapers
remain while custom assets, services and code are absent. The generated APK
validator limits manifest changes and compares every other ZIP entry byte for byte.
An intentional future Google APK update requires reviewing the
replacement, its split behavior and the baseline fingerprint in the checker
and manifest tool. The normal ROM build applies the patch automatically.

Sync both `device/google/coral` and `vendor/google/coral` on
`features-experimental` and rebuild the ROM. After flashing and completing boot,
look for **Pokemon Sidekick** in Wallpaper & style's live wallpaper list. On the
phone, verify the resolved service before testing its animations and Motion Sense:

```bash
adb shell cmd package query-services --brief --components -a android.service.wallpaper.WallpaperService -p com.google.pixel.livewallpaper
adb shell dumpsys package com.google.pixel.livewallpaper
```

## Runtime failure identified on 13 October 2026

The user's Pixel 4 XL bugreport contains ten crashes in
`com.google.pixel.livewallpaper:pokemonwallpaper`. Eight are native Unity crashes;
the other two are a null wallpaper ID after `WALLPAPER_CHANGED` and a missing
`STATUS_BAR_SERVICE` permission while registering an Oslo listener. The manifest
changes above correct the two Java integration faults. They do not fix the
remaining native allocator failure.

The original APK contains Unity `2019.3.0b4 (3271e9fc5036)` and arm64 libraries only.
Its `libunity.so` build ID is `ef363364442ab137e2ef98ccf412cc9c6297db59`, matching
the supplied crash stacks. In the fresh reproduction, the engine logs
`Using memoryadresses from more than 16GB of memory`, then crashes with `SIGSEGV`
at `libunity.so+0x6d7980` while registering an allocation in its limited address
tracking table. Unity documented this failure with Android 11's Scudo allocator
and changed the allocator in later engine releases:

- [Unity 2019.4.15f1 release notes](https://unity.com/releases/editor/whats-new/2019.4.15f1)
- [Unity's official Android 11 allocator explanation](https://discussions.unity.com/t/android-11-arm64-native-heap-allocator-issues/826358)

A compatible Google renderer build or a reviewed native allocator backport is
still required. A 32-bit manifest flag alone cannot help this arm64-only APK.
Do not treat passing registration/payload CI as proof that applying or rendering
Pokemon is fixed. Further full ROM builds for this wallpaper should wait for the
native compatibility change and its validation.

After flashing the updated ROM, apply a static wallpaper first, then test glow
preview and music glow. Face Unlock, OsloFeedback and GoogleParts changes were
not reverted by this removal.
