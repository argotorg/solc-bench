"""Gas benchmarking via forge test --gas-report.

This module defines the metrics and interface
for future gas benchmarking support.

TODO: Eventually move from forge and use evmone or similar for more direct control.
"""

METRICS = {
    "deployment_gas": ("Total deployment gas via forge test --gas-report", "gas"),
    "method_gas": ("Total method call gas via forge test --gas-report", "gas"),
}


def run_gas_benchmark(solc, project_dir):
    """Run forge test --gas-report and collect gas metrics."""
    raise NotImplementedError("Gas benchmarking is not yet implemented")
