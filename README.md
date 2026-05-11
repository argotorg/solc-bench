# solc-bench

Benchmark tool for the Solidity compiler. Measures compile-time performance, memory usage,
hardware counters (via `perf stat`), and bytecode output size across real-world projects.

## Install

### pip

```
pip install -e .
```

### Nix (flake)

```
nix run github:argotorg/solc-bench -- run --solc /path/to/solc
```

Requires Python 3.11+. Runtime tools: `solc` (required), `perf` (optional, for hardware counters),
`forge` (optional, for input extraction and gas benchmarks).

## Usage

### Fetch a solc binary

```bash
solc-bench fetch v0.8.35      # release tag → ./solc-v0.8.35
solc-bench fetch develop      # branch → ./solc-develop (latest CircleCI b_ubu_static)
solc-bench fetch develop --output ./solc --force
```

Release tags are downloaded from the `argotorg/solidity` GitHub release.
Branches resolve to the latest successful `b_ubu_static` CircleCI artifact.
The fetch fails loudly if the ref matches neither, or if the latest pipeline's
build is not yet successful. Linux x86_64 only.

Set `CIRCLECI_TOKEN` to raise CircleCI rate limits, and `GITHUB_TOKEN` for
GitHub.

### Standard suite

The bundled benchmark suite is defined in `benchmarks.toml`, shipped with
the package. Use `solc-bench list` to see which projects are configured.

Each entry needs a corresponding `<name>.json` standard-json input. Inputs
are not packaged. `solc-bench run` reads them from `./benchmark_data/` by
default, overridable with `--benchmark-dir`. The repo's `benchmark_data/`
directory provides the canonical inputs, so running from the repo root
just works:

```bash
solc-bench run --solc ./solc
solc-bench run --solc ./solc --only openzeppelin-5.6.1
solc-bench run --solc ./solc --iterations 10
solc-bench run --solc ./solc --pipeline ir
solc-bench run --solc ./solc --no-optimize
```

Each benchmark runs with the pipelines specified in its TOML entry (or
all pipelines if unspecified). Use `--pipeline` to run a single pipeline
and `--no-optimize` to disable the optimizer.

Results are written to `bench-results.json` in the output directory (current
directory by default). Use `--output-dir DIR` to change the output directory
and `--stdout` to also print results to stdout.

```bash
solc-bench run --solc ./solc --output-dir /tmp/bench-results
solc-bench run --solc ./solc --stdout
```

### Custom benchmarks

To run benchmarks outside the bundled suite without modifying the
package, place your own `benchmarks.toml` next to the input JSONs in a
directory and point `--benchmark-dir` at it:

```
my-benchmarks/
  benchmarks.toml
  my-project.json
```

```bash
solc-bench run --solc ./solc --benchmark-dir my-benchmarks
solc-bench list --benchmark-dir my-benchmarks
```

When `--benchmark-dir` contains a `benchmarks.toml`, that file overrides
the bundled one. Generate the JSON with `solc-bench extract` (from a
Forge project) or `solc-bench extract-sourcify` (from real-world mainnet
contracts). Both are described below.

### Single file

Benchmark a `.sol` file or standard-json input:

```bash
solc-bench run --solc ./solc contract.sol
solc-bench run --solc ./solc contract.sol --pipeline ir
solc-bench run --solc ./solc contract.sol --no-optimize
solc-bench run --solc ./solc input.json
solc-bench run --solc ./solc input.json --pipeline evmasm --no-optimize
```

For `.sol` files, the tool wraps the source in a standard-json input.
For `.json` files, the tool overrides the compilation settings with the
requested pipeline and optimizer configuration. Without `--pipeline`,
all pipelines are run.

### Compare

Compare two result files (e.g. baseline vs PR branch):

```bash
solc-bench compare baseline/bench-results.json target/bench-results.json
solc-bench compare baseline/bench-results.json target/bench-results.json --format json
solc-bench compare baseline/bench-results.json target/bench-results.json --output comparison.json
```

When both result files contain per-function gas data, `--per-function`
adds a per-function delta table after the cross-version one, with `min`,
`mean`, `median`, `max` delta columns sorted by absolute median delta:

```bash
solc-bench compare baseline/bench-results.json target/bench-results.json --per-function
solc-bench compare baseline/bench-results.json target/bench-results.json --per-function max
```

Pass an explicit stat (`min`, `mean`, `median`, `max`) to change the
sort key. Useful for spotting tail regressions (sort by `max`) or
fast-path improvements (sort by `min`) that the median delta hides.

Or compare two pipelines within a single result file (e.g. how `ir` currently compares to `evmasm`):

