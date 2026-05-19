# solc-bench

Benchmark tool for the Solidity compiler: compile-time performance, memory,
hardware counters (`perf stat`), bytecode size, and gas usage across
real-world projects.

## Install

```bash
pip install -e .                                        # pip
nix run github:argotorg/solc-bench -- run --solc ./solc --benchmark-dir ./benchmark_data # Nix flake (published)
```

Python 3.11+. Runtime tools: `solc` (required), `perf` (optional, hardware
counters), `forge` (optional, input extraction + gas benchmarks).

Nix, without installing: run any command as `nix run . -- <command> ...`
from a checkout, or `nix run github:argotorg/solc-bench -- <command> ...`.
`nix shell github:argotorg/solc-bench` puts `solc-bench` on `PATH` for the
shell; `nix build github:argotorg/solc-bench` builds into `./result/`. For
development, `nix develop` provides `forge`, `perf`, and the Python runtime —
use a venv for an editable install (`python -m venv .venv && pip install -e .`).

## Pipelines

Each benchmark is compiled under one or more codegen pipelines. `run` uses
the pipelines in each benchmark's TOML entry (or all if unspecified);
`--pipeline` restricts to one, `--no-optimize` disables the optimizer.

| Pipeline | Standard-json settings |
|----------|----------------------|
| `evmasm` | `"viaIR": false` — EVM assembly codegen |
| `ir` | `"viaIR": true` — IR-based codegen |
| `ir-ssacfg` | `"viaIR": true, "viaSSACFG": true` — SSA-CFG experimental codegen |

## Metrics

