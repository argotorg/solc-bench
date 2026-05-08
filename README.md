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
`forge` (optional, for input extraction).

## Usage

### Fetch a solc binary

```bash
solc-bench fetch v0.8.35      # release tag → ./solc-v0.8.35
solc-bench fetch develop      # branch → ./solc-develop (latest CircleCI b_ubu_static)
solc-bench fetch develop -o ./solc --force
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
directory by default). Use `-o DIR` to change the output directory and
`--stdout` to also print results to stdout.

```bash
solc-bench run --solc ./solc -o /tmp/bench-results
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
the bundled one. Generate the JSON with `solc-bench extract` (see below).

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
solc-bench compare baseline/bench-results.json target/bench-results.json -o comparison.json
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
A ratio of `2.17x` means the first pipeline uses 2.17x as much of the
metric as the second (e.g. instructions, time).

### Extract

Generate a standard-json input from a Forge project (used to add new benchmarks):

```bash
solc-bench extract --solc ./solc --project /path/to/forge-project --output-dir benchmark_data/
```

Produces one `.json` file per project containing the sources and base settings.
Pipeline and optimizer settings are applied at runtime by the `run` command.

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

Gas metrics (`deployment_gas`, `method_gas`) are collected only when a
benchmark has `gas = true` in its TOML entry and a Forge project is
available. The result JSON also stores the full forge per-function dict
(`calls`, `min`, `mean`, `median`, `max`) under `results.<name>.<pipeline>.functions`,
keyed by `ContractName.signature`. `method_gas` aggregates these to a
single number, the per-function detail is what `compare --per-function`
renders (see below).

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
