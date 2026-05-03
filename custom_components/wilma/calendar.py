"""
calendar.py — WilmaCalendar entity (one per child)
===================================================
Exposes each child's timetable as a Home Assistant calendar entity.

Schedule data is managed by WilmaScheduleCoordinator, which polls on its
own independent interval (default: daily). This keeps schedule fetching
separate from the exam/message poll cycle.

The `event` property (current/next lesson) is always populated from the
coordinator's cached data — no on-demand fetch needed.

`async_get_events` serves any date range HA requests. Events inside the
coordinator's cached window are returned directly from cache; events outside
it (e.g. when the user scrolls far into the future) are fetched live.
"""

import datetime
import logging

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    schedule_coordinator = hass.data[DOMAIN][f"{entry.entry_id}_schedule"]
    async_add_entities(
        [
            WilmaCalendar(schedule_coordinator, child, entry.entry_id)
            for child in schedule_coordinator.children
        ],
        True,
    )


class WilmaCalendar(CoordinatorEntity, CalendarEntity):
    def __init__(self, coordinator, child: dict, entry_id: str) -> None:
        super().__init__(coordinator)
        self._child_name = child["name"]
        self._child_id = child["id"]
        self._entry_id = entry_id

    @property
    def name(self) -> str:
        return f"Wilma {self._child_name} Schedule"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._child_id}_cal"

    @property
    def icon(self) -> str:
        return "mdi:calendar-school"

    @property
    def _cached_raw(self) -> list[dict]:
        return self.coordinator.data.get(self._child_name, []) if self.coordinator.data else []

    @property
    def event(self) -> CalendarEvent | None:
        """Return the active event, or the next upcoming one."""
        now = dt_util.now()
        for item in self._cached_raw:
            e = _to_calendar_event(item)
            if e and e.start <= now < e.end:
                return e
        for item in self._cached_raw:
            e = _to_calendar_event(item)
            if e and e.start > now:
                return e
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]:
        cached = self._cached_raw
        if cached:
            first = _parse_date(cached[0]["date"])
            last = _parse_date(cached[-1]["date"])
            if first and last and first <= start_date.date() and end_date.date() <= last:
                # Requested range is fully covered by the cache — serve directly.
                return _filter_events(cached, start_date.date(), end_date.date())

        # Range extends beyond the cache — fetch live.
        return await hass.async_add_executor_job(
            self._fetch_events, start_date, end_date
        )

    def _fetch_events(
        self,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]:
        self.coordinator.client.login()

        events: list[CalendarEvent] = []
        seen_weeks: set[tuple] = set()
        current = start_date.date()

        while current <= end_date.date():
            monday = current - datetime.timedelta(days=current.weekday())
            week_key = monday.isocalendar()[:2]
            if week_key not in seen_weeks:
                seen_weeks.add(week_key)
                date_fi = f"{monday.day}.{monday.month}.{monday.year}"
                try:
                    raw = self.coordinator.client.get_schedule(self._child_id, date_fi)
                    for item in raw:
                        e = _to_calendar_event(item)
                        if e and start_date.date() <= e.start.date() <= end_date.date():
                            events.append(e)
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to fetch schedule for %s (week %s): %s",
                        self._child_name, week_key, err,
                    )
            current += datetime.timedelta(days=7)

        events.sort(key=lambda e: e.start)
        return events


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> datetime.date | None:
    try:
        day, month, year = date_str.split(".")
        return datetime.date(int(year), int(month), int(day))
    except Exception:
        return None


def _filter_events(
    raw: list[dict],
    start: datetime.date,
    end: datetime.date,
) -> list[CalendarEvent]:
    events = []
    for item in raw:
        d = _parse_date(item.get("date", ""))
        if d and start <= d <= end:
            e = _to_calendar_event(item)
            if e:
                events.append(e)
    return events


def _to_calendar_event(item: dict) -> CalendarEvent | None:
    try:
        day, month, year = item["date"].split(".")
        h_start, m_start = item["start_time"].split(":")
        h_end, m_end = item["end_time"].split(":")

        tz = dt_util.DEFAULT_TIME_ZONE
        start = datetime.datetime(
            int(year), int(month), int(day), int(h_start), int(m_start), tzinfo=tz
        )
        end = datetime.datetime(
            int(year), int(month), int(day), int(h_end), int(m_end), tzinfo=tz
        )

        description = item.get("subject_long", "")
        if item.get("teacher"):
            description = f"{description}\n{item['teacher']}".strip()

        return CalendarEvent(
            start=start,
            end=end,
            summary=item.get("subject", ""),
            description=description or None,
            location=item.get("room") or None,
        )
    except Exception as err:
        _LOGGER.debug("Skipping malformed schedule event %s: %s", item, err)
        return None
