#!/usr/bin/env python3
"""Build GCP foundation inventories offline and check local schemas/policies.

No context/auth/Secret/cloud reads. Only local kustomize (or kubectl kustomize)
is executed. Public parent/child builds are independent inventories, never an
instruction to apply their concatenation. Live admission/runtime stay unverified.
"""
from __future__ import annotations

import argparse
import copy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import warnings

sys.dont_write_bytecode = True
import yaml
with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    import jsonschema

SCHEMA_MANIFEST_SHA = 'e5e12d5a98311e31e40bea0e5c3bdf1dc5a9e715f0a0434658ca7992f85bdad8'
PREFIXES = ('gitops/overlays/gcp', 'gitops/argocd/gcp', 'gitops/platform/gke-common')
COVERED = ('gitops/argocd/gcp/crds', 'gitops/argocd/gcp/runtime',
           'gitops/platform/gke-common/planned/bootstrap')
REQUIRED_ROOTS = {
    'gitops/overlays/gcp/dev', 'gitops/argocd/gcp',
    'gitops/platform/gke-common/monitoring', 'gitops/platform/gke-common/planned',
    'gitops/platform/gke-common/planned/airflow-migration-held',
    'gitops/platform/gke-common/planned/dev-platform-prep',
    'gitops/platform/gke-common/planned/dev-service-held',
    'gitops/platform/gke-common/planned/gateway-held',
}
HISTORICAL_DEV = 'gitops/overlays/gcp/dev'
FASTAPI_IMAGE = 'ghcr.io/shinsangseok/datacenter-api@sha256:5358f946c6c4bc853af2687577257821699870ef5e3e9a8d148c68cecadd773c'
HISTORICAL_DEV_PATCH_SHA = '83ba786ca7ddfcbab779dbf9e0217fb889e8c7be201a8deadc5e9e43ca672abe'
MAX_BYTES = 8 * 1024 * 1024
HEX = re.compile(r'[a-f0-9]{64}\Z')
IMAGE = re.compile(r'[^\s@]+@sha256:[a-f0-9]{64}\Z')
SAFE = re.compile(r'[A-Za-z0-9_.:/-]{1,253}\Z')
EXTERNAL = {
 ('datacenter-app-dev', 'Secret', 'datacenter-api-secret'): {'DB_HOST', 'DB_USER', 'DB_PASSWORD'},
 ('datacenter-app-dev', 'Secret', 'datacenter-target-policy'): {'DB_EXPECTED_HOST', 'DB_EXPECTED_USER', 'DB_EXPECTED_NAME'},
 ('datacenter-app-dev', 'Secret', 'datacenter-mlflow-client'): {'MLFLOW_TRACKING_USERNAME', 'MLFLOW_TRACKING_PASSWORD'},
 ('datacenter-app-dev', 'ConfigMap', 'datacenter-api-config'): {'DB_NAME'},
 ('datacenter-app-dev', 'ConfigMap', 'datacenter-api-db-ca'): {'ca.pem'},
 ('datacenter-airflow', 'Secret', 'gke-airflow-database'): {'sql-alchemy-conn'},
 ('datacenter-airflow', 'Secret', 'gke-airflow-crypto'): {'fernet-key', 'jwt-secret'},
 ('datacenter-platform', 'Secret', 'gke-common-gateway-tls'): {'tls.crt', 'tls.key'},
 ('datacenter-observability', 'Secret', 'gke-grafana-admin'): {'admin-user', 'admin-password', 'security-secret-key'},
 ('argocd', 'Secret', 'argocd-secret'): {'server.secretkey', 'admin.password', 'admin.passwordMtime'},
 ('argocd', 'Secret', 'gke03b-argocd-redis-auth'): {'auth'},
 ('argocd', 'Secret', 'gke03b-argocd-redis-tls'): {'tls.crt', 'tls.key', 'ca.crt'},
}
OPTIONAL_ARGO_SECRETS = {
    'argocd-redis': {'redis-username'},
    'gke03b-argocd-redis': {'redis-sentinel-username', 'redis-sentinel-password'},
    'argocd-repo-server-tls': {'tls.crt', 'tls.key', 'ca.crt'},
    'argocd-dex-server-tls': {'tls.crt', 'tls.key', 'ca.crt'},
}
SECURITY_KINDS = {'Namespace', 'NetworkPolicy', 'Role', 'RoleBinding',
                  'ServiceAccount', 'ConfigMap', 'StorageClass'}


