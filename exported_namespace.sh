#!/usr/bin/env bash
# Python standard library only; kubectl supplies cluster access.
set -euo pipefail
exec python3 - "$@" <<'PY'
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import shutil
import subprocess
import sys

APP_NS = 'k10.kasten.io/appNamespace'
PREFIX = 'k10.kasten.io/'


def name_matches(name, pattern):
    return pattern == '*' or (name.startswith(pattern[:-1]) if pattern.endswith('*') else name == pattern)


def selector_matches(selector, name, labels):
    """All selector requirements must match. None means unsupported selector."""
    if not selector:
        return None
    requirements = list(selector.get('matchExpressions', []))
    requirements += [{'key': key, 'operator': 'In', 'values': [value]}
                     for key, value in selector.get('matchLabels', {}).items()]
    unknown = False
    for req in requirements:
        key, op, values = req['key'], req['operator'], req.get('values', [])
        if key.startswith(PREFIX) and key != APP_NS:
            unknown = True
            continue
        if key == APP_NS:
            present = True
            hit = any(name_matches(name, value) for value in values)
        elif labels is None:
            unknown = True
            continue
        else:
            present = key in labels
            hit = present and labels[key] in values
        if op == 'In':
            matched = hit
        elif op == 'NotIn':
            matched = not hit
        elif op == 'Exists':
            matched = present
        elif op == 'DoesNotExist':
            matched = not present
        else:
            unknown = True
            continue
        if not matched:
            return False
    return None if unknown else True


def policy_summary(policy):
    meta, spec = policy['metadata'], policy.get('spec', {})
    exports = []
    for action in spec.get('actions', []):
        if action.get('action') != 'export':
            continue
        params = action.get('exportParameters', {})
        exports.append({'frequency': params.get('frequency'),
                        'effective_frequency': params.get('frequency') or spec.get('frequency'),
                        'frequency_source': 'exportParameters' if params.get('frequency') else 'every_snapshot',
                        'profile': params.get('profile'),
                        'export_data_enabled': params.get('exportData', {}).get('enabled')})
    return {'name': meta['name'], 'namespace': meta['namespace'], 'uid': meta.get('uid'),
            'policy_found': True, 'frequency': spec.get('frequency'),
            'subFrequency': spec.get('subFrequency'), 'paused': spec.get('paused', False),
            'export_actions': exports}


def build_report(actions, policies, namespaces):
    policy_map = {(p['metadata']['namespace'], p['metadata']['name']): p for p in policies}
    labels = {n['metadata']['name']: n['metadata'].get('labels', {}) for n in namespaces}
    groups, unresolved = {}, []
    for action in actions:
        meta, spec = action['metadata'], action.get('spec', {})
        tags = meta.get('labels', {})
        ns = tags.get(APP_NS) or spec.get('subject', {}).get('namespace')
        pname, pns = tags.get(PREFIX + 'policyName'), tags.get(PREFIX + 'policyNamespace')
        item = {'name': meta['name'], 'resource_namespace': meta.get('namespace'),
                'state': action.get('status', {}).get('state'),
                'created_at': meta.get('creationTimestamp'),
                'policy_reference': {'name': pname, 'namespace': pns} if pname else None}
        if not ns:
            unresolved.append(item)
            continue
        groups.setdefault(ns, []).append(item)
    rows = []
    for ns, exports in sorted(groups.items()):
        attributed = Counter((a['policy_reference']['namespace'], a['policy_reference']['name'])
                             for a in exports if a['policy_reference'] and a['policy_reference']['namespace'])
        related, unresolved_selectors = [], []
        for key, policy in sorted(policy_map.items()):
            summary = policy_summary(policy)
            match = selector_matches(policy.get('spec', {}).get('selector'), ns, labels.get(ns))
            if key in attributed or (summary['export_actions'] and match is True):
                summary.update(association='action_policy_labels' if key in attributed else 'current_selector_candidate',
                               attributed_export_action_count=attributed.get(key, 0))
                related.append(summary)
            elif summary['export_actions'] and match is None:
                unresolved_selectors.append({'name': key[1], 'namespace': key[0]})
        for key, count in sorted(attributed.items()):
            if key not in policy_map:
                related.append({'name': key[1], 'namespace': key[0], 'policy_found': False,
                                'association': 'action_policy_labels', 'attributed_export_action_count': count,
                                'frequency': None, 'subFrequency': None, 'export_actions': None})
        rows.append({'namespace': ns, 'export_action_count': len(exports),
                     'states': dict(Counter(a['state'] or 'unknown' for a in exports)),
                     'policies': related, 'unresolved_selector_policies': unresolved_selectors,
                     'export_actions': exports})
    return {'generated_at': datetime.now(timezone.utc).isoformat(),
            'notes': ['Only namespaces with retained ExportActions are included; history may be garbage-collected.',
                      'All action states are included; presence does not imply a successful export.',
                      'Schedules are current policy settings, not necessarily the settings when an action ran.',
                      'Selector candidates are not proof of origin; paused policies remain visible.',
                      'VM or unsupported selectors and missing namespace labels are reported as unresolved.',
                      'Missing export frequency means every snapshot; missing subFrequency is preserved as null.'],
            'namespaces': rows, 'unresolved_export_actions': unresolved}


def main():
    parser = argparse.ArgumentParser(prog='exported_namespace.sh',
                                     description='Report namespaces with retained Kasten ExportActions as JSON.')
    parser.add_argument('--context', help='kubectl context (default: current)')
    parser.add_argument('--namespace', '-n', help='Filter application namespace, not action resource namespace')
    parser.add_argument('--timeout', type=int, default=180, help='Seconds per kubectl request')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if not shutil.which('kubectl'):
        parser.error('kubectl is required')
    base = ['kubectl'] + (['--context', args.context] if args.context else [])
    def get(resource, all_namespaces=True):
        print('Reading ' + resource, file=sys.stderr)
        command = base + ['get', resource] + (['-A'] if all_namespaces else []) + ['-o', 'json']
        response = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
        if response.returncode:
            raise RuntimeError(response.stderr.strip() or 'kubectl failed')
        return json.loads(response.stdout)['items']
    actions = get('exportactions.actions.kio.kasten.io')
    policies = get('policies.config.kio.kasten.io')
    namespaces = get('namespaces', False)
    report = build_report(actions, policies, namespaces)
    if args.namespace:
        report['namespaces'] = [row for row in report['namespaces'] if row['namespace'] == args.namespace]
    json.dump(report, sys.stdout, indent=2)
    print()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print('exported_namespace: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
PY
