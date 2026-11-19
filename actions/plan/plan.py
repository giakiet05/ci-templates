"""Plan one run of the service pipeline.

Reads the caller's Compose file (already converted to JSON by yq, without interpolation), optionally
merged with a build overlay, derives the run mode from the GitHub event, and decides which services
and check-only directories the quality and build jobs must handle. Results are written as
compact JSON to $GITHUB_OUTPUT.

The overlay exists because a deploy platform that sees `build:` (Coolify does) builds the image
itself instead of pulling the one CI tested. The deploy file then lists only what runs, and the
overlay (usually compose.override.yaml, which `docker compose` merges automatically) adds `build:`
and `x-ci` for CI and local development.

Standard library only: GitHub-hosted runners ship Python 3.12, so no 3.13+ syntax is used.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import posixpath
import sys
from dataclasses import dataclass
from typing import Any

# In a pull request, changes under these paths can alter every job, so they check everything.
# (.trivyignore.yaml needs no entry: the scan runs only on main, where every image is rebuilt.)
GLOBAL_TRIGGERS: tuple[str, ...] = (".github/**",)
SHORT_SHA_LENGTH = 7
# The only branch whose pushes build and push images; releases are tags on its commits.
PUBLISH_REF = "refs/heads/main"


class PlanError(Exception):
    """Raised when the event or the Compose file breaks the pipeline contract."""


@dataclass(frozen=True)
class Service:
    """A Compose service with a build section, i.e. one image the pipeline owns.

    Attributes:
        name: Compose service name; also the matrix label.
        image: Image repository without tag, e.g. ghcr.io/owner/app-server.
        dir: Repo-relative directory holding the service's Makefile.
        context: Repo-relative Docker build context.
        dockerfile: Repo-relative path of the Dockerfile.
        target: Optional build stage.
        args: Build arguments as KEY=VALUE lines.
        watch: Repo-relative globs whose changes require this service to be rebuilt.
    """

    name: str
    image: str
    dir: str
    context: str
    dockerfile: str
    target: str
    args: tuple[str, ...]
    watch: tuple[str, ...]


@dataclass(frozen=True)
class Check:
    """A directory that is linted and tested but produces no image (a shared library).

    Attributes:
        name: Matrix label (the directory path).
        dir: Repo-relative directory holding the Makefile.
        watch: Globs whose changes require the checks to run.
    """

    name: str
    dir: str
    watch: tuple[str, ...]


def resolve_mode(event_name: str, ref: str) -> str:
    """Map a GitHub event to the pipeline mode.

    Args:
        event_name: Value of github.event_name.
        ref: Value of github.ref.

    Returns:
        "check" for pull requests, "publish" for pushes and manual runs on the publish branch,
        "release" for pushed tags starting with "v".

    Raises:
        PlanError: For events or refs the pipeline does not support, including a push or manual
            run on any branch other than the publish branch.
    """
    if event_name == "pull_request":
        return "check"
    if event_name == "push" and ref.startswith("refs/tags/v"):
        return "release"
    if event_name in ("push", "workflow_dispatch"):
        if ref == PUBLISH_REF:
            return "publish"
        # Images are built only from main, which receives code through reviewed PRs.
        raise PlanError(f"{event_name} on {ref}: images are published only from {PUBLISH_REF}")
    raise PlanError(f"unsupported trigger: event={event_name} ref={ref}")


def image_repository(image: str, service: str) -> str:
    """Strip the tag and digest from an image reference.

    Args:
        image: Compose image value, e.g. ghcr.io/owner/app:${TAG:-main}.
        service: Service name, used in error messages.

    Returns:
        The repository part, e.g. ghcr.io/owner/app.

    Raises:
        PlanError: If the result is empty or not lowercase (registries reject uppercase).
    """
    repo = image.split("@", 1)[0]
    head, _, last = repo.rpartition("/")
    # A colon in the last path segment is a tag; a colon earlier is a registry port.
    if ":" in last:
        last = last.split(":", 1)[0]
    repo = f"{head}/{last}" if head else last
    if not last:
        raise PlanError(f"service {service}: image {image!r} has no repository name")
    if repo != repo.lower():
        raise PlanError(f"service {service}: image repository {repo!r} must be lowercase")
    return repo


def _repo_path(base: str, relative: str, what: str) -> str:
    """Join a path onto a repo-relative base and reject paths that leave the repository.

    Args:
        base: Repo-relative directory ("." for the root).
        relative: Path relative to base.
        what: Description used in error messages.

    Returns:
        A normalized repo-relative POSIX path ("." for the root).

    Raises:
        PlanError: If the path is absolute or escapes the repository root.
    """
    if posixpath.isabs(relative):
        raise PlanError(f"{what}: absolute path {relative!r} is not allowed")
    path = posixpath.normpath(posixpath.join(base, relative))
    if path == ".." or path.startswith("../"):
        raise PlanError(f"{what}: path {relative!r} escapes the repository")
    return path


def _default_watch(directory: str) -> str:
    """Return the glob matching every file under a repo-relative directory."""
    return "**" if directory == "." else f"{directory}/**"


def _build_args(raw: Any, service: str) -> tuple[str, ...]:
    """Normalize Compose build args (mapping or list) into KEY=VALUE lines.

    Args whose value is null are skipped: Compose would read them from the environment,
    which CI does not provide.

    Raises:
        PlanError: If args is neither a mapping nor a list.
    """
    if raw is None:
        return ()
    if isinstance(raw, dict):
        return tuple(f"{key}={value}" for key, value in raw.items() if value is not None)
    if isinstance(raw, list):
        return tuple(str(item) for item in raw if "=" in str(item))
    raise PlanError(f"service {service}: build.args must be a mapping or a list")


def _string_list(raw: Any, what: str) -> tuple[str, ...]:
    """Validate an optional list of strings from an x-ci field."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise PlanError(f"{what} must be a list of strings")
    return tuple(raw)


