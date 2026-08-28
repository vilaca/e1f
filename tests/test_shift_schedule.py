"""Focused mutation contract for frozen seasonal shift completeness."""

from e1f.experimental import seasonality


def test_shift_schedule_refusal_reports_every_and_only_incomplete_year() -> None:
    refusal = seasonality._shift_schedule_refusal(
        {0: 8, 1: 11, 2: 8, 3: 7},
        {0: 2023, 1: 2023, 2: 2024, 3: 2025},
        8,
        11,
    )
    assert refusal == seasonality.ShiftScheduleRefusal(8, 11, (2024,))


def test_shift_schedule_refusal_accepts_complete_schedule() -> None:
    assert seasonality._shift_schedule_refusal(
        {0: 8, 1: 11},
        {0: 2024, 1: 2024},
        8,
        11,
    ) is None
