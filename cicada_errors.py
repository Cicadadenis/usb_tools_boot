#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Классификация ошибок Cicada USB Boot Tool и диагностическое логирование."""

from __future__ import annotations

import json
import logging
import socket
import sys
import traceback
import urllib.error
from pathlib import Path
from dataclasses import dataclass
from enum import Enum

try:
    import requests.exceptions as requests_exceptions
except ImportError:  # pragma: no cover
    requests_exceptions = None  # type: ignore[assignment]


class ErrorCategory(str, Enum):
    NETWORK = "network"
    TIMEOUT = "timeout"
    ASSETS_UNAVAILABLE = "assets_unavailable"
    FILE_EXISTS = "file_exists"
    USB_LOCKED = "usb_locked"
    USB_ACCESS = "usb_access"
    USB_PATH_LOST = "usb_path_lost"
    PARTITION_REVEAL_TIMEOUT = "partition_reveal_timeout"
    ADMIN = "admin"
    UNKNOWN = "unknown"


ERROR_CODES: dict[ErrorCategory, str] = {
    ErrorCategory.NETWORK: "CICADA-101",
    ErrorCategory.TIMEOUT: "CICADA-102",
    ErrorCategory.ASSETS_UNAVAILABLE: "CICADA-103",
    ErrorCategory.FILE_EXISTS: "CICADA-204",
    ErrorCategory.USB_LOCKED: "CICADA-201",
    ErrorCategory.USB_ACCESS: "CICADA-202",
    ErrorCategory.USB_PATH_LOST: "CICADA-203",
    ErrorCategory.PARTITION_REVEAL_TIMEOUT: "CICADA-205",
    ErrorCategory.ADMIN: "CICADA-301",
    ErrorCategory.UNKNOWN: "CICADA-999",
}

ERROR_TITLES: dict[ErrorCategory, str] = {
    ErrorCategory.NETWORK: "🌐 НЕТ ПОДКЛЮЧЕНИЯ К ИНТЕРНЕТУ",
    ErrorCategory.TIMEOUT: "🌐 ТАЙМАУТ СОЕДИНЕНИЯ",
    ErrorCategory.ASSETS_UNAVAILABLE: "✕ ФАЙЛЫ СБОРКИ НЕДОСТУПНЫ",
    ErrorCategory.FILE_EXISTS: "✕ ОБРАЗ УЖЕ СУЩЕСТВУЕТ",
    ErrorCategory.USB_LOCKED: "🔒 ФЛЕШКА ЗАНЯТА",
    ErrorCategory.USB_ACCESS: "🔒 НЕТ ДОСТУПА К РАЗДЕЛУ",
    ErrorCategory.USB_PATH_LOST: "🔌 ПОТЕРЯН ПУТЬ К USB",
    ErrorCategory.PARTITION_REVEAL_TIMEOUT: "НЕ УДАЛОСЬ ОТКРЫТЬ РАЗДЕЛ",
    ErrorCategory.ADMIN: "🛡 ТРЕБУЮТСЯ ПРАВА АДМИНИСТРАТОРА",
    ErrorCategory.UNKNOWN: "✕ НЕИЗВЕСТНАЯ ОШИБКА",
}

RETRYABLE_CATEGORIES = frozenset({
    ErrorCategory.NETWORK,
    ErrorCategory.TIMEOUT,
    ErrorCategory.USB_LOCKED,
    ErrorCategory.USB_ACCESS,
    ErrorCategory.USB_PATH_LOST,
})

_NETWORK_ERRNOS = frozenset({10060, 10061, 10054, 11001, 11002, 11004, 10065, 10051})
_USB_LOCKED_WINERRORS = frozenset({5, 32})
_USB_LOCKED_MARKERS = (
    "используется другим",
    "is being used by another",
    "in use by another",
    "sharing violation",
    "процессом не может получить доступ",
    "cannot access the file because it is being used",
    "the process cannot access the file",
    "winerror 32",
    "error 32",
)
_USB_ACCESS_MARKERS = (
    "не удалось получить доступ к разделу",
    "add-partitionaccesspath",
    "cannot access the drive",
    "нет доступа к разделу",
    "partition access",
    "virtual disk service error",
    "ошибка службы виртуальных дисков",
    "requested access path is already in use",
    "already in use",
    "storagewmi 42002",
    "не удалось временно открыть раздел",
    "не удалось получить букву раздела",
)
_USB_PATH_LOST_MARKERS = (
    "get-partition",
    "msft_partition",
    "objectnotfound",
    "cmdletizationquery_notfound",
    "failed to find matching objects",
    "не удалось найти объекты",
    "не удалось найти",
    "запрос cim экземпляров",
    "cim instance request",
)
_NETWORK_MARKERS = (
    "urlopen error",
    "urlerror",
    "getaddrinfo failed",
    "name or service not known",
    "nodename nor servname",
    "network is unreachable",
    "network unreachable",
    "connection refused",
    "connection reset",
    "connection aborted",
    "failed to establish",
    "unable to connect",
    "remote end closed",
    "temporary failure in name resolution",
    "no route to host",
    "errno 11001",
    "errno 10060",
    "errno 10061",
    "errno 10054",
    "нет подключения к интернету",
    "name resolution",
)
_TIMEOUT_MARKERS = (
    "timed out",
    "timeout",
    "таймаут",
    "read timed out",
    "connect timed out",
)

