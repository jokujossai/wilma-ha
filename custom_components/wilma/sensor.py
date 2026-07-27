"""
sensor.py — WilmaExamSensor and WilmaMessageSensor entities
============================================================
PURPOSE
    Creates two sensor entities per child: one for upcoming exams and one
    for recent messages. Both subscribe to the same coordinator so a single
    poll cycle updates all sensors at once.

HOW IT WORKS (HA concepts)
    async_setup_entry(hass, entry, async_add_entities)
        HA calls this after __init__.py forwards setup to the sensor
        platform. We pull the coordinator from hass.data and create one
        exam sensor and one message sensor per child.

    CoordinatorEntity (base class)
        Wires the entity into the coordinator's update cycle. Whenever the
        coordinator finishes a poll and has new data, HA automatically
        calls async_write_ha_state() on every subscribed entity.

    coordinator.data structure
        coordinator.data[child_name] = {
            "exams":      [...],   # list of exam dicts
            "messages":   [...],   # list of message dicts (with body)
            "attendance": [...],   # list of attendance mark dicts
            "errors":     {...},   # section name -> error string
        }

    available
        Each sensor goes unavailable while its own section is listed in
        "errors" (that poll's fetch failed and the data shown is stale),
        without affecting the other sensors of the same child.

    unique_id
        Derived from the config entry ID and child ID so it stays unique
        even across multiple Wilma accounts. Message sensor appends "_msg",
        attendance sensor appends "_att".

    extra_state_attributes
        Available in automation templates via state_attr(...).
        Exam sensor:       exams, next_exam, next_exam_date
        Message sensor:    messages, latest_message
        Attendance sensor: entries, latest_entry
"""

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MESSAGE_PRIVACY_COUNT,
    MESSAGE_PRIVACY_SUBJECT,
    MESSAGE_PRIVACY_SUBJECT_SENDER,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for child in coordinator.children:
        entities.append(WilmaExamSensor(coordinator, child, entry.entry_id))
        entities.append(WilmaMessageSensor(coordinator, child, entry.entry_id))
        entities.append(WilmaAttendanceSensor(coordinator, child, entry.entry_id))
    async_add_entities(entities, True)


class WilmaExamSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, child: dict, entry_id: str) -> None:
        super().__init__(coordinator)
        self._child_name = child["name"]
        self._child_id = child["id"]
        self._entry_id = entry_id

    @property
    def name(self) -> str:
        return f"Wilma {self._child_name}"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._child_id}"

    @property
    def icon(self) -> str:
        return "mdi:school"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        child = (self.coordinator.data or {}).get(self._child_name, {})
        return "exams" not in child.get("errors", {})

    @property
    def _exams(self) -> list:
        return self.coordinator.data.get(self._child_name, {}).get("exams", [])

    @property
    def native_value(self) -> int | str:
        return len(self._exams) if self._exams else "Ei kokeita"

    @property
    def native_unit_of_measurement(self) -> str | None:
        return "koetta" if self._exams else None

    @property
    def extra_state_attributes(self) -> dict:
        exams = self._exams
        attrs: dict = {"child": self._child_name, "exams": exams}
        if exams:
            attrs["next_exam"] = exams[0]
            attrs["next_exam_date"] = exams[0].get("date_iso")
        return attrs


class WilmaMessageSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, child: dict, entry_id: str) -> None:
        super().__init__(coordinator)
        self._child_name = child["name"]
        self._child_id = child["id"]
        self._entry_id = entry_id

    @property
    def name(self) -> str:
        return f"Wilma {self._child_name} Messages"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._child_id}_msg"

    @property
    def icon(self) -> str:
        return "mdi:message-text"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        child = (self.coordinator.data or {}).get(self._child_name, {})
        return "messages" not in child.get("errors", {})

    @property
    def _messages(self) -> list:
        return self.coordinator.data.get(self._child_name, {}).get("messages", [])

    @property
    def native_value(self) -> int:
        return sum(1 for m in self._messages if m.get("is_unread"))

    @property
    def native_unit_of_measurement(self) -> str | None:
        return "unread" if self.native_value else None

    @property
    def extra_state_attributes(self) -> dict:
        messages = self._messages
        privacy = self.coordinator.message_privacy
        attrs: dict = {"child": self._child_name}

        if privacy == MESSAGE_PRIVACY_COUNT:
            return attrs

        if privacy == MESSAGE_PRIVACY_SUBJECT:
            filtered = [{"subject": m.get("subject"), "is_unread": m.get("is_unread")} for m in messages]
        elif privacy == MESSAGE_PRIVACY_SUBJECT_SENDER:
            filtered = [{"subject": m.get("subject"), "sender": m.get("sender"), "is_unread": m.get("is_unread")} for m in messages]
        else:
            filtered = messages

        attrs["messages"] = filtered
        if filtered:
            attrs["latest_message"] = filtered[0]
        return attrs


class WilmaAttendanceSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, child: dict, entry_id: str) -> None:
        super().__init__(coordinator)
        self._child_name = child["name"]
        self._child_id = child["id"]
        self._entry_id = entry_id

    @property
    def name(self) -> str:
        return f"Wilma {self._child_name} Attendance"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._child_id}_att"

    @property
    def icon(self) -> str:
        return "mdi:clipboard-check"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        child = (self.coordinator.data or {}).get(self._child_name, {})
        return "attendance" not in child.get("errors", {})

    @property
    def _entries(self) -> list:
        return self.coordinator.data.get(self._child_name, {}).get("attendance", [])

    @property
    def native_value(self) -> int:
        return len(self._entries)

    @property
    def native_unit_of_measurement(self) -> str | None:
        return "entries" if self._entries else None

    @property
    def extra_state_attributes(self) -> dict:
        entries = self._entries
        attrs: dict = {"child": self._child_name, "entries": entries}
        if entries:
            attrs["latest_entry"] = entries[0]
        return attrs
