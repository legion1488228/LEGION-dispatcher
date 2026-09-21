from datetime import date
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import _period_bounds


def test_first_half_bounds():
    assert _period_bounds(date(2026, 9, 1)) == (date(2026, 9, 1), date(2026, 9, 15))
    assert _period_bounds(date(2026, 9, 15)) == (date(2026, 9, 1), date(2026, 9, 15))


def test_second_half_bounds():
    assert _period_bounds(date(2026, 9, 16)) == (date(2026, 9, 16), date(2026, 9, 30))
    assert _period_bounds(date(2026, 2, 28)) == (date(2026, 2, 16), date(2026, 2, 28))
