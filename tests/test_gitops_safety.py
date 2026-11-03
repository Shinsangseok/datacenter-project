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


# Frozen CD-09 preflight baseline, copied from the reviewed original-image
# rollback render. Do not derive live evidence from mutable validator contracts.
BASELINE_ROLLBACK_YAML = """
apiVersion: apps/v1
kind: Deployment
metadata:
  annotations:
    argocd.argoproj.io/sync-options: Prune=false,Delete=false
    deployment.datacenter/image-tag: datacenter-api:git-a647d80
    deployment.datacenter/source-revision: a647d80dd4f230b26a2fc249994e5d4c4254fd44
  name: datacenter-api
  namespace: datacenter-app-dev
spec:
  progressDeadlineSeconds: 600
  replicas: 1
  revisionHistoryLimit: 10
  selector:
    matchLabels:
      app: datacenter-api
  strategy:
    type: Recreate
  template:
    metadata:
      labels:
        app: datacenter-api
    spec:
      automountServiceAccountToken: false
      containers:
      - env:
        - name: DB_NAME
          valueFrom:
            configMapKeyRef:
              key: DB_NAME
              name: datacenter-api-config
        - name: MLFLOW_TRACKING_USERNAME
          valueFrom:
            secretKeyRef:
              key: MLFLOW_TRACKING_USERNAME
              name: datacenter-mlflow-client
        - name: MLFLOW_TRACKING_PASSWORD
          valueFrom:
            secretKeyRef:
              key: MLFLOW_TRACKING_PASSWORD
              name: datacenter-mlflow-client
        - name: DB_EXPECTED_HOST
          valueFrom:
            secretKeyRef:
              key: DB_EXPECTED_HOST
              name: datacenter-target-policy
        - name: DB_EXPECTED_USER
          valueFrom:
            secretKeyRef:
              key: DB_EXPECTED_USER
              name: datacenter-target-policy
        envFrom:
        - configMapRef:
            name: datacenter-api-config
        - secretRef:
            name: datacenter-api-secret
        image: docker.io/library/datacenter-api@sha256:3fd70c2d1e2ded77e496d3bbbb9f1a72730fb5e0d6c7ba3612c26d086deb079e
        imagePullPolicy: Never
        livenessProbe:
          failureThreshold: 3
          httpGet:
            path: /health
            port: 8000
            scheme: HTTP
          initialDelaySeconds: 10
          periodSeconds: 20
          successThreshold: 1
          timeoutSeconds: 1
        name: datacenter-api
        ports:
        - containerPort: 8000
          protocol: TCP
        readinessProbe:
          failureThreshold: 3
          httpGet:
            path: /ready
            port: 8000
            scheme: HTTP
          initialDelaySeconds: 5
          periodSeconds: 10
          successThreshold: 1
          timeoutSeconds: 1
        resources:
          limits:
            cpu: "1"
            memory: 1Gi
          requests:
            cpu: 100m
            memory: 256Mi
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop:
            - ALL
        terminationMessagePath: /dev/termination-log
        terminationMessagePolicy: File
        volumeMounts:
        - mountPath: /etc/datacenter/certs
          name: db-ca
          readOnly: true
      dnsPolicy: ClusterFirst
      nodeSelector:
        datacenter.io/workload: dev
        kubernetes.io/hostname: datacenter-k3s-worker-dev-01
      restartPolicy: Always
      schedulerName: default-scheduler
      securityContext:
        runAsGroup: 10001
        runAsNonRoot: true
        runAsUser: 10001
      terminationGracePeriodSeconds: 30
      tolerations:
      - effect: NoSchedule
        key: datacenter.io/dedicated
        operator: Equal
        value: dev
      volumes:
      - configMap:
          defaultMode: 420
          name: mlflow-rds-ca-75fggf29d8
        name: db-ca
"""


def original_rollback_fixture():
    return yaml.safe_load(BASELINE_ROLLBACK_YAML)


def uds_env_fixture():
    """Reviewed activation values, independent of policy constants."""
    return [
        {"name": "WRITE_FREE_INFERENCE_ENABLED", "value": "true"},
        {"name": "MLFLOW_ENV", "value": "dev"},
        {"name": "MODEL_SOURCE", "value": "registry"},
        {"name": "POD_NAMESPACE", "valueFrom": {
            "fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.namespace"}}},
    ]


