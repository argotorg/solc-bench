import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from solc_bench.metrics import HIDDEN

REPO = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = REPO / "benchmark_data"

SUITE_BENCHMARKS = {
    "weth9": ["evmasm", "ir"],
    "solidity-chains": ["evmasm"],
}

CONTRACT = """\
// SPDX-License-Identifier: GPL-3.0
pragma solidity >=0.8.0;

contract Counter {
    uint256 public count;

    function increment() external {
        count += 1;
    }
}
"""


@pytest.fixture(scope="session")
def solc():
    solc_location = os.environ.get("SOLC")
    if not solc_location:
        pytest.skip("set SOLC to a solc binary to run the smoke tests")
    path = Path(solc_location).resolve()
    if not path.is_file():
        pytest.fail(f"SOLC does not point to a file: {path}")
    return str(path)


@pytest.fixture(scope="session")
def cli():
    command = shlex.split(os.environ.get("SOLC_BENCH", "solc-bench"))

    def run(*args, check=True):
        argv = [*command, *(str(a) for a in args)]
        proc = subprocess.run(argv, capture_output=True, text=True)
        if check and proc.returncode != 0:
            pytest.fail(
                f"{shlex.join(argv)} exited with {proc.returncode}\n"
                f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
            )
        return proc

    return run


@pytest.fixture(scope="session")
def contract(tmp_path_factory):
    path = tmp_path_factory.mktemp("src") / "Counter.sol"
    path.write_text(CONTRACT, encoding="utf-8")
    return path


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def assert_metric(result, metric, positive=True):
    stats = result[metric]
    assert stats["values"], f"{metric} has no samples"
    assert "median" in stats and "mean" in stats
    if positive:
        assert stats["median"] > 0, f"{metric} median is not positive"


def assert_result_file(path, solc_version=None):
    data = load_json(path)
    for key in ("solc_bench_version", "solc_version", "timestamp", "iterations",
                "hardware", "environment", "results"):
        assert key in data, f"missing top-level key {key}"
    if solc_version is not None:
        assert data["solc_version"] == solc_version
    return data


def test_version(cli):
    assert cli("--version").stdout.startswith("solc-bench ")


def test_list_benchmarks_includes_subsets(cli):
    out = cli("list", "--benchmark-dir", BENCHMARK_DIR).stdout
    for name in SUITE_BENCHMARKS:
        assert name in out
    # `include = ["subsets"]` pulls in subsets/benchmarks.toml under a prefix.
    assert "subsets/openzeppelin-timelock-5.6.1" in out


def test_list_tags(cli):
    out = cli("list", "--tags", "--benchmark-dir", BENCHMARK_DIR).stdout
    assert "fast" in out
    assert "benchmark(s)" in out


def test_list_metrics(cli):
    out = cli("list", "--metrics").stdout
    listed = [line.split()[0] for line in out.splitlines()]
    for metric in ("cpu_time", "wall_time", "peak_rss", "creation_size",
                   "runtime_size", "deployment_gas", "cache_miss_rate"):
        assert metric in listed
    for metric in HIDDEN:
        assert metric not in listed


def test_list_without_benchmark_dir_fails(cli):
    proc = cli("list", check=False)
    assert proc.returncode == 1
    assert "--benchmark-dir is required" in proc.stderr


def test_run_single_sol_file(cli, solc, contract, tmp_path):
    out = tmp_path / "single.json"
    proc = cli(
        "run", "--solc", solc, "--iterations", "2",
        "--pipelines", "evmasm,ir", "-o", out, contract,
    )
    assert "solc:" in proc.stderr
    assert "Results written to" in proc.stderr

    data = assert_result_file(out)
    assert data["iterations"] == 2
    assert list(data["results"]) == ["Counter"]
    pipelines = data["results"]["Counter"]
    assert set(pipelines) == {"evmasm", "ir"}
    for result in pipelines.values():
        for metric in ("cpu_time", "wall_time", "peak_rss",
                       "creation_size", "runtime_size"):
            assert_metric(result, metric)
        assert len(result["cpu_time"]["values"]) == 2
        assert "stddev" in result["cpu_time"]
        assert result.get("errors", 0) == 0


