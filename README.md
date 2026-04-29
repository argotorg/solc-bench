# solc-bench

Benchmark tool for the Solidity compiler. Measures compile-time performance, memory usage,
hardware counters (via `perf stat`), and bytecode output size across real-world projects.

## Install

```
pip install -e .
```

Requires Python 3.11+. Runtime tools: `solc` (required), `perf` (optional, for hardware counters),
`forge` (optional, for input extraction).

## Usage

### Standard suite

The bundled benchmark suite is defined in `benchmarks.toml`, shipped with
the package. Use `solc-bench list` to see which projects are configured.

Each entry needs a corresponding `<name>.json` standard-json input. Inputs
are not packaged. `solc-bench run` reads them from `./benchmarks/` by
default, overridable with `--benchmark-dir`. The repo's `benchmarks/`
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

Results are written to `results.json` in the output directory (current
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
solc-bench compare baseline/results.json target/results.json
solc-bench compare baseline/results.json target/results.json --format json
solc-bench compare baseline/results.json target/results.json -o comparison.json
```

### Extract

Generate a standard-json input from a Forge project (used to add new benchmarks):

```bash
solc-bench extract --solc ./solc --project /path/to/forge-project --output-dir benchmarks/
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
| `bytecode_size` | Total bytecode size of all contracts | bytes | solc standard-json output |

`instructions` is the primary metric for comparison when available. It has
the lowest variance between runs (<0.1%), compared to 3-5% for wall time.
When `perf` is not available, the tool falls back to `cpu_time`.

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
   solc-bench extract --solc ./solc --project /path/to/project --output-dir benchmarks/
   ```

2. Add an entry to `src/solc_bench/benchmarks/benchmarks.toml`:
   ```toml
   ["my-project-1.0.0"]
   source = "https://github.com/example/my-project"
   version = "v1.0.0"
   pipelines = ["evmasm", "ir"]
   ```

3. Open a PR.

Note: benchmark names with dots must be quoted in TOML (e.g. `["my-project-1.0.0"]`).
