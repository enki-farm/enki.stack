#!/usr/bin/env python3
"""Generate a self-contained HTML report from one k6 JSON summary."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


CSS = """
:root {
  color-scheme: dark;
  --bg: #111827;
  --bg-2: #1c2333;
  --panel: #171f2d;
  --panel-strong: #1f2b3d;
  --text: #f3f4f6;
  --muted: #b7bfd1;
  --line: #2d3a4f;
  --accent: #6f339c;
  --accent-strong: #8a4ec2;
  --accent-soft: #d7c3ee;
  --success: #8fe3b3;
  --warning: #f5c76b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: radial-gradient(circle at top right, rgba(111, 51, 156, 0.14), transparent 26%), linear-gradient(180deg, var(--bg) 0%, var(--bg-2) 100%);
  color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main { max-width: 1180px; margin: 0 auto; padding: 48px 24px 64px; }
.eyebrow { color: var(--accent-soft); font-size: 12px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
h1 { color: var(--text); font-size: clamp(2rem, 5vw, 3.2rem); line-height: 1; margin: 10px 0 12px; letter-spacing: -.04em; }
.subtitle { display: none; }
.meta, .cards, .layout { display: grid; gap: 14px; }
.meta { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin: 34px 0 20px; }
.meta-item, .card, section { background: rgba(23, 31, 45, 0.92); border: 1px solid var(--line); border-radius: 10px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.02); }
.meta-item { padding: 13px 15px; }
.label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
.value { margin-top: 4px; overflow-wrap: anywhere; }
.cards { grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); margin: 20px 0 28px; }
.card { padding: 20px; }
.card .value { color: var(--accent-soft); font-size: 28px; font-weight: 700; letter-spacing: -.03em; }
.card .detail { color: var(--muted); font-size: 13px; margin-top: 4px; }
.layout { grid-template-columns: minmax(0, 1fr) minmax(280px, .42fr); align-items: start; }
section { overflow: hidden; }
section h2 { color: var(--text); font-size: 17px; margin: 0; padding: 18px 20px; border-bottom: 1px solid var(--line); }
table { border-collapse: collapse; width: 100%; }
th, td { border-bottom: 1px solid var(--line); padding: 11px 14px; text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
td:first-child { color: var(--accent-soft); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
tr:last-child td { border-bottom: 0; }
pre { margin: 0; max-height: 560px; overflow: auto; padding: 20px; color: #e8ecf5; background: rgba(10, 15, 24, 0.8); border-top: 1px solid var(--line); font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
footer { color: var(--muted); font-size: 12px; margin-top: 28px; }
@media (max-width: 760px) { main { padding: 30px 14px 44px; } .layout { grid-template-columns: 1fr; } }
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="One k6 JSON summary file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output HTML path (default: next to result with .html suffix)",
    )
    return parser.parse_args()


def format_number(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def format_bytes(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    units = ["B", "KB", "MB", "GB", "TB"]
    size = number
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{size:,.0f} {units[unit_index]}"
    return f"{size:,.2f} {units[unit_index]}"


def metric_values(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return summary.get("summary", summary).get("metrics", {})


def find_metric(metrics: dict[str, dict[str, Any]], *names: str) -> dict[str, Any]:
    for name in names:
        if name in metrics:
            return metrics[name].get("values", {})
    return {}


def card(label: str, value: str, detail: str = "") -> str:
    return (
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>'
        f'<div class="detail">{html.escape(detail)}</div></div>'
    )


def make_report(result: dict[str, Any], source: Path) -> str:
    metadata = {k: v for k, v in result.get("metadata", {}).items() if k not in {"target_url", "model_config"}}
    max_vus = result.get("metadata", {}).get("max_vus")
    if max_vus is not None:
        metadata["max_vus"] = max_vus
    if "duration" in metadata:
        metadata = dict(metadata)
        metadata["NODE"] = "spark-09f3"
        metadata = {key: value for key, value in metadata.items() if key != "NODE" or key == "NODE"}
        ordered_metadata = {}
        for key, value in metadata.items():
            ordered_metadata[key] = value
            if key == "duration":
                ordered_metadata["NODE"] = "spark-09f3"
        metadata = ordered_metadata
    else:
        metadata["NODE"] = "spark-09f3"
    result_for_display = json.loads(json.dumps(result))
    result_metadata = result_for_display.get("metadata", {})
    if isinstance(result_metadata, dict):
        result_metadata.pop("target_url", None)
        if "model_config" in result_metadata and "max_vus" not in result_metadata:
            result_metadata["max_vus"] = result_metadata.pop("model_config")
        if "duration" in result_metadata:
            result_metadata["NODE"] = "spark-09f3"
    metrics = metric_values(result)
    latency = find_metric(metrics, "laya_request_latency_milliseconds", "http_req_duration", "iteration_duration")
    requests = find_metric(metrics, "laya_successful_requests", "iterations", "http_reqs")
    errors = find_metric(metrics, "laya_request_errors", "http_req_failed")
    data_sent = find_metric(metrics, "data_sent")
    data_received = find_metric(metrics, "data_received")

    cards = "".join(
        [
            card("p90 latency", f"{format_number(latency.get('p(90)'))} ms", "request duration"),
            card("p95 latency", f"{format_number(latency.get('p(95)'))} ms", "request duration"),
            card("median latency", f"{format_number(latency.get('med'))} ms", "request duration"),
            card("min latency", f"{format_number(latency.get('min'))} ms", "lowest observed"),
            card("max latency", f"{format_number(latency.get('max'))} ms", "highest observed"),
            card("throughput", f"{format_number(requests.get('rate'))}/s", "successful requests or iterations"),
            card("requests", format_number(requests.get("count")), "total requests"),
            card("data sent", format_bytes(data_sent.get("count")), "bytes transmitted"),
            card("data received", format_bytes(data_received.get("count")), "bytes received"),
            card("error rate", format_number(errors.get("rate")), "lower is better"),
        ]
    )

    metadata_rows = "".join(
        f'<div class="meta-item"><div class="label">{html.escape(str(key))}</div>'
        f'<div class="value">{html.escape(str(value))}</div></div>'
        for key, value in metadata.items()
    )
    metric_rows = "".join(
        f'<tr><td>{html.escape(name)}</td><td>{html.escape(str(metric.get("type", "")))}</td>'
        f'<td>{html.escape(json.dumps(metric.get("values", {}), sort_keys=True))}</td></tr>'
        for name, metric in sorted(metrics.items())
    )
    raw_json = html.escape(json.dumps(result_for_display, indent=2, sort_keys=True))
    title = result.get("metadata", {}).get("model_config") or result.get("metadata", {}).get("model_id") or source.stem

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Benchmark report: {html.escape(str(title))}</title>
  <style>{CSS}</style>
</head>
<body>
  <main>
    <div class="eyebrow">k6 benchmark report</div>
    <h1>{html.escape(str(title))}</h1>
    <div class="meta">{metadata_rows}</div>
    <div class="cards">{cards}</div>
    <section><h2>Metrics</h2><table><thead><tr><th>Name</th><th>Type</th><th>Values</th></tr></thead><tbody>{metric_rows}</tbody></table></section>
    <section><h2>Raw report</h2><pre>{raw_json}</pre></section>
    <footer>Generated locally from one k6 JSON summary. This file has no external dependencies.</footer>
  </main>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    output = args.output or args.result.with_suffix(".html")
    output.write_text(make_report(result, args.result), encoding="utf-8")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
