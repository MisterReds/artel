from pathlib import Path


def test_summary_present():
    assert (Path(__file__).parent / "SUMMARY.md").stat().st_size > 0
