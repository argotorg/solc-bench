# solc-bench

## Running the tool

`solc-bench` is not installed on PATH. Always run it through the Nix flake from
the repo root:

```sh
nix run . -- list --benchmark-dir ./benchmark_data
nix run . -- run --solc ./solc --benchmark-dir ./benchmark_data
nix run . -- extract --solc ./solc --project /tmp/<project> --output-dir ./benchmark_data
nix run . -- compare baseline/bench-results.json target/bench-results.json
```

`python -m solc_bench.cli` does not work (the module has no `__main__`
guard). Use `nix run . -- <subcommand>` instead.

## Benchmark suite

A benchmark is a `benchmark_data/benchmarks.toml` entry plus a matching
`benchmark_data/<key>.json` standard-json input. Generate the JSON with
`extract` on a cloned Forge project whose directory name equals the TOML key.
