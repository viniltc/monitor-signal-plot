# Software Verification Report — Monitoring Waveform Acceptance Check

## 1. Document Control

| Field | Value |
|---|---|
| Software | Monitoring Waveform Acceptance Check |
| Company | Amber Therapeutics |
| Version reviewed | 1.0 (`APP_VERSION` in [signal_viewer.py](../signal_viewer.py), `FileVersion` 1.0.0.0 in [version_info.txt](../version_info.txt)) |
| File(s) reviewed | `signal_viewer.py` (the application) |
| File(s) out of scope | `plot_batch.py` (a separate, unvalidated developer plotting utility — not part of the acceptance-check application, shares no code with it) |
| Review method | Static code review / requirements traceability. **No automated tests were executed against this version** — see §5. |
| Reviewed by | Vinil T Chackochan (viniltc@gmail.com), with AI-assisted review (Claude) |
| Date | 2026-08-28 |

## 2. Purpose and Scope

This report verifies, by inspection of the source code, that `signal_viewer.py` correctly implements its stated acceptance criteria and data-integrity rules, and behaves safely on invalid input. It is intended as audit evidence that the logic governing PASS/FAIL verdicts matches the documented acceptance limits, and that failure paths do not silently corrupt or misreport results.

This report does **not** constitute dynamic/execution-based testing (no synthetic waveforms were run through the tool). It should be read as a design/code review, and paired with executed test evidence (Approach 1 — an automated test suite) before being relied on as complete verification. See §7.

## 3. Requirements Traceability Matrix

Requirements below are derived from the constants, docstrings, and in-code comments in `signal_viewer.py` itself — there is no separate external requirements document. Each is checked against its implementation.

### 3.1 Acceptance criteria (PASS/FAIL logic)