All metrics are collected on every run, except `deployment_gas` and
`method_gas`, which are opt-in per benchmark (see [Gas benchmarks](#gas-benchmarks)).

| Metric | Description | Unit | Source |
|--------|-------------|------|--------|
| `instructions` | Hardware instruction count | count | `perf stat` |
| `cycles` | CPU cycle count | count | `perf stat` |
| `cpu_time` | CPU time (user + system) | seconds | `os.wait4()` rusage |
| `wall_time` | Wall clock time | seconds | `time.monotonic()` |
| `peak_rss` | Peak resident set size | MiB | rusage.ru_maxrss |
| `creation_size` | Total creation bytecode size | bytes | solc output |
| `runtime_size` | Total runtime bytecode size | bytes | solc output |
| `deployment_gas` | Total deployment gas | gas | `forge test --gas-report` |
| `method_gas` | Total method-call gas (`mean * calls`) | gas | `forge test --gas-report` |

`instructions` is the primary comparison metric (variance <0.1% vs 3-5% for
`wall_time`); falls back to `cpu_time` when `perf` is unavailable. With gas
benchmarking, the result JSON also stores the forge per-function dict
(`calls`, `min`, `mean`, `median`, `max`) under
`results.<name>.<pipeline>.functions` — what `compare --per-function` renders.

## Benchmarks

`run` and `list` require `--benchmark-dir DIR`, a directory containing
`benchmarks.toml` and the input JSONs it references:

```bash
solc-bench run --solc ./solc --benchmark-dir ./benchmark_data
```

### Build your own

`extract` writes one JSON per Forge project, named after the project
directory. The directory name and the TOML key must match, since `run`
looks for `<key>.json` in `--benchmark-dir`:

```bash
solc-bench extract --solc ./solc --project /tmp/openzeppelin-5.6.1 --output-dir ./my-suite

cat >> ./my-suite/benchmarks.toml <<'EOF'
["openzeppelin-5.6.1"]
source = "https://github.com/OpenZeppelin/openzeppelin-contracts"
version = "v5.6.1"
pipelines = ["evmasm", "ir"]
gas = true   # optional: also collect gas metrics
EOF
```

`extract` skips existing JSONs and never touches `benchmarks.toml`, so you
can repeat to add projects. To contribute to the repo's default suite,
extract into `benchmark_data/`, add a TOML entry, and open a PR.

### Gas benchmarks

`gas = true` in a TOML entry also collects `deployment_gas` and `method_gas`
(requires `forge`). The first run clones the project at the `version` git
tag into `<benchmark-dir>/<entry-name>/` and runs `forge test --gas-report
--json`; later runs reuse the clone. Bumping `version` errors out — delete
the stale clone and re-run.

## CLI

### `solc-bench fetch <ref>`

Downloads a Linux x86_64 solc binary. A release tag (`v0.8.35`) comes from
the matching [argotorg/solidity release](https://github.com/argotorg/solidity/releases);
a branch (`develop`) from the latest successful CircleCI `b_ubu_static`
artifact. `CIRCLECI_TOKEN` / `GITHUB_TOKEN` raise rate limits.

| Flag | Default | Description |
|------|---------|-------------|
| `--output FILE` | `./solc-{ref}` | Destination path |
| `--force` | off | Overwrite destination if it exists |

```bash
solc-bench fetch v0.8.35
solc-bench fetch develop --output ./solc --force
```

### `solc-bench run [input_file]`

Benchmarks a suite, or a single `.sol`/`.json` `input_file` (which bypasses
the suite and needs no `--benchmark-dir`). Results land in
`bench-results.json` in `--output-dir`.

| Flag | Default | Description |
|------|---------|-------------|
| `--solc PATH` | required | Path to solc binary |
| `--benchmark-dir DIR` | required for suites | Suite dir (`benchmarks.toml` + JSONs) |
| `--only NAMES` | (all) | Comma-separated benchmark names |
| `--tags TAGS` | (none) | Comma-separated tags, AND'd with `--only` |
| `--iterations N` | `3` | Number of iterations |
| `--output-dir DIR` | current dir | Where to write results + logs |
| `--stdout` | off | Also print results to stdout |
| `--pipeline P` | (all) | Single pipeline: `evmasm`/`ir`/`ir-ssacfg` |
| `--no-optimize` | off | Disable the optimizer |

```bash
solc-bench run --solc ./solc --benchmark-dir ./my-suite --only openzeppelin-5.6.1
solc-bench run --solc ./solc contract.sol --pipeline ir       # single file
```

### `solc-bench compare <baseline> [target]`

Compares two result files (cross-version), or two pipelines within one file
via `--pipelines TARGET:REF`. The output shows each metric's signed percent
delta; every metric is lower-is-better, so negative is an improvement. The
`winner` column names the better side, but shows `~noise` unless the gap
passes a Welch t-test and exceeds 0.10% (statistically real and large enough
to act on). `--per-function` adds a per-function gas delta table when both
files have gas data.

| Flag | Default | Description |
|------|---------|-------------|
| `--pipelines TARGET:REF` | cross-version | Compare two pipelines in one file (e.g. `ir:evmasm`) |
| `--format table`/`json` | `table` | Output format |
| `--output FILE` | (none) | Write comparison JSON to file |
| `--per-function STAT` | `median` | Per-function gas deltas: `min`/`mean`/`median`/`max` |
| `--plot FILE` | (none) | Write a boxplot (requires `[plot]` extra) |
| `--plot-metric METRIC[,...]` | `cpu_time` | Metric(s) to plot |

```bash
solc-bench compare baseline/bench-results.json target/bench-results.json --per-function
solc-bench compare bench-results.json --pipelines ir:evmasm --plot diff.png
```

### `solc-bench extract`

Produces one standard-json `.json` per Forge project (sources + base
settings); pipeline and optimizer settings are applied at runtime by `run`.

| Flag | Default | Description |
|------|---------|-------------|
| `--solc PATH` | required | Path to solc binary |
| `--project DIR` | required | Forge project directory |
| `--output-dir DIR` | project parent | Where to write the standard-json |

### `solc-bench extract-sourcify`

Pulls the top-N most-used mainnet contracts from Sourcify into a ready-to-run
suite (JSONs + `benchmarks.toml`). Pragmas are rewritten to `>=<min_version>;`;
proxies are resolved to their implementation. Refuses to run against a
non-empty directory unless `--force` is given.

| Flag | Default | Description |
|------|---------|-------------|
| `--output-dir DIR` | required | Where to write JSONs and `benchmarks.toml` |
| `--top-n N` | `100` | Number of mainnet contracts to extract |
| `--min-version X` | `0.8.0` | solc version floor: filters older + rewrites pragmas |
| `--force` | off | Wipe `--output-dir` contents first |

### `solc-bench list`

Lists configured benchmarks, or with `--tags`/`--metrics` the tags or
metrics instead.

| Flag | Default | Description |
|------|---------|-------------|
| `--benchmark-dir DIR` | required unless `--metrics` | Suite directory with `benchmarks.toml` |
| `--tags` | off | List all tags across benchmarks |
| `--metrics` | off | List available metrics |
