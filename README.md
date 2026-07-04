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
Apps use the same flat table style as `rvb`: add one `[app-id]` block and one or more `*-dlurl` entries.

```toml
[tracker]
patches_repo = "rushiranpise/morphe-patches"
constants_path = "patches/src/main/kotlin/app/template/patches/shared/Constants.kt"
release_prefix = "tracker"

[splitwise]
enabled = true
app-name = "Splitwise"
package-name = "com.Splitwise.SplitwiseMobile"
constant = "SPLITWISE_COMPATIBILITY"
current-version = "26.5.5"
version = "latest"
included-patches = "'Unlock Pro'"
arch = "all"
dpi = "nodpi anydpi auto"
apkmirror-dlurl = "https://www.apkmirror.com/apk/splitwise/splitwise"
apkcombo-dlurl = "https://apkcombo.com/search/com.Splitwise.SplitwiseMobile/"
uptodown-dlurl = "https://splitwise.en.uptodown.com/android"
```

`included-patches` is only needed for patches that are disabled by default. Normal/default patches are applied automatically by the CLI.

Supported app sources follow the `rvb` downloader model:

- `direct`
- `github`
- `archive`
- `apkmirror`
- `uptodown`
- `apkpure`
- `apkcombo`

Use `version = "latest"` to let the tracker resolve the newest available version from the configured source.

If an app has multiple `*-dlurl` entries, they are tried in this order: `direct`, `github`, `archive`, `apkmirror`, `uptodown`, `apkpure`, `apkcombo`.
The older `[[apps]]` / `[[apps.sources]]` format still works, but new apps should use the flat rvb-style format.

## Generated APKCombo Config

The `Track default patches via APKCombo` workflow can generate a config automatically from `Constants.kt`.
It extracts each compatibility constant's app name, package name, current target version, and constant name, then creates entries like:

```toml
[splitwise]
app-name = "Splitwise"
package-name = "com.Splitwise.SplitwiseMobile"
constant = "SPLITWISE_COMPATIBILITY"
current-version = "26.5.5"
version = "latest"
apkcombo-dlurl = "https://apkcombo.com/search/com.Splitwise.SplitwiseMobile/"
```

This is useful for broad default-patch checks. Keep `config.toml` for apps that need custom fallbacks such as APKMirror, Uptodown, direct URLs, or non-default patch includes.

## GitHub Secrets

Required:

- `PATCHES_REPO_TOKEN`: token with access to push branches and open PRs in `morphe-patches`

## Local Smoke Test

```bash
python -m tracker.cli --config config.example.toml --dry-run
```

The build command is isolated in `tracker/build.py`. Right now it supports Morphe/ReVanced-style CLI patching and direct APK URLs. If you want APKMirror/Uptodown discovery later, add it as a resolver before the build step instead of mixing it into the core tracker.
