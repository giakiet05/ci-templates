# ci-templates

One reusable GitHub Actions pipeline for container services: lint, test, build each image once,
promote the same image on release, deploy, and publish a GitHub Release. It works for
single-service repos and monorepos in any language, because each repo describes itself through
a Compose file and Makefiles instead of through pipeline configuration.

## How a repo uses it

Copy [`templates/pipeline.yml`](templates/pipeline.yml) to `.github/workflows/pipeline.yml` in the
consuming repo, then set `compose-file` and `deploy` in its `with:` block:

```bash
mkdir -p .github/workflows
curl -fsSL https://raw.githubusercontent.com/giakiet05/ci-templates/main/templates/pipeline.yml -o .github/workflows/pipeline.yml
```

The starter sets the triggers the pipeline expects (PRs into `dev`/`main`, pushes to `main`, `v*`
tags, manual runs), the concurrency group and the permission ceiling; keep those as they are.

## The contract

| # | The repo provides | Notes |
|---|---|---|
| 1 | A **deploy Compose file** (`compose-file` input, default `compose.yaml`) with **app services only** | Infrastructure (Postgres, Kafka...) belongs in `compose.dev.yaml` for local use. Coolify runs every service in the deploy file |
| 2 | Every service CI builds has `image: ghcr.io/<owner>/<name>:${TAG:-main}` | Lowercase. CI pushes to that repository with its own tags; the platform pulls it with `TAG=vX.Y.Z`. Services may share an image (e.g. a bootstrap job) only with identical `build:` sections; it is built once |
| 3 | The `build:` sections: in the deploy file, or in a **build overlay** (`build-compose-file` input, e.g. `compose.override.yaml`) | **Required for Coolify**: a deploy file with `build:` makes Coolify build the images on the server instead of pulling the ones CI tested and scanned. With an overlay, every service whose image is under `ghcr.io/<owner>/` must get a `build:` from it, unless marked `x-ci: {external: true}` |
| 4 | A **Makefile next to each service's Dockerfile** with `lint` and `test` | Optional: `test-integration` (run after `test`), `ci-setup` (installs extra tools before anything else) |
| 5 | Optional `x-ci` on a service | `watch`: extra repo-root globs that should rebuild it (shared code outside its folder). `path`: Makefile directory when it is not the Dockerfile's folder |
| 6 | Optional top-level `x-ci.checks` | Directories that are linted and tested but have no image (shared libraries) |
| 7 | Branch flow: PRs into `dev`; `dev` into `main` **by PR** (the normal merge button; "Create a merge commit" recommended); releases are `v*` tags on `main` commits whose pipeline finished green | Images are built from `main` only, so every change reaching them went through a reviewed, checked PR. The `dev` -> `main` PR itself is not re-checked (see below); set `promotion-branch` if the branch is not called `dev`, or to `""` to always check |

Paths in `build:` follow Compose rules: relative to the deploy Compose file's directory, in the
overlay too (Compose resolves every file from the first one). Paths in `x-ci` are relative to the
repo root. `x-ci` may live in either file. `${VAR}` values are never interpolated by CI, so `${VAR:?message}` is fine.
Not supported: `dockerfile_inline`, build contexts outside the repository.

Example monorepo service, split for Coolify:

```yaml
# compose.yaml: what runs (Coolify reads only this file)
services:
  api:
    image: ghcr.io/owner/shop-api:${TAG:-main}
    restart: unless-stopped
```

```yaml
# compose.override.yaml: how to build (CI, and `docker compose up --build` locally)
x-ci:
  checks: [libs/shared]

services:
  api:
    build:
      context: .
      dockerfile: services/api/Dockerfile
    x-ci:
      watch: ["libs/shared/**"]
```

Pass `build-compose-file: compose.override.yaml` in the caller's `with:` block. `docker compose`
merges `compose.override.yaml` automatically when no `-f` is given, so local commands stay the same,
while Coolify runs `docker compose -f compose.yaml` and never sees a `build:` section. A repo that
does not deploy to Coolify may keep everything in one file.

## What runs when

| Event | Mode | Jobs (as named in the Actions UI) |
|---|---|---|
| PR into `dev` or `main` | check | plan run, lint and test, build image (no push) for changed services |
| PR `dev` -> `main` adding nothing beyond `dev` | check | plan run only (green); nothing to lint, test or build |
| Push to `main` (the PR merge) | publish | build image for each **changed** service and push `:sha-<short>`; retag every other image from the previous `main` commit's `:sha-<short>`; Trivy scan (critical, fixable); move `:main`. No lint and test: the PRs into `dev` ran them on this code |
| Manual run on `main` | publish | build image for **every** service, e.g. to pick up a fixed base image |
| Tag `v*` | release | **promote images to release** (`:sha-<short>` to `:vX.Y.Z`), **deploy release**, **create GitHub release** |

