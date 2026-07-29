"""
Заглушки внешних зависимостей, чтобы тесты запускались без установленных
`telegram`, `yt_dlp`, `dotenv` и `sentry_sdk`.

pytest импортирует conftest до сбора тестов, поэтому заглушки успевают встать
раньше, чем тестовые модули импортируют `downloader.*`. Если настоящий пакет
установлен (как в CI), заглушка не ставится вовсе — иначе она перекрыла бы
настоящий пакет вместе со всеми подмодулями (`telegram.ext` и прочими).
"""

from __future__ import annotations

import importlib.util
import sys
import types


def _is_installed(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def _stub_unless_installed(top_level: str, build) -> None:
    if top_level in sys.modules or _is_installed(top_level):
        return
    sys.modules.update(build())


def _dotenv() -> dict[str, types.ModuleType]:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    return {"dotenv": dotenv}


def _sentry_sdk() -> dict[str, types.ModuleType]:
    sentry_sdk = types.ModuleType("sentry_sdk")
    sentry_sdk.init = lambda **kwargs: None
    sentry_sdk.set_tag = lambda *args, **kwargs: None
    sentry_sdk.capture_exception = lambda *args, **kwargs: None
    sentry_sdk.flush = lambda *args, **kwargs: None
    return {"sentry_sdk": sentry_sdk}


def _telegram() -> dict[str, types.ModuleType]:
    telegram = types.ModuleType("telegram")
    telegram_error = types.ModuleType("telegram.error")

    class NetworkError(Exception):
        pass

    telegram_error.NetworkError = NetworkError
    telegram.error = telegram_error
    return {"telegram": telegram, "telegram.error": telegram_error}


def _yt_dlp() -> dict[str, types.ModuleType]:
    yt_dlp = types.ModuleType("yt_dlp")
    yt_dlp_utils = types.ModuleType("yt_dlp.utils")

    class YtdlpDownloadError(Exception):
        pass

    yt_dlp_utils.DownloadError = YtdlpDownloadError
    yt_dlp.utils = yt_dlp_utils
    yt_dlp.YoutubeDL = object
    return {"yt_dlp": yt_dlp, "yt_dlp.utils": yt_dlp_utils}


for _top_level, _build in (
    ("dotenv", _dotenv),
    ("sentry_sdk", _sentry_sdk),
    ("telegram", _telegram),
    ("yt_dlp", _yt_dlp),
):
    _stub_unless_installed(_top_level, _build)
