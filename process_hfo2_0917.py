#!/usr/bin/env python3
"""Build the 2026-09-17 HfO2 5 nm, 30 x 30 um device outputs.

The source is a single +/-3 V condition with Endurance, PUND, and PV folders
directly below the 30x30 directory. Polarization and current density are
normalized by the user-specified 30 um x 30 um electrode area (900 um^2),
not the 20 um x 20 um / 400 um^2 default used by the shared analyzer.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_SCRIPT = SCRIPT_DIR / "process_hfo2_0901.py"
DEVICE_WIDTH_UM = 30.0
DEVICE_HEIGHT_UM = 30.0
ELECTRODE_AREA_UM2 = DEVICE_WIDTH_UM * DEVICE_HEIGHT_UM
MEASUREMENT_VOLTAGE_V = 3.0


def load_base_module() -> Any:
    spec = importlib.util.spec_from_file_location("hfo2_processing_0917_base", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load processing module: {BASE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = load_base_module()


GROUPS = (
    base.GroupSpec(
        thickness_label="5nm",
        thickness_nm=5.0,
        file_group="30x30",
        relative_source="HfO2/5nm/30x30",
        json_subdir="5nm",
    ),
)

EXPECTED_CYCLES_BY_MODE = {
    "Endurance": {100, 1_000, 10_000, 100_000, 1_000_000},
    "PUND": {1, 100, 1_000, 10_000, 100_000},
    "PV": {1, 100, 1_000, 10_000, 100_000},
}


def charge_to_polarization_30x30(
    values: Any,
    area_um2: float = ELECTRODE_AREA_UM2,
) -> Any:
    """Convert charge in C to polarization in uC/cm^2 using 900 um^2."""

    return values / (area_um2 * 1e-8) * 1e6


def current_to_density_30x30(
    values: Any,
    area_um2: float = ELECTRODE_AREA_UM2,
) -> Any:
    """Convert current in A to current density in A/cm^2 using 900 um^2."""

    return values / (area_um2 * 1e-8)


# Updating AREA_UM2 keeps generated metadata correct. Replacing both functions
# is also required because their original default arguments captured 400 um^2
# when the shared module was imported.
base.AREA_UM2 = ELECTRODE_AREA_UM2
base.charge_to_polarization = charge_to_polarization_30x30
base.current_to_density = current_to_density_30x30


def validate_source_voltage(path: Path, mode: str) -> None:
    """Verify that every source workbook belongs to the synthetic 3 V group."""

    settings = base.read_excel_quiet(path, "Settings", header=None)
    if mode == "Endurance":
        checks = (("Vp", 3.0), ("Vfat", 3.0))
    elif mode == "PUND":
        checks = (("Vp", 3.0),)
    elif mode == "PV":
        checks = (("V1", 3.0), ("V2", -3.0))
    else:
        raise ValueError(f"Unsupported mode {mode!r} for {path}")

    for setting, expected in checks:
        actual = base.get_setting(settings, setting)
        if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError(
                f"{path}: expected {setting}={expected:g} V, found {actual:g} V"
            )


def discover_single_condition(
    campaign_root: Path,
    group_spec: Any,
    *,
    date_token: str,
    date_display: str,
) -> tuple[Path, list[Any]]:
    """Discover the direct mode folders as one verified 3 V condition."""

    source_dir = campaign_root / group_spec.relative_source
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Missing source group: {source_dir}")

    files_by_mode: dict[str, list[Path]] = {}
    selected: set[Path] = set()
    for mode in base.MODES:
        mode_dir = source_dir / mode
        if not mode_dir.is_dir():
            raise FileNotFoundError(f"Missing {mode} directory: {mode_dir}")
        paths = sorted(
            mode_dir.glob("*.xls"),
            key=lambda item: (float(base.parse_cycle(item)), item.name),
        )
        if not paths:
            raise ValueError(f"No XLS files found in {mode_dir}")

        seen_cycles: set[int | float] = set()
        for path in paths:
            cycle = base.parse_cycle(path)
            if cycle in seen_cycles:
                raise ValueError(f"Duplicate {mode} cycle {cycle:g}: {path}")
            seen_cycles.add(cycle)
            validate_source_voltage(path, mode)
        if seen_cycles != EXPECTED_CYCLES_BY_MODE[mode]:
            raise ValueError(
                f"Unexpected {mode} cycles in {mode_dir}: "
                f"expected={sorted(EXPECTED_CYCLES_BY_MODE[mode])}, "
                f"found={sorted(seen_cycles)}"
            )

        files_by_mode[mode] = paths
        selected.update(paths)

    all_xls = set(source_dir.rglob("*.xls"))
    if all_xls != selected:
        missing = sorted(str(path.relative_to(source_dir)) for path in all_xls - selected)
        extra = sorted(str(path.relative_to(source_dir)) for path in selected - all_xls)
        raise ValueError(
            "Source selection mismatch; unclassified or missing XLS files: "
            f"unclassified={missing}, missing={extra}"
        )

    condition = base.Condition(
        condition_id=f"3V_{date_token}",
        display_name=f"3V ({date_display})",
        voltage="3V",
        voltage_value=MEASUREMENT_VOLTAGE_V,
        column_name=None,
        leaf_relative=".",
    )
    condition.files = files_by_mode
    return source_dir, [condition]


base.discover_conditions = discover_single_condition


def rewrite_metadata(group: Any) -> None:
    """Replace pulse-centric defaults with geometry and area provenance."""

    replacements = {
        "pulse_width_file_group": (
            "device_size_file_group",
            "30x30",
        ),
        "source_pulse_directory": (
            "source_device_directory",
            "30x30",
        ),
        "pulse_width_note": (
            "flash_condition_note",
            "No flash condition or duration is present in the source path or XLS Settings.",
        ),
        "electrode_area_um2": (
            "electrode_area_um2",
            ELECTRODE_AREA_UM2,
        ),
        "electrode_area_source": (
            "electrode_area_source",
            "User-specified 30 um x 30 um electrode dimensions; area = 900 um^2.",
        ),
        "organization_note": (
            "organization_note",
            "Single 3 V condition; the source has no flash-condition or column directories.",
        ),
    }

    metadata = group.metadata.copy()
    for old_key, (new_key, value) in replacements.items():
        mask = metadata["key"] == old_key
        if int(mask.sum()) != 1:
            raise ValueError(f"Expected one metadata row for {old_key!r}")
        metadata.loc[mask, "key"] = new_key
        metadata.loc[mask, "value"] = value

    extra = base.pd.DataFrame(
        [
            ("device_width_um", DEVICE_WIDTH_UM),
            ("device_height_um", DEVICE_HEIGHT_UM),
            ("electrode_area_cm2", ELECTRODE_AREA_UM2 * 1e-8),
            ("measurement_voltage_v", MEASUREMENT_VOLTAGE_V),
            (
                "area_normalization_note",
                "All polarization and current-density values use the 900 um^2 area.",
            ),
        ],
        columns=["key", "value"],
    )
    group.metadata = base.pd.concat([metadata, extra], ignore_index=True)


def rewrite_manifest(group: Any) -> None:
    """Use a geometry label instead of the base processor's pulse label."""

    if "pulse_condition" not in group.manifest.columns:
        raise ValueError("Missing pulse_condition column in generated manifest")
    if set(group.manifest["pulse_condition"]) != {"30x30"}:
        raise ValueError("Unexpected file-group value in generated manifest")
    group.manifest = group.manifest.rename(
        columns={"pulse_condition": "device_size_file_group"}
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("/Volumes/ESD-USB/Kiethley/09172026"),
    )
    parser.add_argument(
        "--template-json",
        type=Path,
        default=Path(
            "/Users/ryoo/Desktop/LDRD/Manuscript/HfO2/Json/"
            "0910_10nm,5nm/5nm/HfO2_5nm_10.1ms.json"
        ),
    )
    parser.add_argument("--data-output-root", type=Path, required=True)
    parser.add_argument("--json-output-root", type=Path, required=True)
    parser.add_argument("--date-token", default="0917")
    parser.add_argument("--date-display", default="09/17")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacement of an already existing output file.",
    )
    return parser.parse_args()


