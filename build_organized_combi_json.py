#!/usr/bin/env python3
"""Combine per-column Pubfig projects into one three-sheet Organized project.

The source projects are not modified.  Their Endurance values and selected P-V
and J-V cycles are copied verbatim into a new Pubfig schema-v3 JSON project.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

COLUMN_ORDER = ("col5", "col10", "col15", "col20", "col25", "col30", "col35", "col35-1", "col40")
SHEET_NAMES = (
    "Endurance",
    "PV - 1st cycle",
    "PV - 10^5 cycle",
    "PUND - 1st cycle",
    "PUND - 10^5 cycle",
)
FILE_PATTERN = re.compile(r"_col(?P<column>\d+(?:-1)?)\.json$")
COLUMN_COLORS = (
    "#332288",
    "#88CCEE",
    "#44AA99",
    "#117733",
    "#999933",
    "#DDCC77",
    "#CC6677",
    "#882255",
    "#AA4499",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--base-organized",
        type=Path,
        help="Append PUND sheets to this project while preserving its existing content.",
    )
    parser.add_argument("--title", default="Organized")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


class IdFactory:
    def __init__(self, namespace: str):
        self.namespace = namespace
        self.counts = {"sh": 0, "gr": 0, "nd": 0}

    def make(self, prefix: str) -> str:
        index = self.counts[prefix]
        self.counts[prefix] += 1
        digest = hashlib.sha1(
            f"{self.namespace}:{prefix}:{index}".encode("utf-8")
        ).hexdigest()[:8]
        return f"{prefix}{digest}"


def load_sources(input_dir: Path) -> dict[str, dict[str, Any]]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"input directory does not exist: {input_dir}")

    sources: dict[str, dict[str, Any]] = {}
    for path in sorted(input_dir.glob("*.json")):
        match = FILE_PATTERN.search(path.name)
        if not match:
            continue
        column = f"col{match.group('column')}"
        if column in sources:
            raise ValueError(f"duplicate source project for {column}: {path}")
        with path.open("r", encoding="utf-8") as handle:
            project = json.load(handle)
        if project.get("schema_version") != 3:
            raise ValueError(f"source is not Pubfig schema v3: {path}")
        project["__source_path__"] = str(path)
        sources[column] = project

    missing = set(COLUMN_ORDER) - set(sources)
    extra = set(sources) - set(COLUMN_ORDER)
    if missing or extra:
        raise ValueError(
            f"expected exactly {list(COLUMN_ORDER)}; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    return sources


def only_sheet(project: dict[str, Any], name: str) -> dict[str, Any]:
    found = [sheet for sheet in project.get("sheets", []) if sheet.get("name") == name]
    if len(found) != 1:
        raise ValueError(
            f"{project['__source_path__']}: expected one {name!r} sheet, found {len(found)}"
        )
    return found[0]


def only_graph(project: dict[str, Any], sheet: dict[str, Any], kind: str) -> dict[str, Any]:
    graphs = [
        graph
        for graph in project.get("graphs", [])
        if graph.get("sheet_id") == sheet.get("id")
    ]
    if kind == "endurance":
        found = [graph for graph in graphs if graph.get("name") == "Endurance"]
    elif kind == "pv":
        found = [
            graph
            for graph in graphs
            if str(graph.get("name", "")).startswith("PV_")
            and not str(graph.get("name", "")).startswith("PV_IV_")
        ]
    elif kind == "jv":
        found = [
            graph
            for graph in graphs
            if str(graph.get("name", "")).startswith("PV_IV_")
        ]
    elif kind == "pund":
        found = graphs
    else:
        raise ValueError(f"unsupported graph kind: {kind}")
    if len(found) != 1:
        raise ValueError(
            f"{project['__source_path__']}: expected one {kind} graph, found {len(found)}"
        )
    return found[0]


def sheet_arrays(sheet: dict[str, Any]) -> tuple[list[str], list[str], list[str], list[list[Any]]]:
    data = sheet.get("data", {})
    columns = data.get("columns", [])
    rows = data.get("rows", [])
    if not isinstance(columns, list) or len(rows) < 2:
        raise ValueError(f"invalid tabular data in sheet {sheet.get('name')!r}")
    roles, labels = rows[0], rows[1]
    if len(roles) != len(columns) or len(labels) != len(columns):
        raise ValueError(f"header width mismatch in sheet {sheet.get('name')!r}")
    for row in rows[2:]:
        if len(row) != len(columns):
            raise ValueError(f"data row width mismatch in sheet {sheet.get('name')!r}")
    return columns, roles, labels, rows[2:]


def color_map() -> dict[str, str]:
    return dict(zip(COLUMN_ORDER, COLUMN_COLORS, strict=True))


def make_sheet(sheet_id: str, name: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"id": sheet_id, "name": name, "data": {"columns": columns, "rows": rows}}


def combine_endurance(
    sources: dict[str, dict[str, Any]], sheet_id: str, graph_id: str, colors: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    blocks: list[tuple[str, list[str], list[list[Any]]]] = []
    columns: list[str] = []
    labels: list[str] = []
    max_rows = 0

    for column in COLUMN_ORDER:
        sheet = only_sheet(sources[column], "Endurance")
        source_columns, roles, _source_labels, data_rows = sheet_arrays(sheet)
        if roles != ["X", "Y", "Y"] or len(source_columns) != 3:
            raise ValueError(f"{column}: expected Endurance roles X/Y/Y")
        expected_prefixes = ("cycle_", "Psw_", "Qsw_")
        if any(not name.startswith(prefix) for name, prefix in zip(source_columns, expected_prefixes, strict=True)):
            raise ValueError(f"{column}: unexpected Endurance columns {source_columns}")
        columns.extend(source_columns)
        labels.extend([f"{column} cycle", f"{column} Psw", f"{column} Qsw"])
        blocks.append((column, source_columns, data_rows))
        max_rows = max(max_rows, len(data_rows))

    rows: list[list[Any]] = [[role for _ in COLUMN_ORDER for role in ("X", "Y", "Y")], labels]
    for row_index in range(max_rows):
        row: list[Any] = []
        for _column, _source_columns, data_rows in blocks:
            row.extend(data_rows[row_index] if row_index < len(data_rows) else [None, None, None])
        rows.append(row)

    template_sheet = only_sheet(sources[COLUMN_ORDER[0]], "Endurance")
    template_graph = only_graph(sources[COLUMN_ORDER[0]], template_sheet, "endurance")
    base_series = template_graph.get("series_config", [])
    if len(base_series) < 2:
        raise ValueError("Endurance graph template has fewer than two series")

    series: list[dict[str, Any]] = []
    checked_y: list[str] = []
    for column, source_columns, _data_rows in blocks:
        cycle_name, psw_name, qsw_name = source_columns
        for source_style, y_name, metric in (
            (base_series[0], psw_name, "Psw"),
            (base_series[1], qsw_name, "Qsw"),
        ):
            item = copy.deepcopy(source_style)
            item.update(
                {
                    "x": cycle_name,
                    "y": y_name,
                    "label": f"{column} {metric}",
                    "color": colors[column],
                }
            )
            series.append(item)
            checked_y.append(y_name)

    graph = {
        "id": graph_id,
        "name": "Endurance - All columns",
        "sheet_id": sheet_id,
        "plot_config": copy.deepcopy(template_graph["plot_config"]),
        "series_config": series,
        "checked_y": checked_y,
    }
    return make_sheet(sheet_id, SHEET_NAMES[0], columns, rows), graph


def select_pv_triplet(
    project: dict[str, Any], column: str, cycle_suffix: str
) -> tuple[list[str], list[list[Any]]] | None:
    sheet = only_sheet(project, "PV loops")
    columns, roles, _labels, data_rows = sheet_arrays(sheet)
    endurance_columns, _end_roles, _end_labels, _end_rows = sheet_arrays(
        only_sheet(project, "Endurance")
    )
    safe = next(
        (name[len("cycle_") :] for name in endurance_columns if name.startswith("cycle_")),
        None,
    )
    if not safe:
        raise ValueError(f"{column}: cannot determine source column suffix")
    names = [
        f"V_{safe}_{cycle_suffix}",
        f"P_{safe}_{cycle_suffix}",
        f"J_{safe}_{cycle_suffix}",
    ]
    if all(name not in columns for name in names):
        return None
    if any(name not in columns for name in names):
        raise ValueError(f"{column}: incomplete V/P/J triplet for cycle suffix {cycle_suffix}")
    indices = [columns.index(name) for name in names]
    if [roles[index] for index in indices] != ["X", "Y", "Y"]:
        raise ValueError(f"{column}: invalid V/P/J roles for cycle suffix {cycle_suffix}")
    return names, [[row[index] for index in indices] for row in data_rows]


def combine_pv_jv(
    sources: dict[str, dict[str, Any]],
    sheet_id: str,
    pv_graph_id: str,
    jv_graph_id: str,
    colors: dict[str, str],
    cycle_suffix: str,
    sheet_name: str,
    pv_graph_name: str,
    jv_graph_name: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    blocks: list[tuple[str, list[str], list[list[Any]]]] = []
    max_rows = 0
    for column in COLUMN_ORDER:
        triplet = select_pv_triplet(sources[column], column, cycle_suffix)
        if triplet is None:
            continue
        names, data_rows = triplet
        blocks.append((column, names, data_rows))
        max_rows = max(max_rows, len(data_rows))
    if not blocks:
        raise ValueError(f"no P-V data found for cycle suffix {cycle_suffix}")

    columns = [name for _column, names, _data_rows in blocks for name in names]
    labels = [column for column, _names, _data_rows in blocks for _ in range(3)]
    rows: list[list[Any]] = [
        [role for _column, _names, _data_rows in blocks for role in ("X", "Y", "Y")],
        labels,
    ]
    for row_index in range(max_rows):
        row: list[Any] = []
        for _column, _names, data_rows in blocks:
            row.extend(
                data_rows[row_index]
                if row_index < len(data_rows)
                else [None, None, None]
            )
        rows.append(row)

    template_sheet = only_sheet(sources[COLUMN_ORDER[0]], "PV loops")
    pv_template_graph = only_graph(sources[COLUMN_ORDER[0]], template_sheet, "pv")
    jv_template_graph = only_graph(sources[COLUMN_ORDER[0]], template_sheet, "jv")
    if not pv_template_graph.get("series_config"):
        raise ValueError("P-V graph template has no series")
    if not jv_template_graph.get("series_config"):
        raise ValueError("J-V graph template has no series")
    pv_base_series = pv_template_graph["series_config"][0]
    jv_base_series = jv_template_graph["series_config"][0]

    pv_series: list[dict[str, Any]] = []
    jv_series: list[dict[str, Any]] = []
    pv_checked_y: list[str] = []
    jv_checked_y: list[str] = []
    for column, names, _data_rows in blocks:
        x_name, p_name, j_name = names
        pv_item = copy.deepcopy(pv_base_series)
        pv_item.update(
            {
                "x": x_name,
                "y": p_name,
                "label": column,
                "color": colors[column],
            }
        )
        jv_item = copy.deepcopy(jv_base_series)
        jv_item.update(
            {
                "x": x_name,
                "y": j_name,
                "label": column,
                "color": colors[column],
            }
        )
        pv_series.append(pv_item)
        jv_series.append(jv_item)
        pv_checked_y.append(p_name)
        jv_checked_y.append(j_name)

    pv_graph = {
        "id": pv_graph_id,
        "name": pv_graph_name,
        "sheet_id": sheet_id,
        "plot_config": copy.deepcopy(pv_template_graph["plot_config"]),
        "series_config": pv_series,
        "checked_y": pv_checked_y,
    }
    jv_graph = {
        "id": jv_graph_id,
        "name": jv_graph_name,
        "sheet_id": sheet_id,
        "plot_config": copy.deepcopy(jv_template_graph["plot_config"]),
        "series_config": jv_series,
        "checked_y": jv_checked_y,
    }
    return (
        make_sheet(sheet_id, sheet_name, columns, rows),
        [pv_graph, jv_graph],
        [block[0] for block in blocks],
    )


def select_pund_pair(
    project: dict[str, Any], column: str, cycle_suffix: str
) -> tuple[list[str], list[list[Any]]] | None:
    sheet = only_sheet(project, "PUND loops")
    columns, roles, _labels, data_rows = sheet_arrays(sheet)
    endurance_columns, _end_roles, _end_labels, _end_rows = sheet_arrays(
        only_sheet(project, "Endurance")
    )
    safe = next(
        (name[len("cycle_") :] for name in endurance_columns if name.startswith("cycle_")),
        None,
    )
    if not safe:
        raise ValueError(f"{column}: cannot determine source column suffix")
    names = [f"Vloop_{safe}_{cycle_suffix}", f"Ploop_{safe}_{cycle_suffix}"]
    if all(name not in columns for name in names):
        return None
    if any(name not in columns for name in names):
        raise ValueError(f"{column}: incomplete PUND pair for cycle suffix {cycle_suffix}")
    indices = [columns.index(name) for name in names]
    if [roles[index] for index in indices] != ["X", "Y"]:
        raise ValueError(f"{column}: invalid PUND roles for cycle suffix {cycle_suffix}")
    return names, [[row[index] for index in indices] for row in data_rows]


def combine_pund(
    sources: dict[str, dict[str, Any]],
    sheet_id: str,
    graph_id: str,
    colors: dict[str, str],
    cycle_suffix: str,
    sheet_name: str,
    graph_name: str,
    checked_columns: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    blocks: list[tuple[str, list[str], list[list[Any]]]] = []
    max_rows = 0
    for column in COLUMN_ORDER:
        pair = select_pund_pair(sources[column], column, cycle_suffix)
        if pair is None:
            continue
        names, data_rows = pair
        blocks.append((column, names, data_rows))
        max_rows = max(max_rows, len(data_rows))
    if not blocks:
        raise ValueError(f"no PUND data found for cycle suffix {cycle_suffix}")

    columns = [name for _column, names, _data_rows in blocks for name in names]
    labels = [column for column, _names, _data_rows in blocks for _ in range(2)]
    rows: list[list[Any]] = [
        [role for _column, _names, _data_rows in blocks for role in ("X", "Y")],
        labels,
    ]
    for row_index in range(max_rows):
        row: list[Any] = []
        for _column, _names, data_rows in blocks:
            row.extend(data_rows[row_index] if row_index < len(data_rows) else [None, None])
        rows.append(row)

    template_sheet = only_sheet(sources[COLUMN_ORDER[0]], "PUND loops")
    template_graph = only_graph(sources[COLUMN_ORDER[0]], template_sheet, "pund")
    if not template_graph.get("series_config"):
        raise ValueError("PUND graph template has no series")
    base_series = template_graph["series_config"][0]

    series: list[dict[str, Any]] = []
    checked_y: list[str] = []
    for column, names, _data_rows in blocks:
        x_name, y_name = names
        item = copy.deepcopy(base_series)
        item.update(
            {
                "x": x_name,
                "y": y_name,
                "label": column,
                "color": colors[column],
            }
        )
        series.append(item)
        if checked_columns is None or column in checked_columns:
            checked_y.append(y_name)

    graph = {
        "id": graph_id,
        "name": graph_name,
        "sheet_id": sheet_id,
        "plot_config": copy.deepcopy(template_graph["plot_config"]),
        "series_config": series,
        "checked_y": checked_y,
    }
    return make_sheet(sheet_id, sheet_name, columns, rows), graph, [block[0] for block in blocks]


def graph_node(node_id: str, graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "graph",
        "name": graph["name"],
        "ref_id": graph["id"],
        "expanded": True,
        "children": [],
    }


def sheet_node(
    node_id: str,
    sheet: dict[str, Any],
    graphs: list[dict[str, Any]],
    graph_node_ids: list[str],
) -> dict[str, Any]:
    if len(graphs) != len(graph_node_ids):
        raise ValueError("graph and graph-node counts do not match")
    return {
        "id": node_id,
        "type": "sheet",
        "name": sheet["name"],
        "ref_id": sheet["id"],
        "expanded": True,
        "children": [
            graph_node(graph_node_id, graph)
            for graph_node_id, graph in zip(graph_node_ids, graphs, strict=True)
        ],
    }


def walk_tree(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in node.get("children", []):
        yield from walk_tree(child)


def validate_project(project: dict[str, Any]) -> None:
    if project.get("schema_version") != 3:
        raise ValueError("project schema_version must be 3")
    sheets = project.get("sheets", [])
    graphs = project.get("graphs", [])
    if len(sheets) != 5 or len(graphs) != 7:
        raise ValueError(f"expected 5 sheets / 7 graphs, got {len(sheets)} / {len(graphs)}")
    if [sheet.get("name") for sheet in sheets] != list(SHEET_NAMES):
        raise ValueError("unexpected sheet names or order")

    sheet_map = {sheet["id"]: sheet for sheet in sheets}
    graph_map = {graph["id"]: graph for graph in graphs}
    if len(sheet_map) != len(sheets) or len(graph_map) != len(graphs):
        raise ValueError("duplicate sheet or graph ID")

    nodes = list(walk_tree(project["tree"]))
    node_ids = [node["id"] for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("tree node IDs must be unique")
    if project.get("active_node_id") not in set(node_ids):
        raise ValueError("active_node_id is not present in tree")
    sheet_refs = [node["ref_id"] for node in nodes if node["type"] == "sheet"]
    graph_refs = [node["ref_id"] for node in nodes if node["type"] == "graph"]
    if sorted(sheet_refs) != sorted(sheet_map) or sorted(graph_refs) != sorted(graph_map):
        raise ValueError("tree does not reference each sheet and graph exactly once")

    for sheet in sheets:
        columns, roles, labels, data_rows = sheet_arrays(sheet)
        if len(columns) != len(set(columns)):
            raise ValueError(f"{sheet['name']}: duplicate column name")
        for value in [*labels, *roles, *(item for row in data_rows for item in row)]:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{sheet['name']}: non-finite numeric value")

    for graph in graphs:
        sheet = sheet_map.get(graph.get("sheet_id"))
        if sheet is None:
            raise ValueError(f"{graph.get('name')}: missing sheet reference")
        columns = set(sheet["data"]["columns"])
        series_y: list[str] = []
        for series in graph.get("series_config", []):
            if series.get("x") not in columns or series.get("y") not in columns:
                raise ValueError(f"{graph['name']}: series references a missing column")
            series_y.append(series["y"])
        checked_y = graph.get("checked_y", [])
        if len(checked_y) != len(set(checked_y)) or not set(checked_y).issubset(series_y):
            raise ValueError(f"{graph['name']}: checked_y is not a unique series subset")
    json.dumps(project, ensure_ascii=False, allow_nan=False)


def build_project(
    sources: dict[str, dict[str, Any]], title: str
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    ids = IdFactory(title)
    colors = color_map()
    sheet_ids = [ids.make("sh") for _ in SHEET_NAMES]
    graph_ids = [ids.make("gr") for _ in range(7)]

    endurance_sheet, endurance_graph = combine_endurance(
        sources, sheet_ids[0], graph_ids[0], colors
    )
    first_sheet, first_graphs, first_columns = combine_pv_jv(
        sources,
        sheet_ids[1],
        graph_ids[1],
        graph_ids[2],
        colors,
        "1",
        SHEET_NAMES[1],
        "P-V - 1st cycle - All columns",
        "J-V - 1st cycle - All columns",
    )
    cycle_100k_sheet, cycle_100k_graphs, cycle_100k_columns = combine_pv_jv(
        sources,
        sheet_ids[2],
        graph_ids[3],
        graph_ids[4],
        colors,
        "100000",
        SHEET_NAMES[2],
        "P-V - 10^5 cycle - All available columns",
        "J-V - 10^5 cycle - All available columns",
    )
    pund_first_sheet, pund_first_graph, pund_first_columns = combine_pund(
        sources,
        sheet_ids[3],
        graph_ids[5],
        colors,
        "1",
        SHEET_NAMES[3],
        "PUND - 1st cycle - All columns",
    )
    pund_100k_sheet, pund_100k_graph, pund_100k_columns = combine_pund(
        sources,
        sheet_ids[4],
        graph_ids[6],
        colors,
        "100000",
        SHEET_NAMES[4],
        "PUND - 10^5 cycle - All available columns",
    )
    sheets = [
        endurance_sheet,
        first_sheet,
        cycle_100k_sheet,
        pund_first_sheet,
        pund_100k_sheet,
    ]
    graph_groups = [
        [endurance_graph],
        first_graphs,
        cycle_100k_graphs,
        [pund_first_graph],
        [pund_100k_graph],
    ]
    graphs = [graph for group in graph_groups for graph in group]

    root_id = ids.make("nd")
    sheet_node_ids = [ids.make("nd") for _ in sheets]
    graph_node_ids = [ids.make("nd") for _ in graphs]
    graph_node_groups = [
        graph_node_ids[:1],
        graph_node_ids[1:3],
        graph_node_ids[3:5],
        graph_node_ids[5:6],
        graph_node_ids[6:7],
    ]
    tree = {
        "id": root_id,
        "type": "folder",
        "name": title,
        "ref_id": None,
        "expanded": True,
        "children": [
            sheet_node(sheet_node_id, sheet, graph_group, graph_node_group)
            for sheet_node_id, sheet, graph_group, graph_node_group in zip(
                sheet_node_ids,
                sheets,
                graph_groups,
                graph_node_groups,
                strict=True,
            )
        ],
    }
    project = {
        "schema_version": 3,
        "active_node_id": graph_node_ids[0],
        "sheets": sheets,
        "graphs": graphs,
        "tree": tree,
    }
    validate_project(project)
    return project, {
        "endurance": list(COLUMN_ORDER),
        "first_cycle": first_columns,
        "100000_cycle": cycle_100k_columns,
        "pund_first_cycle": pund_first_columns,
        "pund_100000_cycle": pund_100k_columns,
    }


def source_safe(project: dict[str, Any]) -> str:
    columns, _roles, _labels, _rows = sheet_arrays(only_sheet(project, "Endurance"))
    safe = next(
        (name[len("cycle_") :] for name in columns if name.startswith("cycle_")),
        None,
    )
    if not safe:
        raise ValueError(f"{project['__source_path__']}: cannot determine column suffix")
    return safe


def graph_for_base_sheet(
    project: dict[str, Any], sheet_name: str, graph_prefix: str
) -> dict[str, Any]:
    sheets = [sheet for sheet in project.get("sheets", []) if sheet.get("name") == sheet_name]
    if len(sheets) != 1:
        raise ValueError(f"base project must contain one {sheet_name!r} sheet")
    graphs = [
        graph
        for graph in project.get("graphs", [])
        if graph.get("sheet_id") == sheets[0].get("id")
        and str(graph.get("name", "")).startswith(graph_prefix)
    ]
    if len(graphs) != 1:
        raise ValueError(
            f"base project must contain one {graph_prefix!r} graph on {sheet_name!r}"
        )
    return graphs[0]


def colors_from_base(
    project: dict[str, Any], sources: dict[str, dict[str, Any]]
) -> dict[str, str]:
    colors = color_map()
    graph = graph_for_base_sheet(project, SHEET_NAMES[1], "P-V")
    styles = {series.get("y"): series for series in graph.get("series_config", [])}
    for column in COLUMN_ORDER:
        y_name = f"P_{source_safe(sources[column])}_1"
        color = styles.get(y_name, {}).get("color")
        if isinstance(color, str) and color:
            colors[column] = color
    return colors


def project_ids(project: dict[str, Any]) -> set[str]:
    ids = {
        str(item.get("id"))
        for key in ("sheets", "graphs")
        for item in project.get(key, [])
    }
    ids.update(str(node.get("id")) for node in walk_tree(project["tree"]))
    return ids


def append_pund_to_base(
    base_path: Path,
    sources: dict[str, dict[str, Any]],
    title: str,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    with base_path.open("r", encoding="utf-8") as handle:
        base = json.load(handle)
    if base.get("schema_version") != 3:
        raise ValueError(f"base project is not Pubfig schema v3: {base_path}")
    if [sheet.get("name") for sheet in base.get("sheets", [])] != list(SHEET_NAMES[:3]):
        raise ValueError("base project must contain only the existing Endurance and two PV sheets")
    if len(base.get("graphs", [])) != 5:
        raise ValueError("base project must contain the five existing Endurance/P-V/J-V graphs")

    snapshot = copy.deepcopy(base)
    project = copy.deepcopy(base)
    used_ids = project_ids(project)
    base_digest = hashlib.sha256(
        json.dumps(base, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    ids = IdFactory(f"{title}:append-pund:{base_digest}")

    def unique_id(prefix: str) -> str:
        while True:
            candidate = ids.make(prefix)
            if candidate not in used_ids:
                used_ids.add(candidate)
                return candidate

    sheet_ids = [unique_id("sh"), unique_id("sh")]
    graph_ids = [unique_id("gr"), unique_id("gr")]
    colors = colors_from_base(project, sources)
    pund_first_sheet, pund_first_graph, pund_first_columns = combine_pund(
        sources,
        sheet_ids[0],
        graph_ids[0],
        colors,
        "1",
        SHEET_NAMES[3],
        "PUND - 1st cycle - All columns",
    )
    pund_100k_sheet, pund_100k_graph, pund_100k_columns = combine_pund(
        sources,
        sheet_ids[1],
        graph_ids[1],
        colors,
        "100000",
        SHEET_NAMES[4],
        "PUND - 10^5 cycle - All available columns",
    )

    project["sheets"].extend([pund_first_sheet, pund_100k_sheet])
    project["graphs"].extend([pund_first_graph, pund_100k_graph])
    new_sheet_node_ids = [unique_id("nd"), unique_id("nd")]
    new_graph_node_ids = [unique_id("nd"), unique_id("nd")]
    project["tree"]["children"].extend(
        [
            sheet_node(
                new_sheet_node_ids[0],
                pund_first_sheet,
                [pund_first_graph],
                [new_graph_node_ids[0]],
            ),
            sheet_node(
                new_sheet_node_ids[1],
                pund_100k_sheet,
                [pund_100k_graph],
                [new_graph_node_ids[1]],
            ),
        ]
    )
    validate_project(project)

    if project["sheets"][: len(snapshot["sheets"])] != snapshot["sheets"]:
        raise AssertionError("existing sheets changed while appending PUND")
    if project["graphs"][: len(snapshot["graphs"])] != snapshot["graphs"]:
        raise AssertionError("existing graphs changed while appending PUND")
    if project.get("active_node_id") != snapshot.get("active_node_id"):
        raise AssertionError("active node changed while appending PUND")
    old_tree = snapshot["tree"]
    new_tree = project["tree"]
    if {key: value for key, value in new_tree.items() if key != "children"} != {
        key: value for key, value in old_tree.items() if key != "children"
    }:
        raise AssertionError("existing root properties changed while appending PUND")
    if new_tree["children"][: len(old_tree["children"])] != old_tree["children"]:
        raise AssertionError("existing tree children changed while appending PUND")

    return project, {
        "endurance": list(COLUMN_ORDER),
        "first_cycle": list(COLUMN_ORDER),
        "100000_cycle": [column for column in COLUMN_ORDER if column != "col35"],
        "pund_first_cycle": pund_first_columns,
        "pund_100000_cycle": pund_100k_columns,
    }


def write_project(path: Path, project: dict[str, Any], overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists (use --overwrite): {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=".organized-build-", suffix=".json", dir=path.parent, delete=False
    )
    temporary = Path(handle.name)
    handle.close()
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(project, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    args = parse_args()
    sources = load_sources(args.input_dir)
    if args.base_organized is None:
        project, included = build_project(sources, args.title)
    else:
        project, included = append_pund_to_base(
            args.base_organized, sources, args.title
        )
    write_project(args.output, project, args.overwrite)
    print(f"Created {args.output}")
    print(f"Sheets: {', '.join(SHEET_NAMES)}")
    print(
        "Graphs: Endurance; P-V/J-V/PUND at 1st cycle; "
        "P-V/J-V/PUND at 10^5 cycle"
    )
    print(f"Endurance columns: {', '.join(included['endurance'])}")
    print(f"1st-cycle P-V/J-V columns: {', '.join(included['first_cycle'])}")
    print(f"10^5-cycle P-V/J-V columns: {', '.join(included['100000_cycle'])}")
    print(f"1st-cycle PUND columns: {', '.join(included['pund_first_cycle'])}")
    print(f"10^5-cycle PUND columns: {', '.join(included['pund_100000_cycle'])}")


if __name__ == "__main__":
    main()
