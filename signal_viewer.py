"""Signal Viewer - pick a folder of batch recordings and plot them.

Reads CSV files with four metadata lines, a column header, then samples:

    Hobson, Zac (ABC123)          <- subject / ID
    2026/08/19 11:56              <- recording timestamp
    1C,2A,                        <- channel labels
    False,False,False,1000        <- flags + sample rate (Hz)
    Time(ms),Signal(mV),PC Time,Annotation
    0,-4.13592
    ...

Run with:  python signal_viewer.py
"""

import math
import traceback
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from tkinter import Tk, StringVar, filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")  # must be set before pyplot is imported

import numpy as np
import pandas as pd
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg,
    NavigationToolbar2Tk,
)
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure

# ---------------------------------------------------------------------------
# Edit these to change how the application presents itself.
# ---------------------------------------------------------------------------
APP_TITLE = "Monitoring Waveform Acceptance Check"
APP_VERSION = "1.0"
RESULTS_DIR_NAME = "results"   # created inside the selected data folder
PDF_TIMESTAMP_FORMAT = "%Y-%m-%d_%H%M%S"   # no colons; Windows forbids them

APP_DESCRIPTION = (
    "Verifies a set of four recorded channels against acceptance criteria. Select "
    "the folder containing the CSV files and press Check: each channel is plotted, "
    "measured for frequency and peak amplitude, and compared against the limits "
    "below. A PDF report is saved automatically to the results folder."
)
# ---------------------------------------------------------------------------

N_META_LINES = 4  # lines before the column header
EXPECTED_FILES = 4  # a complete batch is four channel-pair recordings
DEFAULT_WINDOW_MS = "500"

# ---------------------------------------------------------------------------
# ACCEPTANCE LIMITS - FIXED
#
# These are the acceptance criteria the tool applies. They are deliberately
# NOT adjustable from the interface: an operator must not be able to widen a
# tolerance until a batch passes. They are shown in the window and printed on
# every report, but can only be changed here, which means a change requires a
# new build, a new version number and re-validation.
#
# Keep APP_VERSION in step with any change made below.
# ---------------------------------------------------------------------------
LIMIT_WAVEFORM = "triangle"
LIMIT_FREQUENCY_HZ = 10.0
LIMIT_PEAK_TO_PEAK_MV = 10.0
LIMIT_TIME_TOLERANCE_MS = 1.0
LIMIT_AMPLITUDE_TOLERANCE_MV = 1.0
LIMIT_FREQUENCY_TOLERANCE_HZ = 0.5
# ---------------------------------------------------------------------------

# Ideal ratio of RMS to peak amplitude for each waveform shape. Used to
# confirm the recorded signal really is the shape that was selected.
WAVEFORM_RMS_RATIO = {
    "triangle": 1 / math.sqrt(3),   # 0.5774
    "sine": 1 / math.sqrt(2),       # 0.7071
    "square": 1.0,
}
WAVEFORM_RATIO_TOL = 0.03   # separates the three shapes with margin to spare

PASS_COLOUR = "#d4f4d4"   # green
FAIL_COLOUR = "#ffd9a6"   # orange


@dataclass
class Limits:
    """Acceptance criteria applied to every channel."""
    waveform: str
    target_freq_hz: float
    target_pkpk_mv: float
    time_tol_ms: float
    amplitude_tol_mv: float
    freq_tol_hz: float

    @property
    def freq_range(self) -> tuple[float, float]:
        return (self.target_freq_hz - self.freq_tol_hz,
                self.target_freq_hz + self.freq_tol_hz)

    @property
    def pkpk_range(self) -> tuple[float, float]:
        return (self.target_pkpk_mv - self.amplitude_tol_mv,
                self.target_pkpk_mv + self.amplitude_tol_mv)

    @property
    def expected_period_ms(self) -> float:
        return 1000.0 / self.target_freq_hz if self.target_freq_hz else float("nan")


@dataclass
class Verdict:
    """Pass/fail outcome for one channel, with the reasoning."""
    passed: bool
    reasons: list[str]

    @property
    def text(self) -> str:
        return "PASS" if self.passed else "FAIL"

    @property
    def explanation(self) -> str:
        return "; ".join(self.reasons)