def output_paths(args: argparse.Namespace, group_spec: Any) -> tuple[Path, Path]:
    workbook_path = (
        args.data_output_root
        / f"HfO2_{group_spec.thickness_label}_{group_spec.file_group}"
        / f"{group_spec.basename}.xlsx"
    )
    project_path = (
        args.json_output_root
        / group_spec.json_subdir
        / f"{group_spec.basename}.json"
    )
    return workbook_path, project_path


def main() -> int:
    args = parse_args()
    planned = [path for item in GROUPS for path in output_paths(args, item)]
    existing = [path for path in planned if path.exists()]
    if existing and not args.overwrite:
        formatted = "\n".join(f"- {path}" for path in existing)
        raise FileExistsError(
            "Refusing to overwrite existing outputs; pass --overwrite if intended:\n"
            f"{formatted}"
        )

    with args.template_json.open(encoding="utf-8") as handle:
        template = json.load(handle)

    prepared: list[tuple[Any, Any, dict[str, Any], Path, Path]] = []
    summaries: list[dict[str, Any]] = []
    for group_spec in GROUPS:
        group = base.process_group(
            args.source_root,
            group_spec,
            date_token=args.date_token,
            date_display=args.date_display,
            script_path=Path(__file__),
        )
        rewrite_metadata(group)
        rewrite_manifest(group)
        project = base.build_project(group, template)
        base.validate_project_payload(project)

        error_count = int((group.manifest["status"] == "error").sum())
        if error_count:
            errors = group.manifest[group.manifest["status"] == "error"]
            details = errors[
                ["source_relative_path", "warning"]
            ].to_dict(orient="records")
            raise RuntimeError(
                f"{group_spec.basename} has {error_count} processing errors: "
                f"{json.dumps(details, ensure_ascii=False)}"
            )

        workbook_path, project_path = output_paths(args, group_spec)
        prepared.append(
            (group_spec, group, project, workbook_path, project_path)
        )
        summaries.append(
            {
                "group": group_spec.basename,
                "source_files": len(group.manifest),
                "conditions": [item.condition_id for item in group.conditions],
                "electrode_area_um2": ELECTRODE_AREA_UM2,
                "warnings": int((group.manifest["status"] == "warning").sum()),
                "errors": error_count,
                "workbook": str(workbook_path),
                "project": str(project_path),
                "sheets": len(project["sheets"]),
                "graphs": len(project["graphs"]),
            }
        )

    for _group_spec, group, project, workbook_path, project_path in prepared:
        base.write_workbook(group, workbook_path)
        base.write_project(project, project_path)

    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
