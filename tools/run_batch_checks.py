"""Dynamic (execution-based) batch validation runner.

Runs the exact same acceptance-check logic as signal_viewer.py (read_recording,
validate_batch, measure, evaluate) against every batch subfolder under a
dataset root, and writes CSV summaries of the results. This is the executed
counterpart to docs/validation_report.md, which verifies the logic by
inspection only.

Each subfolder of <root> is treated as one batch (expected to hold the
4 channel-pair CSV files signal_viewer.py itself expects).

Usage:
    python tools/run_batch_checks.py "<dataset root>" [--out-dir results]
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signal_viewer as sv


def check_folder(folder: Path, limits: sv.Limits):
    """Run the full acceptance check on one batch folder.

    Returns (summary_row, channel_rows) mirroring what the GUI itself
    would compute for this folder.
    """
    paths = sorted(p for p in folder.glob("*.csv") if p.is_file())
    if len(paths) != sv.EXPECTED_FILES:
        return {
            "folder": folder.name,
            "status": "SKIPPED",
            "n_files": len(paths),
            "n_channels": "",
            "n_failed": "",
            "detail": f"expected {sv.EXPECTED_FILES} CSV files, found {len(paths)}",
        }, []

    recordings, warnings, failures = [], [], []
    for path in paths:
        try:
            recording, file_warnings = sv.read_recording(path)
        except sv.FormatError as exc:
            failures.append(f"{path.name}: {exc}")
        else:
            recordings.append(recording)
            warnings.extend(file_warnings)

    if failures:
        return {
            "folder": folder.name,
            "status": "ERROR",
            "n_files": len(paths),
            "n_channels": "",
            "n_failed": "",
            "detail": "; ".join(failures),
        }, []

    warnings.extend(sv.validate_batch(recordings))

    measurements = [sv.measure(rec.data, limits.expected_period_ms) for rec in recordings]
    verdicts = [sv.evaluate(m, limits) for m in measurements]

    channel_rows = []
    for rec, m, v in zip(recordings, measurements, verdicts):
        channel_rows.append({
            "folder": folder.name,
            "channel": rec.label,
            "result": v.text,
            "frequency_hz": f"{m.frequency_hz:.4f}",
            "pk_pk_mv": f"{m.pk_pk_mv:.4f}",
            "shape": sv.identify_waveform(m.rms_ratio),
            "max_cycle_dev_ms": f"{m.max_period_dev_ms:.4f}",
            "duration_s": f"{m.duration_s:.3f}",
            "explanation": v.explanation,
        })

    n_failed = sum(1 for v in verdicts if not v.passed)
    summary_row = {
        "folder": folder.name,
        "status": "PASS" if n_failed == 0 else "FAIL",
        "n_files": len(paths),
        "n_channels": len(verdicts),
        "n_failed": n_failed,
        "detail": " | ".join(warnings) if warnings else "",
    }
    return summary_row, channel_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="folder containing one subfolder per batch")
    parser.add_argument(
        "--out-dir", type=Path, default=Path(__file__).resolve().parent.parent / "results",
        help="where to write the summary CSVs (default: <project>/results, gitignored)",
    )
    args = parser.parse_args()

    if not args.root.is_dir():
        raise SystemExit(f"Not a folder: {args.root}")

    limits = sv.configured_limits()
    subfolders = sorted(p for p in args.root.iterdir() if p.is_dir())
    if not subfolders:
        raise SystemExit(f"No subfolders found under {args.root}")

    summary_rows, channel_rows = [], []
    for folder in subfolders:
        summary_row, rows = check_folder(folder, limits)
        summary_rows.append(summary_row)
        channel_rows.extend(rows)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.out_dir / "batch_check_summary.csv"
    channels_path = args.out_dir / "batch_check_channels.csv"

    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)

    if channel_rows:
        with channels_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(channel_rows[0].keys()))
            writer.writeheader()
            writer.writerows(channel_rows)

    n_pass = sum(1 for r in summary_rows if r["status"] == "PASS")
    n_fail = sum(1 for r in summary_rows if r["status"] == "FAIL")
    n_other = len(summary_rows) - n_pass - n_fail
    print(f"Checked {len(summary_rows)} folder(s) under {args.root}")
    print(f"  PASS: {n_pass}   FAIL: {n_fail}   SKIPPED/ERROR: {n_other}")
    print(f"Summary written to  {summary_path}")
    if channel_rows:
        print(f"Channel detail written to  {channels_path}")


if __name__ == "__main__":
    main()
