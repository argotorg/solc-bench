# solc-bench

Benchmark tool for the Solidity compiler. Measures compile-time
performance, memory usage, hardware counters (via `perf stat`),
bytecode output size, and gas usage across real-world projects.

## Install

### pip

```
pip install -e .
```

### Nix (flake)

Run without installing:

```
nix run github:argotorg/solc-bench -- run --solc /path/to/solc --benchmark-dir DIR
```

Get `solc-bench` on `PATH` for the current shell:

```
nix shell github:argotorg/solc-bench
```

Build into `./result/`:

```
nix build github:argotorg/solc-bench
./result/bin/solc-bench --help
```

For development, `nix develop` provides `forge`, `perf`,
and the Python runtime + deps. Use a venv for an editable install:

```
git clone https://github.com/argotorg/solc-bench && cd solc-bench
nix develop
python -m venv .venv && source .venv/bin/activate
pip install -e .
solc-bench --help
```

Requires Python 3.11+. Runtime tools: `solc` (required), `perf` (optional,
for hardware counters), `forge` (optional, for input extraction and gas
benchmarks).

## Pipelines

Each benchmark is compiled under one or more codegen pipelines.

| Pipeline | Description | Standard-json settings |
|----------|-------------|----------------------|
| `evmasm` | EVM assembly codegen | `"viaIR": false` |
| `ir` | IR-based codegen | `"viaIR": true` |
| `ir-ssacfg` | SSA-CFG experimental codegen | `"viaIR": true, "viaSSACFG": true` |

