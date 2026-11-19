# 2. Keep build sections in a Compose overlay so deploy platforms only pull

- Status: accepted
- Date: 2026-11-19

## Context

The contract used one Compose file for both CI (which reads `build:` to know what to build) and the
deploy platform (which runs the services). A test deploy on Coolify 4.7 showed that when the deploy
file contains `build:`, Coolify clones the repository, injects its own `ARG` lines into the
Dockerfiles and builds the images on the server. Production would then run an image CI never
tested or scanned, and a release tag would only name that local build.

## Decision

- The deploy file (`compose.yaml`) lists only what runs: `image:`, environment, restart, health.
- An optional build overlay (`build-compose-file` input, normally `compose.override.yaml`) adds the
  `build:` sections and `x-ci`; the plan action merges it onto the deploy file.
- With an overlay, every service whose image is under `ghcr.io/<owner>/` must get a `build:` from
  it, unless marked `x-ci: {external: true}`, so a forgotten service fails the plan instead of
  failing the deploy.

## Consequences

- Coolify runs `docker compose -f compose.yaml` and can only pull the CI-built images.
- Local development is unchanged: `docker compose` merges `compose.override.yaml` automatically when
  no `-f` is given.
- About five extra lines per service, written once. Repos that do not deploy to Coolify can keep a
  single file; the input is optional.
- The split is not tied to Coolify: the overlay stays the build description on any platform, and
  only the deploy adapter changes.