_logger: logging.Logger | None = None
_logging_enabled: bool | None = None
_SETTINGS_FILE_NAME = "cicada_settings.json"


def _bundled_settings_path() -> Path:
    from cicada_usb_tool import resource_path

    return resource_path(_SETTINGS_FILE_NAME)


def _default_logging_enabled() -> bool:
    path = _bundled_settings_path()
    if not path.is_file():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    if not isinstance(data, dict):
        return True
    return bool(data.get("logging_enabled", True))


def _user_settings_path() -> Path:
    from cicada_usb_tool import cicada_temp_dir

    return cicada_temp_dir() / _SETTINGS_FILE_NAME


def _settings_read_paths() -> list[Path]:
    from cicada_usb_tool import app_dir

    paths = [
        _user_settings_path(),
        app_dir() / _SETTINGS_FILE_NAME,
        _bundled_settings_path(),
    ]
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _load_logging_enabled_from_disk(default: bool | None = None) -> bool:
    if default is None:
        default = _default_logging_enabled()
    for path in _settings_read_paths():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        return bool(data.get("logging_enabled", default))
    return default


def _remove_user_settings_if_redundant() -> None:
    path = _user_settings_path()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    if set(data.keys()) - {"logging_enabled"}:
        return
    if bool(data.get("logging_enabled", _default_logging_enabled())) == _default_logging_enabled():
        try:
            path.unlink()
        except OSError:
            pass


def _save_logging_enabled_to_disk(enabled: bool) -> None:
    from cicada_usb_tool import ensure_runtime_dir

    path = _user_settings_path()
    data: dict[str, object] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}
    data["logging_enabled"] = enabled
    try:
        ensure_runtime_dir(path.parent)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def is_logging_enabled() -> bool:
    global _logging_enabled
    if _logging_enabled is None:
        _logging_enabled = _load_logging_enabled_from_disk()
    return _logging_enabled


def set_logging_enabled(enabled: bool) -> None:
    global _logging_enabled
    _logging_enabled = enabled
    if enabled == _default_logging_enabled():
        _remove_user_settings_if_redundant()
        return
    _save_logging_enabled_to_disk(enabled)


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    from cicada_usb_tool import ensure_runtime_dir

    log_path = debug_log_path()
    ensure_runtime_dir(log_path.parent)
    logger = logging.getLogger("cicada_usb_tool")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    _logger = logger
    return logger


def debug_log_path() -> Path:
    from cicada_usb_tool import cicada_temp_dir, ensure_runtime_dir

    path = cicada_temp_dir() / "cicada_tool.log"
    ensure_runtime_dir(path.parent)
    return path


def open_debug_log_file() -> Path:
    """Открыть cicada_tool.log в программе по умолчанию (Блокнот и т.п.)."""
    import os
    import subprocess
    import sys

    path = debug_log_path()
    if not path.is_file():
        path.touch()
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)], close_fds=True)
    else:
        subprocess.Popen(["xdg-open", str(path)], close_fds=True)
    return path


def debug_log(message: str) -> None:
    """Запись отладочных сообщений в cicada_tool.log."""
    if not is_logging_enabled():
        return
    _get_logger().info(message)


def write_debug_log_line(message: str) -> None:
    """Запись строки в лог независимо от переключателя UI."""
    _get_logger().info(message)


@dataclass(frozen=True)
class ClassifiedError:
    category: ErrorCategory
    code: str
    title: str
    message: str
    retryable: bool


def _unwrap_exception(exc: BaseException) -> BaseException:
    current = exc
    seen: set[int] = set()
    while True:
        if id(current) in seen:
            break
        seen.add(id(current))
        cause = current.__cause__ or current.__context__
        if cause is None or cause is current:
            break
        if type(cause) is type(current) and str(cause) == str(current):
            break
        current = cause
    return current