def test_run_single_json_file_with_stdout(cli, solc, contract, tmp_path):
    standard_json = tmp_path / "Counter.json"
    standard_json.write_text(json.dumps({
        "language": "Solidity",
        "sources": {"Counter.sol": {"content": CONTRACT}},
        "settings": {"outputSelection": {"*": {"*": ["evm.bytecode.object"]}}},
    }), encoding="utf-8")
    out = tmp_path / "json.json"
    proc = cli(
        "run", "--solc", solc, "--iterations", "1", "--pipeline", "evmasm",
        "--stdout", "-o", out, standard_json,
    )
    printed = json.loads(proc.stdout)
    assert printed == load_json(out)
    assert set(printed["results"]["Counter"]) == {"evmasm"}


def test_run_suite_by_name(cli, solc, tmp_path):
    out_dir = tmp_path / "suite"
    cli(
        "run", "--solc", solc, "--iterations", "1",
        "--benchmark-dir", BENCHMARK_DIR,
        "--only", ",".join(SUITE_BENCHMARKS), "--output-dir", out_dir,
    )
    data = assert_result_file(out_dir / "bench-results.json")
    assert set(data["results"]) == set(SUITE_BENCHMARKS)
    for name, expected_pipelines in SUITE_BENCHMARKS.items():
        assert set(data["results"][name]) == set(expected_pipelines), name
        for result in data["results"][name].values():
            assert_metric(result, "cpu_time")
            assert_metric(result, "creation_size")


def test_run_suite_pipeline_override_and_tags(cli, solc, tmp_path):
    out = tmp_path / "tags.json"
    cli(
        "run", "--solc", solc, "--iterations", "1",
        "--benchmark-dir", BENCHMARK_DIR,
        "--only", "solidity-chains", "--tags", "fast",
        "--pipeline", "ir", "-o", out,
    )
    data = load_json(out)
    # --pipeline overrides the entry's `pipelines = ["evmasm"]`.
    assert set(data["results"]["solidity-chains"]) == {"ir"}


def test_run_ethdebug_pipeline(cli, solc, contract, tmp_path):
    out = tmp_path / "ethdebug.json"
    cli(
        "run", "--solc", solc, "--iterations", "1", "--no-optimize",
        "--pipelines", "ir,ir-ethdebug", "-o", out, contract,
    )
    results = load_json(out)["results"]["Counter"]
    assert set(results) == {"ir", "ir-ethdebug"}
    assert "ethdebug_size" not in results["ir"]
    assert_metric(results["ir-ethdebug"], "ethdebug_size")


def test_run_ethdebug_requires_no_optimize(cli, solc, contract, tmp_path):
    proc = cli(
        "run", "--solc", solc, "--pipeline", "ir-ethdebug",
        "-o", tmp_path / "x.json", contract, check=False,
    )
    assert proc.returncode == 1
    assert "Error:" in proc.stderr
    assert "--no-optimize" in proc.stderr


def test_run_refuses_to_overwrite_results(cli, solc, contract, tmp_path):
    out = tmp_path / "existing.json"
    out.write_text("{}", encoding="utf-8")
    proc = cli(
        "run", "--solc", solc, "--pipeline", "evmasm", "-o", out, contract,
        check=False,
    )
    assert proc.returncode == 1
    assert "results file already exists" in proc.stderr
    assert out.read_text(encoding="utf-8") == "{}"


def test_run_missing_solc_is_usage_error(cli, contract, tmp_path):
    proc = cli(
        "run", "--solc", tmp_path / "no-such-solc", contract, check=False,
    )
    assert proc.returncode == 2
    assert "solc binary not found" in proc.stderr


