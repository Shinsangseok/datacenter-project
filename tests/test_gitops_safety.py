"""Synthetic safety regressions; no cluster connection or credentials."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gitops_safety.py"
SPEC = importlib.util.spec_from_file_location("gitops_safety", SCRIPT)
safety = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safety)


def controls():
    """Independent synthetic Argo inputs, not the validator's expectations."""
    return [
        {"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
         "metadata": {"name": "datacenter-dev", "namespace": "argocd"},
         "spec": {"description": "Manual approval required; initial sync blocked",
                  "sourceRepos": [safety.REPO_URL],
                  "destinations": [{"server": safety.SERVER, "namespace": safety.NS}],
                  "clusterResourceWhitelist": [],
                  "namespaceResourceWhitelist": [{"group": "apps", "kind": "Deployment"}],
                  "syncWindows": [{"kind": "deny", "schedule": "* * * * *", "duration": "24h",
                                   "applications": ["*"], "manualSync": False}]}},
        {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
         "metadata": {"name": "datacenter-api-dev", "namespace": "argocd"},
         "spec": {"project": "datacenter-dev",
                  "source": {"repoURL": safety.REPO_URL, "targetRevision": "NOT_APPROVED_CD01B2",
                             "path": "gitops/overlays/aws/dev-rollback"},
                  "destination": {"server": safety.SERVER, "namespace": safety.NS},
                  "syncPolicy": {"automated": {"enabled": False, "prune": False, "selfHeal": False, "allowEmpty": False},
                                 "syncOptions": ["FailOnSharedResource=true"]}}},
    ]


def live_fixture():
    doc = safety.expected_deployment(rollback=True)
    doc["metadata"].update(uid="f3248303-c1f6-4489-9247-afcd2abbea54", generation=11)
    doc["metadata"]["annotations"].pop(safety.SYNC_KEY)
    doc["metadata"]["annotations"][safety.REVISION_KEY] = "6"
    doc["metadata"]["annotations"][safety.LAST_APPLIED] = '{"password":"DO_NOT_PERSIST_SECRET"}'
    doc["metadata"]["managedFields"] = [{
        "manager": "kubectl-client-side-apply", "operation": "Update", "apiVersion": "apps/v1",
        "fieldsType": "FieldsV1", "time": "2026-09-28T00:58:38Z",
        "fieldsV1": {"f:metadata": {"f:annotations": {"f:" + key: {} for key in safety.PROTECTED}}, "f:spec": {}},
    }]
    return doc


