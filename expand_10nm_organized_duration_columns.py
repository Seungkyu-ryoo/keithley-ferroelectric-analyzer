#!/usr/bin/env python3
"""Add empty duration blocks to the organized 10 nm Pubfig project.

Existing measured blocks are moved only by column position: their identifiers,
metadata rows, scalar values, graph-series objects, and checked selections are
preserved exactly.  Newly requested durations receive empty blocks that repeat
the sheet's existing X/Y role pattern.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
from typing import Any


TARGET_DURATIONS = (
    "asdep",
    "0.1ms",
    "0.2ms",
    "0.4ms",
    "0.8ms",
    "1.2ms",
    "1.6ms",
    "2ms",
    "5ms",
    "7.5ms",
    "10ms",
)
MEASURED_DURATIONS = ("0.4ms", "0.8ms", "1.2ms", "1.6ms", "2ms")


def normalize_duration(value: Any) -> str:
    text = str(value).strip().lower().replace(" ", "")
    if text == "2.0ms":
        return "2ms"
    return text


def duration_token(duration: str) -> str:
    return "d" + re.sub(r"[^A-Za-z0-9]+", "_", duration).strip("_")


def validate_sheet(sheet: dict[str, Any]) -> None:
    data = sheet.get("data", {})
    columns = data.get("columns")
    rows = data.get("rows")
    if not isinstance(columns, list) or not columns:
        raise ValueError(f"Sheet {sheet.get('name')!r} has no columns")
    if len(set(map(str, columns))) != len(columns):
        raise ValueError(f"Sheet {sheet.get('name')!r} has duplicate columns")
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError(f"Sheet {sheet.get('name')!r} lacks role/name rows")
    width = len(columns)
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != width:
            raise ValueError(
                f"Sheet {sheet.get('name')!r} row {row_index} has the wrong width"
            )


def measured_blocks(sheet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    validate_sheet(sheet)
    data = sheet["data"]
    roles = data["rows"][0]
    starts = [index for index, role in enumerate(roles) if str(role).strip().upper() == "X"]
    if not starts or starts[0] != 0:
        raise ValueError(f"Sheet {sheet.get('name')!r} does not start with an X block")
    stops = starts[1:] + [len(roles)]
    widths = {stop - start for start, stop in zip(starts, stops)}
    if len(widths) != 1:
        raise ValueError(f"Sheet {sheet.get('name')!r} has inconsistent block widths")

    result: dict[str, dict[str, Any]] = {}
    for start, stop in zip(starts, stops):
        names = data["rows"][1][start:stop]
        labels = {normalize_duration(name) for name in names if str(name).strip()}
        if len(labels) != 1:
            raise ValueError(
                f"Sheet {sheet.get('name')!r} block {start}:{stop} has ambiguous labels"
            )
        duration = labels.pop()
        if duration in result:
            raise ValueError(f"Sheet {sheet.get('name')!r} repeats {duration}")
        result[duration] = {
            "start": start,
            "stop": stop,
            "columns": list(map(str, data["columns"][start:stop])),
            "rows": [copy.deepcopy(row[start:stop]) for row in data["rows"]],
        }
    if tuple(result) != MEASURED_DURATIONS:
        raise ValueError(
            f"Sheet {sheet.get('name')!r} durations are {tuple(result)}, "
            f"expected {MEASURED_DURATIONS}"
        )
    return result


def empty_block(
    sheet: dict[str, Any],
    duration: str,
    template: dict[str, Any],
) -> dict[str, Any]:
    width = len(template["columns"])
    roles = copy.deepcopy(template["rows"][0])
    template_names = template["rows"][1]
    names = ["" if not str(value).strip() else duration for value in template_names]
    columns = [
        f"{duration_token(duration)}__{sheet['id']}__c{position + 1}"
        for position in range(width)
    ]
    rows = [roles, names]
    rows.extend([[None] * width for _ in sheet["data"]["rows"][2:]])
    return {"columns": columns, "rows": rows}


def expand_sheet(
    sheet: dict[str, Any], durations: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    existing = measured_blocks(sheet)
    template = existing[MEASURED_DURATIONS[0]]
    output_blocks: dict[str, dict[str, Any]] = {}
    for duration in durations:
        output_blocks[duration] = (
            existing[duration]
            if duration in existing
            else empty_block(sheet, duration, template)
        )

    output = copy.deepcopy(sheet)
    output["data"] = {
        "columns": [
            column
            for duration in durations
            for column in output_blocks[duration]["columns"]
        ],
        "rows": [
            [
                value
                for duration in durations
                for value in output_blocks[duration]["rows"][row_index]
            ]
            for row_index in range(len(sheet["data"]["rows"]))
        ],
    }
    validate_sheet(output)
    return output, output_blocks


def clone_series_for_block(
    series: dict[str, Any],
    template: dict[str, Any],
    destination: dict[str, Any],
    duration: str,
) -> dict[str, Any]:
    result = copy.deepcopy(series)
    template_positions = {
        column: index for index, column in enumerate(template["columns"])
    }
    for field in ("x", "y"):
        old_column = str(result.get(field, ""))
        if old_column not in template_positions:
            raise ValueError(f"Template series column {old_column!r} is not in its block")
        result[field] = destination["columns"][template_positions[old_column]]
    error_column = str(result.get("error_column", "")).strip()
    if error_column:
        if error_column not in template_positions:
            raise ValueError(f"Template error column {error_column!r} is not in its block")
        result["error_column"] = destination["columns"][template_positions[error_column]]
    result["label"] = duration
    return result


def expand_graph(
    graph: dict[str, Any],
    existing_blocks: dict[str, dict[str, Any]],
    output_blocks: dict[str, dict[str, Any]],
    durations: tuple[str, ...],
) -> dict[str, Any]:
    result = copy.deepcopy(graph)
    existing_columns = {
        column: duration
        for duration, block in existing_blocks.items()
        for column in block["columns"]
    }
    series_by_duration: dict[str, list[dict[str, Any]]] = {
        duration: [] for duration in existing_blocks
    }
    for series in graph.get("series_config", []):
        x_column = str(series.get("x", ""))
        y_column = str(series.get("y", ""))
        if x_column not in existing_columns or y_column not in existing_columns:
            raise ValueError(f"Graph {graph.get('name')!r} references an unknown column")
        if existing_columns[x_column] != existing_columns[y_column]:
            raise ValueError(f"Graph {graph.get('name')!r} crosses duration blocks")
        series_by_duration[existing_columns[y_column]].append(copy.deepcopy(series))

    template_duration = MEASURED_DURATIONS[0]
    template_series = series_by_duration[template_duration]
    expanded_series: list[dict[str, Any]] = []
    for duration in durations:
        if duration in series_by_duration:
            expanded_series.extend(series_by_duration[duration])
        else:
            expanded_series.extend(
                clone_series_for_block(
                    series,
                    existing_blocks[template_duration],
                    output_blocks[duration],
                    duration,
                )
                for series in template_series
            )
    result["series_config"] = expanded_series
    result.setdefault("plot_config", {})["y_label_offset_mm"] = 6.0
    return result


def validate_project(
    result: dict[str, Any],
    source: dict[str, Any],
    durations: tuple[str, ...],
) -> None:
    if result.get("tree") != source.get("tree"):
        raise ValueError("Project tree changed")
    if result.get("active_node_id") != source.get("active_node_id"):
        raise ValueError("active_node_id changed")
    source_sheets = {str(sheet["id"]): sheet for sheet in source["sheets"]}
    result_sheets = {str(sheet["id"]): sheet for sheet in result["sheets"]}
    if source_sheets.keys() != result_sheets.keys():
        raise ValueError("Sheet IDs changed")

    for sheet_id, before in source_sheets.items():
        after = result_sheets[sheet_id]
        before_blocks = measured_blocks(before)
        after_columns = list(map(str, after["data"]["columns"]))
        block_width = len(next(iter(before_blocks.values()))["columns"])
        if len(after_columns) != block_width * len(durations):
            raise ValueError(f"Sheet {before['name']!r} has the wrong expanded width")
        if len(after["data"]["rows"]) != len(before["data"]["rows"]):
            raise ValueError(f"Sheet {before['name']!r} row count changed")
        for duration_index, duration in enumerate(durations):
            start = duration_index * block_width
            stop = start + block_width
            if duration in before_blocks:
                old = before_blocks[duration]
                if after_columns[start:stop] != old["columns"]:
                    raise ValueError("A measured block's column IDs changed")
                actual_rows = [row[start:stop] for row in after["data"]["rows"]]
                if actual_rows != old["rows"]:
                    raise ValueError("A measured block's roles, names, or values changed")
            else:
                if any(
                    value is not None
                    for row in after["data"]["rows"][2:]
                    for value in row[start:stop]
                ):
                    raise ValueError("A new placeholder block contains data")

    source_graphs = {str(graph["id"]): graph for graph in source["graphs"]}
    result_graphs = {str(graph["id"]): graph for graph in result["graphs"]}
    if source_graphs.keys() != result_graphs.keys():
        raise ValueError("Graph IDs changed")
    original_columns = {
        str(sheet["id"]): set(map(str, sheet["data"]["columns"]))
        for sheet in source["sheets"]
    }
    result_columns = {
        str(sheet["id"]): set(map(str, sheet["data"]["columns"]))
        for sheet in result["sheets"]
    }
    for graph_id, before in source_graphs.items():
        after = result_graphs[graph_id]
        for key in ("id", "name", "sheet_id"):
            if after.get(key) != before.get(key):
                raise ValueError(f"Graph field {key!r} changed")
        if after.get("checked_y") != before.get("checked_y"):
            raise ValueError("A graph checked selection changed")
        for key, value in before.get("plot_config", {}).items():
            if key != "y_label_offset_mm" and after["plot_config"].get(key) != value:
                raise ValueError(f"Graph plot setting {key!r} changed")
        if after["plot_config"].get("y_label_offset_mm") != 6.0:
            raise ValueError("A graph Y-label gap is not 6 mm")
        preserved = [
            series
            for series in after.get("series_config", [])
            if str(series.get("y", "")) in original_columns[before["sheet_id"]]
        ]
        if preserved != before.get("series_config", []):
            raise ValueError("An existing graph series changed")
        columns = result_columns[after["sheet_id"]]
        configured_y: set[str] = set()
        for series in after.get("series_config", []):
            if str(series.get("x", "")) not in columns or str(series.get("y", "")) not in columns:
                raise ValueError("A graph series refers to a missing column")
            configured_y.add(str(series["y"]))
        if not set(map(str, after.get("checked_y", []))).issubset(configured_y):
            raise ValueError("checked_y refers to an unconfigured column")

    node_ids: set[str] = set()
    sheet_refs: list[str] = []
    graph_refs: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        node_id = str(node.get("id", ""))
        if not node_id or node_id in node_ids:
            raise ValueError("Invalid or duplicate tree node ID")
        node_ids.add(node_id)
        if node.get("type") == "sheet":
            sheet_refs.append(str(node.get("ref_id", "")))
        if node.get("type") == "graph":
            graph_refs.append(str(node.get("ref_id", "")))
        for child in node.get("children", []):
            walk(child)

    walk(result["tree"])
    if sorted(sheet_refs) != sorted(result_sheets):
        raise ValueError("Tree sheet references are incomplete")
    if sorted(graph_refs) != sorted(result_graphs):
        raise ValueError("Tree graph references are incomplete")
    if str(result.get("active_node_id")) not in node_ids:
        raise ValueError("active_node_id is invalid")


def transform(
    source: dict[str, Any], durations: tuple[str, ...] = TARGET_DURATIONS
) -> dict[str, Any]:
    if source.get("schema_version") != 3:
        raise ValueError("Expected Pubfig schema_version 3")
    if tuple(duration for duration in durations if duration in MEASURED_DURATIONS) != MEASURED_DURATIONS:
        raise ValueError("Requested durations must preserve measured-duration order")
    if len(durations) != len(set(durations)):
        raise ValueError("Requested durations must be unique")

    result = copy.deepcopy(source)
    blocks_by_sheet: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]] = {}
    expanded_sheets = []
    for sheet in result["sheets"]:
        existing = measured_blocks(sheet)
        expanded, output_blocks = expand_sheet(sheet, durations)
        expanded_sheets.append(expanded)
        blocks_by_sheet[str(sheet["id"])] = (existing, output_blocks)
    result["sheets"] = expanded_sheets

    result["graphs"] = [
        expand_graph(
            graph,
            blocks_by_sheet[str(graph["sheet_id"])][0],
            blocks_by_sheet[str(graph["sheet_id"])][1],
            durations,
        )
        for graph in result["graphs"]
    ]
    validate_project(result, source, durations)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.source.open(encoding="utf-8") as handle:
        source = json.load(handle)
    result = transform(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(
        json.dumps(
            {
                "source": str(args.source),
                "output": str(args.output),
                "durations": list(TARGET_DURATIONS),
                "sheet_count": len(result["sheets"]),
                "graph_count": len(result["graphs"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