def configured_limits() -> Limits:
    """The fixed acceptance limits, checked for sanity at startup.

    Raises ValueError if the constants above have been edited into an
    unusable state, so a bad build fails immediately and visibly rather
    than producing wrong verdicts.
    """
    if LIMIT_WAVEFORM not in WAVEFORM_RMS_RATIO:
        raise ValueError(
            f"LIMIT_WAVEFORM is {LIMIT_WAVEFORM!r}; expected one of "
            + ", ".join(sorted(WAVEFORM_RMS_RATIO))
        )
    positive = {
        "LIMIT_FREQUENCY_HZ": LIMIT_FREQUENCY_HZ,
        "LIMIT_PEAK_TO_PEAK_MV": LIMIT_PEAK_TO_PEAK_MV,
        "LIMIT_TIME_TOLERANCE_MS": LIMIT_TIME_TOLERANCE_MS,
        "LIMIT_AMPLITUDE_TOLERANCE_MV": LIMIT_AMPLITUDE_TOLERANCE_MV,
        "LIMIT_FREQUENCY_TOLERANCE_HZ": LIMIT_FREQUENCY_TOLERANCE_HZ,
    }
    for name, value in positive.items():
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{name} must be a number greater than zero (got {value!r})")

    return Limits(
        waveform=LIMIT_WAVEFORM,
        target_freq_hz=float(LIMIT_FREQUENCY_HZ),
        target_pkpk_mv=float(LIMIT_PEAK_TO_PEAK_MV),
        time_tol_ms=float(LIMIT_TIME_TOLERANCE_MS),
        amplitude_tol_mv=float(LIMIT_AMPLITUDE_TOLERANCE_MV),
        freq_tol_hz=float(LIMIT_FREQUENCY_TOLERANCE_HZ),
    )


def identify_waveform(rms_ratio: float) -> str:
    """Name the closest standard waveform shape, or 'unknown'."""
    if math.isnan(rms_ratio):
        return "unknown"
    best = min(WAVEFORM_RMS_RATIO, key=lambda k: abs(WAVEFORM_RMS_RATIO[k] - rms_ratio))
    if abs(WAVEFORM_RMS_RATIO[best] - rms_ratio) <= WAVEFORM_RATIO_TOL:
        return best
    return "unknown"


def evaluate(m: "Measurement", limits: Limits) -> Verdict:
    """Compare one set of measurements against the acceptance limits."""
    reasons: list[str] = []

    # 1. Waveform shape, from the ratio of RMS to peak amplitude.
    measured_shape = identify_waveform(m.rms_ratio)
    if measured_shape != limits.waveform:
        expected_ratio = WAVEFORM_RMS_RATIO[limits.waveform]
        reasons.append(
            f"waveform looks like {measured_shape} rather than {limits.waveform} "
            f"(RMS/amplitude {m.rms_ratio:.4f}, expected {expected_ratio:.4f} "
            f"+/- {WAVEFORM_RATIO_TOL})"
        )

    # 2. Frequency.
    lo, hi = limits.freq_range
    if math.isnan(m.frequency_hz):
        reasons.append("frequency could not be determined")
    elif not lo <= m.frequency_hz <= hi:
        offset = m.frequency_hz - limits.target_freq_hz
        reasons.append(
            f"frequency {m.frequency_hz:.3f} Hz is {abs(offset):.3f} Hz "
            f"{'above' if offset > 0 else 'below'} target "
            f"(allowed {lo:.3f} to {hi:.3f} Hz)"
        )

    # 3. Peak-to-peak amplitude.
    lo, hi = limits.pkpk_range
    if not lo <= m.pk_pk_mv <= hi:
        offset = m.pk_pk_mv - limits.target_pkpk_mv
        reasons.append(
            f"peak-to-peak {m.pk_pk_mv:.3f} mV is {abs(offset):.3f} mV "
            f"{'above' if offset > 0 else 'below'} target "
            f"(allowed {lo:.3f} to {hi:.3f} mV)"
        )

    # 4. Cycle timing: no individual period may drift beyond the tolerance.
    if math.isnan(m.max_period_dev_ms):
        reasons.append("cycle timing could not be measured")
    elif m.max_period_dev_ms > limits.time_tol_ms:
        reasons.append(
            f"worst cycle period differs from the expected "
            f"{limits.expected_period_ms:.2f} ms by {m.max_period_dev_ms:.3f} ms "
            f"(allowed {limits.time_tol_ms:.3f} ms)"
        )

    if not reasons:
        return Verdict(True, ["all measurements within limits"])
    return Verdict(False, reasons)


@dataclass
class Measurement:
    """Derived quantities for one recording, computed on the full trace."""
    duration_s: float
    samples: int
    sample_rate_hz: float
    frequency_hz: float
    cycles: int
    peak_pos_mv: float
    peak_neg_mv: float
    peak_abs_mv: float
    pk_pk_mv: float
    rms_mv: float
    mean_mv: float
    rms_ratio: float          # RMS / peak amplitude - identifies the shape
    max_period_dev_ms: float  # worst single-cycle deviation from nominal


