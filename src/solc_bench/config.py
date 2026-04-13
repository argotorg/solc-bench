"""Benchmark configuration: pipelines, benchmark loading, input file discovery."""

import os

import tomllib

DEFAULT_BENCHMARK_DIR = "benchmarks"

# Pipeline definitions: maps pipeline name to solc standard-json settings.
# Used to build the setting that override the standard-json input before compilation.
PIPELINE_CONFIGS = {
    "evmasm": {
        "solc_settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "viaIR": False,
        },
    },
    "ir": {
        "solc_settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "viaIR": True,
        },
    },
    "ir-ssacfg": {
        "solc_settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "viaIR": True,
            "viaSSACFG": True,
            "experimental": True,
        },
    },
}

DEFAULT_PIPELINES = list(PIPELINE_CONFIGS.keys())


def load_benchmarks(benchmark_dir):
    """Load benchmark definitions from benchmarks.toml."""
    toml_path = os.path.join(benchmark_dir, "benchmarks.toml")
    with open(toml_path, "rb") as f:
        return tomllib.load(f)


def find_input_file(benchmark_dir, name):
    """Find the standard-json input file for a benchmark."""
    path = os.path.join(benchmark_dir, f"{name}.json")
    if os.path.isfile(path):
        return path
    return None