def candidate_deployment_fixture():
    doc = original_rollback_fixture()
    doc["metadata"]["annotations"].update({
        "deployment.datacenter/image-tag": "ghcr.io/shinsangseok/datacenter-api:sha-001aeaeb93cc80312e0de1890ecb46c7b4baca16-41401445661-1",
        "deployment.datacenter/source-revision": "001aeaeb93cc80312e0de1890ecb46c7b4baca16",
    })
    container = doc["spec"]["template"]["spec"]["containers"][0]
    container.update(
        image="ghcr.io/shinsangseok/datacenter-api@sha256:5358f946c6c4bc853af2687577257821699870ef5e3e9a8d148c68cecadd773c",
        imagePullPolicy="IfNotPresent",
    )
    container["env"].extend(uds_env_fixture())
    return doc


def live_fixture():
    doc = original_rollback_fixture()
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

    def candidate_fixture(self):
        return candidate_deployment_fixture()

    def test_candidate_exact_published_provenance_passes(self):
        safety.validate_deployment(self.candidate_fixture())

    def test_candidate_missing_or_wrong_provenance_fails(self):
        for key in ("deployment.datacenter/image-tag", "deployment.datacenter/source-revision"):
            for value in (None, "unapproved"):
                with self.subTest(key=key, value=value):
                    doc = self.candidate_fixture()
                    if value is None:
                        del doc["metadata"]["annotations"][key]
                    else:
                        doc["metadata"]["annotations"][key] = value
                    self.assert_denied(safety.validate_deployment, doc)

    def test_candidate_provenance_does_not_authorize_pod_metadata(self):
        doc = self.candidate_fixture()
        doc["spec"]["template"]["metadata"]["annotations"] = dict(doc["metadata"]["annotations"])
        self.assert_denied(safety.validate_deployment, doc)

    def test_rollback_preserves_legacy_provenance_and_rejects_candidate(self):
        doc = safety.expected_deployment(True)
        expected = {
            "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
            "deployment.datacenter/image-tag": "datacenter-api:git-a647d80",
            "deployment.datacenter/source-revision": "a647d80dd4f230b26a2fc249994e5d4c4254fd44",
        }
        self.assertEqual(doc["metadata"]["annotations"], expected)
        safety.validate_deployment(doc, rollback=True)
        doc["metadata"]["annotations"] = self.candidate_fixture()["metadata"]["annotations"]
        self.assert_denied(safety.validate_deployment, doc, rollback=True)

    def test_candidate_and_rollback_spec_differ_only_at_image_and_policy(self):
        # Preserve the old comparison after removing only the four reviewed
        # candidate entries; existing DB/auth references must remain identical.
        candidate = self.candidate_fixture()
        rollback = original_rollback_fixture()
        env = candidate["spec"]["template"]["spec"]["containers"][0]["env"]
        baseline_env = rollback["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual(env, baseline_env + uds_env_fixture())
        del env[-4:]
        self.assertEqual(safety.differing_paths(rollback["spec"], candidate["spec"]), [
            "/template/spec/containers/0/image",
            "/template/spec/containers/0/imagePullPolicy",
        ])

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
                self.assertEqual(result["candidate_provenance"], "STATIC_CONTRACT_VERIFIED_EXECUTION_NOT_AUTHORIZED")
                self.assertEqual(result["actual_argo_apply"], "UNVERIFIED")
                self.assertIs(result["sync_authorized"], False)
                for call in run.call_args_list:
                    self.assertEqual(call.args[0][:2], ["/tools/kustomize", "build"])


    def candidate_source_fixture(self):
        candidate = self.candidate_fixture()
        return {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "datacenter-api"},
            "spec": {"template": {"spec": {"containers": [{
                "name": "datacenter-api",
                "env": copy.deepcopy(candidate["spec"]["template"]["spec"]["containers"][0]["env"]),
            }]}}},
        }

    def rollback_source_fixture(self):
        return {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "datacenter-api"},
            "spec": {"template": {"spec": {"containers": [{
                "name": "datacenter-api",
                "image": "docker.io/library/datacenter-api@sha256:3fd70c2d1e2ded77e496d3bbbb9f1a72730fb5e0d6c7ba3612c26d086deb079e",
                "imagePullPolicy": "Never",
                "env": [{"name": entry["name"], "$patch": "delete"} for entry in uds_env_fixture()],
            }]}}},
        }

    def write_scoped_source(self, root, doc, relative="overlays/aws/dev/deployment-contract.patch.yaml"):
        path = root / "gitops" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(doc))
        return path

    def assert_source_denied_before_render(self, doc, relative="overlays/aws/dev/deployment-contract.patch.yaml"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_scoped_source(root, doc, relative)
            with patch.object(safety, "render") as render:
                self.assert_denied(safety.check, root)
                render.assert_not_called()

    def test_reviewed_candidate_and_original_rollback_independent_fixtures_pass(self):
        safety.validate_deployment(self.candidate_fixture())
        safety.validate_deployment(original_rollback_fixture(), rollback=True)
        self.assertEqual(safety.expected_deployment(True), original_rollback_fixture())

    def test_original_live_contract_still_rejects_new_candidate_spec(self):
        raw = live_fixture()
        raw["spec"] = self.candidate_fixture()["spec"]
        self.assert_denied(safety.sanitize_live, raw)
        for env in uds_env_fixture():
            with self.subTest(env=env["name"]):
                raw = live_fixture()
                raw["spec"]["template"]["spec"]["containers"][0]["env"].append(env)
                self.assert_denied(safety.sanitize_live, raw)

    def test_original_live_comparison_remains_separate_from_candidate_contract(self):
        snapshot = safety.sanitize_live(live_fixture())
        self.assertEqual(safety.compare_live(snapshot, original_rollback_fixture())["spec_differences"], 0)
        self.assert_denied(safety.compare_live, snapshot, self.candidate_fixture())
        snapshot["deployment"]["spec"] = self.candidate_fixture()["spec"]
        self.assert_denied(safety.compare_live, snapshot, original_rollback_fixture())

    def test_previous_and_arbitrary_digest_or_tag_cannot_replace_new_candidate(self):
        for image in (
            "ghcr.io/shinsangseok/datacenter-api@sha256:930692df197f2579aa73aec466782808de4f94cb70add1f6c95713dbf6cfc113",
            "ghcr.io/shinsangseok/datacenter-api@sha256:" + "a" * 64,
            "ghcr.io/shinsangseok/datacenter-api:sha-001aeaeb93cc80312e0de1890ecb46c7b4baca16-41401445661-1",
            original_rollback_fixture()["spec"]["template"]["spec"]["containers"][0]["image"],
        ):
            with self.subTest(image=image):
                doc = self.candidate_fixture()
                doc["spec"]["template"]["spec"]["containers"][0]["image"] = image
                self.assert_denied(safety.validate_deployment, doc)

    def test_candidate_provenance_requires_exact_source_run_and_attempt(self):
        mutations = [
            ("deployment.datacenter/image-tag", "ghcr.io/shinsangseok/datacenter-api:sha-001aeaeb93cc80312e0de1890ecb46c7b4baca16-41401445662-1"),
            ("deployment.datacenter/image-tag", "ghcr.io/shinsangseok/datacenter-api:sha-001aeaeb93cc80312e0de1890ecb46c7b4baca16-41401445661-2"),
            ("deployment.datacenter/source-revision", "abd44eb3fe4ef248f2df2e0cf9bab050055d181a"),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                doc = self.candidate_fixture()
                doc["metadata"]["annotations"][key] = value
                self.assert_denied(safety.validate_deployment, doc)

    def test_candidate_rejects_missing_duplicate_and_extra_environment_entries(self):
        for index in range(4):
            for operation in ("remove", "duplicate"):
                with self.subTest(index=index, operation=operation):
                    doc = self.candidate_fixture()
                    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
                    if operation == "remove":
                        del env[5 + index]
                    else:
                        env.append(copy.deepcopy(env[5 + index]))
                    self.assert_denied(safety.validate_deployment, doc)
        for extra in ({"name": "UNREVIEWED", "value": "value"},
                      {"name": "UNREVIEWED", "valueFrom": {"secretKeyRef": {"name": "extra", "key": "extra"}}}):
            doc = self.candidate_fixture()
            doc["spec"]["template"]["spec"]["containers"][0]["env"].append(extra)
            self.assert_denied(safety.validate_deployment, doc)

    def test_candidate_wrong_activation_values_types_and_valuefrom_fail(self):
        values = {
            "WRITE_FREE_INFERENCE_ENABLED": ("false", "TRUE", "1", True, 1, None),
            "MLFLOW_ENV": ("prod", "DEV", "dev ", True),
            "MODEL_SOURCE": ("local", "Registry", "registry ", True),
        }
        for name, replacements in values.items():
            for value in replacements:
                with self.subTest(name=name, value=value):
                    doc = self.candidate_fixture()
                    entry = next(item for item in doc["spec"]["template"]["spec"]["containers"][0]["env"] if item["name"] == name)
                    entry["value"] = value
                    self.assert_denied(safety.validate_deployment, doc)
            doc = self.candidate_fixture()
            entry = next(item for item in doc["spec"]["template"]["spec"]["containers"][0]["env"] if item["name"] == name)
            entry["valueFrom"] = {"configMapKeyRef": {"name": "unreviewed", "key": name}}
            self.assert_denied(safety.validate_deployment, doc)

    def test_namespace_requires_exact_downward_api_reference(self):
        replacements = [
            {"value": "datacenter-app-dev"},
            {"valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
            {"valueFrom": {"fieldRef": {"apiVersion": "v2", "fieldPath": "metadata.namespace"}}},
            {"valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.name"}}},
            {"valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.namespace", "extra": "denied"}}},
            {"valueFrom": {"secretKeyRef": {"name": "unreviewed", "key": "namespace"}}},
            {"value": "datacenter-app-dev", "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.namespace"}}},
        ]
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                doc = self.candidate_fixture()
                doc["spec"]["template"]["spec"]["containers"][0]["env"][-1] = {"name": "POD_NAMESPACE", **replacement}
                self.assert_denied(safety.validate_deployment, doc)
                source = self.candidate_source_fixture()
                source["spec"]["template"]["spec"]["containers"][0]["env"][-1] = {"name": "POD_NAMESPACE", **replacement}
                self.assert_source_denied_before_render(source)

    def test_rollback_rejects_each_activation_entry_even_with_original_image(self):
        for entry in uds_env_fixture():
            with self.subTest(name=entry["name"]):
                doc = original_rollback_fixture()
                doc["spec"]["template"]["spec"]["containers"][0]["env"].append(entry)
                self.assert_denied(safety.validate_deployment, doc, rollback=True)

    def test_scoped_candidate_source_and_exact_rollback_removals_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_scoped_source(root, self.candidate_source_fixture())
            self.write_scoped_source(root, self.rollback_source_fixture(), "overlays/aws/dev-rollback/image.patch.yaml")
            self.assertEqual(safety.inspect_sources(root), 2)

    def test_candidate_activation_literals_cannot_escape_reviewed_source_path(self):
        for relative in ("base/aws/deployment.yaml", "overlays/aws/dev/other.patch.yaml",
                         "overlays/aws/prod/deployment-contract.patch.yaml",
                         "overlays/aws/dev-rollback/deployment-contract.patch.yaml",
                         "overlays/aws/dev/deployment-contract.patch.yml"):
            with self.subTest(relative=relative):
                self.assert_source_denied_before_render(self.candidate_source_fixture(), relative)

    def test_candidate_source_identity_cannot_widen_literal_exception(self):
        mutations = [
            lambda d: d.update(apiVersion="apps/v2"),
            lambda d: d.update(kind="Application"),
            lambda d: d["metadata"].update(name="another-api"),
            lambda d: d["metadata"].update(namespace="datacenter-app"),
            lambda d: d["spec"]["template"]["spec"]["containers"][0].update(name="sidecar"),
            lambda d: d["spec"]["template"]["spec"]["containers"].append({"name": "sidecar"}),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                doc = self.candidate_source_fixture()
                mutate(doc)
                self.assert_source_denied_before_render(doc)

    def test_source_duplicate_missing_or_extra_env_cannot_be_hidden_by_render(self):
        for index in range(9):
            with self.subTest(duplicate_index=index):
                doc = self.candidate_source_fixture()
                env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
                env.append(copy.deepcopy(env[index]))
                self.assert_source_denied_before_render(doc)
        for index in range(4):
            doc = self.candidate_source_fixture()
            del doc["spec"]["template"]["spec"]["containers"][0]["env"][5 + index]
            self.assert_source_denied_before_render(doc)
        for extra in ({"name": "EXTRA", "value": "dev"},
                      {"name": "EXTRA", "valueFrom": {"configMapKeyRef": {"name": "unreviewed", "key": "extra"}}}):
            doc = self.candidate_source_fixture()
            doc["spec"]["template"]["spec"]["containers"][0]["env"].append(extra)
            self.assert_source_denied_before_render(doc)

    def test_source_wrong_activation_value_or_both_value_forms_rejected_before_render(self):
        for index, value in ((5, "false"), (5, True), (5, 1), (6, "prod"), (7, "local")):
            doc = self.candidate_source_fixture()
            doc["spec"]["template"]["spec"]["containers"][0]["env"][index]["value"] = value
            self.assert_source_denied_before_render(doc)
        for index in range(5, 8):
            doc = self.candidate_source_fixture()
            doc["spec"]["template"]["spec"]["containers"][0]["env"][index]["valueFrom"] = {"secretKeyRef": {"name": "extra", "key": "extra"}}
            self.assert_source_denied_before_render(doc)

    def test_reserved_names_cannot_enter_from_other_sources_via_references(self):
        for entry in uds_env_fixture():
            with self.subTest(name=entry["name"]):
                doc = self.candidate_source_fixture()
                doc["spec"]["template"]["spec"]["containers"][0]["env"] = [{
                    "name": entry["name"], "valueFrom": {"configMapKeyRef": {"name": "unreviewed", "key": "hidden"}},
                }]
                self.assert_source_denied_before_render(doc, "overlays/aws/dev/hidden.patch.yaml")

    def test_source_literal_exception_does_not_allow_commands_or_hidden_literals(self):
        mutations = [
            lambda d: d["spec"]["template"]["spec"]["containers"][0].update(command=["sh"]),
            lambda d: d["spec"]["template"]["spec"]["containers"][0].update(args=["--workers", "2"]),
            lambda d: d["spec"]["template"]["spec"]["containers"][0].update(lifecycle={"postStart": {"exec": {"command": ["sh"]}}}),
            lambda d: d["spec"]["template"]["spec"].update(initContainers=[{"name": "hidden", "env": [{"name": "PASSWORD", "value": "DO_NOT_PERSIST_SECRET"}]}]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                doc = self.candidate_source_fixture()
                mutate(doc)
                self.assert_source_denied_before_render(doc)

    def test_rollback_source_requires_complete_exact_removal_directives(self):
        for index in range(4):
            for operation in ("remove", "duplicate", "replace", "extra_key"):
                with self.subTest(index=index, operation=operation):
                    doc = self.rollback_source_fixture()
                    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
                    if operation == "remove":
                        del env[index]
                    elif operation == "duplicate":
                        env.append(copy.deepcopy(env[index]))
                    elif operation == "replace":
                        env[index]["$patch"] = "replace"
                    else:
                        env[index]["value"] = "dev"
                    self.assert_source_denied_before_render(doc, "overlays/aws/dev-rollback/image.patch.yaml")
        doc = self.rollback_source_fixture()
        doc["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "DB_NAME", "$patch": "delete"})
        self.assert_source_denied_before_render(doc, "overlays/aws/dev-rollback/image.patch.yaml")

    def test_rollback_removal_exception_is_not_available_at_other_paths(self):
        self.assert_source_denied_before_render(self.rollback_source_fixture(), "overlays/aws/dev/other.patch.yaml")

    def test_candidate_auth_refs_and_existing_config_mounts_cannot_change(self):
        mutations = [
            lambda c: c["env"][0]["valueFrom"]["configMapKeyRef"].update(name="other-config"),
            lambda c: c["env"][1]["valueFrom"]["secretKeyRef"].update(name="prod-client"),
            lambda c: c["env"][3]["valueFrom"]["secretKeyRef"].update(key="OTHER_HOST"),
            lambda c: c["envFrom"][0]["configMapRef"].update(name="prod-config"),
            lambda c: c["envFrom"][1]["secretRef"].update(name="prod-secret"),
            lambda c: c["volumeMounts"][0].update(readOnly=False),
            lambda c: c["volumeMounts"].append({"mountPath": "/run/verifier", "name": "shared-socket"}),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                doc = self.candidate_fixture()
                mutate(doc["spec"]["template"]["spec"]["containers"][0])
                self.assert_denied(safety.validate_deployment, doc)

    def test_candidate_pod_privileges_process_and_tcp_exposure_cannot_expand(self):
        mutations = [
            lambda p: p["securityContext"].update(runAsUser=0),
            lambda p: p["securityContext"].update(runAsGroup=0),
            lambda p: p["securityContext"].update(runAsNonRoot=False),
            lambda p: p.update(hostNetwork=True),
            lambda p: p.update(serviceAccountName="admin"),
            lambda p: p.update(automountServiceAccountToken=True),
            lambda p: p["containers"][0]["securityContext"].update(privileged=True),
            lambda p: p["containers"][0]["securityContext"].update(allowPrivilegeEscalation=True),
            lambda p: p["containers"][0].update(command=["python", "-m", "uvicorn"]),
            lambda p: p["containers"][0].update(args=["--workers", "2"]),
            lambda p: p["containers"][0]["ports"].append({"containerPort": 9000, "protocol": "TCP"}),
            lambda p: p["containers"].append({"name": "uds-proxy", "image": "proxy:latest"}),
            lambda p: p.update(initContainers=[{"name": "prepare", "image": "helper:latest"}]),
            lambda p: p["volumes"].append({"name": "shared-socket", "emptyDir": {}}),
            lambda p: p["volumes"][0]["configMap"].update(name="other-ca"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                doc = self.candidate_fixture()
                mutate(doc["spec"]["template"]["spec"])
                self.assert_denied(safety.validate_deployment, doc)

    def test_candidate_identity_worker_placement_and_availability_contract_remain_exact(self):
        mutations = [
            lambda d: d["metadata"].update(namespace="datacenter-app"),
            lambda d: d["metadata"].update(name="another-api"),
            lambda d: d["spec"].update(replicas=2),
            lambda d: d["spec"]["strategy"].update(type="RollingUpdate"),
            lambda d: d["spec"]["template"]["metadata"]["labels"].update(app="other"),
            lambda d: d["spec"]["template"]["spec"]["nodeSelector"].update({"datacenter.io/workload": "prod"}),
            lambda d: d["spec"]["template"]["spec"]["nodeSelector"].update({"kubernetes.io/hostname": "other-worker"}),
            lambda d: d["spec"]["template"]["spec"]["tolerations"][0].update(value="prod"),
            lambda d: d["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"].update(path="/health"),
            lambda d: d["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"].update(cpu="1"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                doc = self.candidate_fixture()
                mutate(doc)
                self.assert_denied(safety.validate_deployment, doc)

    def test_new_resource_kinds_remain_forbidden_in_source_and_render(self):
        for kind in ("Secret", "ConfigMap", "PersistentVolumeClaim", "Service", "Ingress", "Gateway", "HTTPRoute", "NetworkPolicy", "Role", "RoleBinding"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_source(root, yaml.safe_dump({"kind": kind, "metadata": {"name": "unreviewed"}}))
                self.assert_denied(safety.inspect_sources, root)
                self.write_source(root, "kind: Deployment\n")
                with patch.object(safety, "render", side_effect=[
                    [self.candidate_fixture(), {"kind": kind}], [original_rollback_fixture()], controls(),
                ]):
                    self.assert_denied(safety.check, root)

    def test_candidate_gate_keeps_argo_automation_and_prod_destination_denied(self):
        for key in ("enabled", "prune", "selfHeal"):
            docs = controls()
            docs[1]["spec"]["syncPolicy"]["automated"][key] = True
            self.assert_denied(safety.validate_controls, docs)
        docs = controls()
        docs[1]["spec"]["destination"]["namespace"] = "datacenter-app"
        self.assert_denied(safety.validate_controls, docs)
        docs = controls()
        docs[1]["spec"]["source"]["path"] = "gitops/overlays/aws/dev"
        self.assert_denied(safety.validate_controls, docs)

    def test_independent_candidate_gate_accepts_original_live_without_authorizing_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_scoped_source(root, self.candidate_source_fixture())
            self.write_scoped_source(root, self.rollback_source_fixture(), "overlays/aws/dev-rollback/image.patch.yaml")
            snapshot = root / "live-snapshot.json"
            snapshot.write_text(json.dumps(safety.sanitize_live(live_fixture())))
            with patch.object(safety, "render", side_effect=[
                [self.candidate_fixture()], [original_rollback_fixture()], controls(),
            ]):
                # Live evidence must remain outside the repository.
                with tempfile.TemporaryDirectory() as evidence_directory:
                    outside_snapshot = Path(evidence_directory) / "live-snapshot.json"
                    outside_snapshot.write_text(snapshot.read_text())
                    result = safety.check(root, str(outside_snapshot))
            self.assertEqual(result["static"], "PASS")
            self.assertEqual(result["live_comparison"]["spec_differences"], 0)
            self.assertIs(result["sync_authorized"], False)
            self.assertEqual(result["actual_argo_apply"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
