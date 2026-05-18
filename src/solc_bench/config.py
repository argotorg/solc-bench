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


def load_benchmarks(benchmark_dir):
    """Load benchmark definitions from <benchmark_dir>/benchmarks.toml."""
    benchmarks_toml = Path(benchmark_dir) / "benchmarks.toml"
    if not benchmarks_toml.is_file():
        raise FileNotFoundError(f"benchmarks.toml not found in {benchmark_dir}")
    with benchmarks_toml.open("r", encoding="utf-8") as f:
        benchmarks = tomlkit.load(f)

    for name, entry in benchmarks.items():
        entry["tags"] = _normalize_tags(name, entry.get("tags", []))

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
