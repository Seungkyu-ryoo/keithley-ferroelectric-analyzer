# Keithley Ferroelectric Cycle Analyzer

Desktop GUI for analyzing Keithley ferroelectric measurement data across Endurance, PUND, and PV workflows.

## Features

- Qt desktop GUI built with PySide6 and Matplotlib
- Endurance folder combiner using `Settings` sheet `max_loops` values
- PUND cycle selection from flexible file names such as `1e2.xls`, `PUND_1e2_cycle.xls`, or `PUND_1_cycle.xls`
- PV cycle selection from cycle-based file names
- Voltage or electric-field x-axis
- PV polarization or current-density y-axis
- Editable area, thickness, figure size, DPI, fonts, line width, marker size, axis limits, grid, and center axes
- Custom color list, Matplotlib colormaps, or single-color alpha progression
- Export current plotted data to CSV/Excel and save figures as PNG/PDF/SVG

## Expected Folder Layout

Select either the parent folder:

```text
SampleFolder/
  Endurance/
  PUND/
  PV/
```

or select one of the individual `Endurance`, `PUND`, or `PV` folders directly and choose the matching mode in the GUI.

## Installation

```powershell
py -m pip install -r requirements.txt
```

## Run

```powershell
py ferro_cycle_qt_app.py
```

## Notes

- Endurance files are sorted by the `max_loops` value in each file's `Settings` sheet. This creates cumulative cycle ranges such as `0-100`, `100-1000`, `1000-10000`, and so on.
- The app expects Keithley Excel files with sheets such as `Data` and `Settings`.
- Raw measurement data files are intentionally not included in this repository.
