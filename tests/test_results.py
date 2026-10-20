import json
import sys

from solc_bench import cli
from solc_bench.results import PipelineResult, ResultFile

# The on-disk layout: metrics and "functions" side by side in each pipeline.
RESULT_FILE = {
    "solc_bench_version": "0.1.0",
    "solc_version": "0.8.35+commit.47b9dedd.Linux.g++",
    "timestamp": "2026-10-01T12:00:00+00:00",
    "iterations": 2,
    "hardware": {"cpu_model": "Some CPU", "cpu_threads_online": 16},
    "environment": {"governor": "performance", "smt_active": False},
    "results": {
        "weth9": {
            "evmasm": {
                "cpu_time": {"values": [0.5, 0.7], "median": 0.6, "mean": 0.6, "stddev": 0.1414},
                "creation_size": {"values": [2439, 2439], "median": 2439, "mean": 2439, "stddev": 0.0},
                "deployment_gas": {"values": [500000], "median": 500000, "mean": 500000},
                "functions": {
                    "WETH9.deposit()": {
                        "calls": 4, "min": 23000, "mean": 24000, "median": 24500, "max": 27000,
                    },
                    "WETH9@c02aaa39.deposit-d0e30db0": {
                        "values": [45038], "median": 45038, "mean": 45038,
                    },
                },
            },
        },
    },
}


def test_result_file_round_trips_the_on_disk_layout():
    result_file = ResultFile.model_validate(RESULT_FILE)
    result = result_file.results["weth9"]["evmasm"]
    assert list(result.metrics) == ["cpu_time", "creation_size", "deployment_gas"]
    assert result.functions["WETH9.deposit()"].calls == 4
    # Compared as text, so an int that turned into a float shows up.
    assert json.dumps(result_file.to_json(), sort_keys=True) == json.dumps(RESULT_FILE, sort_keys=True)


def test_legacy_errors_count_is_dropped():
    result = PipelineResult.model_validate({
        "cpu_time": {"values": [1.0], "median": 1.0, "mean": 1.0},
        "errors": 0,
    })
    assert list(result.metrics) == ["cpu_time"]
    assert "errors" not in result.model_dump()


def test_from_samples_aggregates_each_metric():
    result = PipelineResult.from_samples([
        {"cpu_time": 1.0, "creation_size": 100},
        {"cpu_time": 3.0, "creation_size": 100},
    ])
    assert result.metrics["cpu_time"].model_dump() == {
        "values": [1.0, 3.0], "median": 2.0, "mean": 2.0, "stddev": 2 ** 0.5,
    }
    assert result.metrics["creation_size"].median == 100


def test_invalid_result_file_is_reported_with_the_offending_field(tmp_path, monkeypatch, capsys):
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"results": {"C": {"evmasm": {"cpu_time": {"values": [1]}}}}}))
    monkeypatch.setattr(sys, "argv", ["solc-bench", "compare", str(results), str(results)])
    assert cli.main() == 1
    err = capsys.readouterr().err
    assert f"Error: {results} is not a solc-bench result file" in err
    assert "cpu_time.median: Field required" in err
