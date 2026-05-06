# Keithley Ferroelectric Cycle Analyzer

Desktop GUI for analyzing Keithley ferroelectric measurement data across Endurance, PUND, and PV workflows.

## Features

- Qt desktop GUI built with PySide6 and Matplotlib
- Automatic Endurance, PUND, and PV file detection from Keithley Excel columns
- Endurance combiner using `Settings` sheet `max_loops` values, with drag/drop file ordering and optional broken x-axis view
- PUND cycle selection from flexible file names such as `1e2.xls`, `PUND_1e2_cycle.xls`, or `PUND_1_cycle.xls`
- PV cycle selection from cycle-based file names
- Voltage or electric-field x-axis
- PV polarization or current-density loop plotting
- PUND/PV Ec and Pr metric plots versus cycle with multi-select metric controls
- Editable area, thickness, figure size, DPI, fonts, line width, marker size, axis limits, grid, and center axes
- Custom color list, Matplotlib colormaps, or single-color alpha progression
- Export current plotted data to CSV/Excel and save figures as PNG/PDF/SVG

## Folder Selection

Select either the parent folder:

```text
SampleFolder/
  Endurance/
  PUND/
  PV/
```

or select one of the individual `Endurance`, `PUND`, or `PV` folders directly and choose the matching mode in the GUI.

Files can also be mixed in the selected folder or nested folders. The app classifies files from the Excel `Data` sheet columns rather than relying only on exact folder names.

## Installation

```powershell
py -m pip install -r requirements.txt
```

## Run

```powershell
py ferro_cycle_analyzer.py
```

## Notes

- Endurance files show their `max_loops` value from the `Settings` sheet and are concatenated in the order shown in the file list.
- The app expects Keithley Excel files with sheets such as `Data` and `Settings`.
- Raw measurement data files are intentionally not included in this repository.
