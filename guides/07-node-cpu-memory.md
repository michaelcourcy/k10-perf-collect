# 07 — Node CPU and RAM capacity

## What auditors need

Per node: CPU and memory capacity, allocatable, how much is already requested by
existing workloads, and what the real peak utilisation is. Datamover pods are scheduled
onto these nodes during the backup window, so the figure that matters is not capacity —
it is **headroom at the time the backup runs**.

## Method

```bash
. lib/init.sh
focus_dir 07-node-cpu-memory
```

### Step 1 — capacity, allocatable and current usage in one table

```bash
node_snapshot | tee node-snapshot.tsv | column -t
node_totals   | tee node-totals.txt
```

Validated:

```
NODE      ROLES                 SOURCE         CPU_ALLOC  CPU_USED  CPU_PCT  MEM_ALLOC_GiB  MEM_USED_GiB  MEM_PCT  EPH_CAP_GiB  EPH_USED_GiB  EPH_PCT  PRESSURE
worker-1  worker                stats/summary  15.5       1.55      10       61.69          23.69         38.4     511.25       167.44        32.8     -
worker-4  worker                stats/summary  15.5       1.21      7.8      61.69          18.47         29.9     511.25       424.95        83.1     -
master-0  control-plane,master  stats/summary  7.5        1.61      21.5     30.24          17.53         58       462.94       203.82        44       -
```

```
nodes counted     : 6 (workers)
cpu               : 8.61 of 93.00 cores allocatable in use (9.3%)
memory            : 130.8 of 370.1 GiB allocatable in use (35.3%)
ephemeral storage : 1037.8 of 3067.5 GiB in use (33.8%)
headroom          : 84.39 cores, 239.4 GiB RAM, 2029.7 GiB ephemeral
```

`node_snapshot` ([../lib/nodes.sh](../lib/nodes.sh)) is the same collection
`generate-export-topology.py` puts in its report header. One call per node to
`/api/v1/nodes/<node>/proxy/stats/summary` returns CPU, working-set memory **and** the
root filesystem, which is the ephemeral storage guide 08 is about; `metrics.k8s.io` is
the fallback and gives CPU and memory only.

`node_totals` counts **workers only** by default. Masters are normally tainted
`NoSchedule`, so counting their cores as datamover capacity overstates headroom — pass
`all` if the taints say otherwise (step 4).

> **This is one sample, not an average.** It says what the cluster looked like when you
> ran it. Take it *while an export is in flight* and it tells you what the export costs;
> take it at midday and it tells you nothing about the backup window. Step 3 is the
> trend.

> **Unit trap.** `status.capacity["ephemeral-storage"]` is a `Ki` quantity
> (`"536083696Ki"`) while `status.allocatable["ephemeral-storage"]` is a bare byte count
> (`"492980991592"`). Comparing them as strings, or as the same unit, gives a 1024×
> error. `node_snapshot` parses quantities; if you write your own, check the suffix.

### Step 2 — what is already committed

This is the number the scheduler uses. A node can be at 95 % requested CPU and 10 %
actual utilisation, and a datamover with a CPU request will still not fit.

```bash
kubectl describe nodes | awk '
  /^Name:/            {node=$2}
  /Allocated resources/ {inres=1}
  inres && /cpu  */   {printf "%s\tcpu\t%s %s\n", node, $2, $3; }
  inres && /memory/   {printf "%s\tmemory\t%s %s\n", node, $2, $3; inres=0 }' \
  | tee node-allocated.tsv | column -t
```

Cleaner, from cluster monitoring:

```bash
tq 'sum by (node) (kube_pod_container_resource_requests{resource="cpu"}) / on(node) group_left
     kube_node_status_allocatable{resource="cpu"}' \
  | jq -r '(["NODE","CPU_REQUESTED_FRACTION"]|@tsv),
           (.data.result[] | [.metric.node, (.value[1]|tonumber*1000|round/10|tostring + "%")] | @tsv)' \
  | tee cpu-requested-pct.tsv | column -t

tq 'sum by (node) (kube_pod_container_resource_requests{resource="memory"}) / on(node) group_left
     kube_node_status_allocatable{resource="memory"}' \
  | jq -r '(["NODE","MEM_REQUESTED_FRACTION"]|@tsv),
           (.data.result[] | [.metric.node, (.value[1]|tonumber*1000|round/10|tostring + "%")] | @tsv)' \
  | tee mem-requested-pct.tsv | column -t
```