Pushes to `dev` run nothing (every change reached `dev` through a checked PR); a push or manual run
on any branch other than `main` fails with a clear error. Job IDs in `service.yml` stay short
(`resolve`, `quality`, `build`, `promote`, `deploy`, `release`); they are what `needs:` refers to.

**Changed services** are those whose folder or `watch` globs match the diff against the base commit
(the PR's base, or the previous `main` commit for a publish). An edit to the Compose file or the
build overlay selects only the services whose build definition changed: both files are parsed at the
base and compared service by service on `image` repository, `build:` (context, dockerfile, target,
args) and `x-ci` (path, watch), plus any new `x-ci.checks` entry. Runtime-only edits (`restart`,
environment, ports, networks, healthcheck) build nothing. A change under `.github/` counts as a
change to everything, and so does a Compose file that cannot be parsed at the base.

**Promotion PRs.** A PR from `promotion-branch` (default `dev`, same repository) into `main` runs
only the plan job when the merge result has exactly the tree of the `dev` tip: every commit on
`dev` was linted, tested and built in its own PR. If `main` has something `dev` lacks (a hotfix),
the trees differ and the PR is checked normally.

**Carry on publish.** Every `main` commit still gets a complete `:sha-<short>` set, so any green
`main` commit can be released. Unchanged images are copied from the previous `main` commit's sha tag
(`docker buildx imagetools create`), which was built from the same inputs; when that tag is missing
(its run was cancelled or failed) the image is built instead. A carried image skips the scan only
when it is already `:main`, i.e. it passed the scan before. A manual run, a first push or a force
push has no previous commit to compare with and builds everything. See
[ADR-0003](docs/adr/0003-build-only-changed-images.md).

**Layer cache.** Each built image keeps its BuildKit layer cache in the registry as
`<image>:buildcache`. Publish writes it; PRs only read it. It lives in the registry because Actions
caches are scoped to the branch that wrote them, so a cache written on `main` is invisible to PRs into
the default branch, and because exporting a large image's cache on every PR costs minutes. The lint
and test job logs in to GHCR with read access, so a `test-integration` target that builds the same
image can import the cache too, for example from a CI-only Compose overlay:

```yaml
services:
  app-test:
    build:
      cache_from:
        - type=registry,ref=ghcr.io/<owner>/<image>:buildcache
```

A missing cache (before the first publish, or after a dependency change) only makes the build slower.

**Checking a candidate before tagging** (no staging server needed): once the `main` pipeline is
green, run the exact images a tag would release. `-f compose.yaml` skips the build overlay, so
nothing is built locally:

```bash
TAG=sha-<short> docker compose -f compose.yaml up
```

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
| `COOLIFY_API_URL`, `COOLIFY_TOKEN`, `COOLIFY_APP_UUID` | `deploy: coolify` | The Coolify app must use the repo's Compose file and have a `TAG` environment variable. The API token needs the `read`, `write` and `deploy` permissions (set `TAG`, trigger, poll the deployment status) |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | optional, `deploy: coolify` | A Cloudflare Access service token, when the Coolify API (`/api/`) is behind an Access application with a Service Auth policy; set both or neither |

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
templates/pipeline.yml           starter workflow that consuming repos copy
docs/adr/                        why the pipeline is shaped this way (release model, build overlay, minimal builds)
```

## Working on this repo

```bash
make test   # unit tests for actions/plan
make lint   # actionlint (via Docker) on the workflows and the starter template
```

All third-party actions are pinned to full commit SHAs; Dependabot proposes updates weekly.

**Versioning.** Consumers use `@main` while the template is being developed. For a release: replace
`@main` in the internal `uses: giakiet05/ci-templates/actions/...` lines of `service.yml` with the
major tag (`@v1`), commit, tag `v1.x.y`, and move `v1` to it. The internal references have to name
a ref explicitly because GitHub cannot resolve "the same ref as this workflow" for actions.

**Verified:** Coolify (4.7) builds every service that has `build:` in the deploy file, injecting its
own `ARG` lines into the Dockerfiles; hence the build overlay. **Unverified so far:** the Coolify
API calls (`PATCH /applications/{uuid}/envs`, `GET /deploy`, `GET /deployments/{uuid}`) have not
been checked against a live Coolify instance.
