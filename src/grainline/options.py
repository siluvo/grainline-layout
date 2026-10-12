# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""Explicit, per-call search budgets. No environment variables or global tuning."""
from dataclasses import asdict, dataclass, replace
import math


@dataclass(frozen=True)
class SearchOptions:
    """Balanced defaults; time limits are optional and cooperative (not hard deadlines)."""

    seed_limit: int = 5
    candidate_pool_limit: int = 18
    random_seed: int = 20261011
    partition_tries: int = 6000
    partition_plans: int = 3
    seed_early_stop: bool = True
    max_sheets: int = 100
    time_limit: float | None = None
    partition_time_limit: float | None = None

    def __post_init__(self) -> None:
        for name, minimum in (("seed_limit", 2), ("candidate_pool_limit", 8),
                              ("partition_tries", 0), ("partition_plans", 1),
                              ("max_sheets", 1), ("random_seed", 0)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if type(self.seed_early_stop) is not bool:
            raise ValueError("seed_early_stop must be a boolean")
        for name in ("time_limit", "partition_time_limit"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be a finite positive number of seconds or null")

    @classmethod
    def from_preset(cls, preset: str = "balanced", **overrides) -> "SearchOptions":
        profiles = {
            "fast": dict(seed_limit=2, candidate_pool_limit=8, partition_tries=500, partition_plans=1),
            "balanced": {},
            "thorough": dict(seed_limit=10, candidate_pool_limit=36, partition_tries=24000,
                             partition_plans=8, seed_early_stop=False),
        }
        if preset not in profiles:
            raise ValueError("preset must be fast, balanced, or thorough")
        return replace(cls(**profiles[preset]), **overrides)

    def to_dict(self) -> dict:
        return asdict(self)