def _message_chain(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return " ".join(parts).lower()


def _is_timeout_exception(exc: BaseException, text: str) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if requests_exceptions is not None and isinstance(exc, requests_exceptions.Timeout):
        return True
    if isinstance(exc, socket.timeout):
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout)):
        return True
    return any(marker in text for marker in _TIMEOUT_MARKERS)


def _os_error_code(exc: OSError) -> int | None:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return int(winerror)
    errno = getattr(exc, "errno", None)
    if errno is not None and sys.platform == "win32":
        return int(errno)
    return None


def _is_network_exception(exc: BaseException, text: str) -> bool:
    if isinstance(exc, OSError):
        code = _os_error_code(exc)
        if code in _USB_LOCKED_WINERRORS:
            return False
    if isinstance(exc, ConnectionError):
        return True
    if requests_exceptions is not None and isinstance(exc, requests_exceptions.ConnectionError):
        return True
    if isinstance(exc, socket.gaierror):
        return True
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (ConnectionError, socket.gaierror, OSError)):
            reason = exc.reason
            if isinstance(reason, OSError) and _os_error_code(reason) in _USB_LOCKED_WINERRORS:
                return False
            return True
        return any(marker in text for marker in _NETWORK_MARKERS)
    if isinstance(exc, OSError):
        code = _os_error_code(exc)
        if code in _NETWORK_ERRNOS:
            return True
    return any(marker in text for marker in _NETWORK_MARKERS)


def _is_usb_locked_exception(exc: BaseException, text: str) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        code = _os_error_code(exc)
        if code in _USB_LOCKED_WINERRORS:
            return True
    return any(marker in text for marker in _USB_LOCKED_MARKERS)


def _is_usb_access_exception(text: str) -> bool:
    if any(marker in text for marker in _USB_ACCESS_MARKERS):
        return True
    return "add-partitionaccesspath" in text and "already in use" in text


def _is_no_free_drive_letters(text: str) -> bool:
    return "нет свободных букв дисков" in text


def _is_usb_path_lost_exception(text: str) -> bool:
    if not any(marker in text for marker in _USB_PATH_LOST_MARKERS):
        return False
    return "get-partition" in text or "msft_partition" in text


def _build_user_message(category: ErrorCategory, detail: str = "") -> str:
    detail = detail.strip()
    if category == ErrorCategory.NETWORK:
        if "файлы сборки не найдены" in detail.lower():
            body = (
                "Файлы сборки не найдены локально.\n\n"
                "Положите рядом с программой:\n"
                "- Cicada3301.7z\n"
                "- FAT32.7z\n"
                "- 7z.exe\n\n"
                "или подключите интернет."
            )
        else:
            body = (
                "Не удалось подключиться к Интернету.\n\n"
                "Проверьте:\n"
                "• Wi-Fi\n"
                "• Кабель Ethernet\n"
                "• VPN\n"
                "• Прокси"
            )
    elif category == ErrorCategory.ASSETS_UNAVAILABLE:
        body = (
            "Не удалось скачать необходимые файлы с GitHub.\n\n"
            "Проверьте ссылку, интернет или положите файлы рядом с программой."
        )
    elif category == ErrorCategory.FILE_EXISTS:
        body = (
            "Файл с таким именем уже есть на флешке.\n\n"
            "Переименуйте образ или удалите старый файл."
        )
    elif category == ErrorCategory.TIMEOUT:
        body = (
            "Превышено время ожидания ответа от сервера.\n\n"
            "Проверьте:\n"
            "• Скорость подключения\n"
            "• VPN и прокси\n"
            "• Доступность GitHub"
        )
    elif category == ErrorCategory.USB_LOCKED:
        body = (
            "Устройство используется другой программой.\n\n"
            "Закройте:\n"
            "• Проводник Windows\n"
            "• Total Commander\n"
            "• Rufus\n"
            "• Ventoy\n"
            "• Любые окна флешки"
        )
    elif category == ErrorCategory.USB_ACCESS:
        body = (
            "Не удалось временно открыть раздел Cicada3301.\n\n"
            "Возможные причины:\n"
            "• буква диска уже занята\n"
            "• раздел используется системой\n"
            "• нет прав администратора"
        )
    elif category == ErrorCategory.USB_PATH_LOST:
        if detail and _is_no_free_drive_letters(detail.lower()):
            body = (
                "Нет свободных букв дисков.\n\n"
                "Освободите одну из букв дисков в системе и повторите операцию."
            )
        else:
            body = (
                "Потерян путь к USB-накопителю.\n\n"
                "Диск был отключён, переподключён или изменился номер раздела.\n\n"
                "Проверьте:\n"
                "• USB-накопитель подключён\n"
                "• Нажмите «Обновить» в программе\n"
                "• Переподключите флешку и повторите операцию"
            )
    elif category == ErrorCategory.PARTITION_REVEAL_TIMEOUT:
        body = "Операция заняла слишком много времени."
    elif category == ErrorCategory.ADMIN:
        return "Для работы с разделами необходим запуск программы от имени администратора."
    else:
        body = detail or "Произошла непредвиденная ошибка."
    code = ERROR_CODES[category]
    return f"{body}\n\nКод:\n{code}"