### Step 3 — real utilisation over the effective window, and during the backup window

Headroom, not capacity, is what the recommendation depends on.

```bash
# AUDIT_START / AUDIT_END are exported by lib/init.sh. On a cluster whose Prometheus
# has no PV the real window may be far shorter than 7 days - check the value.
echo "window: ${AUDIT_WINDOW_DAYS}d  ($AUDIT_START -> $AUDIT_END)"

# peak and mean CPU utilisation per node
tqr 'instance:node_cpu_utilisation:rate1m' "$AUDIT_START" "$AUDIT_END" 5m \
  | jq -r '(["NODE","MEAN_CPU_PCT","PEAK_CPU_PCT"]|@tsv),
           (.data.result[] | (.values|map(.[1]|tonumber)) as $v
            | [ (.metric.instance // .metric.node),
                (($v|add)/($v|length)*1000|round/10),
                (($v|max)*1000|round/10) ] | @tsv)' \
  | tee "cpu-utilisation-${AUDIT_WINDOW_DAYS}d.tsv" | column -t

# memory: working set as a fraction of allocatable
tqr '1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)' \
    "$AUDIT_START" "$AUDIT_END" 5m \
  | jq -r '(["INSTANCE","MEAN_MEM_PCT","PEAK_MEM_PCT"]|@tsv),
           (.data.result[] | (.values|map(.[1]|tonumber)) as $v
            | [ .metric.instance,
                (($v|add)/($v|length)*1000|round/10),
                (($v|max)*1000|round/10) ] | @tsv)' \
  | tee "mem-utilisation-${AUDIT_WINDOW_DAYS}d.tsv" | column -t
```

Narrow the bounds to one of the pair's own export windows and re-run — the difference
between the whole-window peak and the export-window peak is what the datamover costs:

```bash
export_actions | head -3 | column -t     # pick an export, note START and END
S=$(date -u -j -f '%Y-%m-%dT%H:%M:%S' '2026-09-20T08:00:52' +%s)   # GNU date: date -u -d '...' +%s
E=$(date -u -j -f '%Y-%m-%dT%H:%M:%S' '2026-09-20T08:04:45' +%s)
tqr 'instance:node_cpu_utilisation:rate1m' "$S" "$E" 15s \
  | jq -r '.data.result[] | (.values|map(.[1]|tonumber)) as $v
           | [ (.metric.instance // .metric.node), (($v|max)*1000|round/10) ] | @tsv' \
  | tee cpu-during-export.tsv | column -t
```

If `instance:node_cpu_utilisation:rate1m` is not present (it is an OpenShift recording
rule), use the portable form:

```bash
tqr '1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))' \
    "$AUDIT_START" "$AUDIT_END" 5m
```

### Step 4 — node pool shape

Heterogeneous nodes change the recommendation: a datamover landing on an 8-core master
behaves differently from one on a 16-core worker.

```bash
jq -r '[.items[] | {cpu: .status.capacity.cpu, mem: .status.capacity.memory,
                    instance: (.metadata.labels["node.kubernetes.io/instance-type"] // "-"),
                    zone: (.metadata.labels["topology.kubernetes.io/zone"] // "-"),
                    role: ([.metadata.labels|keys[]|select(startswith("node-role.kubernetes.io/"))|sub("node-role.kubernetes.io/";"")]|join(","))}]
       | group_by([.cpu,.mem,.instance,.role])
       | map({count: length, cpu: .[0].cpu, mem: .[0].mem, instance: .[0].instance, role: .[0].role,
              zones: (map(.zone)|unique|join(","))})
       | (["COUNT","CPU","MEM","INSTANCE_TYPE","ROLE","ZONES"]|@tsv),
         (.[] | [.count,.cpu,.mem,.instance,.role,.zones]|@tsv)' nodes-raw.json \
  | tee node-pools.tsv | column -t
```

### Step 5 — where datamovers are actually allowed to run

Taints, and any K10 node affinity, decide which of the above nodes are real candidates.

