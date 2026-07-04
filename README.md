# patches-tracker

Small CI tracker for Morphe patches.

The goal is:

- test configured apps on a schedule or manually
- build patched APKs without depending on `rvb`
- create a GitHub release when builds succeed
- open a pull request to `morphe-patches` when a newer app version works
- open or update an issue when a newer app version fails

## Config

Copy `config.example.toml` to `config.toml` and edit the apps you want to track.

```toml
[tracker]
patches_repo = "rushiranpise/morphe-patches"
constants_path = "patches/src/main/kotlin/app/template/patches/shared/Constants.kt"
release_prefix = "tracker"

[[apps]]
id = "splitwise"
name = "Splitwise"
package_name = "com.Splitwise.SplitwiseMobile"
constant = "SPLITWISE_COMPATIBILITY"
current_version = "26.5.5"
candidate_version = "latest"
included_patches = ["Unlock Pro"]

[[apps.sources]]
source = "apkmirror"
url = "https://www.apkmirror.com/apk/splitwise/splitwise"
arch = "all"
dpi = "nodpi anydpi auto"

[[apps.sources]]
source = "apkcombo"
url = "https://apkcombo.com/splitwise/com.Splitwise.SplitwiseMobile"
arch = "all"
dpi = "nodpi anydpi auto"
```

Supported app sources follow the `rvb` downloader model:

- `direct`
- `github`
- `archive`
- `apkmirror`
- `uptodown`
- `apkpure`
- `apkcombo`

Use `candidate_version = "latest"` to let the tracker resolve the newest available version from the configured source.

If an app has multiple `[[apps.sources]]`, they are tried in order. For example, APKMirror can be first and APKCombo can be the fallback. The older single-field format (`source` + `url`) still works for simple apps.

## GitHub Secrets

Required:

- `PATCHES_REPO_TOKEN`: token with access to push branches and open PRs in `morphe-patches`

## Local Smoke Test

```bash
python -m tracker.cli --config config.example.toml --dry-run
```

The build command is isolated in `tracker/build.py`. Right now it supports Morphe/ReVanced-style CLI patching and direct APK URLs. If you want APKMirror/Uptodown discovery later, add it as a resolver before the build step instead of mixing it into the core tracker.
