# patches-tracker

Automated CI pipeline that tracks upstream app releases, downloads stock APKs, runs Morphe patches, and updates `morphe-patches` compatibility constants & reports failures.

## How It Works

```
config.toml  →  resolve latest version  →  download APK  →  patch  →  PR / issue
```

1. Reads apps from `config.toml` (auto-generated from `Constants.kt` in `morphe-patches`).
2. For each app, asks configured sources for the latest version.
3. Skips anything not newer than the already-verified `current-version`.
4. Downloads the stock APK from the first source that has the selected version.
5. Merges split APKs (XAPK/APKS/APKM) into a single patchable APK when needed.
6. Runs Morphe CLI patcher with `--force`.
7. If patching succeeds: uploads artifact, creates a GitHub release, opens a PR to update `Constants.kt`.
8. If patching fails: opens an issue (tracker repo for source errors, `morphe-patches` for patch breakage).

All app checks run concurrently (`parallel_jobs = 4`) inside a single Actions job.

## Quick Start (Fork & Run)

### 1. Fork this repo

### 2. Set up your target patches repo

You need a `morphe-patches` (or equivalent) repo with:
- A `Constants.kt` file listing your supported apps with `*_COMPATIBILITY` constants
- A patcher CLI (JAR) that applies patches to APKs

### 3. Configure `config.toml`

The top section points to your repos:

```toml
[tracker]
patches_repo = "your-user/your-patches"
constants_path = "patches/src/main/kotlin/app/template/patches/shared/Constants.kt"
target_branch = "dev"

[cli]
repo = "MorpheApp/morphe-cli"
asset_regex = ".*\\.jar$"

[patches]
repo = "your-user/your-patches"
asset_regex = ".*\\.(mpp|rvp|jar)$"
```

### 4. Add apps