def scan_raw_credentials(document):
    """Reject literal credentials even in a patch later removed by Kustomize."""
    credential = re.compile(r'password|passwd|token|secret|credential|private[_-]?key', re.I)
    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get('name'), str) and 'value' in node:
                need(not (credential.search(node['name']) and node['value']), 'RAW_INLINE_CREDENTIAL_FORBIDDEN')
            if node.get('kind') == 'ConfigMap':
                for field in ('data', 'binaryData'):
                    for key, value in node.get(field, {}).items():
                        need(not (credential.search(key) and value), 'RAW_CONFIGMAP_CREDENTIAL_FORBIDDEN')
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            need(not re.search(r'-----BEGIN [A-Z ]*PRIVATE KEY-----', node), 'PRIVATE_KEY_LITERAL_FORBIDDEN')
    walk(document)


class Hold(Exception):
    """Only fixed safe codes; never attach an input/body/error message."""


def need(condition, code):
    if not condition:
        raise Hold(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode())


def pairs(rows):
    result = {}
    for key, value in rows:
        need(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def strict_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Hold('NONFINITE_JSON')))
    except Hold:
        raise
    except Exception:
        raise Hold('INVALID_JSON') from None


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        need(not self.check_event(yaml.AliasEvent), 'YAML_ALIAS_FORBIDDEN')
        return super().compose_node(parent, index)


def mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        need(isinstance(key, str), 'NON_STRING_YAML_KEY')
        need(key not in result, 'DUPLICATE_YAML_KEY')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def strict_yaml(raw):
    try:
        docs = [d for d in yaml.load_all(raw, Loader=StrictLoader) if d is not None]
        for doc in docs:
            json.dumps(doc, allow_nan=False)
        return docs
    except Hold:
        raise
    except Exception:
        raise Hold('INVALID_OR_NON_JSON_YAML') from None


def read_local(root, relative):
    rel = Path(relative)
    need(not rel.is_absolute() and '..' not in rel.parts, 'RELATIVE_INPUT_REQUIRED')
    path = root / rel
    need(path.is_file() and path == path.resolve(strict=True) and path.is_relative_to(root),
         'REGULAR_NON_SYMLINK_INPUT_REQUIRED')
    need(path.stat().st_size <= MAX_BYTES, 'INPUT_SIZE_BUDGET')
    return path.read_bytes()


def children(node):
    for key in ('properties', 'patternProperties', 'definitions', '$defs', 'dependencies'):
        value = node.get(key)
        if isinstance(value, dict):
            yield from (v for v in value.values() if isinstance(v, dict))
    for key in ('items', 'additionalItems', 'additionalProperties', 'not'):
        value = node.get(key)
        if isinstance(value, dict):
            yield value
        elif isinstance(value, list):
            yield from (v for v in value if isinstance(v, dict))
    for key in ('allOf', 'anyOf', 'oneOf'):
        if isinstance(node.get(key), list):
            yield from (v for v in node[key] if isinstance(v, dict))


def normalize(schema, definition=None, strict=True):
    """03A normalization: IntOrString/Quantity, nullable, local refs only."""
    result = copy.deepcopy(schema)
    def visit(node, quantity=False):
        if '$ref' in node:
            need(isinstance(node['$ref'], str) and node['$ref'].startswith('#/definitions/'),
                 'EXTERNAL_SCHEMA_REF_FORBIDDEN')
        need(not any(isinstance(node.get(k), str) and node[k] for k in ('id', '$id')),
             'SCHEMA_SCOPE_CHANGE_FORBIDDEN')
        if quantity or node.get('format') == 'int-or-string' or node.get('x-kubernetes-int-or-string') is True:
            node.pop('type', None)
            node.pop('format', None)
            node['anyOf'] = [{'type': 'integer'}, {'type': 'string'}]
        if node.get('nullable') is True or node.get('x-nullable') is True:
            if isinstance(node.get('type'), str):
                node['type'] = [node['type'], 'null']
            elif isinstance(node.get('anyOf'), list):
                node['anyOf'].append({'type': 'null'})
        if strict and node.get('properties') and 'additionalProperties' not in node \
                and node.get('x-kubernetes-preserve-unknown-fields') is not True:
            node['additionalProperties'] = False
        for child in children(node):
            visit(child)
    visit(result, definition == 'io.k8s.apimachinery.pkg.api.resource.Quantity')
    return result