`solc-bench run` picks the pipelines listed in each benchmark's TOML
entry (or all pipelines if unspecified). Use `--pipeline` to restrict to
a single one, and `--no-optimize` to disable the optimizer (it's on by default).

## Metrics

Every metric below is collected on every run, except `deployment_gas`
and `method_gas`, which are opt-in per benchmark (see
[Gas benchmarks](#gas-benchmarks)).

| Metric | Description | Unit | Source |
|--------|-------------|------|--------|
| `instructions` | Hardware instruction count | count | `perf stat` |
| `cycles` | CPU cycle count | count | `perf stat` |
| `cpu_time` | CPU time (user + system) | seconds | `os.wait4()` rusage |
| `wall_time` | Wall clock time | seconds | `time.monotonic()` |
| `peak_rss` | Peak resident set size | MiB | rusage.ru_maxrss |
| `creation_size` | Sum of creation bytecode size across all contracts | bytes | solc standard-json output |
| `runtime_size` | Sum of runtime bytecode size across all contracts | bytes | solc standard-json output |
| `deployment_gas` | Total deployment gas | gas | `forge test --gas-report` |
| `method_gas` | Total method-call gas (sum of `mean * calls`) | gas | `forge test --gas-report` |

`instructions` is the primary comparison metric (variance <0.1% vs 3-5%
for `wall_time`). Falls back to `cpu_time` when `perf` is unavailable.

When gas benchmarking is enabled, the result JSON also stores the full
forge per-function dict (`calls`, `min`, `mean`, `median`, `max`) under
`results.<name>.<pipeline>.functions`, keyed by `ContractName.signature`.
`method_gas` aggregates these to a single number. The per-function
detail is what [`compare --per-function`](#solc-bench-compare) renders.

## Benchmarks

`solc-bench run` and `solc-bench list` require `--benchmark-dir DIR`,
a directory containing `benchmarks.toml` and the input JSONs it references.

### Use the default suite in `benchmark_data/`

```bash
solc-bench run --solc ./solc --benchmark-dir ./benchmark_data
solc-bench list --benchmark-dir ./benchmark_data
```

### Build your own

`solc-bench extract` writes one JSON per Forge project, named after
the project directory. Add a manifest entry by hand. The directory
name and the manifest key must match, because `run` looks for
`<key>.json` in `--benchmark-dir`:

```bash
git clone --depth 1 --branch v5.6.1 \
    https://github.com/OpenZeppelin/openzeppelin-contracts \
    /tmp/openzeppelin-5.6.1
solc-bench extract --solc ./solc --project /tmp/openzeppelin-5.6.1 --output-dir ./my-suite

cat >> ./my-suite/benchmarks.toml <<'EOF'
["openzeppelin-5.6.1"]
source = "https://github.com/OpenZeppelin/openzeppelin-contracts"
version = "v5.6.1"
pipelines = ["evmasm", "ir"]
EOF
```

`extract` skips JSONs that already exist and never touches
`benchmarks.toml`, so you can repeat the steps to add more projects.

### Contributing a benchmark to the repo's default suite

1. Extract the standard-json input from a Forge project into
   `benchmark_data/`:
   ```
   solc-bench extract --solc ./solc --project /path/to/project --output-dir benchmark_data/
   ```

2. Add an entry to `benchmark_data/benchmarks.toml`:
   ```toml
   ["my-project-1.0.0"]
   source = "https://github.com/example/my-project"
   version = "v1.0.0"
   pipelines = ["evmasm", "ir"]
   gas = true   # optional: also collect gas metrics
   ```

3. Open a PR.

### Gas benchmarks

Set `gas = true` in a benchmark's TOML entry to also collect
`deployment_gas` and `method_gas`. Requires `forge` in `$PATH`.

On the first run, solc-bench clones the project at the tag in
`version` (must be a git tag, e.g. `v5.6.1`) into
`<benchmark-dir>/<entry-name>/` and invokes
`forge test --gas-report --json`. Subsequent runs reuse that clone.

If you bump `version`, solc-bench raises `RuntimeError` and prints the
stale clone's path. Delete that directory and re-run to clone the new
version.

## CLI

### `solc-bench fetch`

| Flag | Argument | Default | Description |
|------|----------|---------|-------------|
| `--output` | `FILE` | `./solc-{ref}` | Destination path |
| `--force` | — | off | Overwrite destination if it exists |

Positional: `ref` (required: release tag like `v0.8.35` or branch like `develop`).

```bash
solc-bench fetch v0.8.35                              # ./solc-v0.8.35
solc-bench fetch develop                              # ./solc-develop
solc-bench fetch develop --output ./solc --force
```

The source depends on the ref:

- Release tag (e.g. `v0.8.35`) → downloaded from the matching [argotorg/solidity GitHub release](https://github.com/argotorg/solidity/releases).
- Branch (e.g. `develop`) → downloaded from the latest successful `b_ubu_static` artifact on CircleCI.

Linux x86_64 only. Set `CIRCLECI_TOKEN` to raise CircleCI rate limits, and `GITHUB_TOKEN` for GitHub.

### `solc-bench run`

| Flag | Argument | Default | Description |
|------|----------|---------|-------------|
| `--solc` | `PATH` | required | Path to solc binary |
| `--benchmark-dir` | `DIR` | required for suite runs | Suite directory (`benchmarks.toml` + input JSONs). See [Benchmarks](#benchmarks). Not needed with a positional `input_file`. |
| `--only` | `NAMES` | (all) | Comma-separated benchmark names |
| `--tags` | `TAGS` | (none) | Comma-separated tags. AND'd with `--only` |
| `--iterations` | `N` | `3` | Number of iterations |
| `--output-dir` | `DIR` | current dir | Where to write results + logs |
| `--stdout` | — | off | Also print results to stdout |
| `--pipeline` | `evmasm`/`ir`/`ir-ssacfg` | (all) | Single pipeline to run |
| `--no-optimize` | — | off | Disable the optimizer |

Positional: `input_file` (optional `.sol` or `.json`, bypasses the
suite and benchmarks the file directly).

Run a suite:

```bash
solc-bench run --solc ./solc --benchmark-dir ./my-suite
solc-bench run --solc ./solc --benchmark-dir ./my-suite --only openzeppelin-5.6.1
solc-bench run --solc ./solc --benchmark-dir ./my-suite --tags slow
solc-bench run --solc ./solc --benchmark-dir ./my-suite --iterations 10
solc-bench run --solc ./solc --benchmark-dir ./my-suite --pipeline ir
solc-bench run --solc ./solc --benchmark-dir ./my-suite --no-optimize
solc-bench run --solc ./solc --benchmark-dir ./my-suite --output-dir /tmp/bench-results
solc-bench run --solc ./solc --benchmark-dir ./my-suite --stdout
```

Benchmark a single file (skip the suite, no `--benchmark-dir` needed):

```bash
solc-bench run --solc ./solc contract.sol
solc-bench run --solc ./solc contract.sol --pipeline ir
solc-bench run --solc ./solc input.json
solc-bench run --solc ./solc input.json --pipeline evmasm --no-optimize
```

For `.sol` files, the tool wraps the source in a standard-json input.
For `.json` files, the tool overrides the compilation settings with the
requested pipeline and optimizer configuration.

Results are written to `bench-results.json` in `--output-dir` (current
directory by default).

### `solc-bench compare`

| Flag | Argument | Default | Description |
|------|----------|---------|-------------|
| `--pipelines` | `TARGET:REF` | cross-version mode | Compare two pipelines in one file (e.g. `ir:evmasm`) |
| `--format` | `table`/`json` | `table` | Output format |
| `--output` | `FILE` | (none) | Write comparison JSON to file |
| `--per-function` | `min`/`mean`/`median`/`max` | `median` | Per-function gas deltas (cross-version only) |
| `--plot` | `FILE` | (none) | Write a boxplot (requires `[plot]` extra) |
| `--plot-metric` | `METRIC[,...]` | `cpu_time` | Metric(s) to plot (comma-separated) |

Positional: `bench-results.json` (required, baseline).
`target_bench-results.json` (optional, cross-version mode only).

Compare two result files (e.g. baseline vs PR branch):

```bash
solc-bench compare baseline/bench-results.json target/bench-results.json
solc-bench compare baseline/bench-results.json target/bench-results.json --format json
solc-bench compare baseline/bench-results.json target/bench-results.json --output comparison.json
```

When both files contain per-function gas data, `--per-function` adds a
per-function delta table sorted by absolute median delta. Pass `min`,
`mean`, or `max` to change the sort key:

```bash
solc-bench compare baseline/bench-results.json target/bench-results.json --per-function
solc-bench compare baseline/bench-results.json target/bench-results.json --per-function max
```

Or compare two pipelines within a single result file (e.g. how `ir` compares to `evmasm`):

```bash
solc-bench compare bench-results.json --pipelines ir:evmasm
solc-bench compare bench-results.json --pipelines ir-ssacfg:evmasm --format json
```

The output table shows each metric's signed percent delta
`(target - ref) / ref`. Every metric is lower-is-better, so negative is
an improvement and positive is a regression. The `winner` column names
the side that wins per metric.

Plot per-iteration samples as boxplots:

```bash
solc-bench compare baseline/bench-results.json target/bench-results.json --plot diff.png
solc-bench compare bench-results.json --pipelines ir:evmasm --plot panels.png --plot-metric wall_time,instructions
```

### `solc-bench extract`

| Flag | Argument | Default | Description |
|------|----------|---------|-------------|
| `--solc` | `PATH` | required | Path to solc binary |
| `--project` | `DIR` | required | Forge project directory |
| `--output-dir` | `DIR` | project parent | Where to write the standard-json |

```bash
solc-bench extract --solc ./solc --project /path/to/forge-project --output-dir benchmark_data/
```

Produces one `.json` file per project containing the sources and base
settings. Pipeline and optimizer settings are applied at runtime by the
`run` command.

### `solc-bench extract-sourcify`

| Flag | Argument | Default | Description |
|------|----------|---------|-------------|
| `--output-dir` | `DIR` | required | Where to write JSONs and `benchmarks.toml` |
| `--top-n` | `N` | `100` | Number of mainnet contracts to extract |
| `--min-version` | `X` | `0.8.0` | solc version floor: filters older + rewrites pragmas to `>=X` |
| `--force` | — | off | Wipe `--output-dir` contents first |

```bash
solc-bench extract-sourcify --output-dir sourcify-bench/ --top-n 100
solc-bench extract-sourcify --output-dir sourcify-bench/ --top-n 50 --min-version 0.8.20 --force
```

Pulls the top-N most-used mainnet contracts from Sourcify and writes
a ready-to-run suite (JSONs + `benchmarks.toml`) into one directory.
Pragmas are rewritten to `>=<min_version>;` so the suite compiles
against any newer solc. Proxies are resolved to their implementation
and the proxy address is recorded as `proxy_address` in the TOML.

`extract-sourcify` takes the output directory for itself: it refuses
to run against a non-empty directory, and `--force` wipes it. Use
`solc-bench extract` to extend an existing suite.

### `solc-bench list`

| Flag | Argument | Default | Description |
|------|----------|---------|-------------|
| `--metrics` | — | off | List available metrics instead of benchmarks |
| `--tags` | — | off | List all tags defined across benchmarks |
| `--benchmark-dir` | `DIR` | required unless `--metrics` | Suite directory containing `benchmarks.toml` |

```bash
solc-bench list --benchmark-dir ./my-suite           # list configured benchmarks
solc-bench list --benchmark-dir ./my-suite --tags    # list tags across benchmarks
solc-bench list --metrics                            # list available metrics (no --benchmark-dir needed)
```