Each app is a flat TOML table. You can auto-generate entries from `Constants.kt` (see [Generating Config](#generating-config)) or hand-write them:

```toml
[my-app]
enabled = true
app-name = "My App"
package-name = "com.example.myapp"
constant = "MYAPP_COMPATIBILITY"
current-version = "1.2.3"
version = "latest"
arch = "all"
dpi = "nodpi anydpi auto"
apk-types = "apk"
apkmirror-dlurl = "https://www.apkmirror.com/apk/example/my-app"
uptodown-dlurl = "https://my-app.en.uptodown.com/android"
apkpure-dlurl = "https://apkpure.com/my-app/com.example.myapp"
apkcombo-dlurl = "https://apkcombo.com/search/com.example.myapp/"
gplay-dlurl = "https://play.google.com/store/apps/details?id=com.example.myapp"
```

### 5. Set secrets

| Secret | Required | Purpose |
|---|---|---|
| `PATCHES_REPO_TOKEN` | Yes | Push branches and open PRs in your patches repo |
| `GPLAY_DISPENSER_URL` | No | Google Play download dispenser (if using gplay source) |

### 6. Run

Go to **Actions → Track patches → Run workflow**. Or let the weekly cron handle it.

## Download Sources

Sources are tried in this order. The tracker stops at the first one that provides a downloadable APK for the selected version:

| Priority | Source | Version lookup | Notes |
|---|---|---|---|
| 1 | `direct` | From filename | Direct APK URL |
| 2 | `github` | From tagged release | Specific release tag |
| 3 | `github-release` | From latest release | Latest release only, manual config |
| 4 | `archive` | From directory listing | Static file server |
| 5 | `aoneroom` | From API | MovieBox-specific API |
| 6 | `apkmirror` | From uploads page | FlareSolverr may be needed |
| 7 | `uptodown` | From versions page | Full version history |
| 8 | `apkpure` | From downloading page | FlareSolverr may be needed |
| 9 | `apkcombo` | From download page | Tries apk/xapk/apks |
| 10 | `gplay` | None (fallback only) | Download-only, no version comparison |

### Source config keys

```toml
apkmirror-dlurl = "https://www.apkmirror.com/apk/developer/app-name"
uptodown-dlurl = "https://app-name.en.uptodown.com/android"
apkpure-dlurl = "https://apkpure.com/app-name/com.package.name"
apkcombo-dlurl = "https://apkcombo.com/search/com.package.name/"
gplay-dlurl = "https://play.google.com/store/apps/details?id=com.package.name"
github-dlurl = "https://github.com/owner/repo/releases/tag/v1.0.0"
github-release-dlurl = "https://github.com/owner/repo"
direct-dlurl = "https://example.com/app-v1.0.0.apk"
archive-dlurl = "https://example.com/archive/com.package.name"
aoneroom-dlurl = "https://h5-api.aoneroom.com/..."
```

`github-release-dlurl` is **manual-only** — the config generator does not auto-discover it. Set it by hand for repos where you want to track only the latest GitHub release.

## App Config Reference

```toml
[app-id]
enabled = true                          # false to skip this app
app-name = "App Name"                   # display name
package-name = "com.example.app"        # Android package name
constant = "APP_COMPATIBILITY"          # Constants.kt constant name
current-version = "1.2.3"               # last verified version
version = "latest"                      # "latest" or a specific version
arch = "all"                            # all, arm64-v8a, armeabi-v7a, etc.
dpi = "nodpi anydpi auto"               # DPI filter
apk-types = "apk xapk apks"            # accepted formats
included-patches = "'Patch Name'"       # patches to enable (off by default)
excluded-patches = "'Other Patch'"      # patches to skip
```

## CLI Usage

```bash
# Dry-run the full config (no downloads, no patching)
python -m tracker.cli --config config.toml --dry-run

# Run a single app
python -m tracker.cli --config config.toml --app my-app

# Run multiple specific apps
python -m tracker.cli --config config.toml --app my-app,other-app

# Shard across CI jobs
python -m tracker.cli --config config.toml --shard-index 0 --shard-total 4
```

## CI Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `track.yml` | Weekly cron, manual, after config sync | Full tracker run |
| `track-app.yml` | Manual (app ID input) | Run tracker for a single app |
| `sync-config-from-constants.yml` | Manual, weekly cron | Refresh config from Constants.kt |
| `generate-source-config.yml` | After sync | Discover APK source URLs |
| `reset.yml` | Manual | Reset all versions to trigger re-testing |

## Generating Config

Auto-generate `config.toml` from your patches repo's `Constants.kt`:

```bash
python scripts/generate-config-from-constants.py \
  --constants /path/to/morphe-patches/patches/src/main/kotlin/app/template/patches/shared/Constants.kt \
  --output config.toml \
  --patches-repo your-user/your-patches \
  --target-branch dev
```

The generator:
- Extracts app name, package, constant, and version from each `*_COMPATIBILITY` entry
- Discovers APK source URLs (APKMirror, Uptodown, APKPure) via search
- Falls back to APKCombo (package search URL) and Google Play for all apps
- Preserves manually-set keys like `enabled`, `included-patches`, `excluded-patches`, and `github-release-dlurl`

To skip source discovery (faster):

```bash
python scripts/generate-config-from-constants.py \
  --constants Constants.kt \
  --output config.toml \
  --no-resolve-source-urls
```

## Failure Routing

| Failure type | Issue created in |
|---|---|
| Download / version / config error | This tracker repo |
| Patch / fingerprint / signing error | `morphe-patches` repo |

This keeps source-site breakage (APKMirror down, Cloudflare block) separate from real patch breakage (app updated and patch no longer applies).

## Environment Variables

```bash
RESOLVER_TIMEOUT_SECONDS=300     # per-source version lookup timeout
APK_MERGE_TIMEOUT_SECONDS=240    # split APK merge timeout
FETCH_RETRIES=3                  # FlareSolverr/plain request retries
APKCOMBO_RETRIES=3               # APKCombo download retries
FLARESOLVERR_URL=http://localhost:8191
GPLAY_DISPENSER_URL=             # Google Play dispenser endpoint
```

## Local Setup

```bash
git clone https://github.com/your-user/patches-tracker.git
cd patches-tracker
pip install -r requirements.txt

# System dependencies (Linux)
sudo apt-get install -y jq wget curl unzip zip aapt apksigner
# htmlq: https://github.com/mgdm/htmlq/releases

# Dry-run
python -m tracker.cli --config config.toml --dry-run
```

## Stale App Protection

The tracker fetches `Constants.kt` from your patches repo before each run and skips any configured app whose `*_COMPATIBILITY` constant no longer exists. This prevents false "patch broken" issues for apps that were removed from the patches repo.

The weekly `sync-config-from-constants.yml` cron regenerates `config.toml` from Constants.kt, automatically dropping removed apps from the config itself.

## Credits

- Morphe patches and compatibility constants: `rushiranpise/morphe-patches`
- Morphe/ReVanced-style patching tools and patch format: Morphe and ReVanced projects
- Downloader behavior and config conventions: `rvb` by j-hc and contributors
- APKMirror/APKCombo/APKPure/Uptodown hardening: `FiorenMas/Revanced-And-Revanced-Extended-Non-Root`
- Google Play helper lineage: Aurora Store / AuroraOSS, GPLv3
- APK split merge support: REAndroid APKEditor
- CI runtime: GitHub Actions, FlareSolverr, htmlq, jq, Android build tools