def parse_compose(compose: dict[str, Any], compose_file: str) -> tuple[list[Service], list[Check]]:
    """Extract buildable services and check-only directories from a Compose document.

    Compose paths (build context, dockerfile) follow Compose rules: relative to the Compose
    file's directory and the context respectively. x-ci paths are relative to the repo root.

    Args:
        compose: The Compose document as parsed JSON.
        compose_file: Repo-relative path of the Compose file.

    Returns:
        Services with a build section (in file order) and the top-level x-ci.checks entries.

    Raises:
        PlanError: If the file breaks the contract (no buildable service, missing image,
            duplicate image, inline Dockerfile, paths outside the repository).
    """
    compose_dir = posixpath.dirname(compose_file) or "."
    raw_services = compose.get("services") or {}
    if not isinstance(raw_services, dict):
        raise PlanError(f"{compose_file}: services must be a mapping")

    services: list[Service] = []
    seen_images: dict[str, Service] = {}
    for name, spec in raw_services.items():
        if not isinstance(spec, dict) or "build" not in spec:
            continue  # infrastructure or pre-built images are not ours to build
        build = spec["build"]
        if isinstance(build, str):
            build = {"context": build}
        if not isinstance(build, dict):
            raise PlanError(f"service {name}: build must be a string or a mapping")
        if "dockerfile_inline" in build:
            raise PlanError(f"service {name}: dockerfile_inline is not supported; use a Dockerfile")

        image = spec.get("image")
        if not isinstance(image, str) or not image:
            raise PlanError(f"service {name}: a buildable service needs an image (e.g. ghcr.io/owner/{name}:${{TAG:-main}})")
        repo = image_repository(image, name)

        context = _repo_path(compose_dir, str(build.get("context", ".")), f"service {name} build.context")
        dockerfile = _repo_path(context, str(build.get("dockerfile", "Dockerfile")), f"service {name} build.dockerfile")
        target = str(build.get("target") or "")
        args = _build_args(build.get("args"), name)

        # Several services may run one image (e.g. a bootstrap job overriding the entrypoint);
        # it is built once, under the first service's name, as long as the builds are identical.
        if repo in seen_images:
            first = seen_images[repo]
            if (first.context, first.dockerfile, first.target, first.args) != (context, dockerfile, target, args):
                raise PlanError(f"services {first.name} and {name} share image {repo} but build it differently")
            continue

        x_ci = spec.get("x-ci") or {}
        if not isinstance(x_ci, dict):
            raise PlanError(f"service {name}: x-ci must be a mapping")
        if "path" in x_ci:
            directory = _repo_path(".", str(x_ci["path"]), f"service {name} x-ci.path")
        else:
            directory = posixpath.dirname(dockerfile) or "."
        extra_watch = _string_list(x_ci.get("watch"), f"service {name} x-ci.watch")

        service = Service(
            name=name,
            image=repo,
            dir=directory,
            context=context,
            dockerfile=dockerfile,
            target=target,
            args=args,
            watch=(_default_watch(directory), *extra_watch),
        )
        seen_images[repo] = service
        services.append(service)

    if not services:
        raise PlanError(f"{compose_file}: no service has a build section, so there is nothing to build")

    top_x_ci = compose.get("x-ci") or {}
    if not isinstance(top_x_ci, dict):
        raise PlanError(f"{compose_file}: top-level x-ci must be a mapping")
    checks = [
        Check(name=path, dir=path, watch=(_default_watch(path),))
        for path in (_repo_path(".", raw, "x-ci.checks entry") for raw in _string_list(top_x_ci.get("checks"), "x-ci.checks"))
    ]
    return services, checks


