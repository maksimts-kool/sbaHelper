import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class HeartbeatMonitor:
    """Writes atomic heartbeat/status files for liveness monitoring."""

    def __init__(self, service_name: str):
        self.service_name = service_name
        self._lock = threading.Lock()
        self.base_dir = Path(os.getenv("MONITORING_DIR", "runtime/monitoring"))
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.base_dir / f"{service_name}.json"

    def beat(self, status: str = "running", **details) -> None:
        payload = {
            "service": self.service_name,
            "status": status,
            "pid": os.getpid(),
            "timestamp": time.time(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "details": details,
        }
        temp_path = self.file_path.with_suffix(".json.tmp")
        with self._lock:
            with open(temp_path, "w", encoding="utf-8") as file_obj:
                json.dump(payload, file_obj, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.file_path)

    def fail(self, message: str, **details) -> None:
        self.beat(status="error", message=message, **details)

    def stop(self, **details) -> None:
        self.beat(status="stopped", **details)
