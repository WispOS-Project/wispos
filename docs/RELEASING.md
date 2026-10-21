# Building and releasing WispOS

## Source and identity

Build directly from a clean checkout of this code repository. Product development,
review, dev images and release images share this repository and the same pipeline.
There is no generated public tree or code-export step.

`manifest/lockfile.json` owns version and build ID; `CHANGELOG.md` records changes;
annotated immutable `vX.Y.Z` tags identify releases. A completed task is not a release.
Prepare a candidate on a branch and record the exact revision and artifact hashes.

## Candidate verification

1. Set version/build identity in the manifest. Run `tools/changelog.py highest` to see which SemVer bump the pending fragments call for, then
   `tools/changelog.py release X.Y.Z YYYY-MM-DD` to assemble them into `CHANGELOG.md` and remove the fragments.
2. Run `os/build-all.sh WORKDIR` with an HDD-backed work directory. With the local
   dev inputs documented in [CONTRIBUTING.md](../CONTRIBUTING.md), the same invocation
   also builds the development variant. Do not edit a separate copy of product code.
3. Run relevant host tests, `os/verify_image.py`, `os/check_rootfs.py`, and the
   applicable QEMU system/update tests. Record failures and outstanding proof honestly.
4. Verify the candidate on the handheld. A dev device can test signed candidates
   without a stable release: `os/dev/test-release.sh WORKDIR` serves a signed test
   update; see [developer tooling](../os/dev/README.md). The dev-only update-source
   override uses the same signature and payload checks. Remove the override afterward.
5. Confirm the trial boot and required device behavior. A failure requires a new
   candidate build ID and renewed proof; do not relabel a different image as tested.

## Release

Finalize the changelog on the reviewed source revision. Use `os/repro-check.sh`
to prove the release build matches the tested candidate; changes to image inputs
require rebuilding and repeating the relevant verification. Create the annotated
version tag only from a clean, verified revision after the maintainer merges it.

`os/make-release.py build` packages the candidate KERNEL and SYSTEM with the
signed manifest. Supply the release URL of this repository and a private signing
key kept outside Git. Inspect the strict release gate and any outstanding gaps;
never call an overridden gate a pass. Publish only explicitly authorized release
assets. Do not publish personalized dev images, signing keys, or local configuration.

Stable devices consume the signed assets through the repository's configured
release channel. Test builds use the dev override; they do not need a stable release
or a SemVer bump just to run an experiment. A separate output/update source is a
build variant, not a separate code lineage.

The update carries KERNEL and SYSTEM. Changes to shared boot files, device trees,
or boot arguments may require reflashing and must say so in the release notes.
