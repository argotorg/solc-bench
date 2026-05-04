from importlib.resources import files
from pathlib import Path

import tomllib

# Where to find input JSON files. Overridable via CLI --benchmark-dir.
# The benchmarks.toml itself is bundled with the package (see load_benchmarks).
DEFAULT_BENCHMARK_DIR = "benchmark_data"

# Pipeline definitions: maps pipeline name to solc standard-json settings.
# Used to build the setting that override the standard-json input before compilation.
# TODO: support all Standard JSON Input settings.
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


def load_benchmarks(benchmark_dir=None):
    """Load benchmark definitions.

    If benchmark_dir contains a benchmarks.toml, use it; otherwise fall back
    to the TOML bundled with the package.
    """
    benchmarks_toml = files("solc_bench.benchmarks") / "benchmarks.toml"
    if benchmark_dir:
        local_toml = Path(benchmark_dir) / "benchmarks.toml"
        if local_toml.is_file():
            benchmarks_toml = local_toml
    with benchmarks_toml.open("rb") as f:
        return tomllib.load(f)