def measure(data: pd.DataFrame, expected_period_ms: float | None = None) -> Measurement:
    """Measure duration, frequency, amplitude, shape and cycle timing."""
    t = data["time_ms"].to_numpy(dtype=float) / 1000.0  # seconds
    y = data["signal_mv"].to_numpy(dtype=float)

    duration_s = float(t[-1] - t[0])
    sample_rate = (len(t) - 1) / duration_s if duration_s > 0 else float("nan")
    frequency, cycles, crossings = _frequency(t, y)

    pk_pk = float(y.max() - y.min())
    amplitude = pk_pk / 2.0
    centred = y - y.mean()
    rms_about_mean = float(np.sqrt(np.mean(centred**2)))

    # RMS measured about the mean, divided by peak amplitude, gives a shape
    # figure independent of gain and DC offset: 0.577 triangle, 0.707 sine.
    rms_ratio = rms_about_mean / amplitude if amplitude > 0 else float("nan")

    max_period_dev = _period_deviation(crossings, expected_period_ms, frequency)

    return Measurement(
        duration_s=duration_s,
        samples=len(t),
        sample_rate_hz=sample_rate,
        frequency_hz=frequency,
        cycles=cycles,
        peak_pos_mv=float(y.max()),
        peak_neg_mv=float(y.min()),
        peak_abs_mv=float(np.abs(y).max()),
        pk_pk_mv=pk_pk,
        rms_mv=float(np.sqrt(np.mean(y**2))),
        mean_mv=float(y.mean()),
        rms_ratio=rms_ratio,
        max_period_dev_ms=max_period_dev,
    )


def _period_deviation(crossings: np.ndarray, expected_period_ms: float | None,
                      frequency_hz: float) -> float:
    """Largest deviation of any single cycle from the expected period, in ms.

    Compared against the target period when one is supplied, so the check
    catches a signal that is uniformly at the wrong rate as well as one that
    is merely jittery.
    """
    if crossings is None or len(crossings) < 2:
        return float("nan")
    periods_ms = np.diff(crossings) * 1000.0
    if expected_period_ms is None or not math.isfinite(expected_period_ms):
        if not math.isfinite(frequency_hz) or frequency_hz <= 0:
            return float("nan")
        expected_period_ms = 1000.0 / frequency_hz
    return float(np.abs(periods_ms - expected_period_ms).max())


def _frequency(t: np.ndarray, y: np.ndarray) -> tuple[float, int, np.ndarray | None]:
    """Frequency from rising zero-crossings of the mean-removed signal.

    Crossing times are linearly interpolated between samples, so the estimate
    is not limited by the sample interval. Measuring first-to-last crossing
    over many cycles averages out per-cycle jitter. The crossing times are
    returned so per-cycle timing can be assessed separately.
    """
    centred = y - y.mean()

    # A flat or near-flat trace has no meaningful frequency; the FFT would
    # just return the largest noise bin, so report it as undefined instead.
    if np.ptp(y) < 1e-9:
        return float("nan"), 0, None

    idx = np.where((centred[:-1] <= 0) & (centred[1:] > 0))[0]

    if len(idx) < 2:
        return _frequency_fft(t, y), 0, None

    frac = -centred[idx] / (centred[idx + 1] - centred[idx])
    crossings = t[idx] + frac * (t[idx + 1] - t[idx])
    cycles = len(crossings) - 1
    return float(cycles / (crossings[-1] - crossings[0])), cycles, crossings


def _frequency_fft(t: np.ndarray, y: np.ndarray) -> float:
    """Fallback for signals with too few zero-crossings: dominant FFT bin."""
    centred = y - y.mean()
    if len(centred) < 4:
        return float("nan")
    sample_rate = (len(t) - 1) / (t[-1] - t[0])
    spectrum = np.abs(np.fft.rfft(centred * np.hanning(len(centred))))
    freqs = np.fft.rfftfreq(len(centred), 1 / sample_rate)
    return float(freqs[np.argmax(spectrum[1:]) + 1])  # skip DC bin


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


class FormatError(Exception):
    """Raised when a file does not match the expected batch recording format."""


