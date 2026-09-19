# 07 — Node CPU and RAM capacity

## What Global Engineering needs

Per node: CPU and memory capacity, allocatable, how much is already requested by
existing workloads, and what the real peak utilisation is. Datamover pods are scheduled
onto these nodes during the backup window, so the figure that matters is not capacity —
it is **headroom at the time the backup runs**.

## Setup

```bash
. lib/init.sh
```

Sourcing `lib/init.sh` exports `K10NS`, `AUDIT_DIR`, `CLUSTER_UID`, the metrics window
(`AUDIT_WINDOW_DAYS`, `AUDIT_START`, `AUDIT_END`, `AUDIT_RANGE`) and the query helpers
(`tq`, `tqr`, `kq`, `pf_start`, `pf_stop`). It is idempotent — run it at the start of
every guide and in every new terminal. See [00-prerequisites.md](00-prerequisites.md).

## Method

```bash
mkdir -p "$AUDIT_DIR/07-node-cpu-memory" && cd "$AUDIT_DIR/07-node-cpu-memory"
```

### Step 1 — capacity and allocatable

```bash
kubectl get nodes -o json > nodes-raw.json

jq -r '(["NODE","ROLE","CPU_CAP","CPU_ALLOC","MEM_CAP_GiB","MEM_ALLOC_GiB","MAX_PODS","KUBELET"]|@tsv),
       (.items[]
        | [ .metadata.name,
            ( [ .metadata.labels | keys[] | select(startswith("node-role.kubernetes.io/"))
                | sub("node-role.kubernetes.io/";"") ] | join(",") ),
            .status.capacity.cpu,
            .status.allocatable.cpu,
            ((.status.capacity.memory   | rtrimstr("Ki") | tonumber)/1048576*100|round/100),
            ((.status.allocatable.memory| rtrimstr("Ki") | tonumber)/1048576*100|round/100),
            .status.capacity.pods,
            .status.nodeInfo.kubeletVersion ] | @tsv)' nodes-raw.json \
  | tee node-capacity.tsv | column -t
```

Validated output:

```
NODE                                   ROLE                  CPU_CAP  CPU_ALLOC  MEM_CAP_GiB  MEM_ALLOC_GiB  MAX_PODS
master-0                               control-plane,master  8        7500m      31.34        30.24          250
worker-1                               worker                16       15500m     62.79        61.69          250
```

The gap between capacity and allocatable is kube/system-reserved: 500 m CPU and ~1.1
GiB on these nodes. Never size against capacity.

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

Narrow the bounds to the backup window (from guide 02 step 4) and re-run. The difference
between the whole-window peak and the backup-window peak is the datamover's actual
footprint on the cluster.

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
jq -r '.items[] | select(.spec.taints)
       | "\(.metadata.name)\t\([.spec.taints[] | "\(.key)=\(.value // "")\(.effect)"] | join(","))"' \
   nodes-raw.json | tee node-taints.tsv

# K10 worker-pod placement, if configured
kubectl -n "$K10NS" get actionpodspecs -o yaml 2>/dev/null \
  | grep -A20 -E "nodeSelector|affinity|tolerations" | tee datamover-placement.txt
```

## Caveats

- **Allocatable ≠ available.** Subtract existing requests (step 2) before claiming
  headroom.
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

Steps 1, 2, 4 and 5 fully validated on the validation cluster: 9 nodes, 3 masters at
8 CPU / 31.3 GiB and 6 workers at 16 CPU / 62.8 GiB;
`kube_node_status_capacity` and `kube_node_status_allocatable` each returned 72 series.
The step 2 PromQL returned sensible commitment figures — workers between 32.4 % and
71.9 % of allocatable CPU already requested, masters between 62.8 % and 75.5 %.

Step 3's dependencies were confirmed present on OpenShift 4.18:
`instance:node_cpu_utilisation:rate1m` and `node_memory_MemAvailable_bytes` both
resolved to 9 series. The queries were re-validated using `AUDIT_START`/`AUDIT_END` from
guide 00 §6 (15 days on that cluster). The queries are validated; the trend
interpretation is not, since the validation cluster carried no sustained load.
