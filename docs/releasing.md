# Local macOS releases

`scripts/release.py` separates building a candidate from publishing it. It supports macOS arm64 and uses the repository's existing backend build, electron-builder configuration and updater provider. It does not choose a version, edit the changelog, merge a branch or install an update on your machine.

The tool still needs independent local acceptance. Its first complete build and Apple/GitHub publish chain must be verified during a future authorized release; unit tests do not establish that those external steps work.

## Before building

Freeze the seven root version fields and the matching changelog entry, then merge and push that source to `master`. Run from a clean checkout at that exact commit. The previous stable tag must be an ancestor, and the new tag and Release must not exist.

Install Python 3.11, Node 24, npm 11, Xcode command-line tools and `gh`. Sign in with `gh auth login`. The login keychain must contain one valid Developer ID Application identity. Store notarization credentials using Apple's `notarytool store-credentials`, and set `VENUS_NOTARY_PROFILE` to that approved profile's name. Do not put passwords or tokens in command arguments, manifests or this repository.

Create a private release parent directory outside the repository and user application-data directories. Allow at least 15 GiB of free space. The output directory itself must not already exist.

```sh
python3.11 scripts/release.py candidate \
  --source <exact-master-commit> \
  --previous-tag <previous-stable-tag> \
  --output <absolute-new-release-directory>
```

The candidate command checks local and remote source identity, creates an isolated source checkout, installs dependencies, runs Python checks, Desktop tests and production audits, then calls `build-backend.sh`. That script runs the Frontend check once. Packaging explicitly disables notarization and automatic publishing.

Machine checks cover signed arm64 binaries, Hardened Runtime, the bundled backend and FFmpeg, App/ZIP/DMG consistency, startup readiness, project reads and synthetic recording discovery. A loopback updater feed tests the previous release's update selection and ZIP download. It uses Node HTTP transport and does not execute Squirrel or install an update.

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

The tool pushes the exact tag and creates a draft Release. All four assets must match before the Release becomes public. Anonymous downloads and the previous release's GitHub updater provider then verify the published ZIP. Native update installation is outside this probe.

## When a step fails

Keep the release directory and `progress.json`. Re-run with the same arguments only after understanding the failed step. A saved Apple Submission ID is reused; an uncertain submission without an ID stops for reconciliation. A matching existing remote asset is verified, never uploaded with `--clobber`. Unowned tags or Releases are not adopted.

An interrupted local build or finalize directory is deliberately not repaired automatically. Do not edit a frozen candidate to get past a failed check. If publication succeeded but a later download or cleanup failed, the Release may already be visible: report that state rather than claiming a rollback.

After verified publication, cleanup removes only this invocation's rebuildable directories after checking process occupancy. Formal assets, manifests, notarization records and evidence remain. The source repository, user data and other releases are never cleanup targets.
