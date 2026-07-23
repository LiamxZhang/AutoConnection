from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import net_connector.scheduler as scheduler_module
from net_connector.scheduler import DailyScheduler, ScheduleError


def test_initialization_after_target_schedules_tomorrow():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 9, 0))

    assert scheduler.next_run == datetime(2026, 7, 24, 8, 30)


def test_initialization_before_target_schedules_today():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))

    assert scheduler.next_run == datetime(2026, 7, 23, 8, 30)


def test_initialization_at_target_schedules_tomorrow():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 30))

    assert scheduler.next_run == datetime(2026, 7, 24, 8, 30)


def test_poll_before_target_returns_false_without_changing_next_run():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))
    next_run = scheduler.next_run

    assert scheduler.poll(datetime(2026, 7, 23, 8, 29)) is False
    assert scheduler.next_run == next_run


def test_poll_at_target_returns_true_once_and_advances_to_tomorrow():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))

    assert scheduler.poll(datetime(2026, 7, 23, 8, 30)) is True
    assert scheduler.next_run == datetime(2026, 7, 24, 8, 30)
    assert scheduler.poll(datetime(2026, 7, 23, 8, 30)) is False


def test_poll_after_a_short_sleep_returns_true_once_and_advances():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))

    assert scheduler.poll(datetime(2026, 7, 23, 10, 0)) is True
    assert scheduler.next_run == datetime(2026, 7, 24, 8, 30)


def test_poll_after_multiple_missed_days_returns_true_once_and_skips_to_first_future_run():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))

    assert scheduler.poll(datetime(2026, 7, 26, 10, 0)) is True
    assert scheduler.next_run == datetime(2026, 7, 27, 8, 30)
    assert scheduler.poll(datetime(2026, 7, 26, 10, 0)) is False


def test_poll_after_multiple_missed_days_before_todays_target_keeps_todays_future_run():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))

    assert scheduler.poll(datetime(2026, 7, 26, 7, 0)) is True
    assert scheduler.next_run == datetime(2026, 7, 26, 8, 30)


def test_poll_advances_across_year_end():
    scheduler = DailyScheduler("08:30", datetime(2026, 12, 31, 8, 0))

    assert scheduler.poll(datetime(2026, 12, 31, 10, 0)) is True
    assert scheduler.next_run == datetime(2027, 1, 1, 8, 30)


def test_poll_at_2359_rolls_over_to_the_next_calendar_day():
    scheduler = DailyScheduler("23:59", datetime(2026, 7, 23, 23, 58))

    assert scheduler.poll(datetime(2026, 7, 23, 23, 59)) is True
    assert scheduler.next_run == datetime(2026, 7, 24, 23, 59)


def test_poll_advances_across_leap_day():
    scheduler = DailyScheduler("08:30", datetime(2028, 2, 28, 8, 0))

    assert scheduler.poll(datetime(2028, 2, 29, 10, 0)) is True
    assert scheduler.next_run == datetime(2028, 3, 1, 8, 30)


def test_aware_datetime_preserves_timezone():
    tz = timezone(timedelta(hours=8))
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0, tzinfo=tz))

    assert scheduler.next_run == datetime(2026, 7, 23, 8, 30, tzinfo=tz)
    assert scheduler.next_run.tzinfo is tz


def test_initialization_between_folds_uses_the_second_fold_occurrence():
    new_york = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 1, 15, tzinfo=new_york, fold=1)

    scheduler = DailyScheduler("01:30", now)

    assert scheduler.next_run == datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=1)
    assert scheduler.next_run.fold == 1


def test_poll_after_first_fold_target_does_not_fire_again_on_second_fold():
    new_york = ZoneInfo("America/New_York")
    scheduler = DailyScheduler("01:30", datetime(2026, 11, 1, 1, 0, tzinfo=new_york, fold=0))

    assert scheduler.poll(datetime(2026, 11, 1, 1, 45, tzinfo=new_york, fold=1)) is True
    assert scheduler.next_run == datetime(2026, 11, 2, 1, 30, tzinfo=new_york)
    assert scheduler.poll(datetime(2026, 11, 1, 1, 50, tzinfo=new_york, fold=1)) is False


def test_poll_at_first_fold_target_skips_the_second_fold_that_day():
    new_york = ZoneInfo("America/New_York")
    scheduler = DailyScheduler("01:30", datetime(2026, 11, 1, 1, 0, tzinfo=new_york, fold=0))

    assert scheduler.poll(datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=0)) is True
    assert scheduler.next_run == datetime(2026, 11, 2, 1, 30, tzinfo=new_york)
    assert scheduler.poll(datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=1)) is False