class LocalResolver(jsonschema.RefResolver):
    def resolve_remote(self, uri):
        raise Hold('EXTERNAL_SCHEMA_RESOLUTION_FORBIDDEN')


def validate_schema(document, schema, definitions):
    wrapper = {'$schema': 'http://json-schema.org/draft-04/schema#',
               'definitions': definitions, **schema}
    resolver = LocalResolver.from_schema(wrapper, handlers={
        k: (lambda _: (_ for _ in ()).throw(Hold('EXTERNAL_SCHEMA_RESOLUTION_FORBIDDEN')))
        for k in ('http', 'https', 'file', 'ftp')})
    errors = list(jsonschema.Draft4Validator(wrapper, resolver=resolver).iter_errors(document))
    need(not errors, 'OFFLINE_SCHEMA_MISMATCH')


def identity(doc):
    need(isinstance(doc, dict), 'OBJECT_REQUIRED')
    meta = doc.get('metadata', {})
    row = (doc.get('apiVersion'), doc.get('kind'), meta.get('namespace', ''), meta.get('name'))
    need(all(isinstance(x, str) and (not x or SAFE.fullmatch(x)) for x in row), 'OBJECT_IDENTITY_REQUIRED')
    return row


def quantity(value):
    m = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([eE][+-]?[0-9]+|[numkKMGTPE]i?)?', str(value))
    need(m is not None, 'RESOURCE_QUANTITY_INVALID')
    suffix = m[2] or ''
    units = {'n': Decimal('1e-9'), 'u': Decimal('1e-6'), 'm': Decimal('1e-3'), '': Decimal(1), 'k': Decimal(1000)}
    units.update({u: Decimal(1000) ** (i+1) for i, u in enumerate('KMGTPE')})
    units.update({u+'i': Decimal(1024) ** (i+1) for i, u in enumerate('KMGTPE')})
    multiplier = Decimal('1'+suffix) if suffix.startswith(('e', 'E')) and len(suffix) > 1 else units.get(suffix)
    need(multiplier is not None, 'RESOURCE_QUANTITY_INVALID')
    return Decimal(m[1]) * multiplier