def merge_compose(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge a Compose overlay onto a base document, the way `docker compose -f base -f overlay` does
    for the keys the pipeline reads.

    Mappings are merged key by key; any other value in the overlay (string, list, number) replaces
    the base value. Neither input is modified.

    Args:
        base: The deploy Compose document.
        overlay: The build overlay document.

    Returns:
        A new merged document.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_compose(merged[key], value)
        else:
            merged[key] = value
    return merged


def check_owned_images_are_built(compose: dict[str, Any], owned_registry: str) -> None:
    """Fail when a service runs an image from the caller's own registry namespace but nothing builds it.

    With a build overlay, forgetting a service's `build:` there would leave CI never building an
    image the deploy file expects to pull. A service whose image really comes from elsewhere can opt
    out with `x-ci: {external: true}`.

    Args:
        compose: The merged Compose document.
        owned_registry: Lowercase image prefix the repository owns, e.g. "ghcr.io/owner/".

    Raises:
        PlanError: Listing every service whose owned image has no build section.
    """
    unbuilt = []
    for name, spec in (compose.get("services") or {}).items():
        if not isinstance(spec, dict) or "build" in spec:
            continue
        image = spec.get("image")
        x_ci = spec.get("x-ci") if isinstance(spec.get("x-ci"), dict) else {}
        if isinstance(image, str) and image.lower().startswith(owned_registry) and not x_ci.get("external"):
            unbuilt.append(name)
    if unbuilt:
        raise PlanError(
            f"services {', '.join(unbuilt)} run an image under {owned_registry} but have no build section; "
            "add one to the build overlay, or mark the service x-ci: {external: true} if another repository builds it"
        )


def _matches(globs: tuple[str, ...], changed: list[str]) -> bool:
    """Return True if any changed path matches any glob.

    fnmatch's "*" also matches "/", so "apps/server/**" and "apps/server/*" are equivalent.
    """
    return any(fnmatch.fnmatchcase(path, glob) for path in changed for glob in globs)


def build_plan(
    compose: dict[str, Any],
    compose_file: str,
    mode: str,
    sha: str,
    changed: list[str] | None,
    build_overlay: dict[str, Any] | None = None,
    build_compose_file: str | None = None,
    owned_registry: str | None = None,
) -> dict[str, Any]:
    """Compute the job matrices for one pipeline run.

    Args:
        compose: The deploy Compose document as parsed JSON.
        compose_file: Repo-relative path of the deploy Compose file. Relative paths in both files
            resolve from its directory, as Docker Compose resolves them from the first file.
        mode: Result of resolve_mode.
        sha: Commit SHA the run is for (github.sha).
        changed: Repo-relative paths changed by the pull request, or None to treat everything as
            changed. Only check mode uses it.
        build_overlay: Optional Compose document adding `build:` and `x-ci`, merged onto compose.
        build_compose_file: Repo-relative path of the overlay; in a pull request, a change to it
            checks everything.
        owned_registry: With an overlay, the image prefix the repository owns (e.g. "ghcr.io/owner/");
            services under it must end up with a build section.

    Returns:
        A mapping with mode, sha-tag and the checks/build/all lists:
        - check: lint, test and build (without pushing) only what the pull request touches.
        - publish: build every image, so each main commit has a complete set to release; no lint
          or test, because the pull request into main already ran them on this code.
        - release: nothing to check or build; promote uses the all list.

    Raises:
        PlanError: Propagated from parse_compose and check_owned_images_are_built, or for a SHA
            that is too short.
    """
    if len(sha) < SHORT_SHA_LENGTH:
        raise PlanError(f"commit sha {sha!r} is too short")
    if build_overlay is not None:
        compose = merge_compose(compose, build_overlay)
        if owned_registry:
            check_owned_images_are_built(compose, owned_registry.lower())
    services, checks = parse_compose(compose, compose_file)

    check_targets: list[dict[str, str]] = []
    build_services: list[Service] = []
    if mode == "check":
        triggers = (compose_file, *((build_compose_file,) if build_compose_file else ()), *GLOBAL_TRIGGERS)
        if changed is not None and _matches(triggers, changed):
            changed = None

        def touched(watch: tuple[str, ...]) -> bool:
            return changed is None or _matches(watch, changed)

        build_services = [svc for svc in services if touched(svc.watch)]
        check_targets = [{"name": svc.name, "dir": svc.dir} for svc in build_services]
        check_targets += [{"name": chk.name, "dir": chk.dir} for chk in checks if touched(chk.watch)]
    elif mode == "publish":
        build_services = services

    return {
        "mode": mode,
        "sha-tag": f"sha-{sha[:SHORT_SHA_LENGTH]}",
        "checks": check_targets,
        "build": [
            {
                "name": svc.name,
                "image": svc.image,
                "context": svc.context,
                "dockerfile": svc.dockerfile,
                "target": svc.target,
                "args": "\n".join(svc.args),
            }
            for svc in build_services
        ],
        "all": [{"name": svc.name, "image": svc.image} for svc in services],
    }


def write_outputs(plan: dict[str, Any], output_path: str) -> None:
    """Append the plan to a $GITHUB_OUTPUT file, lists as compact JSON on one line each."""
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in plan.items():
            text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
            handle.write(f"{key}={text}\n")


def _read_changed(path: str) -> list[str]:
    """Read one repo-relative path per line, ignoring blank lines."""
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by action.yml.

    Returns:
        0 on success, 1 when the contract is broken (reported as a GitHub ::error:: annotation).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-json", required=True, help="Compose file converted to JSON")
    parser.add_argument("--compose-file", required=True, help="repo-relative path of the Compose file")
    parser.add_argument("--event", required=True, help="github.event_name")
    parser.add_argument("--ref", required=True, help="github.ref")
    parser.add_argument("--sha", required=True, help="github.sha")
    parser.add_argument("--changed-files", help="file listing changed paths; omit to treat everything as changed")
    parser.add_argument("--build-compose-json", help="build overlay converted to JSON (optional)")
    parser.add_argument("--build-compose-file", help="repo-relative path of the build overlay")
    parser.add_argument("--owned-registry", help="image prefix the repository owns, e.g. ghcr.io/owner/")
    args = parser.parse_args(argv)
    if bool(args.build_compose_json) != bool(args.build_compose_file):
        parser.error("--build-compose-json and --build-compose-file must be given together")

    try:
        with open(args.compose_json, encoding="utf-8") as handle:
            compose = json.load(handle)
        overlay = None
        build_compose_file = None
        if args.build_compose_json:
            with open(args.build_compose_json, encoding="utf-8") as handle:
                overlay = json.load(handle)
            if not isinstance(overlay, dict):
                raise PlanError(f"{args.build_compose_file}: the build overlay must be a mapping")
            build_compose_file = posixpath.normpath(args.build_compose_file)
        changed = _read_changed(args.changed_files) if args.changed_files else None
        compose_file = posixpath.normpath(args.compose_file)
        plan = build_plan(
            compose,
            compose_file,
            resolve_mode(args.event, args.ref),
            args.sha,
            changed,
            build_overlay=overlay,
            build_compose_file=build_compose_file,
            owned_registry=args.owned_registry,
        )
    except (PlanError, OSError, json.JSONDecodeError) as exc:
        print(f"::error title=plan::{exc}")
        return 1

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        write_outputs(plan, output_path)
    scope = "all paths" if changed is None else f"{len(changed)} changed paths"
    print(f"plan mode={plan['mode']} sha_tag={plan['sha-tag']} scope=\"{scope}\"")
    for key in ("checks", "build"):
        names = ", ".join(item["name"] for item in plan[key]) or "-"
        print(f"plan {key}: {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
