# Google live wallpapers on features-experimental

The custom Infinity Sidekick wallpapers were removed on 12 October 2026 at the
user's request after reports of stutter, missing Motion Sense reactions and a
glow regression during the same build period. That timing does not establish the
cause of the glow failure. Glow recovery needs verification on the phone.

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
Google Pokemon service, removes that service's split-module and deletion metadata,
and moves the existing `live_categories` element under `application`. Other
wallpaper services, permissions and all payload entries retain their original
contents. Stale APK signatures are removed from the generated intermediate;
`android_app_import` retains platform signing, JNI processing and normal installation.
No APK, Unity or DEX rebuilding tools are required; the host Python tool uses only
the standard library and rejects any input outside the pinned original import.

The paired device change in GoogleParts clears a saved disabled component state
once at boot after it detects this bundled registration. It resets only the
Pokemon component to the manifest default, does not select a wallpaper, and
does not override later user changes. Without the new vendor registration it
waits instead of enabling the old downloadable module.

The device branch removes the standalone Sidekick module, animation packs,
renderer and Oslo listeners. This vendor branch removes its injection workflow
and tools. The `pixel-wallpaper.yml` workflow checks the source, generates the
manifest patch, compares every non-manifest payload entry byte for byte, and
parses the output with Android AAPT2. It has no write permission.

Run from the ROM source root before building:

```bash
python3 vendor/google/coral/tools/check_pixel_wallpaper.py
python3 vendor/google/coral/tools/enable_google_pokemon_wallpaper.py \
  --input vendor/google/coral/proprietary/product/priv-app/PixelLiveWallpaper/PixelLiveWallpaper.apk \
  --output /tmp/PixelLiveWallpaper-google-pokemon.apk
python3 vendor/google/coral/tools/check_pixel_wallpaper.py \
  --enabled /tmp/PixelLiveWallpaper-google-pokemon.apk
```

The check pins the imported APK's SHA-256 and checks that Google wallpapers
remain while custom assets, services, code and the added SystemUI permission
are absent. An intentional future Google APK update requires reviewing the
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

Build validation confirms registration and payload preservation. Rendering and
Motion Sense reactions still require testing on the Pixel 4 XL.

After flashing the updated ROM, apply a static wallpaper first, then test glow
preview and music glow. Face Unlock, OsloFeedback and GoogleParts changes were
not reverted by this removal.
