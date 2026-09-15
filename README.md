# solc-bench

Benchmark tool for the Solidity compiler: compile-time performance, memory,
hardware counters (`perf stat`), bytecode size, and gas usage across
real-world projects.

## Basic usage

```bash
SOLC1=../solidity/build/solc/solc
SOLC2=../solidity-new/build/solc/solc
solc-bench run --benchmark-dir ./benchmark_data --solc $SOLC1 --tags fast -o old.json
solc-bench run --benchmark-dir ./benchmark_data --solc $SOLC2 --tags fast -o new.json
solc-bench compare old.json new.json
```

## Run in Nix

```bash
git clone https://github.com/argotorg/solc-bench
cd solc-bench
nix develop
# you are now in a nix develop shell
python -m venv .venv && source .venv/bin/activate
pip install -e '.[plot]'
solc-bench ...
```

## Run without Nix

Needs Python 3.11+. `perf` (hardware counters) and `forge` (extract, gas
benchmarks) are optional.

```bash
git clone https://github.com/argotorg/solc-bench
cd solc-bench
python -m venv .venv && source .venv/bin/activate
pip install -e '.[plot]'
solc-bench ...
```

## Pipelines

Each benchmark is compiled under one or more codegen pipelines. `run` uses
the pipelines in each benchmark's TOML entry (or all if unspecified);
`--pipeline` restricts to one, `--no-optimize` disables the optimizer.

| Pipeline | Standard-json settings |
|----------|----------------------|
| `evmasm` | `"viaIR": false` — EVM assembly codegen |
| `ir` | `"viaIR": true` — IR-based codegen |
| `ir-ssacfg` | `"viaIR": true, "viaSSACFG": true` — SSA-CFG experimental codegen |
| `ir-ethdebug` | `"viaIR": true`, optimizer disabled — unoptimized IR codegen with ETHDebug outputs |

## Metrics

All metrics are collected when applicable, except `deployment_gas` and
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
| `ethdebug_size` | Serialized ETHDebug JSON output size | bytes | solc output |
| `deployment_gas` | Total deployment gas | gas | `forge test --gas-report` |
| `method_gas` | Total method-call gas (`mean * calls`) | gas | `forge test --gas-report` |

## CLI

### `solc-bench run`

Benchmarks a suite in `--benchmark-dir` or a single `sol`/`json` file without
the `--benchmark-dir` flag. Results land in `bench-results.json` in
`--output-dir`, or in a file with `-o file.json`.

| Flag | Default | Description |
|------|---------|-------------|
| `--solc PATH` | required | Path to solc binary |
| `--benchmark-dir DIR` | required for suites | Suite dir (`benchmarks.toml` + JSONs) |
| `--only NAMES` | (all) | Comma-separated benchmark names |
| `--tags TAGS` | (none) | Comma-separated tags |
| `--iterations N` | `3` | Number of iterations |
| `--output-dir DIR` | current dir | Where to write results + logs |
| `-o, --output-file FILE` | (none) | Write result JSON to a specific file |
| `--stdout` | off | Also print results to stdout |
| `--pipeline P` | (all) | `evmasm`/`ir`/`ir-ssacfg`/`ir-ethdebug` |
| `--no-optimize` | off | Disable the optimizer |

```bash
solc-bench run --solc ./solc --benchmark-dir ./my-suite --only openzeppelin-5.6.1
solc-bench run --solc ./solc contract.sol --pipeline ir       # single file
```

### `solc-bench compare`

Compares two result files, two pipelines in one file (`--pipelines TARGET:REF`),
or named datasets (`--vs TARGET REF`, label = file stem or `LABEL=PATH`).
`~noise` in the `winner` column means the difference isn't statistically significant or is under 0.10%.

| Flag | Default | Description |
|------|---------|-------------|
| `--pipelines TARGET:REF` | cross-version | Compare two pipelines e.g. `ir:evmasm` |
| `--vs TARGET REF` | off | Compare two named datasets |
| `--format table`/`json` | `table` | Output format |
| `--output FILE` | (none) | Write comparison JSON to file |
| `--per-function [STAT]` | off | Per-function gas deltas |
| `--plot FILE` | (none) | Write a boxplot, requires `[plot]` |
| `--plot-metric METRIC[,...]` | `cpu_time` | Metric(s) to plot |

```bash
solc-bench compare baseline/bench-results.json target/bench-results.json
solc-bench compare bench-results.json --pipelines ir:evmasm --plot diff.png
```

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

## Benchmark suites

A suite is a directory with a `benchmarks.toml` and one `<key>.json`
standard-json input per entry. The repo's default suite is `benchmark_data/`.

### Subsets of large projects

A top-level `include = ["subsets"]` pulls in `subsets/benchmarks.toml`; its
entries are named `subsets/<key>` (e.g. `--only subsets/aave-pool-3.6.0`).

`benchmark_data/subsets/*.json` are generated from the full project JSONs by
`scripts/regenerate_subsets.sh`. Don't edit them by hand; add the subset to
that script instead. It calls `scripts/subset.py`, which keeps only the import
closure of the given root sources and requests codegen for just those roots:

```bash
python scripts/subset.py benchmark_data/openzeppelin-5.6.1.json \
  benchmark_data/subsets/openzeppelin-timelock-5.6.1.json \
  contracts/governance/TimelockController.sol
```

### Adding a benchmark

`extract` writes `<project-dir-name>.json`, so clone the project into a
directory named after the TOML key:

```bash
solc-bench extract --solc ./solc --project /tmp/openzeppelin-5.6.1 --output-dir ./benchmark_data
```

Then add the TOML entry. `extract` skips existing JSONs and never touches
`benchmarks.toml`.

### Gas benchmarks

`gas = true` also collects `deployment_gas` and `method_gas` (requires
`forge`). The first run clones the project at the `version` git tag into
`<benchmark-dir>/<key>/` and runs `forge test --gas-report --json`; later runs
reuse the clone. Bumping `version` errors out — delete the stale clone and
re-run.

## ETHDebug overhead

`ir-ethdebug` is unoptimized `ir` plus the ETHDebug outputs, so it requires
`--no-optimize` (gas is skipped). It adds the `ethdebug_size` metric. Measure
the overhead against a plain unoptimized `ir` run:

```bash
solc-bench run --pipeline ir-ethdebug --no-optimize  --solc ./solc --benchmark-dir ./benchmark_data -o ethdebug_ir.json
solc-bench run --pipeline ir --no-optimize           --solc ./solc --benchmark-dir ./benchmark_data -o ir.json
solc-bench compare ir.json ethdebug_ir.json --vs ethdebug_ir ir
```
