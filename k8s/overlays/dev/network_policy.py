"""Render DEV-only egress policies using independently approved, resolved RDS IPs.

Runtime IPs are deployment inputs, not public repository defaults. This module
has no Kubernetes client or DNS side effects; callers must verify CNI enforcement
and the approved DB hostname, then store its output outside the repository.
"""
import ipaddress

DEV_NAMESPACES = ('mlflow-dev', 'datacenter-app-dev')


def policies(rds_addresses, cluster_cidrs):
    if not rds_addresses or not cluster_cidrs:
        raise ValueError('RDS addresses and cluster ranges are required')
    blocked = [ipaddress.ip_network(c, strict=True) for c in cluster_cidrs]
    addresses = sorted({ipaddress.IPv4Address(a) for a in rds_addresses})
    for address in addresses:
        if (address.is_unspecified or address.is_loopback or address.is_link_local or
                address.is_multicast or any(address in net for net in blocked)):
            raise ValueError('RDS address overlaps a prohibited or cluster address')
    dns = {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
           'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}
    dev = {'namespaceSelector': {'matchExpressions': [
        {'key': 'kubernetes.io/metadata.name', 'operator': 'In', 'values': list(DEV_NAMESPACES)}]}}
    egress = [
        {'to': [dns], 'ports': [{'protocol': p, 'port': 53} for p in ('UDP', 'TCP')]},
        {'to': [dev], 'ports': [{'protocol': 'TCP', 'port': p} for p in (5000, 8000)]},
        {'to': [{'ipBlock': {'cidr': str(a) + '/32'}} for a in addresses],
         'ports': [{'protocol': 'TCP', 'port': 3306}]},
    ]
    return [{'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
             'metadata': {'name': 'dev-service-isolation', 'namespace': namespace},
             'spec': {'podSelector': {}, 'policyTypes': ['Egress'], 'egress': egress}}
            for namespace in DEV_NAMESPACES]
