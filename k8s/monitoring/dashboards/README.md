# Dashboards

Every dashboard Grafana serves lives here. They are rendered into ConfigMaps
labelled `grafana_dashboard: "1"` by `kustomization.yaml` and picked up by the
Grafana sidecar. The chart's own bundled dashboards are disabled
(`infra/monitoring/values-grafana.yaml`), so this folder is the only source.

## Vendored from grafana.com

| File | ID | Revision |
| --- | --- | --- |
| `node-exporter-full.json` | [1860](https://grafana.com/grafana/dashboards/1860) | 45 |
| `nvidia-dcgm-exporter.json` | [12239](https://grafana.com/grafana/dashboards/12239) | 2 |
| `kubernetes-views-global.json` | [15757](https://grafana.com/grafana/dashboards/15757) | 43 |
| `kubernetes-views-namespaces.json` | [15758](https://grafana.com/grafana/dashboards/15758) | 46 |
| `kubernetes-views-nodes.json` | [15759](https://grafana.com/grafana/dashboards/15759) | 40 |
| `kubernetes-views-pods.json` | [15760](https://grafana.com/grafana/dashboards/15760) | 39 |

`llm-inference.json` is maintained here, not vendored.

`ai-gateway-overview.json` is maintained here: requests, latency and token
throughput from the AI Gateway extProc's `gen_ai_*` Prometheus metrics.

`genai-conversations.json` is maintained here: TraceQL views over the GenAI spans
the extProc exports to Tempo (see `infra/monitoring/values-tempo.yaml`).
Conversations are grouped by `session.id`, which the gateway copies from the
`agent-session-id` request header.

`k6-benchmark.json` is maintained here too: it visualizes results pushed by
`scripts/run-benchmark.sh` (see `benchmark/README.md`) via Prometheus
remote-write, keyed by the `model_id`, `model_config` and `run_id` labels.

## Refreshing a vendored dashboard

Download the revision, then strip the import prompts and bind the datasource to
the provisioned `prometheus` uid — otherwise every panel renders empty:

```bash
ID=1860 REV=46 OUT=node-exporter-full.json
curl -sf "https://grafana.com/api/dashboards/$ID/revisions/$REV/download" \
  | sed -E 's/\$\{DS_[A-Z0-9_-]+\}/prometheus/g' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); [d.pop(k,None) for k in ("__inputs","__requires")]; d["id"]=None; print(json.dumps(d,indent=2,sort_keys=True))' \
  > "$OUT"
```

Bump the revision in the table above, then apply:

```bash
./infra/monitoring/install-monitoring.sh --platform dgx-spark --dashboards-only
```

## Adding a dashboard

Drop the JSON here and add a `configMapGenerator` entry. Keep each file under
1 MiB — that is the ConfigMap size limit, and `node-exporter-full.json` is
already about half of it.