def check_policy(doc, root='', notes=None):
    notes = [] if notes is None else notes
    _, kind, ns, name = identity(doc)
    need(kind not in {'Secret', 'Application', 'ApplicationSet', 'ClusterRole', 'ClusterRoleBinding',
                      'Pod', 'Job', 'CronJob', 'DaemonSet', 'PersistentVolume'}, 'FORBIDDEN_RESOURCE_KIND')
    if kind == 'Role':
        need(ns, 'NAMESPACED_ROLE_REQUIRED')
        for rule in doc.get('rules', []):
            need(set(rule.get('verbs', [])) <= {'get', 'list', 'watch'}, 'WRITABLE_ROLE_FORBIDDEN')
            need(not rule.get('nonResourceURLs'), 'NONRESOURCE_ROLE_FORBIDDEN')
            need('*' not in rule.get('resources', []), 'WILDCARD_ROLE_FORBIDDEN')
            if 'secrets' in rule.get('resources', []):
                need(ns == 'argocd' and name in {'gke03b-argocd-application-controller', 'gke03b-argocd-server'},
                     'UNAPPROVED_SECRET_READ_ROLE')
    if kind == 'RoleBinding':
        need(doc.get('roleRef', {}).get('kind') == 'Role', 'CLUSTER_ROLE_REFERENCE_FORBIDDEN')
        need(all(s.get('kind') == 'ServiceAccount' and s.get('namespace') == ns for s in doc.get('subjects', [])),
             'CROSS_NAMESPACE_ROLE_BINDING_FORBIDDEN')
    if kind == 'Service':
        spec = doc.get('spec', {})
        need(spec.get('type', 'ClusterIP') == 'ClusterIP' and not spec.get('externalIPs')
             and not any(p.get('nodePort') for p in spec.get('ports', [])), 'PUBLIC_SERVICE_FORBIDDEN')
    if kind == 'StorageClass':
        need(doc.get('reclaimPolicy') == 'Retain' and doc.get('provisioner') == 'pd.csi.storage.gke.io'
             and doc.get('volumeBindingMode') == 'WaitForFirstConsumer', 'RETAIN_PD_STORAGE_REQUIRED')
    if kind == 'PersistentVolumeClaim':
        need(doc.get('spec', {}).get('storageClassName') == 'gke-common-retain-balanced', 'PVC_RETAIN_STORAGE_REQUIRED')
        need(not doc['spec'].get('volumeName'), 'PREBOUND_SOURCE_PV_FORBIDDEN')
    if kind == 'AppProject':
        spec = doc.get('spec', {})
        need(ns == 'argocd' and name == 'default' and all(spec.get(k) == [] for k in
             ('sourceRepos', 'destinations', 'sourceNamespaces', 'clusterResourceWhitelist'))
             and spec.get('namespaceResourceBlacklist') == [{'group': '*', 'kind': '*'}], 'DEFAULT_PROJECT_NOT_DENY_ALL')
    if kind == 'ConfigMap' and ns == 'argocd':
        data = doc.get('data', {})
        if name == 'argocd-cm':
            need(data.get('cluster.inClusterEnabled') == 'false', 'ARGO_IN_CLUSTER_REGISTRATION_FORBIDDEN')
        if name == 'argocd-cmd-params-cm':
            need(data.get('application.namespaces') == '', 'ARGO_CROSS_NAMESPACE_INFORMER_FORBIDDEN')
    if kind not in ('Deployment', 'StatefulSet'):
        return
    spec = doc['spec']['template']['spec']
    need(not any(spec.get(k) for k in ('hostNetwork', 'hostPID', 'hostIPC', 'hostAliases', 'imagePullSecrets')),
         'HOST_OR_UNAPPROVED_POD_CONFIGURATION')
    need(not any('hostPath' in v for v in spec.get('volumes', [])), 'HOST_PATH_FORBIDDEN')
    need(spec.get('nodeSelector', {}).get('cloud.google.com/gke-nodepool') == 'shared-pool', 'GKE_SHARED_POOL_REQUIRED')
    api_client = ns == 'argocd' and name in {'gke03b-argocd-server', 'gke03b-argocd-application-controller'}
    need(spec.get('automountServiceAccountToken') is api_client, 'POD_API_TOKEN_SCOPE_CHANGED')
    podsc = spec.get('securityContext', {})
    is_fastapi = ns == 'datacenter-app-dev' and name == 'datacenter-api'
    historical = is_fastapi and root == HISTORICAL_DEV and len(spec.get('containers', [])) == 1 \
        and spec['containers'][0].get('image') == FASTAPI_IMAGE
    if historical and 'seccompProfile' not in podsc:
        notes.append('HISTORICAL_02A_DEV_SECCOMP_UNSPECIFIED_DEPLOYMENT_HOLD')
    for c in spec.get('containers', []) + spec.get('initContainers', []):
        need(isinstance(c.get('image'), str) and IMAGE.fullmatch(c['image']), 'IMAGE_DIGEST_REQUIRED')
        sc = c.get('securityContext', {})
        need(sc.get('allowPrivilegeEscalation') is False and sc.get('privileged', False) is False
             and 'ALL' in sc.get('capabilities', {}).get('drop', [])
             and not sc.get('capabilities', {}).get('add'), 'CONTAINER_SECURITY_REQUIRED')
        need(sc.get('runAsNonRoot', podsc.get('runAsNonRoot')) is True
             and sc.get('runAsUser', podsc.get('runAsUser', 1)) != 0, 'NONROOT_REQUIRED')
        seccomp = sc.get('seccompProfile', podsc.get('seccompProfile', {}))
        need((historical and not seccomp) or seccomp.get('type') == 'RuntimeDefault',
             'RUNTIME_DEFAULT_SECCOMP_REQUIRED')
        need(is_fastapi or sc.get('readOnlyRootFilesystem') is True, 'READONLY_ROOT_FILESYSTEM_REQUIRED')
        resources = c.get('resources', {})
        for resource in ('cpu', 'memory'):
            request = quantity(resources.get('requests', {}).get(resource, 0))
            limit = quantity(resources.get('limits', {}).get(resource, 0))
            need(0 < request <= limit, 'POSITIVE_BOUNDED_RESOURCES_REQUIRED')
        need(not c.get('envFrom'), 'UNBOUNDED_ENV_FROM_FORBIDDEN')
        for env in c.get('env', []):
            need(not (re.search(r'PASSWORD|TOKEN|SECRET|CREDENTIAL|PRIVATE_KEY', env.get('name', ''))
                      and env.get('value')), 'INLINE_CREDENTIAL_FORBIDDEN')


