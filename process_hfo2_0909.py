#!/usr/bin/env python3
"""Build the organized 2026-09-09 10 nm HfO2 workbooks and projects.

The numerical processing and Pubfig construction are shared with
``process_hfo2_0901.py``.  This campaign-specific wrapper maps the 5 ms and
7.5 ms flash groups and accepts source column directories such as ``col 13``
without renaming or otherwise modifying the USB data.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_SCRIPT = SCRIPT_DIR / "process_hfo2_0901.py"


def load_base_module() -> Any:
    spec = importlib.util.spec_from_file_location("hfo2_processing_0909_base", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load processing module: {BASE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = load_base_module()


GROUPS = (
    base.GroupSpec(
        "10nm",
        10.0,
        "5.0ms",
        "HfO2 10nm/716V 5ms",
        "10nm",
    ),
    base.GroupSpec(
        "10nm",
        10.0,
        "7.5ms",
        "HfO2 10nm/716V 7.5ms",
        "10nm",
    ),
)


def discover_conditions_with_spaced_columns(
    campaign_root: Path,
    group_spec: Any,
    *,
    date_token: str,
    date_display: str,
) -> tuple[Path, list[Any]]:
    """Discover conditions while normalizing ``col 13`` to ``col13``."""

    source_dir = campaign_root / group_spec.relative_source
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Missing source group: {source_dir}")

    allowed = (
        {Path(item).as_posix() for item in group_spec.allowed_leaf_paths}
        if group_spec.allowed_leaf_paths
        else None
    )
    voltage_pattern = re.compile(r"^(-?\d+(?:\.\d+)?)V$", re.IGNORECASE)
    column_pattern = re.compile(r"^col\s*(\d+(?:-\d+)?)$", re.IGNORECASE)
    discovered: dict[str, Any] = {}
    leaf_by_id: dict[str, str] = {}

    for path in sorted(source_dir.rglob("*.xls")):
        mode = path.parent.name
        if mode not in base.MODES:
            continue
        relative = path.relative_to(source_dir)
        mode_index = len(relative.parts) - 2
        ancestors = relative.parts[:mode_index]
        leaf_relative = Path(*ancestors).as_posix()
        if allowed is not None and leaf_relative not in allowed:
            continue

        voltage_candidates = [
            (part, voltage_pattern.fullmatch(part)) for part in ancestors
        ]
        voltage_candidates = [item for item in voltage_candidates if item[1]]
        if not voltage_candidates:
            raise ValueError(f"No voltage directory found above {path}")
        voltage, voltage_match = voltage_candidates[-1]
        assert voltage_match is not None
        voltage_value = float(voltage_match.group(1))

        column_candidates = [
            (part, column_pattern.fullmatch(part)) for part in ancestors
        ]
        column_candidates = [item for item in column_candidates if item[1]]
        column_name: str | None = None
        if column_candidates:
            _source_column, column_match = column_candidates[-1]
            assert column_match is not None
            column_name = f"col{column_match.group(1)}"

        pieces = [voltage]
        if column_name:
            pieces.append(column_name)
        pieces.append(date_token)
        condition_id = "_".join(pieces)
        display_parts = [voltage]
        if column_name:
            display_parts.append(column_name)
        display_name = f"{' '.join(display_parts)} ({date_display})"

        previous_leaf = leaf_by_id.get(condition_id)
        if previous_leaf is not None and previous_leaf != leaf_relative:
            raise ValueError(
                f"Condition {condition_id!r} maps to both {previous_leaf!r} "
                f"and {leaf_relative!r}"
            )
        leaf_by_id[condition_id] = leaf_relative
        condition = discovered.setdefault(
            condition_id,
            base.Condition(
                condition_id=condition_id,
                display_name=display_name,
                voltage=voltage,
                voltage_value=voltage_value,
                column_name=column_name,
                leaf_relative=leaf_relative,
            ),
        )
        condition.files[mode].append(path)

    if not discovered:
        raise ValueError(f"No Keithley XLS files found in {source_dir}")

    for condition in discovered.values():
        for mode in base.MODES:
            paths = condition.files[mode]
            paths.sort(key=lambda item: (float(base.parse_cycle(item)), item.name))
            seen: set[int | float] = set()
            for path in paths:
                cycle = base.parse_cycle(path)
                if cycle in seen:
                    raise ValueError(
                        f"Duplicate {mode} cycle {cycle:g} for "
                        f"{condition.condition_id}"
                    )
                seen.add(cycle)

    return source_dir, sorted(
        discovered.values(), key=base.condition_sort_key
    )


base.discover_conditions = discover_conditions_with_spaced_columns


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("/Volumes/ESD-USB/Kiethley/09092026"),
    )
    parser.add_argument(
        "--template-json",
        type=Path,
        default=Path(
            "/Users/ryoo/Desktop/LDRD/Manuscript/HfO2/Json/"
            "0903_10nm,5nm/10nm/HfO2_10nm_5.0ms.json"
        ),
    )
    parser.add_argument("--data-output-root", type=Path, required=True)
    parser.add_argument("--json-output-root", type=Path, required=True)
    parser.add_argument("--date-token", default="0909")
    parser.add_argument("--date-display", default="09/09")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacement of an already existing output file.",
    )
    return parser.parse_args()


def output_paths(args: argparse.Namespace, group_spec: Any) -> tuple[Path, Path]:
    workbook_path = (
        args.data_output_root
        / f"HfO2_{group_spec.thickness_label}"
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
