"""Keep the AWS policy strict while routing exactly three GCP trees to their gate."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('aws_policy', Path(__file__).resolve().parents[1] / 'scripts/gitops_safety.py')
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class ScopeTests(unittest.TestCase):
    def test_delegate_scope_is_exact_reviewed_tuple(self):
        self.assertEqual(policy.GCP_SOURCE_PREFIXES, (
            'gitops/overlays/gcp/',
            'gitops/argocd/gcp/',
            'gitops/platform/gke-common/',
        ))

    def put(self, root, relative, body='kind: Namespace\nmetadata: {name: test}\n'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_existing_aws_and_unknown_trees_still_reject_extra_resources(self):
        for relative in ('gitops/overlays/aws/dev/injected.yaml', 'gitops/other/injected.yaml'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.put(root, relative)
                with self.assertRaises(policy.SafetyError):
                    policy.inspect_sources(root)

    def test_exact_gcp_roots_delegate_without_authorizing_deployment(self):
        for prefix in policy.GCP_SOURCE_PREFIXES:
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.put(root, prefix + 'test.yaml')
                self.assertEqual(policy.inspect_sources(root), 0)

    def test_neighboring_prefix_is_not_a_scope_escape(self):
        for prefix in ('gitops/argocd/gcp-unreviewed/', 'gitops/platform/gke-common-other/', 'gitops/overlays/gcp-evil/'):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.put(root, prefix + 'test.yaml')
                with self.assertRaises(policy.SafetyError):
                    policy.inspect_sources(root)

    def test_gcp_symlink_is_rejected_before_delegation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / 'outside.yaml'
            outside.write_text('kind: Namespace\n')
            link = root / 'gitops/overlays/gcp/link.yaml'
            link.parent.mkdir(parents=True)
            link.symlink_to(outside)
            with self.assertRaises(policy.SafetyError):
                policy.inspect_sources(root)


if __name__ == '__main__':
    unittest.main()
