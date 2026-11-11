"""Prometheus exporter for per-process NVIDIA compute memory."""

from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
from collections.abc import Callable, Iterable

from prometheus_client import start_http_server
from prometheus_client.core import CollectorRegistry, GaugeMetricFamily

LOGGER = logging.getLogger(__name__)
PORT = 9400
QUERY = [
    "nvidia-smi",
    "--query-compute-apps=pid,process_name,used_memory",
    "--format=csv,noheader,nounits",
]


def parse_processes(output: str) -> list[tuple[str, str, int]]:
    processes = []
    rows = csv.reader(io.StringIO(output), skipinitialspace=True)
    for row_number, row in enumerate(rows, start=1):
        if not row or all(not field.strip() for field in row):
            continue
        if len(row) != 3:
            raise ValueError(f"expected 3 fields on row {row_number}, got {len(row)}")

        pid, process_name, used_memory = (field.strip() for field in row)
        try:
            pid_number = int(pid)
            memory_mib = int(used_memory)
        except ValueError as exc:
            raise ValueError(f"invalid pid or used_memory on row {row_number}") from exc
        if pid_number < 0 or memory_mib < 0:
            raise ValueError(f"negative pid or used_memory on row {row_number}")
        processes.append((str(pid_number), process_name or "unknown", memory_mib * 1024**2))
    return processes


class NvidiaProcessCollector:
    def __init__(
        self,
        node: str,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.node = node
        self.command_runner = command_runner

    def collect(self) -> Iterable[GaugeMetricFamily]:
        memory = GaugeMetricFamily(
            "nvidia_compute_app_used_memory_bytes",
            "Memory used by an NVIDIA compute process, in bytes.",
            labels=["node", "pid", "process_name"],
        )
        scrape_error = 0
        try:
            result = self.command_runner(
                QUERY,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            for pid, process_name, used_memory_bytes in parse_processes(result.stdout):
                memory.add_metric([self.node, pid, process_name], used_memory_bytes)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            scrape_error = 1
            LOGGER.warning("nvidia-smi collection failed on node %s: %s", self.node, exc)

        error = GaugeMetricFamily(
            "nvidia_smi_scrape_error",
            "Whether the latest nvidia-smi collection failed (1 means failure).",
            labels=["node"],
        )
        error.add_metric([self.node], scrape_error)
        yield memory
        yield error


def main() -> None:
    node = os.environ.get("NODE_NAME")
    if not node:
        raise RuntimeError("NODE_NAME must be set from the Kubernetes Downward API")

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    registry = CollectorRegistry()
    registry.register(NvidiaProcessCollector(node))
    _, thread = start_http_server(PORT, registry=registry)
    thread.join()


if __name__ == "__main__":
    main()