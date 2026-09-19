# Exported namespace report

```bash
./exported_namespace.sh > exported-namespaces.json
./exported_namespace.sh --namespace cpd --context my-cluster > cpd-exports.json
```

Requires Bash, Python 3 (standard library only), and configured kubectl. Reads
ExportActions and Policies across all namespaces plus Namespace labels. RBAC must
allow these lists. No cluster resources are created or changed. Progress goes to
stderr, JSON to stdout. Request errors exit 1 without producing a report;
`--timeout` defaults to 180 seconds per request.

The `namespaces` array contains application namespaces with retained ExportAction
records, including failed and running actions. Application namespace comes from
`k10.kasten.io/appNamespace` or `spec.subject.namespace`, not the action's own
resource namespace, which may be `kasten-io`.

Each entry includes action counts by state, action references, and related policies:

- `association: action_policy_labels`: the action's policy name and namespace labels
  identify the originating policy. Deleted policies remain listed with null schedules.
- `association: current_selector_candidate`: a current policy with an export step
  selects that namespace. This is a candidate, not confirmed historical attribution.
- `frequency` and `subFrequency`: current policy schedule, preserved from the API.
- `export_actions`: every export step, with declared and effective export frequency,
  destination profile, and export-data flag. An omitted export frequency means every
  snapshot, so the effective frequency inherits the policy frequency.

Paused policies remain visible. Policy edits mean current schedules and selectors
may differ from those at export time. Policies selected only through VM or unsupported
selectors appear in `unresolved_selector_policies`; namespace labels for deleted
namespaces cannot be evaluated. Unlabeled manual actions remain in the report;
missing policy namespace labels are not guessed. Actions with no resolvable application
namespace appear in `unresolved_export_actions`, including when filtering namespaces.
Only retained history is available; garbage-collected actions cannot be reported.
This is an observed export report, not a list of all namespaces configured for future exports.

Schema references: [Kasten Actions](https://docs.kasten.io/8.5.0/api/actions/) and
[Kasten Policies](https://docs.kasten.io/8.5.8/api/policies/).
