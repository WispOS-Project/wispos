# Contributing to WispOS

## One source repository

This repository owns all product code, build scripts, developer tools, tests,
issues, pull requests, releases, and update assets. Develop on a branch based on
its `main`, and open the pull request against this repository's `main`.
Contributors may use a fork. Both development and release images are built from
that same checkout and revision; neither is maintained in a second code repository.

Agent configuration belongs outside the product repository. If a developer keeps
a code checkout inside an agent workspace, ignore that checkout in the workspace's
Git repository. It has its own Git history and remote; it is not a submodule,
vendor copy, export target, or source to synchronize from a workflow repository.

## Build both variants

Use an HDD-backed absolute work directory, with scratch space on the same disk.
Install the prerequisites listed in the README through your normal host provisioning
process; build scripts do not substitute missing prerequisites.

```sh
os/build-all.sh /mnt/hdd/wispos-staging/candidate
```

For the development variant, supply a public SSH key file and an announcement
listener address as local environment configuration, then run the same command:

```sh
export WISP_SSH_KEY=/path/to/developer.pub
export WISP_DEV_ANNOUNCE=developer-host:port
os/build-all.sh /mnt/hdd/wispos-staging/candidate
```

The orchestrator builds the shared boot chain and release SYSTEM, then optionally
builds the dev SYSTEM and image. `os/rootfs-dev/` adds diagnostic facilities;
production policy belongs in the shared base, not in leftover experimental overrides.
Release artifacts exclude developer keys and announcement configuration. Do not
publish personalized development images or commit local credentials/configuration.

## Verification and review

Run relevant host tests, then QEMU system tests against the rebuilt image when
appropriate. Hardware behavior requires physical-device proof; distinguish checks
that ran from checks still outstanding. A bug fix includes a regression test shown
failing before the fix and passing afterward. UI changes include inspected captures.
See [the release guide](docs/RELEASING.md) for the build and update sequence.

Every behavior change adds an Unreleased changelog entry with its SemVer class.
Use conventional commit subjects and an issue footer (`Refs #N`, or `Fixes #N`
when complete). Run `tools/install-hooks.sh` once per clone for the commit backstop.
PRs use a conventional title and Summary, Proof, Issue sections. Keep public text
self-contained, free of private workspace details, and explicit about missing proof.
Handle every review finding by fixing it, filing follow-up work, or rebutting it
with evidence. Resolve review threads, watch for new feedback, and leave merging
to the maintainer.
