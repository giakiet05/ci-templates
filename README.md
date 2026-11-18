# ci-templates

One reusable GitHub Actions pipeline for container services: lint, test, build each image once,
promote the same image on release, deploy, and publish a GitHub Release. It works for
single-service repos and monorepos in any language, because each repo describes itself through
a Compose file and Makefiles instead of through pipeline configuration.

## How a repo uses it

`.github/workflows/pipeline.yml` in the consuming repo:

```yaml
name: pipeline
on:
  pull_request:
    branches: [dev, main]
  push:
    branches: [dev]
    tags: ["v*"]
  workflow_dispatch:   # manual run = rebuild everything

concurrency:   # publishes run in order so :dev moves in order; stale PR runs are cancelled
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

permissions:   # ceiling for the template's jobs; each job asks for less
  contents: write
  packages: write

jobs:
  pipeline:
    uses: giakiet05/ci-templates/.github/workflows/service.yml@main
    with:
      compose-file: compose.yaml   # default
      deploy: coolify              # or none (default)
    secrets: inherit
```

## The contract

| # | The repo provides | Notes |
|---|---|---|
| 1 | A **deploy Compose file** (`compose-file` input, default `compose.yaml`) with **app services only** | Infrastructure (Postgres, Kafka...) belongs in `compose.dev.yaml` for local use. Coolify runs every service in the deploy file |
| 2 | Every service with `build:` also has `image: ghcr.io/<owner>/<name>:${TAG:-dev}` | Lowercase. CI pushes to that repository with its own tags; Coolify pulls it with `TAG=vX.Y.Z`. Services may share an image (e.g. a bootstrap job) only with identical `build:` sections; it is built once |
| 3 | A **Makefile next to each service's Dockerfile** with `lint` and `test` | Optional: `test-integration` (run after `test`), `ci-setup` (installs extra tools before anything else) |
| 4 | Optional `x-ci` on a service | `watch`: extra repo-root globs that should rebuild it (shared code outside its folder). `path`: Makefile directory when it is not the Dockerfile's folder |
| 5 | Optional top-level `x-ci.checks` | Directories that are linted and tested but have no image (shared libraries) |
| 6 | Branch flow: PRs into `dev`; `dev` into `main` **fast-forward only**; releases are `v*` tags on `main` | Promotion finds images by commit SHA, so a merge commit on `main` would have no images |

Paths in `build:` follow Compose rules (relative to the Compose file). Paths in `x-ci` are relative
to the repo root. `${VAR}` values are never interpolated by CI, so `${VAR:?message}` is fine.
Not supported: `dockerfile_inline`, build contexts outside the repository.

Example monorepo service:

```yaml
x-ci:
  checks: [libs/shared]

services:
  api:
    image: ghcr.io/owner/shop-api:${TAG:-dev}
    build:
      context: .
      dockerfile: services/api/Dockerfile
    x-ci:
      watch: ["libs/shared/**"]
```

## What runs when

| Event | Mode | Jobs (as named in the Actions UI) |
|---|---|---|
| PR into `dev` | check | plan run, lint and test, build image (no push) for changed services |
| PR into `main` | check | the same, plus **check release images exist** (preflight): every image `:sha-<head>` must exist |
| Push to `dev` | publish | lint and test, build image + push `:sha-<short>`, Trivy scan (critical, fixable), move `:dev`, **retag unchanged images** (carry) |
| Manual run | publish | the same, treating every path as changed |
| Tag `v*` | release | **promote images to release** (`:sha-<short>` to `:vX.Y.Z`), **deploy release**, **create GitHub release** |

Job IDs in `service.yml` stay short (`resolve`, `quality`, `build`, `carry`, `preflight`, `promote`,
`deploy`, `release`); they are what `needs:` refers to.

**Changed services** are those whose folder or `watch` globs match the diff. A change to the
Compose file, to `.github/` or to `.trivyignore.yaml` counts as a change to everything.

**Carry**: services that did not change are retagged from `:dev` to `:sha-<this commit>` (a
manifest copy, no rebuild), so every commit on `dev` has a complete image set and can be released.

**Accepting a scan finding**: when a critical CVE cannot be fixed yet (for example a JAR bundled
by a dependency with no patched release), list it in `.trivyignore.yaml` at the repository root
with the package, a statement of why it is not exploitable, and an `expired_at` date. The scan
picks the file up automatically and fails again once the date passes.

```yaml
vulnerabilities:
  - id: CVE-2022-46337
    purls: ["pkg:maven/org.apache.derby/derby@10.16.1.1"]
    statement: Bundled by pyspark; the Hive metastore that would run Derby is never enabled.
    expired_at: 2027-02-17
```

## Secrets

| Secret | When | What |
|---|---|---|
| `GITHUB_TOKEN` | always | Provided by GitHub; pushes to GHCR and creates the release |
| `COOLIFY_API_URL`, `COOLIFY_TOKEN`, `COOLIFY_APP_UUID` | `deploy: coolify` | The Coolify app must use the repo's Compose file and have a `TAG` environment variable |

The deploy server pulls private GHCR images with a GitHub token that has only `read:packages`,
configured as a registry credential in Coolify. When Coolify is reachable only on the LAN, run the
deploy job on a self-hosted runner: `with: { runs-on: <runner-label> }`.

## Layout

```
.github/workflows/service.yml    the pipeline (workflow_call)
.github/workflows/self-test.yml  CI for this repo
actions/plan/                    Compose + diff -> job matrices (Python, stdlib only, unit tested)
actions/setup-toolchain/         Go / uv / Node from go.mod / pyproject.toml / package.json, then make ci-setup
actions/deploy-coolify/          set TAG, trigger a Coolify deployment, wait for the result
```

## Working on this repo

```bash
make test   # unit tests for actions/plan
make lint   # actionlint (via Docker) on the workflows
```

All third-party actions are pinned to full commit SHAs; Dependabot proposes updates weekly.

**Versioning.** Consumers use `@main` while the template is being developed. For a release: replace
`@main` in the internal `uses: giakiet05/ci-templates/actions/...` lines of `service.yml` with the
major tag (`@v1`), commit, tag `v1.x.y`, and move `v1` to it. The internal references have to name
a ref explicitly because GitHub cannot resolve "the same ref as this workflow" for actions.

**Unverified so far:** the pipeline has not run against a real consumer yet, and the Coolify API
calls (`PATCH /applications/{uuid}/envs`, `GET /deploy`, `GET /deployments/{uuid}`) have not been
checked against a live Coolify instance.
