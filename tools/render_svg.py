# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""Draw every sheet of a Grainline result as one SVG picture.

    grainline optimize examples/cabinet-set-49.json -o result.json
    python tools/render_svg.py result.json -o layout.svg

This is a plain overview drawing. It has no dimension lines, cut order or
edge-banding marks; those are part of the Grainline web application.
"""
import argparse
import json
from html import escape
from pathlib import Path

SHEET_WIDTH_PX = 460
GAP_PX = 28
TITLE_PX = 22


def label(rect, scale):
    """Part name and size, turned to run along the longer side of the part."""
    width, height = rect['width'] * scale, rect['height'] * scale
    cx, cy = (rect['x'] + rect['width'] / 2) * scale, (rect['y'] + rect['height'] / 2) * scale
    along, across = max(width, height), min(width, height)
    size = f"{rect['cut_length_mm']:g} x {rect['cut_width_mm']:g}"
    lines = [escape(str(rect.get('part_name', ''))), size]
    if across < 26 or along < 7 * max(len(line) for line in lines):
        lines = [size]
    if across < 12 or along < 6.2 * len(lines[0]):
        return ''
    turn = f' transform="rotate(-90 {cx:.1f} {cy:.1f})"' if height > width else ''
    start = cy - (len(lines) - 1) * 6
    return ''.join(f'<text x="{cx:.1f}" y="{start + index * 12:.1f}"{turn}>{line}</text>'
                   for index, line in enumerate(lines))


def render(result, columns):
    sheets = [(group, layout) for group in result['boards'] for layout in group['layouts']]
    columns = min(columns, len(sheets))
    cells, x, y, row_height = [], GAP_PX, GAP_PX, 0
    for index, (group, layout) in enumerate(sheets):
        scale = SHEET_WIDTH_PX / group['usable_length_mm']
        height = group['usable_width_mm'] * scale
        if index and index % columns == 0:
            x, y, row_height = GAP_PX, y + row_height + TITLE_PX + GAP_PX, 0
        title = (f"{escape(group['board_id'])} sheet {layout['id']} of {len(group['layouts'])}"
                 f" ({group['usable_length_mm']:g} x {group['usable_width_mm']:g} mm,"
                 f" {layout['utilization']:.0f}% used)")
        parts = ''.join(
            f'<rect x="{r["x"] * scale:.1f}" y="{r["y"] * scale:.1f}" width="{r["width"] * scale:.1f}"'
            f' height="{r["height"] * scale:.1f}" class="part"/>{label(r, scale)}'
            for r in layout['placements'])
        cells.append(f'<g transform="translate({x} {y})"><text class="title" x="0" y="14">{title}</text>'
                     f'<g transform="translate(0 {TITLE_PX})"><rect width="{SHEET_WIDTH_PX}" height="{height:.1f}"'
                     f' class="sheet"/>{parts}</g></g>')
        x += SHEET_WIDTH_PX + GAP_PX
        row_height = max(row_height, height)
    total_width = GAP_PX + columns * (SHEET_WIDTH_PX + GAP_PX)
    total_height = y + row_height + TITLE_PX + GAP_PX
    style = ('text{font:9px system-ui,sans-serif;fill:#1f3d1a;text-anchor:middle;dominant-baseline:middle}'
             '.title{font-size:12px;font-weight:600;fill:#333;text-anchor:start;dominant-baseline:auto}'
             '.sheet{fill:#ead9b5;stroke:#8a7a55}.part{fill:#8fbf7a;stroke:#3f6b33;stroke-width:.8}')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_width} {total_height:.0f}"'
            f' width="{total_width}" height="{total_height:.0f}"><style>{style}</style>'
            f'<rect width="100%" height="100%" fill="#fbfaf7"/>{"".join(cells)}</svg>\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('result', type=Path, help='JSON written by grainline optimize')
    parser.add_argument('-o', '--output', type=Path, required=True)
    parser.add_argument('--columns', type=int, default=2, help='sheets per row (default 2)')
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding='utf-8'))
    args.output.write_text(render(result, max(1, args.columns)), encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
