"""Constants for the Wilma integration."""

DOMAIN = "wilma"

CONF_BASE_URL = "base_url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_CHILDREN = "children"
CONF_SENDER_FILTERS = "sender_filters"
CONF_MESSAGE_LIMIT = "message_limit"

DEFAULT_SCAN_INTERVAL = 14400  # 4 hours
DEFAULT_MESSAGE_LIMIT = 10
DEFAULT_SCHEDULE_PAST_WEEKS = 1
DEFAULT_SCHEDULE_FUTURE_WEEKS = 4

CONF_SCHEDULE_PAST_WEEKS = "schedule_past_weeks"
CONF_SCHEDULE_FUTURE_WEEKS = "schedule_future_weeks"

EVENT_NEW_EXAM = "wilma_new_exam"
EVENT_NEW_MESSAGE = "wilma_new_message"
