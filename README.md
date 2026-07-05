# patches-tracker

`patches-tracker` is a GitHub Actions based validation pipeline for Morphe patch compatibility. It resolves upstream APK versions, downloads stock APKs, runs the Morphe/ReVanced-style patcher, publishes successful patched APK artifacts, and reports failures to the correct repository.

The tracker is designed for two jobs:

- verify whether newer upstream app versions still patch successfully
- update `morphe-patches` compatibility constants only after a real patched build succeeds

## Pipeline

The main workflow is `.github/workflows/track.yml`.

1. Load apps from `config.toml`.
2. Run app checks concurrently using `parallel_jobs`.
3. Resolve the latest app version from the configured download source.
4. Download the stock APK, APKM, XAPK, or APKS.
5. Merge split packages into a patchable APK when needed.
6. Run the patcher with configured include/exclude arguments.
7. Upload logs and patched APK artifacts.
8. Create a release for successful artifacts.
9. Open or update failure issues.
10. Open a PR against `morphe-patches` when verified versions change.

The workflow runs as one GitHub Actions job. App checks run inside that job with `parallel_jobs = 4`, which keeps one status branch, one release, and one PR while reducing total wall-clock time.

## Configuration

Apps are configured in rvb-style flat TOML tables:

```toml
[splitwise]
enabled = true
app-name = "Splitwise"
package-name = "com.Splitwise.SplitwiseMobile"
constant = "SPLITWISE_COMPATIBILITY"
current-version = "26.5.5"
version = "latest"
arch = "all"
dpi = "nodpi anydpi auto"
apk-types = "apk xapk apks"
apkmirror-dlurl = "https://www.apkmirror.com/apk/splitwise/splitwise"
apkcombo-dlurl = "https://apkcombo.com/search/com.Splitwise.SplitwiseMobile/"
uptodown-dlurl = "https://splitwise.en.uptodown.com/android"
```

Default patches do not need to be listed. Use `included-patches` only for patches that are disabled by default, and `excluded-patches` for patches that should not be applied.

The legacy `[[apps]]` / `[[apps.sources]]` format is still accepted, but new entries should use flat tables.

## Source Priority

When more than one source URL is configured for an app, sources are tried in this order:

```text
direct -> github -> archive -> apkmirror -> uptodown -> apkpure -> apkcombo
```

The same source order is used for latest-version resolution. If latest resolution succeeds on one source but downloading from that source fails, the tracker falls through to the remaining configured sources.

Supported package formats:

- `apk`
- `apkm`
- `xapk`
- `apks`

APKCombo follows rvb behavior and tries `apk`, `xapk`, and `apks`. `apkm` is still supported for sources where it is a real file type, such as APKMirror, direct URLs, archives, and GitHub releases.

## Generated APKCombo Config

`.github/workflows/track-apkcombo.yml` updates `config.toml` from `Constants.kt`.

The generator extracts:

- app name
- package name
- compatibility constant
- current target version

It then writes an APKCombo default entry for each app. Manual per-app keys already present in `config.toml` are preserved, so custom fallback URLs and patch options survive regeneration:

```toml
apkmirror-dlurl = "..."
uptodown-dlurl = "..."
apkpure-dlurl = "..."
included-patches = "'Some Patch'"
excluded-patches = "'Other Patch'"
```

Generated fields such as `app-name`, `package-name`, `constant`, `current-version`, `version`, `arch`, `dpi`, `apk-types`, and `apkcombo-dlurl` are refreshed from the generator.

## Failure Routing

Download, version resolution, and config failures are tracker/source failures. These issues are created in the tracker repository.

Patch, fingerprint, signing, and patcher failures are patch compatibility failures. These issues are created in `morphe-patches`.

This keeps source breakage separate from actual patch breakage.

## Runtime Controls

The resolver is intentionally fail-fast by default so one blocked source does not consume the whole CI budget.

Environment variables:

```text
RESOLVER_RETRIES=1
RESOLVER_TIMEOUT_SECONDS=120
FETCH_RETRIES=1
APKCOMBO_RETRIES=1
PATCHER_TIMEOUT_SECONDS=900
```

The tracker streams resolver and patcher stdout/stderr live in Actions logs, including command lines, return codes, FlareSolverr fetches, HTTP requests, and timeout kills.

## Local Commands

Dry-run the whole config without downloading or patching:

```bash
python -m tracker.cli --config config.toml --dry-run
```

Dry-run one shard manually:

```bash
python -m tracker.cli --config config.toml --dry-run --shard-index 0 --shard-total 2
```

Regenerate APKCombo defaults from constants:

```bash
python scripts/generate-config-from-constants.py \
  --constants /path/to/morphe-patches/patches/src/main/kotlin/app/template/patches/shared/Constants.kt \
  --output config.toml \
  --patches-repo rushiranpise/morphe-patches \
  --target-branch dev
```

## Required Secret

`PATCHES_REPO_TOKEN` must have permission to push branches and open pull requests in `morphe-patches`.

## Credits

- Morphe patches and compatibility constants: `rushiranpise/morphe-patches`
- Morphe/ReVanced-style patching tools and patch format: Morphe and ReVanced projects
- Downloader behavior and rvb-style config conventions: `rvb` by j-hc and contributors
- APK split merge support: REAndroid APKEditor
- CI runtime: GitHub Actions, FlareSolverr, htmlq, jq, Android build tools
