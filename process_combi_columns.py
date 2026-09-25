#!/usr/bin/env python3
"""Build one audit workbook and one Pubfig v3 project per Keithley column.

The numerical transforms intentionally mirror ``ferro_cycle_analyzer_fixed.py``.
Graph formatting is copied from a user-supplied Pubfig schema-v3 JSON template;
the template's measured data are never copied into the generated projects.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import math
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from matplotlib import colormaps
from matplotlib.colors import to_hex
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from scipy.integrate import cumulative_trapezoid
from scipy.interpolate import interp1d


MODE_ORDER = ("Endurance", "PUND", "PV")
REQUIRED_COLUMNS = {
    "Endurance": {"pundEndurance", "iteration", "Psw", "Qsw"},
    "PUND": {"pundTest", "V", "I", "t"},
    "PV": {"doubleSweepSeg", "Vforce", "Imeas", "Charge"},
}
METRICS = (
    "Ec+",
    "Ec-",
    "|Ec+|",
    "|Ec-|",
    "|Ec+|-|Ec-|",
    "2Ec",
    "Pr+",
    "Pr-",
    "|Pr+|",
    "|Pr-|",
    "|Pr+|-|Pr-|",
    "2Pr",
)

WORKBOOK_SHEETS = (
    "Metadata",
    "File_manifest",
    "Endurance",
    "PUND_loops",
    "PUND_metrics",
    "PV_loops",
    "PV_metrics",
)

WORKBOOK_WIDTHS = {
    "Metadata": [24, 55],
    "File_manifest": [17, 17, 12, 18, 13, 52, 12, 13, 12, 55, 12],
    "Endurance": [15, 12, 14, 15, 12, 23, 24, 23, 24, 24, 23, 24, 23, 23, 23, 12, 14, 12, 19, 19],
    "PUND_loops": [15, 12, 12, 24, 20, 24, 19],
    "PUND_metrics": [15, 12, 12, 20, 21, 20, 20, 22, 19, 19, 20, 19, 19, 21, 19],
    "PV_loops": [15, 12, 12, 24, 24, 22, 23],
    "PV_metrics": [15, 12, 12, 20, 21, 20, 20, 20, 20, 19, 20, 19, 19, 21, 19],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--data-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--template-json", type=Path, required=True)
    parser.add_argument("--relative-base", type=Path)
    parser.add_argument("--project-prefix", required=True)
    parser.add_argument("--material", default="M2")
    parser.add_argument("--measurement-label", default="M2 RTA 200deg 666V 3.5ms")
    parser.add_argument("--pulse-condition", default="3.5ms")
    parser.add_argument("--date-code", default="0901")
    parser.add_argument("--date-display", default="09/01")
    parser.add_argument("--thickness-nm", type=float, required=True)
    parser.add_argument("--area-um2", type=float, required=True)
    parser.add_argument(
        "--column",
        action="append",
        dest="columns",
        help="Process only this column directory; repeat for multiple columns.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_excel_quiet(path: Path, sheet_name: str, header: int | None = 0) -> pd.DataFrame:
    with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
        warnings.simplefilter("ignore")
        return pd.read_excel(path, sheet_name=sheet_name, header=header)


def get_setting(settings: pd.DataFrame, name: str) -> float:
    rows = settings[settings.iloc[:, 0].astype(str).str.strip() == name]
    if rows.empty:
        raise ValueError(f"missing Settings value {name!r}")
    value = float(rows.iloc[0, 3])
    if not math.isfinite(value):
        raise ValueError(f"non-finite Settings value {name!r}")
    return value


def parse_cycle(path: Path) -> int:
    try:
        value = float(path.stem)
    except ValueError as exc:
        raise ValueError(f"cannot parse cycle from {path.name!r}") from exc
    if not math.isfinite(value) or value <= 0 or not value.is_integer():
        raise ValueError(f"cycle filename must represent a positive integer: {path.name!r}")
    return int(value)


def sorted_cycle_files(folder: Path) -> list[tuple[int, Path]]:
    if not folder.is_dir():
        raise FileNotFoundError(f"missing mode directory: {folder}")
    found: dict[int, Path] = {}
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".xls", ".xlsx"}:
            continue
        cycle = parse_cycle(path)
        if cycle in found:
            raise ValueError(
                f"duplicate cycle {cycle:g}: {found[cycle].name!r} and {path.name!r}"
            )
        found[cycle] = path
    if not found:
        raise ValueError(f"no Excel measurement files in {folder}")
    return sorted(found.items())


def require_columns(frame: pd.DataFrame, mode: str, path: Path) -> None:
    missing = REQUIRED_COLUMNS[mode] - set(map(str, frame.columns))
    if missing:
        raise ValueError(f"{path}: missing {mode} columns {sorted(missing)}")


def charge_to_polarization(values: Any, area_um2: float) -> Any:
    return values / (area_um2 * 1e-8) * 1e6


def current_to_density(values: Any, area_um2: float) -> Any:
    return values / (area_um2 * 1e-8)


def voltage_to_field(values: Any, thickness_nm: float) -> Any:
    return values * 10.0 / thickness_nm


def crossing_values(x_data: Iterable[float], y_data: Iterable[float], target: float) -> list[float]:
    x = np.asarray(x_data, dtype=float)
    y = np.asarray(y_data, dtype=float)
    values: list[float] = []
    for idx in range(len(x) - 1):
        x0, x1 = x[idx], x[idx + 1]
        y0, y1 = y[idx], y[idx + 1]
        if not np.isfinite([x0, x1, y0, y1]).all():
            continue
        d0 = y0 - target
        d1 = y1 - target
        if d0 == 0:
            values.append(float(x0))
        elif d0 * d1 < 0 and y1 != y0:
            values.append(float(x0 + (target - y0) * (x1 - x0) / (y1 - y0)))
    return values


def interpolate_at_x(x_data: Iterable[float], y_data: Iterable[float], target: float) -> list[float]:
    x = np.asarray(x_data, dtype=float)
    y = np.asarray(y_data, dtype=float)
    values: list[float] = []
    for idx in range(len(x) - 1):
        x0, x1 = x[idx], x[idx + 1]
        y0, y1 = y[idx], y[idx + 1]
        if not np.isfinite([x0, x1, y0, y1]).all():
            continue
        d0 = x0 - target
        d1 = x1 - target
        if d0 == 0:
            values.append(float(y0))
        elif d0 * d1 < 0 and x1 != x0:
            values.append(float(y0 + (target - x0) * (y1 - y0) / (x1 - x0)))
    return values


def metric_row_from_pund(
    cycle: int,
    file_name: str,
    pos_voltage: pd.Series,
    pos_pol: pd.Series,
    neg_voltage: pd.Series,
    neg_pol: pd.Series,
) -> dict[str, Any]:
    ec_pos_candidates = [v for v in crossing_values(pos_voltage, pos_pol, 0.0) if v > 0]
    ec_neg_candidates = [v for v in crossing_values(neg_voltage, neg_pol, 0.0) if v < 0]
    ec_pos = min(ec_pos_candidates, key=abs) if ec_pos_candidates else np.nan
    ec_neg = min(ec_neg_candidates, key=abs) if ec_neg_candidates else np.nan

    pos_v = np.asarray(pos_voltage, dtype=float)
    pos_p = np.asarray(pos_pol, dtype=float)
    neg_v = np.asarray(neg_voltage, dtype=float)
    neg_p = np.asarray(neg_pol, dtype=float)
    pos_start = int(np.nanargmax(pos_v))
    neg_start = int(np.nanargmin(neg_v))
    pr_pos = pos_p[pos_start + int(np.nanargmin(np.abs(pos_v[pos_start:])))]
    pr_neg = neg_p[neg_start + int(np.nanargmin(np.abs(neg_v[neg_start:])))]
    return make_metric_row(cycle, file_name, ec_pos, ec_neg, pr_pos, pr_neg)


def make_metric_row(
    cycle: int,
    file_name: str,
    ec_pos: float,
    ec_neg: float,
    pr_pos: float,
    pr_neg: float,
) -> dict[str, Any]:
    ec_finite = np.isfinite([ec_pos, ec_neg]).all()
    pr_finite = np.isfinite([pr_pos, pr_neg]).all()
    return {
        "cycle": cycle,
        "file": file_name,
        "Ec+": ec_pos,
        "Ec-": ec_neg,
        "|Ec+|": abs(ec_pos) if np.isfinite(ec_pos) else np.nan,
        "|Ec-|": abs(ec_neg) if np.isfinite(ec_neg) else np.nan,
        "|Ec+|-|Ec-|": abs(ec_pos) - abs(ec_neg) if ec_finite else np.nan,
        "2Ec": abs(ec_pos) + abs(ec_neg) if ec_finite else np.nan,
        "Pr+": pr_pos,
        "Pr-": pr_neg,
        "|Pr+|": abs(pr_pos) if np.isfinite(pr_pos) else np.nan,
        "|Pr-|": abs(pr_neg) if np.isfinite(pr_neg) else np.nan,
        "|Pr+|-|Pr-|": abs(pr_pos) - abs(pr_neg) if pr_finite else np.nan,
        "2Pr": pr_pos - pr_neg if pr_finite else np.nan,
    }


def calculate_pund(path: Path, area_um2: float) -> pd.DataFrame:
    data = read_excel_quiet(path, "Data")
    require_columns(data, "PUND", path)
    settings = read_excel_quiet(path, "Settings", header=None)
    tp = get_setting(settings, "tp")
    td = get_setting(settings, "td")
    trf = get_setting(settings, "trf")
    pulse_duration = trf + tp + trf
    pulse_interval = pulse_duration + td
    starts = [td + idx * pulse_interval for idx in range(4)]
    t_common = np.linspace(0.0, pulse_duration, 2000)
    times = data["t"].to_numpy(dtype=float)
    volts = data["V"].to_numpy(dtype=float)
    currents = data["I"].to_numpy(dtype=float)

    def interp_pulse(start: float) -> tuple[np.ndarray, np.ndarray]:
        mask = (times >= start) & (times <= start + pulse_duration)
        if int(mask.sum()) < 2:
            raise ValueError(f"{path}: insufficient waveform points near t={start:g}")
        t_rel = times[mask] - start
        return (
            interp1d(t_rel, volts[mask], bounds_error=False, fill_value=0)(t_common),
            interp1d(t_rel, currents[mask], bounds_error=False, fill_value=0)(t_common),
        )

    v_p, i_p = interp_pulse(starts[0])
    _, i_u = interp_pulse(starts[1])
    v_n, i_n = interp_pulse(starts[2])
    _, i_d = interp_pulse(starts[3])
    q_pos = cumulative_trapezoid(i_p - i_u, t_common, initial=0)
    q_neg = cumulative_trapezoid(i_n - i_d, t_common, initial=0)
    p_pos = charge_to_polarization(q_pos, area_um2)
    p_neg = charge_to_polarization(q_neg, area_um2)
    p_pos -= (p_pos[0] + p_pos[-1]) / 2.0
    p_neg -= (p_neg[0] + p_neg[-1]) / 2.0
    return pd.DataFrame(
        {
            "Voltage_Pos_V": v_p,
            "P_Pos_uC_cm2": p_pos,
            "Voltage_Neg_V": v_n,
            "P_Neg_uC_cm2": p_neg,
        }
    )


def pv_offset(voltage: pd.Series, polarization: pd.Series) -> tuple[float, bool]:
    minus_pr = float(polarization.iloc[0])
    plus_pr: float | None = None
    threshold = 0
    for idx in range(len(voltage)):
        if voltage.iloc[idx] > 0.2:
            threshold = idx
            break
    for idx in range(threshold, len(voltage)):
        if voltage.iloc[idx] < 0:
            plus_pr = float(polarization.iloc[idx])
            break
    if plus_pr is None:
        return float(polarization.median()), True
    return (minus_pr + plus_pr) / 2.0, False


def calculate_pv(
    path: Path, cycle: int, area_um2: float, thickness_nm: float
) -> tuple[pd.DataFrame, dict[str, Any], bool]:
    data = read_excel_quiet(path, "Data")
    require_columns(data, "PV", path)
    voltage = data["Vforce"].astype(float)
    polarization = charge_to_polarization(data["Charge"].astype(float), area_um2)
    offset, used_fallback = pv_offset(voltage, polarization)
    polarization = polarization - offset
    current_density = current_to_density(data["Imeas"].astype(float), area_um2)

    ec_crossings = crossing_values(voltage, polarization, 0.0)
    ec_pos_values = [value for value in ec_crossings if value > 0]
    ec_neg_values = [value for value in ec_crossings if value < 0]
    ec_pos = min(ec_pos_values, key=abs) if ec_pos_values else np.nan
    ec_neg = min(ec_neg_values, key=abs) if ec_neg_values else np.nan
    pr_crossings = interpolate_at_x(voltage, polarization, 0.0)
    pr_pos_values = [value for value in pr_crossings if value > 0]
    pr_neg_values = [value for value in pr_crossings if value < 0]
    pr_pos = max(pr_pos_values) if pr_pos_values else np.nan
    pr_neg = min(pr_neg_values) if pr_neg_values else np.nan
    metric = make_metric_row(cycle, path.name, ec_pos, ec_neg, pr_pos, pr_neg)

    loop = pd.DataFrame(
        {
            "Voltage_V": voltage,
            "Field_MV_cm": voltage_to_field(voltage, thickness_nm),
            "P_uC_cm2": polarization,
            "J_A_cm2": current_density,
        }
    )
    return loop, metric, used_fallback


def relative_source_path(path: Path, relative_base: Path | None, source_root: Path) -> str:
    bases = [base for base in (relative_base, source_root.parent) if base is not None]
    for base in bases:
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def manifest_record(
    pulse_condition: str,
    condition: str,
    mode: str,
    cycle: int,
    path: Path,
    relative_base: Path | None,
    source_root: Path,
    input_rows: int,
    output_rows: int,
) -> dict[str, Any]:
    return {
        "pulse_condition": pulse_condition,
        "condition": condition,
        "mode": mode,
        "cycle_file_value": cycle,
        "source_file": path.name,
        "source_relative_path": relative_source_path(path, relative_base, source_root),
        "input_rows": input_rows,
        "output_rows": output_rows,
        "status": "ok",
        "warning": None,
        "size_bytes": path.stat().st_size,
    }


def add_manifest_warning(record: dict[str, Any], message: str) -> None:
    existing = record.get("warning")
    record["warning"] = f"{existing}; {message}" if existing else message
    record["status"] = "warning"


def find_manifest_record(
    records: list[dict[str, Any]], mode: str, file_name: str
) -> dict[str, Any]:
    for record in records:
        if record["mode"] == mode and record["source_file"] == file_name:
            return record
    raise KeyError((mode, file_name))


@dataclass
class ProcessedColumn:
    column: str
    condition: str
    display: str
    metadata: pd.DataFrame
    manifest: pd.DataFrame
    endurance: pd.DataFrame
    pund_loops: pd.DataFrame
    pund_metrics: pd.DataFrame
    pv_loops: pd.DataFrame
    pv_metrics: pd.DataFrame


def process_column(column_dir: Path, args: argparse.Namespace) -> ProcessedColumn:
    column = column_dir.name
    condition = f"{column}_{args.date_code}"
    display = f"{column} ({args.date_display})"
    manifest: list[dict[str, Any]] = []

    endurance_blocks: list[pd.DataFrame] = []
    endurance_endpoints: list[tuple[str, float, float]] = []
    previous_end = 0
    endurance_files = sorted_cycle_files(column_dir / "Endurance")
    for cycle, path in endurance_files:
        data = read_excel_quiet(path, "Data")
        require_columns(data, "Endurance", path)
        settings = read_excel_quiet(path, "Settings", header=None)
        max_loops_float = get_setting(settings, "max_loops")
        if not max_loops_float.is_integer():
            raise ValueError(f"{path}: max_loops is not integral: {max_loops_float}")
        max_loops = int(max_loops_float)

        block = data.copy()
        block.insert(0, "condition", condition)
        block.insert(1, "file", path.name)
        block.insert(2, "global_cycle", previous_end + block["iteration"])
        block["plot_cycle"] = block["global_cycle"].clip(lower=1)
        block["axis_segment"] = 0
        block["axis_cycle"] = block["plot_cycle"]
        block["Psw_uC_cm2"] = charge_to_polarization(block["Psw"], args.area_um2)
        block["Qsw_uC_cm2"] = charge_to_polarization(block["Qsw"], args.area_um2)
        endurance_blocks.append(block)
        endpoint = block.iloc[-1]
        endurance_endpoints.append(
            (path.name, float(endpoint["Psw_uC_cm2"]), float(endpoint["Qsw_uC_cm2"]))
        )
        manifest.append(
            manifest_record(
                args.pulse_condition,
                condition,
                "Endurance",
                cycle,
                path,
                args.relative_base,
                args.source_root,
                len(data),
                len(block),
            )
        )
        previous_end += max_loops
    endurance = pd.concat(endurance_blocks, ignore_index=True)

    pund_frames: list[pd.DataFrame] = []
    pund_metric_rows: list[dict[str, Any]] = []
    pund_cycles: set[int] = set()
    for cycle, path in sorted_cycle_files(column_dir / "PUND"):
        raw = read_excel_quiet(path, "Data")
        require_columns(raw, "PUND", path)
        loop = calculate_pund(path, args.area_um2)
        metric = metric_row_from_pund(
            cycle,
            path.name,
            loop["Voltage_Pos_V"],
            loop["P_Pos_uC_cm2"],
            loop["Voltage_Neg_V"],
            loop["P_Neg_uC_cm2"],
        )
        loop.insert(0, "condition", condition)
        loop.insert(1, "cycle", cycle)
        loop.insert(2, "file", path.name)
        metric = {"condition": condition, **metric}
        pund_frames.append(loop)
        pund_metric_rows.append(metric)
        pund_cycles.add(cycle)
        manifest.append(
            manifest_record(
                args.pulse_condition,
                condition,
                "PUND",
                cycle,
                path,
                args.relative_base,
                args.source_root,
                len(raw),
                len(loop),
            )
        )
    pund_loops = pd.concat(pund_frames, ignore_index=True)
    pund_metrics = pd.DataFrame(pund_metric_rows).sort_values("cycle").reset_index(drop=True)

    pv_frames: list[pd.DataFrame] = []
    pv_metric_rows: list[dict[str, Any]] = []
    for cycle, path in sorted_cycle_files(column_dir / "PV"):
        raw = read_excel_quiet(path, "Data")
        require_columns(raw, "PV", path)
        loop, metric, fallback = calculate_pv(
            path, cycle, args.area_um2, args.thickness_nm
        )
        loop.insert(0, "condition", condition)
        loop.insert(1, "cycle", cycle)
        loop.insert(2, "file", path.name)
        metric = {"condition": condition, **metric}
        pv_frames.append(loop)
        pv_metric_rows.append(metric)
        record = manifest_record(
            args.pulse_condition,
            condition,
            "PV",
            cycle,
            path,
            args.relative_base,
            args.source_root,
            len(raw),
            len(loop),
        )
        if fallback:
            add_manifest_warning(record, "PV offset used median fallback")
        manifest.append(record)
    pv_loops = pd.concat(pv_frames, ignore_index=True)
    pv_metrics = pd.DataFrame(pv_metric_rows).sort_values("cycle").reset_index(drop=True)

    # Preserve abrupt final-device failures while making them explicit in the audit trail.
    prior_strengths: list[float] = []
    prior_positive_psw: list[float] = []
    for index, (file_name, psw, qsw) in enumerate(endurance_endpoints):
        strength = max(abs(psw), abs(qsw))
        if index:
            prior_max = max(prior_strengths)
            collapsed = prior_max >= 1.0 and strength < 0.35 * prior_max
            reversed_polarity = psw < 0 and any(value > 0 for value in prior_positive_psw)
            if collapsed or reversed_polarity:
                record = find_manifest_record(manifest, "Endurance", file_name)
                message = "measured polarization collapse/sign reversal retained (possible device breakdown)"
                if int(record["cycle_file_value"]) not in pund_cycles:
                    message += "; no matching post-Endurance PUND/PV checkpoint"
                add_manifest_warning(record, message)
        prior_strengths.append(strength)
        prior_positive_psw.append(psw)

    # Flag a strong file-size outlier only when the workbook still has the expected rows.
    for mode in MODE_ORDER:
        mode_records = [record for record in manifest if record["mode"] == mode]
        median_size = float(np.median([record["size_bytes"] for record in mode_records]))
        for record in mode_records:
            if record["size_bytes"] < 0.9 * median_size:
                add_manifest_warning(
                    record,
                    "file-size outlier retained; workbook and expected output rows parsed successfully",
                )

    manifest_columns = [
        "pulse_condition",
        "condition",
        "mode",
        "cycle_file_value",
        "source_file",
        "source_relative_path",
        "input_rows",
        "output_rows",
        "status",
        "warning",
        "size_bytes",
    ]
    manifest_frame = pd.DataFrame(manifest, columns=manifest_columns)
    warning_count = int((manifest_frame["status"] == "warning").sum())
    metadata_rows = [
        ("material", args.material),
        ("measurement_label", args.measurement_label),
        ("column", column),
        ("nominal_thickness_nm", float(args.thickness_nm)),
        ("pulse_width_file_group", args.pulse_condition),
        ("source_pulse_directory", args.source_root.name),
        (
            "pulse_width_note",
            "Project label comes from the requested output group; raw PUND Settings tp is retained separately in each XLS source.",
        ),
        ("conditions", condition),
        ("source_root", str(args.source_root)),
        ("source_file_count", len(manifest_frame)),
        ("source_modes", ", ".join(MODE_ORDER)),
        ("electrode_area_um2", float(args.area_um2)),
        ("electrode_area_source", "user-provided for this processing run"),
        ("thickness_source", "user-provided for this processing run"),
        ("metric_ec_axis", "Voltage (V), matching analyzer and JSON-template default"),
        (
            "pund_curve_definition",
            "One loop per cycle: P-U (2000 points) followed by N-D (2000 points) in Pubfig JSON.",
        ),
        ("warning_file_count", warning_count),
        (
            "organization_note",
            "Each source column directory is an independent device; similarly named reruns remain separate.",
        ),
        ("generated_utc", datetime.now(timezone.utc).isoformat()),
        ("analysis_script", str(Path(__file__).resolve())),
    ]
    metadata = pd.DataFrame(metadata_rows, columns=["key", "value"])
    return ProcessedColumn(
        column=column,
        condition=condition,
        display=display,
        metadata=metadata,
        manifest=manifest_frame,
        endurance=endurance,
        pund_loops=pund_loops,
        pund_metrics=pund_metrics,
        pv_loops=pv_loops,
        pv_metrics=pv_metrics,
    )


def write_workbook(path: Path, processed: ProcessedColumn) -> None:
    tables = {
        "Metadata": processed.metadata,
        "File_manifest": processed.manifest,
        "Endurance": processed.endurance,
        "PUND_loops": processed.pund_loops,
        "PUND_metrics": processed.pund_metrics,
        "PV_loops": processed.pv_loops,
        "PV_metrics": processed.pv_metrics,
    }
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name in WORKBOOK_SHEETS:
            tables[sheet_name].to_excel(writer, sheet_name=sheet_name, index=False)

    workbook = load_workbook(path)
    fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    font = Font(bold=True, color="FFFFFF")
    alignment = Alignment(horizontal="center")
    for sheet_name in WORKBOOK_SHEETS:
        sheet = workbook[sheet_name]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.fill = fill
            cell.font = font
            cell.alignment = alignment
        widths = WORKBOOK_WIDTHS[sheet_name]
        if len(widths) != sheet.max_column:
            raise ValueError(
                f"{sheet_name}: expected {len(widths)} columns, got {sheet.max_column}"
            )
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
    workbook.save(path)


def json_scalar(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if pd.isna(value):
        return None
    return str(value) if isinstance(value, Path) else value


def frame_rows(frame: pd.DataFrame) -> list[list[Any]]:
    return [[json_scalar(value) for value in row] for row in frame.itertuples(index=False, name=None)]


def safe_condition(condition: str) -> str:
    return re.sub(r"[^0-9A-Za-z_|-]+", "_", condition).replace("-", "_")


def cycle_suffix(cycle: int) -> str:
    return f"{float(cycle):g}"


def cycle_label(cycle: int) -> str:
    if cycle == 1:
        return "1st cycle"
    exponent = math.log10(cycle)
    if cycle >= 10 and abs(exponent - round(exponent)) < 1e-12:
        return f"10^{int(round(exponent))} cycle"
    return f"{cycle:g} cycle"


def adaptive_cycle_colors(count: int) -> list[str]:
    if count <= 0:
        return []
    if count == 1:
        points = [0.475]
    else:
        # This 0.05-0.90 range reproduces the supplied 08/26 JSON exactly.
        points = np.linspace(0.05, 0.90, count)
    return [to_hex(colormaps["viridis"](point), keep_alpha=False) for point in points]


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


@dataclass(frozen=True)
class GraphTemplates:
    old_safe: str
    old_display: str
    endurance: dict[str, Any]
    pund_ec: dict[str, Any]
    pund_pr: dict[str, Any]
    pv_ec: dict[str, Any]
    pv_pr: dict[str, Any]
    pund_loop: dict[str, Any]
    pv_loop: dict[str, Any]
    pv_current: dict[str, Any]


def load_graph_templates(path: Path) -> GraphTemplates:
    with path.open("r", encoding="utf-8") as handle:
        project = json.load(handle)
    if project.get("schema_version") != 3:
        raise ValueError(f"template is not Pubfig schema v3: {path}")
    first_sheets: dict[str, dict[str, Any]] = {}
    for sheet in project.get("sheets", []):
        first_sheets.setdefault(sheet.get("name", ""), sheet)
    required_names = {"Endurance", "PUND loops", "PUND metrics", "PV loops", "PV metrics"}
    missing = required_names - set(first_sheets)
    if missing:
        raise ValueError(f"template missing sheets: {sorted(missing)}")

    graphs = project.get("graphs", [])
    by_sheet: dict[str, list[dict[str, Any]]] = {}
    for graph in graphs:
        by_sheet.setdefault(graph["sheet_id"], []).append(graph)

    def graphs_for(sheet_name: str) -> list[dict[str, Any]]:
        return by_sheet.get(first_sheets[sheet_name]["id"], [])

    end_graph = next(g for g in graphs_for("Endurance") if g["name"] == "Endurance")
    pund_metric_graphs = {g["name"]: g for g in graphs_for("PUND metrics")}
    pv_metric_graphs = {g["name"]: g for g in graphs_for("PV metrics")}
    pund_loop_graph = graphs_for("PUND loops")[0]
    pv_loop_graphs = graphs_for("PV loops")
    pv_graph = next(g for g in pv_loop_graphs if g["name"].startswith("PV_") and not g["name"].startswith("PV_IV_"))
    pv_current_graph = next(g for g in pv_loop_graphs if g["name"].startswith("PV_IV_"))

    end_columns = first_sheets["Endurance"]["data"]["columns"]
    old_safe = next(column[len("cycle_") :] for column in end_columns if column.startswith("cycle_"))
    old_display = end_graph["series_config"][0]["label"].rsplit(" Psw", 1)[0]
    return GraphTemplates(
        old_safe=old_safe,
        old_display=old_display,
        endurance=end_graph,
        pund_ec=pund_metric_graphs["PUND Ec vs cycle"],
        pund_pr=pund_metric_graphs["PUND Pr vs cycle"],
        pv_ec=pv_metric_graphs["PV Ec vs cycle"],
        pv_pr=pv_metric_graphs["PV Pr vs cycle"],
        pund_loop=pund_loop_graph,
        pv_loop=pv_graph,
        pv_current=pv_current_graph,
    )


def replace_template_strings(value: Any, templates: GraphTemplates, safe: str, display: str) -> Any:
    if isinstance(value, str):
        return value.replace(templates.old_safe, safe).replace(templates.old_display, display)
    if isinstance(value, list):
        return [replace_template_strings(item, templates, safe, display) for item in value]
    if isinstance(value, dict):
        return {
            key: replace_template_strings(item, templates, safe, display)
            for key, item in value.items()
        }
    return value


def make_sheet(sheet_id: str, name: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"id": sheet_id, "name": name, "data": {"columns": columns, "rows": rows}}


def make_tabular_sheet(
    sheet_id: str, name: str, frame: pd.DataFrame, roles: list[str], labels: list[str]
) -> dict[str, Any]:
    columns = list(map(str, frame.columns))
    if not (len(columns) == len(roles) == len(labels)):
        raise ValueError(f"{name}: columns/roles/labels length mismatch")
    return make_sheet(sheet_id, name, columns, [roles, labels, *frame_rows(frame)])


def metadata_json_sheet(sheet_id: str, frame: pd.DataFrame) -> dict[str, Any]:
    columns = list(map(str, frame.columns))
    return make_sheet(
        sheet_id,
        "Metadata",
        columns,
        [["" for _ in columns], columns, *frame_rows(frame)],
    )


def manifest_json_sheet(sheet_id: str, frame: pd.DataFrame) -> dict[str, Any]:
    columns = list(map(str, frame.columns))
    return make_sheet(
        sheet_id,
        "File manifest",
        columns,
        [["" for _ in columns], columns, *frame_rows(frame)],
    )


def build_endurance_json(
    sheet_id: str, processed: ProcessedColumn, safe: str
) -> dict[str, Any]:
    columns = [f"cycle_{safe}", f"Psw_{safe}", f"Qsw_{safe}"]
    labels = [
        f"{processed.display} cycle",
        f"{processed.display} Psw",
        f"{processed.display} Qsw",
    ]
    frame = processed.endurance[["plot_cycle", "Psw_uC_cm2", "Qsw_uC_cm2"]].copy()
    frame.columns = columns
    frame[columns[0]] = frame[columns[0]].astype(float)
    return make_tabular_sheet(sheet_id, "Endurance", frame, ["X", "Y", "Y"], labels)


def build_pund_loop_json(
    sheet_id: str, processed: ProcessedColumn, safe: str
) -> tuple[dict[str, Any], list[int]]:
    cycles = sorted(int(value) for value in processed.pund_loops["cycle"].unique())
    columns: list[str] = []
    labels: list[str] = []
    roles: list[str] = []
    arrays: list[np.ndarray] = []
    for cycle in cycles:
        block = processed.pund_loops[processed.pund_loops["cycle"] == cycle]
        suffix = cycle_suffix(cycle)
        columns.extend([f"Vloop_{safe}_{suffix}", f"Ploop_{safe}_{suffix}"])
        label = cycle_label(cycle)
        labels.extend([label, label])
        roles.extend(["X", "Y"])
        arrays.extend(
            [
                np.concatenate(
                    [block["Voltage_Pos_V"].to_numpy(), block["Voltage_Neg_V"].to_numpy()]
                ),
                np.concatenate(
                    [block["P_Pos_uC_cm2"].to_numpy(), block["P_Neg_uC_cm2"].to_numpy()]
                ),
            ]
        )
    max_rows = max(map(len, arrays))
    rows = [
        [json_scalar(array[row]) if row < len(array) else None for array in arrays]
        for row in range(max_rows)
    ]
    return make_sheet(sheet_id, "PUND loops", columns, [roles, labels, *rows]), cycles


def build_pv_loop_json(
    sheet_id: str, processed: ProcessedColumn, safe: str
) -> tuple[dict[str, Any], list[int]]:
    cycles = sorted(int(value) for value in processed.pv_loops["cycle"].unique())
    columns: list[str] = []
    labels: list[str] = []
    roles: list[str] = []
    arrays: list[np.ndarray] = []
    for cycle in cycles:
        block = processed.pv_loops[processed.pv_loops["cycle"] == cycle]
        suffix = cycle_suffix(cycle)
        columns.extend(
            [f"V_{safe}_{suffix}", f"P_{safe}_{suffix}", f"J_{safe}_{suffix}"]
        )
        label = cycle_label(cycle)
        labels.extend([label, label, label])
        roles.extend(["X", "Y", "Y"])
        arrays.extend(
            [
                block["Voltage_V"].to_numpy(),
                block["P_uC_cm2"].to_numpy(),
                block["J_A_cm2"].to_numpy(),
            ]
        )
    max_rows = max(map(len, arrays))
    rows = [
        [json_scalar(array[row]) if row < len(array) else None for array in arrays]
        for row in range(max_rows)
    ]
    return make_sheet(sheet_id, "PV loops", columns, [roles, labels, *rows]), cycles


def build_metric_json(
    sheet_id: str,
    name: str,
    processed: ProcessedColumn,
    frame: pd.DataFrame,
    safe: str,
) -> dict[str, Any]:
    columns = [f"cycle_{safe}"] + [f"{metric.replace('+', '')}_{safe}" for metric in METRICS]
    labels = [f"{processed.display} cycle"] + [
        f"{processed.display} {metric}" for metric in METRICS
    ]
    output = frame[["cycle", *METRICS]].copy()
    output.columns = columns
    output[columns[0]] = output[columns[0]].astype(float)
    return make_tabular_sheet(sheet_id, name, output, ["X", *(["Y"] * 12)], labels)


def graph_from_template(
    graph_id: str,
    name: str,
    sheet_id: str,
    source: dict[str, Any],
    templates: GraphTemplates,
    safe: str,
    display: str,
) -> dict[str, Any]:
    series = replace_template_strings(
        copy.deepcopy(source["series_config"]), templates, safe, display
    )
    checked = replace_template_strings(copy.deepcopy(source["checked_y"]), templates, safe, display)
    return {
        "id": graph_id,
        "name": name,
        "sheet_id": sheet_id,
        "plot_config": copy.deepcopy(source["plot_config"]),
        "series_config": series,
        "checked_y": checked,
    }


def loop_graph_from_template(
    graph_id: str,
    name: str,
    sheet_id: str,
    source: dict[str, Any],
    safe: str,
    cycles: list[int],
    x_prefix: str,
    y_prefix: str,
) -> dict[str, Any]:
    base_series = copy.deepcopy(source["series_config"][0])
    colors = adaptive_cycle_colors(len(cycles))
    series: list[dict[str, Any]] = []
    checked: list[str] = []
    for cycle, color in zip(cycles, colors, strict=True):
        suffix = cycle_suffix(cycle)
        item = copy.deepcopy(base_series)
        item["x"] = f"{x_prefix}_{safe}_{suffix}"
        item["y"] = f"{y_prefix}_{safe}_{suffix}"
        item["label"] = cycle_label(cycle)
        item["color"] = color
        series.append(item)
        checked.append(item["y"])
    return {
        "id": graph_id,
        "name": name,
        "sheet_id": sheet_id,
        "plot_config": copy.deepcopy(source["plot_config"]),
        "series_config": series,
        "checked_y": checked,
    }


def sheet_node(node_id: str, name: str, ref_id: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "sheet",
        "name": name,
        "ref_id": ref_id,
        "expanded": True,
        "children": children,
    }


def graph_node(node_id: str, graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "graph",
        "name": graph["name"],
        "ref_id": graph["id"],
        "expanded": True,
        "children": [],
    }


def build_pubfig_project(
    processed: ProcessedColumn,
    templates: GraphTemplates,
    project_name: str,
) -> dict[str, Any]:
    ids = IdFactory(project_name)
    safe = safe_condition(processed.condition)
    sheet_ids = {name: ids.make("sh") for name in WORKBOOK_SHEETS}
    sheets: list[dict[str, Any]] = [
        metadata_json_sheet(sheet_ids["Metadata"], processed.metadata),
        manifest_json_sheet(sheet_ids["File_manifest"], processed.manifest),
        build_endurance_json(sheet_ids["Endurance"], processed, safe),
    ]
    pund_loop_sheet, pund_cycles = build_pund_loop_json(
        sheet_ids["PUND_loops"], processed, safe
    )
    sheets.append(pund_loop_sheet)
    sheets.append(
        build_metric_json(
            sheet_ids["PUND_metrics"],
            "PUND metrics",
            processed,
            processed.pund_metrics,
            safe,
        )
    )
    pv_loop_sheet, pv_cycles = build_pv_loop_json(sheet_ids["PV_loops"], processed, safe)
    sheets.append(pv_loop_sheet)
    sheets.append(
        build_metric_json(
            sheet_ids["PV_metrics"],
            "PV metrics",
            processed,
            processed.pv_metrics,
            safe,
        )
    )

    graph_ids = {
        "endurance": ids.make("gr"),
        "pund_ec": ids.make("gr"),
        "pund_pr": ids.make("gr"),
        "pv_ec": ids.make("gr"),
        "pv_pr": ids.make("gr"),
        "pund_loop": ids.make("gr"),
        "pv_loop": ids.make("gr"),
        "pv_current": ids.make("gr"),
    }
    graph_map: dict[str, dict[str, Any]] = {
        "endurance": graph_from_template(
            graph_ids["endurance"],
            "Endurance",
            sheet_ids["Endurance"],
            templates.endurance,
            templates,
            safe,
            processed.display,
        ),
        "pund_ec": graph_from_template(
            graph_ids["pund_ec"],
            "PUND Ec vs cycle",
            sheet_ids["PUND_metrics"],
            templates.pund_ec,
            templates,
            safe,
            processed.display,
        ),
        "pund_pr": graph_from_template(
            graph_ids["pund_pr"],
            "PUND Pr vs cycle",
            sheet_ids["PUND_metrics"],
            templates.pund_pr,
            templates,
            safe,
            processed.display,
        ),
        "pv_ec": graph_from_template(
            graph_ids["pv_ec"],
            "PV Ec vs cycle",
            sheet_ids["PV_metrics"],
            templates.pv_ec,
            templates,
            safe,
            processed.display,
        ),
        "pv_pr": graph_from_template(
            graph_ids["pv_pr"],
            "PV Pr vs cycle",
            sheet_ids["PV_metrics"],
            templates.pv_pr,
            templates,
            safe,
            processed.display,
        ),
        "pund_loop": loop_graph_from_template(
            graph_ids["pund_loop"],
            processed.display,
            sheet_ids["PUND_loops"],
            templates.pund_loop,
            safe,
            pund_cycles,
            "Vloop",
            "Ploop",
        ),
        "pv_loop": loop_graph_from_template(
            graph_ids["pv_loop"],
            f"PV_{processed.display}",
            sheet_ids["PV_loops"],
            templates.pv_loop,
            safe,
            pv_cycles,
            "V",
            "P",
        ),
        "pv_current": loop_graph_from_template(
            graph_ids["pv_current"],
            f"PV_IV_{processed.display}",
            sheet_ids["PV_loops"],
            templates.pv_current,
            safe,
            pv_cycles,
            "V",
            "J",
        ),
    }
    graphs = [
        graph_map["endurance"],
        graph_map["pund_ec"],
        graph_map["pund_pr"],
        graph_map["pv_ec"],
        graph_map["pv_pr"],
        graph_map["pund_loop"],
        graph_map["pv_loop"],
        graph_map["pv_current"],
    ]

    endurance_graph_node = graph_node(ids.make("nd"), graph_map["endurance"])
    tree = {
        "id": ids.make("nd"),
        "type": "folder",
        "name": project_name,
        "ref_id": None,
        "expanded": True,
        "children": [
            sheet_node(ids.make("nd"), "Metadata", sheet_ids["Metadata"], []),
            sheet_node(ids.make("nd"), "File manifest", sheet_ids["File_manifest"], []),
            {
                "id": ids.make("nd"),
                "type": "folder",
                "name": processed.column,
                "ref_id": None,
                "expanded": True,
                "children": [
                    sheet_node(
                        ids.make("nd"),
                        "Endurance",
                        sheet_ids["Endurance"],
                        [endurance_graph_node],
                    ),
                    sheet_node(
                        ids.make("nd"),
                        "PUND loops",
                        sheet_ids["PUND_loops"],
                        [graph_node(ids.make("nd"), graph_map["pund_loop"])],
                    ),
                    sheet_node(
                        ids.make("nd"),
                        "PUND metrics",
                        sheet_ids["PUND_metrics"],
                        [
                            graph_node(ids.make("nd"), graph_map["pund_ec"]),
                            graph_node(ids.make("nd"), graph_map["pund_pr"]),
                        ],
                    ),
                    sheet_node(
                        ids.make("nd"),
                        "PV loops",
                        sheet_ids["PV_loops"],
                        [
                            graph_node(ids.make("nd"), graph_map["pv_loop"]),
                            graph_node(ids.make("nd"), graph_map["pv_current"]),
                        ],
                    ),
                    sheet_node(
                        ids.make("nd"),
                        "PV metrics",
                        sheet_ids["PV_metrics"],
                        [
                            graph_node(ids.make("nd"), graph_map["pv_ec"]),
                            graph_node(ids.make("nd"), graph_map["pv_pr"]),
                        ],
                    ),
                ],
            },
        ],
    }
    project = {
        "schema_version": 3,
        "active_node_id": endurance_graph_node["id"],
        "sheets": sheets,
        "graphs": graphs,
        "tree": tree,
    }
    validate_project(project)
    return project


def walk_tree(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in node.get("children", []):
        yield from walk_tree(child)


def validate_project(project: dict[str, Any]) -> None:
    if project.get("schema_version") != 3:
        raise ValueError("project schema_version must be 3")
    sheets = project.get("sheets", [])
    graphs = project.get("graphs", [])
    if len(sheets) != 7 or len(graphs) != 8:
        raise ValueError(f"expected 7 sheets / 8 graphs, got {len(sheets)} / {len(graphs)}")
    sheet_map = {sheet["id"]: sheet for sheet in sheets}
    graph_map = {graph["id"]: graph for graph in graphs}
    if len(sheet_map) != len(sheets) or len(graph_map) != len(graphs):
        raise ValueError("duplicate sheet or graph ID")

    nodes = list(walk_tree(project["tree"]))
    node_ids = [node["id"] for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("duplicate tree node ID")
    if project.get("active_node_id") not in set(node_ids):
        raise ValueError("active_node_id is not present in tree")
    sheet_refs = [node["ref_id"] for node in nodes if node["type"] == "sheet"]
    graph_refs = [node["ref_id"] for node in nodes if node["type"] == "graph"]
    if sorted(sheet_refs) != sorted(sheet_map):
        raise ValueError("tree does not reference every sheet exactly once")
    if sorted(graph_refs) != sorted(graph_map):
        raise ValueError("tree does not reference every graph exactly once")

    for graph in graphs:
        sheet = sheet_map.get(graph["sheet_id"])
        if sheet is None:
            raise ValueError(f"graph {graph['id']} references missing sheet")
        columns = set(sheet["data"]["columns"])
        for series in graph.get("series_config", []):
            if series.get("x") not in columns or series.get("y") not in columns:
                raise ValueError(
                    f"graph {graph['name']}: invalid X/Y {series.get('x')!r}/{series.get('y')!r}"
                )
            error_column = series.get("error_column", "")
            if error_column and error_column not in columns:
                raise ValueError(f"graph {graph['name']}: invalid error column")
        checked = set(graph.get("checked_y", []))
        if not checked.issubset(columns):
            raise ValueError(f"graph {graph['name']}: checked_y references missing column")
    json.dumps(project, ensure_ascii=False, allow_nan=False)


def reserve_destination(path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists (use --overwrite): {path}")


def temporary_path(parent: Path, suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(prefix=".combi-build-", suffix=suffix, dir=parent, delete=False)
    path = Path(handle.name)
    handle.close()
    return path


def write_pair(
    workbook_path: Path,
    json_path: Path,
    processed: ProcessedColumn,
    templates: GraphTemplates,
    project_name: str,
    overwrite: bool,
) -> None:
    reserve_destination(workbook_path, overwrite)
    reserve_destination(json_path, overwrite)
    workbook_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    temp_workbook = temporary_path(workbook_path.parent, ".xlsx")
    temp_json = temporary_path(json_path.parent, ".json")
    try:
        write_workbook(temp_workbook, processed)
        project = build_pubfig_project(processed, templates, project_name)
        with temp_json.open("w", encoding="utf-8") as handle:
            json.dump(project, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        # NamedTemporaryFile starts at 0600; match the supplied 0644 artifacts.
        os.chmod(temp_workbook, 0o644)
        os.chmod(temp_json, 0o644)
        os.replace(temp_workbook, workbook_path)
        os.replace(temp_json, json_path)
    finally:
        temp_workbook.unlink(missing_ok=True)
        temp_json.unlink(missing_ok=True)


def column_sort_key(path: Path) -> tuple[int, str]:
    match = re.fullmatch(r"col(\d+)(.*)", path.name, flags=re.IGNORECASE)
    if match:
        return int(match.group(1)), match.group(2)
    return 10**9, path.name


def discover_columns(source_root: Path, selected: list[str] | None) -> list[Path]:
    if not source_root.is_dir():
        raise FileNotFoundError(source_root)
    columns = [path for path in source_root.iterdir() if path.is_dir()]
    if selected:
        selected_set = set(selected)
        columns = [path for path in columns if path.name in selected_set]
        missing = selected_set - {path.name for path in columns}
        if missing:
            raise FileNotFoundError(f"requested columns not found: {sorted(missing)}")
    columns.sort(key=column_sort_key)
    if not columns:
        raise ValueError("no column directories selected")
    return columns


def main() -> int:
    args = parse_args()
    if args.area_um2 <= 0 or args.thickness_nm <= 0:
        raise ValueError("area and thickness must be positive")
    args.source_root = args.source_root.resolve()
    args.data_output = args.data_output.resolve()
    args.json_output = args.json_output.resolve()
    args.template_json = args.template_json.resolve()
    if args.relative_base is not None:
        args.relative_base = args.relative_base.resolve()
    templates = load_graph_templates(args.template_json)
    summaries: list[dict[str, Any]] = []
    for column_dir in discover_columns(args.source_root, args.columns):
        processed = process_column(column_dir, args)
        basename = f"{args.project_prefix}_{processed.column}"
        workbook_path = args.data_output / f"{basename}.xlsx"
        json_path = args.json_output / f"{basename}.json"
        project_name = f"{args.project_prefix} {processed.column}"
        write_pair(
            workbook_path,
            json_path,
            processed,
            templates,
            project_name,
            args.overwrite,
        )
        counts = processed.manifest.groupby("mode").size().to_dict()
        summaries.append(
            {
                "column": processed.column,
                "source_files": int(len(processed.manifest)),
                "mode_counts": {key: int(value) for key, value in counts.items()},
                "warning_files": int((processed.manifest["status"] == "warning").sum()),
                "workbook": str(workbook_path),
                "json": str(json_path),
            }
        )
    print(json.dumps({"generated": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