def test_poll_uses_scheduler_timezone_dates_for_compatible_aware_datetimes():
    new_york = ZoneInfo("America/New_York")
    scheduler = DailyScheduler("23:30", datetime(2026, 12, 30, 8, 0, tzinfo=new_york))
    utc_now = datetime(2027, 1, 1, 4, 0, tzinfo=timezone.utc)

    assert scheduler.poll(utc_now) is True
    assert scheduler.next_run == datetime(2026, 12, 31, 23, 30, tzinfo=new_york)
    assert scheduler.poll(utc_now) is False


def test_nonexistent_spring_forward_target_uses_its_round_tripped_future_time():
    new_york = ZoneInfo("America/New_York")
    scheduler = DailyScheduler("02:30", datetime(2026, 3, 8, 1, 0, tzinfo=new_york))

    assert scheduler.next_run == datetime(2026, 3, 8, 3, 30, tzinfo=new_york)


def test_initialization_skips_a_nonexistent_civil_date():
    apia = ZoneInfo("Pacific/Apia")
    scheduler = DailyScheduler("08:30", datetime(2011, 12, 29, 9, 0, tzinfo=apia))

    assert scheduler.next_run == datetime(2011, 12, 31, 8, 30, tzinfo=apia)


def test_poll_rejects_naive_aware_mismatch_without_changing_state():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))
    next_run = scheduler.next_run

    with pytest.raises(ScheduleError):
        scheduler.poll(datetime(2026, 7, 23, 8, 30, tzinfo=timezone.utc))

    assert scheduler.next_run == next_run


def test_reschedule_later_today_recomputes_next_run():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))

    scheduler.reschedule("10:00", datetime(2026, 7, 23, 9, 0))

    assert scheduler.schedule_time == "10:00"
    assert scheduler.next_run == datetime(2026, 7, 23, 10, 0)


def test_reschedule_to_an_earlier_time_schedules_tomorrow():
    scheduler = DailyScheduler("10:00", datetime(2026, 7, 23, 8, 0))

    scheduler.reschedule("08:30", datetime(2026, 7, 23, 9, 0))

    assert scheduler.schedule_time == "08:30"
    assert scheduler.next_run == datetime(2026, 7, 24, 8, 30)


def test_invalid_reschedule_leaves_existing_state_unchanged():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))
    previous_time = scheduler.schedule_time
    previous_next_run = scheduler.next_run

    with pytest.raises(ScheduleError):
        scheduler.reschedule("8:30", datetime(2026, 7, 23, 9, 0))

    assert scheduler.schedule_time == previous_time
    assert scheduler.next_run == previous_next_run


def test_poll_near_datetime_max_raises_without_changing_state():
    scheduler = DailyScheduler("23:59", datetime.max.replace(hour=23, minute=58))
    next_run = scheduler.next_run

    with pytest.raises(ScheduleError, match="unrepresentable future"):
        scheduler.poll(datetime.max.replace(hour=23, minute=59))

    assert scheduler.next_run == next_run


def test_reschedule_near_datetime_max_raises_without_changing_state():
    scheduler = DailyScheduler("23:58", datetime.max.replace(hour=23, minute=57))
    previous_time = scheduler.schedule_time
    previous_next_run = scheduler.next_run

    with pytest.raises(ScheduleError, match="unrepresentable future"):
        scheduler.reschedule("23:59", datetime.max.replace(hour=23, minute=59))

    assert scheduler.schedule_time == previous_time
    assert scheduler.next_run == previous_next_run


def test_initialization_near_datetime_max_raises_schedule_error():
    with pytest.raises(ScheduleError, match="unrepresentable future"):
        DailyScheduler("23:59", datetime.max.replace(hour=23, minute=59))


def test_multi_year_missed_poll_uses_one_future_occurrence_calculation(monkeypatch):
    scheduler = DailyScheduler("08:30", datetime(2026, 1, 1, 8, 0))
    calls = 0
    original = scheduler_module._first_occurrence_on_or_after

    def counting_occurrence(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(scheduler_module, "_first_occurrence_on_or_after", counting_occurrence)

    assert scheduler.poll(datetime(2126, 1, 1, 10, 0)) is True
    assert calls == 1
    assert scheduler.next_run == datetime(2126, 1, 2, 8, 30)


@pytest.mark.parametrize(
    "schedule_time",
    [None, 830, "8:30", "24:00", "12:60", "", " 08:30", "08:30 ", "0::00", "08-30", "0a:00", "08:3a"],
)
def test_invalid_schedule_times_are_rejected(schedule_time):
    with pytest.raises(ScheduleError):
        DailyScheduler(schedule_time, datetime(2026, 7, 23, 8, 0))


@pytest.mark.parametrize("now", [None, "2026-07-23T08:00", 0, object()])
def test_invalid_now_types_are_rejected(now):
    with pytest.raises(ScheduleError):
        DailyScheduler("08:30", now)
