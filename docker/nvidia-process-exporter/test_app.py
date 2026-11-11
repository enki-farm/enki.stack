import subprocess

import app
import pytest
from prometheus_client.core import CollectorRegistry
from prometheus_client.exposition import generate_latest
from prometheus_client.parser import text_string_to_metric_families


def test_parse_processes_converts_mib_to_bytes_and_preserves_quoted_names():
    assert app.parse_processes('123,"python, worker",12\n456, vllm, 0\n') == [
        ("123", "python, worker", 12 * 1024**2),
        ("456", "vllm", 0),
    ]


def test_parse_processes_accepts_no_running_processes():
    assert app.parse_processes("") == []
    assert app.parse_processes("\n") == []


@pytest.mark.parametrize(
    "output",
    ["123,worker", "pid,worker,12", "123,worker,N/A", "-1,worker,12"],
)
def test_parse_processes_rejects_malformed_rows(output):
    with pytest.raises(ValueError):
        app.parse_processes(output)


def test_collector_exports_node_process_labels_and_success():
    def run(command, **kwargs):
        assert command == app.QUERY
        assert kwargs["timeout"] == 5
        return subprocess.CompletedProcess(command, 0, '123,"python, worker",12\n', "")

    registry = CollectorRegistry()
    registry.register(app.NvidiaProcessCollector("spark-1", run))

    metrics = generate_latest(registry).decode()

    samples = [
        sample
        for family in text_string_to_metric_families(metrics)
        for sample in family.samples
    ]
    process_sample = next(
        sample
        for sample in samples
        if sample.name == "nvidia_compute_app_used_memory_bytes"
    )
    error_sample = next(sample for sample in samples if sample.name == "nvidia_smi_scrape_error")
    assert process_sample.labels == {
        "node": "spark-1",
        "pid": "123",
        "process_name": "python, worker",
    }
    assert process_sample.value == 12 * 1024**2
    assert error_sample.value == 0


def test_collector_reports_command_failure_without_raising():
    def run(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi is unavailable")

    registry = CollectorRegistry()
    registry.register(app.NvidiaProcessCollector("spark-1", run))

    metrics = generate_latest(registry).decode()

    samples = [
        sample
        for family in text_string_to_metric_families(metrics)
        for sample in family.samples
    ]
    assert not any(sample.name == "nvidia_compute_app_used_memory_bytes" for sample in samples)
    error_sample = next(sample for sample in samples if sample.name == "nvidia_smi_scrape_error")
    assert error_sample.labels == {"node": "spark-1"}
    assert error_sample.value == 1