#!/usr/bin/env python3
"""Merge measured 2 ms Pubfig data into the organized 5 nm project.

The organized project already contains duration-major placeholder blocks.  This
script replaces the 2 ms placeholders for voltages present in the measured
source project, while preserving every other duration and all existing graph
selections.  If the measured 2 ms source has an additional loop cycle, its
columns and the required table rows are inserted so no measurement is dropped.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any


DURATION = "2ms"
DURATION_PREFIX = "d2ms__"
VOLTAGE_FOLDER = re.compile(r"^-?\d+(?:\.\d+)?V$")
LOOP_COMPONENTS = {
    "PUND loops": ("Vloop", "Ploop"),
    "PV loops": ("V", "P", "J"),
}


def prefixed(column: str) -> str:
    return f"{DURATION_PREFIX}{column}"


def display_name(name: Any) -> str:
    text = "" if name is None else str(name).strip()
    return f"{DURATION} | {text}" if text else DURATION


def project_index(project: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    sheets = {str(sheet["id"]): sheet for sheet in project.get("sheets", [])}
    graphs = {str(graph["id"]): graph for graph in project.get("graphs", [])}
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for folder in project.get("tree", {}).get("children", []):
        voltage = str(folder.get("name", ""))
        if folder.get("type") != "folder" or not VOLTAGE_FOLDER.fullmatch(voltage):
            continue
        voltage_items: dict[str, dict[str, Any]] = {}
        for node in folder.get("children", []):
            if node.get("type") != "sheet":
                continue
            sheet_id = str(node.get("ref_id", ""))
            if sheet_id not in sheets:
                raise ValueError(f"Tree refers to missing sheet {sheet_id!r}")
            sheet = sheets[sheet_id]
            graph_items = []
            for child in node.get("children", []):
                if child.get("type") != "graph":
                    continue
                graph_id = str(child.get("ref_id", ""))
                if graph_id not in graphs:
                    raise ValueError(f"Tree refers to missing graph {graph_id!r}")
                graph_items.append(graphs[graph_id])
            name = str(sheet.get("name", ""))
            if name in voltage_items:
                raise ValueError(f"Duplicate sheet {voltage}/{name}")
            voltage_items[name] = {"sheet": sheet, "graphs": graph_items}
        result[voltage] = voltage_items
    return result


def validate_sheet(sheet: dict[str, Any]) -> None:
    data = sheet.get("data", {})
    columns = data.get("columns")
    rows = data.get("rows")
    if not isinstance(columns, list) or not columns:
        raise ValueError(f"Sheet {sheet.get('name')!r} has no columns")
    if len(set(map(str, columns))) != len(columns):
        raise ValueError(f"Sheet {sheet.get('name')!r} has duplicate columns")
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError(f"Sheet {sheet.get('name')!r} lacks metadata rows")
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(
                f"Sheet {sheet.get('name')!r} row {row_index} has the wrong width"
            )


def duration_bounds(sheet: dict[str, Any]) -> tuple[int, int]:
    columns = list(map(str, sheet["data"]["columns"]))
    indices = [index for index, column in enumerate(columns) if column.startswith(DURATION_PREFIX)]
    if not indices:
        raise ValueError(f"Sheet {sheet.get('name')!r} has no {DURATION} block")
    start, stop = indices[0], indices[-1] + 1
    if indices != list(range(start, stop)):
        raise ValueError(f"Sheet {sheet.get('name')!r} has a split {DURATION} block")
    return start, stop


def cycle_key(column: str) -> Decimal:
    base = column[len(DURATION_PREFIX) :] if column.startswith(DURATION_PREFIX) else column
    token = base.rsplit("_", 1)[-1]
    try:
        return Decimal(token)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse cycle from column {column!r}") from exc


def component(column: str) -> str:
    base = column[len(DURATION_PREFIX) :] if column.startswith(DURATION_PREFIX) else column
    return base.split("_", 1)[0]


def loop_groups(
    columns: list[str], expected_components: tuple[str, ...]
) -> dict[Decimal, list[int]]:
    groups: dict[Decimal, list[int]] = {}
    for index, column in enumerate(columns):
        groups.setdefault(cycle_key(column), []).append(index)
    for cycle, indices in groups.items():
        actual = tuple(component(columns[index]) for index in indices)
        if actual != expected_components:
            raise ValueError(
                f"Cycle {cycle} has components {actual}, expected {expected_components}"
            )
    return groups


def padded(values: list[Any], length: int) -> list[Any]:
    return list(values) + [None] * (length - len(values))


def merge_sheet(
    target_sheet: dict[str, Any],
    source_sheet: dict[str, Any],
    condition_label: str,
) -> dict[str, Any]:
    validate_sheet(target_sheet)
    validate_sheet(source_sheet)
    target_data = target_sheet["data"]
    source_data = source_sheet["data"]
    target_columns = list(map(str, target_data["columns"]))
    source_columns = list(map(str, source_data["columns"]))
    target_rows = copy.deepcopy(target_data["rows"])
    source_rows = source_data["rows"]
    start, stop = duration_bounds(target_sheet)
    old_block_columns = target_columns[start:stop]
    old_block_rows = [row[start:stop] for row in target_rows]
    output_data_rows = max(len(target_rows), len(source_rows)) - 2

    source_to_output: dict[str, str] = {}
    target_only_columns: set[str] = set()
    target_only_mapping: dict[str, str] = {}
    specs: list[tuple[str, Any, Any, list[Any]]] = []
    sheet_name = str(target_sheet.get("name", ""))

    if sheet_name in LOOP_COMPONENTS:
        expected = LOOP_COMPONENTS[sheet_name]
        target_groups = loop_groups(old_block_columns, expected)
        source_groups = loop_groups(source_columns, expected)
        for cycle in sorted(set(target_groups) | set(source_groups)):
            if cycle in source_groups:
                for source_index in source_groups[cycle]:
                    source_column = source_columns[source_index]
                    output_column = prefixed(source_column)
                    source_to_output[source_column] = output_column
                    specs.append(
                        (
                            output_column,
                            source_rows[0][source_index],
                            display_name(
                                f"{condition_label} | {source_rows[1][source_index]}"
                            ),
                            [row[source_index] for row in source_rows[2:]],
                        )
                    )
            else:
                for target_index in target_groups[cycle]:
                    old_column = old_block_columns[target_index]
                    old_base = old_column[len(DURATION_PREFIX) :]
                    cycle_token = old_base.rsplit("_", 1)[-1]
                    old_component = component(old_column)
                    template = next(
                        (
                            source_column.rsplit("_", 1)[0]
                            for source_column in source_columns
                            if component(source_column) == old_component
                        ),
                        None,
                    )
                    if template is None:
                        raise ValueError(
                            f"No source template for placeholder component {old_component!r}"
                        )
                    output_column = prefixed(f"{template}_{cycle_token}")
                    target_only_mapping[old_column] = output_column
                    target_only_columns.add(output_column)
                    old_name = str(old_block_rows[1][target_index]).strip()
                    old_name = old_name.removeprefix(f"{DURATION} | ")
                    specs.append(
                        (
                            output_column,
                            old_block_rows[0][target_index],
                            display_name(f"{condition_label} | {old_name}"),
                            [row[target_index] for row in old_block_rows[2:]],
                        )
                    )
    else:
        if len(source_columns) != len(old_block_columns):
            raise ValueError(
                f"{sheet_name!r} source width {len(source_columns)} does not match "
                f"the placeholder width {len(old_block_columns)}"
            )
        if source_rows[0] != old_block_rows[0]:
            raise ValueError(f"{sheet_name!r} source roles do not match the placeholder")
        for source_index, source_column in enumerate(source_columns):
            output_column = prefixed(source_column)
            source_to_output[source_column] = output_column
            specs.append(
                (
                    output_column,
                    source_rows[0][source_index],
                    display_name(source_rows[1][source_index]),
                    [row[source_index] for row in source_rows[2:]],
                )
            )

    new_block_columns = [spec[0] for spec in specs]
    if len(set(new_block_columns)) != len(new_block_columns):
        raise ValueError(f"Merged {sheet_name!r} block has duplicate columns")
    new_block_rows: list[list[Any]] = [
        [spec[1] for spec in specs],
        [spec[2] for spec in specs],
    ]
    for row_index in range(output_data_rows):
        new_block_rows.append(
            [spec[3][row_index] if row_index < len(spec[3]) else None for spec in specs]
        )

    old_width = len(target_columns)
    while len(target_rows) < output_data_rows + 2:
        target_rows.append([None] * old_width)
    merged_rows = [
        target_row[:start] + block_row + target_row[stop:]
        for target_row, block_row in zip(target_rows, new_block_rows)
    ]
    target_sheet["data"] = {
        "columns": target_columns[:start] + new_block_columns + target_columns[stop:],
        "rows": merged_rows,
    }
    validate_sheet(target_sheet)
    return {
        "source_to_output": source_to_output,
        "target_only_columns": target_only_columns,
        "target_only_mapping": target_only_mapping,
        "condition_label": condition_label,
        "old_block_columns": set(old_block_columns),
        "new_block_columns": set(new_block_columns),
        "old_block_width": len(old_block_columns),
        "new_block_width": len(new_block_columns),
        "target_data_rows_before": len(target_data["rows"]) - 2,
        "source_data_rows": len(source_rows) - 2,
        "output_data_rows": len(merged_rows) - 2,
    }


def graph_kind(name: str) -> str:
    if name == "Endurance":
        return "endurance"
    if "Ec vs cycle" in name:
        return "ec_metrics"
    if "Pr vs cycle" in name:
        return "pr_metrics"
    if name.startswith("PV_IV_"):
        return "pv_current"
    if name.startswith("PV_"):
        return "pv_polarization"
    return "pund_loop"


def voltage_condition_label(voltage_items: dict[str, dict[str, Any]]) -> str:
    endurance = voltage_items.get("Endurance", {}).get("sheet")
    if not endurance:
        raise ValueError("Source voltage folder has no Endurance sheet")
    name = str(endurance["data"]["rows"][1][0]).strip()
    suffix = " cycle"
    if not name.endswith(suffix):
        raise ValueError(f"Cannot derive source condition label from {name!r}")
    return name[: -len(suffix)]


def mapped_source_series(
    source_series: dict[str, Any],
    source_to_output: dict[str, str],
    label_context: str = "",
) -> dict[str, Any]:
    series = copy.deepcopy(source_series)
    for field in ("x", "y"):
        source_column = str(series.get(field, ""))
        if source_column not in source_to_output:
            raise ValueError(f"Source graph references unmapped column {source_column!r}")
        series[field] = source_to_output[source_column]
    error_column = str(series.get("error_column", "")).strip()
    if error_column:
        if error_column not in source_to_output:
            raise ValueError(f"Source graph error column {error_column!r} is unmapped")
        series["error_column"] = source_to_output[error_column]
    old_label = str(series.get("label", "")).strip()
    contextual_label = f"{label_context} | {old_label}" if label_context else old_label
    series["label"] = (
        f"{DURATION} | {contextual_label}" if contextual_label else DURATION
    )
    return series


def mapped_target_only_series(
    target_series: dict[str, Any],
    target_only_mapping: dict[str, str],
    condition_label: str,
) -> dict[str, Any]:
    series = copy.deepcopy(target_series)
    for field in ("x", "y"):
        old_column = str(series.get(field, ""))
        if old_column not in target_only_mapping:
            raise ValueError(f"Blank placeholder series column {old_column!r} is unmapped")
        series[field] = target_only_mapping[old_column]
    error_column = str(series.get("error_column", "")).strip()
    if error_column:
        if error_column not in target_only_mapping:
            raise ValueError(f"Blank placeholder error column {error_column!r} is unmapped")
        series["error_column"] = target_only_mapping[error_column]
    old_label = str(series.get("label", "")).strip().removeprefix(f"{DURATION} | ")
    series["label"] = display_name(f"{condition_label} | {old_label}")
    return series


def merge_graph(
    target_graph: dict[str, Any],
    source_graph: dict[str, Any],
    merge_info: dict[str, Any],
    output_sheet: dict[str, Any],
) -> None:
    kind = graph_kind(str(target_graph.get("name", "")))
    if kind != graph_kind(str(source_graph.get("name", ""))):
        raise ValueError("Source and target graph order does not match")
    old_series = target_graph.get("series_config", [])
    old_2ms_indices = [
        index
        for index, series in enumerate(old_series)
        if str(series.get("y", "")).startswith(DURATION_PREFIX)
    ]
    if not old_2ms_indices:
        raise ValueError(f"Graph {target_graph.get('name')!r} has no 2ms series")
    first, last = old_2ms_indices[0], old_2ms_indices[-1]
    if old_2ms_indices != list(range(first, last + 1)):
        raise ValueError(f"Graph {target_graph.get('name')!r} has split 2ms series")

    measured_series = [
        mapped_source_series(
            series,
            merge_info["source_to_output"],
            merge_info["condition_label"]
            if kind in {"pund_loop", "pv_current", "pv_polarization"}
            else "",
        )
        for series in source_graph.get("series_config", [])
    ]
    target_only_series = [
        mapped_target_only_series(
            series,
            merge_info["target_only_mapping"],
            merge_info["condition_label"],
        )
        for series in old_series[first : last + 1]
        if str(series.get("y", "")) in merge_info["target_only_mapping"]
    ]
    output_positions = {
        str(column): index for index, column in enumerate(output_sheet["data"]["columns"])
    }
    replacement = measured_series + target_only_series
    replacement.sort(key=lambda series: output_positions[str(series["y"])])
    target_graph["series_config"] = old_series[:first] + replacement + old_series[last + 1 :]


def named_sheet(project: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [sheet for sheet in project.get("sheets", []) if sheet.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {name!r} sheet")
    return matches[0]


def merge_manifest(project: dict[str, Any], source: dict[str, Any]) -> int:
    target_sheet = named_sheet(project, "File manifest")
    source_sheet = named_sheet(source, "File manifest")
    validate_sheet(target_sheet)
    validate_sheet(source_sheet)
    target_data = target_sheet["data"]
    source_data = source_sheet["data"]
    if target_data["columns"] != source_data["columns"]:
        raise ValueError("Source and target file manifests have different schemas")
    path_index = target_data["columns"].index("source_relative_path")
    existing = {row[path_index] for row in target_data["rows"][2:]}
    additions = [
        copy.deepcopy(row)
        for row in source_data["rows"][2:]
        if row[path_index] not in existing
    ]
    target_data["rows"].extend(additions)
    return len(additions)


def metadata_rows(project: dict[str, Any]) -> list[list[Any]]:
    sheet = named_sheet(project, "Metadata")
    if sheet.get("data", {}).get("columns") != ["key", "value"]:
        raise ValueError("Metadata sheet has an unexpected schema")
    return sheet["data"]["rows"]


def set_metadata(rows: list[list[Any]], key: str, value: Any) -> None:
    for row in rows[2:]:
        if len(row) >= 2 and row[0] == key:
            row[1] = value
            return
    rows.append([key, value])


def validate_result(
    result: dict[str, Any],
    target_before: dict[str, Any],
    source: dict[str, Any],
    merge_infos: dict[tuple[str, str], dict[str, Any]],
    manifest_added: int,
) -> None:
    result_index = project_index(result)
    before_index = project_index(target_before)
    source_index = project_index(source)

    if result.get("tree") != target_before.get("tree"):
        raise ValueError("Project tree changed during the merge")
    if result.get("active_node_id") != target_before.get("active_node_id"):
        raise ValueError("active_node_id changed during the merge")

    for voltage, sheets in before_index.items():
        for sheet_name, before_entry in sheets.items():
            after_sheet = result_index[voltage][sheet_name]["sheet"]
            before_sheet = before_entry["sheet"]
            validate_sheet(after_sheet)
            if (voltage, sheet_name) not in merge_infos:
                if after_sheet != before_sheet:
                    raise ValueError(f"Unrelated sheet {voltage}/{sheet_name} changed")
                continue

            info = merge_infos[(voltage, sheet_name)]
            source_sheet = source_index[voltage][sheet_name]["sheet"]
            after_columns = list(map(str, after_sheet["data"]["columns"]))
            after_rows = after_sheet["data"]["rows"]
            before_columns = list(map(str, before_sheet["data"]["columns"]))
            before_rows = before_sheet["data"]["rows"]

            for column_index, source_column in enumerate(source_sheet["data"]["columns"]):
                output_column = info["source_to_output"][str(source_column)]
                output_index = after_columns.index(output_column)
                actual = [row[output_index] for row in after_rows[2 : 2 + len(source_sheet["data"]["rows"]) - 2]]
                expected = [row[column_index] for row in source_sheet["data"]["rows"][2:]]
                if actual != expected:
                    raise ValueError(
                        f"Source values changed in {voltage}/{sheet_name}/{source_column}"
                    )

            for before_index_value, column in enumerate(before_columns):
                if column.startswith(DURATION_PREFIX):
                    continue
                after_index_value = after_columns.index(column)
                actual = [row[after_index_value] for row in after_rows[: len(before_rows)]]
                expected = [row[before_index_value] for row in before_rows]
                if actual != expected:
                    raise ValueError(
                        f"Non-2ms column {voltage}/{sheet_name}/{column} changed"
                    )
                if any(
                    row[after_index_value] is not None for row in after_rows[len(before_rows) :]
                ):
                    raise ValueError("A non-2ms column has data in an appended row")

    before_graphs = {str(graph["id"]): graph for graph in target_before["graphs"]}
    result_graphs = {str(graph["id"]): graph for graph in result["graphs"]}
    if before_graphs.keys() != result_graphs.keys():
        raise ValueError("Graph IDs changed during the merge")
    for graph_id, before_graph in before_graphs.items():
        after_graph = result_graphs[graph_id]
        if after_graph.get("plot_config") != before_graph.get("plot_config"):
            raise ValueError("A graph plot_config changed")
        if after_graph.get("checked_y") != before_graph.get("checked_y"):
            raise ValueError("A graph checked_y selection changed")
        before_non_2ms = [
            series
            for series in before_graph.get("series_config", [])
            if not str(series.get("y", "")).startswith(DURATION_PREFIX)
        ]
        after_non_2ms = [
            series
            for series in after_graph.get("series_config", [])
            if not str(series.get("y", "")).startswith(DURATION_PREFIX)
        ]
        if after_non_2ms != before_non_2ms:
            raise ValueError("A non-2ms graph series changed")

    columns_by_sheet = {
        str(sheet["id"]): set(map(str, sheet["data"]["columns"]))
        for sheet in result["sheets"]
    }
    for graph in result.get("graphs", []):
        sheet_id = str(graph.get("sheet_id", ""))
        if sheet_id not in columns_by_sheet:
            raise ValueError("A graph refers to a missing sheet")
        columns = columns_by_sheet[sheet_id]
        configured_y: set[str] = set()
        for series in graph.get("series_config", []):
            if str(series.get("x", "")) not in columns or str(series.get("y", "")) not in columns:
                raise ValueError("A graph series refers to a missing column")
            configured_y.add(str(series["y"]))
        if not set(map(str, graph.get("checked_y", []))).issubset(configured_y):
            raise ValueError("checked_y refers to an unconfigured column")
        if graph.get("plot_config", {}).get("y_label_offset_mm") != 6.0:
            raise ValueError("Y-label gap is no longer 6 mm")

    target_manifest = named_sheet(target_before, "File manifest")["data"]["rows"]
    output_manifest = named_sheet(result, "File manifest")["data"]["rows"]
    if output_manifest[: len(target_manifest)] != target_manifest:
        raise ValueError("Existing file-manifest rows changed")
    if len(output_manifest) != len(target_manifest) + manifest_added:
        raise ValueError("File-manifest append count is wrong")


def transform(
    target: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if target.get("schema_version") != 3 or source.get("schema_version") != 3:
        raise ValueError("Both projects must use Pubfig schema_version 3")
    result = copy.deepcopy(target)
    result_index = project_index(result)
    source_index = project_index(source)
    source_voltages = sorted(source_index, key=lambda item: float(item[:-1]))
    if source_voltages != ["2.5V", "3V"]:
        raise ValueError(f"Unexpected measured 2ms voltages: {source_voltages}")

    merge_infos: dict[tuple[str, str], dict[str, Any]] = {}
    report_sheets: list[dict[str, Any]] = []
    for voltage in source_voltages:
        if voltage not in result_index:
            raise ValueError(f"Target lacks voltage folder {voltage}")
        if set(source_index[voltage]) != set(result_index[voltage]):
            raise ValueError(f"Source and target sheet sets differ for {voltage}")
        condition_label = voltage_condition_label(source_index[voltage])
        for sheet_name, source_entry in source_index[voltage].items():
            target_entry = result_index[voltage][sheet_name]
            info = merge_sheet(
                target_entry["sheet"], source_entry["sheet"], condition_label
            )
            merge_infos[(voltage, sheet_name)] = info
            if len(target_entry["graphs"]) != len(source_entry["graphs"]):
                raise ValueError(f"Graph count mismatch for {voltage}/{sheet_name}")
            for target_graph, source_graph in zip(
                target_entry["graphs"], source_entry["graphs"]
            ):
                merge_graph(target_graph, source_graph, info, target_entry["sheet"])
            report_sheets.append(
                {
                    "voltage": voltage,
                    "sheet": sheet_name,
                    "2ms_columns_before": info["old_block_width"],
                    "2ms_columns_after": info["new_block_width"],
                    "target_data_rows_before": info["target_data_rows_before"],
                    "source_data_rows": info["source_data_rows"],
                    "output_data_rows": info["output_data_rows"],
                    "preserved_blank_columns": len(info["target_only_columns"]),
                }
            )

    manifest_added = merge_manifest(result, source)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    metadata = metadata_rows(result)
    set_metadata(
        metadata,
        "organized_placeholder_note",
        "Measured values are present for 5ms at 2V, 2.5V, 3V, and 3.5V, and for 2ms at 2.5V and 3V. Other duration/voltage blocks remain empty placeholders.",
    )
    set_metadata(metadata, "organized_2ms_source_project", str(source_path))
    set_metadata(metadata, "organized_2ms_source_sha256", source_sha256)
    set_metadata(metadata, "organized_2ms_conditions", "2.5V_col20_0901; 3V_col20_0901")
    set_metadata(metadata, "organized_measured_durations", "2ms; 5ms")
    set_metadata(
        metadata,
        "organized_2ms_merge_note",
        "All measured 2ms values were imported. The unmeasured 2.5V 10^7 loop slots remain blank; the measured 3V 10^5 loop columns and required endurance/metric rows were added.",
    )
    set_metadata(metadata, "organized_2ms_imported_utc", datetime.now(timezone.utc).isoformat())
    set_metadata(
        metadata,
        "organized_manifest_file_count",
        len(named_sheet(result, "File manifest")["data"]["rows"]) - 2,
    )
    set_metadata(
        metadata,
        "organized_warning_file_count",
        sum(
            bool(row[9])
            for row in named_sheet(result, "File manifest")["data"]["rows"][2:]
        ),
    )

    validate_result(result, target, source, merge_infos, manifest_added)
    report = {
        "source": str(source_path),
        "source_sha256": source_sha256,
        "imported_voltages": source_voltages,
        "manifest_rows_added": manifest_added,
        "sheets": report_sheets,
        "sheet_count": len(result["sheets"]),
        "graph_count": len(result["graphs"]),
    }
    return result, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.target.open(encoding="utf-8") as handle:
        target = json.load(handle)
    with args.source.open(encoding="utf-8") as handle:
        source = json.load(handle)
    result, report = transform(target, source, args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    report["target"] = str(args.target)
    report["output"] = str(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
