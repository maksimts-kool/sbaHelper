import os
import sys
import time
import subprocess
import requests
from datetime import datetime

# Импорт помощников
import playlist_service
import time_service

# --- КОНФИГУРАЦИЯ ---
AZURACAST_HOST = os.getenv("AZURACAST_HOST", "https://radio.maksimtsikvasvili24.thkit.ee")
STATION_ID = int(os.getenv("STATION_ID", "2"))
API_KEY = os.getenv("API_KEY", "")
INTRO_FILE_TEXT = os.getenv("INTRO_FILE_TEXT", "intro") 
TTS_VOICE = os.getenv("TTS_VOICE", "ru-RU-SvetlanaNeural")

BASE_API = f"{AZURACAST_HOST}/api"

# --- ОБЩИЕ ИНСТРУМЕНТЫ ---

class AzuraClient:
    def __init__(self, host, api_key, station_id):
        self.host = host
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.station_id = station_id

    def get_queue(self):
        url = f"{self.host}/station/{self.station_id}/queue"
        r = requests.get(url, headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_files(self):
        url = f"{self.host}/station/{self.station_id}/files"
        r = requests.get(url, headers=self.headers, timeout=15)
        return r.json()

    def upload_file(self, local_path, remote_name):
        url = f"{self.host}/station/{self.station_id}/files/upload"
        with open(local_path, "rb") as f:
            files = {"file": (remote_name, f)}
            r = requests.post(url, headers=self.headers, files=files, timeout=30)
        print(f"[API] Upload Status: {r.status_code}")
        r.raise_for_status()
        return r.json()

    # --- НОВЫЙ МЕТОД: УСТАНОВКА ПЛЕЙЛИСТА ---
    def set_file_playlist(self, file_id, playlist_id):
        """Присваивает файл к указанному ID плейлиста."""
        url = f"{self.host}/station/{self.station_id}/file/{file_id}"
        # Формат для API AzuraCast: список объектов плейлистов
        payload = {
            "playlists": [
                {
                    "id": playlist_id,
                    "weight": 0 # Вес по умолчанию
                }
            ]
        }
        try:
            r = requests.put(url, headers=self.headers, json=payload, timeout=10)
            if r.status_code == 200:
                print(f"[API] Файл {file_id} успешно привязан к плейлисту {playlist_id}.")
            else:
                print(f"[API] Ошибка привязки к плейлисту: {r.status_code} | {r.text}")
        except Exception as e:
            print(f"[API] Ошибка запроса set_file_playlist: {e}")

    def enqueue_file(self, file_path):
        url = f"{self.host}/station/{self.station_id}/files/batch"
        payload = {"do": "queue", "files": [file_path]}
        r = requests.put(url, headers=self.headers, json=payload, timeout=10)
        print(f"[API] Queue Status: {r.status_code}")
        return r.json()

    def delete_file(self, file_id):
        url = f"{self.host}/station/{self.station_id}/file/{file_id}"
        try:
            r = requests.delete(url, headers=self.headers, timeout=10)
            if r.status_code in [200, 204]:
                print(f" -> Файл удален (ID: {file_id})")
            else:
                print(f"Ошибка удаления файла {file_id}: {r.status_code}")
        except Exception as e:
            print(f"Ошибка delete_file: {e}")

class TTSEngine:
    def __init__(self, voice):
        self.voice = voice

    def synth(self, text, out_file, add_silence=True):
        from pydub import AudioSegment
        temp_mp3 = out_file + ".tmp"
        
        cmd = [
            sys.executable, "-m", "edge_tts",
            "--voice", self.voice,
            "--text", text,
            "--write-media", temp_mp3
        ]
        subprocess.check_call(cmd)

        audio = AudioSegment.from_file(temp_mp3, format="mp3")
        audio = audio + 12 
        
        if add_silence:
            silence = AudioSegment.silent(duration=1500) 
            final_audio = silence + audio + silence * 2
        else:
            final_audio = audio

        final_audio.export(out_file, format="mp3", bitrate="192k")
        if os.path.exists(temp_mp3):
            os.remove(temp_mp3)

# --- ОСНОВНОЙ ЦИКЛ ---

def main():
    print(">>> Запуск SbaRadio Bot")
    print(f"Target: {AZURACAST_HOST} | Station ID: {STATION_ID}")
    
    api = AzuraClient(BASE_API, API_KEY, STATION_ID)
    tts = TTSEngine(TTS_VOICE)

    while True:
        try:
            queue = api.get_queue()
            
            # 1. Запуск PlaylistService
            playlist_service.run(api, tts, queue, INTRO_FILE_TEXT) 

            # 2. Запуск TimeService
            time_service.run(api, tts, queue, INTRO_FILE_TEXT)

            time.sleep(25)
        except KeyboardInterrupt:
            print("\nОстановлено пользователем.")
            break
        except Exception as e:
            print(f"Ошибка в главном цикле: {e}")
            time.sleep(25)

if __name__ == "__main__":
    main()