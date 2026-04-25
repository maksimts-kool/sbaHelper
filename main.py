"""
TTS Worker — entrypoint.
Запускает цикл генерации анонсов плейлиста и расписания.
"""
import logging
import time as time_module

from core.config import API_KEY, AZURACAST_HOST, INTRO_FILE_TEXT, STATION_ID, TTS_VOICE
from core.azura_client import AzuraClient
from core.tts_engine import TTSEngine
from bot.state import is_radio_decommissioned
from monitoring.logging_utils import configure_logging
from monitoring.runtime import HeartbeatMonitor
from monitoring.sentry import init_sentry
from services import playlist_service, schedule_service

BASE_API = f"{AZURACAST_HOST}/api"

configure_logging("sbaradio-tts")
init_sentry("sbaradio-tts")
logger = logging.getLogger(__name__)
monitor = HeartbeatMonitor("sbaradio-tts")


def main():
    if is_radio_decommissioned():
        logger.info("Radio is decommissioned. TTS worker will not start.")
        return

    logger.info(">>> Запуск SbaRadio TTS Worker")
    logger.info("Target: %s | Station ID: %s", AZURACAST_HOST, STATION_ID)

    api = AzuraClient(BASE_API, API_KEY, STATION_ID)
    tts = TTSEngine(TTS_VOICE)
    monitor.beat(status="starting", station_id=STATION_ID)

    try:
        while True:
            if is_radio_decommissioned():
                logger.info("Radio decommissioned. Stopping TTS worker.")
                try:
                    monitor.file_path.unlink(missing_ok=True)
                except Exception:
                    logger.debug("Failed to remove decommissioned TTS monitor file", exc_info=True)
                break

            try:
                queue = api.get_queue()
                playlist_service.run(api, tts, queue, INTRO_FILE_TEXT)
                schedule_service.run(api, tts)
                monitor.beat(status="running", queue_size=len(queue))
                time_module.sleep(25)
            except KeyboardInterrupt:
                logger.info("Остановлено пользователем.")
                monitor.stop(reason="keyboard_interrupt")
                break
            except Exception as e:
                logger.exception("Ошибка в главном цикле")
                monitor.fail("main loop error", error=str(e))
                time_module.sleep(25)
    finally:
        api.close()

if __name__ == "__main__":
    main()
