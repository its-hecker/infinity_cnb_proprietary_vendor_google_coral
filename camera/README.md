# Original stock Google Camera 8.7 for coral

This directory is used only by the `gcam-experimental` device product.
`GoogleCameraCoral` installs Google's original Camera
**8.7.250.494820638.44** (`65822832`) as a signed split package in `/product/app`.
The base plus nine common feature splits are retained. Six `p21`, `p22` and
`cheetah` Tensor-specific modules are excluded.

`source.json` pins the original APKMirror bundle, its SHA-256, every APK's exact
bytes and Google's signing-certificate SHA-256. No app code, resources, manifest
or APK signature is modified. The installer preserves the native startup
library; it does not ship the incomplete base APK alone.

`prebuilt/GoogleCameraCoral.apks.*` are sequential 64 MiB chunks of one APK set,
so no individual Git object exceeds GitHub's 100 MiB limit. Soong joins them in
name order and `android_app_set` installs the base and splits byte-for-byte.
There is no network access during the Android build and no Git LFS requirement
for Camera. `prebuilt/payload.json` records the archive and chunk hashes.

The table of contents uses AOSP extract_apks' single install group. All retained
feature APKs are selected as part of that group; their real manifest split IDs
remain unchanged. The original Google APK license remains proprietary.

## Verification

With Android Build Tools 35.0.0 in `PATH`:

```bash
python3 camera/tools/prepare_camera.py --check
```

This reconstructs the APK set, validates the complete split selection and hashes,
and runs `apksigner verify` and `aapt2 dump badging` on every installed APK.
`validation.txt` is the matching GitHub Actions report. CI verification does not
replace a full ROM build and physical coral camera/Face ID testing on Android 17.

To reproduce the import from the pinned original `.apkm`:

```bash
python3 camera/tools/prepare_camera.py --input /path/to/original-camera.apkm
python3 camera/tools/prepare_camera.py --check
```

`--download` fetches the pinned release from APKMirror's public CDN and rejects
any source whose hash differs. This is only needed when rebuilding the committed
payload. If hosting blocks an automated download, supply the original bundle
with `--input`. The payload is already committed; the verification workflow is
read-only and does not download Camera from an external site.

Build instructions live in the matching local-manifest branch at
`docs/GCAM_EXPERIMENTAL.md`.
