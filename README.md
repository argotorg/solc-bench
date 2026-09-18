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

Compares two result files, or two pipelines in one file (`--pipelines TARGET:REF`).
`~noise` in the `winner` column means the difference isn't statistically significant or is under 0.10%.

| Flag | Default | Description |
|------|---------|-------------|
| `--pipelines TARGET:REF` | cross-version | Compare two pipelines e.g. `ir:evmasm` |
| `--output FILE` | (none) | Write comparison JSON to file |
| `--per-function [STAT]` | off | Per-function gas deltas |
| `--summary` | off | Print the summary even with fewer than 10 benchmarks |
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

### Gas-bench fixtures (real mainnet transactions)

A `gas-bench-fixtures = "<dir>"` entry in `benchmarks.toml` names a
`benchmark_data/gas/<dir>/` directory of captured real mainnet transactions.

#### Capturing fixtures

`solc-bench capture-contract <address>` finds `<address>`'s most popular
calls on mainnet and base on data fetched from JSON-RPC trace-capable endpoint
creates a gas bench fixture in the foram of [EEST](https://github.com/ethereum/execution-spec-tests), plus a stub `targets.toml`.

| Flag | Default | Description |
|------|-------|-------------|
| `--output-dir DIR` | required | Directory to write fixtures + `targets.toml` to |
| `--evmone PATH` | required | Path to the `evmone` binary |
| `--rpc-url URL` | `$ETH_RPC_URL` | Trace-capable JSON-RPC endpoint (`debug_traceTransaction` with `prestateTracer`) |
| `--etherscan-api-key KEY` | `$ETHERSCAN_API_KEY` | For discovery of the most popular transacitons |
| `--limit N` | `500` | Recent transactions to scan for popular calls |
| `--max-selectors N` | `5` | Max distinct selectors to capture |
| `--min-calls N` | `1` | Minimum call count for a selector to qualify |
| `--force` | off | Overwrite existing fixtures/`targets.toml` |
| `--end-block N` | (latest) | Only scan calls up to this block (e.g. a proxy since upgraded past the implementation being benchmarked) |
| `--target-address ADDR` | `address` | Bytecode-swap target, if different from `address` (e.g. `address` is a proxy) |

```bash
solc-bench capture-contract 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2 \
  --output-dir benchmark_data/gas/weth9 --evmone ./evmone
solc-bench run --solc ./solc --benchmark-dir ./benchmark_data --only weth9 \
  --evmone-statetest ./evmone-statetest
```

`--force` re-runs discovery from scratch and can pick different
selectors/transactions than last time. The fixtures that fall out of the new
selection are left behind rather than deleted, and `targets.toml` is fully
rewritten down to just the stub fields below. **Any hand-filled
`standard_json`/`contract_name`/`libraries`/`immutables` are lost**, so
re-fill them after a forced re-run.

#### `targets.toml`: swapping in freshly-compiled bytecode

Each `[[target]]` entry says which fixture address to swap bytecode into,
and which source to compile it from:

```toml
[[target]]
address = "0x728a138a4823392c2efa55e028d434f526fe03cf"
discovery_address = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
discovery_end_block = 25975096
discovery_limit = 500
standard_json = "aave-v3-origin-3.6.0.json"
contract_name = "PoolInstance"
source_name = "src/contracts/instances/PoolInstance.sol"

[target.libraries]
BorrowLogic = "0x52da0ce88202d1542543598d1e1e27f0d344726a"
SupplyLogic = "0x584c7d8c4cb05304fe5ac7fbc97f20a10fb07564"

[target.immutables]
ADDRESSES_PROVIDER = "0x0000000000000000000000002f39d218133afab8f2b819b1066c7e434ad94e9e"
```

`capture-contract` only ever writes the first four fields (`address`,
`discovery_address`, `discovery_end_block`, `discovery_limit`).
Everything else is filled in by hand afterward, pointing at a standard-json input
already in the benchmark suite.
`discovery_address` is the account the popular-calls scan ran against (may differ from `address` when
`--target-address` is used, as here — `address` is the `PoolInstance`
implementation, `discovery_address` the proxy calls were discovered
through).
`discovery_end_block`/`discovery_limit` record the query so it can be reproduced
later by passing them back as `--end-block`/`--limit`.
`source_name` disambiguates `contract_name` when it's declared in more than one file.
`[target.libraries]` keys are bare library names (as declared by
`library Foo { ... }`).
Every library the contract links against needs an entry, or it's deployed at a dummy placeholder
address instead.
`[target.immutables]` keys are the contract's immutable variable names.

## ETHDebug overhead

`ir-ethdebug` is unoptimized `ir` plus the ETHDebug outputs, so it requires
`--no-optimize` (gas is skipped). It adds the `ethdebug_size` metric. Measure
the overhead against plain unoptimized `ir` in the same run:

```bash
solc-bench run --pipelines ir,ir-ethdebug --no-optimize --solc ./solc --benchmark-dir ./benchmark_data -o ethdebug.json
solc-bench compare ethdebug.json --pipelines ir-ethdebug:ir
```
