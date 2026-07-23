"""Once-daily scheduling without background execution or persistence."""

from datetime import datetime, time, timedelta, timezone


class ScheduleError(Exception):
    """Raised when scheduler input is invalid or incompatible."""


_INVALID_TIME_SYNTAX = "invalid schedule time syntax"
_INVALID_TIME_RANGE = "invalid schedule time range"
_INVALID_DATETIME = "invalid datetime"
_AWARENESS_MISMATCH = "datetime awareness mismatch"
_UNREPRESENTABLE_FUTURE = "unrepresentable future"


class DailyScheduler:
    def __init__(self, schedule_time: str, now: datetime):
        target_time = _parse_schedule_time(schedule_time)
        _validate_datetime(now)
        self._schedule_time = schedule_time
        self._target_time = target_time
        self._next_run = _next_occurrence(target_time, now)

    @property
    def schedule_time(self) -> str:
        return self._schedule_time

    @property
    def next_run(self) -> datetime:
        return self._next_run

    def poll(self, now: datetime) -> bool:
        _validate_datetime(now)
        if _is_aware(now) != _is_aware(self._next_run):
            raise ScheduleError(_AWARENESS_MISMATCH)
        if _is_before(now, self._next_run):
            return False

        local_now = _scheduler_local_datetime(now, self._next_run.tzinfo)
        if self._next_run.date() == local_now.date():
            next_run = _first_occurrence_on_or_after(
                _next_calendar_day(local_now.date()), self._target_time, self._next_run.tzinfo
            )
        else:
            next_run = _next_occurrence(self._target_time, local_now)
        if not _is_before(local_now, next_run):
            raise ScheduleError(_UNREPRESENTABLE_FUTURE)
        self._next_run = next_run
        return True

    def reschedule(self, schedule_time: str, now: datetime) -> None:
        target_time = _parse_schedule_time(schedule_time)
        _validate_datetime(now)
        next_run = _next_occurrence(target_time, now)
        self._schedule_time = schedule_time
        self._target_time = target_time
        self._next_run = next_run


def _parse_schedule_time(value: str) -> time:
    if (
        type(value) is not str
        or len(value) != 5
        or value[2] != ":"
        or not value[0].isascii()
        or not value[1].isascii()
        or not value[3].isascii()
        or not value[4].isascii()
        or not value[0].isdigit()
        or not value[1].isdigit()
        or not value[3].isdigit()
        or not value[4].isdigit()
    ):
        raise ScheduleError(_INVALID_TIME_SYNTAX)
    hour = int(value[:2])
    minute = int(value[3:])
    if hour > 23 or minute > 59:
        raise ScheduleError(_INVALID_TIME_RANGE)
    return time(hour, minute)


def _validate_datetime(value: datetime) -> None:
    if not isinstance(value, datetime):
        raise ScheduleError(_INVALID_DATETIME)


def _next_occurrence(target_time: time, now: datetime) -> datetime:
    for candidate in _occurrences_on_date(now.date(), target_time, now.tzinfo):
        if _is_before(now, candidate):
            return candidate
    return _first_occurrence_on_or_after(_next_calendar_day(now.date()), target_time, now.tzinfo)


def _first_occurrence_on_or_after(day, target_time: time, tzinfo) -> datetime:
    while True:
        occurrences = _occurrences_on_date(day, target_time, tzinfo)
        if occurrences:
            return occurrences[0]
        day = _next_calendar_day(day)


def _occurrences_on_date(day, target_time: time, tzinfo) -> list[datetime]:
    if tzinfo is None:
        return [datetime.combine(day, target_time)]

    exact = []
    fallback = []
    seen_instants = set()
    for fold in (0, 1):
        candidate = datetime.combine(day, target_time.replace(fold=fold), tzinfo=tzinfo)
        round_tripped = _round_trip(candidate, tzinfo)
        instant = _instant(round_tripped)
        if instant in seen_instants:
            continue
        seen_instants.add(instant)
        if round_tripped.date() != day:
            continue
        if round_tripped.hour == target_time.hour and round_tripped.minute == target_time.minute:
            exact.append(round_tripped)
        elif round_tripped.timetz().replace(tzinfo=None) > target_time:
            fallback.append(round_tripped)

    return sorted(exact or fallback, key=_instant)


def _next_calendar_day(day):
    try:
        return day + timedelta(days=1)
    except OverflowError:
        raise ScheduleError(_UNREPRESENTABLE_FUTURE) from None


def _round_trip(value: datetime, tzinfo) -> datetime:
    try:
        return value.astimezone(timezone.utc).astimezone(tzinfo)
    except (OverflowError, ValueError):
        raise ScheduleError(_UNREPRESENTABLE_FUTURE) from None


def _scheduler_local_datetime(now: datetime, tzinfo) -> datetime:
    if tzinfo is None:
        return now
    try:
        return now.astimezone(tzinfo)
    except (OverflowError, ValueError):
        raise ScheduleError(_UNREPRESENTABLE_FUTURE) from None


def _is_before(left: datetime, right: datetime) -> bool:
    if _is_aware(left):
        return _instant(left) < _instant(right)
    return left < right


def _instant(value: datetime) -> datetime:
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ScheduleError(_UNREPRESENTABLE_FUTURE) from None


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
