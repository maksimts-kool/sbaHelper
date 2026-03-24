import logging
import os
import time
import tempfile
from datetime import datetime
from collections import OrderedDict

from core.config import SCHEDULE_PLAYLIST_ID
from services.playlist_names import PLAYLIST_NAMES

logger = logging.getLogger(__name__)

_intro_was_in_queue = False


def _get_readable_name(raw_name):
    return PLAYLIST_NAMES.get(raw_name.lower().strip(), raw_name)


def _join_names(names):
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " и " + names[-1]


def _build_next5_text(schedules):
    now_ts = time.time()
    current = []
    upcoming = []

    for item in schedules:
        name = item.get("name") or item.get("title") or ""
        start_ts = item.get("start_timestamp", 0)
        end_ts = item.get("end_timestamp", 0)
        is_now = item.get("is_now", False)
        readable = _get_readable_name(name)

        if is_now or (start_ts <= now_ts <= end_ts):
            current.append(readable)
        elif start_ts > now_ts:
            start_str = item.get("start", "")
            try:
                dt = datetime.fromisoformat(start_str)
                time_label = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                time_label = ""
            upcoming.append((readable, time_label, start_ts))

    upcoming.sort(key=lambda x: x[2])
    upcoming = upcoming[:5]

    parts = []

    if current:
        parts.append(f"Сейчас в эфире: {_join_names(current)}")

    if upcoming:
        grouped = OrderedDict()
        for name, time_label, _ in upcoming:
            grouped.setdefault(time_label or "", []).append(name)

        next_parts = []
        for time_label, names in grouped.items():
            combined = _join_names(names)
            next_parts.append(f"{combined} в {time_label}" if time_label else combined)
        parts.append("Далее: " + ", ".join(next_parts))

    if not parts:
        return None

    return ". ".join(parts) + "."


def run(api, tts, queue, intro_text):
    global _intro_was_in_queue

    intro_in_queue = any(
        (i.get("song", {}).get("text") == intro_text or i.get("song", {}).get("title") == intro_text)
        and not i.get("is_played")
        for i in queue
    )

    if _intro_was_in_queue and not intro_in_queue:
        logger.info("[%s] [PlaylistService] Intro finished. Generating schedule announcement...", datetime.now().strftime('%H:%M:%S'))

        try:
            schedules = api.get_schedules()
            if not schedules:
                logger.warning("[PlaylistService] No schedule data.")
                _intro_was_in_queue = intro_in_queue
                return

            text = _build_next5_text(schedules) or "Далее на радио отличная музыка."
            logger.info("[PlaylistService] TTS text: %s", text)

            with tempfile.TemporaryDirectory() as td:
                fpath = os.path.join(td, "next5.mp3")
                tts.synth(text, fpath)
                resp = api.upload_file(fpath, "tts_next5.mp3")
                file_id = resp.get("id")
                if file_id:
                    api.set_file_playlist(file_id, SCHEDULE_PLAYLIST_ID)

            logger.info("[PlaylistService] tts_next5.mp3 updated successfully.")

        except Exception as e:
            logger.exception("[PlaylistService] Error: %s", e)

    _intro_was_in_queue = intro_in_queue
