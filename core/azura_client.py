import requests


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

    def get_schedules(self, rows=10):
        """Fetch upcoming schedule items from AzuraCast."""
        url = f"{self.host}/station/{self.station_id}/schedule"
        params = {"rows": rows}
        r = requests.get(url, headers=self.headers, params=params, timeout=10)
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

    def set_file_playlist(self, file_id, playlist_id):
        """Присваивает файл к указанному ID плейлиста."""
        url = f"{self.host}/station/{self.station_id}/file/{file_id}"
        payload = {
            "playlists": [
                {"id": playlist_id, "weight": 0}
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
