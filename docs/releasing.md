# Releasing BMeet

Each platform is released on its own. A Linux fix doesn't need a Mac release, and a Mac
release doesn't touch the Linux one. Every platform has its own:

| | Linux | macOS |
|---|---|---|
| version | `VERSION` | `macos/VERSION` |
| changelog | `CHANGELOG.md` | `macos/CHANGELOG.md` |
| tags | `v0.8.1`, `v0.8.2`, … | `macos-v0.9.0`, … |
| GitHub release | *BMeet 0.8.2*, marked **Latest** | *BMeet for macOS 0.9.0* |
| workflow | `.github/workflows/release-linux.yml` | `.github/workflows/release-macos.yml` |
| the one-line install fetches | `v$VERSION` (`install.sh`) | `macos-v$MACOS_VERSION` (`install.sh`) |

**Shared code:** both platforms run the same `npu-transcribe`, `bmeet-ctl`, BMeet window and
Chrome extension.
- In the repository these carry the Linux version (`__version__`, the extension's
  manifest).
- On a Mac they report `macos/VERSION` instead (`macos/bmeetmac.py`).
- A change to shared code goes into the changelog of each platform it changes. CI runs
  the Linux and the macOS jobs for every change and every release.

## Making a release

From a checkout of `main`, on Linux or on a Mac:

1. **Write the notes.** While you work, add what changed to the platform's changelog
   under `## [Unreleased]`, using *Added* / *Changed* / *Fixed* / *Tests* as before.
2. **Set the version:**

   ```bash
   release/release bump macos 0.9.1
   ```

   This writes the version wherever that platform carries it and turns `## [Unreleased]`
   into `## [0.9.1] - <today>`. Commit it, with the change or on its own, and push to
   `main`. CI must pass.
3. **Release it:**

   ```bash
   release/release macos --title "the headline"
   ```

   - **It checks first:**
     - you are on `main` and match GitHub;
     - nothing is uncommitted;
     - the versions agree (`release/check-versions`);
     - the changelog has a dated section;
     - the tag is new;
     - CI passed for this commit.
   - **Then:** it shows what it will do, asks, tags `macos-v0.9.1` (the tag's message is
     the release's title) and pushes the tag.
4. **GitHub does the rest.** `release-macos.yml` runs all of CI on the tag, the Mac job on
   Apple silicon included. Only if it passes does it publish the release, with the
   changelog section as its notes (`release/publish`). Watch it under *Actions*.

After that, the one line in the README installs the new version. It reads the version from
`main`'s `install.sh`, which `bump` updates too; that is why the bump goes to `main` before the
tag. Between that push and the tag, the one line can't find the release yet for that platform,
so release as soon as CI has passed.

`release/release status` shows each platform's version, its last release and the commits
since.

## How users get it

BMeet looks for a newer release of its own platform once a day: `bmeet-ctl update --check --auto`, which the BMeet window, the panel widget and BMeet.app run. It reads the GitHub releases whose tags have the platform's prefix (`v`, `macos-v`). Drafts and pre-releases are skipped, and a release is published only after CI passed on its tag. The apps then show **Update…**, which runs `bmeet-ctl update` in a terminal window. That command downloads the new release's own `install.sh` and runs it with the options BMeet was installed with (`~/.local/share/bmeet/install-options`, written by the installers). So a release reaches its users within a day of being published.

Releases made before this (Linux 0.8.1, macOS 0.9.0) have no update check: their users update once with the install line.

## Adding a platform (Windows)

1. Give it a version file and a changelog (`windows/VERSION`, `windows/CHANGELOG.md`).
2. Add it to `release/platforms.sh`: its name, files, tag prefix (`windows-v`) and the
   lines that carry its version.
3. Copy `release-macos.yml` to `release-windows.yml`, with the `windows-v*` tags. If its
   jobs need a Windows runner, add a `windows` job to `ci.yml`.
4. Teach `install.sh` (or a Windows installer) which tag to fetch.
5. Add its column to the table above and its badge to the README.