def check_dashboard(source, candidate, contract):
    need(source.get('metadata', {}).get('name') == contract['uid']
         and candidate.get('metadata', {}).get('name') == contract['uid'], 'DASHBOARD_UID_CHANGED')
    need(candidate.get('apiVersion') == 'dashboard.grafana.app/v2' and candidate.get('kind') == 'Dashboard'
         and candidate.get('status') == {}, 'DASHBOARD_API_ENVELOPE_INVALID')
    need(canonical(source['spec']) == contract['source_spec_sha256'], 'APPROVED_AWS_DASHBOARD_SOURCE_CHANGED')
    expected = copy.deepcopy(source['spec'])
    changes = []
    def walk(node):
        if isinstance(node, dict):
            ds = node.get('datasource')
            if isinstance(ds, dict) and ds.get('name') == contract['datasource_before']:
                need(set(ds) == {'name'}, 'DASHBOARD_DATASOURCE_AMBIGUOUS')
                ds['name'] = contract['datasource_after']
                changes.append(True)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(expected)
    need(len(changes) == contract['mapping_count'], 'EXACT_SIX_DATASOURCE_MAPPINGS_REQUIRED')
    need(candidate['spec'] == expected, 'DASHBOARD_SPEC_OR_PROMQL_CHANGED')
    elements = candidate['spec'].get('elements', {})
    need(len(elements) == 6, 'EXACT_SIX_PANELS_REQUIRED')
    return {'uid': contract['uid'], 'panels': 6, 'datasource_mappings': 6,
            'exact_source_spec_and_PromQL_preserved_except_datasource': True,
            'plugin_runtime_data_correctness': 'UNVERIFIED'}


def load_schemas(script_dir, repo):
    schema_dir = script_dir / 'schemas'
    manifest_raw = read_local(schema_dir, 'manifest.json')
    need(sha(manifest_raw) == SCHEMA_MANIFEST_SHA, 'SCHEMA_MANIFEST_PIN_MISMATCH')
    manifest = strict_json(manifest_raw)
    data = {}
    for entry in manifest['files']:
        raw = read_local(schema_dir, entry['path'])
        need(sha(raw) == entry['sha256'], 'SCHEMA_FILE_PIN_MISMATCH')
        data[entry['path']] = strict_json(raw)
    core = data['kubernetes-subset.json']
    definitions = {name: normalize(s, name) for name, s in core['definitions'].items()}
    schemas = {k: {'$ref': '#/definitions/'+v} for k, v in core['roots'].items()}
    for key, s in data['gateway-subset.json']['schemas'].items():
        schemas[key] = normalize(s)
    argo = strict_yaml(read_local(repo, 'gitops/argocd/gcp/crds/crds.yaml'))
    for doc in argo:
        if doc['spec']['names']['kind'] != 'AppProject':
            continue
        for version in doc['spec']['versions']:
            if version.get('served'):
                schemas[doc['spec']['group']+'/'+version['name']+'|AppProject'] = normalize(version['schema']['openAPIV3Schema'])
    for schema in schemas.values():
        if 'properties' in schema and 'metadata' in schema['properties']:
            schema['properties']['metadata'] = {'$ref': '#/definitions/io.k8s.apimachinery.pkg.apis.meta.v1.ObjectMeta'}
    return definitions, schemas, data, manifest


def check_sources(repo, baseline):
    found = {}
    for prefix in baseline['prefixes']:
        path = repo / prefix
        if path.exists():
            for p in sorted(path.rglob('*')):
                if p.is_file():
                    relative = str(p.relative_to(repo))
                    found[relative] = sha(read_local(repo, relative))
    need(found == baseline['files'], 'AWS_SOURCE_BASELINE_CHANGED')
    return len(found)


