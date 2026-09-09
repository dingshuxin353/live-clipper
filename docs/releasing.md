# Local macOS releases

`scripts/release.py` separates building a candidate from publishing it. It supports macOS arm64 and uses the repository's existing backend build, electron-builder configuration and updater provider. It does not choose a version, edit the changelog, merge a branch or install an update on your machine.

The tool still needs independent local acceptance. Its first complete build and Apple/GitHub publish chain must be verified during a future authorized release; unit tests do not establish that those external steps work.

## Before building

Freeze the seven root version fields and the matching changelog entry, then merge and push that source to `master`. Run from a clean checkout at that exact commit. The previous stable tag must be an ancestor, and the new tag and Release must not exist.

Install Python 3.11, Node 24, npm 11, Xcode command-line tools and `gh`. Sign in with `gh auth login`. The login keychain must contain one valid Developer ID Application identity. Store notarization credentials using Apple's `notarytool store-credentials`, and set `VENUS_NOTARY_PROFILE` to that approved profile's name. Do not put passwords or tokens in command arguments, manifests or this repository.

Create a private release parent directory outside the repository and user application-data directories. The preflight reserves 15 GiB for the build plus 4 GiB for the shared download cache; on one volume, allow at least 19 GiB free. It prints residual directories, ownership and measured usage before downloading. Separate volumes are checked individually. The output directory itself must not already exist.

```sh
python3.11 scripts/release.py candidate \
  --source <exact-master-commit> \
  --previous-tag <previous-stable-tag> \
  --output <absolute-new-release-directory>
```

The candidate command checks local and remote source identity, creates an isolated source checkout, installs dependencies, runs Python checks, Desktop tests and a full Desktop audit, including development dependencies, which must report zero vulnerabilities, then calls `build-backend.sh`. That script runs the Frontend check once. Packaging explicitly disables notarization and automatic publishing.

Machine checks cover signed arm64 binaries, Hardened Runtime, the bundled backend, FFmpeg and ffprobe, macOS 14 declarations, App/ZIP/DMG consistency, startup readiness, project reads and synthetic recording discovery. A loopback updater feed tests the previous release's update selection and ZIP download. It uses Node HTTP transport and does not execute Squirrel or install an update.

The corresponding-source notice inside the App must name the same versioned media archive and SHA-256. The archive stays outside the App and updater metadata; it is copied unchanged through finalization.

The resulting `candidate-manifest.json` binds the source, tools, candidate bytes and machine evidence. Repeating the same candidate command verifies the frozen files; it does not rebuild. An unfinished directory is preserved and rejected, not silently replaced.

## Human acceptance

“Candidate ready” does not mean “approved for release”. Complete the generated `acceptance.json` with a reviewer, a timezone-qualified review timestamp and all three checks:

- New-user first frame.
- Correct startup after upgrading an isolated copy of previous-version data.
- This release's risk-related interactions.

For each check, set `status` to `passed` and supply evidence entries containing `path` and `sha256`. Paths are relative to the acceptance file or absolute. A `waived` check also requires `user_decision` quoting this release's explicit waiver and evidence of that decision. Pending, failed, missing or changed evidence blocks publishing.

Launch only the reported candidate executable, `candidate/mac-arm64/Venus.app/Contents/MacOS/Venus`, with `LIVE_CLIPPER_HOME` set to a new directory under the private release root. Its sibling Electron runtime directory must remain there too. Verify the running executable's absolute path and PID, then verify a window actually appeared. Do not use `open -a Venus`: that can start an installed copy instead of the candidate. Never reuse real recordings, model caches or the default application home for acceptance.

## Publish the accepted candidate

Obtain explicit approval for the exact version after reviewing the source, asset hashes and acceptance exceptions. The flag below records that operator confirmation; supplying a flag is not a substitute for permission.

```sh
python3.11 scripts/release.py publish \
  --candidate <absolute-release-directory>/candidate-manifest.json \
  --acceptance <absolute-release-directory>/acceptance.json \
  --confirm-version <approved-vX.Y.Z>
```

Publishing rechecks the candidate and environment, creates an annotated tag, submits the frozen ZIP and DMG to Apple and saves each Submission ID. It waits on existing submissions rather than resubmitting them. After acceptance, it staples owned copies, repacks only the stapled App, regenerates update metadata and verifies signatures and Gatekeeper.

The tool pushes the exact tag and creates a draft Release. All five assets—DMG, ZIP, ZIP blockmap, `latest-mac.yml` and `Venus-<version>-media-sources.tar.gz`—must match before the Release becomes public. Anonymous downloads and the previous release's GitHub updater provider then verify the published ZIP. Native update installation is outside this probe.

## When a step fails

Keep the release directory and `progress.json`. Re-run with the same arguments only after understanding the failed step. A saved Apple Submission ID is reused; an uncertain submission without an ID stops for reconciliation. A matching existing remote asset is verified, never uploaded with `--clobber`. Unowned tags or Releases are not adopted.

An interrupted local build or finalize directory is deliberately not repaired automatically. Do not edit a frozen candidate to get past a failed check. If publication succeeded but a later download or cleanup failed, the Release may already be visible: report that state rather than claiming a rollback.

## Cache and cleanup

Each release parent contains one owned `download-cache` for npm, pip, Electron and media inputs. Environments and `node_modules` remain private to each build. Media reuse checks the source inputs, toolchain, platform, product version and every recorded output hash. A different product version goes through the normal media build entry because its source notice changes.

The 4 GiB cache cap allows headroom over the roughly 390 MiB measured for cold and warm Desktop/pinned MLX downloads. This is a download measurement, not a complete build benchmark. npm and pip use their native purge commands when the cap is exceeded. Only owned caches are eligible; occupied paths and invalid media entries stop cleanup. Media entries retain the newest two verified local releases and unresolved active versions.

After public verification, the tool retains one set of five assets for each of the latest two owned releases. It removes registered build and probe directories after checking their device/inode, symlinks, mounts and process occupancy. Older assets are deleted only after their public recovery source and hashes are verified. Unknown directories are listed for manual review.

Inspect or retry cleanup independently:

```sh
python3.11 scripts/release.py cleanup --root <absolute-release-directory>
python3.11 scripts/release.py cleanup --root <absolute-release-directory> --apply
```

The preview makes no changes. `--apply` also recovers recorded services and mounts after checking their identities; a reused PID or changed mount is left alone. It never rebuilds, submits to Apple or publishes. `cleanup-report.json` records residual paths, reasons, logical file sizes and observed free space. APFS snapshots and clones can make the free-space change differ from removed file sizes.

Unresolved failed releases keep their diagnostic files. Once an incident is resolved and its successor is published, an operator can write `resolution.json` in the failed release root with `owner_id`, an explanation in `reason`, the absolute `successor` root and its `successor_candidate_sha256`. Cleanup checks the ownership, successor publication record and source ancestry before archiving compiler logs and removing registered copies. A newer directory name alone is insufficient.

Publication and cleanup have separate results. If cleanup fails, preserve its report and retry the cleanup command after resolving the listed cause. Do not repeat publication to free disk space.
