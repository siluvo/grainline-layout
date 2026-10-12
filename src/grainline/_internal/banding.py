# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
# Extracted/adapted 2026-10-11; see PROVENANCE.md for original source and changes.
from __future__ import annotations

from collections import defaultdict
from typing import Any


EDGE_KEYS = (
    "length_edge_1",
    "length_edge_2",
    "width_edge_1",
    "width_edge_2",
)

EDGE_DISPLAY_LABELS = {
    "length_edge_1": "Length edge 1",
    "length_edge_2": "Length edge 2",
    "width_edge_1": "Width edge 1",
    "width_edge_2": "Width edge 2",
}


def banding_material_id_fields() -> tuple[str, ...]:
    return tuple(f"{edge_key}_material_id" for edge_key in EDGE_KEYS)


def collect_banding_material_ids(parts: list[Any] | tuple[Any, ...] | None) -> list[str]:
    ids: set[str] = set()
    for part in parts or []:
        for field in banding_material_id_fields():
            value = getattr(part, field, None)
            if value:
                ids.add(str(value))
    return sorted(ids)


def serialize_banding_material(material: Any) -> dict[str, Any]:
    return {
        "id": str(getattr(material, "id", "") or ""),
        "code": str(getattr(material, "code", "") or ""),
        "name": str(getattr(material, "name", "") or ""),
        "thickness_mm": round(float(getattr(material, "thickness_mm", 0) or 0), 4),
        "active": bool(getattr(material, "active", True)),
    }


def _edge_material_payload(part: Any, edge_key: str, materials_by_id: dict[str, Any]) -> dict[str, Any] | None:
    material_id = getattr(part, f"{edge_key}_material_id", None)
    if not material_id:
        return None
    material = materials_by_id.get(str(material_id))
    if material is None:
        return {
            "edge_key": edge_key,
            "edge_label": EDGE_DISPLAY_LABELS[edge_key],
            "material_id": str(material_id),
            "material_code": None,
            "material_name": None,
            "thickness_mm": None,
            "active": None,
        }
    payload = serialize_banding_material(material)
    return {
        "edge_key": edge_key,
        "edge_label": EDGE_DISPLAY_LABELS[edge_key],
        "material_id": payload["id"],
        "material_code": payload["code"],
        "material_name": payload["name"],
        "thickness_mm": payload["thickness_mm"],
        "active": payload["active"],
    }


def build_part_banding_payload(part: Any, materials_by_id: dict[str, Any] | None = None) -> dict[str, Any]:
    materials_by_id = materials_by_id or {}
    edges: dict[str, dict[str, Any] | None] = {}
    length_edge_decrement = 0.0
    width_edge_decrement = 0.0
    active_edges: list[dict[str, Any]] = []

    for edge_key in EDGE_KEYS:
        payload = _edge_material_payload(part, edge_key, materials_by_id)
        edges[edge_key] = payload
        if not payload or payload.get("thickness_mm") is None:
            continue
        thickness = round(float(payload["thickness_mm"] or 0), 4)
        if edge_key.startswith("length_edge"):
            length_edge_decrement += thickness
        else:
            width_edge_decrement += thickness
        active_edges.append(payload)

    finished_length = round(float(getattr(part, "length_mm", 0) or 0), 4)
    finished_width = round(float(getattr(part, "width_mm", 0) or 0), 4)
    cut_length = round(max(0.0, finished_length - width_edge_decrement), 4)
    cut_width = round(max(0.0, finished_width - length_edge_decrement), 4)
    return {
        "has_banding": bool(active_edges),
        "finished_length_mm": finished_length,
        "finished_width_mm": finished_width,
        "cut_length_mm": cut_length,
        "cut_width_mm": cut_width,
        "decrement_length_mm": round(width_edge_decrement, 4),
        "decrement_width_mm": round(length_edge_decrement, 4),
        "edges": edges,
        "active_edges": active_edges,
    }