def _is_file_exists_exception(root: BaseException, text: str) -> bool:
    if isinstance(root, FileExistsError):
        return True
    return "файл уже существует" in text or (
        "already exists" in text and "partition" not in text
    )


def _is_partition_reveal_timeout_exception(text: str) -> bool:
    return "cicada-205" in text


def _is_assets_unavailable_exception(text: str) -> bool:
    return "cicada-103" in text or "файлы сборки недоступны" in text


def _is_offline_no_assets_exception(text: str) -> bool:
    return "cicada-101" in text or (
        "файлы сборки не найдены" in text and "интернет" in text
    )


def classify_exception(exc: BaseException) -> ClassifiedError:
    root = _unwrap_exception(exc)
    text = _message_chain(exc)

    if _is_file_exists_exception(root, text):
        category = ErrorCategory.FILE_EXISTS
    elif _is_partition_reveal_timeout_exception(text):
        category = ErrorCategory.PARTITION_REVEAL_TIMEOUT
    elif _is_assets_unavailable_exception(text):
        category = ErrorCategory.ASSETS_UNAVAILABLE
    elif _is_offline_no_assets_exception(text):
        category = ErrorCategory.NETWORK
    elif _is_usb_locked_exception(root, text):
        category = ErrorCategory.USB_LOCKED
    elif _is_timeout_exception(root, text):
        category = ErrorCategory.TIMEOUT
    elif _is_network_exception(root, text):
        category = ErrorCategory.NETWORK
    elif _is_usb_access_exception(text):
        category = ErrorCategory.USB_ACCESS
    elif _is_no_free_drive_letters(text):
        category = ErrorCategory.USB_PATH_LOST
    elif _is_usb_path_lost_exception(text):
        category = ErrorCategory.USB_PATH_LOST
    else:
        category = ErrorCategory.UNKNOWN

    detail = str(exc)
    hide_detail = category in {
        ErrorCategory.USB_ACCESS,
        ErrorCategory.USB_PATH_LOST,
        ErrorCategory.FILE_EXISTS,
        ErrorCategory.ASSETS_UNAVAILABLE,
        ErrorCategory.PARTITION_REVEAL_TIMEOUT,
    }
    title = ERROR_TITLES[category]
    if category == ErrorCategory.USB_PATH_LOST and _is_no_free_drive_letters(text):
        title = "⚠ НЕТ СВОБОДНЫХ БУКВ ДИСКОВ"
    elif category == ErrorCategory.USB_ACCESS:
        title = "🔒 НЕТ ДОСТУПА К РАЗДЕЛУ"

    if category == ErrorCategory.USB_PATH_LOST and _is_no_free_drive_letters(text):
        message_detail = text
    elif hide_detail:
        message_detail = ""
    else:
        message_detail = detail

    return ClassifiedError(
        category=category,
        code=ERROR_CODES[category],
        title=title,
        message=_build_user_message(category, message_detail),
        retryable=category in RETRYABLE_CATEGORIES,
    )


def classify_admin_error() -> ClassifiedError:
    category = ErrorCategory.ADMIN
    return ClassifiedError(
        category=category,
        code=ERROR_CODES[category],
        title=ERROR_TITLES[category],
        message=_build_user_message(category),
        retryable=False,
    )


def log_exception(exc: BaseException, code: str) -> None:
    if not is_logging_enabled():
        return
    root = _unwrap_exception(exc)
    stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    entry = (
        "[ERROR]\n"
        f"Type: {type(root).__name__}\n"
        f"Message: {root}\n"
        f"Code: {code}\n"
        f"{stack}"
    )
    _get_logger().error(entry)


def exception_log_summary(exc: BaseException, classified: ClassifiedError) -> str:
    root = _unwrap_exception(exc)
    return f"{classified.title} ({classified.code}): {root}"
