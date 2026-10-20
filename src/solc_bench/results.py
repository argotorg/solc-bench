"""The result file `solc-bench run` writes and `compare` reads.

On disk, a pipeline's metrics sit directly next to its "functions" entry;
the models keep the two apart and convert at the file boundary.
"""

import json
import statistics
from typing import Any

from pydantic import BaseModel, ValidationError, model_serializer, model_validator

# Sizes and gas are ints, times are floats. Ints must stay ints in the JSON.
Number = int | float


class Stats(BaseModel):
    """One metric over a benchmark's iterations."""

    values: list[Number]
    median: Number
    mean: Number
    stddev: float | None = None

    @classmethod
    def from_samples(cls, values: list[Number]) -> "Stats":
        return cls(
            values=values,
            median=statistics.median(values),
            mean=statistics.mean(values),
            stddev=statistics.stdev(values) if len(values) > 1 else None,
        )


class FunctionGas(BaseModel):
    """Gas of one function. `forge test --gas-report` gives calls and
    min/mean/median/max over those calls; a replayed mainnet fixture gives
    the values/median/mean of its single replay."""

    calls: int | None = None
    min: Number | None = None
    mean: Number | None = None
    median: Number | None = None
    max: Number | None = None
    values: list[Number] | None = None


class PipelineResult(BaseModel):
    """One benchmark compiled with one pipeline."""

    metrics: dict[str, Stats] = {}
    functions: dict[str, FunctionGas] = {}

    @classmethod
    def from_samples(cls, samples: list[dict[str, Number]]) -> "PipelineResult":
        """Aggregate per-iteration `{metric: value}` samples."""
        names = sorted({name for sample in samples for name in sample})
        return cls(
            metrics={
                name: Stats.from_samples([s[name] for s in samples if name in s])
                for name in names
            }
        )

    @model_validator(mode="before")
    @classmethod
    def _from_file_layout(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "metrics" in data:
            return data
        metrics = dict(data)
        functions = metrics.pop("functions", {})
        # Written by older versions; always 0, results with compile errors aren't recorded.
        metrics.pop("errors", None)
        return {"metrics": metrics, "functions": functions}

    @model_serializer
    def _to_file_layout(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            name: stats.model_dump(exclude_none=True) for name, stats in self.metrics.items()
        }
        if self.functions:
            out["functions"] = {
                name: gas.model_dump(exclude_none=True) for name, gas in self.functions.items()
            }
        return out


class RunInfo(BaseModel):
    """Which solc a result file measured, how, and on what host."""

    solc_bench_version: str | None = None
    solc_version: str = "unknown"
    timestamp: str = ""
    iterations: int | None = None
    hardware: dict[str, Any] = {}
    environment: dict[str, Any] = {}


class ResultFile(RunInfo):
    """A `solc-bench run` result: results[benchmark][pipeline]."""

    results: dict[str, dict[str, PipelineResult]] = {}

    @classmethod
    def load(cls, path) -> "ResultFile":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        try:
            return cls.model_validate(data)
        except ValidationError as e:
            problems = "\n".join(
                f"  {'.'.join(map(str, error['loc']))}: {error['msg']}" for error in e.errors()
            )
            raise ValueError(f"{path} is not a solc-bench result file:\n{problems}") from None

    @property
    def run_info(self) -> RunInfo:
        return RunInfo(**{name: getattr(self, name) for name in RunInfo.model_fields})

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
