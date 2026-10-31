#!/usr/bin/env python3
"""Fail-closed CD-01B-3 static and local, read-only live evidence gates.

No command here writes Kubernetes resources. A successful static/live comparison
never establishes Argo's actual apply result or authorizes synchronization.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import yaml

NS = "datacenter-app-dev"
NAME = "datacenter-api"
REPO_URL = "https://github.com/Shinsangseok/datacenter-project.git"
SERVER = "https://kubernetes.default.svc"
ROLLBACK = "docker.io/library/datacenter-api@sha256:3fd70c2d1e2ded77e496d3bbbb9f1a72730fb5e0d6c7ba3612c26d086deb079e"
CANDIDATE = "ghcr.io/shinsangseok/datacenter-api@sha256:930692df197f2579aa73aec466782808de4f94cb70add1f6c95713dbf6cfc113"
PROTECTED = {
    "deployment.datacenter/image-tag": "datacenter-api:git-a647d80",
    "deployment.datacenter/source-revision": "a647d80dd4f230b26a2fc249994e5d4c4254fd44",
}
CANDIDATE_PROVENANCE = {
    "deployment.datacenter/image-tag": "ghcr.io/shinsangseok/datacenter-api:sha-abd44eb3fe4ef248f2df2e0cf9bab050055d181a-40211788456-1",
    "deployment.datacenter/source-revision": "abd44eb3fe4ef248f2df2e0cf9bab050055d181a",
}
SYNC_KEY = "argocd.argoproj.io/sync-options"
SYNC_VALUE = "Prune=false,Delete=false"
TRACKING_KEY = "argocd.argoproj.io/tracking-id"
TRACKING_VALUE = "datacenter-api-dev:apps/Deployment:datacenter-app-dev/datacenter-api"
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
REVISION_KEY = "deployment.kubernetes.io/revision"
PATHS = ("gitops/overlays/aws/dev", "gitops/overlays/aws/dev-rollback", "gitops/argocd/dev")
SCHEMA = "cd01b3-sanitized-deployment-v1"


class SafetyError(Exception):
    """Errors contain field paths or fixed messages, never live field values."""


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise SafetyError("YAML mapping has duplicate or non-string keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_yaml(text):
    try:
        result = list(yaml.load_all(text, Loader=UniqueLoader))
    except yaml.YAMLError:
        raise SafetyError("Invalid YAML syntax") from None
    require(result and all(isinstance(item, dict) for item in result), "YAML documents must be mappings")
    return result


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def differing_paths(left, right, path=""):
    if type(left) is not type(right):
        return [path or "/"]
    if isinstance(left, dict):
        result = []
        for key in sorted(set(left) | set(right)):
            child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            result.extend([child] if key not in left or key not in right else differing_paths(left[key], right[key], child))
        return result
    if isinstance(left, list):
        if len(left) != len(right):
            return [path]
        return [p for i, pair in enumerate(zip(left, right)) for p in differing_paths(*pair, path + "/" + str(i))]
    return [] if left == right else [path]


def exact(actual, expected, label):
    paths = differing_paths(actual, expected)
    require(not paths, label + " differs at " + ", ".join(paths[:20]))


def expected_spec(image=CANDIDATE, policy="IfNotPresent"):
    def probe(path, delay, period):
        return {"failureThreshold": 3, "httpGet": {"path": path, "port": 8000, "scheme": "HTTP"},
                "initialDelaySeconds": delay, "periodSeconds": period, "successThreshold": 1, "timeoutSeconds": 1}
    env = [{"name": "DB_NAME", "valueFrom": {"configMapKeyRef": {"key": "DB_NAME", "name": "datacenter-api-config"}}}]
    for key, name in (("MLFLOW_TRACKING_USERNAME", "datacenter-mlflow-client"),
                      ("MLFLOW_TRACKING_PASSWORD", "datacenter-mlflow-client"),
                      ("DB_EXPECTED_HOST", "datacenter-target-policy"), ("DB_EXPECTED_USER", "datacenter-target-policy")):
        env.append({"name": key, "valueFrom": {"secretKeyRef": {"key": key, "name": name}}})
    container = {
        "name": NAME, "image": image, "imagePullPolicy": policy, "env": env,
        "envFrom": [{"configMapRef": {"name": "datacenter-api-config"}}, {"secretRef": {"name": "datacenter-api-secret"}}],
        "livenessProbe": probe("/health", 10, 20), "readinessProbe": probe("/ready", 5, 10),
        "ports": [{"containerPort": 8000, "protocol": "TCP"}],
        "resources": {"limits": {"cpu": "1", "memory": "1Gi"}, "requests": {"cpu": "100m", "memory": "256Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File",
        "volumeMounts": [{"mountPath": "/etc/datacenter/certs", "name": "db-ca", "readOnly": True}],
    }
    return {
        "progressDeadlineSeconds": 600, "replicas": 1, "revisionHistoryLimit": 10,
        "selector": {"matchLabels": {"app": NAME}}, "strategy": {"type": "Recreate"},
        "template": {"metadata": {"labels": {"app": NAME}}, "spec": {
            "automountServiceAccountToken": False, "containers": [container], "dnsPolicy": "ClusterFirst",
            "restartPolicy": "Always", "schedulerName": "default-scheduler",
            "securityContext": {"runAsGroup": 10001, "runAsNonRoot": True, "runAsUser": 10001},
            "terminationGracePeriodSeconds": 30,
            "nodeSelector": {"datacenter.io/workload": "dev", "kubernetes.io/hostname": "datacenter-k3s-worker-dev-01"},
            "tolerations": [{"effect": "NoSchedule", "key": "datacenter.io/dedicated", "operator": "Equal", "value": "dev"}],
            "volumes": [{"configMap": {"defaultMode": 420, "name": "mlflow-rds-ca-75fggf29d8"}, "name": "db-ca"}],
        }},
    }


def expected_deployment(rollback=False):
    annotations = {SYNC_KEY: SYNC_VALUE, **(PROTECTED if rollback else CANDIDATE_PROVENANCE)}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": NAME, "namespace": NS, "annotations": annotations},
            "spec": expected_spec(ROLLBACK, "Never") if rollback else expected_spec()}


def validate_deployment(doc, rollback=False):
    # Exact contracts deny new containers, hooks, literal env/args, references,
    # service accounts, Pod annotations and privilege changes as well as images.
    exact(doc, expected_deployment(rollback), "Rollback Deployment" if rollback else "Candidate Deployment")


def validate_controls(docs, runtime=False):
    require(len(docs) == 2, "Argo render must contain exactly AppProject and Application")
    by_kind = {doc.get("kind"): doc for doc in docs}
    require(set(by_kind) == {"AppProject", "Application"}, "Unexpected Argo resource kinds")
    project = by_kind["AppProject"]
    expected_project = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
        "metadata": {"name": "datacenter-dev", "namespace": "argocd"},
        "spec": {"description": "Manual approval required; initial sync blocked", "sourceRepos": [REPO_URL],
                 "destinations": [{"server": SERVER, "namespace": NS}], "clusterResourceWhitelist": [],
                 "namespaceResourceWhitelist": [{"group": "apps", "kind": "Deployment"}],
                 "syncWindows": [{"kind": "deny", "schedule": "* * * * *", "duration": "24h", "applications": ["*"], "manualSync": False}]},
    }
    exact(project, expected_project, "AppProject")
    app = by_kind["Application"]
    revision = app.get("spec", {}).get("source", {}).get("targetRevision")
    pinned = isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision)
    require(pinned or (not runtime and revision == "NOT_APPROVED_CD01B2"), "Revision must be immutable; placeholder is repository-only")
    expected_app = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": "datacenter-api-dev", "namespace": "argocd"},
        "spec": {"project": "datacenter-dev", "source": {"repoURL": REPO_URL, "targetRevision": revision, "path": PATHS[1]},
                 "destination": {"server": SERVER, "namespace": NS},
                 "syncPolicy": {"automated": {"enabled": False, "prune": False, "selfHeal": False, "allowEmpty": False},
                                "syncOptions": ["FailOnSharedResource=true"]}},
    }
    exact(app, expected_app, "Application")
    return "IMMUTABLE_COMMIT" if pinned else "BLOCKED_REPOSITORY_TEMPLATE"


def inside(path, root):
    return path == root or root in path.parents


def reject_inline_values(value):
    if isinstance(value, dict):
        require(not ({"command", "args"} & set(value)), "Source command/args are not approved")
        if "env" in value:
            require(isinstance(value["env"], list) and all(isinstance(entry, dict) and "value" not in entry for entry in value["env"]),
                    "Source literal environment values are forbidden")
        for child in value.values():
            reject_inline_values(child)
    elif isinstance(value, list):
        for child in value:
            reject_inline_values(child)


def inspect_sources(repo):
    root = (repo / "gitops").resolve()
    require(inside(root, repo) and root.is_dir(), "GitOps directory must be inside repository")
    files = sorted(set(root.rglob("*.yaml")) | set(root.rglob("*.yml")) | set(root.rglob("Kustomization")))
    require(bool(files), "No GitOps YAML found")
    for path in files:
        require(not path.is_symlink() and inside(path.resolve(), root), "GitOps symlinks are forbidden")
        docs = load_yaml(path.read_text())
        require(len(docs) == 1, "Source YAML must contain one document")
        doc = docs[0]
        require(doc.get("kind") in {"Kustomization", "Deployment", "Application", "AppProject"}, "Unexpected source resource kind")
        if doc.get("kind") != "Kustomization":
            reject_inline_values(doc)
            continue
        require(set(doc) <= {"apiVersion", "kind", "resources", "namespace", "patches"}, "Unsafe Kustomization transformer or generator")
        require(doc.get("apiVersion") == "kustomize.config.k8s.io/v1beta1", "Unexpected Kustomization API")
        require("namespace" not in doc or doc["namespace"] == NS, "Kustomization namespace must be DEV")
        resources = doc.get("resources", [])
        patches = doc.get("patches", [])
        require(isinstance(resources, list) and isinstance(patches, list), "Kustomize references must be lists")
        require(all(isinstance(p, dict) and set(p) == {"path"} for p in patches), "Only local file patches are allowed")
        for ref in resources + [p["path"] for p in patches]:
            require(isinstance(ref, str) and re.fullmatch(r"[A-Za-z0-9_./-]+", ref) and not Path(ref).is_absolute(), "Remote or absolute Kustomize reference denied")
            target = path.parent / ref
            require(inside(target.resolve(), root) and target.exists(), "Kustomize reference escapes GitOps or is missing")
            require(target.is_dir() or target.suffix in {".yaml", ".yml"}, "Resource and patch files must be validated YAML")
            require(not any(part.is_symlink() for part in [target, *target.parents] if inside(part, root)), "Kustomize symlink reference denied")
    return len(files)


def run_readonly(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SafetyError("Read-only tool unavailable or timed out") from None
    require(result.returncode == 0, "Read-only tool failed; raw output suppressed")
    return result.stdout


def render(repo, relative):
    executable = shutil.which("kustomize")
    command = [executable, "build"] if executable else ["kubectl", "kustomize"]
    return load_yaml(run_readonly(command + [str(repo / relative)]))


def evidence_path(path):
    require(path.is_absolute(), "Evidence path must be absolute")
    require(not any((parent / ".git").exists() for parent in [path.parent, *path.parents]),
            "Live evidence must be outside every Git checkout")


def check(repo, live_snapshot=None):
    repo = Path(repo).resolve()
    count = inspect_sources(repo)
    dev, rollback, argo = [render(repo, path) for path in PATHS]
    require(len(dev) == len(rollback) == 1, "Each overlay must render exactly one DEV Deployment")
    validate_deployment(dev[0])
    validate_deployment(rollback[0], rollback=True)
    comparable = copy.deepcopy(rollback[0]["spec"])
    comparable["template"]["spec"]["containers"][0].update(image=CANDIDATE, imagePullPolicy="IfNotPresent")
    exact(comparable, dev[0]["spec"], "Candidate/rollback spec outside image and pull policy")
    revision = validate_controls(argo)
    result = {"status": "PASS", "static": "PASS", "yaml_files": count, "revision": revision,
              "live_comparison": "NOT_RUN", "actual_argo_apply": "UNVERIFIED", "sync_authorized": False,
              "candidate_provenance": "STATIC_CONTRACT_VERIFIED_EXECUTION_NOT_AUTHORIZED"}
    if live_snapshot is not None:
        evidence_path(Path(live_snapshot))
        path = Path(live_snapshot).resolve()
        evidence_path(path)
        require(not inside(path, repo), "Live snapshot must remain outside the repository")
        result["live_comparison"] = compare_live(load_json(path.read_text()), rollback[0])
    return result


def load_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs)
    except (ValueError, TypeError):
        raise SafetyError("Invalid JSON evidence") from None


def sanitize_live(raw):
    require(isinstance(raw, dict) and raw.get("apiVersion") == "apps/v1" and raw.get("kind") == "Deployment", "Unexpected live resource")
    meta = raw.get("metadata", {})
    require(meta.get("name") == NAME and meta.get("namespace") == NS, "Unexpected live Deployment identity")
    require(isinstance(meta.get("uid"), str) and re.fullmatch(r"[a-f0-9-]{36}", meta["uid"]), "Missing live UID")
    require(type(meta.get("generation")) is int and meta["generation"] > 0, "Missing live generation")
    # Never persist arbitrary live spec strings: only the reviewed complete
    # contract is safe to retain. Any drift fails before the output is opened.
    exact(raw.get("spec"), expected_spec(ROLLBACK, "Never"), "Live spec (snapshot refused)")
    annotations = meta.get("annotations", {})
    require(isinstance(annotations, dict), "Invalid live annotations")
    safe_annotations, hashes, omitted = {}, {}, []
    known = {**PROTECTED, SYNC_KEY: SYNC_VALUE, TRACKING_KEY: TRACKING_VALUE}
    for key, value in annotations.items():
        require(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_./-]+", key) and isinstance(value, str), "Unsafe annotation shape")
        if key == LAST_APPLIED:
            omitted.append(key)
        elif key in known:
            require(value == known[key], "Protected or tracking annotation differs")
            safe_annotations[key] = value
        elif key == REVISION_KEY:
            require(re.fullmatch(r"[0-9]+", value), "Unexpected Deployment revision annotation")
            safe_annotations[key] = value
        else:
            hashes[key] = hashlib.sha256(value.encode()).hexdigest()
    for key, value in PROTECTED.items():
        require(safe_annotations.get(key) == value, "Protected live annotation missing")
    managers = []
    require(isinstance(meta.get("managedFields"), list) and meta["managedFields"], "ManagedFields ownership evidence unavailable")
    for field in meta["managedFields"]:
        require(isinstance(field, dict), "Invalid managedFields")
        summary = {key: field[key] for key in ("manager", "operation", "apiVersion", "fieldsType", "subresource", "time") if key in field}
        require(all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:/+TZ-]{1,128}", v) for v in summary.values()), "Unsafe managedFields summary")
        fields = field.get("fieldsV1", {})
        require(isinstance(fields, dict), "Invalid managedFields ownership")
        annotations_owned = fields.get("f:metadata", {}).get("f:annotations", {})
        summary["protected_annotation_keys"] = sorted(k for k in PROTECTED if "f:" + k in annotations_owned)
        summary["owns_spec_fields"] = "f:spec" in fields
        managers.append(summary)
    return {"schema": SCHEMA, "deployment": {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": NAME, "namespace": NS, "uid": meta["uid"], "generation": meta["generation"],
                         "annotations": safe_annotations, "annotationValueHashes": hashes, "omittedAnnotationKeys": omitted},
            "spec": copy.deepcopy(raw["spec"]), "managedFieldsSummary": managers},
            "actual_argo_apply": "UNVERIFIED", "sync_authorized": False}


def compare_live(snapshot, desired):
    require(isinstance(snapshot, dict) and snapshot.get("schema") == SCHEMA, "Unsupported sanitized snapshot schema")
    require(set(snapshot) == {"schema", "deployment", "actual_argo_apply", "sync_authorized"}, "Unexpected snapshot fields")
    require(snapshot.get("actual_argo_apply") == "UNVERIFIED" and snapshot.get("sync_authorized") is False, "Snapshot cannot authorize synchronization")
    live = snapshot.get("deployment", {})
    require(isinstance(live, dict) and set(live) == {"apiVersion", "kind", "metadata", "spec", "managedFieldsSummary"}, "Invalid snapshot resource fields")
    meta = live.get("metadata", {})
    require(isinstance(meta, dict) and set(meta) == {"name", "namespace", "uid", "generation", "annotations", "annotationValueHashes", "omittedAnnotationKeys"}, "Invalid snapshot metadata fields")
    require(live.get("kind") == "Deployment" and live.get("apiVersion") == "apps/v1", "Invalid snapshot resource")
    require(meta.get("name") == NAME and meta.get("namespace") == NS, "Live identity mismatch")
    require(isinstance(meta.get("uid"), str) and re.fullmatch(r"[a-f0-9-]{36}", meta["uid"]), "Invalid snapshot UID")
    require(type(meta.get("generation")) is int and meta["generation"] > 0, "Invalid snapshot generation")
    exact(live.get("spec"), desired["spec"], "Live/Git spec")
    annotations = meta.get("annotations", {})
    require(isinstance(annotations, dict), "Invalid snapshot annotations")
    for key, value in PROTECTED.items():
        require(annotations.get(key) == desired["metadata"]["annotations"].get(key) == value, "Protected annotation deletion or modification")
    require(meta.get("annotationValueHashes") == {}, "Unreviewed live annotations require ownership review")
    allowed = {**PROTECTED, SYNC_KEY: SYNC_VALUE, TRACKING_KEY: TRACKING_VALUE}
    for key, value in annotations.items():
        if key == REVISION_KEY:
            require(isinstance(value, str) and re.fullmatch(r"[0-9]+", value), "Invalid revision annotation")
        else:
            require(key in allowed and value == allowed[key], "Unexpected live annotation difference")
    require(set(meta.get("omittedAnnotationKeys", [])) <= {LAST_APPLIED}, "Unexpected omitted annotations")
    require(isinstance(live.get("managedFieldsSummary"), list) and live["managedFieldsSummary"], "ManagedFields review evidence missing")
    return {"status": "PASS", "spec_differences": 0, "pod_template_differences": 0,
            "protected_annotation_deletions": 0, "uid": meta["uid"], "generation": meta["generation"],
            "expected_metadata_additions": [key for key in (SYNC_KEY, TRACKING_KEY) if key not in annotations],
            "managed_fields_review": "REQUIRES_OPERATOR_AND_ACTUAL_ARGO_DIFF_REVIEW"}


def snapshot_to_file(output):
    evidence_path(Path(output))
    path = Path(output).resolve()
    evidence_path(path)
    repo = Path(__file__).resolve().parents[1]
    require(Path(output).is_absolute(), "Snapshot output must be an absolute evidence path")
    require(not inside(path, repo), "Snapshot output must be outside the repository")
    require(path.parent.is_dir() and not path.exists(), "Snapshot requires an existing evidence directory and a new file")
    raw = load_json(run_readonly(["kubectl", "get", "deployment", NAME, "--namespace", NS, "--output", "json", "--show-managed-fields=true"]))
    safe = sanitize_live(raw)
    encoded = json.dumps(safe, indent=2, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(encoded)
    return {"status": "PASS", "snapshot": str(path), "schema": SCHEMA,
            "actual_argo_apply": "UNVERIFIED", "sync_authorized": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    checker = commands.add_parser("check")
    checker.add_argument("--repo", default=".")
    checker.add_argument("--live-snapshot")
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = check(args.repo, args.live_snapshot) if args.command == "check" else snapshot_to_file(args.output)
    except SafetyError as exc:
        result = {"status": "FAIL", "error": str(exc), "actual_argo_apply": "UNVERIFIED", "sync_authorized": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        result = {"status": "FAIL", "error": "Invalid input or unavailable local evidence; values suppressed",
                  "actual_argo_apply": "UNVERIFIED", "sync_authorized": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
