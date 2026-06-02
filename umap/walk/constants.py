from __future__ import annotations

import re


PLACEHOLDER_RE = re.compile(r"\((Routes|Transport)\s+(\d+)\)", re.IGNORECASE)
TRANSPORT_HINT_RE = re.compile(r"\b(bus|train|tram|trolleybus|rail)\b", re.IGNORECASE)
PLANNED_NEW_NOTIFICATION_TITLE = "Новый план маршрута добавлен"
PLANNED_CHANGE_NOTIFICATION_TITLE = "Изменен план маршрута"
PLANNED_NEW_NOTIFICATION_EMOJIS = (("5958798052641738769", "📱"),)
PLANNED_CHANGE_NOTIFICATION_EMOJIS = (
    ("5395444784611480792", "✏️"),
    ("5958798052641738769", "📱"),
)
TRANSPORT_EMOJI_KEYS = {
    "🚌": "bus",
    "🚆": "train",
    "🚋": "tram",
    "🚎": "trolleybus",
}
