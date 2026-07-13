# patches-tracker

`patches-tracker` is a GitHub Actions workflow for checking Morphe patch compatibility. It finds newer upstream APKs, downloads the stock app, runs the Morphe/ReVanced-style patcher, publishes successful builds, and reports failures in the right place.

It has two main jobs:

- verify whether newer upstream app versions still patch successfully
- update `morphe-patches` compatibility constants only after a real patched build succeeds

## Pipeline

The main workflow is `.github/workflows/track.yml`.

1. Read apps from `config.toml`.
2. Run app checks concurrently using `parallel_jobs`.
3. Find the latest app version from the configured sources.
4. Download the stock APK, APKM, XAPK, or APKS.
5. Merge split packages into a patchable APK when needed.
6. Run the patcher with Morphe CLI `--force` so newer versions are tested instead of skipped by compatibility metadata.
7. Upload logs and patched APK artifacts.
8. Create a release for successful artifacts.
9. Open or update failure issues.
10. Open a PR against `morphe-patches` when verified versions change.

The workflow runs as one GitHub Actions job. App checks run inside that job with `parallel_jobs = 4`, which keeps one status branch, one release, and one PR while reducing total wall-clock time.

## Configuration

Apps use rvb-style flat TOML tables:

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
apkpure-dlurl = "https://apkpure.com/apk-info/com.Splitwise.SplitwiseMobile"
```

Default patches do not need to be listed. Use `included-patches` only for patches that are off by default, and `excluded-patches` for patches you want to skip.

The legacy `[[apps]]` / `[[apps.sources]]` format is still accepted, but new entries should use flat tables.

## Source Priority

When an app has more than one source, the tracker tries them in this order:

```text
direct -> github -> archive -> apkmirror -> uptodown -> apkpure -> apkcombo
```

The same order is used when checking the latest version. The tracker stops at the first source that reports a newer version and produces a downloadable APK. If version lookup or download fails, it moves on to the next configured source. Once an APK downloads, patching is attempted from that APK and lower-priority sources are skipped.

Successful builds only update `Constants.kt` when the tested version is newer than `current-version`. Fallback sources that report the current or an older version are skipped, so compatibility constants are never downgraded.

Supported package formats:

- `apk`
- `apkm`
- `xapk`
- `apks`

APKCombo follows rvb behavior and tries `apk`, `xapk`, and `apks`. `apkm` is still supported for sources where it is a real file type, such as APKMirror, direct URLs, archives, and GitHub releases.

## Generated Source Config

`.github/workflows/sync-config-from-constants.yml` updates `config.toml` from `Constants.kt` without resolving APK source links. Use it when Morphe constants changed and the tracker config only needs the current app versions refreshed.

`.github/workflows/generate-source-config.yml` does the heavier source discovery pass for APKMirror, Uptodown, and APKPure links. It also runs automatically after a successful constants sync.

The generator extracts:

- app name
- package name
- compatibility constant
- current target version
- preferred APK file type from `apkFileType`, when present

It uses the package name to discover source links, then writes the first final app page URL found in source-priority order: APKMirror, then Uptodown, then APKPure. APKCombo keeps the package search URL because that is the downloader entrypoint. Existing final higher-priority source URLs are kept, so repeat runs do not keep checking lower-priority mirrors once a better source is available. If constants do not declare `apkFileType`, source discovery can infer a narrower `apk-types` value from the resolved source page. Manual patch options also survive regeneration:

```toml
included-patches = "'Some Patch'"
excluded-patches = "'Other Patch'"
```

Generated fields such as `app-name`, `package-name`, `constant`, `current-version`, `version`, `arch`, `dpi`, `apk-types`, `apkmirror-dlurl`, `uptodown-dlurl`, `apkpure-dlurl`, and `apkcombo-dlurl` are refreshed from the generator.

Generated source discovery starts from these package-name URLs:

```toml
apkmirror-dlurl = "https://www.apkmirror.com/?post_type=app_release&searchtype=app&sortby=date&sort=desc&s=com.whatsapp"
uptodown-dlurl = "https://en.uptodown.com/android/search?query=com.whatsapp"
apkpure-dlurl = "https://apkpure.com/apk-info/com.whatsapp"
apkcombo-dlurl = "https://apkcombo.com/search/com.whatsapp/"
```

For APKMirror and Uptodown, discovery checks the search results and keeps the app whose package name matches. APKPure uses the `apk-info/<package>` redirect when the app exists there.

The generated `config.toml` stores the resolved final URLs, for example:

```toml
apkmirror-dlurl = "https://www.apkmirror.com/apk/whatsapp-inc/whatsapp-messenger/"
uptodown-dlurl = "https://whatsapp-messenger.en.uptodown.com/android"
apkpure-dlurl = "https://apkpure.com/whatsapp-android/com.whatsapp"
apkcombo-dlurl = "https://apkcombo.com/search/com.whatsapp/"
```

Source discovery runs gently because APKMirror and APKPure may need FlareSolverr. The workflow currently uses `--max-source-checks 0`, which means no source-check cap. Raise `--source-workers` carefully if the source sites become stable enough for more concurrency.

## Failure Routing

Download, version lookup, and config failures are tracker/source problems. These issues are created in the tracker repository.

Patch, fingerprint, signing, and patcher failures are patch compatibility problems. These issues are created in `morphe-patches`.

That keeps source-site breakage separate from real patch breakage.

## Runtime Controls

The resolver is intentionally fail-fast by default so one blocked source does not eat the whole CI budget.

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

Regenerate source defaults from constants:

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
