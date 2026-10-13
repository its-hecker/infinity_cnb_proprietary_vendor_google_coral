# Google wallpapers on cnb-Latest-stable

Custom Sidekick wallpapers and Google Pokemon Sidekick are removed from this
branch. The user confirmed Motion Sense and glow working, but applying Pokemon
crashed its original Unity 2019.3.0b4 renderer on Android 17.

The original Google input APK remains pinned and unchanged for reproducibility.
The Soong genrule builds the installed PixelLiveWallpaper APK by removing:

- The Pokemon wallpaper service, so saved component overrides cannot re-enable it.
- Its entry in the Come Alive service array, so the picker cannot offer a download tile.
- Its Unity application metadata, 87 Unity asset files and five Unity native libraries.

The existing live category metadata stays under application for discovery of the
remaining wallpapers. All other Google service definitions, DEX, assets, native
libraries and resource values are preserved. In particular, libgdx and the Breel
renderer stay in place. Soong supplies the platform signature and normal JNI/APK
alignment. There are no custom Sidekick services or assets.

GoogleParts no longer enables Pokemon at boot. Its Motion Sense, glow, Face Unlock
and eSIM integration are retained. Both the device and vendor branches use
cnb-Latest-stable, and the local manifest selects the kernel branch renamed from cnb.

The read-only pixel-wallpaper workflow verifies the pinned input, checks removal,
compares every retained non-manifest/non-resource payload entry byte for byte,
and parses the generated manifest and resources with AAPT2.

Run from the ROM source root before building:

```bash
python3 vendor/google/coral/tools/strip_google_pokemon_wallpaper.py \
  --input vendor/google/coral/proprietary/product/priv-app/PixelLiveWallpaper/PixelLiveWallpaper.apk \
  --output /tmp/PixelLiveWallpaper-without-pokemon.apk
python3 vendor/google/coral/tools/check_pixel_wallpaper.py \
  --without-pokemon /tmp/PixelLiveWallpaper-without-pokemon.apk
```

After flashing, Pokemon must be absent from Wallpaper & style. The generated APK
contains 33 other Google wallpaper services. A full ROM build and phone check are
still needed for the removal; existing Motion Sense/glow fixes are not reverted.
