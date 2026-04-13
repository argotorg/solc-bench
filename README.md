# solc-bench

Precision benchmark tool for the Solidity compiler (`solc`).

Measures compile-time performance, memory usage, hardware counters (via `perf stat`),
and bytecode output size across real-world projects.

## Install

```
pip install -e .
```

Requires Python 3.11+. No Python dependencies (stdlib only).

Runtime tools: `solc` (required), `perf` (optional, for hardware counters),
`forge` (optional, for input extraction).

## Modes

### Standard suite

Run all configured benchmarks from `benchmarks/benchmarks.toml`:

```bash
solc-bench run --solc ./solc
solc-bench run --solc ./solc --only openzeppelin-5.6.1
solc-bench run --solc ./solc --iterations 10 -o results.json
```

Each benchmark is compiled with all configured pipelines (e.g. legacy and IR).

### Ad-hoc

Benchmark a single `.sol` file or standard-json input:

```bash
solc-bench run --solc ./solc contract.sol
solc-bench run --solc ./solc contract.sol --pipeline ir
solc-bench run --solc ./solc contract.sol --pipeline ir-ssacfg
solc-bench run --solc ./solc contract.sol --no-optimize
solc-bench run --solc ./solc input.json
```

For `.sol` files, the tool wraps the source in a standard-json input with
full output selection. The `--pipeline` and `--no-optimize` flags control
the compilation settings (these only apply to `.sol` files, not `.json` inputs
which already contain their own settings).

### Compare

Compare two result files (e.g. baseline vs PR branch):

```bash
solc-bench compare baseline.json target.json
solc-bench compare baseline.json target.json --format json
solc-bench compare baseline.json target.json -o comparison.json
```

### Extract

Generate standard-json inputs from a Forge project (used to add new benchmarks):

```bash
solc-bench extract --solc ./solc --project /path/to/forge-project --output-dir benchmarks/
```

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

Available for ad-hoc `.sol` file benchmarks via `--pipeline`:

| Pipeline | Description | Standard-json settings |
|----------|-------------|----------------------|
| `legacy` | Legacy codegen (default) | (none) |
| `ir` | IR-based codegen | `"viaIR": true` |
| `ir-ssacfg` | SSA-CFG experimental codegen | `"viaIR": true, "viaSSACFG": true` |

## Adding a benchmark

1. Extract standard-json inputs from a Forge project:
   ```
   solc-bench extract --solc ./solc --project /path/to/project --output-dir benchmarks/
   ```

2. Add an entry to `benchmarks/benchmarks.toml`:
   ```toml
   ["my-project-1.0.0"]
   source = "https://github.com/example/my-project"
   version = "v1.0.0"
   pipelines = ["legacy", "ir"]
   ```

3. Open a PR.

Note: benchmark names with dots must be quoted in TOML (e.g. `["my-project-1.0.0"]`).

