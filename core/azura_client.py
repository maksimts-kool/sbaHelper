import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class AzuraClient:
    def __init__(self, host, api_key, station_id):
        self.host = host
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.station_id = station_id
        self.client = httpx.Client(
            headers=self.headers,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
        )

    def close(self):
        self.client.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)),
        reraise=True,
    )
    def _request(self, method, url, **kwargs):
        response = self.client.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def get_queue(self):
        url = f"{self.host}/station/{self.station_id}/queue"
        return self._request("GET", url).json()

    def get_schedules(self, rows=10):
        """Fetch upcoming schedule items from AzuraCast."""
        url = f"{self.host}/station/{self.station_id}/schedule"
        params = {"rows": rows}
        return self._request("GET", url, params=params).json()

    def get_files(self):
        url = f"{self.host}/station/{self.station_id}/files"
        return self._request("GET", url).json()

    def upload_file(self, local_path, remote_name):
        url = f"{self.host}/station/{self.station_id}/files/upload"
        with open(local_path, "rb") as f:
            files = {"file": (remote_name, f)}
            response = self._request("POST", url, files=files)
        logger.info("[API] Upload status: %s", response.status_code)
        return response.json()

    def set_file_playlist(self, file_id, playlist_id):
        """Присваивает файл к указанному ID плейлиста."""
        url = f"{self.host}/station/{self.station_id}/file/{file_id}"
        payload = {
            "playlists": [
                {"id": playlist_id, "weight": 0}
            ]
        }
        response = self._request("PUT", url, json=payload)
        logger.info("[API] File %s linked to playlist %s (status=%s)", file_id, playlist_id, response.status_code)
        return response.json() if response.content else {}

    def enqueue_file(self, file_path):
        url = f"{self.host}/station/{self.station_id}/files/batch"
        payload = {"do": "queue", "files": [file_path]}
        response = self._request("PUT", url, json=payload)
        logger.info("[API] Queue status: %s", response.status_code)
        return response.json()

    def delete_file(self, file_id):
        url = f"{self.host}/station/{self.station_id}/file/{file_id}"
        response = self._request("DELETE", url)
        logger.info("[API] File deleted (id=%s, status=%s)", file_id, response.status_code)