class GitOpsSafetyTests(unittest.TestCase):
    def assert_denied(self, fn, *args, **kwargs):
        with self.assertRaises(safety.SafetyError):
            fn(*args, **kwargs)

    def test_safe_deployments_and_controls_pass(self):
        safety.validate_deployment(safety.expected_deployment())
        safety.validate_deployment(safety.expected_deployment(True), rollback=True)
        self.assertEqual(safety.validate_controls(controls()), "BLOCKED_REPOSITORY_TEMPLATE")

    def test_duplicate_yaml_keys_and_non_string_keys_rejected(self):
        for document in ("kind: Deployment\nkind: Secret\n", "metadata:\n  annotations:\n    a: 1\n    a: 2\n", "true: value\n"):
            with self.subTest(document=document):
                self.assert_denied(safety.load_yaml, document)

    def test_protected_annotation_removal_and_change_fail(self):
        for key in safety.PROTECTED:
            for remove in (True, False):
                doc = safety.expected_deployment(True)
                if remove:
                    del doc["metadata"]["annotations"][key]
                else:
                    doc["metadata"]["annotations"][key] = "unapproved"
                self.assert_denied(safety.validate_deployment, doc, rollback=True)

    def test_metadata_patch_does_not_authorize_pod_annotation(self):
        doc = safety.expected_deployment(True)
        safety.validate_deployment(doc, rollback=True)
        doc["spec"]["template"]["metadata"]["annotations"] = dict(safety.PROTECTED)
        self.assert_denied(safety.validate_deployment, doc, rollback=True)

    def test_unapproved_images_and_policies_fail(self):
        for rollback in (False, True):
            for field, value in (("image", "ghcr.io/attacker/api@sha256:" + "a" * 64), ("imagePullPolicy", "Always")):
                doc = safety.expected_deployment(rollback)
                doc["spec"]["template"]["spec"]["containers"][0][field] = value
                self.assert_denied(safety.validate_deployment, doc, rollback=rollback)

    def test_prod_namespace_node_and_taint_fail(self):
        mutations = [
            lambda d: d["metadata"].update(namespace="datacenter-app-prod"),
            lambda d: d["spec"]["template"]["spec"]["nodeSelector"].update({"datacenter.io/workload": "prod"}),
            lambda d: d["spec"]["template"]["spec"]["tolerations"][0].update(value="prod"),
        ]
        for mutate in mutations:
            doc = safety.expected_deployment(True)
            mutate(doc)
            self.assert_denied(safety.validate_deployment, doc, rollback=True)

    def test_replicas_strategy_probe_resources_refs_and_volumes_fail(self):
        mutations = [
            lambda d: d["spec"].update(replicas=2),
            lambda d: d["spec"]["strategy"].update(type="RollingUpdate"),
            lambda d: d["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"].update(path="/"),
            lambda d: d["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"].update(memory="8Gi"),
            lambda d: d["spec"]["template"]["spec"]["containers"][0]["envFrom"][1]["secretRef"].update(name="prod-secret"),
            lambda d: d["spec"]["template"]["spec"]["volumes"][0].update(hostPath={"path": "/"}),
            lambda d: d["spec"]["template"]["spec"].update(serviceAccountName="admin"),
        ]
        for mutate in mutations:
            doc = safety.expected_deployment(True)
            mutate(doc)
            self.assert_denied(safety.validate_deployment, doc, rollback=True)

    def test_inline_secrets_args_and_hooks_fail(self):
        for extra in ({"env": [{"name": "PASSWORD", "value": "DO_NOT_PERSIST_SECRET"}]},
                      {"args": ["--password=DO_NOT_PERSIST_SECRET"]}, {"command": ["sh", "-c", "secret"]},
                      {"lifecycle": {"postStart": {"exec": {"command": ["secret"]}}}}):
            doc = safety.expected_deployment(True)
            doc["spec"]["template"]["spec"]["containers"][0].update(extra)
            self.assert_denied(safety.validate_deployment, doc, rollback=True)

    def test_automated_sync_true_missing_and_string_false_fail(self):
        for key in ("enabled", "prune", "selfHeal", "allowEmpty"):
            for value in (True, "false", 0):
                docs = controls()
                docs[1]["spec"]["syncPolicy"]["automated"][key] = value
                self.assert_denied(safety.validate_controls, docs)
        docs = controls()
        del docs[1]["spec"]["syncPolicy"]["automated"]
        self.assert_denied(safety.validate_controls, docs)

    def test_dangerous_sync_options_and_bypasses_fail(self):
        for option in ("Force=true", "Replace=true", "ServerSideApply=true", "Prune=true", "Validate=false"):
            docs = controls()
            docs[1]["spec"]["syncPolicy"]["syncOptions"].append(option)
            self.assert_denied(safety.validate_controls, docs)
        for field, value in (("ignoreDifferences", []), ("sources", [])):
            docs = controls()
            docs[1]["spec"][field] = value
            self.assert_denied(safety.validate_controls, docs)
        for field in ("operation", "finalizers"):
            docs = controls()
            if field == "operation":
                docs[1][field] = {"sync": {}}
            else:
                docs[1]["metadata"][field] = ["resources-finalizer.argocd.argoproj.io"]
            self.assert_denied(safety.validate_controls, docs)

    def test_deny_window_and_project_scope_cannot_widen(self):
        for mutate in (
            lambda d: d[0]["spec"]["syncWindows"][0].update(manualSync=True),
            lambda d: d[0]["spec"]["syncWindows"][0].update(kind="allow"),
            lambda d: d[0]["spec"]["destinations"][0].update(namespace="*"),
            lambda d: d[0]["spec"]["namespaceResourceWhitelist"].append({"group": "", "kind": "Secret"}),
            lambda d: d[0]["spec"].update(roles=[{"name": "admin"}]),
        ):
            docs = controls()
            mutate(docs)
            self.assert_denied(safety.validate_controls, docs)

    def test_revision_and_repository_constraints(self):
        for revision in ("main", "HEAD", "v1.0", "a" * 39, "A" * 40, "NOT_APPROVED_CD01B3"):
            docs = controls()
            docs[1]["spec"]["source"]["targetRevision"] = revision
            self.assert_denied(safety.validate_controls, docs)
        self.assert_denied(safety.validate_controls, controls(), runtime=True)
        docs = controls()
        docs[1]["spec"]["source"]["targetRevision"] = "a" * 40
        self.assertEqual(safety.validate_controls(docs, runtime=True), "IMMUTABLE_COMMIT")
        docs[1]["spec"]["source"]["repoURL"] = "https://github.com/unapproved/repo.git"
        self.assert_denied(safety.validate_controls, docs)

    def test_candidate_cannot_silently_inherit_legacy_provenance(self):
        doc = safety.expected_deployment()
        doc["metadata"]["annotations"].update(safety.PROTECTED)
        self.assert_denied(safety.validate_deployment, doc)

    def write_source(self, root, content):
        path = root / "gitops" / "kustomization.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_remote_paths_escape_and_unsafe_generators_rejected_before_render(self):
        for source in ("https://github.com/a/b", "git@github.com:a/b", "../../outside", "/etc/passwd"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_source(root, yaml.safe_dump({"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": [source]}))
                with patch.object(safety, "render") as render:
                    self.assert_denied(safety.check, root)
                    render.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_source(root, "kind: Kustomization\nsecretGenerator: []\n")
            self.assert_denied(safety.inspect_sources, root)

    def test_source_literal_secret_is_rejected_even_if_render_would_remove_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_source(root, "kind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n      - env:\n        - name: PASSWORD\n          value: DO_NOT_PERSIST_SECRET\n")
            with patch.object(safety, "render") as render:
                self.assert_denied(safety.check, root)
                render.assert_not_called()

    def test_extensionless_kustomization_cannot_hide_remote_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_source(root, "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: [https://example.com/remote]\n")
            source.rename(source.with_name("Kustomization"))
            with patch.object(safety, "render") as render:
                self.assert_denied(safety.check, root)
                render.assert_not_called()

    def test_symlink_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gitops").mkdir()
            target = root / "outside.yaml"
            target.write_text("kind: Deployment\n")
            (root / "gitops" / "deployment.yaml").symlink_to(target)
            self.assert_denied(safety.inspect_sources, root)

    def test_extra_resource_kinds_and_overlay_count_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kind in ("Secret", "Namespace", "Service", "PersistentVolumeClaim"):
                self.write_source(root, "kind: " + kind + "\n")
                self.assert_denied(safety.inspect_sources, root)
            self.write_source(root, "kind: Deployment\n")
            with patch.object(safety, "render", side_effect=[[safety.expected_deployment(), {"kind": "Service"}], [safety.expected_deployment(True)], controls()]):
                self.assert_denied(safety.check, root)
        self.assert_denied(safety.validate_controls, controls() + [{"kind": "Secret"}])

    def test_snapshot_sanitizes_last_applied_and_managed_fields(self):
        result = safety.sanitize_live(live_fixture())
        encoded = json.dumps(result)
        self.assertNotIn("DO_NOT_PERSIST_SECRET", encoded)
        self.assertNotIn("fieldsV1", encoded)
        summary = result["deployment"]["managedFieldsSummary"][0]
        self.assertEqual(summary["protected_annotation_keys"], sorted(safety.PROTECTED))
        comparison = safety.compare_live(result, safety.expected_deployment(True))
        self.assertEqual(comparison["spec_differences"], 0)
        self.assertIn(safety.SYNC_KEY, comparison["expected_metadata_additions"])
        self.assertFalse(result["sync_authorized"])
        self.assertEqual(result["actual_argo_apply"], "UNVERIFIED")

    def test_snapshot_without_managed_fields_is_rejected(self):
        raw = live_fixture()
        del raw["metadata"]["managedFields"]
        self.assert_denied(safety.sanitize_live, raw)

    def test_unknown_annotations_are_hashed_and_require_review(self):
        raw = live_fixture()
        raw["metadata"]["annotations"]["example.com/private"] = "DO_NOT_PERSIST_SECRET"
        result = safety.sanitize_live(raw)
        self.assertNotIn("DO_NOT_PERSIST_SECRET", json.dumps(result))
        self.assert_denied(safety.compare_live, result, safety.expected_deployment(True))

    def test_live_fixture_drift_in_spec_template_and_annotations_fails(self):
        for mutate in (
            lambda d: d["deployment"]["spec"].update(replicas=2),
            lambda d: d["deployment"]["spec"]["template"]["metadata"].update(annotations={"changed": "yes"}),
            lambda d: d["deployment"]["metadata"]["annotations"].pop(next(iter(safety.PROTECTED))),
            lambda d: d["deployment"]["metadata"].update(namespace="datacenter-app-prod"),
            lambda d: d.update(actual_argo_apply="VERIFIED"),
        ):
            snap = safety.sanitize_live(live_fixture())
            mutate(snap)
            self.assert_denied(safety.compare_live, snap, safety.expected_deployment(True))

    def test_unsafe_live_spec_is_never_persisted_or_printed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            raw = live_fixture()
            raw["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "PASSWORD", "value": "DO_NOT_PERSIST_SECRET"})
            with patch.object(safety, "run_readonly", return_value=json.dumps(raw)), patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(safety.main(["snapshot", "--output", str(target)]), 1)
                self.assertNotIn("DO_NOT_PERSIST_SECRET", output.getvalue())
            self.assertFalse(target.exists())

    def test_snapshot_readonly_command_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            with patch.object(safety, "run_readonly", return_value=json.dumps(live_fixture())) as command:
                safety.snapshot_to_file(str(target))
                command.assert_called_once_with(["kubectl", "get", "deployment", "datacenter-api", "--namespace", "datacenter-app-dev", "--output", "json", "--show-managed-fields=true"])
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assert_denied(safety.snapshot_to_file, str(target))
            self.assertNotIn("DO_NOT_PERSIST_SECRET", target.read_text())

    def test_snapshot_rejects_repository_path_before_query(self):
        target = SCRIPT.parent.parent / "DO_NOT_CREATE_snapshot.json"
        with patch.object(safety, "run_readonly") as command:
            self.assert_denied(safety.snapshot_to_file, str(target))
            command.assert_not_called()
        self.assertFalse(target.exists())

    def test_snapshot_rejects_other_git_checkouts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").write_text("gitdir: /synthetic/path")
            with patch.object(safety, "run_readonly") as command:
                self.assert_denied(safety.snapshot_to_file, str(root / "snapshot.json"))
                command.assert_not_called()

    def test_cli_outputs_json_and_nonzero_failure(self):
        with patch.object(safety, "check", side_effect=safety.SafetyError("Synthetic policy failure")), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(safety.main(["check", "--repo", "."]), 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "FAIL")

    def test_static_gate_never_invokes_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_source(root, "kind: Deployment\n")
            with patch.object(safety.shutil, "which", return_value="/tools/kustomize"), patch.object(safety, "run_readonly", side_effect=[yaml.safe_dump_all([safety.expected_deployment()]), yaml.safe_dump_all([safety.expected_deployment(True)]), yaml.safe_dump_all(controls())]) as run:
                result = safety.check(root)
                self.assertEqual(result["static"], "PASS")
                self.assertEqual(result["live_comparison"], "NOT_RUN")
                for call in run.call_args_list:
                    self.assertEqual(call.args[0][:2], ["/tools/kustomize", "build"])


if __name__ == "__main__":
    unittest.main()