def discover(repo, source_pins):
    discovered = []
    raw_objects = []
    raw_resource_files = set()
    used_files = set()
    actual_files = {}
    for prefix in PREFIXES:
        base = repo / prefix
        need(base.is_dir() and base == base.resolve(), 'GCP_SOURCE_PREFIX_REQUIRED')
        for ancestor in [repo/Path(prefix).parts[0], repo/Path(*Path(prefix).parts[:2]), base]:
            need(not ancestor.is_symlink(), 'SOURCE_DIRECTORY_SYMLINK_FORBIDDEN')
        for directory, dirs, files in os.walk(base, followlinks=False):
            need(not any((Path(directory)/p).is_symlink() for p in dirs+files), 'SOURCE_SYMLINK_FORBIDDEN')
            for name in files:
                path = Path(directory)/name
                relative = str(path.relative_to(repo))
                need(name not in ('kustomization.yml', 'Kustomization'), 'ALTERNATE_KUSTOMIZATION_NAME_FORBIDDEN')
                actual_files[relative] = sha(read_local(repo, relative))
        for p in sorted(base.rglob('kustomization.yaml')):
            discovered.append(str(p.parent.relative_to(repo)))
        for p in sorted(base.rglob('*')):
            if p.is_file() and p.suffix.lower() in ('.yaml', '.yml', '.json'):
                raw = read_local(repo, str(p.relative_to(repo)))
                docs = [strict_json(raw)] if p.suffix.lower() == '.json' else strict_yaml(raw)
                relative = str(p.relative_to(repo))
                for doc in docs:
                    if isinstance(doc, dict):
                        scan_raw_credentials(doc)
                        need(doc.get('kind') not in ('Secret', 'Application', 'ApplicationSet', 'ClusterRole', 'ClusterRoleBinding'),
                             'FORBIDDEN_GCP_SOURCE_KIND')
                        if doc.get('apiVersion') and doc.get('kind') not in (None, 'Kustomization', 'Dashboard'):
                            raw_objects.append(doc)
                            raw_resource_files.add(relative)
                            # Partial Deployment patches are checked after their build.
                            if doc.get('kind') not in ('Deployment', 'StatefulSet'):
                                check_policy(doc)
    need(actual_files == source_pins, 'APPROVED_GCP_SOURCE_SNAPSHOT_CHANGED')
    public = [p for p in discovered if p not in COVERED]
    need(REQUIRED_ROOTS <= set(public), 'GCP_PUBLIC_ROOT_MISSING')
    # Resolve every source reference before subprocess: local only; no plugins,
    # Helm generators, remote bases or executable KRM functions can be enabled.
    visited = set()
    def inspect(relative):
        if relative in visited:
            return
        visited.add(relative)
        used_files.add(relative+'/kustomization.yaml')
        obj = strict_yaml(read_local(repo, relative+'/kustomization.yaml'))
        need(len(obj) == 1 and isinstance(obj[0], dict), 'KUSTOMIZATION_OBJECT_REQUIRED')
        obj = obj[0]
        allowed = {'apiVersion', 'kind', 'resources', 'patches', 'namespace', 'images', 'labels', 'annotations',
                   'namePrefix', 'nameSuffix', 'commonLabels', 'commonAnnotations', 'replicas'}
        need(set(obj) <= allowed and obj.get('kind') == 'Kustomization', 'KUSTOMIZE_PLUGIN_OR_GENERATOR_FORBIDDEN')
        paths = list(obj.get('resources', []))
        paths += [p['path'] for p in obj.get('patches', []) if isinstance(p, dict) and 'path' in p]
        for ref in paths:
            need(isinstance(ref, str) and not Path(ref).is_absolute() and '://' not in ref and '?' not in ref and '\\' not in ref,
                 'REMOTE_OR_INVALID_KUSTOMIZE_REFERENCE')
            path = (repo/relative/ref).resolve(strict=True)
            need(path.is_relative_to(repo), 'KUSTOMIZE_REFERENCE_OUTSIDE_REPOSITORY')
            if path.is_dir():
                inspect(str(path.relative_to(repo)))
            else:
                file_relative = str(path.relative_to(repo))
                strict_yaml(read_local(repo, file_relative))
                used_files.add(file_relative)
    for root in discovered:
        inspect(root)
    need(raw_resource_files <= used_files, 'UNREFERENCED_GCP_MANIFEST_FORBIDDEN')
    return public, [r for r in discovered if r in COVERED], raw_objects


