# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
# Extracted/adapted 2026-10-11; see PROVENANCE.md for original source and changes.
"""
Small normalization helpers used across routers/services.

Keep these functions dependency-free so they can be imported anywhere.
"""

from __future__ import annotations


def normalize_grain_direction(raw: str | None, *, strict: bool = False) -> str:
    """
    Normalize grain direction strings to schema-compatible values.

    Canonical values: length | width | any

    Common synonyms:
      - long / lengthwise / l / len  -> length
      - short / wide / widthwise / w -> width
      - none / n/a / ""              -> any

    When strict=False (default), unknown values fall back to "any".
    When strict=True, unknown values raise ValueError.
    """
    s = (raw or "").strip().lower()
    token = (
        s.replace("_", " ")
        .replace("-", " ")
        .replace("/", " ")
    )
    token = " ".join(token.split())
    if s in ("", "any", "none", "na", "n/a", "-", "*"):
        return "any"
    if s in ("length", "long", "lengthwise", "len", "l"):
        return "length"
    if s in ("width", "wide", "widthwise", "short", "w"):
        return "width"
    if token in ("not important", "not critical", "ignore grain", "doesnt matter", "doesn't matter"):
        return "any"
    if token in ("along length", "with length", "length direction", "along the length"):
        return "length"
    if token in ("along width", "with width", "width direction", "along the width"):
        return "width"
    if strict:
        raise ValueError("grain direction must be Not important, Along length, or Along width")
    return "any"
