"""Unit tests for the plan action (run with: python3 -m unittest discover -s actions/plan/tests)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plan  # noqa: E402

SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def monorepo() -> dict:
    """A Compose document shaped like the thesis deploy file (compose under deploy/)."""
    return {
        "x-ci": {"checks": ["apps/scraper/shared"]},
        "services": {
            "server": {
                "image": "ghcr.io/owner/thesis-server:${TAG:-dev}",
                "build": {"context": "../apps/server"},
            },
            "processor": {
                "image": "ghcr.io/owner/thesis-processor:${TAG:-dev}",
                "build": {"context": "..", "dockerfile": "apps/processor/Dockerfile"},
                "x-ci": {"watch": ["packages/**"]},
            },
            "kafka": {"image": "apache/kafka:3.8.0"},
        },
    }


class ResolveModeTest(unittest.TestCase):
    """Event and ref to pipeline mode."""

    def test_pull_request_is_check(self) -> None:
        self.assertEqual(plan.resolve_mode("pull_request", "refs/pull/7/merge"), "check")

    def test_branch_push_is_publish(self) -> None:
        self.assertEqual(plan.resolve_mode("push", "refs/heads/dev"), "publish")

    def test_manual_run_is_publish(self) -> None:
        self.assertEqual(plan.resolve_mode("workflow_dispatch", "refs/heads/dev"), "publish")

    def test_version_tag_is_release(self) -> None:
        self.assertEqual(plan.resolve_mode("push", "refs/tags/v0.3.0"), "release")

    def test_other_tag_is_rejected(self) -> None:
        with self.assertRaises(plan.PlanError):
            plan.resolve_mode("push", "refs/tags/nightly")

    def test_unknown_event_is_rejected(self) -> None:
        with self.assertRaises(plan.PlanError):
            plan.resolve_mode("schedule", "refs/heads/dev")


class ImageRepositoryTest(unittest.TestCase):
    """Tag and digest stripping."""

    def test_strips_interpolated_tag(self) -> None:
        self.assertEqual(plan.image_repository("ghcr.io/o/app:${TAG:-dev}", "s"), "ghcr.io/o/app")

    def test_strips_digest(self) -> None:
        self.assertEqual(plan.image_repository("ghcr.io/o/app@sha256:abc", "s"), "ghcr.io/o/app")

    def test_keeps_registry_port(self) -> None:
        self.assertEqual(plan.image_repository("localhost:5000/app:1", "s"), "localhost:5000/app")

    def test_untagged_image(self) -> None:
        self.assertEqual(plan.image_repository("ghcr.io/o/app", "s"), "ghcr.io/o/app")

    def test_rejects_uppercase(self) -> None:
        with self.assertRaises(plan.PlanError):
            plan.image_repository("ghcr.io/Owner/app:1", "s")


class ParseComposeTest(unittest.TestCase):
    """Compose contract parsing."""

    def test_monorepo_paths_are_repo_relative(self) -> None:
        services, checks = plan.parse_compose(monorepo(), "deploy/compose.yaml")
        by_name = {svc.name: svc for svc in services}
        self.assertEqual(list(by_name), ["server", "processor"])  # kafka has no build section
        self.assertEqual(by_name["server"].context, "apps/server")
        self.assertEqual(by_name["server"].dockerfile, "apps/server/Dockerfile")
        self.assertEqual(by_name["server"].dir, "apps/server")
        self.assertEqual(by_name["processor"].context, ".")
        self.assertEqual(by_name["processor"].dir, "apps/processor")
        self.assertEqual(by_name["processor"].watch, ("apps/processor/**", "packages/**"))
        self.assertEqual([chk.dir for chk in checks], ["apps/scraper/shared"])

    def test_single_service_at_root(self) -> None:
        compose = {"services": {"api": {"image": "ghcr.io/o/api:${TAG:-dev}", "build": "."}}}
        (svc,), checks = plan.parse_compose(compose, "compose.yaml")
        self.assertEqual((svc.context, svc.dockerfile, svc.dir, svc.watch), (".", "Dockerfile", ".", ("**",)))
        self.assertEqual(checks, [])

    def test_x_ci_path_overrides_makefile_dir(self) -> None:
        compose = {"services": {"api": {"image": "ghcr.io/o/api", "build": ".", "x-ci": {"path": "services/api"}}}}
        (svc,), _ = plan.parse_compose(compose, "compose.yaml")
        self.assertEqual(svc.dir, "services/api")

    def test_target_and_args(self) -> None:
        compose = {
            "services": {
                "api": {
                    "image": "ghcr.io/o/api",
                    "build": {"context": ".", "target": "runtime", "args": {"A": "1", "FROM_ENV": None}},
                },
                "web": {"image": "ghcr.io/o/web", "build": {"context": "web", "args": ["B=2", "ONLY_NAME"]}},
            }
        }
        api, web = plan.parse_compose(compose, "compose.yaml")[0]
        self.assertEqual((api.target, api.args), ("runtime", ("A=1",)))
        self.assertEqual(web.args, ("B=2",))

    def test_missing_image_is_rejected(self) -> None:
        with self.assertRaisesRegex(plan.PlanError, "needs an image"):
            plan.parse_compose({"services": {"api": {"build": "."}}}, "compose.yaml")

    def test_shared_image_with_different_builds_is_rejected(self) -> None:
        compose = {
            "services": {
                "a": {"image": "ghcr.io/o/x:1", "build": "a"},
                "b": {"image": "ghcr.io/o/x:2", "build": "b"},
            }
        }
        with self.assertRaisesRegex(plan.PlanError, "build it differently"):
            plan.parse_compose(compose, "compose.yaml")

    def test_shared_image_with_identical_build_is_built_once(self) -> None:
        compose = {
            "services": {
                "server-bootstrap": {"image": "ghcr.io/o/server:${TAG:-dev}", "build": {"context": "../apps/server"}},
                "server": {"image": "ghcr.io/o/server:${TAG:-dev}", "build": {"context": "../apps/server"}},
            }
        }
        services, _ = plan.parse_compose(compose, "deploy/compose.yaml")
        self.assertEqual([(svc.name, svc.image) for svc in services], [("server-bootstrap", "ghcr.io/o/server")])

    def test_inline_dockerfile_is_rejected(self) -> None:
        compose = {"services": {"a": {"image": "ghcr.io/o/a", "build": {"dockerfile_inline": "FROM scratch"}}}}
        with self.assertRaisesRegex(plan.PlanError, "dockerfile_inline"):
            plan.parse_compose(compose, "compose.yaml")

    def test_context_outside_repo_is_rejected(self) -> None:
        compose = {"services": {"a": {"image": "ghcr.io/o/a", "build": "../other"}}}
        with self.assertRaisesRegex(plan.PlanError, "escapes the repository"):
            plan.parse_compose(compose, "compose.yaml")

    def test_no_buildable_service_is_rejected(self) -> None:
        with self.assertRaisesRegex(plan.PlanError, "nothing to build"):
            plan.parse_compose({"services": {"db": {"image": "postgres:17"}}}, "compose.yaml")

    def test_watch_must_be_a_string_list(self) -> None:
        compose = {"services": {"a": {"image": "ghcr.io/o/a", "build": ".", "x-ci": {"watch": "packages/**"}}}}
        with self.assertRaisesRegex(plan.PlanError, "list of strings"):
            plan.parse_compose(compose, "compose.yaml")


class BuildPlanTest(unittest.TestCase):
    """Matrix selection per mode and change set."""

    def names(self, result: dict, key: str) -> list[str]:
        return [item["name"] for item in result[key]]

    def test_everything_when_changes_unknown(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "publish", SHA, None)
        self.assertEqual(self.names(result, "build"), ["server", "processor"])
        self.assertEqual(self.names(result, "checks"), ["server", "processor", "apps/scraper/shared"])
        self.assertEqual(result["carry"], [])
        self.assertEqual(result["sha-tag"], "sha-a1b2c3d")

    def test_only_changed_service_is_built_and_rest_carried(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "publish", SHA, ["apps/server/main.go"])
        self.assertEqual(self.names(result, "build"), ["server"])
        self.assertEqual(self.names(result, "checks"), ["server"])
        self.assertEqual(result["carry"], [{"name": "processor", "image": "ghcr.io/owner/thesis-processor"}])

    def test_extra_watch_triggers_rebuild(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "publish", SHA, ["packages/contracts/x.py"])
        self.assertEqual(self.names(result, "build"), ["processor"])

    def test_check_dir_change_runs_only_its_checks(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "check", SHA, ["apps/scraper/shared/a.py"])
        self.assertEqual(self.names(result, "checks"), ["apps/scraper/shared"])
        self.assertEqual(result["build"], [])

    def test_compose_change_rebuilds_everything(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "publish", SHA, ["deploy/compose.yaml"])
        self.assertEqual(self.names(result, "build"), ["server", "processor"])

    def test_workflow_change_rebuilds_everything(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "check", SHA, [".github/workflows/ci.yml"])
        self.assertEqual(self.names(result, "build"), ["server", "processor"])

    def test_scan_ignore_change_rebuilds_everything(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "publish", SHA, [".trivyignore.yaml"])
        self.assertEqual(self.names(result, "build"), ["server", "processor"])

    def test_unrelated_change_builds_nothing_and_carries_all(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "publish", SHA, ["docs/readme.md"])
        self.assertEqual(result["build"], [])
        self.assertEqual(result["checks"], [])
        self.assertEqual(self.names(result, "carry"), ["server", "processor"])

    def test_check_mode_never_carries(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "check", SHA, ["docs/readme.md"])
        self.assertEqual(result["carry"], [])

    def test_release_only_lists_all_images(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "release", SHA, None)
        self.assertEqual((result["checks"], result["build"], result["carry"]), ([], [], []))
        self.assertEqual(self.names(result, "all"), ["server", "processor"])

    def test_build_entry_carries_docker_inputs(self) -> None:
        result = plan.build_plan(monorepo(), "deploy/compose.yaml", "check", SHA, None)
        processor = result["build"][1]
        self.assertEqual(processor["context"], ".")
        self.assertEqual(processor["dockerfile"], "apps/processor/Dockerfile")
        self.assertEqual((processor["target"], processor["args"]), ("", ""))

    def test_short_sha_is_rejected(self) -> None:
        with self.assertRaises(plan.PlanError):
            plan.build_plan(monorepo(), "deploy/compose.yaml", "check", "abc", None)


def deploy_file() -> dict:
    """A deploy Compose file without build sections (what the platform runs)."""
    return {
        "services": {
            "api": {"image": "ghcr.io/owner/app-api:${TAG:-dev}", "restart": "unless-stopped"},
            "worker": {"image": "ghcr.io/owner/app-worker:${TAG:-dev}", "environment": {"X": "1"}},
            "db": {"image": "postgres:17"},
        },
    }


def build_overlay() -> dict:
    """The matching build overlay (compose.override.yaml)."""
    return {
        "x-ci": {"checks": ["libs/shared"]},
        "services": {
            "api": {
                "build": {"context": ".", "dockerfile": "services/api/Dockerfile"},
                "x-ci": {"watch": ["libs/shared/**"]},
            },
            "worker": {"build": {"context": "services/worker"}},
        },
    }


class BuildOverlayTest(unittest.TestCase):
    """A deploy file plus a build overlay behaves like one combined file."""

    def plan_with_overlay(self, overlay: dict, changed: list[str] | None = None, mode: str = "publish") -> dict:
        return plan.build_plan(
            deploy_file(),
            "compose.yaml",
            mode,
            SHA,
            changed,
            build_overlay=overlay,
            build_compose_file="compose.override.yaml",
            owned_registry="ghcr.io/owner/",
        )

    def test_overlay_supplies_build_and_x_ci(self) -> None:
        result = self.plan_with_overlay(build_overlay())
        builds = {item["name"]: item for item in result["build"]}
        self.assertEqual(builds["api"]["dockerfile"], "services/api/Dockerfile")
        self.assertEqual(builds["worker"]["dockerfile"], "services/worker/Dockerfile")
        self.assertIn("libs/shared", [item["name"] for item in result["checks"]])

    def test_overlay_watch_applies(self) -> None:
        result = self.plan_with_overlay(build_overlay(), changed=["libs/shared/greeting.go"])
        self.assertEqual([item["name"] for item in result["build"]], ["api"])
        self.assertEqual([item["name"] for item in result["carry"]], ["worker"])

    def test_overlay_change_rebuilds_everything(self) -> None:
        result = self.plan_with_overlay(build_overlay(), changed=["compose.override.yaml"])
        self.assertEqual({item["name"] for item in result["build"]}, {"api", "worker"})

    def test_owned_image_without_build_is_rejected(self) -> None:
        overlay = build_overlay()
        del overlay["services"]["worker"]
        with self.assertRaisesRegex(plan.PlanError, "worker"):
            self.plan_with_overlay(overlay)

    def test_external_service_is_exempt(self) -> None:
        overlay = build_overlay()
        overlay["services"]["worker"] = {"x-ci": {"external": True}}
        result = self.plan_with_overlay(overlay)
        self.assertEqual([item["name"] for item in result["all"]], ["api"])

    def test_owned_registry_match_ignores_case(self) -> None:
        overlay = build_overlay()
        del overlay["services"]["worker"]
        with self.assertRaises(plan.PlanError):
            plan.build_plan(deploy_file(), "compose.yaml", "publish", SHA, None,
                            build_overlay=overlay, build_compose_file="compose.override.yaml",
                            owned_registry="GHCR.IO/Owner/")

    def test_merge_keeps_base_keys_and_inputs_untouched(self) -> None:
        base, overlay = deploy_file(), build_overlay()
        merged = plan.merge_compose(base, overlay)
        self.assertEqual(merged["services"]["api"]["restart"], "unless-stopped")
        self.assertIn("build", merged["services"]["api"])
        self.assertNotIn("build", base["services"]["api"])

    def test_overlay_scalars_and_lists_replace_base(self) -> None:
        merged = plan.merge_compose({"a": {"b": [1, 2], "c": "x"}}, {"a": {"b": [3], "c": "y"}})
        self.assertEqual(merged, {"a": {"b": [3], "c": "y"}})


class MainTest(unittest.TestCase):
    """CLI behaviour, including the $GITHUB_OUTPUT format."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.compose_json = self.dir / "compose.json"
        self.compose_json.write_text(json.dumps(monorepo()), encoding="utf-8")
        self.output = self.dir / "github_output"
        self.output.write_text("", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, *extra: str) -> int:
        argv = [
            "--compose-json", str(self.compose_json),
            "--compose-file", "deploy/compose.yaml",
            "--event", "push",
            "--ref", "refs/heads/dev",
            "--sha", SHA,
            *extra,
        ]
        previous = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = str(self.output)
        try:
            return plan.main(argv)
        finally:
            if previous is None:
                del os.environ["GITHUB_OUTPUT"]
            else:
                os.environ["GITHUB_OUTPUT"] = previous

    def outputs(self) -> dict[str, str]:
        lines = self.output.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines)

    def test_writes_single_line_json_outputs(self) -> None:
        changed = self.dir / "changed.txt"
        changed.write_text("apps/server/main.go\n\n", encoding="utf-8")
        self.assertEqual(self.run_main("--changed-files", str(changed)), 0)
        out = self.outputs()
        self.assertEqual(out["mode"], "publish")
        self.assertEqual(out["sha-tag"], "sha-a1b2c3d")
        self.assertEqual([item["name"] for item in json.loads(out["build"])], ["server"])
        self.assertEqual([item["name"] for item in json.loads(out["carry"])], ["processor"])

    def test_build_overlay_flags(self) -> None:
        self.compose_json.write_text(json.dumps(deploy_file()), encoding="utf-8")
        overlay_json = self.dir / "compose-build.json"
        overlay_json.write_text(json.dumps(build_overlay()), encoding="utf-8")
        argv = [
            "--compose-json", str(self.compose_json),
            "--compose-file", "compose.yaml",
            "--event", "push",
            "--ref", "refs/heads/dev",
            "--sha", SHA,
            "--build-compose-json", str(overlay_json),
            "--build-compose-file", "compose.override.yaml",
            "--owned-registry", "ghcr.io/owner/",
        ]
        os.environ["GITHUB_OUTPUT"] = str(self.output)
        self.addCleanup(os.environ.pop, "GITHUB_OUTPUT", None)
        self.assertEqual(plan.main(argv), 0)
        self.assertEqual({item["name"] for item in json.loads(self.outputs()["build"])}, {"api", "worker"})

    def test_overlay_flags_must_come_together(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_main("--build-compose-file", "compose.override.yaml")

    def test_contract_error_returns_one(self) -> None:
        self.compose_json.write_text(json.dumps({"services": {}}), encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
