"""
coordinator.py — DataUpdateCoordinator for the Wilma integration
================================================================
PURPOSE
    The coordinator is the single source of truth for exam, message, and
    schedule data. It owns the polling loop, calls the Wilma HTTP client,
    detects new exams and messages, and makes the data available to all
    sensor and calendar entities. Only one login + HTTP round-trip per
    poll cycle regardless of how many entities exist.

HOW IT WORKS (HA concepts)
    DataUpdateCoordinator (HA base class)
        A helper HA provides for the "poll once, share with many" pattern.
        It manages the update_interval timer and calls _async_update_data()
        on schedule. All entities that subscribe to the coordinator are
        automatically refreshed when new data arrives.

    _async_update_data()
        The method HA calls on each poll. It must return the new data dict
        or raise UpdateFailed (which HA turns into a sensor "unavailable"
        state with a log entry). After the executor job returns we fire
        events safely from the async context.

    async_add_executor_job()
        The requests library is blocking (synchronous). HA runs on an
        asyncio event loop, so blocking calls must be run in a thread pool
        via async_add_executor_job. This keeps the event loop free while
        the HTTP calls are in flight.

    hass.bus.async_fire()
        Fires a named event onto the HA event bus. Any automation with a
        matching event trigger will be woken up. We fire "wilma_new_exam"
        and "wilma_new_message" with the full data dict as event data so
        automations can use the details directly in templates.

    Child id re-discovery
        Wilma rotates the /!{id}/ role ids (observed at school-year
        rollover), so ids stored in the config entry at setup time go
        stale and Wilma answers 500. Every poll re-runs get_children()
        after login and resolves each configured child's id by name,
        falling back to the stored id only if the name is not found.
        Entity unique_ids still use the stored id, so entities stay
        stable across rotations.

    New-exam detection
        Each exam is fingerprinted as "date_iso|topic|subject". On the
        first poll _known_exams is empty so no events fire (avoids a
        flood of notifications on startup). From the second poll onward,
        any key not seen previously triggers an event.

    New-message detection
        Message IDs are incremental, so they serve as a reliable cursor.
        _known_message_ids tracks the set of IDs seen in the previous poll
        per child. First poll populates silently; subsequent polls fire an
        event for each new ID.

    Message filtering
        sender_filters is a list of glob patterns (e.g. ['*smith*']).
        All metadata is fetched in one JSON call, filtered client-side,
        and bodies are fetched only for the top message_limit matches.
        An empty sender_filters list passes all senders through.

    Data structure
        coordinator.data[child_name] = {
            "exams":      [...],   # list of exam dicts
            "messages":   [...],   # list of message dicts (with body)
            "schedule":   [...],   # list of schedule event dicts
            "attendance": [...],   # list of attendance mark dicts
        }

    update_interval
        How often the coordinator polls Wilma. Configured via scan_interval
        in the options flow (default: 4 hours). HA manages the timer.
"""

import fnmatch
import logging
from datetime import date, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import WilmaClient
from .const import (
    DOMAIN,
    EVENT_NEW_EXAM,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_ATTENDANCE,
    DEFAULT_SCHEDULE_PAST_WEEKS,
    DEFAULT_SCHEDULE_FUTURE_WEEKS,
    DEFAULT_MESSAGE_PRIVACY,
    MESSAGE_PRIVACY_COUNT,
    MESSAGE_PRIVACY_SUBJECT,
    MESSAGE_PRIVACY_SUBJECT_SENDER,
    MESSAGE_PRIVACY_FULL,
)

_LOGGER = logging.getLogger(__name__)


def _sender_matches(sender: str, patterns: list[str]) -> bool:
    """Return True if sender matches any glob pattern, or if patterns is empty."""
    if not patterns:
        return True
    sender_lower = sender.lower()
    return any(fnmatch.fnmatch(sender_lower, pat.lower()) for pat in patterns)


class WilmaCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        base_url: str,
        username: str,
        password: str,
        children: list[dict],
        scan_interval: int,
        sender_filters: list[str],
        message_limit: int,
        past_weeks: int = DEFAULT_SCHEDULE_PAST_WEEKS,
        future_weeks: int = DEFAULT_SCHEDULE_FUTURE_WEEKS,
        message_privacy: str = DEFAULT_MESSAGE_PRIVACY,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = WilmaClient(base_url, username, password)
        self.children = children
        self.sender_filters = sender_filters
        self.message_limit = message_limit
        self.message_privacy = message_privacy
        self.past_weeks = past_weeks
        self.future_weeks = future_weeks
        self._known_exams: dict[str, set] = {}
        self._known_message_ids: dict[str, set] = {}
        self._known_attendance_keys: dict[str, set] = {}

    async def _async_update_data(self) -> dict:
        try:
            data, new_exam_events, new_message_events, new_attendance_events = await self.hass.async_add_executor_job(
                self._fetch_all
            )
        except Exception as err:
            raise UpdateFailed(f"Error fetching Wilma data: {err}") from err

        for event_data in new_exam_events:
            self.hass.bus.async_fire(EVENT_NEW_EXAM, event_data)
        for event_data in new_message_events:
            self.hass.bus.async_fire(EVENT_NEW_MESSAGE, event_data)
        for event_data in new_attendance_events:
            self.hass.bus.async_fire(EVENT_NEW_ATTENDANCE, event_data)

        return data

    def _apply_message_privacy(self, msg: dict) -> dict:
        if self.message_privacy == MESSAGE_PRIVACY_SUBJECT:
            return {"id": msg["id"], "subject": msg.get("subject"), "is_unread": msg.get("is_unread")}
        if self.message_privacy == MESSAGE_PRIVACY_SUBJECT_SENDER:
            return {"id": msg["id"], "subject": msg.get("subject"), "sender": msg.get("sender"), "is_unread": msg.get("is_unread")}
        if self.message_privacy == MESSAGE_PRIVACY_FULL:
            return msg
        return {"id": msg["id"]}  # count_only

    def _fetch_all(self) -> tuple[dict, list[dict], list[dict], list[dict]]:
        self.client.login()

        # Wilma rotates the /!{id}/ role ids (observed at school-year
        # rollover), after which ids stored in the config entry return
        # HTTP 500. Re-discover on every poll and resolve ids by child
        # name; the stored id is only a fallback.
        try:
            fresh_ids = {c["name"]: c["id"] for c in self.client.get_children()}
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Re-discovering Wilma children failed: %s", err)
            fresh_ids = {}

        result = {}
        new_exam_events = []
        new_message_events = []
        new_attendance_events = []

        today = date.today()
        start_date = today - timedelta(weeks=self.past_weeks)
        end_date = today + timedelta(weeks=self.future_weeks)

        for child in self.children:
            name = child["name"]
            child_id = fresh_ids.get(name, child["id"])
            if name not in fresh_ids:
                _LOGGER.warning(
                    "Child %r not found on the Wilma home page; "
                    "falling back to stored id %s", name, child["id"],
                )

            # ── Exams ────────────────────────────────────────────────────────
            exams = self.client.get_exams(child_id)

            current_keys = {
                f"{e.get('date_iso')}|{e.get('topic')}|{e.get('subject')}"
                for e in exams
            }
            known_keys = self._known_exams.get(name)
            if known_keys is not None:
                new_keys = current_keys - known_keys
                for exam in exams:
                    key = f"{exam.get('date_iso')}|{exam.get('topic')}|{exam.get('subject')}"
                    if key in new_keys:
                        new_exam_events.append({"child": name, **exam})
            self._known_exams[name] = current_keys

            # ── Messages ─────────────────────────────────────────────────────
            # Fetch all metadata (1 call), take the N newest regardless of
            # sender, then filter that window by sender. Bodies are fetched
            # only for the matched subset — at most message_limit HTTP calls.
            all_messages = self.client.get_messages(child_id)
            newest = all_messages[:self.message_limit]
            matched = [
                m for m in newest
                if _sender_matches(m["sender"], self.sender_filters)
            ]

            if self.message_privacy == MESSAGE_PRIVACY_FULL:
                for msg in matched:
                    msg["body"] = self.client.fetch_message_body(child_id, msg["id"])

            known_ids = self._known_message_ids.get(name)
            if known_ids is not None:
                for msg in matched:
                    if msg["id"] not in known_ids:
                        new_message_events.append({"child": name, **self._apply_message_privacy(msg)})
            self._known_message_ids[name] = {m["id"] for m in matched}

            # ── Schedule ─────────────────────────────────────────────────────
            schedule_events: list[dict] = []
            seen_weeks: set[tuple] = set()
            current = start_date

            while current <= end_date:
                monday = current - timedelta(days=current.weekday())
                week_key = monday.isocalendar()[:2]
                if week_key not in seen_weeks:
                    seen_weeks.add(week_key)
                    date_fi = f"{monday.day}.{monday.month}.{monday.year}"
                    try:
                        schedule_events.extend(self.client.get_schedule(child_id, date_fi))
                    except Exception as err:
                        _LOGGER.warning(
                            "Failed to fetch schedule for %s (week %s): %s",
                            name, week_key, err,
                        )
                current += timedelta(days=7)

            schedule_events.sort(key=lambda e: (e["date"].split(".")[::-1], e["start_time"]))

            # ── Attendance ───────────────────────────────────────────────────
            attendance = self.client.get_attendance(child_id)

            current_att_keys = {
                f"{e['date_iso']}|{e['subject']}|{e['type']}|{e['type_id']}"
                for e in attendance
            }
            known_att_keys = self._known_attendance_keys.get(name)
            if known_att_keys is not None:
                new_att_keys = current_att_keys - known_att_keys
                for entry in attendance:
                    key = f"{entry['date_iso']}|{entry['subject']}|{entry['type']}|{entry['type_id']}"
                    if key in new_att_keys:
                        new_attendance_events.append({"child": name, **entry})
            self._known_attendance_keys[name] = current_att_keys

            result[name] = {"exams": exams, "messages": matched, "schedule": schedule_events, "attendance": attendance}

        return result, new_exam_events, new_message_events, new_attendance_events
