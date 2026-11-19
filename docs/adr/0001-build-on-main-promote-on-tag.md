# 1. Build images on main, promote them on tag

- Status: accepted
- Date: 2026-11-19

## Context

The first version built and pushed images on every push to `dev`, retagged unchanged services
("carry") so each `dev` commit had a full image set, and required `dev` to be fast-forwarded into
`main` so a release tag pointed at a commit whose images existed.

That design has costs that only pay off with a staging environment running the `dev` images:

- `main` could only move by a terminal fast-forward, never through a PR merge button, because every
  merge style creates a new SHA with no images.
- Carry copied `:dev` forward; when a publish failed or a queued run was cancelled, a later commit
  could carry an image built from older code.
- The consuming projects are mostly solo and run no staging server, so the `dev` images were never
  exercised before release; only unit tests and the Trivy scan stood behind them.

## Decision

- Pull requests (into `dev` or `main`): lint, test and build, without pushing, only the services the
  diff touches.
- A push to `main` (the PR merge) builds every image, scans it, pushes `:sha-<short>` and moves
  `:main`. Lint and test are skipped there: the PR into `main` ran them on the same code.
- A `v*` tag on a `main` commit retags its `:sha-<short>` images to the version, then deploys.
- Pushes or manual runs on any other branch fail with an explicit error.

## Consequences

- `dev` merges into `main` through a normal PR; carry, `:dev` and the release preflight check are
  gone, and with them the stale-carry failure mode.
- Build once, promote the same image still holds: the release is the image built and scanned on
  `main`, not a rebuild.
- Every merge into `main` rebuilds all images; merges into `main` are infrequent (releases) and the
  layer cache keeps unchanged services fast.
- A tag pushed before the `main` pipeline finishes fails in promote; push it again once it is green.
- Without staging, a candidate is checked locally with
  `TAG=sha-<short> docker compose -f compose.yaml up`.
- A repo that later adds staging can deploy `:main` there before tagging, without changing this
  model.
