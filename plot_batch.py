"""Plot Time vs Signal for a folder of batch recording files.

Each CSV has four metadata lines, a column header, then the samples:

    Hobson, Zac (ABC123)          <- subject / ID
    2026/08/19 11:56              <- recording timestamp
    1C,2A,                        <- channel labels
    False,False,False,1000        <- flags + sample rate (Hz)
    Time(ms),Signal(mV),PC Time,Annotation
    0,-4.13592
    ...

Usage:
    python plot_batch.py                        # plots ./*.csv
    python plot_batch.py /path/to/folder
    python plot_batch.py /path/to/folder --save plots.png
"""

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

N_META_LINES = 4  # lines before the column header


@dataclass
class Recording:
    """One parsed CSV: metadata plus the sample data."""
    path: Path
    subject: str
    timestamp: str
    channels: str
    sample_rate_hz: float | None
    data: pd.DataFrame

    @property
    def label(self) -> str:
        return self.channels or self.path.stem


def read_recording(path: Path) -> Recording:
    """Read one file, returning metadata and a two-column DataFrame."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        meta = [next(fh).strip() for _ in range(N_META_LINES)]

    subject, timestamp, channel_line, config_line = meta
    channels = "-".join(p for p in channel_line.split(",") if p.strip())

    # Last field of the config line is the sample rate in Hz.
    try:
        sample_rate = float(config_line.split(",")[-1])
    except ValueError:
        sample_rate = None

    # usecols guards against the trailing PC Time / Annotation columns,
    # which are declared in the header but empty in these files.
    data = pd.read_csv(
        path,
        skiprows=N_META_LINES,
        usecols=[0, 1],
        names=["time_ms", "signal_mv"],
        header=0,
    ).apply(pd.to_numeric, errors="coerce").dropna()

    return Recording(path, subject, timestamp, channels, sample_rate, data)


def plot_recordings(
    recordings: list[Recording],
    save_path: Path | None = None,
    window_ms: float | None = None,
) -> None:
    """Draw one subplot per recording on a shared grid."""
    n = len(recordings)
    ncols = 2 if n > 1 else 1
    nrows = math.ceil(n / ncols)

    fig, axes = plt.subplots(
        nrows, ncols,
        figsize=(6.5 * ncols, 3.2 * nrows),
        sharex=True,
        squeeze=False,
    )
    flat_axes = axes.ravel()

    for ax, rec in zip(flat_axes, recordings):
        df = rec.data
        if window_ms is not None:
            df = df[df["time_ms"] <= window_ms]
        ax.plot(df["time_ms"], df["signal_mv"], linewidth=0.7)
        ax.set_title(rec.label, fontsize=11)
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel("Signal (mV)")
        ax.grid(True, alpha=0.3)
        ax.axhline(0, color="grey", linewidth=0.5)

    # Blank any unused panes when the file count doesn't fill the grid.
    for ax in flat_axes[n:]:
        ax.axis("off")

    subject = recordings[0].subject if recordings else ""
    fig.suptitle(subject, fontsize=13)
    fig.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150)
        print(f"Saved {save_path}")
    else:
        plt.show()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", default=".", type=Path)
    parser.add_argument("--pattern", default="*.csv")
    parser.add_argument("--save", type=Path, help="write to file instead of displaying")
    parser.add_argument(
        "--window", type=float, metavar="MS",
        help="plot only the first MS milliseconds (useful for waveform detail)",
    )
    args = parser.parse_args()

    paths = sorted(args.folder.glob(args.pattern))
    if not paths:
        raise SystemExit(f"No files matching {args.pattern!r} in {args.folder}")

    recordings = [read_recording(p) for p in paths]
    for rec in recordings:
        duration_s = rec.data["time_ms"].iloc[-1] / 1000
        print(
            f"{rec.path.name}: {rec.label}, {len(rec.data)} samples, "
            f"{duration_s:.1f} s @ {rec.sample_rate_hz:g} Hz"
        )

    plot_recordings(recordings, args.save, args.window)


if __name__ == "__main__":
    main()
