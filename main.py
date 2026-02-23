"""
TTS Worker — entrypoint.
Запускает цикл генерации анонсов плейлиста и объявления времени.
"""
import time as time_module

from core.config import API_KEY, AZURACAST_HOST, INTRO_FILE_TEXT, STATION_ID, TTS_VOICE
from core.azura_client import AzuraClient
from core.tts_engine import TTSEngine
from services import playlist_service, time_service

BASE_API = f"{AZURACAST_HOST}/api"


def main():
    print(">>> Запуск SbaRadio TTS Worker")
    print(f"Target: {AZURACAST_HOST} | Station ID: {STATION_ID}")

    api = AzuraClient(BASE_API, API_KEY, STATION_ID)
    tts = TTSEngine(TTS_VOICE)

    while True:
        try:
            queue = api.get_queue()
            playlist_service.run(api, tts, queue, INTRO_FILE_TEXT)
            time_service.run(api, tts, queue, INTRO_FILE_TEXT)
            time_module.sleep(25)
        except KeyboardInterrupt:
            print("\nОстановлено пользователем.")
            break
        except Exception as e:
            print(f"Ошибка в главном цикле: {e}")
            time_module.sleep(25)

if __name__ == "__main__":
    main()