def build_display_edge_map(*, rotated: bool) -> dict[str, str]:
    if not rotated:
        return {
            "length_edge_1": "top",
            "length_edge_2": "bottom",
            "width_edge_1": "left",
            "width_edge_2": "right",
        }
    return {
        "length_edge_1": "left",
        "length_edge_2": "right",
        "width_edge_1": "top",
        "width_edge_2": "bottom",
    }


def build_rotated_banding_payload(banding_payload: dict[str, Any] | None, *, rotated: bool) -> dict[str, Any] | None:
    if not isinstance(banding_payload, dict):
        return None
    edge_map = build_display_edge_map(rotated=rotated)
    next_edges: dict[str, Any] = {}
    for edge_key, edge_payload in (banding_payload.get("edges") or {}).items():
        if not isinstance(edge_payload, dict):
            next_edges[edge_key] = edge_payload
            continue
        next_edge = dict(edge_payload)
        next_edge["display_edge"] = edge_map.get(edge_key)
        next_edges[edge_key] = next_edge
    next_payload = dict(banding_payload)
    next_payload["edges"] = next_edges
    next_payload["display_edge_map"] = edge_map
    next_payload["rotated"] = bool(rotated)
    return next_payload


def summarize_banding_from_board_results(board_results: list[dict[str, Any]] | None) -> dict[str, Any]:
    materials: dict[str, dict[str, Any]] = {}
    part_rows: list[dict[str, Any]] = []
    total_edges = 0
    total_parts = 0

    for board_group in board_results or []:
        for sheet in (board_group or {}).get("layouts") or []:
            for placement in (sheet or {}).get("placements") or []:
                banding = placement.get("banding") if isinstance(placement, dict) else None
                if not isinstance(banding, dict) or not banding.get("has_banding"):
                    continue
                total_parts += 1
                active_edges = []
                for edge_key, edge in (banding.get("edges") or {}).items():
                    if not isinstance(edge, dict) or not edge.get("material_id"):
                        continue
                    total_edges += 1
                    active_edges.append(
                        {
                            "edge_key": edge_key,
                            "edge_label": edge.get("edge_label"),
                            "display_edge": edge.get("display_edge"),
                            "material_id": edge.get("material_id"),
                            "material_code": edge.get("material_code"),
                            "material_name": edge.get("material_name"),
                            "thickness_mm": edge.get("thickness_mm"),
                        }
                    )
                    material_key = str(edge.get("material_id"))
                    bucket = materials.setdefault(
                        material_key,
                        {
                            "material_id": material_key,
                            "material_code": edge.get("material_code"),
                            "material_name": edge.get("material_name"),
                            "thickness_mm": edge.get("thickness_mm"),
                            "edge_count": 0,
                            "part_count": 0,
                        },
                    )
                    bucket["edge_count"] += 1
                seen_materials = {str(item["material_id"]) for item in active_edges if item.get("material_id")}
                for material_key in seen_materials:
                    materials[material_key]["part_count"] += 1
                part_rows.append(
                    {
                        "part_id": placement.get("part_id"),
                        "part_name": placement.get("part_name"),
                        "board_id": board_group.get("board_id"),
                        "sheet_id": sheet.get("id"),
                        "finished_length_mm": placement.get("length_mm"),
                        "finished_width_mm": placement.get("width_mm"),
                        "cut_length_mm": placement.get("cut_length_mm"),
                        "cut_width_mm": placement.get("cut_width_mm"),
                        "edges": active_edges,
                    }
                )

    material_rows = sorted(
        materials.values(),
        key=lambda item: (str(item.get("material_code") or ""), str(item.get("material_name") or "")),
    )
    return {
        "has_banding": bool(part_rows),
        "total_parts": total_parts,
        "total_edges": total_edges,
        "materials": material_rows,
        "parts": part_rows,
    }


def banding_material_usage(parts: list[Any] | None) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for part in parts or []:
        for field in banding_material_id_fields():
            value = getattr(part, field, None)
            if value:
                counts[str(value)] += 1
    return dict(counts)