```bash
solc-bench compare bench-results.json --pipelines ir:evmasm
solc-bench compare bench-results.json --pipelines ir-ssacfg:evmasm --format json
```

The second pipeline is the baseline. The first is compared against it.
Output shows each metric's signed percent delta `(target - ref) / ref`:
negative means the target uses less of the metric than the baseline,
positive means it uses more. Every metric is lower-is-better (time,
size, cycles, memory), so negative is always an improvement. The
`winner` column names the pipeline that wins per metric.

### Extract

Generate a standard-json input from a Forge project (used to add new benchmarks):

```bash
solc-bench extract --solc ./solc --project /path/to/forge-project --output-dir benchmark_data/
```

Produces one `.json` file per project containing the sources and base settings.
Pipeline and optimizer settings are applied at runtime by the `run` command.

### Sourcify extraction

Pull the top-N most-used mainnet contracts from Sourcify and assemble a
ready-to-run benchmark suite:

```bash
solc-bench extract-sourcify --output-dir sourcify-bench/ --top-n 100
solc-bench extract-sourcify --output-dir sourcify-bench/ --top-n 50 --min-version 0.8.20 --force
```

For each contract, the extractor fetches its standard-json input and
writes one `<bench-id>.json` per contract plus a `benchmarks.toml`
index, ready for `solc-bench run --benchmark-dir sourcify-bench/`.

Options:

- `--top-n N` — number of contracts to extract (default: 100).
- `--min-version X` — solc version floor (default: `0.8.0`). Filters
  out contracts compiled with older solc, AND rewrites every source's
  `pragma solidity ...;` to `pragma solidity >=X;` so contracts compile
  against any newer solc.
- `--force` — wipe `--output-dir` contents before writing the new suite.

Proxy contracts are resolved transparently: the verified
implementation's source is used, the proxy address is recorded as
`proxy_address` in the TOML.

### Gas benchmarks

To collect `deployment_gas` and `method_gas` for a benchmark, set
`gas = true` in its TOML entry alongside `source` and `version` (see
[Adding a benchmark](#adding-a-benchmark) for the full entry shape).

First run clones the project at the tag in `version` (must be a git
tag name, e.g. `v5.6.1`) into `<benchmark-dir>/<entry-name>/` and
invokes `forge test --gas-report --json` against it. Subsequent runs
reuse that clone with no re-cloning.

If you change `version` in the TOML, the existing clone no longer
matches. solc-bench raises `RuntimeError` and prints the exact path of
the stale clone. Delete `<benchmark-dir>/<entry-name>/` and re-run to clone at the new
version.

Without `gas = true`, gas metrics are skipped silently and only the
compile-time metrics are collected. Requires `forge` in `$PATH`.

### List

```bash
solc-bench list                    # list configured benchmarks
solc-bench list --metrics          # list available metrics
```

## Metrics

The tool always collects all available metrics. No configuration needed.

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

`instructions` is the primary metric for comparison when available. It has
the lowest variance between runs (<0.1%), compared to 3-5% for wall time.
When `perf` is not available, the tool falls back to `cpu_time`.

When gas benchmarking is enabled (see [Gas benchmarks](#gas-benchmarks)),
the result JSON stores the full forge per-function dict (`calls`, `min`,
`mean`, `median`, `max`) under `results.<name>.<pipeline>.functions`,
keyed by `ContractName.signature`. `method_gas` aggregates these to a
single number. The per-function detail is what `compare --per-function`
renders.

## Pipelines

`--pipeline` selects a single pipeline. Without it, all pipelines run
(for single files) or the ones configured in `benchmarks.toml` (for suites).
`--no-optimize` disables the optimizer (enabled by default).

| Pipeline | Description | Standard-json settings |
|----------|-------------|----------------------|
| `evmasm` | EVM assembly codegen | `"viaIR": false` |
| `ir` | IR-based codegen | `"viaIR": true` |
| `ir-ssacfg` | SSA-CFG experimental codegen | `"viaIR": true, "viaSSACFG": true` |

## Adding a benchmark

1. Extract standard-json input from a Forge project:
   ```
   solc-bench extract --solc ./solc --project /path/to/project --output-dir benchmark_data/
   ```

2. Add an entry to `src/solc_bench/benchmarks/benchmarks.toml`:
   ```toml
   ["my-project-1.0.0"]
   source = "https://github.com/example/my-project"
   version = "v1.0.0"
   pipelines = ["evmasm", "ir"]
   gas = true   # optional: also collect gas metrics via forge test --gas-report
   ```

3. Open a PR.

Note: benchmark names with dots must be quoted in TOML (e.g. `["my-project-1.0.0"]`).
