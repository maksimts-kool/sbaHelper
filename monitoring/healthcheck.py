import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser(description="Heartbeat-based healthcheck")
    parser.add_argument("--service", required=True)
    parser.add_argument("--max-age", type=int, default=int(os.getenv("HEALTHCHECK_MAX_AGE", "180")))
    args = parser.parse_args()

    monitoring_dir = Path(os.getenv("MONITORING_DIR", "runtime/monitoring"))
    status_file = monitoring_dir / f"{args.service}.json"
    if not status_file.exists():
        print(f"missing heartbeat: {status_file}")
        return 1

    try:
        with open(status_file, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except Exception as exc:
        print(f"invalid heartbeat file: {exc}")
        return 1

    timestamp = payload.get("timestamp", 0)
    age = time.time() - float(timestamp)
    status = payload.get("status", "unknown")
    if age > args.max_age:
        print(f"stale heartbeat: age={age:.1f}s status={status}")
        return 1
    if status == "error":
        print(f"service reported error: {payload.get('message', 'unknown error')}")
        return 1
    if status == "stopped":
        print("service stopped")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
