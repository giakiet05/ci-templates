# 3. Build only changed images; carry the rest on main

- Status: accepted
- Date: 2026-11-22
- Amends: [ADR-0001](0001-build-on-main-promote-on-tag.md) (every merge into `main` rebuilt all images)

## Context

In a monorepo with seven images (three of them about 2 GB), a one-line `restart: "no"` edit to the
deploy Compose file took three full pipeline rounds, each linting, testing and building every image:

1. The feature -> `dev` PR: any Compose edit counted as a change to every service.
2. The `dev` -> `main` PR: the same code again, already checked commit by commit on its way into `dev`.
3. The push to `main`: publish rebuilt every image by design (ADR-0001), so each `main` commit had a
   complete image set to release.

ADR-0001 dropped the old carry (copying `:dev` forward) because a moving tag could hand a later commit
an image built from older code after a failed or cancelled run.

## Decision

- **Compose edits are compared, not counted as global.** The plan action parses the deploy file and
  the build overlay at the base commit and selects only services whose build definition changed
  (image repository, `build:` context/dockerfile/target/args, `x-ci` path/watch) and new
  `x-ci.checks` entries. A base that cannot be parsed falls back to selecting everything.
  `.github/**` stays a global trigger.
- **Promotion PRs are not re-checked.** A PR from `promotion-branch` (default `dev`, same repository)
  into `main` checks and builds nothing when the merge commit's tree equals the branch tip's tree.
  Otherwise `main` holds something the branch lacks and the PR is checked normally. Callers can set
  `promotion-branch: ""` to restore the old behaviour.
- **Publish builds changed images and carries the rest.** On a push to `main`, the diff against
  `github.event.before` picks the services to build. Every other image is retagged from
  `:sha-<before>` to `:sha-<new>`. If that source tag is missing, the image is built instead. A
  carried image is scanned unless its digest equals `:main`, which only moves after a passed scan.
  A manual run, the first push or a force push (no usable `before`) builds every image.
- Release is unchanged: the tag promotes the commit's `:sha-<short>` set listed in `all`.

## Consequences

- A runtime-only Compose edit builds no image in any of the three rounds; the `main` round costs one
  short retag job per image.
- The carry is safe where the old one was not: its source is the immutable sha tag of exactly the
  commit the diff was taken against, never a moving tag, so by induction every `:sha-<short>` image
  was built from that commit's build inputs. Publishes stay serialized by the caller's concurrency
  group.
- The checks that stand behind a release are the feature -> `dev` PRs. Two PRs merged into `dev`
  back to back are never checked together; the same was true of `dev` before.
- Changes to this template (consumed at `@main`) do not appear in a caller's diff; run the pipeline
  manually on `main` to rebuild everything after a template change that affects images.
- A carried image is not rescanned against a newer vulnerability database; a manual run rebuilds and
  rescans everything.
