# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""Dependency-free internal defaults for the legacy cleanup helpers."""
import logging
from dataclasses import dataclass

@dataclass(frozen=True)
class _Defaults:
    default_kerf_mm: float = 3.0
    default_algorithm: str = "adaptive_frontier_release_v1"
    max_parts: int = 500
    max_sheets: int = 100

@dataclass(frozen=True)
class _Config:
    optimization: _Defaults = _Defaults()

config = _Config()

def append_optimize_trace(message: str) -> None:
    logging.getLogger("grainline.engine").debug(message)

def get_request_id() -> None:
    return None
