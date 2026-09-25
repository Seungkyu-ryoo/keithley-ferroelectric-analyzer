#!/usr/bin/env python3
"""Add 5 nm duration-comparison loop sheets to a Pubfig v3 project.

The existing organized project supplies the measured 2 ms and 5 ms blocks.
The 0.4, 0.8, 1.2, and 1.6 ms blocks are read from the 2026-09-03 projects.
For 2.5 V the comparison cycles are the pristine/first and 10^6 cycles; for
3 V they are the pristine/first and 10^4 cycles.

Each voltage receives four sheets.  A PV sheet contains V/P/J columns and two
graphs (P-V and J-V), while a PUND sheet contains V/P columns and one graph.
All pre-existing sheets, graphs, values, IDs, selections, and tree entries are
preserved; the comparison objects are appended.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable


DURATIONS = ("0.4ms", "0.8ms", "1.2ms", "1.6ms", "2ms", "5ms")
TODAY_SOURCE_FILES = {
    "0.4ms": "HfO2_5nm_0.4ms.json",
    "0.8ms": "HfO2_5nm_0.8ms.json",
    "1.2ms": "HfO2_5nm_1.2ms.json",
    "1.6ms": "HfO2_5nm_1.6ms.json",
}
EXISTING_DURATION_PREFIXES = {
    "2ms": "d2ms__",
    "5ms": "d5ms__",
}
DURATION_COLORS = {
    "0.4ms": "#471365",
    "0.8ms": "#3e4c8a",
    "1.2ms": "#297a8e",
    "1.6ms": "#21a685",
    "2ms": "#69cd5b",
    "5ms": "#dfe318",
}


@dataclass(frozen=True)
class CycleSpec:
    slug: str
    suffix: str
    display: str
    sheet_display: str


CYCLES_BY_VOLTAGE = {
    "2.5V": (
        CycleSpec("pristine", "1", "Pristine (1st cycle)", "Pristine (1st cycle)"),
        CycleSpec("1e6", "1e+06", "10^6 cycle", "10^6 cycle"),
    ),
    "3V": (
        CycleSpec("pristine", "1", "Pristine (1st cycle)", "Pristine (1st cycle)"),
        CycleSpec("1e4", "10000", "10^4 cycle", "10^4 cycle"),
    ),
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        project = json.load(handle)
    if project.get("schema_version") != 3:
        raise ValueError(f"Expected Pubfig schema_version 3: {path}")
    return project


def safe_token(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def tree_voltage_folder(project: dict[str, Any], voltage: str) -> dict[str, Any]:
    matches = [
        child
        for child in project.get("tree", {}).get("children", [])
        if child.get("type") == "folder" and child.get("name") == voltage
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one {voltage} tree folder, found {len(matches)}")
    return matches[0]


def voltage_sheet(
    project: dict[str, Any], voltage: str, sheet_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    folder = tree_voltage_folder(project, voltage)
    nodes = [
        child
        for child in folder.get("children", [])
        if child.get("type") == "sheet" and child.get("name") == sheet_name
    ]
    if len(nodes) != 1:
        raise ValueError(
            f"Expected one {voltage}/{sheet_name} tree sheet, found {len(nodes)}"
        )
    sheets = {str(sheet["id"]): sheet for sheet in project.get("sheets", [])}
    sheet_id = str(nodes[0].get("ref_id", ""))
    if sheet_id not in sheets:
        raise ValueError(f"Tree refers to missing sheet {sheet_id!r}")
    return sheets[sheet_id], nodes[0]


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
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(
                f"Sheet {sheet.get('name')!r} row {row_index} has wrong width"
            )


def matching_column(
    sheet: dict[str, Any], component: str, cycle_suffix: str, prefix: str = ""
) -> str:
    validate_sheet(sheet)
    start = f"{prefix}{component}_"
    matches = [
        str(column)
        for column in sheet["data"]["columns"]
        if str(column).startswith(start)
        and str(column).rsplit("_", 1)[-1] == cycle_suffix
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one {sheet.get('name')!r} {start}* cycle "
            f"{cycle_suffix}, found {matches}"
        )
    return matches[0]


def extract_components(
    sheet: dict[str, Any],
    components: tuple[str, ...],
    cycle_suffix: str,
    prefix: str = "",
) -> dict[str, list[Any]]:
    columns = list(map(str, sheet["data"]["columns"]))
    selected = {
        component: matching_column(sheet, component, cycle_suffix, prefix)
        for component in components
    }
    indices = {component: columns.index(column) for component, column in selected.items()}
    rows = [
        {component: row[index] for component, index in indices.items()}
        for row in sheet["data"]["rows"][2:]
    ]
    while rows and all(value is None for value in rows[-1].values()):
        rows.pop()
    if not rows:
        raise ValueError(
            f"No measured values for {sheet.get('name')!r}, cycle {cycle_suffix}"
        )
    for row_index, row in enumerate(rows):
        missing = [component for component, value in row.items() if value is None]
        if missing:
            raise ValueError(
                f"Partial row {row_index} in {sheet.get('name')!r}: missing {missing}"
            )
        for component, value in row.items():
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(
                    f"Non-finite {component} value at row {row_index} in "
                    f"{sheet.get('name')!r}"
                )
    return {
        component: [row[component] for row in rows]
        for component in components
    }


def collect_measurements(
    organized: dict[str, Any], today_dir: Path
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], list[str]]:
    today_projects = {
        duration: load_json(today_dir / filename)
        for duration, filename in TODAY_SOURCE_FILES.items()
    }
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    provenance: list[str] = []
    for voltage, cycle_specs in CYCLES_BY_VOLTAGE.items():
        for cycle in cycle_specs:
            for duration in DURATIONS:
                if duration in today_projects:
                    source_project = today_projects[duration]
                    prefix = ""
                    source_label = str(today_dir / TODAY_SOURCE_FILES[duration])
                else:
                    source_project = organized
                    prefix = EXISTING_DURATION_PREFIXES[duration]
                    source_label = f"existing organized {duration} block"

                pund_sheet, _ = voltage_sheet(source_project, voltage, "PUND loops")
                pv_sheet, _ = voltage_sheet(source_project, voltage, "PV loops")
                pund = extract_components(
                    pund_sheet,
                    ("Vloop", "Ploop"),
                    cycle.suffix,
                    prefix,
                )
                pv = extract_components(
                    pv_sheet,
                    ("V", "P", "J"),
                    cycle.suffix,
                    prefix,
                )
                if len(pund["Vloop"]) != 4000:
                    raise ValueError(
                        f"{voltage} {duration} {cycle.display} PUND has "
                        f"{len(pund['Vloop'])} points, expected 4000"
                    )
                if len(pv["V"]) != 830:
                    raise ValueError(
                        f"{voltage} {duration} {cycle.display} PV has "
                        f"{len(pv['V'])} points, expected 830"
                    )
                result[(voltage, cycle.slug, duration)] = {
                    "pund": pund,
                    "pv": pv,
                    "source": source_label,
                }
                provenance.append(
                    f"{voltage} | {cycle.display} | {duration} <- {source_label}"
                )
    return result, provenance


class IdFactory:
    def __init__(self, project: dict[str, Any]):
        self.used = {
            str(item.get("id"))
            for collection in (project.get("sheets", []), project.get("graphs", []))
            for item in collection
        }

        def walk(node: dict[str, Any]) -> None:
            self.used.add(str(node.get("id")))
            for child in node.get("children", []):
                walk(child)

        walk(project.get("tree", {}))

    def make(self, prefix: str, label: str) -> str:
        digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]
        candidate = f"{prefix}{digest}"
        if candidate in self.used:
            raise ValueError(f"Generated ID collision for {label!r}: {candidate}")
        self.used.add(candidate)
        return candidate


def graph_prototypes(
    project: dict[str, Any], voltage: str
) -> dict[str, dict[str, Any]]:
    graphs = {str(graph["id"]): graph for graph in project.get("graphs", [])}
    pund_sheet, pund_node = voltage_sheet(project, voltage, "PUND loops")
    pv_sheet, pv_node = voltage_sheet(project, voltage, "PV loops")

    def child_graphs(node: dict[str, Any]) -> list[dict[str, Any]]:
        found = []
        for child in node.get("children", []):
            if child.get("type") != "graph":
                continue
            graph_id = str(child.get("ref_id", ""))
            if graph_id not in graphs:
                raise ValueError(f"Tree refers to missing graph {graph_id!r}")
            found.append(graphs[graph_id])
        return found

    pund_candidates = [
        graph
        for graph in child_graphs(pund_node)
        if any(str(column).startswith("d5ms__Ploop_") for column in graph.get("checked_y", []))
    ]
    pv_candidates = [
        graph
        for graph in child_graphs(pv_node)
        if str(graph.get("name", "")).startswith("PV_")
        and not str(graph.get("name", "")).startswith("PV_IV_")
        and any(str(column).startswith("d5ms__P_") for column in graph.get("checked_y", []))
    ]
    iv_candidates = [
        graph
        for graph in child_graphs(pv_node)
        if str(graph.get("name", "")).startswith("PV_IV_")
        and any(str(column).startswith("d5ms__J_") for column in graph.get("checked_y", []))
    ]
    for name, candidates in (
        ("PUND", pund_candidates),
        ("PV", pv_candidates),
        ("PV-IV", iv_candidates),
    ):
        if len(candidates) != 1:
            raise ValueError(
                f"Expected one {voltage} {name} 5ms prototype, found {len(candidates)}"
            )
    return {
        "pund": pund_candidates[0],
        "pv": pv_candidates[0],
        "iv": iv_candidates[0],
    }


def selected_style(graph: dict[str, Any]) -> dict[str, Any]:
    selected = set(map(str, graph.get("checked_y", [])))
    matches = [
        series
        for series in graph.get("series_config", [])
        if str(series.get("y", "")) in selected
    ]
    if not matches:
        raise ValueError(f"Graph {graph.get('name')!r} has no selected series style")
    return copy.deepcopy(matches[0])


def series_from_style(
    style: dict[str, Any], x_column: str, y_column: str, duration: str
) -> dict[str, Any]:
    series = copy.deepcopy(style)
    series["x"] = x_column
    series["y"] = y_column
    series["label"] = duration
    series["color"] = DURATION_COLORS[duration]
    series["error_column"] = ""
    return series


def wide_rows(
    roles: list[str], labels: list[str], values: list[list[Any]]
) -> list[list[Any]]:
    if not (len(roles) == len(labels) == len(values)):
        raise ValueError("Wide-sheet metadata lengths do not match")
    maximum = max(map(len, values), default=0)
    rows = [roles, labels]
    rows.extend(
        [items[row_index] if row_index < len(items) else None for items in values]
        for row_index in range(maximum)
    )
    return rows


def clone_graph(
    prototype: dict[str, Any],
    graph_id: str,
    name: str,
    sheet_id: str,
    series: list[dict[str, Any]],
) -> dict[str, Any]:
    graph = copy.deepcopy(prototype)
    graph["id"] = graph_id
    graph["name"] = name
    graph["sheet_id"] = sheet_id
    graph["series_config"] = series
    graph["checked_y"] = [item["y"] for item in series]
    return graph


def sheet_node(
    factory: IdFactory,
    name: str,
    sheet_id: str,
    graph_items: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    children = [
        {
            "id": factory.make("nd", f"tree graph {graph['id']}"),
            "type": "graph",
            "name": graph["name"],
            "ref_id": graph["id"],
            "expanded": True,
            "children": [],
        }
        for graph in graph_items
    ]
    return {
        "id": factory.make("nd", f"tree sheet {sheet_id}"),
        "type": "sheet",
        "name": name,
        "ref_id": sheet_id,
        "expanded": True,
        "children": children,
    }


def append_comparison_objects(
    project: dict[str, Any],
    collected: dict[tuple[str, str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    factory = IdFactory(project)
    additions: list[dict[str, Any]] = []
    for voltage, cycle_specs in CYCLES_BY_VOLTAGE.items():
        folder = tree_voltage_folder(project, voltage)
        sheets_before = len(project["sheets"])
        graphs_before = len(project["graphs"])
        tree_items_before = len(folder.get("children", []))
        existing_names = {
            str(child.get("name")) for child in folder.get("children", [])
        }
        prototypes = graph_prototypes(project, voltage)
        pund_style = selected_style(prototypes["pund"])
        pv_style = selected_style(prototypes["pv"])
        iv_style = selected_style(prototypes["iv"])

        for cycle in cycle_specs:
            context = f"{voltage} {cycle.slug} duration comparison"

            pv_sheet_name = f"PV - {cycle.sheet_display} - {voltage}"
            pund_sheet_name = f"PUND - {cycle.sheet_display} - {voltage}"
            for name in (pv_sheet_name, pund_sheet_name):
                if name in existing_names:
                    raise ValueError(f"Comparison sheet already exists: {voltage}/{name}")

            pv_sheet_id = factory.make("sh", f"sheet PV {context}")
            pv_columns: list[str] = []
            pv_roles: list[str] = []
            pv_labels: list[str] = []
            pv_values: list[list[Any]] = []
            pv_series: list[dict[str, Any]] = []
            iv_series: list[dict[str, Any]] = []
            for duration in DURATIONS:
                token = f"{safe_token(voltage)}_{cycle.slug}_{safe_token(duration)}"
                v_column = f"V_cmp_{token}"
                p_column = f"P_cmp_{token}"
                j_column = f"J_cmp_{token}"
                values = collected[(voltage, cycle.slug, duration)]["pv"]
                pv_columns.extend([v_column, p_column, j_column])
                pv_roles.extend(["X", "Y", "Y"])
                pv_labels.extend(["", duration, duration])
                pv_values.extend([values["V"], values["P"], values["J"]])
                pv_series.append(
                    series_from_style(pv_style, v_column, p_column, duration)
                )
                iv_series.append(
                    series_from_style(iv_style, v_column, j_column, duration)
                )
            pv_sheet = {
                "id": pv_sheet_id,
                "name": pv_sheet_name,
                "data": {
                    "columns": pv_columns,
                    "rows": wide_rows(pv_roles, pv_labels, pv_values),
                },
            }
            pv_graph = clone_graph(
                prototypes["pv"],
                factory.make("gr", f"graph PV {context}"),
                f"PV_{safe_token(voltage)}_{cycle.slug}_by_duration",
                pv_sheet_id,
                pv_series,
            )
            iv_graph = clone_graph(
                prototypes["iv"],
                factory.make("gr", f"graph IV {context}"),
                f"PV_IV_{safe_token(voltage)}_{cycle.slug}_by_duration",
                pv_sheet_id,
                iv_series,
            )
            project["sheets"].append(pv_sheet)
            project["graphs"].extend([pv_graph, iv_graph])
            folder.setdefault("children", []).append(
                sheet_node(
                    factory,
                    pv_sheet_name,
                    pv_sheet_id,
                    [pv_graph, iv_graph],
                )
            )

            pund_sheet_id = factory.make("sh", f"sheet PUND {context}")
            pund_columns: list[str] = []
            pund_roles: list[str] = []
            pund_labels: list[str] = []
            pund_values: list[list[Any]] = []
            pund_series: list[dict[str, Any]] = []
            for duration in DURATIONS:
                token = f"{safe_token(voltage)}_{cycle.slug}_{safe_token(duration)}"
                v_column = f"Vloop_cmp_{token}"
                p_column = f"Ploop_cmp_{token}"
                values = collected[(voltage, cycle.slug, duration)]["pund"]
                pund_columns.extend([v_column, p_column])
                pund_roles.extend(["X", "Y"])
                pund_labels.extend(["", duration])
                pund_values.extend([values["Vloop"], values["Ploop"]])
                pund_series.append(
                    series_from_style(pund_style, v_column, p_column, duration)
                )
            pund_sheet = {
                "id": pund_sheet_id,
                "name": pund_sheet_name,
                "data": {
                    "columns": pund_columns,
                    "rows": wide_rows(pund_roles, pund_labels, pund_values),
                },
            }
            pund_graph = clone_graph(
                prototypes["pund"],
                factory.make("gr", f"graph PUND {context}"),
                f"PUND_{safe_token(voltage)}_{cycle.slug}_by_duration",
                pund_sheet_id,
                pund_series,
            )
            project["sheets"].append(pund_sheet)
            project["graphs"].append(pund_graph)
            folder.setdefault("children", []).append(
                sheet_node(factory, pund_sheet_name, pund_sheet_id, [pund_graph])
            )
            additions.append(
                {
                    "voltage": voltage,
                    "cycle": cycle.display,
                    "pv_sheet": pv_sheet_name,
                    "pund_sheet": pund_sheet_name,
                }
            )

        # Match the established 10 nm organized ordering:
        # PV pristine, PV target, PUND pristine, PUND target.
        new_sheets = project["sheets"][sheets_before:]
        new_graphs = project["graphs"][graphs_before:]
        new_tree_items = folder["children"][tree_items_before:]
        if len(new_sheets) != 4 or len(new_graphs) != 6 or len(new_tree_items) != 4:
            raise ValueError(f"Unexpected comparison object count for {voltage}")
        project["sheets"][sheets_before:] = [
            new_sheets[0],
            new_sheets[2],
            new_sheets[1],
            new_sheets[3],
        ]
        project["graphs"][graphs_before:] = [
            new_graphs[0],
            new_graphs[1],
            new_graphs[3],
            new_graphs[4],
            new_graphs[2],
            new_graphs[5],
        ]
        folder["children"][tree_items_before:] = [
            new_tree_items[0],
            new_tree_items[2],
            new_tree_items[1],
            new_tree_items[3],
        ]
    return additions


def append_metadata(
    project: dict[str, Any], today_dir: Path, provenance: list[str]
) -> None:
    matches = [
        sheet for sheet in project.get("sheets", []) if sheet.get("name") == "Metadata"
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one Metadata sheet, found {len(matches)}")
    metadata = matches[0]
    if metadata.get("data", {}).get("columns") != ["key", "value"]:
        raise ValueError("Metadata sheet does not use key/value columns")
    rows = metadata["data"]["rows"]
    existing_keys = {
        str(row[0])
        for row in rows
        if isinstance(row, list) and len(row) == 2 and row[0] not in (None, "")
    }
    additions = (
        ("duration_comparison_durations", "; ".join(DURATIONS)),
        ("duration_comparison_2.5V_cycles", "1; 1e6"),
        ("duration_comparison_3V_cycles", "1; 1e4"),
        ("duration_comparison_0903_source_dir", str(today_dir)),
        (
            "duration_comparison_source_note",
            "0.4-1.6ms values come from 0903 JSON projects; existing organized 2ms and 5ms values are reused unchanged.",
        ),
        ("duration_comparison_provenance", "; ".join(provenance)),
        ("duration_comparison_generated_utc", datetime.now(timezone.utc).isoformat()),
    )
    for key, value in additions:
        if key in existing_keys:
            raise ValueError(f"Metadata key already exists: {key}")
        rows.append([key, value])


def walk_tree(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in node.get("children", []):
        yield from walk_tree(child)


def validate_project(project: dict[str, Any]) -> None:
    if project.get("schema_version") != 3:
        raise ValueError("Expected Pubfig schema_version 3")
    sheets = {str(sheet["id"]): sheet for sheet in project.get("sheets", [])}
    graphs = {str(graph["id"]): graph for graph in project.get("graphs", [])}
    if len(sheets) != len(project.get("sheets", [])):
        raise ValueError("Duplicate sheet IDs")
    if len(graphs) != len(project.get("graphs", [])):
        raise ValueError("Duplicate graph IDs")
    for sheet in sheets.values():
        validate_sheet(sheet)
    for graph in graphs.values():
        sheet_id = str(graph.get("sheet_id", ""))
        if sheet_id not in sheets:
            raise ValueError(f"Graph {graph.get('name')!r} refers to missing sheet")
        columns = set(map(str, sheets[sheet_id]["data"]["columns"]))
        configured_y: set[str] = set()
        for series in graph.get("series_config", []):
            x_column = str(series.get("x", ""))
            y_column = str(series.get("y", ""))
            if x_column not in columns or y_column not in columns:
                raise ValueError(
                    f"Graph {graph.get('name')!r} refers to missing columns "
                    f"{x_column!r}/{y_column!r}"
                )
            configured_y.add(y_column)
        if not set(map(str, graph.get("checked_y", []))).issubset(configured_y):
            raise ValueError(f"Graph {graph.get('name')!r} has invalid checked_y")

    nodes = list(walk_tree(project.get("tree", {})))
    node_ids = [str(node.get("id", "")) for node in nodes]
    if not all(node_ids) or len(node_ids) != len(set(node_ids)):
        raise ValueError("Missing or duplicate tree node IDs")
    sheet_refs = [
        str(node.get("ref_id", "")) for node in nodes if node.get("type") == "sheet"
    ]
    graph_refs = [
        str(node.get("ref_id", "")) for node in nodes if node.get("type") == "graph"
    ]
    if sorted(sheet_refs) != sorted(sheets):
        raise ValueError("Tree sheet references are incomplete or duplicated")
    if sorted(graph_refs) != sorted(graphs):
        raise ValueError("Tree graph references are incomplete or duplicated")
    if str(project.get("active_node_id", "")) not in set(node_ids):
        raise ValueError("active_node_id is not a valid tree node")


def validate_preservation(
    before: dict[str, Any], after: dict[str, Any]
) -> None:
    before_sheets = {str(sheet["id"]): sheet for sheet in before["sheets"]}
    after_sheets = {str(sheet["id"]): sheet for sheet in after["sheets"]}
    for sheet_id, old_sheet in before_sheets.items():
        new_sheet = after_sheets[sheet_id]
        if old_sheet.get("name") == "Metadata":
            old_rows = old_sheet["data"]["rows"]
            if new_sheet["data"]["columns"] != old_sheet["data"]["columns"]:
                raise ValueError("Metadata columns changed")
            if new_sheet["data"]["rows"][: len(old_rows)] != old_rows:
                raise ValueError("Existing Metadata rows changed")
        elif new_sheet != old_sheet:
            raise ValueError(f"Existing sheet changed: {old_sheet.get('name')!r}")

    before_graphs = {str(graph["id"]): graph for graph in before["graphs"]}
    after_graphs = {str(graph["id"]): graph for graph in after["graphs"]}
    for graph_id, old_graph in before_graphs.items():
        if after_graphs[graph_id] != old_graph:
            raise ValueError(f"Existing graph changed: {old_graph.get('name')!r}")

    before_root = before["tree"]
    after_root = after["tree"]
    if before_root.get("id") != after_root.get("id"):
        raise ValueError("Root tree ID changed")
    if before_root.get("name") != after_root.get("name"):
        raise ValueError("Root tree name changed")
    before_children = before_root.get("children", [])
    after_children = after_root.get("children", [])
    if len(before_children) != len(after_children):
        raise ValueError("Root tree children changed")
    for old_child, new_child in zip(before_children, after_children, strict=True):
        if old_child.get("type") == "folder" and old_child.get("name") in CYCLES_BY_VOLTAGE:
            old_items = old_child.get("children", [])
            if new_child.get("children", [])[: len(old_items)] != old_items:
                raise ValueError(f"Existing {old_child.get('name')} tree entries changed")
            for key in ("id", "type", "name", "ref_id", "expanded"):
                if new_child.get(key) != old_child.get(key):
                    raise ValueError(f"Existing voltage folder field changed: {key}")
        elif new_child != old_child:
            raise ValueError(f"Existing tree entry changed: {old_child.get('name')!r}")


def transform(
    source: dict[str, Any], today_dir: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validate_project(source)
    before = copy.deepcopy(source)
    result = copy.deepcopy(source)
    collected, provenance = collect_measurements(result, today_dir)
    additions = append_comparison_objects(result, collected)
    append_metadata(result, today_dir, provenance)
    validate_project(result)
    validate_preservation(before, result)

    if len(result["sheets"]) != len(before["sheets"]) + 8:
        raise ValueError("Expected exactly eight new comparison sheets")
    if len(result["graphs"]) != len(before["graphs"]) + 12:
        raise ValueError("Expected exactly twelve new comparison graphs")
    for item in additions:
        voltage = item["voltage"]
        for sheet_name, expected_width, expected_rows in (
            (item["pv_sheet"], 18, 832),
            (item["pund_sheet"], 12, 4002),
        ):
            sheet, node = voltage_sheet(result, voltage, sheet_name)
            if len(sheet["data"]["columns"]) != expected_width:
                raise ValueError(f"{sheet_name} has wrong width")
            if len(sheet["data"]["rows"]) != expected_rows:
                raise ValueError(f"{sheet_name} has wrong row count")
            graph_count = len(
                [child for child in node.get("children", []) if child.get("type") == "graph"]
            )
            expected_graphs = 2 if sheet_name.startswith("PV -") else 1
            if graph_count != expected_graphs:
                raise ValueError(f"{sheet_name} has wrong graph count")
    return result, additions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing 5nm_organized.json")
    parser.add_argument("today_dir", type=Path, help="Directory with 0903 5 nm JSONs")
    parser.add_argument("output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output.exists() and not args.overwrite:
        raise FileExistsError(
            f"Refusing to overwrite {args.output}; pass --overwrite if intended"
        )
    source = load_json(args.source)
    result, additions = transform(source, args.today_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(
        json.dumps(
            {
                "source": str(args.source),
                "output": str(args.output),
                "durations": list(DURATIONS),
                "additions": additions,
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
