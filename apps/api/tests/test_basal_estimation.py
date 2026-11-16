"""Tests for the shared temp-basal/profile delivery estimate."""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.models.pump_profile import SEGMENT_UNKNOWN_AS_NULL_KEY
from src.services.basal_estimation import (
    BasalEvent,
    BasalEventKind,
    estimate_basal_delivery,
)


def _segment(start_minutes: int, rate: float | None) -> dict:
    return {
        "start_minutes": start_minutes,
        "basal_rate": rate,
        SEGMENT_UNKNOWN_AS_NULL_KEY: True,
    }


def test_temp_duration_uses_scheduled_rate_for_uncovered_time():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end = start + timedelta(minutes=40)

    estimate = estimate_basal_delivery(
        [
            BasalEvent(start, rate=3.0, duration_minutes=2),
            BasalEvent(end, rate=1.0),
        ],
        start,
        end,
        profile_segments=[_segment(0, 0.8)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(0.6066666667)


@pytest.mark.parametrize("profile_segments", [None, [], [_segment(0, None)]])
def test_unknown_scheduled_rate_is_not_treated_as_zero(profile_segments):
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end = start + timedelta(minutes=40)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=3.0, duration_minutes=2)],
        start,
        end,
        profile_segments=profile_segments,
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is False
    assert estimate.units == pytest.approx(0.1)


def test_real_zero_scheduled_window_is_known_zero():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end = start + timedelta(minutes=40)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=3.0, duration_minutes=2)],
        start,
        end,
        profile_segments=[_segment(0, 0.0)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(0.1)


def test_scheduled_rate_changes_inside_uncovered_time():
    start = datetime(2026, 1, 1, 9, 50, tzinfo=UTC)
    end = start + timedelta(minutes=20)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=3.0, duration_minutes=2)],
        start,
        end,
        profile_segments=[_segment(0, 0.5), _segment(10 * 60, 1.0)],
        legacy_max_gap_hours=2.0,
    )

    # 2 min at 3.0 + 8 min at 0.5 + 10 min at 1.0.
    assert estimate.complete is True
    assert estimate.units == pytest.approx(1 / 3)


def test_scheduled_fill_uses_full_uncovered_interval():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end = start + timedelta(hours=6)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=3.0, duration_minutes=2)],
        start,
        end,
        profile_segments=[_segment(0, 0.8)],
        legacy_max_gap_hours=2.0,
    )

    # The legacy cap applies only to durationless event rates. A known profile
    # schedule fills all 5 h 58 min after the temp ends.
    assert estimate.complete is True
    assert estimate.units == pytest.approx(4.8733333333)


def test_out_of_range_scheduled_rate_is_unknown_not_clamped():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end = start + timedelta(minutes=40)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=3.0, duration_minutes=2)],
        start,
        end,
        profile_segments=[_segment(0, 1000.0)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is False
    assert estimate.units == pytest.approx(0.1)


@pytest.mark.parametrize("durations", [(30, 2), (2, 30)])
def test_same_timestamp_events_prefer_shorter_duration(durations):
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end = start + timedelta(minutes=40)

    estimate = estimate_basal_delivery(
        [
            *(BasalEvent(start, rate=3.0, duration_minutes=d) for d in durations),
            BasalEvent(end, rate=1.0),
        ],
        start,
        end,
        profile_segments=[_segment(0, 0.8)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(0.6066666667)


def test_durationless_source_keeps_legacy_result():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end = start + timedelta(minutes=40)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=3.0)],
        start,
        end,
        profile_segments=[_segment(0, 0.8)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(2.0)


def test_profile_lookup_uses_local_time_of_day():
    # 15:00 UTC is 10:00 in New York in January. The UTC schedule rate is
    # deliberately different so this catches UTC-clock profile lookups.
    start = datetime(2026, 1, 1, 15, tzinfo=UTC)
    end = start + timedelta(minutes=40)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=3.0, duration_minutes=2)],
        start,
        end,
        profile_segments=[_segment(0, 2.0), _segment(10 * 60, 0.8)],
        profile_timezone=ZoneInfo("America/New_York"),
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(0.6066666667)


