"""Benchmark configuration and input file handling."""

import json
import os
import tempfile
from contextlib import contextmanager

import tomllib

DEFAULT_BENCHMARK_DIR = "benchmarks"


def load_benchmarks(benchmark_dir):
    """Load benchmark definitions from benchmarks.toml.

    Raises FileNotFoundError if the file does not exist.
    """
    toml_path = os.path.join(benchmark_dir, "benchmarks.toml")
    with open(toml_path, "rb") as f:
        return tomllib.load(f)


def find_input_files(benchmark_dir, name, pipelines):
    """Find standard-json input files for a benchmark."""
    inputs = {}
    for pipeline in pipelines:
        path = os.path.join(benchmark_dir, f"{name}-{pipeline}.json")
        if os.path.isfile(path):
            inputs[pipeline] = path
    return inputs


@contextmanager
def wrap_sol_as_standard_json(sol_path, pipeline="legacy", optimize=True):
    """Wrap a .sol file into a temporary standard-json input file.

    Usage:
        with wrap_sol_as_standard_json("contract.sol", pipeline="ir") as path:
            run_benchmark(solc, path, iterations)
    """
    with open(sol_path) as f:
        source = f.read()

    settings = {
        "optimizer": {"enabled": optimize},
        "outputSelection": {"*": {"*": ["*"]}},
    }
    if pipeline in ("ir", "ir-ssacfg"):
        settings["viaIR"] = True
    if pipeline == "ir-ssacfg":
        settings["viaSSACFG"] = True
        settings["experimental"] = True

    standard_input = {
        "language": "Solidity",
        "sources": {
            os.path.basename(sol_path): {
                "content": source,
            }
        },
        "settings": settings,
    }

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="solc-bench-", delete=False
    )
    try:
        json.dump(standard_input, tmp)
        tmp.close()
        yield tmp.name
    finally:
        os.remove(tmp.name)