```bash
kubectl get nodes -o json > nodes-raw.json
jq -r '.items[] | select(.spec.taints)
       | "\(.metadata.name)\t\([.spec.taints[] | "\(.key)=\(.value // "")\(.effect)"] | join(","))"' \
   nodes-raw.json | tee node-taints.tsv

# Where the datamovers of THIS namespace may run. An ActionPodSpecBinding in the
# application namespace is what overrides placement for it - see guide 10 step 5.
kubectl -n "$AUDIT_NS" get actionpodspecbindings -o yaml 2>/dev/null | tee aps-binding.yaml
kubectl -n "$K10NS" get actionpodspecs -o yaml 2>/dev/null | tee actionpodspecs.yaml
```

Validated: both empty on this cluster, so datamovers for `$AUDIT_NS` are scheduled with
no nodeSelector, no affinity and no tolerations — they land wherever the scheduler puts
them, and never on a tainted master.

Where they actually landed on the last export, which is the real answer:

```bash
datamover_for_export "$(export_actions | awk -F'\t' 'NR==2 {print $1}')" \
  | tee datamover-placement.txt
```

## Caveats

- **Allocatable ≠ available.** Subtract existing requests (step 2) before claiming
  headroom. `node_totals` reports allocatable-versus-used, which is a different and
  more optimistic number than allocatable-versus-requested.
- **One sample is not a trend.** Step 1 is an instant; step 3 is the window. Never
  present step 1 as "the node is fine".
- **Masters usually cannot host datamovers** because of the
  `node-role.kubernetes.io/master:NoSchedule` taint. Exclude them from the headroom
  calculation unless step 5 shows a matching toleration. On the validation cluster the
  three 8-core masters were tainted, leaving six 16-core workers as the real capacity.
- **The window is not whatever you ask for.** It is `AUDIT_WINDOW_DAYS` from guide 00
  §6 — the smaller of Prometheus retention and, when Prometheus has no persistent
  volume, the longest-running replica's uptime. Label every figure in this guide with
  that window; a "mean CPU" over 2 days and over 15 days are not comparable numbers.
- `node_memory_MemAvailable_bytes` is node-exporter; if node-exporter is absent, fall
  back to `sum by (node) (container_memory_working_set_bytes{container!=""})` over
  `kube_node_status_allocatable{resource="memory"}`, which is close enough.
- Autoscaled node pools make "per node" figures unstable. Report the pool shape from
  step 4 and the min/max pool size alongside.

## What to send back

| File | Contents |
|------|----------|
| `node-capacity.tsv` | capacity, allocatable, max pods, kubelet version per node |
| `node-pools.tsv` | node pool shape, collapsed by identical spec |
| `cpu-requested-pct.tsv`, `mem-requested-pct.tsv` | scheduler commitment per node |
| `cpu-utilisation-<N>d.tsv`, `mem-utilisation-<N>d.tsv` | mean and peak real utilisation over `AUDIT_WINDOW_DAYS` |
| `node-taints.tsv`, `datamover-placement.txt` | which nodes datamovers can actually use |

## Validation status

Steps 1, 2, 4 and 5 fully validated on K10 9.0.5 / OpenShift 4.18: 9 nodes, 3 masters at
7.5 allocatable cores / 30.24 GiB and 6 workers at 15.5 / 61.69. `node_snapshot`
returned `stats/summary` as the usage source for every node, and its capacity and
allocatable figures match `generate-export-topology.py`'s `nodes` block. Worker
headroom at the time of sampling: 84.39 cores and 239.4 GiB.

One worker reported **83.1 %** ephemeral-storage use against the others' 18–33 % — the
kind of imbalance that decides where a datamover must not be scheduled. Guide 08 step 4
turns that into a margin-to-eviction figure.

Step 2's PromQL returned sensible commitment figures. Step 3's dependencies were
confirmed present: `instance:node_cpu_utilisation:rate1m` and
`node_memory_MemAvailable_bytes` both resolve. Step 5 returned empty `ActionPodSpec`
and `ActionPodSpecBinding` lists, consistent with the `resources: {}` observed on live
datamover pods in guide 09.

Not validated: the trend interpretation itself. The queries are correct; the validation
cluster carried no sustained load outside the export windows, so a "node is saturated
during backups" finding was not reproduced.