def test_profile_change_in_spring_forward_gap_uses_transition_instant():
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 3, 8, 6, 50, tzinfo=UTC)  # 01:50 local
    end = datetime(2026, 3, 8, 7, 10, tzinfo=UTC)  # 03:10 local

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=3.0, duration_minutes=2)],
        start,
        end,
        profile_segments=[_segment(0, 0.5), _segment(2 * 60 + 30, 1.0)],
        profile_timezone=zone,
        legacy_max_gap_hours=2.0,
    )

    # 2 min at 3.0, 8 min at 0.5, then 10 min at 1.0 from the 03:00 jump.
    assert estimate.complete is True
    assert estimate.units == pytest.approx(1 / 3)


def test_scheduled_fill_splits_at_fall_back_transition():
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT
    end = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 EST

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=1.0, duration_minutes=0)],
        start,
        end,
        profile_segments=[_segment(0, 0.5), _segment(90, 1.0)],
        profile_timezone=zone,
        legacy_max_gap_hours=2.0,
    )

    # 30 min at the 01:30 EDT rate, then 30 min at the repeated 01:00 rate.
    assert estimate.complete is True
    assert estimate.units == pytest.approx(0.75)


def test_overlapping_temp_is_truncated_by_next_temp():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [
            BasalEvent(start, rate=2.0, duration_minutes=30),
            BasalEvent(start + timedelta(minutes=10), rate=1.0, duration_minutes=10),
        ],
        start,
        start + timedelta(minutes=20),
        profile_segments=[_segment(0, 0.5)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(2.0 * 10 / 60 + 1.0 * 10 / 60)


def test_temp_is_truncated_by_next_durationless_basal():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [
            BasalEvent(start, rate=2.0, duration_minutes=30),
            BasalEvent(start + timedelta(minutes=5), rate=0.8),
        ],
        start,
        start + timedelta(minutes=15),
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(2.0 * 5 / 60 + 0.8 * 10 / 60)


def test_seed_temp_crossing_window_start_uses_remaining_duration():
    window_start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [BasalEvent(window_start - timedelta(minutes=5), 2.0, 10)],
        window_start,
        window_start + timedelta(minutes=15),
        profile_segments=[_segment(0, 0.6)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(2.0 * 5 / 60 + 0.6 * 10 / 60)


@pytest.mark.parametrize(
    ("gap_seconds", "expected_complete"),
    [(59, True), (60, False), (10 * 60, False)],
)
def test_leading_gap_threshold_without_profile_fill(gap_seconds, expected_complete):
    window_start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [BasalEvent(window_start + timedelta(seconds=gap_seconds), 1.0)],
        window_start,
        window_start + timedelta(minutes=30),
        profile_segments=[_segment(0, 0.6)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.evaluated is True
    assert estimate.complete is expected_complete
    assert estimate.units == pytest.approx((30 * 60 - gap_seconds) / 3600)


def test_pre_window_seed_avoids_leading_gap():
    window_start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [BasalEvent(window_start - timedelta(minutes=10), 1.0)],
        window_start,
        window_start + timedelta(minutes=30),
        legacy_max_gap_hours=2.0,
    )

    assert estimate.evaluated is True
    assert estimate.complete is True
    assert estimate.units == pytest.approx(0.5)


def test_empty_window_is_not_evaluated():
    window_start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [],
        window_start,
        window_start + timedelta(minutes=30),
        legacy_max_gap_hours=2.0,
    )

    assert estimate.evaluated is False
    assert estimate.complete is True
    assert estimate.units == 0


def test_expired_unknown_seed_does_not_mark_window_incomplete():
    window_start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [
            BasalEvent(
                window_start - timedelta(minutes=30),
                None,
                10,
                kind=BasalEventKind.UNKNOWN_RATE,
            )
        ],
        window_start,
        window_start + timedelta(minutes=20),
        profile_segments=[_segment(0, 0.6)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(0.6 * 20 / 60)


def test_window_end_truncates_temp_duration():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, rate=2.0, duration_minutes=30)],
        start,
        start + timedelta(minutes=5),
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(2.0 * 5 / 60)


