# Changelog fragments

Each change adds one file here instead of editing `CHANGELOG.md`, so concurrent PRs never conflict.

- Name: `<issue>.<class>.md`, `<issue>` the GitHub issue number, `<class>` one of `major`, `minor`, `patch`, `none`.
  `none` is for documentation and tooling with no SemVer effect. Classes are defined in the SemVer policy (a change nothing ever promised is `minor`, not `patch`).
- Body: the entry as it should read in the changelog, without the leading `- `, in the same shape as existing entries
  (**`bold title` (kind, CLASS; #issue):** what changed and why).
- The author chooses the class; review checks it. Nothing infers it from the commit prefix.
- At release the maintainer runs `tools/changelog.py release X.Y.Z YYYY-MM-DD` (see `docs/RELEASING.md`); it moves the fragments into a dated
  `CHANGELOG.md` section and deletes them. `tools/changelog.py check` validates the files; `highest` names the bump the pending changes call for.