def read_recording(path: Path) -> tuple[Recording, list[str]]:
    """Read and validate one file.

    Returns the parsed recording plus any non-blocking warnings.
    Raises FormatError with a specific message if the file is unusable.
    """
    warnings: list[str] = []

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            header_block = [fh.readline() for _ in range(N_META_LINES + 1)]
    except OSError as exc:
        raise FormatError(f"could not be opened ({exc.strerror})") from exc
    except UnicodeDecodeError:
        raise FormatError("is not a readable text file (binary or wrong encoding)")

    if any(line == "" for line in header_block):
        raise FormatError(
            f"has fewer than {N_META_LINES + 1} lines, so it cannot contain "
            "the 4 metadata lines plus a column header"
        )

    subject, timestamp, channel_line, config_line, column_line = (
        line.strip() for line in header_block
    )

    # The column header must name a time column then a signal column.
    columns = [c.strip().lower() for c in column_line.split(",")]
    if len(columns) < 2 or "time" not in columns[0] or "signal" not in columns[1]:
        raise FormatError(
            f"has an unexpected column header on line {N_META_LINES + 1}: "
            f"{column_line!r}. Expected it to start with Time(ms),Signal(mV)"
        )

    channels = "-".join(p for p in channel_line.split(",") if p.strip())
    if not channels:
        warnings.append(f"{path.name}: no channel labels on line 3; using the filename")

    # Last field of the config line is the sample rate in Hz.
    try:
        declared_rate = float(config_line.split(",")[-1])
    except ValueError:
        declared_rate = None
        warnings.append(f"{path.name}: no sample rate found on line 4")

    try:
        raw = pd.read_csv(
            path,
            skiprows=N_META_LINES,
            usecols=[0, 1],
            names=["time_ms", "signal_mv"],
            header=0,
        )
    except Exception as exc:
        raise FormatError(f"could not be parsed as CSV ({exc})") from exc

    data = raw.apply(pd.to_numeric, errors="coerce")
    bad_rows = int(data.isna().any(axis=1).sum())
    data = data.dropna()

    if data.empty:
        raise FormatError("contains no numeric sample rows below the header")
    if len(data) < 2:
        raise FormatError(
            f"contains only {len(data)} numeric sample row; at least 2 are needed"
        )
    if bad_rows:
        warnings.append(
            f"{path.name}: {bad_rows} non-numeric row(s) skipped out of {len(raw)}"
        )

    time = data["time_ms"].to_numpy(dtype=float)
    if np.any(np.diff(time) <= 0):
        raise FormatError(
            "has a time column that does not increase throughout; "
            "the file may be corrupt or rows may be out of order"
        )

    # Cross-check the declared sample rate against the actual time step.
    if declared_rate:
        actual_rate = (len(time) - 1) / ((time[-1] - time[0]) / 1000.0)
        if abs(actual_rate - declared_rate) / declared_rate > 0.01:
            warnings.append(
                f"{path.name}: header declares {declared_rate:g} Hz but the time "
                f"column implies {actual_rate:.1f} Hz"
            )

    recording = Recording(path, subject, timestamp, channels, declared_rate, data)
    return recording, warnings


def validate_batch(recordings: list[Recording]) -> list[str]:
    """Checks that only make sense across the set of files, not within one."""
    warnings: list[str] = []

    subjects = {r.subject for r in recordings}
    if len(subjects) > 1:
        warnings.append(
            "Files are from more than one subject: " + ", ".join(sorted(subjects))
        )

    labels = [r.label for r in recordings]
    duplicates = {l for l in labels if labels.count(l) > 1}
    if duplicates:
        warnings.append("Duplicate channel labels: " + ", ".join(sorted(duplicates)))

    durations = [r.data["time_ms"].iloc[-1] - r.data["time_ms"].iloc[0]
                 for r in recordings]
    if durations and (max(durations) - min(durations)) / max(durations) > 0.01:
        warnings.append(
            f"Recording lengths differ: {min(durations)/1000:.2f} s to "
            f"{max(durations)/1000:.2f} s"
        )

    return warnings


def write_report(
    pdf_path: Path,
    recordings: list["Recording"],
    measurements: list["Measurement"],
    verdicts: list["Verdict"],
    limits: "Limits",
    warnings: list[str],
    run_time: datetime,
    plot_figure: Figure,
    window_ms: float | None,
) -> None:
    """Write a two-page PDF: summary and results table, then the plots."""
    n_failed = sum(1 for v in verdicts if not v.passed)
    overall = "PASS" if n_failed == 0 else "FAIL"

    with PdfPages(pdf_path) as pdf:
        pdf.savefig(_summary_page(
            recordings, measurements, verdicts, limits, warnings,
            run_time, overall, n_failed,
        ))

        # Re-title the on-screen figure so the printed page carries the verdict,
        # then restore it afterwards so the display is unchanged.
        original = plot_figure._suptitle.get_text() if plot_figure._suptitle else ""
        plot_figure.suptitle(
            f"{recordings[0].subject}    {overall}    "
            f"{run_time.strftime('%Y-%m-%d %H:%M:%S')}",
            fontsize=11,
        )
        # State the plotted span, so a windowed view is never mistaken for
        # the full recording that the measurements were taken over.
        span = (f"Showing the first {window_ms:g} ms of each recording. "
                if window_ms is not None else "Showing the full recording. ")
        caption = plot_figure.text(
            0.5, 0.005,
            span + "Shaded bands are the acceptable peak range.",
            fontsize=7, color="grey", ha="center",
        )
        pdf.savefig(plot_figure)
        caption.remove()
        plot_figure.suptitle(original, fontsize=12)

        meta = pdf.infodict()
        meta["Title"] = f"{APP_TITLE} report - {overall}"
        meta["Subject"] = f"Batch check of {len(recordings)} channels"
        meta["Creator"] = f"{APP_TITLE} v{APP_VERSION}"
        meta["CreationDate"] = run_time


