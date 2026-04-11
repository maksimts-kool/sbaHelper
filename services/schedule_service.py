"""
ScheduleService — regenerates TTS schedule announcement on three triggers:
  1. Fixed interval (every SCHEDULE_INTERVAL_MIN minutes)
  2. Block boundary — active schedule items change (block starts or ends)
  3. Schedule data change — admin edited the schedule in AzuraCast
"""
import logging
import os
import time
import hashlib
import json
import tempfile
from datetime import datetime
from collections import OrderedDict

from core.config import (
    SCHEDULE_PLAYLIST_ID,
    SCHEDULE_INTERVAL_MIN,
    BG_START_PATH,
    BG_MID_PATH,
    BG_END_PATH,
    BG_FADE_MS,
)
from services.playlist_names import PLAYLIST_NAMES

logger = logging.getLogger(__name__)

_last_run_slot = None          # (hour, slot_index) — interval trigger
_last_active_keys = None       # frozenset of (id, start_ts) — block boundary trigger
_last_schedule_hash = None     # md5 of schedule structure — change detection trigger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_slot():
    now = datetime.now()
    return (now.hour, now.minute // SCHEDULE_INTERVAL_MIN)


def _get_readable_name(raw_name):
    return PLAYLIST_NAMES.get(raw_name.lower().strip(), raw_name)


def _join_names(names):
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " и " + names[-1]


def _active_keys(schedules):
    """frozenset of (id, start_timestamp) for items that are active right now."""
    now_ts = time.time()
    keys = set()
    for item in schedules:
        start_ts = item.get("start_timestamp", 0)
        end_ts = item.get("end_timestamp", 0)
        if item.get("is_now") or (start_ts <= now_ts <= end_ts):
            keys.add((item.get("id", 0), start_ts))
    return frozenset(keys)


def _schedule_hash(schedules):
    """MD5 fingerprint of schedule structure (id + start + end per item)."""
    items = sorted(
        (item.get("id", 0), item.get("start_timestamp", 0), item.get("end_timestamp", 0))
        for item in schedules
    )
    return hashlib.md5(json.dumps(items).encode()).hexdigest()


def _build_schedule_text(schedules):
    now_ts = time.time()
    current = []
    upcoming = []

    for item in schedules:
        name = item.get("name") or item.get("title") or ""
        start_ts = item.get("start_timestamp", 0)
        end_ts = item.get("end_timestamp", 0)
        readable = _get_readable_name(name)

        if item.get("is_now") or (start_ts <= now_ts <= end_ts):
            current.append(readable)
        elif start_ts > now_ts:
            start_str = item.get("start", "")
            try:
                time_label = datetime.fromisoformat(start_str).strftime("%H:%M")
            except (ValueError, TypeError):
                time_label = ""
            upcoming.append((readable, time_label, start_ts))

    upcoming.sort(key=lambda x: x[2])

    parts = []
    if current:
        parts.append(f"Сейчас в эфире: {_join_names(current)}")

    if upcoming:
        # Only announce the very next time block
        next_ts = upcoming[0][2]
        next_block = [(name, tl) for name, tl, ts in upcoming if ts == next_ts]
        names = [n for n, _ in next_block]
        time_label = next_block[0][1]
        next_str = f"{_join_names(names)} в {time_label}" if time_label else _join_names(names)
        parts.append("Далее: " + next_str)

    return ". ".join(parts) + "." if parts else None


def _generate_and_upload(api, tts, schedules, reason):
    text = _build_schedule_text(schedules)
    if not text:
        logger.info("[ScheduleService] Nothing to announce (%s).", reason)
        return

    logger.info("[%s] [ScheduleService] [%s] %s", datetime.now().strftime('%H:%M:%S'), reason, text)

    has_bg = all(os.path.isfile(p) for p in (BG_START_PATH, BG_MID_PATH, BG_END_PATH))

    with tempfile.TemporaryDirectory() as td:
        fpath = os.path.join(td, "schedule.mp3")
        try:
            if has_bg:
                tts.synth_with_background(
                    text, fpath,
                    bg_start=BG_START_PATH, bg_mid=BG_MID_PATH, bg_end=BG_END_PATH,
                    fade_ms=BG_FADE_MS,
                )
            else:
                tts.synth(text, fpath)
        except Exception:
            if not has_bg:
                raise
            logger.exception("[ScheduleService] Background mix failed, retrying without background (%s).", reason)
            tts.synth(text, fpath)

        resp = api.upload_file(fpath, "tts_schedule.mp3")
        file_id = resp.get("id")
        if file_id:
            api.set_file_playlist(file_id, SCHEDULE_PLAYLIST_ID)

    logger.info("[ScheduleService] tts_schedule.mp3 updated (%s).", reason)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(api, tts):
    global _last_run_slot, _last_active_keys, _last_schedule_hash

    try:
        schedules = api.get_schedules()
        if not schedules:
            logger.warning("[ScheduleService] No schedule data from API.")
            return

        slot = _current_slot()
        active = _active_keys(schedules)
        shash = _schedule_hash(schedules)

        initialized = _last_run_slot is not None  # False only on very first call

        reasons = []

        # Trigger 1: fixed interval
        if slot != _last_run_slot:
            reasons.append("interval")

        # Trigger 2: block boundary (a playlist block started or ended)
        if initialized and active != _last_active_keys:
            started = active - _last_active_keys
            ended = _last_active_keys - active
            if started:
                reasons.append("block started")
            if ended:
                reasons.append("block ended")

        # Trigger 3: schedule structure changed (admin edit)
        if initialized and shash != _last_schedule_hash:
            reasons.append("schedule updated")

        if not reasons:
            return

        _generate_and_upload(api, tts, schedules, " + ".join(reasons))

        _last_run_slot = slot
        _last_active_keys = active
        _last_schedule_hash = shash

    except Exception as e:
        logger.exception("[ScheduleService] Error: %s", e)
