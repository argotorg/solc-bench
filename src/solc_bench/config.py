import sys
from pathlib import Path

import tomlkit

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
RUN_PIPELINES = [*DEFAULT_PIPELINES, "ir-ethdebug"]

# Default basename for a run's result JSON when -o/--output-file is omitted.
# `compare` also infers a dataset label from the parent directory when a file
# carries this name, so it is part of the result-file contract, not merely a
# default. Keep the path construction and that heuristic referring to this.
DEFAULT_RESULT_FILENAME = "bench-results.json"


def load_benchmarks(benchmark_dir):
    """Load benchmark definitions from <benchmark_dir>/benchmarks.toml, merging in
    the benchmarks.toml of each subdirectory named in its top-level `include` list.
    """
    benchmark_dir = Path(benchmark_dir)
    toml_path = benchmark_dir / "benchmarks.toml"
    if not toml_path.is_file():
        raise FileNotFoundError(f"benchmarks.toml not found: {toml_path}")
    with toml_path.open("r", encoding="utf-8") as f:
        doc = tomlkit.load(f)

    includes = doc.pop("include", [])

    benchmarks = {}
    for name, entry in doc.items():
        entry["tags"] = _normalize_tags(name, entry.get("tags", []))
        benchmarks[name] = entry

    for include in includes:
        for name, entry in load_benchmarks(benchmark_dir / include).items():
            if name in benchmarks:
                raise ValueError(f"duplicate benchmark name '{name}' (found in {benchmark_dir / include})")
            benchmarks[str(Path(include) / name)] = entry

    return benchmarks


def _normalize_tags(name, raw):
    """Coerce a benchmark's ``tags`` value to a clean list of lowercase strings."""
    if isinstance(raw, str):
        print(
            f"warning: '{name}' has tags as a string, "
            "treating as a single-element list",
            file=sys.stderr,
        )
        raw = [raw]
    elif not isinstance(raw, list):
        print(
            f"warning: '{name}' has tags of unsupported type "
            f"{type(raw).__name__}, ignoring",
            file=sys.stderr,
        )
        raw = []

    seen = set()
    out = []
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out
