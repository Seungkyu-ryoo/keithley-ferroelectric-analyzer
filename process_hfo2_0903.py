#!/usr/bin/env python3
"""Build the organized 2026-09-03 HfO2 workbooks and Pubfig projects.

This campaign uses a different source directory layout from the 2026-09-01
campaign.  The numerical processing and Pubfig project construction are kept
in ``process_hfo2_0901.py``; this wrapper supplies the 09/03 group mapping and
records the one known source filename typo without modifying the USB data.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_SCRIPT = SCRIPT_DIR / "process_hfo2_0901.py"


def load_base_module() -> Any:
    spec = importlib.util.spec_from_file_location("hfo2_processing_base", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load processing module: {BASE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = load_base_module()


GROUPS = (
    # The USB tree omits the flash directory for 10 nm; the user confirmed
    # that all four voltage/column leaves belong to the 716 V, 5 ms group.
    base.GroupSpec("10nm", 10.0, "5.0ms", "HfO2 10nm", "10nm"),
    base.GroupSpec("5nm", 5.0, "Asdep", "HfO2 5nm/Asdep", "5nm"),
    base.GroupSpec("5nm", 5.0, "0.4ms", "HfO2 5nm/716V 0.4ms", "5nm"),
    base.GroupSpec("5nm", 5.0, "0.8ms", "HfO2 5nm/716V 0.8ms", "5nm"),
    base.GroupSpec("5nm", 5.0, "1.2ms", "HfO2 5nm/716V 1.2ms", "5nm"),
    base.GroupSpec("5nm", 5.0, "1.6ms", "HfO2 5nm/716V 1.6ms", "5nm"),
    base.GroupSpec("5nm", 5.0, "2.0ms", "HfO2 5nm/716V 2ms", "5nm"),
)


# In 5 nm / 716 V 1.6 ms / 2.5 V / col14 / PUND, the source sequence is
# 1, 1e2, ..., 1e5, 1w6, 1e7 while the companion PV series contains 1e6.
# Interpret this single filename as 1e6 and preserve the original name in the
# manifest so the correction remains auditable.
CYCLE_FILENAME_ALIASES = {"1w6": 1_000_000}
_parse_cycle_original = base.parse_cycle
_manifest_record_original = base.manifest_record


def parse_cycle_with_alias(path: Path) -> int | float:
    alias = CYCLE_FILENAME_ALIASES.get(path.stem.lower())
    if alias is not None:
        return alias
    return _parse_cycle_original(path)


def manifest_record_with_alias(*args: Any, **kwargs: Any) -> dict[str, Any]:
    record = _manifest_record_original(*args, **kwargs)
    path = args[4] if len(args) > 4 else kwargs.get("path")
    if isinstance(path, Path) and path.stem.lower() in CYCLE_FILENAME_ALIASES:
        note = (
            f"source filename {path.name!r} interpreted as cycle "
            f"{CYCLE_FILENAME_ALIASES[path.stem.lower()]:g} (1e6)"
        )
        if record["status"] == "ok":
            record["status"] = "warning"
            record["warning"] = note
        else:
            existing = record.get("warning")
            record["warning"] = f"{existing}; {note}" if existing else note
    return record


base.parse_cycle = parse_cycle_with_alias
base.manifest_record = manifest_record_with_alias


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("/Volumes/ESD-USB/Kiethley/09022026"),
    )
    parser.add_argument(
        "--template-json",
        type=Path,
        default=Path(
            "/Users/ryoo/Desktop/LDRD/Manuscript/HfO2/Json/"
            "0901_10nm,5nm/5nm/HfO2_5nm_2.0ms.json"
        ),
    )
    parser.add_argument("--data-output-root", type=Path, required=True)
    parser.add_argument("--json-output-root", type=Path, required=True)
    parser.add_argument("--date-token", default="0903")
    parser.add_argument("--date-display", default="09/03")
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
        base.write_workbook(group, workbook_path)
        base.write_project(project, project_path)
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

    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
