"""New foundation policy regression tests; no provider/build/auth operations."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1]/'scripts/gcp/validate_foundation.py'
spec = importlib.util.spec_from_file_location('foundation', MODULE)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


def obj(kind, name='synthetic', namespace='synthetic'):
    return {'apiVersion': 'v1', 'kind': kind, 'metadata': {'name': name, 'namespace': namespace}}


def pod():
    d = obj('Deployment')
    d['apiVersion'] = 'apps/v1'
    d['spec'] = {'replicas': 0, 'template': {'spec': {
        'automountServiceAccountToken': False,
        'nodeSelector': {'cloud.google.com/gke-nodepool': 'shared-pool'},
        'securityContext': {'runAsNonRoot': True, 'seccompProfile': {'type': 'RuntimeDefault'}},
        'containers': [{'name': 'synthetic', 'image': 'example.invalid/image@sha256:'+'a'*64,
                        'securityContext': {'allowPrivilegeEscalation': False,
                                            'capabilities': {'drop': ['ALL']},
                                            'readOnlyRootFilesystem': True},
                        'resources': {'requests': {'cpu': '10m', 'memory': '32M'},
                                      'limits': {'cpu': '20m', 'memory': '32Mi'}}}]}}}
    return d


class FoundationPolicyTests(unittest.TestCase):
    def hold(self, code, fn, *args):
        with self.assertRaises(v.Hold) as caught:
            fn(*args)
        self.assertEqual(str(caught.exception), code)

    def test_valid_nonroot_pinned_pod_and_decimal_resources(self):
        v.check_policy(pod())
        self.assertLess(v.quantity('32M'), v.quantity('32Mi'))

    def test_secret_application_and_cluster_rbac_rejected(self):
        for kind in ['Secret', 'Application', 'ApplicationSet', 'ClusterRole', 'ClusterRoleBinding']:
            with self.subTest(kind=kind):
                self.hold('FORBIDDEN_RESOURCE_KIND', v.check_policy, obj(kind))

    def test_writable_and_unapproved_secret_read_roles_rejected(self):
        d = obj('Role'); d['rules'] = [{'verbs': ['get', 'patch'], 'resources': ['configmaps']}]
        self.hold('WRITABLE_ROLE_FORBIDDEN', v.check_policy, d)
        d['rules'] = [{'verbs': ['get'], 'resources': ['secrets']}]
        self.hold('UNAPPROVED_SECRET_READ_ROLE', v.check_policy, d)

    def test_tag_image_and_privilege_escalation_rejected(self):
        d = pod(); d['spec']['template']['spec']['containers'][0]['image'] = 'example.invalid/image:latest'
        self.hold('IMAGE_DIGEST_REQUIRED', v.check_policy, d)
        d = pod(); d['spec']['template']['spec']['containers'][0]['securityContext']['allowPrivilegeEscalation'] = True
        self.hold('CONTAINER_SECURITY_REQUIRED', v.check_policy, d)

    def test_unconfined_seccomp_never_uses_historical_exception(self):
        d = pod(); d['metadata'].update(name='datacenter-api', namespace='datacenter-app-dev')
        d['spec']['template']['spec']['containers'][0]['image'] = v.FASTAPI_IMAGE
        d['spec']['template']['spec']['securityContext']['seccompProfile'] = {'type': 'Unconfined'}
        self.hold('RUNTIME_DEFAULT_SECCOMP_REQUIRED', v.check_policy, d, v.HISTORICAL_DEV)

    def test_delete_storage_and_public_service_rejected(self):
        d = obj('StorageClass'); d['reclaimPolicy'] = 'Delete'
        self.hold('RETAIN_PD_STORAGE_REQUIRED', v.check_policy, d)
        d = obj('Service'); d['spec'] = {'type': 'LoadBalancer'}
        self.hold('PUBLIC_SERVICE_FORBIDDEN', v.check_policy, d)

    def test_default_project_wildcard_and_incluster_enable_rejected(self):
        d = obj('AppProject', 'default', 'argocd')
        d['spec'] = {'sourceRepos': ['*'], 'destinations': [], 'sourceNamespaces': [],
                     'clusterResourceWhitelist': [], 'namespaceResourceBlacklist': [{'group': '*', 'kind': '*'}]}
        self.hold('DEFAULT_PROJECT_NOT_DENY_ALL', v.check_policy, d)
        d = obj('ConfigMap', 'argocd-cm', 'argocd'); d['data'] = {'cluster.inClusterEnabled': 'true'}
        self.hold('ARGO_IN_CLUSTER_REGISTRATION_FORBIDDEN', v.check_policy, d)

    def test_duplicate_json_yaml_and_alias_rejected(self):
        self.hold('DUPLICATE_JSON_KEY', v.strict_json, '{"a":1,"a":2}')
        self.hold('DUPLICATE_YAML_KEY', v.strict_yaml, 'a: 1\na: 2\n')
        self.hold('YAML_ALIAS_FORBIDDEN', v.strict_yaml, 'a: &a [1]\nb: *a\n')

    def test_schema_unknown_field_and_external_ref_rejected(self):
        schema = v.normalize({'type': 'object', 'properties': {'ok': {'type': 'boolean'}}})
        self.hold('OFFLINE_SCHEMA_MISMATCH', v.validate_schema, {'ok': True, 'bad': 1}, schema, {})
        self.hold('EXTERNAL_SCHEMA_REF_FORBIDDEN', v.normalize, {'$ref': 'https://example.invalid/schema'})

    def test_dashboard_only_six_datasource_changes_and_no_promql_rewrite(self):
        source = {'metadata': {'name': 'adtgr97'}, 'spec': {'elements': {
            str(i): {'datasource': {'name': 'prometheus'}, 'expr': 'synthetic_metric_'+str(i)} for i in range(6)}}}
        contract = {'uid': 'adtgr97', 'source_spec_sha256': v.canonical(source['spec']),
                    'datasource_before': 'prometheus', 'datasource_after': 'gke-new-prometheus', 'mapping_count': 6}
        candidate = copy.deepcopy(source); candidate.update(apiVersion='dashboard.grafana.app/v2', kind='Dashboard', status={})
        for el in candidate['spec']['elements'].values():
            el['datasource']['name'] = 'gke-new-prometheus'
        self.assertEqual(v.check_dashboard(source, candidate, contract)['datasource_mappings'], 6)
        candidate['spec']['elements']['0']['expr'] = 'different_metric'
        self.hold('DASHBOARD_SPEC_OR_PROMQL_CHANGED', v.check_dashboard, source, candidate, contract)

    def test_unknown_external_secret_reference_rejected(self):
        d = pod(); p = d['spec']['template']['spec']; p['serviceAccountName'] = 'synthetic'
        p['containers'][0]['env'] = [{'name': 'PASS', 'valueFrom': {'secretKeyRef': {'name': 'unknown', 'key': 'password'}}}]
        sa = obj('ServiceAccount')
        self.hold('UNRESOLVED_OR_UNDECLARED_REFERENCE', v.check_references, [d, sa])

    def test_only_pinned_argo_optional_tls_names_and_keys_allowed(self):
        d = pod(); d['metadata']['namespace'] = 'argocd'
        p = d['spec']['template']['spec']; p['serviceAccountName'] = 'synthetic'
        p['volumes'] = [{'name': 'tls', 'secret': {'secretName': 'argocd-dex-server-tls', 'optional': True,
                                                 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}]
        sa = obj('ServiceAccount', namespace='argocd')
        self.assertEqual(v.check_references([d, sa]), [])
        p['volumes'][0]['secret']['items'][0]['key'] = 'unapproved'
        self.hold('OPTIONAL_REFERENCE_KEY_UNDECLARED', v.check_references, [d, sa])

    def test_source_symlink_rejected_without_following(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for prefix in v.PREFIXES:
                (root/prefix).mkdir(parents=True)
            (root/v.PREFIXES[0]/'outside').symlink_to('/dev/null')
            self.hold('SOURCE_SYMLINK_FORBIDDEN', v.discover, root, {})

    def test_raw_configmap_and_removed_patch_literal_rejected(self):
        d = obj('ConfigMap'); d['data'] = {'DB_PASSWORD': 'synthetic-canary'}
        self.hold('RAW_CONFIGMAP_CREDENTIAL_FORBIDDEN', v.scan_raw_credentials, d)
        self.hold('RAW_INLINE_CREDENTIAL_FORBIDDEN', v.scan_raw_credentials,
                  {'spec': {'template': {'spec': {'containers': [{'env': [{'name': 'password', 'value': 'synthetic'}]}]}}}})

    def test_missing_or_widened_network_policy_rejected(self):
        d = obj('NetworkPolicy', 'default-deny'); d['spec'] = {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}
        contract = {'|'.join(v.identity(d)): v.canonical(d)}
        v.check_security_contract([d], contract)
        self.hold('APPROVED_NAMESPACE_RBAC_NETWORK_CONFIG_OR_STORAGE_CHANGED', v.check_security_contract, [], contract)
        d['spec']['egress'] = [{}]
        self.hold('APPROVED_NAMESPACE_RBAC_NETWORK_CONFIG_OR_STORAGE_CHANGED', v.check_security_contract, [d], contract)

    def test_alternate_kustomization_name_not_delegated_unchecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for prefix in v.PREFIXES:
                (root/prefix).mkdir(parents=True)
            (root/v.PREFIXES[0]/'Kustomization').write_text('kind: Kustomization\nsecretGenerator: []\n')
            self.hold('ALTERNATE_KUSTOMIZATION_NAME_FORBIDDEN', v.discover, root, {})

    def test_added_source_file_rejected_by_closed_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for prefix in v.PREFIXES:
                (root/prefix).mkdir(parents=True)
            (root/v.PREFIXES[0]/'hidden.txt').write_text('unreviewed')
            self.hold('APPROVED_GCP_SOURCE_SNAPSHOT_CHANGED', v.discover, root, {})


if __name__ == '__main__':
    unittest.main()