| ID | Requirement | Implementation | Review verdict |
|---|---|---|---|
| R1 | Accepted waveform shape is triangle only | `LIMIT_WAVEFORM = "triangle"` ([L66](../signal_viewer.py#L66)); shape identified from RMS/peak ratio against `WAVEFORM_RMS_RATIO` ([L76-L80](../signal_viewer.py#L76-L80)), compared in `evaluate()` ([L176-L182](../signal_viewer.py#L176-L182)) | **Verified.** Ratio constants (1/√3 triangle, 1/√2 sine, 1.0 square) match standard waveform RMS formulas. |
| R2 | Frequency target 10.0 Hz ± 0.5 Hz | `LIMIT_FREQUENCY_HZ`/`LIMIT_FREQUENCY_TOLERANCE_HZ` ([L67](../signal_viewer.py#L67), [L71](../signal_viewer.py#L71)); range check in `evaluate()` ([L185-L194](../signal_viewer.py#L185-L194)) | **Verified.** Both above- and below-range and "undetermined" (NaN) cases produce a FAIL with a specific reason. |
| R3 | Peak-to-peak amplitude target 10.0 mV ± 1.0 mV | `LIMIT_PEAK_TO_PEAK_MV`/`LIMIT_AMPLITUDE_TOLERANCE_MV` ([L68](../signal_viewer.py#L68), [L70](../signal_viewer.py#L70)); range check ([L196-L204](../signal_viewer.py#L196-L204)) | **Verified.** |
| R4 | No single cycle period may deviate more than 1.0 ms from the nominal 100 ms period | `LIMIT_TIME_TOLERANCE_MS` ([L69](../signal_viewer.py#L69)); worst-cycle deviation computed in `_period_deviation()` ([L276-L291](../signal_viewer.py#L276-L291)) against every individual cycle (not just an average), checked in `evaluate()` ([L206-L214](../signal_viewer.py#L206-L214)) | **Verified.** Uses interpolated zero-crossing times ([L294-L317](../signal_viewer.py#L294-L317)), so resolution isn't limited to the sample interval. |
| R5 | A PASS requires *all four* checks (shape, frequency, amplitude, timing) to succeed; any failing check must state why | `evaluate()` accumulates a `reasons` list from all four checks before deciding; only returns PASS if the list is empty ([L216-L218](../signal_viewer.py#L216-L218)) | **Verified.** Failing on multiple axes reports all reasons, not just the first. |
| R6 | Acceptance limits must not be editable from the running application; changing them requires a rebuild and version bump | Limits are module-level constants, displayed read-only via `ttk.Label` (not `Entry`) in the UI panel ([L679-L692](../signal_viewer.py#L679-L692)); no code path writes to the `LIMIT_*` constants at runtime | **Verified.** |
| R7 | The application must fail visibly at startup if the fixed limits are internally inconsistent, rather than producing wrong verdicts silently | `configured_limits()` validates the waveform name and that all tolerances are positive numbers, raising `ValueError` ([L127-L157](../signal_viewer.py#L127-L157)); called once in `main()` before the window is created ([L1035](../signal_viewer.py#L1035)) and caught by the top-level handler ([L1044-L1067](../signal_viewer.py#L1044-L1067)) | **Verified.** |

### 3.2 File format and data-integrity checks

| ID | Requirement | Implementation | Review verdict |
|---|---|---|---|
| R8 | A batch must contain exactly 4 CSV files | `EXPECTED_FILES = 4` ([L52](../signal_viewer.py#L52)); checked before any file is parsed ([L912-L924](../signal_viewer.py#L912-L924)) | **Verified.** Wrong count blocks the check entirely and lists what was found. |
| R9 | Each file must have 4 metadata lines followed by a column header naming Time then Signal | `read_recording()` reads `N_META_LINES + 1` lines, rejects short files ([L366-L370](../signal_viewer.py#L366-L370)), and validates the header text ([L376-L382](../signal_viewer.py#L376-L382)) | **Verified.** |
| R10 | Time values must strictly increase (no corrupt/reordered rows) | `if np.any(np.diff(time) <= 0): raise FormatError` ([L422-L426](../signal_viewer.py#L422-L426)) | **Verified.** |
| R11 | The declared sample rate (metadata line 4) should match the actual time step; a mismatch >1% is flagged | Cross-check at [L429-L435](../signal_viewer.py#L429-L435) | **Verified, with one minor observation** — see §6.1. |
| R12 | Non-numeric sample rows are dropped, not silently included, and the operator is told how many | `data.isna().any(axis=1).sum()` counted before `dropna()`, warning appended if any ([L406-L419](../signal_viewer.py#L406-L419)) | **Verified.** |
| R13 | A file that cannot be opened, decoded, or parsed produces a specific, named failure rather than crashing the app | `FormatError` raised with a distinct message for each failure mode (`OSError`, `UnicodeDecodeError`, CSV parse `Exception`) ([L358-L364](../signal_viewer.py#L358-L364), [L403-L404](../signal_viewer.py#L403-L404)); caught per-file in `run_check()` ([L928-L937](../signal_viewer.py#L928-L937)) | **Verified.** |

### 3.3 Batch-level consistency (non-blocking warnings)

| ID | Requirement | Implementation | Review verdict |
|---|---|---|---|
| R14 | All files in a batch should be from the same subject | `validate_batch()` ([L445-L449](../signal_viewer.py#L445-L449)) | **Verified.** Warning only, does not block the check — reasonable, since subject mismatch doesn't invalidate individual channel measurements. |
| R15 | Channel labels should be unique within a batch | [L451-L454](../signal_viewer.py#L451-L454) | **Verified.** |
| R16 | Recording durations across the batch should agree within 1% | [L456-L462](../signal_viewer.py#L456-L462) | **Verified.** |

### 3.4 Result integrity and reporting

| ID | Requirement | Implementation | Review verdict |
|---|---|---|---|
| R17 | Results on screen (plots, table, verdict banner) must be cleared before every new check, so a failed run can never leave stale data next to a new error | `_clear_results()` called unconditionally at the top of `run_check()` ([L909](../signal_viewer.py#L909)), and again from `choose_folder()` | **Verified.** |
| R18 | A PDF report is produced automatically for every completed check, with a unique filename | `_save_report()` → `next_report_path()` increments a numeric suffix on collision ([L592-L600](../signal_viewer.py#L592-L600)) | **Verified.** |
| R19 | If the PDF cannot be written (locked file, folder permissions), the already-displayed results must remain valid and the user must be warned, not blocked | `_save_report()` is called *after* `plot()`/`_fill_table()`/`_set_verdict_banner()` have already run ([L963-L967](../signal_viewer.py#L963-L967)); write failures are caught (`PermissionError`, generic `Exception`) and shown as a non-blocking `messagebox.showwarning` ([L834-L848](../signal_viewer.py#L834-L848)) | **Verified.** |
| R20 | The report must carry software identity and the run timestamp for traceability | PDF metadata (`Title`, `Creator`) set from `APP_TITLE`/`APP_VERSION` ([L509-L513](../signal_viewer.py#L509-L513)); summary page header includes run time, subject, recorded time, software version ([L523-L530](../signal_viewer.py#L523-L530)) | **Verified.** |
| R21 | Measurements shown to the operator are computed on the full recording, never on a truncated/windowed view, even when the plot itself is windowed for readability | `measure()` is called on the untruncated `rec.data` ([L953-L954](../signal_viewer.py#L953-L954)); only `plot()` truncates to `window_ms` for display ([L998-L1000](../signal_viewer.py#L998-L1000)); the PDF and on-screen table both carry an explicit caption stating the plotted span is not the measured span ([L496-L499](../signal_viewer.py#L496-L499), [L765-L771](../signal_viewer.py#L765-L771)) | **Verified.** |

## 4. Code Review Checklist (general robustness)

| Check | Result |
|---|---|
| Application-level exceptions during startup are caught and shown to the user (no silent crash in a windowed build with no console) | Pass — [L1044-L1067](../signal_viewer.py#L1044-L1067) |
| No bare `except:` clauses that could mask unexpected errors | Pass — all `except` clauses name specific exception types or explicitly re-raise/report via `exc` |
| No mutable global state used for the acceptance limits (only read via `configured_limits()`/constants) | Pass |
| File paths and folder creation handle `OSError` explicitly rather than assuming success | Pass — [L818-L826](../signal_viewer.py#L818-L826) |
| No use of `eval`, `exec`, shell invocation, or other injection-prone constructs | Pass |
| Version number (`APP_VERSION`) is co-located with the fixed limits with an explicit comment to keep them in step | Pass — [L38-L64](../signal_viewer.py#L38-L64) |

## 5. What this review does *not* cover

- **No execution/dynamic testing.** No synthetic waveform (known-good, known-bad, boundary-value) was actually run through `measure()`/`evaluate()` to confirm the numeric output is correct — this review confirms the *logic* matches the stated limits, not that the *arithmetic* is bug-free under real data (e.g., correctness of the FFT fallback frequency estimate, or behavior with irregular sample spacing).
- **No UI/usability testing** (see the separate architecture discussion earlier in this session for how the UI is built).
- **No review of the PyInstaller packaging** (`build.bat`, `MonitoringWaveformAcceptanceCheck.spec`) — packaging integrity is out of scope for this pass.

## 6. Observations

### 6.1 Minor — sample-rate cross-check skipped when declared rate is exactly 0
[L429](../signal_viewer.py#L429) uses `if declared_rate:`, which is falsy for `0.0`. A file that declares a sample rate of exactly 0 Hz (rather than an unparsable value) would skip the cross-check silently instead of generating a warning. Very low likelihood in practice (a real recorder would not declare 0 Hz), and it fails safe (skips a warning, not a required check), so no action is required unless the team wants stricter handling.

### 6.2 Recommendation — pair this report with executed tests
This is a static/code-review verification. It is strong evidence the implementation *matches its stated criteria*, but per §5 it is not a substitute for running known-input test vectors through the tool and recording actual output. See §7 for an executed run that partially closes this gap; a synthetic-input pytest suite (ideal signal, boundary-value signals just inside/outside each limit, malformed files) is still recommended for full arithmetic-level coverage of `measure()`/`evaluate()`/`read_recording()`.

## 7. Executed Test Evidence (Dynamic Check)

In addition to the static review above, the acceptance-check logic was executed against a set of real recorded batches using [tools/run_batch_checks.py](../tools/run_batch_checks.py) — a runner that imports and calls the exact same functions used by the application (`read_recording`, `validate_batch`, `measure`, `evaluate`), so the result is evidence about the application's actual behaviour, not a reimplementation.

| Field | Value |
|---|---|
| Date run | 2026-08-28 |
| Batches checked | 9 (one subfolder per batch, each with the expected 4 CSV files) |
| Result | 3 PASS, 6 FAIL, 0 SKIPPED/ERROR |
| Raw per-batch and per-channel output | `results/batch_check_summary.csv`, `results/batch_check_channels.csv` (not committed — derived from identifiable subject data; see `.gitignore`) |

**Findings from this run:**
- Measured peak-to-peak amplitude scaled monotonically and consistently with the source recordings' stated stimulus amplitude across all batches, and measured frequency tracked the stated target frequency (including correctly failing an off-target ~9 Hz batch). This is evidence the frequency and amplitude measurement logic (§3.1, R2–R3) produces sensible, consistent results on real hardware output, not just on the constants it was reviewed against.
- Two batches contained individual channels with a large single-cycle timing deviation (tens of ms, against sibling channels in the same batch measuring a normal ~0.14 ms) — correctly flagged as FAIL by R4/R16. Because the deviation appears on isolated channels within an otherwise-clean batch (same code, same run), this looks like a genuine recording-level artifact rather than a defect in the timing-deviation logic itself, but the underlying recordings should be reviewed by someone familiar with the acquisition hardware to confirm the cause.
- No file in this run triggered `FormatError` or was skipped for the wrong file count, so this run does not add coverage of the file-format failure paths (R9–R13) — those remain verified by static review only (§3.2).

## 8. Conclusion

Based on static code review, all identified acceptance-criteria and data-integrity requirements (R1–R21) in `signal_viewer.py` v1.0 are correctly implemented as stated in the code's own constants and comments. One low-risk, fail-safe observation was noted (§6.1); no defects were found that would cause an incorrect PASS/FAIL verdict. An executed run against real data (§7) corroborates the amplitude and frequency measurement logic and demonstrates the timing check correctly detects a real-world anomaly; file-format failure paths remain verified by static review only.

| Role | Name | Signature | Date |
|---|---|---|---|
| Reviewer | | | |
| Approver | | | |