def test_suspend_example_overrides_running_temp_until_next_temp():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [
            BasalEvent(start, rate=2.0, duration_minutes=30),
            BasalEvent(
                start + timedelta(minutes=5),
                rate=0.0,
                duration_minutes=30,
                kind=BasalEventKind.SUSPEND,
            ),
            BasalEvent(start + timedelta(minutes=35), rate=1.0),
        ],
        start,
        start + timedelta(minutes=35),
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(2.0 * 5 / 60)


def test_unbounded_suspend_ending_at_resume_is_complete():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [
            BasalEvent(start, None, kind=BasalEventKind.SUSPEND),
            BasalEvent(
                start + timedelta(minutes=10),
                None,
                kind=BasalEventKind.RESUME,
            ),
        ],
        start,
        start + timedelta(minutes=20),
        profile_segments=[_segment(0, 0.6)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(0.6 * 10 / 60)


def test_unbounded_suspend_without_end_is_incomplete():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, None, kind=BasalEventKind.SUSPEND)],
        start,
        start + timedelta(minutes=20),
        profile_segments=[_segment(0, 0.6)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is False
    assert estimate.units == 0


def test_stored_zero_duration_cancel_resumes_schedule():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [
            BasalEvent(start, 2.0, 30),
            BasalEvent(
                start + timedelta(minutes=5),
                0.0,
                0,
                kind=BasalEventKind.CANCEL,
            ),
        ],
        start,
        start + timedelta(minutes=15),
        profile_segments=[_segment(0, 0.6)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(2.0 * 5 / 60 + 0.6 * 10 / 60)


def test_percent_temp_is_incomplete_then_returns_to_schedule():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [BasalEvent(start, None, 10, kind=BasalEventKind.UNKNOWN_RATE)],
        start,
        start + timedelta(minutes=20),
        profile_segments=[_segment(0, 0.6)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is False
    assert estimate.units == pytest.approx(0.6 * 10 / 60)


def test_subminute_gap_after_truncated_duration_is_ignored():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [
            BasalEvent(start, 1.0, 4),
            BasalEvent(start + timedelta(minutes=4, seconds=59), 0.8),
        ],
        start,
        start + timedelta(minutes=5),
        profile_segments=None,
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(4 / 60 + 0.8 / 60 / 60)


def test_same_timestamp_durationless_rows_keep_highest_order():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    estimate = estimate_basal_delivery(
        [
            BasalEvent(start, 1.0, order=uuid.UUID(int=10)),
            BasalEvent(start, 2.0, order=uuid.UUID(int=11)),
        ],
        start,
        start + timedelta(minutes=10),
        legacy_max_gap_hours=2.0,
    )

    assert estimate.units == pytest.approx(2.0 * 10 / 60)


@pytest.mark.parametrize("suspend_first", [False, True])
def test_same_timestamp_suspend_wins_over_basal_in_both_id_orders(suspend_first):
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    basal = BasalEvent(start, 1.5, order=uuid.UUID(int=1 if suspend_first else 2))
    suspend = BasalEvent(
        start,
        None,
        kind=BasalEventKind.SUSPEND,
        order=uuid.UUID(int=2 if suspend_first else 1),
    )
    resume = BasalEvent(
        start + timedelta(hours=1),
        None,
        kind=BasalEventKind.RESUME,
        order=uuid.UUID(int=3),
    )

    estimate = estimate_basal_delivery(
        [suspend, basal, resume] if suspend_first else [basal, suspend, resume],
        start,
        start + timedelta(hours=1),
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == 0


def test_unstored_nightscout_cancel_keeps_full_stated_temp_duration():
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)

    # Nightscout cancel treatments are not stored. With no following event,
    # the estimator can only honor the temp's original stated duration.
    estimate = estimate_basal_delivery(
        [BasalEvent(start, 2.0, 30)],
        start,
        start + timedelta(minutes=30),
        profile_segments=[_segment(0, 0.6)],
        legacy_max_gap_hours=2.0,
    )

    assert estimate.complete is True
    assert estimate.units == pytest.approx(1.0)
