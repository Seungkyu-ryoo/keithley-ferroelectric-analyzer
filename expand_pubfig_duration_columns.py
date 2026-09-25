#!/usr/bin/env python3
"""Expand each voltage sheet's existing columns into duration placeholders.

The source project is treated as the measured 5 ms block.  For every sheet
under a voltage folder, the complete column/role pattern is repeated in the
requested duration order.  Only the 5 ms block retains the source values;
other blocks are deliberately empty so measurement data are never fabricated.
Existing graph-series formatting is repeated for every duration.  Only the
5 ms series remain selected, so adding empty placeholders does not change the
figures currently rendered by the project.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


DEFAULT_DURATIONS = (
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
VOLTAGE_FOLDER = re.compile(r"^-?\d+(?:\.\d+)?V$")


def duration_token(duration: str) -> str:
    return "d" + re.sub(r"[^A-Za-z0-9]+", "_", duration).strip("_")


def duration_column(duration: str, original_column: str) -> str:
    return f"{duration_token(duration)}__{original_column}"


def duration_display_name(duration: str, original_name: Any) -> str:
    text = "" if original_name is None else str(original_name).strip()
    return f"{duration} | {text}" if text else duration


def voltage_sheet_ids(tree: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for child in tree.get("children", []):
        if child.get("type") != "folder" or not VOLTAGE_FOLDER.fullmatch(
            str(child.get("name", ""))
        ):
            continue
        for node in child.get("children", []):
            if node.get("type") == "sheet" and node.get("ref_id"):
                result.add(str(node["ref_id"]))
    return result


def validate_source_sheet(sheet: dict[str, Any]) -> None:
    data = sheet.get("data", {})
    columns = data.get("columns")
    rows = data.get("rows")
    if not isinstance(columns, list) or not columns:
        raise ValueError(f"Sheet {sheet.get('name')!r} has no columns")
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError(f"Sheet {sheet.get('name')!r} lacks role/name rows")
    width = len(columns)
    if len(set(map(str, columns))) != width:
        raise ValueError(f"Sheet {sheet.get('name')!r} has duplicate columns")
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != width:
            raise ValueError(
                f"Sheet {sheet.get('name')!r} row {index} has width "
                f"{len(row) if isinstance(row, list) else 'non-list'}, expected {width}"
            )


def expand_sheet(
    sheet: dict[str, Any],
    durations: tuple[str, ...],
    source_duration: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    validate_source_sheet(sheet)
    data = sheet["data"]
    original_columns = [str(column) for column in data["columns"]]
    original_rows = data["rows"]
    roles = list(original_rows[0])
    names = list(original_rows[1])
    width = len(original_columns)

    expanded_columns = [
        duration_column(duration, column)
        for duration in durations
        for column in original_columns
    ]
    expanded_roles = [role for _duration in durations for role in roles]
    expanded_names = [
        duration_display_name(duration, name)
        for duration in durations
        for name in names
    ]
    expanded_rows: list[list[Any]] = [expanded_roles, expanded_names]
    empty_block = [None] * width
    for original_row in original_rows[2:]:
        expanded_row: list[Any] = []
        for duration in durations:
            expanded_row.extend(
                copy.deepcopy(original_row)
                if duration == source_duration
                else empty_block
            )
        expanded_rows.append(expanded_row)

    result = copy.deepcopy(sheet)
    result["data"] = {
        "columns": expanded_columns,
        "rows": expanded_rows,
    }
    source_mapping = {
        column: duration_column(source_duration, column)
        for column in original_columns
    }
    return result, source_mapping


def append_metadata(
    project: dict[str, Any],
    durations: tuple[str, ...],
    source_duration: str,
    y_label_gap_mm: float | None,
) -> None:
    metadata = next(
        (sheet for sheet in project.get("sheets", []) if sheet.get("name") == "Metadata"),
        None,
    )
    if metadata is None:
        return
    data = metadata.get("data", {})
    if data.get("columns") != ["key", "value"]:
        return
    rows = data.get("rows", [])
    existing_keys = {
        row[0]
        for row in rows
        if isinstance(row, list) and len(row) >= 2 and isinstance(row[0], str)
    }
    additions: list[tuple[str, Any]] = [
        ("organized_duration_columns", "; ".join(durations)),
        ("organized_source_duration", source_duration),
        (
            "organized_placeholder_note",
            "Only the 5ms block contains measured values; all added duration blocks are empty structural placeholders.",
        ),
        ("organized_generated_utc", datetime.now(timezone.utc).isoformat()),
    ]
    if y_label_gap_mm is not None:
        additions.append(("organized_y_label_gap_mm", y_label_gap_mm))
    for key, value in additions:
        if key not in existing_keys:
            rows.append([key, value])


def transform_project(
    source: dict[str, Any],
    durations: tuple[str, ...] = DEFAULT_DURATIONS,
    source_duration: str = "5ms",
    y_label_gap_mm: float | None = None,
) -> dict[str, Any]:
    if source.get("schema_version") != 3:
        raise ValueError("Expected a Pubfig schema_version 3 project")
    if source_duration not in durations:
        raise ValueError("source_duration must be included in durations")
    if len(durations) != len(set(durations)):
        raise ValueError("durations must be unique")

    project = copy.deepcopy(source)
    target_sheet_ids = voltage_sheet_ids(project.get("tree", {}))
    if not target_sheet_ids:
        raise ValueError("No voltage-folder sheets found")

    mappings: dict[str, dict[str, str]] = {}
    expanded_sheets: list[dict[str, Any]] = []
    for sheet in project.get("sheets", []):
        sheet_id = str(sheet.get("id", ""))
        if sheet_id in target_sheet_ids:
            expanded, mapping = expand_sheet(sheet, durations, source_duration)
            expanded_sheets.append(expanded)
            mappings[sheet_id] = mapping
        else:
            expanded_sheets.append(sheet)
    project["sheets"] = expanded_sheets

    for graph in project.get("graphs", []):
        if y_label_gap_mm is not None:
            graph.setdefault("plot_config", {})["y_label_offset_mm"] = float(
                y_label_gap_mm
            )
        sheet_id = str(graph.get("sheet_id", ""))
        mapping = mappings.get(sheet_id)
        if mapping is None:
            continue
        original_series = copy.deepcopy(graph.get("series_config", []))
        expanded_series: list[dict[str, Any]] = []
        for duration in durations:
            for original in original_series:
                old_x = str(original.get("x", ""))
                old_y = str(original.get("y", ""))
                if old_x not in mapping or old_y not in mapping:
                    raise ValueError(
                        f"Graph {graph.get('name')!r} references a column outside its sheet"
                    )
                series = copy.deepcopy(original)
                series["x"] = duration_column(duration, old_x)
                series["y"] = duration_column(duration, old_y)
                old_error = str(series.get("error_column", "")).strip()
                if old_error:
                    if old_error not in mapping:
                        raise ValueError(
                            f"Graph {graph.get('name')!r} has an error column outside its sheet"
                        )
                    series["error_column"] = duration_column(duration, old_error)
                old_label = str(series.get("label", "")).strip()
                series["label"] = (
                    f"{duration} | {old_label}" if old_label else duration
                )
                expanded_series.append(series)
        graph["series_config"] = expanded_series
        graph["checked_y"] = [
            mapping[str(column)] for column in graph.get("checked_y", [])
        ]

    root = project.get("tree", {})
    if root.get("type") == "folder":
        root["name"] = "HfO2_5nm organized"
    append_metadata(project, durations, source_duration, y_label_gap_mm)
    validate_project(
        project,
        source,
        target_sheet_ids,
        durations,
        source_duration,
        y_label_gap_mm,
    )
    return project


def validate_project(
    project: dict[str, Any],
    source: dict[str, Any],
    target_sheet_ids: set[str],
    durations: tuple[str, ...],
    source_duration: str,
    y_label_gap_mm: float | None,
) -> None:
    source_sheets = {str(item["id"]): item for item in source["sheets"]}
    output_sheets = {str(item["id"]): item for item in project["sheets"]}
    if source_sheets.keys() != output_sheets.keys():
        raise ValueError("Sheet IDs changed during expansion")

    for sheet_id in target_sheet_ids:
        before = source_sheets[sheet_id]
        after = output_sheets[sheet_id]
        validate_source_sheet(after)
        old_columns = [str(item) for item in before["data"]["columns"]]
        old_rows = before["data"]["rows"]
        new_columns = after["data"]["columns"]
        new_rows = after["data"]["rows"]
        width = len(old_columns)
        if len(new_columns) != width * len(durations):
            raise ValueError(f"Incorrect expanded width for sheet {before['name']!r}")
        if len(new_rows) != len(old_rows):
            raise ValueError(f"Row count changed for sheet {before['name']!r}")
        for duration_index, duration in enumerate(durations):
            start = duration_index * width
            stop = start + width
            expected_columns = [
                duration_column(duration, column) for column in old_columns
            ]
            if new_columns[start:stop] != expected_columns:
                raise ValueError(f"Column block mismatch for {duration}")
            if new_rows[0][start:stop] != old_rows[0]:
                raise ValueError(f"Role block mismatch for {duration}")
            for row_index in range(2, len(new_rows)):
                block = new_rows[row_index][start:stop]
                if duration == source_duration:
                    if block != old_rows[row_index]:
                        raise ValueError("Measured source block changed")
                elif any(value is not None for value in block):
                    raise ValueError("A placeholder block contains fabricated values")

    columns_by_sheet = {
        str(sheet["id"]): set(map(str, sheet["data"]["columns"]))
        for sheet in project["sheets"]
    }
    sheet_ids = set(columns_by_sheet)
    graph_ids = {str(graph["id"]) for graph in project.get("graphs", [])}
    for graph in project.get("graphs", []):
        if y_label_gap_mm is not None and graph.get("plot_config", {}).get(
            "y_label_offset_mm"
        ) != float(y_label_gap_mm):
            raise ValueError("A graph has the wrong Y-label gap")
        sheet_id = str(graph.get("sheet_id", ""))
        if sheet_id not in sheet_ids:
            raise ValueError("Graph refers to a missing sheet")
        columns = columns_by_sheet[sheet_id]
        configured_y: set[str] = set()
        for series in graph.get("series_config", []):
            x_column = str(series.get("x", ""))
            y_column = str(series.get("y", ""))
            if x_column not in columns or y_column not in columns:
                raise ValueError("Graph series refers to a missing column")
            configured_y.add(y_column)
        if not set(map(str, graph.get("checked_y", []))).issubset(configured_y):
            raise ValueError("checked_y refers to an unconfigured series")

    node_ids: set[str] = set()
    found_graph_refs: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        node_id = str(node.get("id", ""))
        if not node_id or node_id in node_ids:
            raise ValueError("Invalid or duplicate tree node ID")
        node_ids.add(node_id)
        if node.get("type") == "sheet" and str(node.get("ref_id")) not in sheet_ids:
            raise ValueError("Tree sheet node has an invalid reference")
        if node.get("type") == "graph":
            ref_id = str(node.get("ref_id"))
            if ref_id not in graph_ids:
                raise ValueError("Tree graph node has an invalid reference")
            found_graph_refs.add(ref_id)
        for child in node.get("children", []):
            walk(child)

    walk(project["tree"])
    if found_graph_refs != graph_ids:
        raise ValueError("Tree graph references do not match graph objects")
    if str(project.get("active_node_id")) not in node_ids:
        raise ValueError("active_node_id is not a tree node")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--durations",
        nargs="+",
        default=list(DEFAULT_DURATIONS),
    )
    parser.add_argument("--source-duration", default="5ms")
    parser.add_argument("--y-label-gap-mm", type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.source.open(encoding="utf-8") as handle:
        source = json.load(handle)
    project = transform_project(
        source,
        tuple(args.durations),
        args.source_duration,
        args.y_label_gap_mm,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(project, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(
        json.dumps(
            {
                "source": str(args.source),
                "output": str(args.output),
                "durations": args.durations,
                "voltage_sheet_count": len(voltage_sheet_ids(project["tree"])),
                "sheet_count": len(project["sheets"]),
                "graph_count": len(project["graphs"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
