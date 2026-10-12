# Google live wallpapers on features-experimental

The custom Infinity Sidekick wallpapers were removed on 12 October 2026 at the
user's request after reports of stutter, missing Motion Sense reactions and a
glow regression during the same build period. That timing does not establish the
cause of the glow failure. Glow recovery needs verification on the phone.

`PixelLiveWallpaper.apk` is restored byte for byte from the original Google APK
import at commit `460f71da894d2a8435a20a761a82c351e1d29587`, before custom
Sidekick injection or Pokemon environment changes. Google's Pokemon Wave Hello,
Come Alive and Living Universe wallpapers remain in that package. The existing
platform signing and Motion Sense bridge configuration remain in use.

The device branch removes the standalone Sidekick module, animation packs,
renderer and Oslo listeners. This vendor branch removes its injection workflow
and tools. No workflow rebuilds or publishes Sidekick into the APK. The
`pixel-wallpaper.yml` workflow only verifies the original file and its contents;
it has no write permission.

Run from the ROM source root before building:

```bash
python3 vendor/google/coral/tools/check_pixel_wallpaper.py
```

The check pins the imported APK's SHA-256 and checks that Google wallpapers
remain while custom assets, services, code and the added SystemUI permission
are absent. An intentional future Google APK update requires reviewing the
replacement and updating the baseline fingerprint in the checker.

After flashing the updated ROM, apply a static wallpaper first, then test glow
preview and music glow. Face Unlock, OsloFeedback and GoogleParts changes were
not reverted by this removal.