def test_run_reports_compile_errors(cli, solc, tmp_path):
    broken = tmp_path / "Broken.sol"
    broken.write_text(
        "// SPDX-License-Identifier: GPL-3.0\npragma solidity >=0.8.0;\n"
        "contract Broken { function f() external { undefined_symbol; } }\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    proc = cli(
        "run", "--solc", solc, "--pipeline", "evmasm",
        "--output-dir", out_dir, broken,
    )
    assert "No results to write" in proc.stderr
    log = out_dir / "Broken-evmasm.errors.log"
    assert log.is_file()
    assert "undefined_symbol" in log.read_text(encoding="utf-8")
    assert not (out_dir / "bench-results.json").exists()


@pytest.fixture(scope="session")
def two_runs(cli, solc, contract, tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("compare")
    paths = []
    for label in ("baseline", "target"):
        path = out_dir / f"{label}.json"
        cli(
            "run", "--solc", solc, "--iterations", "2",
            "--pipelines", "evmasm,ir", "-o", path, contract,
        )
        paths.append(path)
    return tuple(paths)


def test_compare_cross_version(cli, two_runs, tmp_path):
    baseline, target = two_runs
    cmp_json = tmp_path / "cmp.json"
    proc = cli("compare", baseline, target, "--summary", "--output", cmp_json)
    assert "Baseline:" in proc.stdout
    assert "Target:" in proc.stdout
    assert "Counter" in proc.stdout

    result = load_json(cmp_json)
    assert result["mode"] == "cross-version"
    assert "baseline" in result and "target" in result
    assert set(result["benchmarks"]["Counter"]) == {"evmasm", "ir"}
    comparison = result["benchmarks"]["Counter"]["evmasm"]
    assert "cpu_time" in comparison and "creation_size" in comparison


def test_compare_cross_pipeline(cli, two_runs):
    baseline, _ = two_runs
    proc = cli("compare", baseline, "--pipelines", "ir:evmasm")
    assert "Counter" in proc.stdout


def test_compare_rejects_mixed_modes(cli, two_runs):
    baseline, target = two_runs
    proc = cli("compare", baseline, target, "--pipelines", "ir:evmasm", check=False)
    assert proc.returncode == 1
    assert "Error:" in proc.stderr


@pytest.fixture
def results_with_hidden(tmp_path):
    metrics = {
        "cpu_time": {"values": [1, 1], "mean": 1},
        "cycles": {"values": [1, 1], "mean": 1},
    }
    path = tmp_path / "hidden.json"
    path.write_text(json.dumps({"results": {"C": {"evmasm": metrics, "ir": metrics}}}))
    return path


@pytest.mark.parametrize("mode", ["cross-version", "cross-pipeline"])
def test_compare_hides_metrics_unless_show_hidden(cli, results_with_hidden, tmp_path, mode):
    baseline = target = results_with_hidden
    if mode == "cross-version":
        args = [baseline, target]
    else:
        args = [baseline, "--pipelines", "ir:evmasm"]

    for show_hidden in (False, True):
        cmp_json = tmp_path / f"cmp-{show_hidden}.json"
        flags = ["--show-hidden"] if show_hidden else []
        proc = cli("compare", *args, *flags, "--output", cmp_json)
        comparison = load_json(cmp_json)["benchmarks"]["C"]
        if mode == "cross-version":
            comparison = comparison["evmasm"]

        assert "cpu_time" in comparison
        assert ("cycles" in comparison) == show_hidden
        assert ("cycles" in proc.stdout) == show_hidden


def test_compare_plot_metric_rejects_hidden_unless_show_hidden(cli, results_with_hidden):
    baseline = target = results_with_hidden
    proc = cli("compare", baseline, target, "--plot-metric", "cycles", check=False)
    assert proc.returncode == 1
    assert "unknown metric in --plot-metric: cycles" in proc.stderr
    cli("compare", baseline, target, "--plot-metric", "cycles", "--show-hidden")


def test_fetch_refuses_to_overwrite_without_force(cli, tmp_path):
    # Fails before any network access.
    existing = tmp_path / "solc-existing"
    existing.write_text("", encoding="utf-8")
    proc = cli("fetch", "v0.8.37", "--output", existing, check=False)
    assert proc.returncode == 1
    assert "refusing to overwrite" in proc.stderr