def build(repo, root, tool):
    with tempfile.TemporaryDirectory(prefix='gke-foundation-offline-') as tmp:
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
               'KUBECONFIG': '/dev/null', 'XDG_CACHE_HOME': tmp, 'LANG': 'C.UTF-8'}
        result = subprocess.run(tool+[str(repo/root), '--load-restrictor=LoadRestrictionsRootOnly'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, timeout=60, check=False)
    need(result.returncode == 0, 'OFFLINE_KUSTOMIZE_BUILD_FAILED')
    need(len(result.stdout) <= MAX_BYTES, 'BUILD_OUTPUT_SIZE_BUDGET')
    return result.stdout


def check_references(documents):
    index = {(d['kind'], d['metadata'].get('namespace', ''), d['metadata']['name']): d for d in documents}
    external = set()
    def require(kind, ns, name, key=None, optional=False):
        need(isinstance(name, str) and SAFE.fullmatch(name), 'REFERENCE_NAME_INVALID')
        found = index.get((kind, ns, name))
        if found is not None:
            if kind == 'ConfigMap' and key and not optional:
                need(key in found.get('data', {}) or key in found.get('binaryData', {}), 'CONFIGMAP_KEY_MISSING')
            return
        allowed = EXTERNAL.get((ns, kind, name))
        if allowed is not None:
            need(key is None or key in allowed, 'EXTERNAL_REFERENCE_KEY_UNDECLARED')
            external.add((ns, kind, name))
            return
        if optional and ns == 'argocd' and kind == 'Secret' and name in OPTIONAL_ARGO_SECRETS:
            need(key is None or key in OPTIONAL_ARGO_SECRETS[name], 'OPTIONAL_REFERENCE_KEY_UNDECLARED')
            return
        if optional and ns == 'argocd' and kind == 'ConfigMap' and name == 'argocd-styles-cm' and key is None:
            return
        raise Hold('UNRESOLVED_OR_UNDECLARED_REFERENCE')
    for d in documents:
        kind = d['kind']; ns = d['metadata'].get('namespace', '')
        if kind == 'PersistentVolumeClaim':
            require('StorageClass', '', d['spec']['storageClassName'])
        if kind == 'RoleBinding':
            require('Role', ns, d['roleRef']['name'])
            for s in d.get('subjects', []):
                require('ServiceAccount', s['namespace'], s['name'])
        if kind == 'Gateway':
            for listener in d.get('spec', {}).get('listeners', []):
                for ref in listener.get('tls', {}).get('certificateRefs', []):
                    require('Secret', ref.get('namespace', ns), ref['name'])
        if kind not in ('Deployment', 'StatefulSet'):
            continue
        p = d['spec']['template']['spec']
        require('ServiceAccount', ns, p.get('serviceAccountName', 'default'))
        for c in p.get('containers', [])+p.get('initContainers', []):
            for env in c.get('env', []):
                value = env.get('valueFrom', {})
                for field, refkind in [('secretKeyRef', 'Secret'), ('configMapKeyRef', 'ConfigMap')]:
                    if field in value:
                        r = value[field]; require(refkind, ns, r['name'], r['key'], r.get('optional', False))
        for v in p.get('volumes', []):
            for field, refkind, namekey in [('secret', 'Secret', 'secretName'), ('configMap', 'ConfigMap', 'name'),
                                            ('persistentVolumeClaim', 'PersistentVolumeClaim', 'claimName')]:
                if field in v:
                    ref = v[field]; require(refkind, ns, ref[namekey], optional=ref.get('optional', False))
                    for item in ref.get('items', []):
                        require(refkind, ns, ref[namekey], item['key'], ref.get('optional', False))
    # Argo's settings Secret is consumed internally rather than through an env ref.
    if ('ConfigMap', 'argocd', 'argocd-cm') in index:
        require('Secret', 'argocd', 'argocd-secret')
    return [{'namespace': ns, 'kind': kind, 'name': name, 'state': 'EXTERNAL_RUNTIME_DEPENDENCY_UNVERIFIED'}
            for ns, kind, name in sorted(external)]


def check_security_contract(documents, contract):
    actual = {}
    for d in documents:
        if d['kind'] in SECURITY_KINDS:
            key = '|'.join(identity(d))
            value = canonical(d)
            need(key not in actual or actual[key] == value, 'CONFLICTING_SECURITY_OBJECT_ACROSS_ROOTS')
            actual[key] = value
    need(actual == contract, 'APPROVED_NAMESPACE_RBAC_NETWORK_CONFIG_OR_STORAGE_CHANGED')


def validate(repo):
    repo = Path(repo).resolve(strict=True)
    script_dir = Path(__file__).resolve().parent
    definitions, schemas, data, manifest = load_schemas(script_dir, repo)
    source_count = check_sources(repo, data['aws-source-baseline.json'])
    need(sha(read_local(repo, HISTORICAL_DEV+'/deployment-contract.patch.yaml')) == HISTORICAL_DEV_PATCH_SHA,
         'HISTORICAL_DEV_PATCH_CHANGED')
    closed_contract = data['source-metadata-contract.json']
    roots, covered, raw_objects = discover(repo, closed_contract['approved_source_files_sha256'])
    executable = shutil.which('kustomize')
    tool = [executable, 'build'] if executable else [shutil.which('kubectl') or '', 'kustomize']
    need(bool(tool[0]), 'LOCAL_KUSTOMIZE_TOOL_REQUIRED')
    all_documents = []; builds = []; notes = []
    for root in roots:
        raw = build(repo, root, tool)
        docs = strict_yaml(raw); seen = set()
        for doc in docs:
            key = identity(doc)
            need(key not in seen, 'DUPLICATE_OBJECT_WITHIN_BUILD')
            seen.add(key)
            check_policy(doc, root, notes)
            schema = schemas.get(key[0]+'|'+key[1])
            need(schema is not None, 'NO_LOCAL_SCHEMA_FOR_GVK')
            validate_schema(doc, schema, definitions)
        all_documents.extend(docs)
        builds.append({'path': root, 'objects': len(docs), 'sha256': sha(raw), 'schema_and_policy': 'PASS'})
    external = check_references(all_documents)
    check_security_contract(all_documents, closed_contract['approved_security_objects_sha256'])
    contract = data['dashboard-contract.json']
    source = strict_json(read_local(repo, contract['source_path']))
    candidate_raw = read_local(repo, contract['candidate_path'])
    need(sha(candidate_raw) == contract['candidate_sha256'], 'APPROVED_DASHBOARD_CANDIDATE_CHANGED')
    candidate = strict_json(candidate_raw)
    dashboard = check_dashboard(source, candidate, contract)
    graf = data['grafana-dashboard-v2-subset.json']
    grafdefs = {n: normalize(s, strict=False) for n, s in graf['definitions'].items()}
    validate_schema(candidate, {'$ref': '#/definitions/'+graf['root']}, grafdefs)
    dashboard['target_served_13_3_1_structural_schema'] = 'PASS'
    return {'result': 'GKE_FOUNDATION_OFFLINE_PASS', 'mode': 'LOCAL_BUILD_SCHEMA_POLICY_ONLY',
            'builds': builds, 'public_root_count': len(roots), 'internal_roots_covered_by_parent': covered,
            'document_checks_across_independent_builds': len(all_documents),
            'duplicate_objects_across_parent_child_roots': 'Allowed independent inventories; do not concatenate or apply as one list.',
            'AWS_source_files_preserved': source_count, 'dashboard': dashboard,
            'external_dependencies': external, 'warnings': sorted(set(notes)),
            'schema_manifest_sha256': SCHEMA_MANIFEST_SHA,
            'schema_limitations': manifest['limitations'],
            'live_admission_CEL_defaulting_PSS_runtime': 'UNVERIFIED',
            'context_auth_Secret_cloud_network_calls': 0, 'resource_or_Git_changes': 0,
            'deployment_approval': False}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Hold('ARGUMENTS_REJECTED_NO_VALUES_LOGGED')


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    report = validate(args.repo)
    payload = (json.dumps(report, ensure_ascii=False, indent=2)+'\n').encode()
    if args.output:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
    print(json.dumps(report, ensure_ascii=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Hold as exc:
        print(json.dumps({'result': 'GKE_FOUNDATION_OFFLINE_HOLD', 'safe_code': str(exc)}))
        raise SystemExit(2)
    except Exception:
        print(json.dumps({'result': 'GKE_FOUNDATION_OFFLINE_HOLD', 'safe_code': 'LOCAL_VALIDATION_FAILED_NO_VALUES_LOGGED'}))
        raise SystemExit(2)
