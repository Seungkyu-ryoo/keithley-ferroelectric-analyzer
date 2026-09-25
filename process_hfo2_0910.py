#!/usr/bin/env python3
"""Build the organized 2026-09-10 HfO2 workbooks and Pubfig projects.

The numerical processing and Pubfig construction are shared with
process_hfo2_0901.py. This campaign-specific wrapper maps the 10.1 ms and
7.5 ms flash groups, restricts discovery to the intended standard-size
capacitor leaves, and normalizes the source typo "co10" to "col10"
without modifying the USB data. The "2.5V_larger_capcitors" branch is
intentionally excluded at the user's request.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_SCRIPT = SCRIPT_DIR / "process_hfo2_0901.py"


def load_base_module() -> Any:
    spec = importlib.util.spec_from_file_location("hfo2_processing_0910_base", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load processing module: {BASE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = load_base_module()


GROUPS = (
    base.GroupSpec(
        thickness_label="10nm",
        thickness_nm=10.0,
        file_group="10.1ms",
        relative_source="HfO2 10nm/716V 10.1ms",
        json_subdir="10nm",
        allowed_leaf_paths=(
            "4V/col10",
            "4V/col20",
            "4V/col30",
            "5V/col10",
            "5V/col20",
            "5V/col30",
        ),
    ),
    base.GroupSpec(
        thickness_label="5nm",
        thickness_nm=5.0,
        file_group="10.1ms",
        relative_source="HfO2 5nm/716V 10.1ms",
        json_subdir="5nm",
        allowed_leaf_paths=(
            "2.5V/col18",
            "2.5V/col20",
            "3V/co10",
            "3V/col20",
            "3V/col30",
        ),
    ),
    base.GroupSpec(
        thickness_label="5nm",
        thickness_nm=5.0,
        file_group="7.5ms",
        relative_source="HfO2 5nm/716V 7.5ms",
        json_subdir="5nm",
        allowed_leaf_paths=(
            "2.5V/col10",
            "2.5V/col20",
            "3V/col20",
        ),
    ),
)


CAMPAIGN_METADATA_NOTES = {
    "HfO2_5nm_10.1ms": (
        (
            "excluded_source_branch",
            "2.5V_larger_capcitors (20 XLS files; excluded at user request)",
        ),
        (
            "exclusion_reason",
            "Larger-capacitor area is not encoded in the source XLS files",
        ),
        (
            "column_alias_note",
            "Source folder 3V/co10 is represented as condition 3V_col10_0910; "
            "the original path is preserved in File manifest",
        ),
    ),
}


def append_campaign_metadata(group: Any) -> None:
    """Add campaign-specific provenance notes to the standalone outputs."""

    notes = CAMPAIGN_METADATA_NOTES.get(group.spec.basename)
    if not notes:
        return
    extra = base.pd.DataFrame(notes, columns=["key", "value"])
    group.metadata = base.pd.concat([group.metadata, extra], ignore_index=True)


def discover_conditions_with_column_aliases(
    campaign_root: Path,
    group_spec: Any,
    *,
    date_token: str,
    date_display: str,
) -> tuple[Path, list[Any]]:
    """Discover whitelisted leaves and canonicalize "co10" as "col10"."""

    source_dir = campaign_root / group_spec.relative_source
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Missing source group: {source_dir}")

    allowed = (
        {Path(item).as_posix() for item in group_spec.allowed_leaf_paths}
        if group_spec.allowed_leaf_paths
        else None
    )
    voltage_pattern = re.compile(r"^(-?\d+(?:\.\d+)?)V$", re.IGNORECASE)
    column_pattern = re.compile(
        r"^(?:col|co)\s*(\d+(?:-\d+)?)$", re.IGNORECASE
    )
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


base.discover_conditions = discover_conditions_with_column_aliases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("/Volumes/ESD-USB/Kiethley/09102026"),
    )
    parser.add_argument(
        "--template-json",
        type=Path,
        default=Path(
            "/Users/ryoo/Desktop/LDRD/Manuscript/HfO2/Json/"
            "0909_10nm/10nm/HfO2_10nm_5.0ms.json"
        ),
    )
    parser.add_argument("--data-output-root", type=Path, required=True)
    parser.add_argument("--json-output-root", type=Path, required=True)
    parser.add_argument("--date-token", default="0910")
    parser.add_argument("--date-display", default="09/10")
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
        append_campaign_metadata(group)
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