def _summary_page(recordings, measurements, verdicts, limits, warnings,
                  run_time, overall, n_failed) -> Figure:
    """Build the first page: header, limits, results table, explanations."""
    fig = Figure(figsize=(11.69, 8.27))  # A4 landscape

    fig.text(0.05, 0.95, APP_TITLE, fontsize=16, fontweight="bold", va="top")

    header_lines = [
        f"Run:       {run_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Subject:   {recordings[0].subject}",
        f"Recorded:  {recordings[0].timestamp}",
        f"Software:  {APP_TITLE} v{APP_VERSION}",
    ]
    fig.text(0.05, 0.88, "\n".join(header_lines), fontsize=9,
             family="monospace", va="top")

    lo_f, hi_f = limits.freq_range
    lo_p, hi_p = limits.pkpk_range
    limit_lines = [
        f"Acceptance limits (fixed by software version {APP_VERSION})",
        f"  Waveform type:   {limits.waveform}",
        f"  Frequency:       {limits.target_freq_hz:g} +/- {limits.freq_tol_hz:g} Hz"
        f"  ({lo_f:.3f} to {hi_f:.3f})",
        f"  Peak-to-peak:    {limits.target_pkpk_mv:g} "
        f"+/- {limits.amplitude_tol_mv:g} mV"
        f"  ({lo_p:.3f} to {hi_p:.3f})",
        f"  Cycle timing:    within {limits.time_tol_ms:g} ms of "
        f"{limits.expected_period_ms:.2f} ms",
    ]
    fig.text(0.48, 0.88, "\n".join(limit_lines), fontsize=9,
             family="monospace", va="top")

    banner = (f"all {len(verdicts)} channels within limits" if overall == "PASS"
              else f"{n_failed} of {len(verdicts)} channels outside limits")
    fig.text(0.05, 0.70, f"  {overall}  -  {banner}  ",
             fontsize=14, fontweight="bold", va="center",
             bbox=dict(facecolor=PASS_COLOUR if overall == "PASS" else FAIL_COLOUR,
                       edgecolor="grey", pad=8))

    ax = fig.add_axes([0.05, 0.40, 0.90, 0.22])
    ax.axis("off")

    col_labels = ["Result", "Channel", "Time (s)", "Freq (Hz)",
                  "Pk-Pk (mV)", "Shape", "Max cycle dev (ms)"]
    rows, colours = [], []
    for rec, m, v in zip(recordings, measurements, verdicts):
        rows.append([
            v.text, rec.label, f"{m.duration_s:.3f}", f"{m.frequency_hz:.3f}",
            f"{m.pk_pk_mv:.3f}", identify_waveform(m.rms_ratio),
            f"{m.max_period_dev_ms:.3f}",
        ])
        colours.append([PASS_COLOUR if v.passed else FAIL_COLOUR] * len(col_labels))

    table = ax.table(cellText=rows, colLabels=col_labels, cellColours=colours,
                     loc="upper center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.5)
    for i in range(len(col_labels)):
        table[0, i].set_text_props(fontweight="bold")
        table[0, i].set_facecolor("#e0e0e0")

    notes = ["Explanation"]
    for rec, v in zip(recordings, verdicts):
        notes.append(f"  {rec.label}: {v.explanation}")
    if warnings:
        notes += ["", "Warnings"] + [f"  {w}" for w in warnings]

    fig.text(0.05, 0.35, "\n".join(notes), fontsize=8, family="monospace", va="top")
    fig.text(0.05, 0.03,
             "Measurements are taken over the full recording. Frequency is derived from "
             "interpolated rising zero-crossings of the mean-removed signal.",
             fontsize=7, color="grey")
    return fig


def next_report_path(results_dir: Path, run_time: datetime) -> Path:
    """Timestamped filename, with a numeric suffix if that second is taken."""
    stem = run_time.strftime(PDF_TIMESTAMP_FORMAT)
    candidate = results_dir / f"{stem}.pdf"
    counter = 2
    while candidate.exists():
        candidate = results_dir / f"{stem}_{counter}.pdf"
        counter += 1
    return candidate



class SignalViewer(ttk.Frame):
    def __init__(self, master: Tk):
        super().__init__(master, padding=10)
        self.grid(row=0, column=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)

        self.folder: Path | None = None
        self.folder_var = StringVar(value="No folder selected")
        self.status_var = StringVar(value="Select a folder containing the batch CSV files.")
        self.window_var = StringVar(value=DEFAULT_WINDOW_MS)
        self.limits = configured_limits()   # fixed; see the constants above

        self._build_header()
        self._build_controls()
        self._build_canvas()
        self._build_table()

        self.rowconfigure(5, weight=1)   # the plot area absorbs spare height
        self.columnconfigure(0, weight=1)

    def _build_header(self) -> None:
        """Application title and a short description of what the tool does."""
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header, text=APP_TITLE, font=("TkDefaultFont", 14, "bold")
        ).grid(row=0, column=0, sticky="w")

        self.description_label = ttk.Label(
            header, text=APP_DESCRIPTION, foreground="grey", justify="left"
        )
        self.description_label.grid(row=1, column=0, sticky="w", pady=(2, 0))

        # Re-wrap the description to match the window width as it is resized,
        # otherwise a long line is simply clipped on a narrow window.
        header.bind(
            "<Configure>",
            lambda e: self.description_label.configure(wraplength=max(e.width - 20, 200)),
        )

        ttk.Separator(header, orient="horizontal").grid(
            row=2, column=0, sticky="ew", pady=(8, 0)
        )

    def _build_controls(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        bar.columnconfigure(1, weight=1)

        ttk.Button(bar, text="Open folder...", command=self.choose_folder).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Label(bar, textvariable=self.folder_var, relief="sunken", padding=4).grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Label(bar, text="Window (ms):").grid(row=0, column=2, padx=(8, 4))
        ttk.Entry(bar, textvariable=self.window_var, width=8).grid(row=0, column=3)

        self.check_button = ttk.Button(
            bar, text="Check", command=self.run_check, state="disabled"
        )
        self.check_button.grid(row=0, column=4, padx=(8, 0))

        L = self.limits
        panel = ttk.LabelFrame(
            self, text=f"Acceptance limits - fixed by software version {APP_VERSION}",
            padding=8,
        )
        panel.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        # Displayed, not editable. Field order and wording follow the recording
        # software's settings panel so the two can be compared at a glance.
        rows = [
            ("Waveform type:", L.waveform),
            ("Frequency (Hz):", f"{L.target_freq_hz:g}"),
            ("Peak-to-peak (mV):", f"{L.target_pkpk_mv:g}"),
            ("Permitted time tolerance (ms):", f"{L.time_tol_ms:g}"),
            ("Permitted amplitude tolerance (mV):", f"{L.amplitude_tol_mv:g}"),
            ("Permitted frequency tolerance (Hz):", f"{L.freq_tol_hz:g}"),
        ]
        for row, (label, value) in enumerate(rows):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", pady=1)
            ttk.Label(
                panel, text=value, relief="sunken", padding=(8, 2), width=14,
                anchor="w", background="#f0f0f0",
            ).grid(row=row, column=1, sticky="w", padx=(12, 0), pady=1)

        lo_f, hi_f = L.freq_range
        lo_p, hi_p = L.pkpk_range
        ttk.Label(
            panel,
            text=("These limits cannot be changed here. Changing them requires a new\n"
                  "build, a new version number and re-validation.\n\n"
                  f"Frequency accepted:     {lo_f:.3f} to {hi_f:.3f} Hz\n"
                  f"Peak-to-peak accepted:  {lo_p:.3f} to {hi_p:.3f} mV\n"
                  f"Cycle period:           {L.expected_period_ms:.2f} ms "
                  f"+/- {L.time_tol_ms:g} ms\n\n"
                  "Shape is confirmed from the ratio of RMS to peak amplitude."),
            foreground="grey", justify="left", font=("TkDefaultFont", 8),
        ).grid(row=0, column=2, rowspan=6, sticky="nw", padx=(30, 0))

        self.verdict_label = ttk.Label(
            self, text="", padding=8, anchor="center", font=("TkDefaultFont", 11, "bold")
        )
        self.verdict_label.grid(row=3, column=0, sticky="ew", pady=(8, 0))

        ttk.Label(self, textvariable=self.status_var, padding=(0, 6)).grid(
            row=4, column=0, sticky="w"
        )

    def _build_canvas(self) -> None:
        holder = ttk.Frame(self)
        holder.grid(row=5, column=0, sticky="nsew")
        holder.rowconfigure(1, weight=1)
        holder.columnconfigure(0, weight=1)

        self.figure = Figure(figsize=(10, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.figure, master=holder)

        toolbar_frame = ttk.Frame(holder)
        toolbar_frame.grid(row=0, column=0, sticky="ew")
        NavigationToolbar2Tk(self.canvas, toolbar_frame).update()

        self.canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew")
        self.canvas.draw()

    def _build_table(self) -> None:
        """Measurement table. Values are for the full trace, not the plotted window."""
        frame = ttk.Frame(self)
        frame.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)

        columns = [
            ("result", "Result", 70),
            ("channel", "Channel", 80),
            ("duration", "Total time (s)", 95),
            ("frequency", "Frequency (Hz)", 105),
            ("pk_pk", "Pk-Pk (mV)", 90),
            ("shape", "Shape", 80),
            ("timing", "Max cycle dev (ms)", 120),
            ("detail", "Explanation", 380),
        ]
        self.table = ttk.Treeview(
            frame, columns=[c[0] for c in columns], show="headings", height=5
        )
        for key, heading, width in columns:
            self.table.heading(key, text=heading)
            self.table.column(key, width=width, anchor="e", stretch=False)
        self.table.column("channel", anchor="w")
        self.table.column("result", anchor="center")
        self.table.column("detail", anchor="w", stretch=True)

        # Colour is a secondary cue only; the Result column always says PASS or FAIL.
        self.table.tag_configure("pass", background=PASS_COLOUR)
        self.table.tag_configure("fail", background=FAIL_COLOUR)

        self.table.grid(row=0, column=0, sticky="ew")

        ttk.Label(
            frame,
            text="Measurements are taken over the full recording, "
                 "independent of the plotted window.",
            foreground="grey",
            padding=(2, 4),
        ).grid(row=1, column=0, sticky="w")

    def _fill_table(self, recordings: list[Recording],
                    measurements: list[Measurement],
                    verdicts: list[Verdict]) -> None:
        self.table.delete(*self.table.get_children())
        for rec, m, v in zip(recordings, measurements, verdicts):
            self.table.insert("", "end", tags=("pass" if v.passed else "fail",), values=(
                v.text,
                rec.label,
                f"{m.duration_s:.3f}",
                f"{m.frequency_hz:.3f}",
                f"{m.pk_pk_mv:.3f}",
                identify_waveform(m.rms_ratio),
                f"{m.max_period_dev_ms:.3f}",
                v.explanation,
            ))

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Select folder containing batch CSV files")
        if not selected:
            return

        self.folder = Path(selected)
        self.folder_var.set(str(self.folder))
        self._clear_results()

        count = len(self._csv_paths())
        self.check_button.state(["!disabled"] if count else ["disabled"])
        if count == EXPECTED_FILES:
            self.status_var.set(f"Found {count} CSV files. Press Check to plot.")
        elif count:
            self.status_var.set(
                f"Found {count} CSV file(s) - expected {EXPECTED_FILES}. "
                "Press Check for details."
            )
        else:
            self.status_var.set("No CSV files in that folder.")

    def _save_report(self, recordings, measurements, verdicts, limits,
                     warnings, run_time, window_ms) -> Path | None:
        """Write the PDF report. Returns the path, or None if it could not be saved.

        A failure here must not discard the check results, which are already on
        screen, so problems are reported without aborting.
        """
        results_dir = self.folder / RESULTS_DIR_NAME
        try:
            results_dir.mkdir(exist_ok=True)
        except OSError as exc:
            messagebox.showwarning(
                "Report not saved",
                f"Could not create the results folder:\n{results_dir}\n\n"
                f"{exc.strerror}\n\nThe results on screen are still valid.",
            )
            return None

        pdf_path = next_report_path(results_dir, run_time)
        try:
            write_report(
                pdf_path, recordings, measurements, verdicts, limits,
                warnings, run_time, self.figure, window_ms,
            )
        except PermissionError:
            messagebox.showwarning(
                "Report not saved",
                f"Could not write:\n{pdf_path}\n\nThe file may be open in a PDF "
                "viewer. Close it and press Check again.\n\n"
                "The results on screen are still valid.",
            )
            return None
        except Exception as exc:
            messagebox.showwarning(
                "Report not saved",
                f"Could not write the PDF report:\n{exc}\n\n"
                "The results on screen are still valid.",
            )
            return None

        self.canvas.draw()  # restore the on-screen title after the export
        return pdf_path

    def _set_verdict_banner(self, verdicts: list[Verdict]) -> None:
        failed = [v for v in verdicts if not v.passed]
        total = len(verdicts)
        if failed:
            self.verdict_label.configure(
                text=f"FAIL  -  {len(failed)} of {total} channels outside limits",
                background=FAIL_COLOUR,
            )
        else:
            self.verdict_label.configure(
                text=f"PASS  -  all {total} channels within limits",
                background=PASS_COLOUR,
            )

    def _clear_results(self) -> None:
        """Blank the table, banner and plots so nothing stale survives a failed check."""
        self.verdict_label.configure(text="", background="")
        self.table.delete(*self.table.get_children())
        self.figure.clear()
        self.canvas.draw()

    def _csv_paths(self) -> list[Path]:
        if not self.folder:
            return []
        return sorted(p for p in self.folder.glob("*.csv") if p.is_file())

    def _window_ms(self) -> float | None:
        """Parse the window box; blank means plot the full trace."""
        raw = self.window_var.get().strip()
        if not raw:
            return None
        value = float(raw)  # ValueError handled by the caller
        if value <= 0:
            raise ValueError("window must be greater than zero")
        return value

    def run_check(self) -> None:
        if not self.folder:
            return

        try:
            window_ms = self._window_ms()
        except ValueError:
            messagebox.showerror(
                "Invalid window",
                "Window (ms) must be a positive number, or blank to show the full trace.",
            )
            return

        limits = self.limits

        run_time = datetime.now()
        paths = self._csv_paths()

        # Clear previous results up front, so a failed check can never leave
        # stale plots or measurements on screen next to a new error message.
        self._clear_results()

        # Check 1: the folder must hold exactly the expected number of files.
        if len(paths) != EXPECTED_FILES:
            found = "\n".join(f"  - {p.name}" for p in paths) if paths else "  (none)"
            messagebox.showerror(
                "Wrong number of files",
                f"Expected {EXPECTED_FILES} CSV files in this folder, "
                f"but found {len(paths)}.\n\n{self.folder}\n\n{found}\n\n"
                "Check the folder is the right one and that no files are "
                "missing or left over from a previous batch.",
            )
            self.status_var.set(
                f"Stopped: found {len(paths)} files, expected {EXPECTED_FILES}."
            )
            return

        # Check 2: every file must parse and match the expected format.
        recordings, warnings, failures = [], [], []
        for path in paths:
            try:
                recording, file_warnings = read_recording(path)
            except FormatError as exc:
                failures.append(f"  - {path.name} {exc}")
            except Exception as exc:  # unexpected; still name the file
                failures.append(f"  - {path.name}: unexpected error - {exc}")
            else:
                recordings.append(recording)
                warnings.extend(file_warnings)

        if failures:
            messagebox.showerror(
                "Invalid file format",
                f"{len(failures)} of {len(paths)} file(s) could not be read:\n\n"
                + "\n".join(failures)
                + "\n\nNo plots have been produced. Fix or remove the listed "
                "files and try again.",
            )
            self.status_var.set(f"Stopped: {len(failures)} file(s) failed validation.")
            return

        # Check 3: consistency across the batch. Non-blocking.
        warnings.extend(validate_batch(recordings))

        measurements = [measure(rec.data, limits.expected_period_ms)
                        for rec in recordings]
        verdicts = [evaluate(m, limits) for m in measurements]
        self.plot(recordings, measurements, verdicts, limits, window_ms)
        self._fill_table(recordings, measurements, verdicts)
        self._set_verdict_banner(verdicts)

        n_failed = sum(1 for v in verdicts if not v.passed)
        outcome = "PASS" if n_failed == 0 else f"FAIL ({n_failed} channel(s))"

        saved_to = self._save_report(
            recordings, measurements, verdicts, limits, warnings,
            run_time, window_ms,
        )
        saved_note = f" Report saved to {saved_to.name}." if saved_to else ""

        if warnings:
            self.status_var.set(
                f"{outcome}. Plotted {len(recordings)} files with "
                f"{len(warnings)} warning(s).{saved_note}"
            )
            messagebox.showwarning(
                "Check passed with warnings",
                "The plots and measurements below are usable, but note:\n\n"
                + "\n".join(f"  - {w}" for w in warnings),
            )
        else:
            self.status_var.set(
                f"{outcome}. Plotted {len(recordings)} files, "
                f"no format issues found.{saved_note}"
            )

    def plot(self, recordings: list[Recording], measurements: list[Measurement],
             verdicts: list[Verdict], limits: Limits,
             window_ms: float | None) -> None:
        self.figure.clear()

        n = len(recordings)
        ncols = 2 if n > 1 else 1
        nrows = math.ceil(n / ncols)
        axes = self.figure.subplots(nrows, ncols, squeeze=False).ravel()

        pkpk_lo, pkpk_hi = limits.pkpk_range

        for ax, rec, m, v in zip(axes, recordings, measurements, verdicts):
            df = rec.data
            if window_ms is not None:
                df = df[df["time_ms"] <= window_ms]

            # Shade the acceptable peak bands, positioned about this channel's
            # own mean so the check remains one of amplitude, not offset.
            centre = rec.data["signal_mv"].mean()
            ax.axhspan(centre + pkpk_lo / 2, centre + pkpk_hi / 2,
                       color="green", alpha=0.10)
            ax.axhspan(centre - pkpk_hi / 2, centre - pkpk_lo / 2,
                       color="green", alpha=0.10)

            ax.plot(df["time_ms"], df["signal_mv"], linewidth=0.8, color="#1f77b4")
            ax.set_title(
                f"{v.text}  {rec.label}   {m.frequency_hz:.2f} Hz   "
                f"{m.pk_pk_mv:.2f} mV pk-pk   {identify_waveform(m.rms_ratio)}",
                fontsize=9,
                color="#1a7f1a" if v.passed else "#c26a00",
                fontweight="bold",
            )
            ax.set_xlabel("Time (ms)")
            ax.set_ylabel("Signal (mV)")
            ax.grid(True, alpha=0.3)
            ax.axhline(0, color="grey", linewidth=0.5)

        # Blank any unused panes when the file count doesn't fill the grid.
        for ax in axes[n:]:
            ax.axis("off")

        self.figure.suptitle(recordings[0].subject, fontsize=12)
        self.figure.tight_layout()
        self.canvas.draw()


def main() -> None:
    # Fail immediately and visibly if the fixed limits have been edited into
    # an unusable state, rather than partway through a check.
    configured_limits()

    root = Tk()
    root.title(f"{APP_TITLE}  v{APP_VERSION}")
    root.geometry("1200x950")
    SignalViewer(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A packaged windowed build has no console, so a startup crash would
        # otherwise vanish with no message at all. Show it in a dialog, and
        # fall back to the console when one is available.
        details = traceback.format_exc()
        try:
            import tkinter.messagebox as _mb
            _root = Tk()
            _root.withdraw()
            _mb.showerror(
                f"{APP_TITLE} - startup error",
                "The application could not start.\n\n"
                f"{details}\n\nPlease report this message.",
            )
            _root.destroy()
        except Exception:
            print(details)
            try:
                input("\nPress Enter to close...")
            except (EOFError, RuntimeError):
                pass
