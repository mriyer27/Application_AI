"""Config schedule entries -> APScheduler triggers, previews and missed-run detection.

Everything here is pure (no database, no running scheduler) so the daemon,
``job-agent schedule`` and the dashboard compute schedules the same way.

Cron expressions use standard crontab meaning, including the day-of-week field
(``0``/``7`` = Sunday, ``1-5`` = Monday-Friday). APScheduler 3 numbers weekdays
from Monday, so numeric weekday fields are translated to names before they
reach ``CronTrigger.from_crontab`` -- otherwise ``1-5`` would silently mean
Tuesday-Saturday.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from job_agent.config import ScheduleEntry
from job_agent.errors import ConfigError
from job_agent.utils.time import to_iso

#: Order for catch-up runs and ``daemon --once``: check health first, refresh
#: jobs and email, then derive follow-ups and the report from fresh data.
TASK_ORDER: tuple[str, ...] = ("health", "discover_and_score", "email_sync", "followups", "report")

#: Skip a catch-up run when the regular schedule fires this soon anyway.
CATCH_UP_SKIP_IF_NEXT_WITHIN = timedelta(minutes=15)

_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")  # crontab numbering, 0 = Sunday
# Look-back windows for "most recent fire time": small first so frequent crons stay cheap,
# larger ones so weekly / monthly crons are still caught up.
_LOOKBACK_WINDOWS = (timedelta(hours=2), timedelta(days=2), timedelta(days=9), timedelta(days=32))


def order_key(name: str, config_order: list[str] | None = None) -> tuple[int, int, str]:
    """Sort key placing known tasks in ``TASK_ORDER`` first, then others in config order."""
    primary = TASK_ORDER.index(name) if name in TASK_ORDER else len(TASK_ORDER)
    secondary = config_order.index(name) if config_order and name in config_order else 0
    return primary, secondary, name


# --------------------------------------------------------------------------- #
# Cron parsing
# --------------------------------------------------------------------------- #
def _dow_value(token: str) -> int:
    token = token.strip().lower()
    if token.isdigit():
        value = int(token)
        if not 0 <= value <= 7:
            raise ValueError(f"day of week {token!r} is out of range 0-7")
        return value
    if token in _DOW_NAMES:
        return _DOW_NAMES.index(token)
    raise ValueError(f"day of week {token!r} is not a number 0-7 or a name like mon")


def _expand_dow(part: str) -> set[int]:
    step = 1
    if "/" in part:
        part, _, step_text = part.partition("/")
        if not step_text.isdigit() or int(step_text) < 1:
            raise ValueError(f"invalid day-of-week step {step_text!r}")
        step = int(step_text)
    if part == "*":
        low, high = 0, 6
    elif "-" in part:
        first, _, last = part.partition("-")
        low, high = _dow_value(first), _dow_value(last)
        if high == 0 and low > 0:  # e.g. "mon-sun"
            high = 7
        if low > high:
            raise ValueError(f"day-of-week range {part!r} ends before it starts")
    else:
        low = _dow_value(part)
        high = low if step == 1 else max(low, 6)
    return {value % 7 for value in range(low, high + 1, step)}


def crontab_day_of_week(field: str) -> str:
    """Translate a crontab day-of-week field into explicit APScheduler day names."""
    text = field.strip().lower()
    if text in {"*", "?"}:
        return "*"
    days: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ValueError(f"empty item in day-of-week field {field!r}")
        days |= _expand_dow(part)
    return ",".join(_DOW_NAMES[d] for d in sorted(days))


def cron_trigger(expression: str, tz: tzinfo) -> CronTrigger:
    """``CronTrigger.from_crontab`` with standard crontab weekday numbering."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(fields)}")
    minute, hour, day, month, dow = fields
    return CronTrigger.from_crontab(" ".join([minute, hour, day, month, crontab_day_of_week(dow)]),
                                    timezone=tz)


def build_trigger(name: str, entry: ScheduleEntry, tz: tzinfo, *, now: datetime | None = None) -> BaseTrigger:
    """Trigger for one ``scheduler.tasks`` entry. Raises ``ConfigError`` for an invalid cron."""
    if entry.cron is not None:
        try:
            return cron_trigger(entry.cron, tz)
        except ValueError as exc:
            raise ConfigError(
                f"scheduler.tasks.{name}.cron {entry.cron!r} is invalid: {exc}",
                human_action=f"Fix scheduler.tasks.{name}.cron in config/config.yaml "
                             "(5 fields: minute hour day-of-month month day-of-week).",
            ) from None
    assert entry.interval_minutes is not None  # guaranteed by ScheduleEntry validation
    interval = timedelta(minutes=entry.interval_minutes)
    start = (now or datetime.now(tz)).astimezone(tz) + interval
    return IntervalTrigger(minutes=entry.interval_minutes, start_date=start, timezone=tz)


def schedule_text(entry: ScheduleEntry) -> str:
    """Human-readable schedule, as stored in ``scheduled_tasks.schedule``."""
    if entry.cron is not None:
        return f"cron {entry.cron}"
    return f"every {entry.interval_minutes} min"


# --------------------------------------------------------------------------- #
# Fire times
# --------------------------------------------------------------------------- #
def next_fire_times(trigger: BaseTrigger, now: datetime, count: int) -> list[datetime]:
    times: list[datetime] = []
    previous: datetime | None = None
    for _ in range(max(count, 0)):
        nxt = trigger.get_next_fire_time(previous, previous or now)
        if nxt is None:
            break
        times.append(nxt)
        previous = nxt
    return times


def most_recent_fire_time(trigger: BaseTrigger, now: datetime) -> datetime | None:
    """The latest fire time <= ``now`` (within ~a month), found by iterating forward."""
    for window in _LOOKBACK_WINDOWS:
        last: datetime | None = None
        candidate = trigger.get_next_fire_time(None, now - window)
        while candidate is not None and candidate <= now:
            last = candidate
            candidate = trigger.get_next_fire_time(candidate, candidate)
        if last is not None:
            return last
    return None


def missed_run_marker(entry: ScheduleEntry, trigger: BaseTrigger, *, last_success: datetime | None,
                      now: datetime, next_run: datetime | None = None,
                      skip_if_next_within: timedelta = CATCH_UP_SKIP_IF_NEXT_WITHIN) -> str | None:
    """Identify a run that was missed (laptop off / asleep / daemon stopped).

    Returns a stable marker for the missed run (the missed fire time, or for
    interval tasks the time the run became due) so that each missed run is
    caught up at most once, even across daemon restarts. ``None`` = nothing missed.
    """
    if next_run is not None and next_run - now <= skip_if_next_within:
        return None
    if entry.cron is not None:
        due = most_recent_fire_time(trigger, now)
        if due is None or (last_success is not None and last_success >= due):
            return None
        return to_iso(due)
    assert entry.interval_minutes is not None
    if last_success is None:
        return "never-succeeded"
    due = last_success + timedelta(minutes=entry.interval_minutes)
    return to_iso(due) if due <= now else None
