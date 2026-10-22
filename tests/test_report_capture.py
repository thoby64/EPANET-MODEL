# tests/test_report_capture.py
"""
The EPANET report must survive even when EPANET cannot write its report file.

EPANET writes ``<inp>.rpt`` next to the .inp. When it cannot write that file
it prints the whole report to stdout instead and no file is created — this is
what happened on the deployed service, which made "View report" and
"Download report" fail with a 404.

The service therefore captures stdout while EPANET runs and uses it as the
fallback source for the report text.
"""

import pytest

from app.services.simulation_service import (
    _read_report_file,
    _report_text_from_stdout,
    _StdoutCapture,
)

pytest.importorskip("epanet_plus", reason="epyt-flow is not installed")
from epanet_plus.epanet_toolkit import EPyT  # noqa: E402

# A trimmed but faithful excerpt of what EPANET prints when it falls back to
# stdout: the page header, the flow balance and the closing line.
EPANET_REPORT_SAMPLE = """  Page 1                                    Thu Oct 22 11:57:04 2026
  Hydraulic Flow Balance (m3/h)
  ================================
  Total Inflow:           151.414
  Consumer Demand:          3.548
  ================================

  Analysis ended Thu Oct 22 11:57:04 2026
"""


def test_stdout_capture_grabs_c_level_output(tmp_path):
    """The capture must catch what the C library prints, not just Python.

    EPANET's ``printf`` writes to file descriptor 1 directly, so the write
    below is done with ``os.write`` rather than ``print`` — pytest replaces
    ``sys.stdout`` with its own object, which would hide the difference.
    """
    import os

    inp = str(tmp_path / "net.inp")
    with open(inp, "w", encoding="utf-8") as fh:
        fh.write("[JUNCTIONS]\n J1 0\n\n[END]\n")

    with _StdoutCapture() as cap:
        with EPyT(inp):
            os.write(1, b"HELLO-FROM-C-LEVEL-PRINTF\n")

    assert "HELLO-FROM-C-LEVEL-PRINTF" in cap.text, (
        "the stdout capture did not see the C-level output"
    )


def test_captured_output_is_replayed_to_stdout(capfd):
    """The capture must not swallow the service log."""
    with _StdoutCapture():
        print("logged-line-inside-the-capture")
    out = capfd.readouterr().out
    assert "logged-line-inside-the-capture" in out


def test_report_text_is_picked_out_of_the_captured_output():
    text = _report_text_from_stdout(
        "some log line\n" + EPANET_REPORT_SAMPLE + "trailing noise\n"
    )
    assert "Hydraulic Flow Balance" in text
    assert "Analysis ended" in text
    assert not text.startswith("some log line")


def test_report_text_from_stdout_accepts_bytes():
    text = _report_text_from_stdout(EPANET_REPORT_SAMPLE.encode("utf-8"))
    assert "Hydraulic Flow Balance" in text


def test_report_text_from_stdout_ignores_empty_input():
    assert _report_text_from_stdout("") == ""
    assert _report_text_from_stdout(b"") == ""
    assert _report_text_from_stdout(None) == ""


def test_read_report_file_returns_empty_when_missing(tmp_path):
    assert _read_report_file(str(tmp_path / "nothing.inp")) == ""


def test_read_report_file_reads_the_file(tmp_path):
    inp = str(tmp_path / "net.inp")
    with open(inp + ".rpt", "w", encoding="utf-8") as fh:
        fh.write(EPANET_REPORT_SAMPLE)
    assert "Flow Balance" in _read_report_file(inp)
