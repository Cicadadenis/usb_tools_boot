#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cicada USB Boot Tool — ядро: разметка диска, загрузка файлов, 7z.

Интерфейс v2.1 — в cicada_usb_tool_frosted.py (запуск через start_tool.bat).
"""

from __future__ import annotations

import ctypes
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

BOOT_PARTITION_MB = 1536
APP_TITLE = "Cicada USB Boot Tool"
CREATE_NO_WINDOW = 0x08000000

CICADA_FLAG_FILE = ".cicada3301.flag"
CICADA_SIGNATURE = "CICADA3301_BOOT"
CICADA_LAYOUT = "NTFS+FAT32"
# NTFS P1 (Cicada3301) остаётся видимым (0x07) — быстрее и без unhide/rehide.
FULL_HIDE_AFTER_CREATE = False
HIDDEN_PARTITION_TYPE = "17"
HIDDEN_BOOT_PARTITION_TYPE = "1C"
VISIBLE_NTFS_TYPE = "07"
VISIBLE_MBR_TYPE_HEX = "0x07"
HIDDEN_MBR_TYPE_HEXES = frozenset({"0x17", "0x23"})
CICADA_MBR_SIGNATURE_PREFIX = 0x33010000
CICADA_MBR_SIGNATURE_PREFIX_MASK = 0xFFFF0000
CICADA_MBR_SIGNATURE_LEGACY = 0x3301C1CA
CICADA_MBR_SIGNATURE_LEGACY_HEX = "3301C1CA"
CICADA_MBR_COLLISION_UI_MESSAGE = (
    "USB-накопитель переведён Windows в Offline из-за совпадения MBR Signature."
)
CICADA_MBR_COLLISION_UI_MESSAGE_MULTILINE = (
    "USB-накопитель переведён Windows в Offline\n"
    "из-за совпадения MBR Signature."
)
CICADA_STATS_FILENAME = ".cicada_stats.json"
PERF_SLOW_SEC = 3.0

ASSET_URLS = {
    "Cicada3301.7z": "https://github.com/Cicadadenis/usb_boot/raw/refs/heads/main/Cicada3301.7z",
    "FAT32.7z": "https://github.com/Cicadadenis/usb_boot/raw/refs/heads/main/FAT32.7z",
    "7z.exe": "https://github.com/Cicadadenis/usb_boot/raw/refs/heads/main/7z.exe",
}


def _subprocess_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def hide_console_window() -> None:
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def gui_executable() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


@dataclass
class UsbDisk:
    number: int
    model: str
    size_bytes: int
    is_cicada: bool = False
    cicada_verified: bool = False
    signature: str | None = None
    unique_id: str | None = None
    fast_is_cicada_signature: bool = False
    fast_is_cicada_layout: bool = False
    mbr_collision_offline: bool = False

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024**3)

    @property
    def fast_cicada_detected(self) -> bool:
        return self.fast_is_cicada_signature and self.fast_is_cicada_layout

    @property
    def cicada_bad_layout(self) -> bool:
        return self.fast_is_cicada_signature and not self.fast_is_cicada_layout

    @property
    def is_cicada_probable(self) -> bool:
        return self.is_cicada and not self.cicada_verified and not self.fast_is_cicada_signature

    def label(self) -> str:
        base = f"{self.number} — {self.model} — {self.size_gb:.0f} GB"
        if self.is_cicada:
            suffix = " · Cicada USB Boot"
            if self.is_cicada_probable:
                suffix = " · Cicada USB Boot?"
            return f"{base}{suffix}"
        return base


def _copy_usb_disk(disk: UsbDisk) -> UsbDisk:
    return UsbDisk(
        disk.number,
        disk.model,
        disk.size_bytes,
        is_cicada=disk.is_cicada,
        cicada_verified=disk.cicada_verified,
        signature=disk.signature,
        unique_id=disk.unique_id,
        fast_is_cicada_signature=disk.fast_is_cicada_signature,
        fast_is_cicada_layout=disk.fast_is_cicada_layout,
        mbr_collision_offline=disk.mbr_collision_offline,
    )


@dataclass
class ImageEntry:
    path: Path
    name: str
    size_bytes: int
    category: str
    relative_path: str = ""

    @property
    def icon_key(self) -> str:
        return {
            "WINDOWS": "windows",
            "LINUX": "linux",
            "WINPE": "winpe",
        }.get(self.category.upper(), "iso")

    @property
    def file_icon_key(self) -> str:
        ext = self.path.suffix.lower()
        if ext in {".iso", ".img", ".wim", ".vhd", ".vhdx", ".esd"}:
            return "iso"
        return self.icon_key

    @property
    def boot_title(self) -> str:
        version = windows_version_from_path(self.relative_path or self.path)
        return make_boot_title(self.category, version, self.path)


_WINDOWS_VERSION_PATH_MARKERS: tuple[tuple[str, str], ...] = (
    ("/WIN11/", "Windows 11"),
    ("/WIN10/", "Windows 10"),
    ("/WIN7/", "Windows 7"),
    ("/VISTA/", "Windows Vista"),
    ("/XP/", "Windows XP"),
    ("/SVR2022/", "Windows Server 2022"),
    ("/SVR2019/", "Windows Server 2019"),
    ("/SVR2016/", "Windows Server 2016"),
    ("/SVR2012/", "Windows Server 2012"),
    ("/SVR2K8R2/", "Windows Server 2008 R2"),
)


def windows_version_from_path(image_path: Path | str) -> str | None:
    path_str = str(image_path).replace("\\", "/").upper()
    for marker, label in _WINDOWS_VERSION_PATH_MARKERS:
        if marker in path_str:
            return label
    return None


def make_boot_title(
    category: str,
    version: str | None,
    image_path: Path | str,
) -> str:
    path = Path(image_path)
    name = path.name
    stem = path.stem

    cat = category.upper()
    if cat == "WINPE":
        return f"WinPE • {stem}"
    if cat == "WINDOWS":
        return f"{version} • {stem}" if version else f"Windows • {stem}"
    if cat == "LINUX":
        return f"Linux • {stem}"
    return name


def resolve_import_dest_name(
    category: str,
    subfolder: str | None,
    source: Path,
) -> str:
    """E2B/agFM ожидают Windows.iso в папках установки Windows."""
    if category.upper() != "WINDOWS" or not subfolder:
        return source.name
    sub = subfolder.upper()
    if sub == "WINXP":
        sub = "XP"
    if sub in _WINDOWS_INSTALL_SUBFOLDERS:
        return WINDOWS_INSTALL_DEST_NAME
    return source.name


def app_dir() -> Path:
    """Каталог рядом с exe (или исходниками) — только чтение bundled-ресурсов и локальных 7z."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def cicada_temp_dir() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / "Cicada3301"


def ensure_runtime_dir(path: Path | None = None) -> Path:
    """Создать каталог runtime только перед реальной записью файла."""
    target = path if path is not None else cicada_temp_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def cicada_cache_dir(*, create: bool = False) -> Path:
    directory = cicada_temp_dir() / "cache"
    if create:
        ensure_runtime_dir(directory)
    return directory


def cicada_work_temp_dir(*, create: bool = False) -> Path:
    directory = cicada_temp_dir() / "temp"
    if create:
        ensure_runtime_dir(directory)
    return directory


def migrate_runtime_files_to_temp() -> None:
    """Перенос только устаревших assets/icon в cache (без runtime-файлов)."""
    import tempfile

    cache = cicada_cache_dir()
    exe_dir = app_dir()
    if exe_dir.resolve() == cicada_temp_dir().resolve():
        return
    legacy_ico = exe_dir / "cicada_icon.ico"
    dst_ico = cache / "cicada_icon.ico"
    if legacy_ico.is_file() and not dst_ico.exists():
        try:
            ensure_runtime_dir(cache)
            shutil.copy2(legacy_ico, dst_ico)
        except OSError:
            pass
    old_assets = Path(tempfile.gettempdir()) / "CicadaUSB"
    if old_assets.is_dir() and old_assets.resolve() != cache.resolve():
        for filename in ASSET_URLS:
            src = old_assets / filename
            dst = cache / filename
            if src.is_file() and not dst.is_file():
                try:
                    ensure_runtime_dir(cache)
                    shutil.copy2(src, dst)
                except OSError:
                    pass


def _bundle_dir() -> Path:
    """Каталог встроенных read-only ресурсов (PyInstaller _MEIPASS или исходники)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> Path:
    """Путь к bundled-ресурсу: _MEIPASS при сборке PyInstaller, иначе рядом с исходником."""
    return _bundle_dir() / Path(relative.replace("/", os.sep))


def app_icon_path() -> Path | None:
    candidates = (
        "icon.ico",
        "cicada_icon.ico",
        "cicada_icon.png",
        "img/256/07_about.png",
    )
    for name in candidates:
        path = resource_path(name)
        if path.is_file():
            return path
    return None


def load_app_icon() -> QIcon | None:
    path = app_icon_path()
    if path is None:
        return None
    icon = QIcon(str(path))
    return icon if not icon.isNull() else None


WINDOWS_APP_ID = "Cicada3301.USB.BootTool.v2"


def setup_windows_app_id() -> None:
    """Отдельная группа на панели задач (не pythonw.exe)."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception:
        pass


def _ensure_ico_file() -> Path | None:
    for name in ("cicada_icon.ico", "icon.ico"):
        source = resource_path(name)
        if source.is_file():
            return source
    ico_local = cicada_cache_dir() / "cicada_icon.ico"
    if ico_local.is_file():
        return ico_local
    png = resource_path("cicada_icon.png")
    if png.is_file():
        try:
            from PyQt6.QtGui import QPixmap

            ensure_runtime_dir(ico_local.parent)
            pixmap = QPixmap(str(png))
            if not pixmap.isNull() and pixmap.save(str(ico_local), "ICO"):
                return ico_local
        except Exception:
            pass
    return None


def apply_windows_taskbar_icon(window: QWidget) -> None:
    """Иконка на панели задач Windows (pythonw.exe по умолчанию показывает Python)."""
    if sys.platform != "win32":
        return
    setup_windows_app_id()
    icon_path = _ensure_ico_file()
    if icon_path is None:
        return
    try:
        hwnd = int(window.winId())
        if hwnd == 0:
            return
        path_str = str(icon_path.resolve())
        image_icon = 1
        lr_loadfromfile = 0x00000010
        lr_defaultsize = 0x00000040
        wm_seticon = 0x0080
        for icon_size in (0, 1):
            hicon = ctypes.windll.user32.LoadImageW(
                None,
                path_str,
                image_icon,
                0,
                0,
                lr_loadfromfile | lr_defaultsize,
            )
            if hicon:
                ctypes.windll.user32.SendMessageW(hwnd, wm_seticon, icon_size, hicon)
    except Exception:
        pass


def temp_assets_dir() -> Path:
    return cicada_cache_dir()


def assets_complete(path: Path) -> bool:
    return all(
        (path / filename).is_file() and (path / filename).stat().st_size > 0
        for filename in ASSET_URLS
    )


def resolve_assets_dir() -> tuple[Path, bool]:
    """Возвращает (каталог ресурсов, нужно_ли_скачивать_недостающие)."""
    if assets_complete(app_dir()):
        return app_dir(), False
    temp_dir = temp_assets_dir()
    if assets_complete(temp_dir):
        return temp_dir, False
    return temp_dir, True


def _raise_assets_offline_error() -> None:
    raise RuntimeError(
        "НЕТ ПОДКЛЮЧЕНИЯ К ИНТЕРНЕТУ\n\n"
        "Файлы сборки не найдены локально.\n\n"
        "Положите рядом с программой:\n"
        "- Cicada3301.7z\n"
        "- FAT32.7z\n"
        "- 7z.exe\n\n"
        "или подключите интернет.\n\n"
        "Код: CICADA-101"
    )


def _raise_assets_unavailable_error() -> None:
    raise RuntimeError(
        "ФАЙЛЫ СБОРКИ НЕДОСТУПНЫ\n\n"
        "Не удалось скачать необходимые файлы с GitHub.\n\n"
        "Проверьте ссылку, интернет или положите файлы рядом с программой.\n\n"
        "Код: CICADA-103"
    )


def download_assets(
    dest_dir: Path,
    log: callable | None = None,
    progress: callable[[int], None] | None = None,
) -> None:
    ensure_runtime_dir(dest_dir)
    total_files = len(ASSET_URLS)
    for index, (filename, url) in enumerate(ASSET_URLS.items()):
        dest = dest_dir / filename
        if dest.is_file() and dest.stat().st_size > 0:
            if log:
                log(f"{filename} уже есть — пропуск загрузки")
            if progress:
                overall = int((index + 1) / total_files * 10)
                progress(max(1, overall))
            continue
        if log:
            log(f"Загрузка {filename}...")
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "CicadaUSBTool/2.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                content_length = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 256 * 1024
                with dest.open("wb") as output:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded += len(chunk)
                        if progress and content_length:
                            file_ratio = downloaded / content_length
                            overall = int((index + file_ratio) / total_files * 10)
                            progress(max(1, overall))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404) or exc.code >= 500:
                _raise_assets_unavailable_error()
            raise
        except (
            urllib.error.URLError,
            OSError,
            socket.gaierror,
            TimeoutError,
        ) as exc:
            if not assets_complete(dest_dir):
                text = str(exc).lower()
                if any(
                    marker in text
                    for marker in (
                        "urlopen error",
                        "getaddrinfo failed",
                        "name or service not known",
                        "network is unreachable",
                        "connection refused",
                        "timed out",
                        "нет подключения",
                    )
                ):
                    _raise_assets_offline_error()
            raise
        if log:
            size_mb = dest.stat().st_size / (1024 * 1024)
            log(f"  {filename} загружен ({size_mb:.1f} MB)")


def cleanup_temp_assets(dest_dir: Path) -> None:
    for filename in ASSET_URLS:
        path = dest_dir / filename
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    executable = gui_executable()
    if getattr(sys, "frozen", False):
        params = " ".join(f'"{arg}"' for arg in sys.argv[1:])
    else:
        script = Path(sys.argv[0]).resolve()
        extra = list(sys.argv[1:])
        if extra and Path(extra[0]).resolve() == script:
            extra = extra[1:]
        params = f'"{script}"'
        if extra:
            params += " " + " ".join(f'"{arg}"' for arg in extra)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", executable, params, str(app_dir()), 1
    )
    sys.exit(0)


def run_powershell(
    script: str, *, strict: bool = True, timeout: float | None = None
) -> str:
    stdout, stderr, returncode = _run_powershell_capture(script, timeout=timeout)
    if strict:
        if returncode != 0:
            raise RuntimeError((stderr or stdout or "PowerShell error").strip())
        return stdout.strip()
    return (stdout + stderr).strip()


def _run_powershell_capture(
    script: str, *, timeout: float | None = None
) -> tuple[str, str, int]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            timeout=timeout,
            **_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("PowerShell operation timed out") from exc
    stdout = decode_console_output(result.stdout)
    stderr = decode_console_output(result.stderr)
    return stdout, stderr, result.returncode


def find_7z(base: Path) -> Path:
    candidates = [
        base / "7z.exe",
        Path(r"C:\Program Files\7-Zip\7z.exe"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    found = subprocess.run(
        ["where", "7z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_subprocess_kwargs(),
    )
    if found.returncode == 0 and found.stdout.strip():
        return Path(found.stdout.strip().splitlines()[0])
    raise FileNotFoundError(
        "7z.exe не найден. Положите 7z.exe рядом с программой или установите 7-Zip."
    )


def list_usb_disks() -> list[UsbDisk]:
    script = r"""
$disks = Get-Disk | Where-Object {
    ($_.BusType -eq 'USB' -or $_.BusType -eq 'SD') -and $_.Size -gt 0
} | Sort-Object Number
$result = @()
foreach ($d in $disks) {
    $model = (Get-PhysicalDisk -DeviceNumber $d.Number -ErrorAction SilentlyContinue).FriendlyName
    if (-not $model) { $model = $d.FriendlyName }
    if (-not $model) { $model = 'USB Device' }
    $result += [PSCustomObject]@{
        Number = [int]$d.Number
        Model = [string]$model
        Size = [long]$d.Size
    }
}
$result | ConvertTo-Json -Compress
"""
    raw = run_powershell(script)
    if not raw or raw == "null":
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return [UsbDisk(d["Number"], d["Model"], int(d["Size"])) for d in data]


CICADA_P1_MBR_TYPES = {0x07, 0x17, 7, 23}
CICADA_P2_MBR_TYPES = {0x17, 0x1C, 17, 28}
MIN_CICADA_P1_BYTES = 2 * 1024**3
BOOT_PARTITION_BYTES = BOOT_PARTITION_MB * 1024 * 1024
BOOT_PARTITION_TOLERANCE_BYTES = 256 * 1024 * 1024
_USB_SCAN_CACHE: tuple[float, list[UsbDisk]] | None = None
_USB_SCAN_CACHE_TTL = 5.0
_USB_SCAN_CACHE_LOCK = threading.Lock()
_NTFS_DISK_LOCKS: dict[int, threading.Lock] = {}
_NTFS_DISK_LOCKS_GUARD = threading.Lock()
IMPORT_REVEAL_TIMEOUT_SEC = 60.0
MOUNT_CLOSE_TIMEOUT_SEC = 15.0
DISK_PROBE_TIMEOUT_SEC = 20.0
_FAST_MOUNT_SUPPORTED: dict[str, bool] = {}
_HIDDEN_NTFS_SUPPORTED: dict[str, bool] = {}
_FAST_MOUNT_LOCK = threading.Lock()
_FLAG_VERIFIED_CACHE: dict[str, bool] = {}
_FLAG_VERIFIED_CACHE_LOCK = threading.Lock()


def is_cicada_flag_verified_cached(identity_key: str) -> bool:
    if not identity_key:
        return False
    with _FLAG_VERIFIED_CACHE_LOCK:
        return bool(_FLAG_VERIFIED_CACHE.get(identity_key))


def mark_cicada_flag_verified_cached(identity_key: str) -> None:
    if not identity_key:
        return
    from cicada_errors import debug_log

    with _FLAG_VERIFIED_CACHE_LOCK:
        if _FLAG_VERIFIED_CACHE.get(identity_key):
            return
        _FLAG_VERIFIED_CACHE[identity_key] = True
        debug_log(f"[FLAG] cached verified for {identity_key}")


def clear_cicada_flag_verified_cache(identity_key: str | None = None) -> None:
    with _FLAG_VERIFIED_CACHE_LOCK:
        if identity_key is None:
            _FLAG_VERIFIED_CACHE.clear()
        else:
            _FLAG_VERIFIED_CACHE.pop(identity_key, None)


def is_fast_mount_supported(device_key: str) -> bool:
    with _FAST_MOUNT_LOCK:
        return _FAST_MOUNT_SUPPORTED.get(device_key, True)


def mark_fast_mount_unsupported(device_key: str) -> None:
    from cicada_errors import debug_log

    with _FAST_MOUNT_LOCK:
        if _FAST_MOUNT_SUPPORTED.get(device_key) is False:
            return
        _FAST_MOUNT_SUPPORTED[device_key] = False
        debug_log(f"[MOUNT] fast mount disabled for {device_key}")


disable_fast_mount_for_device = mark_fast_mount_unsupported


def is_hidden_ntfs_supported(device_key: str) -> bool:
    with _FAST_MOUNT_LOCK:
        return _HIDDEN_NTFS_SUPPORTED.get(device_key, True)


def mark_hidden_ntfs_unsupported(device_key: str) -> None:
    from cicada_errors import debug_log

    with _FAST_MOUNT_LOCK:
        if _HIDDEN_NTFS_SUPPORTED.get(device_key) is False:
            return
        _HIDDEN_NTFS_SUPPORTED[device_key] = False
        debug_log(
            f"[MOUNT] Cicada Hidden NTFS unsupported for {device_key} "
            "(MbrType 0x17 <-> 0x07 blocked by USB controller)"
        )


HIDDEN_NTFS_UNSUPPORTED_MESSAGE = (
    "Данный USB-накопитель не поддерживает изменение типа раздела 0x17 ↔ 0x07."
)

FAST_MOUNT_UNAVAILABLE_DIAGNOSTIC = (
    "Быстрый доступ к NTFS недоступен для этого USB-накопителя."
)


def is_fast_mount_diagnostic_error(message: str) -> bool:
    text = (message or "").strip()
    return FAST_MOUNT_UNAVAILABLE_DIAGNOSTIC in text


def _is_not_supported_mount_error(exc: BaseException) -> bool:
    text = str(exc)
    return (
        _is_not_supported_access_path_error(text)
        or _is_not_supported_new_drive_letter_error(text)
    )


def _is_not_supported_access_path_error(message: str) -> bool:
    text = message.lower().replace(" ", "")
    return "notsupported" in text and "add-partitionaccesspath" in text


def _is_not_supported_new_drive_letter_error(message: str) -> bool:
    text = message.lower().replace(" ", "")
    return "notsupported" in text and "newdriveletter" in text
IMPORT_TRACE_SLOW_SEC = 2.0


def _import_trace(step: str) -> None:
    from cicada_errors import debug_log

    debug_log(f"[TRACE] {step}")


def _import_trace_run(
    step: str,
    action,
    *,
    slow_threshold: float = IMPORT_TRACE_SLOW_SEC,
):
    from cicada_errors import debug_log

    _import_trace(step)
    started = time.perf_counter()
    try:
        result = action()
    except Exception as exc:
        debug_log(f"[TRACE] {step} exception: {exc}")
        debug_log(traceback.format_exc())
        raise
    elapsed = time.perf_counter() - started
    if elapsed >= slow_threshold:
        debug_log(f"[TRACE][SLOW] {step} {elapsed:.2f} sec")
    return result


class PartitionRevealTimeoutError(RuntimeError):
    def __str__(self) -> str:
        return (
            "НЕ УДАЛОСЬ ОТКРЫТЬ РАЗДЕЛ\n"
            "Операция заняла слишком много времени.\n"
            "Код: CICADA-205"
        )


class PartitionMbrTypeChangeError(RuntimeError):
    def __str__(self) -> str:
        return HIDDEN_NTFS_UNSUPPORTED_MESSAGE


@contextmanager
def ntfs_disk_lock(disk_number: int):
    """Сериализует reveal/hide и доступ к NTFS одного диска между потоками."""
    with _NTFS_DISK_LOCKS_GUARD:
        lock = _NTFS_DISK_LOCKS.setdefault(disk_number, threading.RLock())
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _normalize_disk_signature(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    text = str(value).strip().lower()
    if not text or text == "null":
        return None
    if text.startswith("0x"):
        try:
            return int(text, 16) & 0xFFFFFFFF
        except ValueError:
            return None
    try:
        return int(text) & 0xFFFFFFFF
    except ValueError:
        return None


def _is_cicada_mbr_signature_prefix(signature: int | None) -> bool:
    if signature is None:
        return False
    return (signature & CICADA_MBR_SIGNATURE_PREFIX_MASK) == CICADA_MBR_SIGNATURE_PREFIX


def _is_legacy_cicada_mbr_signature(signature: int | None) -> bool:
    return signature == CICADA_MBR_SIGNATURE_LEGACY


def _format_mbr_signature_hex(signature: int) -> str:
    return f"{signature & 0xFFFFFFFF:08X}"


def generate_cicada_mbr_signature() -> int:
    """Уникальная MBR Signature Cicada: 0x3301 + случайные младшие 16 бит."""
    return CICADA_MBR_SIGNATURE_PREFIX | random.randint(0, 0xFFFF)


def cicada_mbr_signature_display(signature: object | None) -> str:
    normalized = _normalize_disk_signature(signature)
    if normalized is not None and _is_cicada_mbr_signature_prefix(normalized):
        return f"0x{_format_mbr_signature_hex(normalized)}"
    return "0x3301XXXX"


def _log_cicada_signature_kind(signature: object | None) -> None:
    from cicada_errors import debug_log

    normalized = _normalize_disk_signature(signature)
    if normalized is None or not _is_cicada_mbr_signature_prefix(normalized):
        return
    if _is_legacy_cicada_mbr_signature(normalized):
        debug_log("[SCAN] legacy Cicada signature detected")
    else:
        debug_log("[SCAN] unique Cicada signature detected")


def _signature_matches_cicada(signature: object | None, unique_id: object | None) -> bool:
    normalized = _normalize_disk_signature(signature)
    if normalized is not None and _is_cicada_mbr_signature_prefix(normalized):
        return True
    uid = str(unique_id or "").upper().replace("-", "").replace("{", "").replace("}", "")
    if CICADA_MBR_SIGNATURE_LEGACY_HEX in uid:
        return True
    return bool(re.search(r"3301[0-9A-F]{4}", uid))


def _bring_cicada_disk_online_after_collision(
    disk_number: int,
    *,
    signature: object | None = None,
    unique_id: object | None = None,
    log_prefix: str = "[SCAN]",
) -> bool:
    """Снимает Offline после проверки Cicada-сигнатуры (OfflineReason = Collision)."""
    from cicada_errors import debug_log

    if signature is None and unique_id is None:
        info = get_disk_signature(disk_number)
        if not info:
            return False
        signature = info.get("Signature")
        unique_id = info.get("UniqueId")
    if not _signature_matches_cicada(signature, unique_id):
        debug_log(
            f"{log_prefix} disk {disk_number} collision offline skipped: "
            "not Cicada MBR signature"
        )
        return False
    _log_cicada_signature_kind(signature)

    status_script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$d = Get-Disk -Number {disk_number}
if (-not $d) {{ return }}
[PSCustomObject]@{{
    is_offline = [bool]$d.IsOffline
    offline_reason = if ($d.PSObject.Properties.Name -contains 'OfflineReason') {{ [string]$d.OfflineReason }} else {{ $null }}
    operational_status = [string]$d.OperationalStatus
}} | ConvertTo-Json -Compress
"""
    raw = run_powershell(status_script).strip()
    if not raw or raw.lower() == "null":
        return False
    status = json.loads(raw)
    is_offline = bool(status.get("is_offline"))
    offline_reason = str(status.get("offline_reason") or "")
    if not (is_offline and offline_reason == "Collision"):
        return False

    debug_log(
        f"{log_prefix} disk {disk_number} OfflineReason=Collision "
        f"(OperationalStatus={status.get('operational_status')})"
    )
    online_script = rf"""
$ErrorActionPreference = 'Stop'
$d = Get-Disk -Number {disk_number} -ErrorAction Stop
if (-not [bool]$d.IsOffline) {{ 'already_online'; return }}
if ([string]$d.OfflineReason -ne 'Collision') {{ 'not_collision'; return }}
Set-Disk -Number {disk_number} -IsOffline $false
Update-HostStorageCache
'ok'
"""
    result = run_powershell(online_script).strip().lower()
    if result == "ok":
        debug_log(
            f"{log_prefix} disk {disk_number} Set-Disk -IsOffline $false "
            "(OfflineReason=Collision)"
        )
        return True
    debug_log(
        f"{log_prefix} disk {disk_number} collision offline not cleared: "
        f"{result or 'empty'}"
    )
    return False


def resolve_cicada_mbr_collision_offline(
    disk_number: int,
    *,
    signature: object | None = None,
    unique_id: object | None = None,
    log_prefix: str = "[SCAN]",
) -> bool:
    """True если обнаружен Offline/Collision у Cicada-диска (даже если online не удался)."""
    if signature is None and unique_id is None:
        info = get_disk_signature(disk_number)
        if not info:
            return False
        signature = info.get("Signature")
        unique_id = info.get("UniqueId")
    if not _signature_matches_cicada(signature, unique_id):
        return False

    status_script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$d = Get-Disk -Number {disk_number}
if (-not $d) {{ return }}
[PSCustomObject]@{{
    is_offline = [bool]$d.IsOffline
    offline_reason = if ($d.PSObject.Properties.Name -contains 'OfflineReason') {{ [string]$d.OfflineReason }} else {{ $null }}
}} | ConvertTo-Json -Compress
"""
    raw = run_powershell(status_script).strip()
    if not raw or raw.lower() == "null":
        return False
    status = json.loads(raw)
    collision = bool(status.get("is_offline")) and str(status.get("offline_reason") or "") == "Collision"
    if not collision:
        return False
    _bring_cicada_disk_online_after_collision(
        disk_number,
        signature=signature,
        unique_id=unique_id,
        log_prefix=log_prefix,
    )
    return True


def get_disk_signature(disk_number: int) -> dict[str, str | int | None] | None:
    script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$d = Get-Disk -Number {disk_number}
if (-not $d) {{ return }}
[PSCustomObject]@{{
    Number = [int]$d.Number
    Signature = if ($null -eq $d.Signature) {{ $null }} else {{ [string]$d.Signature }}
    UniqueId = [string]$d.UniqueId
}} | ConvertTo-Json -Compress
"""
    raw = run_powershell(script).strip()
    if not raw or raw.lower() == "null":
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        return None
    return {
        "Number": int(data.get("Number", disk_number)),
        "Signature": data.get("Signature"),
        "UniqueId": data.get("UniqueId"),
    }


def is_cicada_signature(disk_number: int) -> bool:
    info = get_disk_signature(disk_number)
    if not info:
        return False
    return _signature_matches_cicada(info.get("Signature"), info.get("UniqueId"))


def stamp_cicada_disk_signature(
    disk_number: int, signature: int | None = None
) -> int:
    """Устанавливает уникальную MBR Disk Signature Cicada USB Boot после Initialize-Disk."""
    from cicada_errors import debug_log

    if signature is None:
        signature = generate_cicada_mbr_signature()
    sig_hex = _format_mbr_signature_hex(signature)
    script = rf"""
$ErrorActionPreference = 'Stop'
Set-Disk -Number {disk_number} -Signature 0x{sig_hex}
Update-HostStorageCache
"""
    try:
        run_powershell(script)
        if _is_legacy_cicada_mbr_signature(signature):
            debug_log(f"[CREATE] disk signature set (legacy reuse): 0x{sig_hex}")
        else:
            debug_log(f"[CREATE] unique Cicada MBR signature set: 0x{sig_hex}")
    except Exception as ps_err:
        debug_log(f"[CREATE] Set-Disk signature failed: {ps_err}")
        run_diskpart(
            f"""select disk {disk_number}
uniqueid disk id={sig_hex}
exit
""",
            strict=False,
        )
        debug_log(f"[CREATE] disk signature set via diskpart: {sig_hex}")
    return signature


def detect_cicada_layout(
    *,
    partition_style: str,
    partition_count: int,
    p1_size: int,
    p1_mbr: int,
    p2_size: int,
    p2_mbr: int,
) -> bool:
    if str(partition_style).upper() != "MBR":
        return False
    if partition_count != 2 or p1_size <= 0 or p2_size <= 0:
        return False
    if p1_size < MIN_CICADA_P1_BYTES:
        return False
    if int(p1_mbr) not in CICADA_P1_MBR_TYPES:
        return False
    if abs(int(p2_size) - BOOT_PARTITION_BYTES) > BOOT_PARTITION_TOLERANCE_BYTES:
        return False
    if int(p2_mbr) not in CICADA_P2_MBR_TYPES:
        return False
    return True


def repair_wrong_cicada_ntfs_hidden_type(disk_number: int) -> bool:
    """Старые флешки: раскрыть скрытый NTFS P1 (0x17) для режима без скрытия."""
    from cicada_errors import debug_log

    if FULL_HIDE_AFTER_CREATE:
        return _repair_wrong_cicada_ntfs_hidden_type_legacy(disk_number)
    try:
        ensure_cicada_partition_visible(disk_number)
        debug_log(f"[SCAN] NTFS partition made visible on disk {disk_number}")
        return True
    except Exception as exc:
        debug_log(f"[SCAN] make NTFS visible failed: {exc}")
        return False


def _repair_wrong_cicada_ntfs_hidden_type_legacy(disk_number: int) -> bool:
    """Исправляет P1 с ошибочным MbrType=17 (десятичный) на скрытый NTFS 0x17."""
    from cicada_errors import debug_log

    script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$p1 = Get-Partition -DiskNumber {disk_number} -PartitionNumber 1
if (-not $p1) {{ 'false'; return }}
if ([int]$p1.MbrType -ne 17) {{ 'false'; return }}
try {{
    Set-Partition -DiskNumber {disk_number} -PartitionNumber 1 -MbrType 0x17 -ErrorAction Stop
    'true'
}} catch {{
    @"
select disk {disk_number}
select partition 1
set id=17 override
attributes partition set hidden
exit
"@ | diskpart | Out-Null
    'true'
}}
"""
    try:
        repaired = run_powershell(script).strip().lower() == "true"
    except Exception as exc:
        debug_log(f"[SCAN] repair wrong NTFS type failed: {exc}")
        return False
    if repaired:
        debug_log(f"[SCAN] repaired wrong NTFS hidden type on disk {disk_number}")
    return repaired


def fast_detect_cicada_disk(disk_number: int) -> bool:
    if not is_cicada_signature(disk_number):
        return False
    script = rf"""
$ErrorActionPreference = 'Stop'
$d = Get-Disk -Number {disk_number} -ErrorAction SilentlyContinue
if (-not $d) {{ 'false'; return }}
$parts = @(Get-Partition -DiskNumber {disk_number} -ErrorAction SilentlyContinue | Sort-Object PartitionNumber)
$p1 = $parts | Where-Object PartitionNumber -eq 1 | Select-Object -First 1
$p2 = $parts | Where-Object PartitionNumber -eq 2 | Select-Object -First 1
if (-not $p1 -or -not $p2) {{ 'false'; return }}
[PSCustomObject]@{{
    PartitionStyle = [string]$d.PartitionStyle
    PartitionCount = [int]$parts.Count
    P1Size = [long]$p1.Size
    P1Mbr = [int]$p1.MbrType
    P2Size = [long]$p2.Size
    P2Mbr = [int]$p2.MbrType
}} | ConvertTo-Json -Compress
"""
    raw = run_powershell(script)
    if not raw or raw.lower() == "false":
        return False
    data = json.loads(raw)
    return detect_cicada_layout(
        partition_style=str(data.get("PartitionStyle", "")),
        partition_count=int(data.get("PartitionCount", 0)),
        p1_size=int(data.get("P1Size", 0)),
        p1_mbr=int(data.get("P1Mbr", 0)),
        p2_size=int(data.get("P2Size", 0)),
        p2_mbr=int(data.get("P2Mbr", 0)),
    )


def list_usb_disks_fast() -> list[UsbDisk]:
    script = rf"""
$BootMb = {BOOT_PARTITION_MB}
$BootTolMb = 256
$MinP1Mb = 2048

function Test-CicadaLayout($disk, $parts) {{
    if ($disk.PartitionStyle -ne 'MBR' -or $parts.Count -lt 2) {{ return $false }}
    $p1 = $parts | Where-Object PartitionNumber -eq 1 | Select-Object -First 1
    $p2 = $parts | Where-Object PartitionNumber -eq 2 | Select-Object -First 1
    if (-not $p1 -or -not $p2) {{ return $false }}
    $p1Mb = [double]$p1.Size / 1MB
    $p2Mb = [double]$p2.Size / 1MB
    $p1Ok = ($p1Mb -gt $MinP1Mb) -and ($p1.MbrType -in @(7, 17, 23))
    $p2Ok = ([math]::Abs($p2Mb - $BootMb) -le $BootTolMb) -and ($p2.MbrType -in @(17, 28))
    return ($p1Ok -and $p2Ok)
}}

$disks = Get-Disk | Where-Object {{
    ($_.BusType -eq 'USB' -or $_.BusType -eq 'SD') -and $_.Size -gt 0
}} | Sort-Object Number

$result = @()
foreach ($d in $disks) {{
    $model = (Get-PhysicalDisk -DeviceNumber $d.Number -ErrorAction SilentlyContinue).FriendlyName
    if (-not $model) {{ $model = $d.FriendlyName }}
    if (-not $model) {{ $model = 'USB Device' }}
    $parts = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | Sort-Object PartitionNumber)
    $p1 = $parts | Where-Object PartitionNumber -eq 1 | Select-Object -First 1
    $p2 = $parts | Where-Object PartitionNumber -eq 2 | Select-Object -First 1
    $result += [PSCustomObject]@{{
        Number = [int]$d.Number
        Model = [string]$model
        Size = [long]$d.Size
        PartitionStyle = [string]$d.PartitionStyle
        PartitionCount = [int]$parts.Count
        P1Size = if ($p1) {{ [long]$p1.Size }} else {{ [long]0 }}
        P1MbrType = if ($p1) {{ [int]$p1.MbrType }} else {{ [int]0 }}
        P1IsHidden = if ($p1) {{ [bool]$p1.IsHidden }} else {{ $false }}
        P2Size = if ($p2) {{ [long]$p2.Size }} else {{ [long]0 }}
        P2MbrType = if ($p2) {{ [int]$p2.MbrType }} else {{ [int]0 }}
        P2IsHidden = if ($p2) {{ [bool]$p2.IsHidden }} else {{ $false }}
        Signature = if ($null -eq $d.Signature) {{ $null }} else {{ [string]$d.Signature }}
        UniqueId = [string]$d.UniqueId
        IsOffline = [bool]$d.IsOffline
        OfflineReason = if ($d.PSObject.Properties.Name -contains 'OfflineReason') {{ [string]$d.OfflineReason }} else {{ $null }}
        OperationalStatus = [string]$d.OperationalStatus
        FastIsCicadaLayout = [bool](Test-CicadaLayout $d $parts)
    }}
}}
$result | ConvertTo-Json -Compress
"""
    raw = run_powershell(script, timeout=120.0)
    if not raw or raw == "null":
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    from cicada_errors import debug_log

    disks: list[UsbDisk] = []
    for item in data:
        signature = item.get("Signature")
        unique_id = item.get("UniqueId")
        layout_ok = bool(item.get("FastIsCicadaLayout"))
        if not layout_ok:
            layout_ok = detect_cicada_layout(
                partition_style=str(item.get("PartitionStyle", "")),
                partition_count=int(item.get("PartitionCount", 0)),
                p1_size=int(item.get("P1Size", 0)),
                p1_mbr=int(item.get("P1MbrType", 0)),
                p2_size=int(item.get("P2Size", 0)),
                p2_mbr=int(item.get("P2MbrType", 0)),
            )
        disk_number = int(item["Number"])
        signature_ok = _signature_matches_cicada(signature, unique_id)
        is_offline = bool(item.get("IsOffline"))
        offline_reason = str(item.get("OfflineReason") or "")
        collision_offline = is_offline and offline_reason == "Collision"
        if signature_ok and collision_offline:
            resolve_cicada_mbr_collision_offline(
                disk_number,
                signature=signature,
                unique_id=unique_id,
                log_prefix="[SCAN]",
            )
            layout_ok = fast_detect_cicada_disk(disk_number)
        if signature_ok and not layout_ok and int(item.get("P1MbrType", 0)) in {
            17,
            23,
        }:
            if repair_wrong_cicada_ntfs_hidden_type(disk_number):
                layout_ok = fast_detect_cicada_disk(disk_number)
        is_cicada = signature_ok and layout_ok
        identity_key = _disk_store_key(
            disk_number,
            unique_id=str(unique_id) if unique_id else None,
            model=str(item["Model"]),
            size_bytes=int(item["Size"]),
        )
        debug_log(
            f"[SCAN] disk {disk_number} signature: "
            f"{signature if signature is not None else '—'} / unique_id={unique_id or '—'}"
        )
        debug_log(f"[SCAN] disk identity key: {identity_key}")
        if signature_ok:
            _log_cicada_signature_kind(signature)
        if collision_offline and not signature_ok:
            debug_log(
                f"[SCAN] disk {disk_number} OfflineReason=Collision "
                f"(OperationalStatus={item.get('OperationalStatus')}) — not Cicada signature"
            )
        debug_log(f"[SCAN] cicada signature: {str(signature_ok).lower()}")
        debug_log(f"[SCAN] cicada layout: {str(layout_ok).lower()}")
        if signature_ok and not layout_ok:
            debug_log(
                f"[SCAN] disk {disk_number} layout details: "
                f"style={item.get('PartitionStyle')} count={item.get('PartitionCount')} "
                f"p1={item.get('P1Size')}/{item.get('P1MbrType')} "
                f"p2={item.get('P2Size')}/{item.get('P2MbrType')}"
            )
        disks.append(
            UsbDisk(
                disk_number,
                str(item["Model"]),
                int(item["Size"]),
                is_cicada=is_cicada,
                cicada_verified=False,
                signature=str(signature) if signature is not None else None,
                unique_id=str(unique_id) if unique_id else None,
                fast_is_cicada_signature=signature_ok,
                fast_is_cicada_layout=layout_ok,
                mbr_collision_offline=signature_ok and collision_offline,
            )
        )
    legacy_sig_disks = [
        d
        for d in disks
        if _is_legacy_cicada_mbr_signature(_normalize_disk_signature(d.signature))
    ]
    if len(legacy_sig_disks) > 1:
        debug_log(
            f"[SCAN] {len(legacy_sig_disks)} USB share legacy MBR signature "
            f"0x{CICADA_MBR_SIGNATURE_LEGACY_HEX}; Windows may Offline duplicates "
            "(each stick is tracked by UniqueId)"
        )
        for disk in legacy_sig_disks:
            debug_log(
                f"[SCAN]   disk {disk.number} model={disk.model!r} "
                f"identity={disk_identity_key(disk)}"
            )
    return disks


def invalidate_usb_scan_cache() -> None:
    global _USB_SCAN_CACHE
    with _USB_SCAN_CACHE_LOCK:
        _USB_SCAN_CACHE = None


def list_usb_disks_fast_cached() -> tuple[list[UsbDisk], bool]:
    """Быстрый список USB. Второй элемент — True, если результат взят из кеша (5 сек)."""
    global _USB_SCAN_CACHE
    now = time.time()
    with _USB_SCAN_CACHE_LOCK:
        if _USB_SCAN_CACHE is not None:
            cached_at, cached_disks = _USB_SCAN_CACHE
            if now - cached_at < _USB_SCAN_CACHE_TTL:
                return ([_copy_usb_disk(disk) for disk in cached_disks], True)
    disks = list_usb_disks_fast()
    with _USB_SCAN_CACHE_LOCK:
        _USB_SCAN_CACHE = (now, disks)
    return ([_copy_usb_disk(disk) for disk in disks], False)


def format_user_error_message(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return "Неизвестная ошибка."
    low = text.lower()
    if "файл уже существует" in low or (
        "already exists" in low and "partition" not in low
    ):
        return (
            "ОБРАЗ УЖЕ СУЩЕСТВУЕТ\n\n"
            "Файл с таким именем уже есть на флешке.\n\n"
            "Переименуйте образ или удалите старый файл.\n\n"
            "Код: CICADA-204"
        )
    if "cicada-103" in low or "файлы сборки недоступны" in low:
        return (
            "ФАЙЛЫ СБОРКИ НЕДОСТУПНЫ\n\n"
            "Не удалось скачать необходимые файлы с GitHub.\n\n"
            "Проверьте ссылку, интернет или положите файлы рядом с программой.\n\n"
            "Код: CICADA-103"
        )
    if "cicada-101" in low or (
        "файлы сборки не найдены" in low and "интернет" in low
    ):
        return (
            "НЕТ ПОДКЛЮЧЕНИЯ К ИНТЕРНЕТУ\n\n"
            "Файлы сборки не найдены локально.\n\n"
            "Положите рядом с программой:\n"
            "- Cicada3301.7z\n"
            "- FAT32.7z\n"
            "- 7z.exe\n\n"
            "или подключите интернет.\n\n"
            "Код: CICADA-101"
        )
    first = text.splitlines()[0].strip()
    low = first.lower()
    if "add-partitionaccesspath" in low or "already in use" in low:
        return "Не удалось назначить букву разделу USB. Закройте программы, использующие флешку."
    if "access is denied" in low or "access denied" in low or "отказано в доступе" in low:
        return "Нет доступа к USB-накопителю."
    if "virtual disk service" in low or "службы виртуальных дисков" in low:
        return "Служба дисков Windows недоступна или занята."
    if "0x17" in text and "0x07" in text:
        return HIDDEN_NTFS_UNSUPPORTED_MESSAGE
    if len(first) > 160:
        return first[:157] + "..."
    return first


def decode_console_output(raw: bytes) -> str:
    for encoding in ("cp866", "cp1251", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


DISKPART_HARD_ERROR_MARKERS = (
    "diskpart has encountered an error",
)

DISKPART_LAYOUT_SUCCESS_MARKERS = (
    "diskpart: очистка диска выполнена успешно",
    "очистка диска выполнена успешно",
    "diskpart succeeded in cleaning the disk",
    "успешно преобразован к формату mbr",
    "successfully converted the selected disk to mbr",
    "diskpart: указанный раздел успешно создан",
    "successfully created the specified partition",
    "успешно отформатировала том",
    "successfully formatted the volume",
)

DISKPART_VDS_WARNING_MARKERS = (
    "диск подключен к сети",
    "the disk is already online",
)

DISKPART_COMPLETION_MARKERS = (
    "завершение работы diskpart",
    "leaving diskpart",
)

DISKPART_VDS_ERROR_MARKERS = (
    "virtual disk service error",
    "ошибка службы виртуальных дисков",
)


@dataclass(frozen=True)
class _DiskpartAnalysis:
    success: bool
    detected_warning: str | None = None
    detected_error: str | None = None


def _diskpart_output_lower(output: str) -> str:
    return output.lower()


def _has_diskpart_layout_success_markers(output: str) -> bool:
    lower = _diskpart_output_lower(output)
    return any(marker in lower for marker in DISKPART_LAYOUT_SUCCESS_MARKERS)


def _extract_diskpart_vds_block(output: str) -> tuple[int, str] | None:
    lower = _diskpart_output_lower(output)
    best_idx = -1
    best_marker = ""
    for marker in DISKPART_VDS_ERROR_MARKERS:
        idx = lower.find(marker)
        if idx >= 0 and (best_idx < 0 or idx < best_idx):
            best_idx = idx
            best_marker = marker
    if best_idx < 0:
        return None
    tail = output[best_idx:]
    block_lines: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            if block_lines:
                break
            continue
        block_lines.append(stripped)
        lower_line = stripped.lower()
        if (
            any(ok in lower_line for ok in ("успешно", "successfully"))
            and "ошибка" not in lower_line
            and "error" not in lower_line
        ):
            break
        if len(block_lines) >= 4:
            break
    return best_idx, block_lines[0] if block_lines else best_marker


def _is_diskpart_vds_online_warning(block: str) -> bool:
    lower = block.lower()
    return any(marker in lower for marker in DISKPART_VDS_WARNING_MARKERS)


def _has_diskpart_progress_after(output: str, position: int) -> bool:
    tail = _diskpart_output_lower(output[position + 1 :])
    progress_markers = (
        "diskpart:",
        "diskpart succeeded",
        "successfully",
        "успешно",
        "завершено (в процентах)",
        "percent completed",
    )
    return any(marker in tail for marker in progress_markers)


def _find_diskpart_post_completion_error(output: str) -> str | None:
    lower = _diskpart_output_lower(output)
    completion_pos = -1
    for marker in DISKPART_COMPLETION_MARKERS:
        idx = lower.rfind(marker)
        if idx > completion_pos:
            completion_pos = idx
    if completion_pos < 0:
        return None
    for line in output[completion_pos:].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        line_lower = stripped.lower()
        if "ошибка" not in line_lower and "error" not in line_lower:
            continue
        if any(marker in line_lower for marker in DISKPART_VDS_ERROR_MARKERS):
            continue
        if _is_diskpart_vds_online_warning(stripped):
            continue
        return stripped
    return None


def _analyze_diskpart_output(output: str, returncode: int) -> _DiskpartAnalysis:
    lower = _diskpart_output_lower(output)
    warnings: list[str] = []
    errors: list[str] = []

    for marker in DISKPART_HARD_ERROR_MARKERS:
        if marker in lower:
            errors.append(marker)

    vds = _extract_diskpart_vds_block(output)
    if vds is not None:
        vds_pos, vds_text = vds
        if _is_diskpart_vds_online_warning(vds_text):
            warnings.append(vds_text)
        elif _has_diskpart_progress_after(output, vds_pos):
            warnings.append(vds_text)
        elif not _has_diskpart_layout_success_markers(output):
            errors.append(vds_text)

    post_completion_error = _find_diskpart_post_completion_error(output)
    if post_completion_error:
        errors.append(post_completion_error)

    if post_completion_error:
        return _DiskpartAnalysis(
            success=False,
            detected_warning="; ".join(warnings) or None,
            detected_error=post_completion_error,
        )

    if _has_diskpart_layout_success_markers(output):
        return _DiskpartAnalysis(
            success=True,
            detected_warning="; ".join(warnings) or None,
            detected_error="; ".join(errors) or None,
        )

    if errors:
        return _DiskpartAnalysis(
            success=False,
            detected_warning="; ".join(warnings) or None,
            detected_error="; ".join(errors),
        )

    if returncode == 0 or "успешно" in lower or "successfully" in lower:
        return _DiskpartAnalysis(
            success=True,
            detected_warning="; ".join(warnings) or None,
        )

    if returncode != 0:
        return _DiskpartAnalysis(
            success=False,
            detected_warning="; ".join(warnings) or None,
            detected_error=f"diskpart exit code {returncode}",
        )

    return _DiskpartAnalysis(
        success=True,
        detected_warning="; ".join(warnings) or None,
    )


def _log_diskpart_result(
    success: bool,
    detected_warning: str | None,
    detected_error: str | None,
) -> None:
    from cicada_errors import debug_log

    debug_log(f"[DISKPART] success={str(success).lower()}")
    if detected_warning:
        debug_log(f"[DISKPART] detected_warning={detected_warning}")
    if detected_error:
        debug_log(f"[DISKPART] detected_error={detected_error}")


def _verify_cicada_layout_partitions(disk_number: int) -> bool:
    """Проверка Cicada-разметки: раздел 1 NTFS + раздел 2 FAT32."""
    from cicada_errors import debug_log

    script = rf"""
$ErrorActionPreference = 'Stop'
$parts = @(Get-Partition -DiskNumber {disk_number} -ErrorAction Stop | Sort-Object PartitionNumber)
if ($parts.Count -lt 2) {{
    [PSCustomObject]@{{
        ok = $false
        reason = 'partition_count'
        count = $parts.Count
    }} | ConvertTo-Json -Compress
    return
}}
$fs1 = $null
$fs2 = $null
foreach ($n in 1, 2) {{
    $p = $parts | Where-Object {{ $_.PartitionNumber -eq $n }} | Select-Object -First 1
    if (-not $p) {{
        [PSCustomObject]@{{
            ok = $false
            reason = 'missing_partition'
            partition = $n
        }} | ConvertTo-Json -Compress
        return
    }}
    $v = Get-Volume -Partition $p -ErrorAction SilentlyContinue
    if (-not $v -or -not $v.FileSystem) {{
        [PSCustomObject]@{{
            ok = $false
            reason = 'no_volume'
            partition = $n
        }} | ConvertTo-Json -Compress
        return
    }}
    if ($n -eq 1) {{ $fs1 = [string]$v.FileSystem }}
    if ($n -eq 2) {{ $fs2 = [string]$v.FileSystem }}
}}
[PSCustomObject]@{{
    ok = ($fs1 -eq 'NTFS' -and $fs2 -eq 'FAT32')
    fs1 = $fs1
    fs2 = $fs2
}} | ConvertTo-Json -Compress
"""
    try:
        raw = run_powershell(script)
        data = json.loads(raw)
        ok = bool(data.get("ok"))
        debug_log(
            f"[DISKPART] layout_verify disk={disk_number} ok={str(ok).lower()} "
            f"fs1={data.get('fs1')} fs2={data.get('fs2')} reason={data.get('reason')}"
        )
        return ok
    except Exception as exc:
        debug_log(f"[DISKPART] layout_verify disk={disk_number} failed: {exc}")
        return False


def run_diskpart(
    script: str,
    *,
    strict: bool = True,
    timeout: float | None = None,
    verify_layout_disk: int | None = None,
) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
        newline="\r\n",
        dir=cicada_work_temp_dir(create=True),
    ) as tmp:
        tmp.write(script)
        script_path = tmp.name
    try:
        try:
            result = subprocess.run(
                ["diskpart", "/s", script_path],
                capture_output=True,
                timeout=timeout,
                **_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("diskpart operation timed out") from exc
        output = decode_console_output(result.stdout + result.stderr)
        lower = _diskpart_output_lower(output)
        if "помощь по команде online disk" in lower or "help for the online disk" in lower:
            _log_diskpart_result(False, None, "invalid online disk command in script")
            if strict:
                raise RuntimeError(
                    "diskpart: неверная команда в скрипте разметки.\n" + output.strip()
                )
            return output

        analysis = _analyze_diskpart_output(output, result.returncode)
        success = analysis.success
        detected_warning = analysis.detected_warning
        detected_error = analysis.detected_error

        if verify_layout_disk is not None and _verify_cicada_layout_partitions(
            verify_layout_disk
        ):
            if not success and detected_error:
                detected_warning = "; ".join(
                    part
                    for part in (detected_warning, detected_error)
                    if part
                ) or None
                detected_error = None
            success = True

        _log_diskpart_result(success, detected_warning, detected_error)

        if not success and strict:
            raise RuntimeError(
                detected_error or output.strip() or "diskpart завершился с ошибкой"
            )
        return output
    finally:
        os.unlink(script_path)


DRIVE_LETTER_POOL = "ZYXWVUTSRQPONMLKJIHG"


def _is_letter_in_use_error(message: str) -> bool:
    low = message.lower()
    return (
        "already in use" in low
        or "requested access path is already in use" in low
        or "storagewmi 42002" in low
    )


def get_used_drive_letters() -> set[str]:
    script = r"""
$used = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
Get-Volume -ErrorAction SilentlyContinue |
    Where-Object DriveLetter |
    ForEach-Object { [void]$used.Add([string]$_.DriveLetter) }
Get-Partition -ErrorAction SilentlyContinue |
    Where-Object DriveLetter |
    ForEach-Object { [void]$used.Add([string]$_.DriveLetter) }
@($used) | ConvertTo-Json -Compress
"""
    raw = run_powershell(script)
    if not raw or raw == "null":
        return set()
    data = json.loads(raw)
    if isinstance(data, str):
        return {data.upper()}
    if isinstance(data, list):
        return {str(item).upper() for item in data if item}
    return set()


def get_free_drive_letter() -> str:
    used = get_used_drive_letters()
    for letter in DRIVE_LETTER_POOL:
        if letter not in used:
            return letter
    raise RuntimeError("Нет свободных букв дисков.")


def _normalize_mbr_type_hex(mbr_type: object) -> str:
    text = str(mbr_type or "").strip()
    if not text or text == "—":
        return ""
    if text.lower().startswith("0x"):
        try:
            return f"0x{int(text, 16):02X}"
        except ValueError:
            return text.upper()
    try:
        return f"0x{int(text, 0):02X}"
    except ValueError:
        return text.upper()


def _mbr_type_log_label(mbr_type: object) -> str:
    normalized = _normalize_mbr_type_hex(mbr_type)
    if not normalized:
        return "—"
    return normalized[2:].upper().zfill(2)


def _log_powershell_io(label: str, stdout: str, stderr: str) -> None:
    from cicada_errors import debug_log

    if stdout.strip():
        debug_log(f"[MOUNT] {label} stdout: {stdout.strip()}")
    if stderr.strip():
        debug_log(f"[MOUNT] {label} stderr: {stderr.strip()}")
    if not stdout.strip() and not stderr.strip():
        debug_log(f"[MOUNT] {label} (no output)")


def _parse_disk_partition_diagnostics_json(raw: str) -> dict[str, object]:
    if not raw or raw.lower() == "null":
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _query_disk_partition_health_flags(
    disk_number: int, partition_number: int
) -> dict[str, object]:
    script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$d = Get-Disk -Number {disk_number}
$p = Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number}
$partitionReadOnly = $null
if ($p -and ($p.PSObject.Properties.Name -contains 'IsReadOnly')) {{
    $partitionReadOnly = [bool]$p.IsReadOnly
}}
[PSCustomObject]@{{
    disk_read_only = if ($d) {{ [bool]$d.IsReadOnly }} else {{ $null }}
    disk_offline = if ($d) {{ [bool]$d.IsOffline }} else {{ $null }}
    disk_offline_reason = if ($d -and ($d.PSObject.Properties.Name -contains 'OfflineReason')) {{ [string]$d.OfflineReason }} else {{ $null }}
    disk_operational_status = if ($d) {{ [string]$d.OperationalStatus }} else {{ $null }}
    partition_style = if ($d) {{ [string]$d.PartitionStyle }} else {{ $null }}
    bus_type = if ($d) {{ [string]$d.BusType }} else {{ $null }}
    partition_offset = if ($p) {{ [long]$p.Offset }} else {{ $null }}
    partition_size = if ($p) {{ [long]$p.Size }} else {{ $null }}
    disk_size = if ($d) {{ [long]$d.Size }} else {{ $null }}
    partition_read_only = $partitionReadOnly
}} | ConvertTo-Json -Compress
"""
    stdout, stderr, _ = _run_powershell_capture(script)
    if stderr.strip():
        from cicada_errors import debug_log

        debug_log(f"[MOUNT] diagnostics flags stderr: {stderr.strip()}")
    return _parse_disk_partition_diagnostics_json(stdout)


def _is_disk_or_partition_readonly(diag: dict[str, object]) -> bool:
    return bool(diag.get("disk_read_only")) or bool(diag.get("partition_read_only"))


def _log_disk_partition_diagnostics(
    disk_number: int, partition_number: int
) -> dict[str, object]:
    from cicada_errors import debug_log

    debug_log(
        f"[MOUNT] diagnostics: Get-Disk -Number {disk_number} | Format-List *"
    )
    disk_list_script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$d = Get-Disk -Number {disk_number}
if ($d) {{ (Get-Disk -Number {disk_number} | Format-List * | Out-String).Trim() }}
else {{ '(disk not found)' }}
"""
    stdout, stderr, _ = _run_powershell_capture(disk_list_script)
    if stdout.strip():
        debug_log(f"[MOUNT] Get-Disk Format-List:\n{stdout.strip()}")
    if stderr.strip():
        debug_log(f"[MOUNT] Get-Disk Format-List stderr: {stderr.strip()}")

    debug_log(
        f"[MOUNT] diagnostics: Get-Partition -DiskNumber {disk_number} "
        f"-PartitionNumber {partition_number} | Format-List *"
    )
    part_list_script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$p = Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number}
if ($p) {{
    (Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} |
        Format-List * | Out-String).Trim()
}} else {{ '(partition not found)' }}
"""
    stdout, stderr, _ = _run_powershell_capture(part_list_script)
    if stdout.strip():
        debug_log(f"[MOUNT] Get-Partition Format-List:\n{stdout.strip()}")
    if stderr.strip():
        debug_log(f"[MOUNT] Get-Partition Format-List stderr: {stderr.strip()}")

    diag = _query_disk_partition_health_flags(disk_number, partition_number)
    debug_log(
        "[MOUNT] diagnostics summary: "
        f"IsReadOnly(disk)={diag.get('disk_read_only')} "
        f"IsReadOnly(partition)={diag.get('partition_read_only')} "
        f"IsOffline={diag.get('disk_offline')} "
        f"OfflineReason={diag.get('disk_offline_reason')} "
        f"OperationalStatus={diag.get('disk_operational_status')} "
        f"PartitionStyle={diag.get('partition_style')} "
        f"BusType={diag.get('bus_type')} "
        f"Offset={diag.get('partition_offset')} "
        f"Size(partition)={diag.get('partition_size')} "
        f"Size(disk)={diag.get('disk_size')}"
    )
    return diag


def _clear_disk_partition_readonly(
    disk_number: int,
    partition_number: int,
    *,
    timeout: float | None = None,
) -> None:
    from cicada_errors import debug_log

    debug_log("[MOUNT] clearing ReadOnly: Set-Disk -IsReadOnly $false")
    ps_script = rf"""
$ErrorActionPreference = 'Stop'
Set-Disk -Number {disk_number} -IsReadOnly $false -ErrorAction Stop
$p = Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} -ErrorAction SilentlyContinue
if ($p -and ($p.PSObject.Properties.Name -contains 'IsReadOnly')) {{
    Set-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} -IsReadOnly $false -ErrorAction Stop
}}
"""
    stdout, stderr, returncode = _run_powershell_capture(ps_script, timeout=timeout)
    _log_powershell_io("Set-Disk/Set-Partition ReadOnly clear", stdout, stderr)
    if returncode != 0:
        debug_log(f"[MOUNT] ReadOnly clear exit code: {returncode} (continuing with DiskPart)")

    diskpart_script = f"""select disk {disk_number}
attributes disk clear readonly
select partition {partition_number}
attributes partition clear readonly
exit
"""
    debug_log(
        f"[MOUNT] DiskPart ReadOnly clear: select disk {disk_number}; "
        "attributes disk clear readonly; "
        f"select partition {partition_number}; attributes partition clear readonly"
    )
    output = run_diskpart(diskpart_script, strict=False, timeout=timeout)
    if output.strip():
        debug_log(f"[MOUNT] DiskPart ReadOnly clear output:\n{output.strip()}")
    else:
        debug_log("[MOUNT] DiskPart ReadOnly clear output: (empty)")


def _attempt_mbr_type_change_to_visible(
    disk_number: int,
    partition_number: int,
    *,
    timeout: float | None = None,
    from_label: str | None = None,
) -> bool:
    from cicada_errors import debug_log

    if from_label is None:
        before = _query_partition_mount_state(disk_number, partition_number)
        from_label = _mbr_type_log_label(before.get("mbr_type"))

    debug_log(f"[MOUNT] changing type {from_label} -> {VISIBLE_NTFS_TYPE}")

    ps_script = rf"""
$ErrorActionPreference = 'Stop'
Set-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} -MbrType 7 -ErrorAction Stop
"""

    def _powershell_mbr() -> None:
        stdout, stderr, returncode = _run_powershell_capture(ps_script, timeout=timeout)
        _log_powershell_io("Set-Partition -MbrType", stdout, stderr)
        if returncode != 0:
            debug_log(
                f"[MOUNT] Set-Partition exit code: {returncode} "
                "(ignored; verify MbrType)"
            )

    _mount_trace_run("Set-MbrType", _powershell_mbr)

    after_ps = _query_partition_mount_state(disk_number, partition_number)
    if _is_mbr_type_visible(after_ps):
        return True

    still = _mbr_type_log_label(after_ps.get("mbr_type"))
    debug_log(
        f"[MOUNT] Set-Partition MbrType still {still}, trying DiskPart fallback"
    )
    diskpart_script = f"""select disk {disk_number}
select partition {partition_number}
set id={VISIBLE_NTFS_TYPE} override
exit
"""
    debug_log(
        f"[MOUNT] DiskPart: select disk {disk_number}; "
        f"select partition {partition_number}; set id={VISIBLE_NTFS_TYPE} override"
    )

    def _diskpart_mbr() -> None:
        output = run_diskpart(diskpart_script, strict=False, timeout=timeout)
        if output.strip():
            debug_log(f"[MOUNT] DiskPart output:\n{output.strip()}")
        else:
            debug_log("[MOUNT] DiskPart output: (empty)")

    _mount_trace_run("Set-MbrType DiskPart fallback", _diskpart_mbr)

    after_dp = _query_partition_mount_state(disk_number, partition_number)
    debug_log(f"[MOUNT] MbrType after fallback = {after_dp.get('mbr_type')}")
    return _is_mbr_type_visible(after_dp)


def _is_mbr_type_visible(state: dict[str, object]) -> bool:
    return _normalize_mbr_type_hex(state.get("mbr_type")) == VISIBLE_MBR_TYPE_HEX


def _partition_needs_mbr_unhide(state: dict[str, object]) -> bool:
    return _normalize_mbr_type_hex(state.get("mbr_type")) in HIDDEN_MBR_TYPE_HEXES


def _mount_stage_log(stage: str, event: str, started: float | None = None) -> float:
    from cicada_errors import debug_log

    now = time.perf_counter()
    if event == "start":
        debug_log(f"[MOUNT] {stage} start")
        return now
    elapsed = (now - started) if started is not None else 0.0
    debug_log(f"[MOUNT] {stage} end {elapsed:.2f} sec")
    return now


def _mount_trace_run(label: str, action):
    from cicada_errors import debug_log

    _import_trace(f"{label} start")
    started = time.perf_counter()
    try:
        return action()
    except Exception:
        elapsed = time.perf_counter() - started
        _import_trace(f"{label} end {elapsed:.2f} sec")
        raise
    else:
        elapsed = time.perf_counter() - started
        _import_trace(f"{label} end {elapsed:.2f} sec")
        if elapsed >= IMPORT_TRACE_SLOW_SEC:
            debug_log(f"[TRACE][SLOW] {label} {elapsed:.2f} sec")


def _drive_letter_from_state(state: dict[str, object]) -> str | None:
    letter = str(state.get("drive_letter") or "").strip().upper()
    if letter:
        return letter
    for path in state.get("access_paths") or []:
        text = str(path).strip().upper()
        if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
            return text[0]
    return None


def _log_mount_cost_summary(
    *,
    unhide_cost: float,
    assign_cost: float,
    verify_cost: float,
    total_cost: float,
) -> None:
    from cicada_errors import debug_log

    debug_log(f"[MOUNT] unhide cost {unhide_cost:.2f} sec")
    debug_log(f"[MOUNT] assign cost {assign_cost:.2f} sec")
    debug_log(f"[MOUNT] verify cost {verify_cost:.2f} sec")
    debug_log(f"[MOUNT] total cost {total_cost:.2f} sec")


def _query_partition_mount_state(
    disk_number: int, partition_number: int
) -> dict[str, object]:
    script = rf"""
$ErrorActionPreference = 'Stop'
$p = Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} -ErrorAction Stop
$mbrType = if ($null -ne $p.MbrType) {{ ('0x{{0:X}}' -f [int]$p.MbrType) }} else {{ $null }}
$gptType = if ($p.GptType) {{ [string]$p.GptType.Guid }} else {{ $null }}
[PSCustomObject]@{{
    partition_number = [int]$p.PartitionNumber
    type = [string]$p.Type
    mbr_type = $mbrType
    gpt_type = $gptType
    hidden = [bool]$p.IsHidden
    no_default_drive_letter = [bool]$p.NoDefaultDriveLetter
    drive_letter = if ($p.DriveLetter) {{ [string]$p.DriveLetter }} else {{ $null }}
    access_paths = @($p.AccessPaths)
}} | ConvertTo-Json -Compress
"""

    def _run() -> dict[str, object]:
        raw = run_powershell(script)
        return _parse_partition_mount_state_json(raw, partition_number)

    return _mount_trace_run("Get-Partition", _run)


def _parse_partition_mount_state_json(
    raw: str, partition_number: int
) -> dict[str, object]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    access_paths = data.get("access_paths")
    if isinstance(access_paths, str):
        paths = [access_paths] if access_paths else []
    elif isinstance(access_paths, list):
        paths = [str(item) for item in access_paths if item]
    else:
        paths = []
    return {
        "partition_number": int(data.get("partition_number") or partition_number),
        "type": str(data.get("type") or "—"),
        "mbr_type": str(data.get("mbr_type") or "—"),
        "gpt_type": str(data.get("gpt_type") or "—"),
        "hidden": bool(data.get("hidden")),
        "no_default_drive_letter": bool(data.get("no_default_drive_letter")),
        "drive_letter": str(data.get("drive_letter") or ""),
        "access_paths": paths,
    }


def _emit_partition_mount_state_logs(
    state: dict[str, object],
    disk_number: int,
    partition_number: int,
    *,
    phase: str = "",
) -> None:
    from cicada_errors import debug_log

    phase_note = f" ({phase})" if phase else ""
    debug_log(
        f"[MOUNT] Get-Partition disk={disk_number} p={partition_number}{phase_note}: "
        f"PartitionNumber={state.get('partition_number')} "
        f"Type={state.get('type')} "
        f"IsHidden={str(state.get('hidden')).lower()} "
        f"NoDefaultDriveLetter={str(state.get('no_default_drive_letter')).lower()} "
        f"DriveLetter={state.get('drive_letter') or '—'}"
    )
    debug_log(f"[MOUNT] MbrType = {state.get('mbr_type')}")
    debug_log(f"[MOUNT] GptType = {state.get('gpt_type')}")
    debug_log(f"[MOUNT] Type = {state.get('type')}")
    debug_log(f"[MOUNT] IsHidden = {str(state.get('hidden')).lower()}")
    debug_log(
        f"[MOUNT] NoDefaultDriveLetter = "
        f"{str(state.get('no_default_drive_letter')).lower()}"
    )
    debug_log(f"[MOUNT] AccessPaths = {state.get('access_paths')}")


def _log_partition_mount_state(
    disk_number: int,
    partition_number: int,
    *,
    phase: str = "",
    state: dict[str, object] | None = None,
) -> dict[str, object]:
    if state is None:
        state = _query_partition_mount_state(disk_number, partition_number)
    _emit_partition_mount_state_logs(
        state, disk_number, partition_number, phase=phase
    )
    return state


def _verify_partition_unhide_result(
    before: dict[str, object],
    after: dict[str, object],
) -> bool:
    from cicada_errors import debug_log

    def _run() -> bool:
        before_mbr = _normalize_mbr_type_hex(before.get("mbr_type"))
        after_mbr = _normalize_mbr_type_hex(after.get("mbr_type"))
        if before_mbr in HIDDEN_MBR_TYPE_HEXES and after_mbr == VISIBLE_MBR_TYPE_HEX:
            debug_log(f"[MOUNT] unhide: MbrType changed {before_mbr} -> {after_mbr}")
        elif after_mbr == VISIBLE_MBR_TYPE_HEX:
            debug_log(f"[MOUNT] unhide: MbrType already {after_mbr}")
        elif before_mbr != after_mbr:
            debug_log(f"[MOUNT] unhide: MbrType changed {before_mbr} -> {after_mbr}")
        else:
            debug_log(
                f"[MOUNT] unhide: WARNING MbrType still {after_mbr or '—'}, "
                f"expected {VISIBLE_MBR_TYPE_HEX}"
            )

        visible = _is_mbr_type_visible(after)
        if visible:
            debug_log("[MOUNT] effective visibility achieved via MbrType=0x07")
            if after.get("hidden"):
                debug_log(
                    "[MOUNT] unhide: IsHidden still true "
                    "(ignored; MbrType governs visibility)"
                )
        return visible

    return _mount_trace_run("verify partition", _run)


def _verify_partition_prepare_result(
    before: dict[str, object],
    after: dict[str, object],
    *,
    phase: str = "prepare",
) -> None:
    from cicada_errors import debug_log

    if _is_mbr_type_visible(after):
        debug_log(f"[MOUNT] {phase}: MbrType={VISIBLE_MBR_TYPE_HEX}, skipping IsHidden check")
        prop, label = "no_default_drive_letter", "NoDefaultDriveLetter"
        was_set = bool(before.get(prop))
        still_set = bool(after.get(prop))
        if was_set and not still_set:
            debug_log(f"[MOUNT] {phase}: {label} cleared OK")
        elif was_set and still_set:
            debug_log(f"[MOUNT] {phase}: {label} still true (non-blocking)")
        return

    prop, label = "no_default_drive_letter", "NoDefaultDriveLetter"
    was_set = bool(before.get(prop))
    still_set = bool(after.get(prop))
    if was_set and not still_set:
        debug_log(f"[MOUNT] {phase}: {label} cleared OK")
    elif was_set and still_set:
        debug_log(f"[MOUNT] {phase}: {label} still true (non-blocking)")


def _prepare_partition_for_letter_assignment(
    disk_number: int, partition_number: int
) -> None:
    state = _query_partition_mount_state(disk_number, partition_number)
    if _is_mbr_type_visible(state):
        return

    _log_partition_mount_state(
        disk_number, partition_number, phase="before prepare", state=state
    )
    script = rf"""
$ErrorActionPreference = 'Stop'
Set-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} -NoDefaultDriveLetter $false
"""
    run_powershell(script)
    after = _query_partition_mount_state(disk_number, partition_number)
    _log_partition_mount_state(
        disk_number, partition_number, phase="after prepare", state=after
    )
    _verify_partition_prepare_result(state, after, phase="prepare")


def _set_partition_mbr_type_visible(
    disk_number: int,
    partition_number: int,
    *,
    timeout: float | None = None,
    before_state: dict[str, object] | None = None,
) -> None:
    from cicada_errors import debug_log

    before = before_state or _query_partition_mount_state(disk_number, partition_number)
    if _is_mbr_type_visible(before):
        return

    _ensure_cicada_disk_online_before_mount(disk_number)

    from_label = _mbr_type_log_label(before.get("mbr_type"))
    diag = _log_disk_partition_diagnostics(disk_number, partition_number)
    readonly = _is_disk_or_partition_readonly(diag)
    readonly_cleared = False

    if readonly:
        _clear_disk_partition_readonly(
            disk_number, partition_number, timeout=timeout
        )
        readonly_cleared = True

    if _attempt_mbr_type_change_to_visible(
        disk_number,
        partition_number,
        timeout=timeout,
        from_label=from_label,
    ):
        return

    if not readonly_cleared:
        diag_after = _query_disk_partition_health_flags(disk_number, partition_number)
        if _is_disk_or_partition_readonly(diag_after):
            debug_log(
                "[MOUNT] ReadOnly detected after failed MbrType change, "
                "clearing and retrying"
            )
            _clear_disk_partition_readonly(
                disk_number, partition_number, timeout=timeout
            )
            readonly_cleared = True
            if _attempt_mbr_type_change_to_visible(
                disk_number,
                partition_number,
                timeout=timeout,
                from_label=from_label,
            ):
                return

    debug_log(
        "[MOUNT] controller incompatible with Cicada Hidden NTFS "
        "(MbrType 0x17 <-> 0x07 not supported by USB controller)"
    )
    raise PartitionMbrTypeChangeError()


def _unhide_partition_for_mount(
    disk_number: int,
    partition_number: int,
    *,
    timeout: float | None = None,
    before_state: dict[str, object] | None = None,
) -> dict[str, object]:
    before = before_state or _query_partition_mount_state(disk_number, partition_number)
    _log_partition_mount_state(
        disk_number, partition_number, phase="before unhide", state=before
    )
    if _is_mbr_type_visible(before):
        return before

    _set_partition_mbr_type_visible(
        disk_number,
        partition_number,
        timeout=timeout,
        before_state=before,
    )
    after = _query_partition_mount_state(disk_number, partition_number)
    _log_partition_mount_state(
        disk_number, partition_number, phase="after unhide", state=after
    )
    return after


def _assign_partition_letter_diskpart(
    disk_number: int,
    partition_number: int,
    letter: str,
    *,
    log_state: bool = False,
    refresh_cache: bool = True,
) -> str:
    from cicada_errors import debug_log

    assigned = letter.upper()
    if log_state:
        state = _query_partition_mount_state(disk_number, partition_number)
        _log_partition_mount_state(
            disk_number, partition_number, phase="before DiskPart assign", state=state
        )
    debug_log(
        f"[MOUNT] trying DiskPart assign disk={disk_number} "
        f"partition={partition_number} letter={assigned}"
    )
    diskpart_script = f"""select disk {disk_number}
select partition {partition_number}
assign letter={assigned}
exit
"""

    def _diskpart_assign() -> None:
        run_diskpart(diskpart_script)

    try:
        _mount_trace_run("DiskPart assign", _diskpart_assign)
    except RuntimeError:
        debug_log("[MOUNT] DiskPart assign failed")
        raise
    debug_log("[MOUNT] DiskPart assign success")
    if refresh_cache:
        _mount_trace_run(
            "Update-HostStorageCache",
            lambda: run_powershell("Update-HostStorageCache", strict=False),
        )
    return assigned


def get_partition_drive_letter(
    disk_number: int,
    partition_number: int,
    *,
    state: dict[str, object] | None = None,
) -> str | None:
    if state is not None:
        return _drive_letter_from_state(state)

    script = rf"""
$ErrorActionPreference = 'Stop'
$p = Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} -ErrorAction SilentlyContinue
if (-not $p) {{ return }}
if ($p.DriveLetter) {{
    [string]$p.DriveLetter
    return
}}
foreach ($path in @($p.AccessPaths)) {{
    if ($path -match '^([A-Z]):\\?$') {{
        $matches[1]
        return
    }}
}}
"""

    def _run() -> str | None:
        raw = run_powershell(script).strip().strip('"')
        return raw.upper() if raw else None

    return _mount_trace_run("verify drive letter", _run)


def _set_partition_access_letter(
    disk_number: int, partition_number: int, letter: str
) -> str:
    state = _query_partition_mount_state(disk_number, partition_number)
    existing = _drive_letter_from_state(state)
    if existing:
        return existing

    _prepare_partition_for_letter_assignment(disk_number, partition_number)

    assign_script = rf"""
$ErrorActionPreference = 'Stop'
$p = Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} -ErrorAction Stop
if ($p.DriveLetter) {{ return }}
foreach ($path in @($p.AccessPaths)) {{
    if ($path -match '^([A-Z]):\\?$') {{
        return
    }}
}}
Add-PartitionAccessPath -DiskNumber {disk_number} -PartitionNumber {partition_number} -AccessPath "{letter}:"
"""
    try:
        run_powershell(assign_script)
        return letter.upper()
    except RuntimeError as exc:
        err_text = str(exc)
        if _is_letter_in_use_error(err_text):
            retry_state = _query_partition_mount_state(disk_number, partition_number)
            existing = _drive_letter_from_state(retry_state)
            if existing:
                return existing
            raise
        if _is_not_supported_access_path_error(err_text):
            return _assign_partition_letter_diskpart(
                disk_number, partition_number, letter, refresh_cache=False
            )
        raise


def assign_free_letter(disk_number: int, partition_number: int) -> str:
    state = _query_partition_mount_state(disk_number, partition_number)
    existing = _drive_letter_from_state(state)
    if existing:
        return existing
    if not _is_mbr_type_visible(state):
        raise PartitionMbrTypeChangeError()

    used = get_used_drive_letters()
    for letter in DRIVE_LETTER_POOL:
        if letter in used:
            continue
        try:
            return _set_partition_access_letter(disk_number, partition_number, letter)
        except RuntimeError as exc:
            if _is_letter_in_use_error(str(exc)):
                used.add(letter)
                continue
            raise

    raise RuntimeError("Нет свободных букв дисков.")


def perf_log(operation: str, started: float, *, prefix: str = "PERF") -> float:
    from cicada_errors import debug_log

    elapsed = time.perf_counter() - started
    debug_log(f"[{prefix}] {operation} {elapsed:.2f} sec")
    if elapsed > PERF_SLOW_SEC:
        debug_log(f"[{prefix}][SLOW] {operation} took {elapsed:.2f} sec")
    return elapsed


@dataclass
class _NtfsMountHandle:
    disk_number: int
    mount_path: Path
    letter: str
    needs_hide: bool
    opened_by_us: bool = True
    partition_number: int = 1
    assigned_letter: bool = False
    was_hidden: bool = False
    original_mbr_type: str | None = None
    used_fast_path: bool = False


def _build_fast_open_ntfs_script(disk_number: int, partition_number: int) -> str:
    pool = DRIVE_LETTER_POOL
    return rf"""
$ErrorActionPreference = 'Stop'
$dn = {disk_number}
$pn = {partition_number}
$letterPool = '{pool}'

function Get-PartLetter($Partition) {{
    if ($Partition.DriveLetter) {{
        return ([string]$Partition.DriveLetter).ToUpper()
    }}
    foreach ($path in @($Partition.AccessPaths)) {{
        if ($path -match '^([A-Z]):\\?$') {{
            return $matches[1].ToUpper()
        }}
    }}
    return $null
}}

$p = Get-Partition -DiskNumber $dn -PartitionNumber $pn -ErrorAction Stop
$originalMbrType = ('{{0:X}}' -f [int]$p.MbrType)
$wasHidden = ($p.MbrType -eq 0x17 -or $p.MbrType -eq 23)
$assignedLetter = $false
$letter = Get-PartLetter $p

if (-not $wasHidden -and $letter) {{
    [PSCustomObject]@{{
        letter = $letter
        was_hidden = $false
        assigned_letter = $false
        original_mbr_type = $originalMbrType
        access_path = ('{{0}}:\' -f $letter)
    }} | ConvertTo-Json -Compress
    return
}}

if ($wasHidden) {{
    $p = Get-Partition -DiskNumber $dn -PartitionNumber $pn -ErrorAction Stop
    $letter = Get-PartLetter $p
}}

if (-not $letter) {{
    $used = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    Get-Volume -ErrorAction SilentlyContinue |
        Where-Object {{ $_.DriveLetter }} |
        ForEach-Object {{ [void]$used.Add(([string]$_.DriveLetter).ToUpper()) }}
    Get-Partition -ErrorAction SilentlyContinue |
        Where-Object {{ $_.DriveLetter }} |
        ForEach-Object {{ [void]$used.Add(([string]$_.DriveLetter).ToUpper()) }}

    foreach ($ch in $letterPool.ToCharArray()) {{
        $candidate = [string]$ch
        if ($used.Contains($candidate)) {{ continue }}
        $accessPath = "$candidate`:"
        try {{
            Add-PartitionAccessPath -DiskNumber $dn -PartitionNumber $pn -AccessPath $accessPath -ErrorAction Stop
            $letter = $candidate
            $assignedLetter = $true
            break
        }} catch {{
            $errMsg = $_.Exception.Message
            if ($errMsg -match 'already in use|42002|Requested access path') {{
                $pCheck = Get-Partition -DiskNumber $dn -PartitionNumber $pn -ErrorAction Stop
                $existingLetter = Get-PartLetter $pCheck
                if ($existingLetter -eq $candidate) {{
                    $letter = $candidate
                    $assignedLetter = $false
                    break
                }}
                [void]$used.Add($candidate)
                continue
            }}
            if ($errMsg -match 'Not Supported') {{
                throw [System.InvalidOperationException]::new(
                    "Add-PartitionAccessPath Not Supported; use DiskPart assign"
                )
            }}
            throw
        }}
    }}
    if (-not $letter) {{
        throw [System.InvalidOperationException]::new('No free drive letters.')
    }}
    $p = Get-Partition -DiskNumber $dn -PartitionNumber $pn -ErrorAction Stop
    $confirmed = Get-PartLetter $p
    if ($confirmed) {{ $letter = $confirmed }}
}}

[PSCustomObject]@{{
    letter = $letter
    was_hidden = $wasHidden
    assigned_letter = $assignedLetter
    original_mbr_type = $originalMbrType
    access_path = ('{{0}}:\' -f $letter)
}} | ConvertTo-Json -Compress
"""


def _assign_free_letter_diskpart_fallback(
    disk_number: int,
    partition_number: int,
    *,
    stage_log: bool = True,
) -> str:
    from cicada_errors import debug_log

    state = _query_partition_mount_state(disk_number, partition_number)
    if not _is_mbr_type_visible(state):
        raise PartitionMbrTypeChangeError()

    assign_started = _mount_stage_log("assign", "start") if stage_log else None
    try:
        used = get_used_drive_letters()
        for letter in DRIVE_LETTER_POOL:
            if letter in used:
                continue
            try:
                return _assign_partition_letter_diskpart(
                    disk_number,
                    partition_number,
                    letter,
                    log_state=False,
                    refresh_cache=False,
                )
            except RuntimeError as exc:
                if _is_letter_in_use_error(str(exc)):
                    used.add(letter)
                    continue
                debug_log(f"[MOUNT] DiskPart assign failed for {letter}: {exc}")
                raise
        raise RuntimeError("Нет свободных букв дисков.")
    finally:
        if stage_log and assign_started is not None:
            _mount_stage_log("assign", "end", assign_started)


def _ensure_cicada_disk_online_before_mount(disk_number: int) -> None:
    """Online Cicada-диск, если Windows перевёл его в Offline (Collision)."""
    resolve_cicada_mbr_collision_offline(disk_number, log_prefix="[MOUNT]")


def open_ntfs_mount_fast(
    disk_number: int,
    partition_number: int = 1,
    *,
    timeout: float | None = None,
) -> _NtfsMountHandle:
    """Открыть NTFS-раздел одним PowerShell-вызовом (без folder mount)."""
    from cicada_errors import debug_log

    mount_started = time.perf_counter()
    unhide_cost = 0.0
    assign_cost = 0.0
    verify_cost = 0.0

    _ensure_cicada_disk_online_before_mount(disk_number)

    before_state = _query_partition_mount_state(disk_number, partition_number)
    was_hidden = _partition_needs_mbr_unhide(before_state)
    original_mbr_type = _normalize_mbr_type_hex(before_state.get("mbr_type")).replace(
        "0x", "", 1
    )
    letter = _drive_letter_from_state(before_state)
    assigned_letter = False
    data: dict[str, object] = {}

    if was_hidden:
        unhide_started = _mount_stage_log("unhide", "start")
        after_unhide = _unhide_partition_for_mount(
            disk_number,
            partition_number,
            timeout=timeout,
            before_state=before_state,
        )
        unhide_cost = time.perf_counter() - unhide_started
        _mount_stage_log("unhide", "end", unhide_started)

        verify_started = _mount_stage_log("verify", "start")
        visible = _verify_partition_unhide_result(before_state, after_unhide)
        verify_cost += time.perf_counter() - verify_started
        _mount_stage_log("verify", "end", verify_started)
        if not visible:
            raise PartitionMbrTypeChangeError()

        letter = _drive_letter_from_state(after_unhide)
        if not letter:
            assign_started = _mount_stage_log("assign", "start")
            letter = _assign_free_letter_diskpart_fallback(
                disk_number, partition_number, stage_log=False
            )
            assign_cost += time.perf_counter() - assign_started
            _mount_stage_log("assign", "end", assign_started)
            assigned_letter = True

    if not letter:
        if was_hidden:
            current_state = _query_partition_mount_state(disk_number, partition_number)
            if not _is_mbr_type_visible(current_state):
                raise PartitionMbrTypeChangeError()
        script = _build_fast_open_ntfs_script(disk_number, partition_number)
        assign_started = _mount_stage_log("assign", "start")
        try:
            raw = run_powershell(script, timeout=timeout)
        except RuntimeError as exc:
            if _is_not_supported_access_path_error(str(exc)):
                letter = _assign_free_letter_diskpart_fallback(
                    disk_number, partition_number, stage_log=False
                )
                assigned_letter = True
            else:
                _mount_stage_log("assign", "end", assign_started)
                raise
        else:
            data = json.loads(raw)
            if not data.get("letter"):
                letter = _assign_free_letter_diskpart_fallback(
                    disk_number, partition_number, stage_log=False
                )
                assigned_letter = True
            else:
                letter = str(data["letter"]).upper()
                assigned_letter = bool(data.get("assigned_letter"))
        assign_cost += time.perf_counter() - assign_started
        _mount_stage_log("assign", "end", assign_started)

    if not letter:
        raise RuntimeError("Не удалось назначить букву NTFS-разделу")

    letter = str(letter).upper()

    total_cost = time.perf_counter() - mount_started
    _log_mount_cost_summary(
        unhide_cost=unhide_cost,
        assign_cost=assign_cost,
        verify_cost=verify_cost,
        total_cost=total_cost,
    )
    perf_log("fast open ntfs", mount_started)
    opened_by_us = was_hidden or assigned_letter
    return _NtfsMountHandle(
        disk_number=disk_number,
        mount_path=Path(f"{letter}:\\"),
        letter=letter,
        needs_hide=was_hidden,
        opened_by_us=opened_by_us,
        partition_number=partition_number,
        assigned_letter=assigned_letter,
        was_hidden=was_hidden,
        original_mbr_type=original_mbr_type or str(data.get("original_mbr_type") or ""),
        used_fast_path=True,
    )


def close_ntfs_mount_fast(
    handle: _NtfsMountHandle,
    *,
    timeout: float | None = None,
) -> None:
    """Закрыть NTFS-раздел одним PowerShell-вызовом."""
    from cicada_errors import debug_log

    if not FULL_HIDE_AFTER_CREATE:
        debug_log("[MOUNT] visible mode: close skipped")
        return
    if not handle.assigned_letter and not handle.was_hidden:
        return
    started = time.perf_counter()
    dn = handle.disk_number
    pn = handle.partition_number
    letter = handle.letter.upper()
    steps: list[str] = ["$ErrorActionPreference = 'Stop'"]
    if handle.assigned_letter:
        steps.append(
            f'Remove-PartitionAccessPath -DiskNumber {dn} -PartitionNumber {pn} '
            f'-AccessPath "{letter}:" -ErrorAction SilentlyContinue'
        )
    if handle.was_hidden and FULL_HIDE_AFTER_CREATE:
        debug_log("[MOUNT] changing type 07 -> 17")
        steps.append(
            f"""try {{
    Set-Partition -DiskNumber {dn} -PartitionNumber {pn} -MbrType 0x17 -ErrorAction Stop
}} catch {{
    @"
select disk {dn}
select partition {pn}
set id=17 override
exit
"@ | diskpart | Out-Null
}}"""
        )
    run_powershell("\n".join(steps), strict=False, timeout=timeout)
    _import_trace("close_ntfs powershell returned")
    perf_log("fast close ntfs", started)
    _import_trace("close_ntfs perf_log done")


def _parse_access_paths_json(raw: str) -> list[str]:
    if not raw or raw == "null":
        return []
    data = json.loads(raw)
    if isinstance(data, str):
        return [data] if data else []
    if isinstance(data, list):
        return [str(item) for item in data if item]
    return []


def _get_partition_access_paths(disk_number: int, partition_number: int) -> list[str]:
    script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$p = Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number}
if (-not $p) {{ '[]' }}
else {{ @($p.AccessPaths) | ConvertTo-Json -Compress }}
"""
    return _parse_access_paths_json(run_powershell(script))


def _remove_partition_access_path(
    disk_number: int, partition_number: int, access_path: str
) -> None:
    escaped = access_path.replace("`", "``").replace('"', '`"')
    script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
Remove-PartitionAccessPath -DiskNumber {disk_number} -PartitionNumber {partition_number} -AccessPath "{escaped}"
"""
    run_powershell(script, strict=False)


def _ensure_partition_visible(
    disk_number: int,
    *,
    timeout: float | None = None,
) -> None:
    deadline = time.monotonic() + timeout if timeout is not None else None
    _ensure_cicada_disk_online_before_mount(disk_number)
    before_state = _query_partition_mount_state(disk_number, 1)
    if _is_mbr_type_visible(before_state):
        return
    if not _partition_needs_mbr_unhide(before_state):
        return
    try:
        unhide_started = _mount_stage_log("unhide", "start")
        after_state = _unhide_partition_for_mount(
            disk_number,
            1,
            timeout=_remaining_timeout(deadline),
            before_state=before_state,
        )
        _mount_stage_log("unhide", "end", unhide_started)

        verify_started = _mount_stage_log("verify", "start")
        visible = _verify_partition_unhide_result(before_state, after_state)
        _mount_stage_log("verify", "end", verify_started)
        if not visible:
            raise PartitionMbrTypeChangeError()

        if not _drive_letter_from_state(after_state):
            _assign_free_letter_diskpart_fallback(disk_number, 1)
    except PartitionMbrTypeChangeError:
        raise
    except PartitionRevealTimeoutError:
        raise
    except RuntimeError as exc:
        if deadline is not None and (
            time.monotonic() >= deadline or "timed out" in str(exc).lower()
        ):
            raise PartitionRevealTimeoutError() from exc
        raise


def _ensure_partition_hidden(disk_number: int) -> None:
    letter = get_partition_drive_letter(disk_number, 1)
    for access_path in _get_partition_access_paths(disk_number, 1):
        _remove_partition_access_path(disk_number, 1, access_path)
    if letter:
        _remove_partition_access_path(disk_number, 1, f"{letter.upper()}:")
    remove_letter = f"remove letter={letter}" if letter else "remove letter=*"
    script = rf"""
$ErrorActionPreference = 'Stop'
$dn = {disk_number}
Set-Partition -DiskNumber $dn -PartitionNumber 1 -IsHidden $true
Set-Partition -DiskNumber $dn -PartitionNumber 1 -MbrType 0x17
Update-HostStorageCache
"""
    try:
        run_powershell(script)
    except Exception:
        run_diskpart(
            f"""select disk {disk_number}
select partition 1
{remove_letter}
set id={HIDDEN_PARTITION_TYPE} override
attributes partition set hidden
exit
""",
            strict=False,
        )


def legacy_open_ntfs_mount(
    disk_number: int,
    *,
    timeout: float | None = None,
) -> _NtfsMountHandle:
    letter = get_partition_drive_letter(disk_number, 1)
    needs_hide = is_partition_hidden(disk_number, 1)

    if letter and not needs_hide:
        return _NtfsMountHandle(
            disk_number,
            Path(f"{letter.upper()}:\\"),
            letter.upper(),
            False,
            opened_by_us=False,
        )

    if needs_hide:
        _ensure_partition_visible(disk_number, timeout=timeout)

    letter = get_partition_drive_letter(disk_number, 1)
    if not letter:
        letter = assign_free_letter(disk_number, 1)

    return _NtfsMountHandle(
        disk_number,
        Path(f"{letter.upper()}:\\"),
        letter.upper(),
        needs_hide,
        opened_by_us=True,
        was_hidden=needs_hide,
        assigned_letter=True,
    )


def _try_legacy_ntfs_mount(
    disk_number: int,
    *,
    timeout: float | None = None,
) -> _NtfsMountHandle:
    from cicada_errors import debug_log

    debug_log("[MOUNT] switching to legacy")
    try:
        handle = legacy_open_ntfs_mount(disk_number, timeout=timeout)
        debug_log("[MOUNT] legacy mount success")
        return handle
    except PartitionMbrTypeChangeError:
        raise
    except Exception as exc:
        debug_log("[MOUNT] legacy mount failed")
        debug_log(f"[MOUNT] legacy open error: {exc}")
        raise RuntimeError(f"Не удалось открыть NTFS-раздел: {exc}") from exc


def _try_direct_ntfs_mount(
    disk_number: int,
    partition_number: int = 1,
) -> _NtfsMountHandle | None:
    """Прямой доступ по букве диска — без unhide/rehide и diskpart."""
    from cicada_errors import debug_log

    _ensure_cicada_disk_online_before_mount(disk_number)
    if FULL_HIDE_AFTER_CREATE and is_partition_hidden(disk_number, partition_number):
        return None
    letter = get_partition_drive_letter(disk_number, partition_number)
    if not letter:
        return None
    mount_path = Path(f"{letter.upper()}:\\")
    if not mount_path.is_dir():
        return None
    debug_log(f"[MOUNT] direct access via {letter}:\\ (no reveal)")
    return _NtfsMountHandle(
        disk_number=disk_number,
        mount_path=mount_path,
        letter=letter.upper(),
        needs_hide=False,
        opened_by_us=False,
        partition_number=partition_number,
        used_fast_path=False,
    )


def _open_ntfs_mount(
    disk_number: int,
    *,
    timeout: float | None = None,
    fast_only: bool = False,
    device_key: str | None = None,
) -> _NtfsMountHandle:
    from cicada_errors import debug_log

    _ = fast_only  # legacy fallback is always used when fast mount is unavailable
    legacy_attempted = False

    direct = _try_direct_ntfs_mount(disk_number, 1)
    if direct is not None:
        return direct

    def _legacy_once() -> _NtfsMountHandle:
        nonlocal legacy_attempted
        if legacy_attempted:
            raise RuntimeError(
                "Не удалось открыть NTFS-раздел: повторная попытка legacy mount запрещена"
            )
        legacy_attempted = True
        try:
            return _try_legacy_ntfs_mount(disk_number, timeout=timeout)
        except PartitionMbrTypeChangeError:
            if device_key:
                mark_hidden_ntfs_unsupported(device_key)
            raise

    if device_key and not is_fast_mount_supported(device_key):
        return _legacy_once()

    try:
        return open_ntfs_mount_fast(disk_number, 1, timeout=timeout)
    except PartitionMbrTypeChangeError:
        if device_key:
            mark_hidden_ntfs_unsupported(device_key)
        raise
    except Exception as exc:
        if device_key and _is_not_supported_mount_error(exc):
            disable_fast_mount_for_device(device_key)
        debug_log("[MOUNT] fast failed")
        debug_log(f"[MOUNT] fast open error: {exc}")
        return _legacy_once()


def legacy_close_ntfs_mount(
    handle: _NtfsMountHandle,
    *,
    timeout: float | None = None,
) -> None:
    if not FULL_HIDE_AFTER_CREATE:
        from cicada_errors import debug_log

        debug_log("[MOUNT] visible mode: close skipped")
        return
    if not handle.opened_by_us:
        return
    try:
        _remove_partition_access_path(
            handle.disk_number, handle.partition_number, f"{handle.letter}:"
        )
    except Exception:
        pass

    if handle.needs_hide and FULL_HIDE_AFTER_CREATE:
        try:
            _ensure_partition_hidden(handle.disk_number)
        except Exception:
            pass


def _close_ntfs_mount(
    handle: _NtfsMountHandle,
    *,
    timeout: float | None = None,
) -> None:
    if not FULL_HIDE_AFTER_CREATE:
        from cicada_errors import debug_log

        debug_log("[MOUNT] visible mode: close skipped")
        return
    if not handle.opened_by_us:
        return
    if handle.used_fast_path:
        try:
            close_ntfs_mount_fast(handle, timeout=timeout)
            return
        except Exception as exc:
            from cicada_errors import debug_log

            debug_log("[MOUNT] fast close failed, fallback to legacy")
            debug_log(traceback.format_exc())
            debug_log(f"[MOUNT] fast close error: {exc}")
    legacy_close_ntfs_mount(handle, timeout=timeout)


def _close_ntfs_mount_timed(
    handle: _NtfsMountHandle,
    *,
    timeout: float = MOUNT_CLOSE_TIMEOUT_SEC,
) -> bool:
    """Закрыть NTFS с таймаутом PowerShell (без nested thread pool)."""
    from cicada_errors import debug_log

    if not handle.opened_by_us:
        _import_trace("close_ntfs skipped (not opened by us)")
        return True

    def _do_close() -> None:
        _close_ntfs_mount(handle, timeout=timeout)

    try:
        _import_trace_run("close_ntfs_mount", _do_close)
        return True
    except subprocess.TimeoutExpired:
        debug_log(f"[MOUNT] close ntfs timed out after {timeout:.0f}s")
        return False
    except RuntimeError as exc:
        if "timed out" in str(exc).lower():
            debug_log(f"[MOUNT] close ntfs timed out after {timeout:.0f}s")
            return False
        debug_log(f"[MOUNT] close ntfs failed: {exc}")
        debug_log(traceback.format_exc())
        return False
    except Exception as exc:
        debug_log(f"[MOUNT] close ntfs failed: {exc}")
        debug_log(traceback.format_exc())
        return False


def ensure_partition_letters(disk_number: int) -> dict[int, str]:
    """Назначить буквы разделам 1 и 2 (скрытый FAT32 тоже нужен для распаковки)."""
    return {
        1: assign_free_letter(disk_number, 1),
        2: assign_free_letter(disk_number, 2),
    }


def get_partition_letters(disk_number: int) -> dict[int, str]:
    script = rf"""
Get-Partition -DiskNumber {disk_number} |
    Where-Object {{ $_.DriveLetter }} |
    Sort-Object PartitionNumber |
    ForEach-Object {{
        [PSCustomObject]@{{
            Partition = [int]$_.PartitionNumber
            Letter = [string]$_.DriveLetter
        }}
    }} | ConvertTo-Json -Compress
"""
    raw = run_powershell(script)
    if not raw:
        return {}
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return {int(item["Partition"]): item["Letter"] for item in data}


def wait_for_letters(
    disk_number: int,
    timeout: float = 12.0,
    *,
    log: callable | None = None,
) -> dict[int, str]:
    deadline = time.time() + timeout
    last_status = time.time()
    while time.time() < deadline:
        letters = get_partition_letters(disk_number)
        if 1 in letters and 2 in letters:
            return letters
        now = time.time()
        if log and now - last_status >= 3.0:
            log("Ожидание назначения букв разделам...")
            last_status = now
        time.sleep(1)
    if log:
        log("Таймаут ожидания букв — назначение вручную...")
    return ensure_partition_letters(disk_number)


IMAGE_EXTENSIONS = {".iso", ".img", ".wim", ".esd", ".vhd", ".vhdx", ".dmg"}
DELETABLE_IMAGE_EXTENSIONS = {".iso", ".img", ".wim", ".vhd", ".vhdx"}
IMAGE_CATEGORIES = ("WINDOWS", "LINUX", "WINPE")
IMPORT_TEMP_SUFFIX = ".part"
WINDOWS_INSTALL_DEST_NAME = "Windows.iso"
_WINDOWS_INSTALL_SUBFOLDERS = frozenset({
    "WIN10",
    "WIN11",
    "WIN7",
    "WIN8",
    "VISTA",
    "XP",
    "WINXP",
    "SVR2022",
    "SVR2019",
    "SVR2016",
    "SVR2012",
    "SVR2K8R2",
})

_PARTITION_STATS_CACHE: dict[str, dict[str, float | int]] = {}
_PARTITION_STATS_CACHE_LOCK = threading.Lock()
_DISK_IMAGES_CACHE: dict[str, list[ImageEntry]] = {}
_DISK_IMAGES_CACHE_LOCK = threading.Lock()


def _disk_cache_key(disk_number: int) -> str:
    return f"d{disk_number}"


def disk_identity_key(
    disk: UsbDisk,
) -> str:
    """Стабильный ключ физического USB: UniqueId (не MBR Signature, не номер диска)."""
    return _disk_store_key(
        disk.number,
        unique_id=disk.unique_id,
        model=disk.model,
        size_bytes=disk.size_bytes,
    )


def _normalize_disk_uid(unique_id: str | None) -> str | None:
    if not unique_id:
        return None
    normalized = (
        str(unique_id)
        .upper()
        .replace("{", "")
        .replace("}", "")
        .replace("-", "")
        .strip()
    )
    return normalized or None


def _disk_store_key(
    disk_number: int,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> str:
    """Ключ кеша/состояния. MBR Signature намеренно не используется (одинаков у Cicada USB)."""
    uid = _normalize_disk_uid(unique_id)
    if uid:
        return f"uid_{uid}"
    safe_model = "".join(ch if ch.isalnum() else "_" for ch in model.strip())[:48]
    if safe_model and size_bytes > 0:
        return f"fallback_{safe_model}_{size_bytes}"
    return _disk_cache_key(disk_number)


def _stats_plausible_for_disk(
    stats: dict[str, float | int], size_bytes: int
) -> bool:
    if size_bytes <= 0:
        return True
    total_gb = float(stats.get("total_gb", 0))
    if total_gb <= 0:
        return False
    disk_gb = size_bytes / (1024**3)
    return total_gb <= disk_gb + 0.5


def _disk_cache_entry_matches_disk(
    entry: dict,
    *,
    unique_id: str | None,
    size_bytes: int,
) -> bool:
    stats = entry.get("stats")
    if not isinstance(stats, dict):
        return False
    if size_bytes > 0 and not _stats_plausible_for_disk(stats, size_bytes):
        return False
    stored_uid = _normalize_disk_uid(entry.get("disk_unique_id"))
    current_uid = _normalize_disk_uid(unique_id)
    if current_uid and stored_uid and stored_uid != current_uid:
        return False
    stored_size = entry.get("disk_size_bytes")
    if (
        size_bytes > 0
        and stored_size is not None
        and int(stored_size) != int(size_bytes)
    ):
        return False
    return True


def _disk_cache_lookup_keys(
    disk_number: int,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> list[str]:
    primary = _disk_store_key(
        disk_number,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
    )
    legacy = _disk_cache_key(disk_number)
    if primary == legacy:
        return [primary]
    return [primary, legacy]


def _usb_stats_file_path(ntfs_root: Path) -> Path:
    return cicada_data_root(ntfs_root) / CICADA_STATS_FILENAME


def _load_disk_stats_payload(
    ntfs_root: Path,
    *,
    unique_id: str | None = None,
    size_bytes: int = 0,
) -> dict | None:
    path = _usb_stats_file_path(ntfs_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if not _disk_cache_entry_matches_disk(
        data,
        unique_id=unique_id,
        size_bytes=size_bytes,
    ):
        return None
    return data


def _delete_disk_stats_payload(ntfs_root: Path) -> None:
    path = _usb_stats_file_path(ntfs_root)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def _image_entry_to_cache(entry: ImageEntry) -> dict:
    return {
        "name": entry.name,
        "size_bytes": entry.size_bytes,
        "category": entry.category,
        "relative_path": entry.relative_path,
    }


def _image_entry_from_cache(data: dict) -> ImageEntry:
    relative_path = str(data.get("relative_path") or "")
    name = str(data.get("name") or Path(relative_path).name)
    return ImageEntry(
        path=Path(relative_path.replace("/", "\\")) if relative_path else Path(name),
        name=name,
        size_bytes=int(data.get("size_bytes") or 0),
        category=str(data.get("category") or ""),
        relative_path=relative_path,
    )


def load_partition_stats_from_file(
    disk_number: int,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> dict[str, float | int] | None:
    ntfs_root = get_ntfs_partition_path(disk_number)
    if ntfs_root is None:
        return None
    payload = _load_disk_stats_payload(
        ntfs_root,
        unique_id=unique_id,
        size_bytes=size_bytes,
    )
    if payload is None:
        return None
    stats = payload.get("stats")
    if isinstance(stats, dict):
        return dict(stats)
    return None


def get_cached_deletable_images(
    disk_number: int,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> list[ImageEntry] | None:
    keys = _disk_cache_lookup_keys(
        disk_number,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
    )
    with _DISK_IMAGES_CACHE_LOCK:
        for key in keys:
            cached = _DISK_IMAGES_CACHE.get(key)
            if cached is not None:
                return list(cached)
    ntfs_root = get_ntfs_partition_path(disk_number)
    if ntfs_root is None:
        return None
    payload = _load_disk_stats_payload(
        ntfs_root,
        unique_id=unique_id,
        size_bytes=size_bytes,
    )
    if payload is None:
        return None
    images = payload.get("images")
    if not isinstance(images, list):
        return None
    return [
        _image_entry_from_cache(item)
        for item in images
        if isinstance(item, dict)
    ]


def save_disk_cache(
    disk_number: int,
    stats: dict[str, float | int],
    images: list[ImageEntry] | None = None,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
    ntfs_root: Path | None = None,
) -> None:
    key = _disk_store_key(
        disk_number,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
    )
    set_cached_partition_stats(
        disk_number,
        stats,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
    )
    with _DISK_IMAGES_CACHE_LOCK:
        if images is not None:
            _DISK_IMAGES_CACHE[key] = list(images)
        legacy = _disk_cache_key(disk_number)
        if key != legacy:
            _DISK_IMAGES_CACHE.pop(legacy, None)
    _ = ntfs_root  # visible mode: stats/images только в памяти, без TEMP/USB


def refresh_disk_cache_from_ntfs(
    disk_number: int,
    ntfs_root: Path,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> None:
    stats = scan_partition_stats(ntfs_root)
    images = list_deletable_images(cicada_data_root(ntfs_root))
    save_disk_cache(
        disk_number,
        stats,
        images,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
        ntfs_root=ntfs_root,
    )


def refresh_disk_cache_for_usb(
    disk: UsbDisk,
    *,
    timeout: float | None = None,
    device_key: str | None = None,
) -> None:
    """Обновить кеш статистики с NTFS P1 (прямой путь в visible mode)."""
    from cicada_errors import debug_log

    if not FULL_HIDE_AFTER_CREATE:
        ntfs = get_ntfs_partition_path(disk.number)
        if ntfs is not None:
            debug_log(f"[MOUNT] visible mode: direct NTFS path {ntfs}, no reveal")
            refresh_disk_cache_from_ntfs(
                disk.number,
                ntfs,
                unique_id=disk.unique_id,
                model=disk.model,
                size_bytes=disk.size_bytes,
            )
            return
    with cicada_ntfs_access(
        disk.number,
        timeout=timeout,
        device_key=device_key,
    ) as ntfs:
        refresh_disk_cache_from_ntfs(
            disk.number,
            ntfs,
            unique_id=disk.unique_id,
            model=disk.model,
            size_bytes=disk.size_bytes,
        )


def get_validated_partition_stats(disk: UsbDisk) -> dict[str, float | int] | None:
    """Кеш статистики с проверкой, что он относится к этому физическому диску."""
    stats = get_cached_partition_stats(
        disk.number,
        unique_id=disk.unique_id,
        model=disk.model,
        size_bytes=disk.size_bytes,
    )
    if stats is not None and not _stats_plausible_for_disk(stats, disk.size_bytes):
        clear_cached_partition_stats(
            disk.number,
            unique_id=disk.unique_id,
            model=disk.model,
            size_bytes=disk.size_bytes,
        )
        stats = None
    if stats is None:
        stats = load_partition_stats_from_file(
            disk.number,
            unique_id=disk.unique_id,
            model=disk.model,
            size_bytes=disk.size_bytes,
        )
        if stats is not None:
            set_cached_partition_stats(
                disk.number,
                stats,
                unique_id=disk.unique_id,
                model=disk.model,
                size_bytes=disk.size_bytes,
            )
    return stats


_STATS_CATEGORY_KEY = {
    "WINDOWS": "windows",
    "LINUX": "linux",
    "WINPE": "winpe",
}


def _image_relative_path(
    category: str,
    dest_name: str,
    subfolder: str | None = None,
) -> str:
    parts = [category.upper()]
    if subfolder:
        parts.append(subfolder)
    parts.append(dest_name)
    return "/".join(parts)


def apply_disk_cache_after_import(
    disk: UsbDisk,
    category: str,
    size_bytes: int,
    *,
    dest_name: str,
    subfolder: str | None = None,
) -> dict[str, float | int] | None:
    """Инкрементальное обновление кеша после импорта образа (без mount/rescan)."""
    stats = get_validated_partition_stats(disk)
    stat_key = _STATS_CATEGORY_KEY.get(category.upper())
    if stats is None or stat_key is None:
        return None

    stats = dict(stats)
    stats[stat_key] = int(stats.get(stat_key, 0)) + 1

    delta_gb = size_bytes / (1024**3)
    total_gb = float(stats.get("total_gb", 0))
    free_gb = float(stats.get("free_gb", 0))
    stats["free_gb"] = max(0.0, min(total_gb, free_gb - delta_gb))

    relative_path = _image_relative_path(category, dest_name, subfolder)
    images = get_cached_deletable_images(
        disk.number,
        unique_id=disk.unique_id,
        model=disk.model,
        size_bytes=disk.size_bytes,
    )
    if images is None:
        images = []
    else:
        images = list(images)
    images.append(
        ImageEntry(
            path=Path(dest_name),
            name=dest_name,
            size_bytes=size_bytes,
            category=category.upper(),
            relative_path=relative_path,
        )
    )
    images.sort(key=lambda entry: entry.name.lower())

    save_disk_cache(
        disk.number,
        stats,
        images,
        unique_id=disk.unique_id,
        model=disk.model,
        size_bytes=disk.size_bytes,
    )
    return stats


def apply_disk_cache_after_delete(
    disk: UsbDisk,
    entry: ImageEntry,
    *,
    size_bytes: int | None = None,
) -> dict[str, float | int] | None:
    """Инкрементальное обновление кеша после удаления образа (без mount/rescan)."""
    stats = get_validated_partition_stats(disk)
    stat_key = _STATS_CATEGORY_KEY.get(entry.category.upper())
    if stats is None or stat_key is None:
        return None

    deleted_size = size_bytes if size_bytes is not None else entry.size_bytes
    stats = dict(stats)
    stats[stat_key] = max(0, int(stats.get(stat_key, 0)) - 1)

    delta_gb = deleted_size / (1024**3)
    total_gb = float(stats.get("total_gb", 0))
    free_gb = float(stats.get("free_gb", 0))
    stats["free_gb"] = max(0.0, min(total_gb, free_gb + delta_gb))

    relative_path = (entry.relative_path or "").strip()
    images = get_cached_deletable_images(
        disk.number,
        unique_id=disk.unique_id,
        model=disk.model,
        size_bytes=disk.size_bytes,
    )
    if images is None:
        images = []
    elif relative_path:
        images = [
            item for item in images if item.relative_path != relative_path
        ]
    else:
        images = [item for item in images if item.name != entry.name]

    save_disk_cache(
        disk.number,
        stats,
        images,
        unique_id=disk.unique_id,
        model=disk.model,
        size_bytes=disk.size_bytes,
    )
    return stats


def remove_image_from_disk_cache(disk_number: int, entry: ImageEntry) -> None:
    relative_path = (entry.relative_path or "").strip()
    if not relative_path:
        return
    stats = get_cached_partition_stats(disk_number)
    images = get_cached_deletable_images(disk_number)
    if stats is None or images is None:
        return
    images = [item for item in images if item.relative_path != relative_path]
    save_disk_cache(disk_number, stats, images)


def invalidate_disk_cache(
    disk_number: int,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> None:
    clear_cached_partition_stats(
        disk_number,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
    )
    keys = _disk_cache_lookup_keys(
        disk_number,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
    )
    with _DISK_IMAGES_CACHE_LOCK:
        for key in keys:
            _DISK_IMAGES_CACHE.pop(key, None)
    ntfs_root = get_ntfs_partition_path(disk_number)
    if ntfs_root is not None:
        _delete_disk_stats_payload(ntfs_root)


def invalidate_disk_stats_cache(
    disk_number: int,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> None:
    """Сброс кеша статистики раздела (память; файл на флешке только если уже был)."""
    invalidate_disk_cache(
        disk_number,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
    )


def get_cached_partition_stats(
    disk_number: int,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> dict[str, float | int] | None:
    keys = _disk_cache_lookup_keys(
        disk_number,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
    )
    with _PARTITION_STATS_CACHE_LOCK:
        for key in keys:
            cached = _PARTITION_STATS_CACHE.get(key)
            if cached is not None:
                return dict(cached)
        legacy = _PARTITION_STATS_CACHE.get(_disk_cache_key(disk_number))
        return dict(legacy) if legacy is not None else None


def set_cached_partition_stats(
    disk_number: int,
    stats: dict[str, float | int],
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> None:
    key = _disk_store_key(
        disk_number,
        unique_id=unique_id,
        model=model,
        size_bytes=size_bytes,
    )
    with _PARTITION_STATS_CACHE_LOCK:
        _PARTITION_STATS_CACHE[key] = dict(stats)
        legacy = _disk_cache_key(disk_number)
        if key != legacy:
            _PARTITION_STATS_CACHE.pop(legacy, None)


def clear_cached_partition_stats(
    disk_number: int | None = None,
    *,
    unique_id: str | None = None,
    model: str = "",
    size_bytes: int = 0,
) -> None:
    with _PARTITION_STATS_CACHE_LOCK:
        if disk_number is None:
            _PARTITION_STATS_CACHE.clear()
        else:
            for key in _disk_cache_lookup_keys(
                disk_number,
                unique_id=unique_id,
                model=model,
                size_bytes=size_bytes,
            ):
                _PARTITION_STATS_CACHE.pop(key, None)
    with _DISK_IMAGES_CACHE_LOCK:
        if disk_number is None:
            _DISK_IMAGES_CACHE.clear()
        else:
            for key in _disk_cache_lookup_keys(
                disk_number,
                unique_id=unique_id,
                model=model,
                size_bytes=size_bytes,
            ):
                _DISK_IMAGES_CACHE.pop(key, None)

CICADA_FLAG_PAYLOAD = {
    "signature": CICADA_SIGNATURE,
    "version": "2.1",
    "layout": CICADA_LAYOUT,
    "protected": True,
}


def write_cicada_flag(ntfs_root: Path) -> None:
    flag_path = ntfs_root / CICADA_FLAG_FILE
    flag_path.write_text(
        json.dumps(CICADA_FLAG_PAYLOAD, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def restore_cicada_usb_flag(
    disk_number: int,
    *,
    timeout: float | None = None,
    fast_only: bool = False,
    device_key: str | None = None,
) -> None:
    """Временно открывает NTFS и восстанавливает .cicada3301.flag."""
    from cicada_errors import debug_log

    debug_log(f"[FLAG] restore started disk={disk_number}")
    with cicada_ntfs_access(
        disk_number,
        timeout=timeout,
        fast_only=fast_only,
        device_key=device_key,
    ) as ntfs_root:
        write_cicada_flag(ntfs_root)
    debug_log(f"[FLAG] restore finished disk={disk_number}")


def read_cicada_flag(ntfs_root: Path) -> dict | None:
    flag_path = ntfs_root / CICADA_FLAG_FILE
    if not flag_path.is_file():
        return None
    try:
        data = json.loads(flag_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def is_cicada_usb(ntfs_root: Path) -> bool:
    data = read_cicada_flag(ntfs_root)
    if not data:
        return False
    return (
        data.get("signature") == CICADA_SIGNATURE
        and data.get("layout") == CICADA_LAYOUT
    )


def is_partition_hidden(disk_number: int, partition_number: int = 1) -> bool:
    try:
        state = _query_partition_mount_state(disk_number, partition_number)
    except Exception:
        return False
    if _is_mbr_type_visible(state):
        return False
    return _partition_needs_mbr_unhide(state) or bool(state.get("hidden"))


def _assign_partition_letter(disk_number: int, partition_number: int) -> str:
    return assign_free_letter(disk_number, partition_number)


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PartitionRevealTimeoutError()
    return remaining


def reveal_ntfs_partition(
    disk_number: int,
    *,
    timeout: float | None = None,
) -> str:
    deadline = time.monotonic() + timeout if timeout is not None else None
    existing = get_partition_drive_letter(disk_number, 1)
    if existing:
        return existing
    try:
        _ensure_partition_visible(disk_number, timeout=timeout)
        existing = get_partition_drive_letter(disk_number, 1)
        if existing:
            return existing
        _remaining_timeout(deadline)
        return assign_free_letter(disk_number, 1)
    except RuntimeError as exc:
        if deadline is not None and (
            time.monotonic() >= deadline or "timed out" in str(exc).lower()
        ):
            raise PartitionRevealTimeoutError() from exc
        raise


def hide_ntfs_partition(disk_number: int, letter: str | None = None) -> None:
    if not FULL_HIDE_AFTER_CREATE:
        return
    if letter is None:
        letter = get_partition_drive_letter(disk_number, 1)
    for access_path in _get_partition_access_paths(disk_number, 1):
        _remove_partition_access_path(disk_number, 1, access_path)
    if letter:
        _remove_partition_access_path(disk_number, 1, f"{letter.upper()}:")
    remove_letter = f"remove letter={letter}" if letter else "remove letter=*"
    script = rf"""
$ErrorActionPreference = 'Stop'
$dn = {disk_number}
Set-Partition -DiskNumber $dn -PartitionNumber 1 -IsHidden $true
Set-Partition -DiskNumber $dn -PartitionNumber 1 -MbrType 0x17
Update-HostStorageCache
"""
    try:
        run_powershell(script)
    except Exception:
        run_diskpart(
            f"""select disk {disk_number}
select partition 1
{remove_letter}
set id={HIDDEN_PARTITION_TYPE} override
attributes partition set hidden
exit
""",
            strict=False,
        )


def cicada_ntfs_needs_reveal(disk_number: int) -> bool:
    if is_partition_hidden(disk_number, 1):
        return True
    return get_partition_drive_letter(disk_number, 1) is None


@contextmanager
def cicada_ntfs_access(
    disk_number: int,
    *,
    timeout: float | None = None,
    fast_only: bool = False,
    device_key: str | None = None,
):
    """Открывает NTFS-раздел 1 для чтения/записи (видимый или временно раскрытый)."""
    with ntfs_disk_lock(disk_number):
        if not FULL_HIDE_AFTER_CREATE:
            from cicada_errors import debug_log

            path = resolve_visible_ntfs_path(disk_number)
            if path is None:
                path = ensure_visible_ntfs_path(disk_number)
            debug_log(f"[MOUNT] visible mode: direct NTFS path {path}, no reveal")
            yield path
            debug_log("[MOUNT] visible mode: close skipped")
            return

        handle: _NtfsMountHandle | None = None
        try:
            handle = _open_ntfs_mount(
                disk_number,
                timeout=timeout,
                fast_only=fast_only,
                device_key=device_key,
            )
            yield handle.mount_path
        finally:
            if handle is not None:
                _close_ntfs_mount(handle)


def disk_has_cicada_flag(
    disk_number: int,
    *,
    timeout: float | None = None,
    fast_only: bool = False,
    device_key: str | None = None,
) -> bool:
    try:
        if not FULL_HIDE_AFTER_CREATE:
            ntfs_root = ensure_visible_ntfs_path(disk_number)
            return is_cicada_usb(ntfs_root)
        with cicada_ntfs_access(
            disk_number,
            timeout=timeout,
            fast_only=fast_only,
            device_key=device_key,
        ) as ntfs_root:
            return is_cicada_usb(ntfs_root)
    except Exception:
        return False


def verify_cicada_usb_flag(
    disk_number: int,
    *,
    timeout: float | None = None,
    fast_only: bool = False,
    device_key: str | None = None,
) -> bool:
    """Проверяет .cicada3301.flag перед реальной операцией с разделом."""
    if device_key and is_cicada_flag_verified_cached(device_key):
        return True
    if not FULL_HIDE_AFTER_CREATE:
        from cicada_errors import debug_log

        try:
            ntfs_root = resolve_visible_ntfs_path(disk_number)
            if ntfs_root is None:
                ntfs_root = ensure_visible_ntfs_path(disk_number)
            debug_log(f"[MOUNT] visible mode: direct NTFS path {ntfs_root}, no reveal")
            verified = is_cicada_usb(ntfs_root)
        except Exception:
            verified = False
        if verified and device_key:
            mark_cicada_flag_verified_cached(device_key)
        return verified
    verified = disk_has_cicada_flag(
        disk_number,
        timeout=timeout,
        fast_only=fast_only,
        device_key=device_key,
    )
    if verified and device_key:
        mark_cicada_flag_verified_cached(device_key)
    return verified


def find_cicada_usb_disks() -> list[UsbDisk]:
    return [disk for disk in list_usb_disks_with_cicada_flags() if disk.is_cicada]


def list_usb_disks_with_cicada_flags() -> list[UsbDisk]:
    disks, _cached = list_usb_disks_fast_cached()
    return disks


def reset_usb_to_single_partition(
    disk_number: int, label: str = "USB", *, skip_prepare: bool = False
) -> None:
    if not skip_prepare:
        prepare_disk_for_wipe(disk_number)
    safe_label = label.replace("'", "''")
    script = rf"""
$ErrorActionPreference = 'Stop'
$dn = {disk_number}

Set-Disk -Number $dn -IsOffline $false
Set-Disk -Number $dn -IsReadOnly $false
Clear-Disk -Number $dn -RemoveData -Confirm:$false
Initialize-Disk -Number $dn -PartitionStyle MBR -Confirm:$false

$p = New-Partition -DiskNumber $dn -UseMaximumSize -AssignDriveLetter
Format-Volume -Partition $p -FileSystem exFAT -NewFileSystemLabel '{safe_label}' -Confirm:$false -Force | Out-Null
Update-HostStorageCache
"""
    try:
        run_powershell(script)
    except Exception as ps_err:
        from cicada_errors import debug_log

        debug_log(f"[RESET] PowerShell reset failed: {ps_err}")
        run_diskpart(
            f"""select disk {disk_number}
online disk noerr
attributes disk clear readonly noerr
clean
convert mbr
create partition primary
format fs=exfat quick label={label}
assign
exit
""",
            strict=True,
        )


def get_ntfs_partition_path(disk_number: int) -> Path | None:
    letter = get_partition_drive_letter(disk_number, 1)
    if not letter:
        return None
    path = Path(f"{letter.upper()}:\\")
    return path if path.exists() else None


def resolve_visible_ntfs_path(disk_number: int) -> Path | None:
    """Прямой путь к видимому NTFS P1, если буква уже назначена."""
    return get_ntfs_partition_path(disk_number)


def cicada_data_root(ntfs_root: Path) -> Path:
    nested = ntfs_root / "Cicada3301"
    return nested if nested.is_dir() else ntfs_root


def _iter_category_image_files(
    folder: Path,
    extensions: set[str],
    *,
    max_depth: int = 1,
):
    """Только папка категории (WINDOWS/LINUX/WINPE), без обхода всего раздела."""
    if not folder.is_dir():
        return
    for item in folder.iterdir():
        if item.is_file() and item.suffix.lower() in extensions:
            yield item
        elif max_depth > 0 and item.is_dir():
            for sub in item.iterdir():
                if sub.is_file() and sub.suffix.lower() in extensions:
                    yield sub


def count_category_images(data_root: Path, category: str) -> int:
    folder = data_root / category
    return sum(1 for _ in _iter_category_image_files(folder, IMAGE_EXTENSIONS))


def scan_partition_stats(ntfs_root: Path) -> dict[str, float | int]:
    data_root = cicada_data_root(ntfs_root)
    usage = shutil.disk_usage(str(ntfs_root))
    return {
        "windows": count_category_images(data_root, "WINDOWS"),
        "linux": count_category_images(data_root, "LINUX"),
        "winpe": count_category_images(data_root, "WINPE"),
        "free_gb": usage.free / (1024**3),
        "total_gb": usage.total / (1024**3),
    }


def format_file_size(size_bytes: int) -> str:
    if size_bytes >= 1024**3:
        return f"{size_bytes / (1024**3):.1f} ГБ"
    if size_bytes >= 1024**2:
        return f"{size_bytes / (1024**2):.1f} МБ"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} КБ"
    return f"{size_bytes} Б"


def list_image_files(data_root: Path, category: str | None = None) -> list[Path]:
    roots = [data_root / category] if category else [data_root / cat for cat in IMAGE_CATEGORIES]
    files: list[Path] = []
    for root in roots:
        files.extend(_iter_category_image_files(root, IMAGE_EXTENSIONS))
    return sorted(files, key=lambda p: p.name.lower())


def list_deletable_images(data_root: Path) -> list[ImageEntry]:
    entries: list[ImageEntry] = []
    for category in IMAGE_CATEGORIES:
        root = data_root / category
        for item in _iter_category_image_files(root, DELETABLE_IMAGE_EXTENSIONS):
            try:
                size_bytes = item.stat().st_size
            except OSError:
                size_bytes = 0
            try:
                relative_path = str(item.relative_to(data_root)).replace("\\", "/")
            except ValueError:
                relative_path = f"{category}/{item.name}"
            entries.append(
                ImageEntry(
                    path=item,
                    name=item.name,
                    size_bytes=size_bytes,
                    category=category,
                    relative_path=relative_path,
                )
            )
    return sorted(entries, key=lambda entry: entry.name.lower())


def import_temp_dest_path(final_dest: Path) -> Path:
    return final_dest.with_name(f"{final_dest.name}{IMPORT_TEMP_SUFFIX}")


def resolve_image_entry_path(data_root: Path, entry: ImageEntry) -> Path:
    relative_path = (entry.relative_path or "").strip().replace("\\", "/")
    if not relative_path:
        raise FileNotFoundError(
            f"Не указан relative_path для образа: {entry.name}"
        )
    if PurePosixPath(relative_path).is_absolute():
        raise FileNotFoundError(
            f"Недопустимый relative_path (абсолютный путь): {relative_path}"
        )
    if ".." in PurePosixPath(relative_path).parts:
        raise FileNotFoundError(
            f"Недопустимый relative_path (..): {relative_path}"
        )
    target = (data_root / Path(*PurePosixPath(relative_path).parts)).resolve()
    data_root_resolved = data_root.resolve()
    try:
        target.relative_to(data_root_resolved)
    except ValueError as exc:
        raise FileNotFoundError(
            f"Путь выходит за пределы data_root: {target}"
        ) from exc
    if not target.is_file():
        raise FileNotFoundError(f"Файл не найден: {target}")
    return target


def _log_delete_list_entry(entry: ImageEntry, data_root: Path | None = None) -> None:
    from cicada_errors import debug_log

    full_path = str(entry.path)
    if data_root is not None and entry.relative_path:
        try:
            full_path = str(resolve_image_entry_path(data_root, entry))
        except FileNotFoundError:
            rel = Path(entry.relative_path.replace("/", "\\"))
            full_path = str(data_root / rel)
    debug_log(
        f"[DELETE] list item: category={entry.category} "
        f"relative_path={entry.relative_path} full_path={full_path}"
    )


def _log_import_dest_check(dest: Path) -> None:
    from cicada_errors import debug_log

    target_exists = dest.exists()
    target_size = dest.stat().st_size if target_exists else 0
    debug_log(f"[IMPORT] target:\n{dest}")
    debug_log(f"[IMPORT] exists:\n{target_exists}")
    debug_log(f"[IMPORT] target_size: {target_size}")


def copy_image_to_category(
    source: Path,
    ntfs_root: Path,
    category: str,
    *,
    subfolder: str | None = None,
) -> Path:
    from cicada_errors import debug_log

    data_root = cicada_data_root(ntfs_root)
    dest_dir = data_root / category
    if subfolder:
        dest_dir = dest_dir / subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    _log_import_dest_check(dest)
    if dest.exists():
        debug_log(f"[IMPORT] destination path = {dest}")
        raise FileExistsError(f"Файл уже существует: {dest.name}")
    _copy_file_streaming(source, dest)
    return dest


def _copy_file_streaming(source: Path, dest: Path, chunk_size: int = 8 * 1024 * 1024) -> None:
    """Потоковое копирование больших образов без загрузки в память."""
    with source.open("rb") as src, dest.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=chunk_size)
    try:
        shutil.copystat(source, dest)
    except OSError:
        pass


def delete_image_file(image_path: Path) -> None:
    if not image_path.is_file():
        raise FileNotFoundError(f"Файл не найден: {image_path}")
    if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("Можно удалять только файлы образов (.iso, .img, .wim и т.д.)")
    image_path.unlink()


def prepare_disk_for_wipe(disk_number: int) -> None:
    for partition_number in (1, 2):
        for access_path in _get_partition_access_paths(disk_number, partition_number):
            _remove_partition_access_path(disk_number, partition_number, access_path)
    script = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$dn = {disk_number}
Get-Partition -DiskNumber $dn | ForEach-Object {{
    if ($_.DriveLetter) {{
        Remove-PartitionAccessPath -DiskNumber $dn -PartitionNumber $_.PartitionNumber -AccessPath "$($_.DriveLetter):" 
    }}
    foreach ($path in @($_.AccessPaths)) {{
        Remove-PartitionAccessPath -DiskNumber $dn -PartitionNumber $_.PartitionNumber -AccessPath $path -ErrorAction SilentlyContinue
    }}
}}
Get-Disk -Number $dn | Set-Disk -IsOffline $false
Get-Disk -Number $dn | Set-Disk -IsReadOnly $false
Update-HostStorageCache
"""
    run_powershell(script)


def partition_usb_disk(disk_number: int, part1_size_mb: int) -> dict[int, str]:
    """Разметка через PowerShell (надёжнее diskpart на занятых USB)."""
    mbr_signature = generate_cicada_mbr_signature()
    sig_hex = _format_mbr_signature_hex(mbr_signature)
    from cicada_errors import debug_log

    debug_log(f"[CREATE] generated unique Cicada MBR signature: 0x{sig_hex}")
    script = rf"""
$ErrorActionPreference = 'Stop'
$dn = {disk_number}
$part1Bytes = {part1_size_mb} * 1MB

Get-Partition -DiskNumber $dn -ErrorAction SilentlyContinue | ForEach-Object {{
    if ($_.DriveLetter) {{
        Remove-PartitionAccessPath -DiskNumber $dn -PartitionNumber $_.PartitionNumber -AccessPath "$($_.DriveLetter):"
    }}
}}
Start-Sleep -Seconds 1
Update-HostStorageCache

Set-Disk -Number $dn -IsOffline $false
Set-Disk -Number $dn -IsReadOnly $false
Clear-Disk -Number $dn -RemoveData -Confirm:$false
Initialize-Disk -Number $dn -PartitionStyle MBR -Confirm:$false
Set-Disk -Number $dn -Signature 0x{sig_hex}

$p1 = New-Partition -DiskNumber $dn -Size $part1Bytes -AssignDriveLetter
Format-Volume -Partition $p1 -FileSystem NTFS -NewFileSystemLabel 'Cicada3301' -Confirm:$false -Force | Out-Null

$p2 = New-Partition -DiskNumber $dn -UseMaximumSize -AssignDriveLetter
Format-Volume -Partition $p2 -FileSystem FAT32 -NewFileSystemLabel 'BOOT' -Confirm:$false -Force | Out-Null

Start-Sleep -Seconds 2
Update-HostStorageCache

$letters = @{{}}
Get-Partition -DiskNumber $dn -PartitionNumber 1,2 | ForEach-Object {{
    if ($_.DriveLetter) {{ $letters["$($_.PartitionNumber)"] = [string]$_.DriveLetter }}
}}
$letters | ConvertTo-Json -Compress
"""
    raw = run_powershell(script)
    letters: dict[int, str] = {}
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                letters = {int(k): str(v) for k, v in data.items() if v}
        except json.JSONDecodeError:
            pass
    if 1 not in letters or 2 not in letters:
        letters = ensure_partition_letters(disk_number)
    if not is_cicada_signature(disk_number):
        stamp_cicada_disk_signature(disk_number, mbr_signature)
    return letters


def partition_usb_disk_diskpart(
    disk_number: int, part1_size_mb: int, mbr_signature: int | None = None
) -> int:
    if mbr_signature is None:
        mbr_signature = generate_cicada_mbr_signature()
    prepare_disk_for_wipe(disk_number)
    script = build_diskpart_script(disk_number, part1_size_mb, mbr_signature)
    run_diskpart(script, verify_layout_disk=disk_number)
    return mbr_signature


def build_diskpart_script(
    disk_number: int, part1_size_mb: int, mbr_signature: int | None = None
) -> str:
    if mbr_signature is None:
        mbr_signature = generate_cicada_mbr_signature()
    sig_hex = _format_mbr_signature_hex(mbr_signature)
    return f"""san policy=OnlineAll
select disk {disk_number}
online disk noerr
attributes disk clear readonly noerr
clean
convert mbr
uniqueid disk id={sig_hex}
create partition primary size={part1_size_mb}
select partition 1
format fs=ntfs quick label=Cicada3301 override
assign
create partition primary
select partition 2
format fs=fat32 quick label=BOOT override
assign
exit
"""


def hide_boot_partition(disk_number: int) -> None:
    """Скрыть раздел 2 (BOOT) — ID 1C, без буквы диска."""
    script = rf"""
$ErrorActionPreference = 'Stop'
$dn = {disk_number}
$p2 = Get-Partition -DiskNumber $dn -PartitionNumber 2 -ErrorAction Stop
if ($p2.DriveLetter) {{
    Remove-PartitionAccessPath -DiskNumber $dn -PartitionNumber 2 -AccessPath "$($p2.DriveLetter):" -ErrorAction SilentlyContinue
}}
Set-Partition -DiskNumber $dn -PartitionNumber 2 -IsHidden $true
Set-Partition -DiskNumber $dn -PartitionNumber 2 -MbrType 0x1C
Update-HostStorageCache
"""
    try:
        run_powershell(script)
    except Exception:
        run_diskpart(
            f"""select disk {disk_number}
select partition 2
remove letter=*
set id={HIDDEN_BOOT_PARTITION_TYPE} override
attributes partition set hidden
exit
""",
            strict=False,
        )


def ensure_cicada_partition_visible(disk_number: int) -> str:
    """Раздел 1 (Cicada3301) — видимый NTFS с буквой диска, не скрытый."""
    letter = get_partition_drive_letter(disk_number, 1)
    try:
        state = _query_partition_mount_state(disk_number, 1)
    except Exception:
        state = {}
    if (
        letter
        and _is_mbr_type_visible(state)
        and not bool(state.get("hidden"))
    ):
        return letter.upper()
    script = rf"""
$ErrorActionPreference = 'Stop'
$dn = {disk_number}
Set-Partition -DiskNumber $dn -PartitionNumber 1 -IsHidden $false
Set-Partition -DiskNumber $dn -PartitionNumber 1 -NoDefaultDriveLetter $false
Set-Partition -DiskNumber $dn -PartitionNumber 1 -MbrType 0x07
Update-HostStorageCache
"""
    try:
        run_powershell(script)
        return assign_free_letter(disk_number, 1)
    except Exception:
        pass
    run_diskpart(
        f"""select disk {disk_number}
select partition 1
set id=07
attributes partition clear hidden
assign
exit
""",
        strict=False,
    )
    return assign_free_letter(disk_number, 1)


def ensure_visible_ntfs_path(disk_number: int) -> Path:
    """Путь к видимому NTFS P1 (без mount/unhide/hide)."""
    path = resolve_visible_ntfs_path(disk_number)
    if path is not None:
        return path
    letter = ensure_cicada_partition_visible(disk_number)
    return Path(f"{letter.upper()}:\\")


def flatten_extracted_root(dest: Path, root_name: str) -> None:
    """Переносит содержимое единственной вложенной папки в корень dest."""
    nested = dest / root_name
    if not nested.is_dir():
        return
    for item in nested.iterdir():
        target = dest / item.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(item), str(target))
    nested.rmdir()


def extract_7z(
    seven_z: Path,
    archive: Path,
    dest: Path,
    *,
    strip_root_folder: bool = False,
    root_folder_name: str | None = None,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [str(seven_z), "x", str(archive), f"-o{dest}", "-y", "-bsp1"]
    if strip_root_folder:
        cmd.insert(3, "-spe")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_subprocess_kwargs(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "Ошибка распаковки 7z").strip()
        )
    if root_folder_name:
        flatten_extracted_root(dest, root_folder_name)


class CreateWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    step_changed = pyqtSignal(int)
    finished_ok = pyqtSignal()
    finished_err = pyqtSignal(object)

    def __init__(
        self,
        disk: UsbDisk,
        assets_dir: Path | None = None,
        *,
        download_missing: bool = False,
    ):
        super().__init__()
        self.disk = disk
        self.assets_dir = assets_dir if assets_dir is not None else app_dir()
        self.download_missing = download_missing
        self.cleanup_after = (
            download_missing
            and self.assets_dir.resolve() == temp_assets_dir().resolve()
        )

    def run(self) -> None:
        try:
            self._create()
            self.finished_ok.emit()
        except Exception as exc:
            self.finished_err.emit(exc)
        finally:
            if self.cleanup_after:
                self.log.emit("Удаление временных файлов...")
                cleanup_temp_assets(self.assets_dir)
                self.log.emit("  временные файлы удалены.")

    def _create(self) -> None:
        self.step_changed.emit(0)

        if self.download_missing:
            self.log.emit(f"Загрузка файлов в {self.assets_dir}")
            download_assets(
                self.assets_dir,
                log=lambda message: self.log.emit(message),
                progress=self.progress.emit,
            )
            self.progress.emit(10)
        elif not assets_complete(self.assets_dir):
            raise RuntimeError(
                "Файлы сборки не найдены в "
                f"{self.assets_dir}. Положите Cicada3301.7z, FAT32.7z и 7z.exe."
            )

        self.log.emit(f"Диск {self.disk.number}: {self.disk.model} ({self.disk.size_gb:.2f} GB)")
        self.progress.emit(12 if self.download_missing else 5)

        total_mb = self.disk.size_bytes // (1024 * 1024)
        part1_mb = total_mb - BOOT_PARTITION_MB
        if part1_mb < 512:
            raise RuntimeError(
                f"Диск слишком мал: нужен минимум ~2 GB (сейчас {self.disk.size_gb:.1f} GB)."
            )

        self.step_changed.emit(1)
        self.log.emit(
            f"Разметка: раздел 1 NTFS Cicada3301 — {part1_mb} MB, "
            f"раздел 2 FAT32 BOOT (скрытый) — {BOOT_PARTITION_MB} MB"
        )
        self.progress.emit(10)

        letters: dict[int, str] = {}
        self.log.emit("Подготовка диска (отмонтирование разделов)...")
        try:
            prepare_disk_for_wipe(self.disk.number)
        except Exception as exc:
            self.log.emit(f"  предупреждение: {exc}")

        self.step_changed.emit(2)
        self.log.emit("Разметка через PowerShell (MBR, NTFS + FAT32)...")
        try:
            letters = partition_usb_disk(self.disk.number, part1_mb)
            self.log.emit("  PowerShell: разметка выполнена.")
        except Exception as ps_err:
            self.log.emit(f"  PowerShell не удался: {ps_err}")
            self.log.emit("  Повтор через diskpart...")
            mbr_signature = partition_usb_disk_diskpart(self.disk.number, part1_mb)
            letters = {}
            if not is_cicada_signature(self.disk.number):
                stamp_cicada_disk_signature(self.disk.number, mbr_signature)

        self.progress.emit(35)

        if not letters or 1 not in letters or 2 not in letters:
            self.log.emit("Назначение букв разделам...")
            letters = wait_for_letters(
                self.disk.number,
                log=lambda message: self.log.emit(message),
            )
        ntfs_letter = letters[1]
        fat_letter = letters[2]
        self.log.emit(f"Раздел 1 (NTFS): {ntfs_letter}:\\")
        self.log.emit(f"Раздел 2 (FAT32): {fat_letter}:\\")
        self.progress.emit(45)

        self.step_changed.emit(3)
        seven_z = find_7z(self.assets_dir)
        self.log.emit(f"7z: {seven_z}")

        cicada_archive = self.assets_dir / "Cicada3301.7z"
        fat_archive = self.assets_dir / "FAT32.7z"
        if not cicada_archive.is_file():
            raise FileNotFoundError(f"Не найден {cicada_archive}")
        if not fat_archive.is_file():
            raise FileNotFoundError(f"Не найден {fat_archive}")

        ntfs_root = Path(f"{ntfs_letter}:\\")
        fat_root = Path(f"{fat_letter}:\\")

        self.log.emit(f"Распаковка FAT32.7z → {fat_root}")
        extract_7z(
            seven_z,
            fat_archive,
            fat_root,
            strip_root_folder=True,
            root_folder_name="FAT32",
        )
        self.progress.emit(60)

        self.step_changed.emit(4)
        self.log.emit(f"Распаковка Cicada3301.7z → {ntfs_root}")
        extract_7z(seven_z, cicada_archive, ntfs_root)
        self.progress.emit(75)

        self.step_changed.emit(5)
        self.log.emit("Запись служебного флага .cicada3301.flag...")
        write_cicada_flag(ntfs_root)
        self.log.emit("  Флаг Cicada записан.")
        try:
            refresh_disk_cache_from_ntfs(
                self.disk.number,
                ntfs_root,
                unique_id=self.disk.unique_id,
                model=self.disk.model,
                size_bytes=self.disk.size_bytes,
            )
        except OSError:
            pass

        self.log.emit("Скрытие раздела BOOT (FAT32)...")
        try:
            hide_boot_partition(self.disk.number)
            self.log.emit("  Раздел BOOT скрыт (ID 1C).")
        except Exception as exc:
            self.log.emit(f"  предупреждение: {exc}")

        self.log.emit(f"  Cicada3301: {ntfs_letter}:\\ (видимый NTFS)")

        self.step_changed.emit(6)
        self.progress.emit(100)
        self.log.emit("Готово!")


class DeleteCicadaWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal()
    finished_err = pyqtSignal(object)

    def __init__(self, disk: UsbDisk):
        super().__init__()
        self.disk = disk

    def run(self) -> None:
        try:
            self.log.emit("Полное удаление Cicada USB Boot...")
            self.progress.emit(5)
            self.log.emit("Подготовка диска (отмонтирование разделов)...")
            prepare_disk_for_wipe(self.disk.number)
            self.progress.emit(25)
            self.log.emit("Очистка диска и создание раздела exFAT...")
            reset_usb_to_single_partition(self.disk.number, skip_prepare=True)
            self.progress.emit(95)
            self.log.emit("Cicada USB Boot удалён")
            self.log.emit("Флешка возвращена в обычный режим")
            invalidate_disk_cache(self.disk.number)
            self.progress.emit(100)
            self.finished_ok.emit()
        except Exception as exc:
            self.finished_err.emit(exc)


class ImportCancelled(Exception):
    """Импорт образа отменён пользователем."""


class ImageImportWorker(QThread):
    """Импорт образа на Cicada USB в фоне: проверка флага, доступ к NTFS, копирование."""

    stage_changed = pyqtSignal(str)
    progress_changed = pyqtSignal(int)
    bytes_changed = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)
    import_cancelled = pyqtSignal()

    STAGE_CHECK = "Проверка флешки"
    STAGE_REVEAL = "Открытие раздела"
    STAGE_SPACE = "Проверка места"
    STAGE_COPY = "Копирование образа"
    STAGE_FLUSH = "Завершение копирования"
    STAGE_RENAME = "Переименование .part -> .iso"
    STAGE_CLOSE = "Завершение доступа к разделу"
    STAGE_HIDE = "Скрытие раздела"
    STAGE_CACHE = "Обновление кеша"
    STAGE_DONE = "Готово"

    def __init__(
        self,
        disk: UsbDisk,
        source: Path,
        category: str,
        *,
        subfolder: str | None = None,
    ):
        super().__init__()
        self.disk = disk
        self.source = source
        self.category = category
        self.subfolder = subfolder
        self._cancel_requested = False
        self._dest_path: Path | None = None
        self._temp_dest_path: Path | None = None
        self._import_renamed_ok = False
        self.mount_close_warning: str | None = None
        self._temp_cleaned = False

    def request_cancel(self) -> None:
        self._cancel_requested = True

    def emit_import_progress(
        self,
        percent: int,
        stage: str | None = None,
        *,
        copied: int | None = None,
        total: int | None = None,
    ) -> None:
        """Единственная точка обновления progress/stage для импорта."""
        from cicada_errors import debug_log

        clamped = max(0, min(100, percent))
        if copied is not None and total is not None:
            debug_log(f"[IMPORT] copied: {copied} / {total}")
        debug_log(f"[IMPORT] progress: {clamped}")
        self.progress_changed.emit(clamped)
        if stage is not None:
            debug_log(f"[IMPORT] stage: {stage}")
            self.stage_changed.emit(stage)

    def _emit_copy_progress(self, copied: int, total_size: int) -> None:
        copy_percent = int(copied / total_size * 85) if total_size > 0 else 0
        overall_percent = min(95, 10 + copy_percent)
        self.emit_import_progress(overall_percent, copied=copied, total=total_size)
        self.bytes_changed.emit(copied, total_size)

    def _check_cancel(self) -> None:
        if self._cancel_requested:
            raise ImportCancelled("Добавление образа отменено")

    def _cleanup_temp_file(self, temp_path: Path | None, *, reason: str) -> None:
        from cicada_errors import debug_log

        if self._temp_cleaned:
            debug_log("[IMPORT] cleanup temp skipped (already done)")
            return
        final_dest = self._dest_path
        cleanup_path = temp_path
        if cleanup_path is None:
            self._temp_cleaned = True
            return
        if final_dest is not None and cleanup_path.resolve() == final_dest.resolve():
            debug_log("[IMPORT][BUG] refusing to delete final dest during cleanup")
            self._temp_cleaned = True
            return
        debug_log(f"[IMPORT] cleanup temp on {reason} -> {cleanup_path.name}")
        if cleanup_path.exists():
            debug_log(f"[IMPORT] temp_path.unlink() -> {cleanup_path}")
            try:
                cleanup_path.unlink()
            except OSError as exc:
                debug_log(f"[IMPORT] temp_path.unlink failed: {exc}")
                debug_log(traceback.format_exc())
        self._temp_cleaned = True

    def _cleanup_temp_files_before_unmount(self) -> None:
        if self._import_renamed_ok:
            return
        reason = "cancel" if self._cancel_requested else "error"
        self._cleanup_temp_file(self._temp_dest_path, reason=reason)

    def _cleanup_import_temp(self, reason: str) -> None:
        from cicada_errors import debug_log

        if self._temp_cleaned:
            debug_log("[IMPORT] cleanup temp skipped (already done before unmount)")
            return
        self._cleanup_temp_file(self._temp_dest_path, reason=reason)

    def _remove_partial_dest(self) -> None:
        self._cleanup_import_temp("error")

    @staticmethod
    def _log_stage_finished(stage_name: str, started: float) -> None:
        perf_log(stage_name, started)

    def _copy_with_progress(self, source: Path, dest: Path, total_size: int) -> None:
        from cicada_errors import debug_log

        chunk_size = 8 * 1024 * 1024
        copied = 0
        debug_log("[IMPORT] copy started")
        copy_started = time.perf_counter()
        try:
            with source.open("rb") as src, dest.open("wb") as dst:
                while True:
                    self._check_cancel()
                    chunk = src.read(chunk_size)
                    if not chunk:
                        break
                    dst.write(chunk)
                    copied += len(chunk)
                    self._emit_copy_progress(copied, total_size)
                debug_log("[IMPORT] copy reached 100")
                debug_log("[IMPORT] flush file started")
                try:
                    dst.flush()
                    os.fsync(dst.fileno())
                except OSError as exc:
                    debug_log(f"[IMPORT] flush file warning: {exc}")
                debug_log("[IMPORT] flush file finished")
        except ImportCancelled:
            _import_trace("copy cancelled, closing file handles")
            raise
        finally:
            _import_trace("copy file handles closed")
        try:
            shutil.copystat(source, dest)
        except OSError:
            pass
        self.emit_import_progress(95)
        self.emit_import_progress(96, self.STAGE_FLUSH)
        self._log_stage_finished("copy", copy_started)

    def _run_import(self) -> str:
        from cicada_errors import debug_log

        self.emit_import_progress(0, self.STAGE_CHECK)
        self._check_cancel()
        if not self.disk.is_cicada:
            raise RuntimeError(
                "Cicada USB Boot не обнаружен.\n"
                "Сначала создайте загрузочную флешку."
            )

        self.emit_import_progress(5, self.STAGE_REVEAL)
        self._check_cancel()
        dest_name = ""
        self.mount_close_warning = None
        try:
            with cicada_ntfs_access(
                self.disk.number,
                timeout=IMPORT_REVEAL_TIMEOUT_SEC,
                device_key=disk_identity_key(self.disk),
            ) as ntfs_root:
                debug_log("[IMPORT] verify flag started")
                verify_started = time.perf_counter()
                if not is_cicada_usb(ntfs_root):
                    raise RuntimeError(
                        "Флаг Cicada3301 не найден или повреждён.\n"
                        "Сначала создайте загрузочную флешку."
                    )
                self._log_stage_finished("verify flag", verify_started)

                self.emit_import_progress(10, self.STAGE_SPACE)
                self._check_cancel()
                debug_log("[IMPORT] free space check started")
                space_started = time.perf_counter()
                source_size = self.source.stat().st_size
                usage = shutil.disk_usage(str(ntfs_root))
                if usage.free < source_size:
                    free_gb = usage.free / (1024**3)
                    need_gb = source_size / (1024**3)
                    raise RuntimeError(
                        f"Недостаточно свободного места на флешке.\n"
                        f"Нужно: {need_gb:.1f} ГБ, доступно: {free_gb:.1f} ГБ."
                    )
                data_root = cicada_data_root(ntfs_root)
                dest_dir = data_root / self.category
                if self.subfolder:
                    dest_dir = dest_dir / self.subfolder
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_filename = resolve_import_dest_name(
                    self.category,
                    self.subfolder,
                    self.source,
                )
                final_dest = dest_dir / dest_filename
                temp_dest = import_temp_dest_path(final_dest)
                self._dest_path = final_dest
                self._temp_dest_path = temp_dest

                debug_log(f"[IMPORT] final dest = {final_dest}")
                debug_log(f"[IMPORT] temp dest = {temp_dest}")
                debug_log(f"[IMPORT] temp exists = {temp_dest.exists()}")
                debug_log(f"[IMPORT] final exists = {final_dest.exists()}")
                _log_import_dest_check(final_dest)

                if temp_dest.exists():
                    debug_log("[IMPORT] cleanup stale temp")
                    try:
                        temp_dest.unlink()
                    except OSError:
                        pass

                if final_dest.exists():
                    debug_log(f"[IMPORT] destination path = {final_dest}")
                    raise FileExistsError(f"Файл уже существует: {final_dest.name}")
                self._log_stage_finished("free space check", space_started)

                self.emit_import_progress(10, self.STAGE_COPY)
                self._check_cancel()
                self._copy_with_progress(self.source, temp_dest, source_size)

                self.emit_import_progress(97, self.STAGE_RENAME)
                debug_log("[IMPORT] rename temp -> final started")
                try:
                    temp_dest.rename(final_dest)
                except OSError as exc:
                    debug_log(f"[IMPORT] rename temp -> final failed: {exc}")
                    raise
                debug_log("[IMPORT] rename temp -> final finished")
                self._temp_dest_path = None
                self._import_renamed_ok = True
                dest_name = final_dest.name
                debug_log(f"[IMPORT] final exists after import = {final_dest.exists()}")
                debug_log(f"[IMPORT] temp exists after import = {temp_dest.exists()}")
                if not final_dest.exists():
                    raise RuntimeError(
                        f"Файл образа не найден после импорта: {final_dest.name}"
                    )
                if temp_dest.exists():
                    raise RuntimeError(
                        f"Временный файл не удалён после переименования: {temp_dest.name}"
                    )
                if FULL_HIDE_AFTER_CREATE:
                    self.emit_import_progress(98, self.STAGE_HIDE)
                else:
                    self.emit_import_progress(98, self.STAGE_CLOSE)
        finally:
            _import_trace("finally enter _run_import")
            try:
                _import_trace("before cleanup temp")
                try:
                    self._cleanup_temp_files_before_unmount()
                except Exception as exc:
                    debug_log(f"[TRACE] cleanup temp exception: {exc}")
                    debug_log(traceback.format_exc())
                _import_trace("after cleanup temp")
            except Exception as exc:
                debug_log(f"[TRACE] before/after cleanup temp wrapper failed: {exc}")
                debug_log(traceback.format_exc())
            _import_trace("finally exit _run_import")

        return dest_name

    def run(self) -> None:
        from cicada_errors import debug_log

        import_started = time.perf_counter()
        ok_name: str | None = None
        err_message: str | None = None
        cancelled = False

        try:
            debug_log("[IMPORT] worker run() started")
            with ntfs_disk_lock(self.disk.number):
                ok_name = self._run_import()
            perf_log("image import", import_started)
        except ImportCancelled:
            debug_log("[IMPORT] worker run() cancelled")
            cancelled = True
        except PartitionRevealTimeoutError as exc:
            debug_log(f"[IMPORT] worker run() reveal timeout: {exc}")
            debug_log(traceback.format_exc())
            err_message = str(exc)
        except Exception as exc:
            debug_log(f"[IMPORT] worker run() error: {exc}")
            debug_log(traceback.format_exc())
            err_message = str(exc)
        finally:
            _import_trace("finally enter")

        try:
            _import_trace("before refresh cache")
            _import_trace("after refresh cache")
            _import_trace("before emit finished")
            if ok_name is not None:
                self.emit_import_progress(99, self.STAGE_CACHE)
                self.emit_import_progress(100, self.STAGE_DONE)
                debug_log("[IMPORT] worker finished signal emitted")
                self.finished_ok.emit(ok_name)
            elif cancelled:
                self._cleanup_import_temp("cancel")
                debug_log("[IMPORT] worker cancelled signal emitted")
                self.import_cancelled.emit()
            elif err_message is not None:
                self._cleanup_import_temp("error")
                debug_log(f"[IMPORT] worker error signal emitted: {err_message}")
                self.finished_err.emit(err_message)
            _import_trace("after emit finished")
            debug_log("[IMPORT] finished")
        except Exception as exc:
            debug_log(f"[IMPORT] worker signal emit failed: {exc}")
            debug_log(traceback.format_exc())
        finally:
            _import_trace("finally exit")


class ImageDeleteWorker(QThread):
    """Список и удаление образов Cicada USB в фоне (без блокировки UI)."""

    list_finished = pyqtSignal(list)
    delete_started = pyqtSignal(str)
    delete_progress = pyqtSignal(str)
    delete_finished = pyqtSignal()
    delete_error = pyqtSignal(str)
    finished_err = pyqtSignal(object)

    STAGE_CHECK = "Проверка флешки"
    STAGE_REVEAL = "Открытие раздела"
    STAGE_DELETE = "Удаление файла"
    STAGE_HIDE = "Скрытие раздела"
    STAGE_DONE = "Готово"

    def __init__(self, disk: UsbDisk, entry: ImageEntry | None = None):
        super().__init__()
        self.disk = disk
        self.entry = entry
        self.entry_path = entry.path if entry is not None else None

    def _emit_progress(self, stage: str) -> None:
        self.delete_progress.emit(stage)

    def run(self) -> None:
        from cicada_errors import debug_log

        total_started = time.perf_counter()
        try:
            if self.entry_path is None:
                self._run_list(debug_log, total_started)
            else:
                self.delete_started.emit(self.entry_path.name)
                with ntfs_disk_lock(self.disk.number):
                    self._run_delete(debug_log, total_started)
                debug_log(
                    f"[DELETE] total finished in {time.perf_counter() - total_started:.2f} sec"
                )
                self.delete_finished.emit()
        except Exception as exc:
            debug_log(
                f"[DELETE] failed in {time.perf_counter() - total_started:.2f} sec: {exc}"
            )
            if self.entry_path is None:
                self.finished_err.emit(exc)
            else:
                self.delete_error.emit(str(exc))

    def _run_list(self, debug_log, total_started: float) -> None:
        list_started = time.perf_counter()
        stats = get_validated_partition_stats(self.disk)
        if stats is not None and sum(
            int(stats[k]) for k in ("windows", "linux", "winpe")
        ) == 0:
            perf_log("delete list", list_started)
            self.list_finished.emit([])
            return

        with cicada_ntfs_access(self.disk.number) as ntfs:
            if not is_cicada_usb(ntfs):
                raise RuntimeError(
                    "Флаг Cicada3301 не найден или повреждён.\n"
                    "Сначала создайте загрузочную флешку."
                )
            data_root = cicada_data_root(ntfs)
            entries = list_deletable_images(data_root)
            for entry in entries:
                _log_delete_list_entry(entry, data_root)
            try:
                refresh_disk_cache_from_ntfs(
                    self.disk.number,
                    ntfs,
                    unique_id=self.disk.unique_id,
                    model=self.disk.model,
                    size_bytes=self.disk.size_bytes,
                )
            except OSError:
                pass
        perf_log("delete list", list_started)
        self.list_finished.emit(entries)

    def _run_delete(self, debug_log, total_started: float) -> None:
        if self.entry is None:
            raise RuntimeError("Не указан образ для удаления")
        debug_log(f"[DELETE] deleting: {self.entry.name}")

        self._emit_progress(self.STAGE_CHECK)
        if not self.disk.is_cicada:
            raise RuntimeError(
                "Cicada USB Boot не обнаружен.\n"
                "Сначала создайте загрузочную флешку."
            )

        self._emit_progress(self.STAGE_REVEAL)
        with cicada_ntfs_access(
            self.disk.number,
            timeout=IMPORT_REVEAL_TIMEOUT_SEC,
        ) as ntfs_root:
            if not is_cicada_usb(ntfs_root):
                raise RuntimeError(
                    "Флаг Cicada3301 не найден или повреждён.\n"
                    "Сначала создайте загрузочную флешку."
                )

            self._emit_progress(self.STAGE_DELETE)
            data_root = cicada_data_root(ntfs_root)
            debug_log(f"[DELETE] ntfs_root = {ntfs_root}")
            debug_log(f"[DELETE] data_root = {data_root}")
            debug_log(f"[DELETE] category = {self.entry.category}")
            debug_log(f"[DELETE] relative_path = {self.entry.relative_path}")

            try:
                target = resolve_image_entry_path(data_root, self.entry)
            except FileNotFoundError:
                debug_log(
                    "[DELETE] file already missing, updating cache: "
                    f"{self.entry.relative_path}"
                )
                if apply_disk_cache_after_delete(self.disk, self.entry) is None:
                    raise RuntimeError(
                        "Файл не найден на флешке и локальный кеш статистики недоступен."
                    )
                self._emit_progress(self.STAGE_HIDE)
                return

            size_before = target.stat().st_size
            debug_log(f"[DELETE] resolved target = {target}")
            debug_log(f"[DELETE] exists before delete = {target.exists()}")
            debug_log(f"[DELETE] size before delete = {size_before}")

            delete_image_file(target)

            if target.exists():
                raise RuntimeError(f"Файл не удалён: {target}")
            debug_log("[DELETE] exists after delete = False")

            if apply_disk_cache_after_delete(
                self.disk, self.entry, size_bytes=size_before
            ) is None:
                raise RuntimeError(
                    "Файл удалён, но не удалось обновить локальный кеш статистики."
                )

            if target.exists():
                raise RuntimeError(f"Файл не удалён после обновления кеша: {target}")
            debug_log(f"[DELETE] final exists = {target.exists()}")
            debug_log("[DELETE] file deleted")

            self._emit_progress(self.STAGE_HIDE)

        self._emit_progress(self.STAGE_DONE)


# LEGACY UI CODE — НЕ ИСПОЛЬЗУЕТСЯ.
# Текущий интерфейс находится в cicada_usb_tool_frosted.py.
# Не редактировать без необходимости.

APP_STYLESHEET = """
QMainWindow {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #0a0a14, stop:0.45 #12122a, stop:1 #0f1020);
}
QWidget#centralRoot { background: transparent; }
QFrame#heroCard {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(43, 233, 154, 0.12), stop:0.5 rgba(240, 41, 190, 0.08), stop:1 rgba(43, 233, 154, 0.05));
    border: 1px solid rgba(43, 233, 154, 0.25);
    border-radius: 16px;
}
QFrame#panelCard {
    background: rgba(22, 22, 40, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 14px;
}
QFrame#partCard {
    background: rgba(14, 14, 28, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 10px;
}
QFrame#partCardNtfs { border-left: 3px solid #2be99a; }
QFrame#partCardFat  { border-left: 3px solid #f029be; }
QLabel#brandTitle {
    color: #2be99a;
    font-size: 26px;
    font-weight: 700;
    letter-spacing: 1px;
}
QLabel#brandSub {
    color: rgba(232, 232, 240, 0.75);
    font-size: 13px;
}
QLabel#badge {
    background: rgba(43, 233, 154, 0.12);
    border: 1px solid rgba(43, 233, 154, 0.35);
    border-radius: 12px;
    padding: 4px 12px;
    color: #2be99a;
    font-size: 11px;
    font-weight: 600;
}
QLabel#badgePink {
    background: rgba(240, 41, 190, 0.12);
    border: 1px solid rgba(240, 41, 190, 0.35);
    color: #ff6fd8;
}
QLabel#sectionTitle {
    color: #e8e8f0;
    font-size: 13px;
    font-weight: 600;
}
QLabel#fieldLabel {
    color: rgba(200, 200, 220, 0.85);
    font-size: 12px;
}
QLabel#partTitle { color: #f0f0ff; font-weight: 600; font-size: 12px; }
QLabel#partMeta  { color: rgba(180, 180, 200, 0.8); font-size: 11px; }
QLabel#statusOk  { color: #2be99a; font-size: 11px; font-weight: 600; }
QLabel#statusWarn { color: #ffd166; font-size: 11px; font-weight: 600; }
QLabel#progressLabel { color: rgba(200, 200, 220, 0.9); font-size: 11px; }
QComboBox {
    background: rgba(10, 10, 22, 0.9);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    padding: 10px 14px;
    color: #f4f4ff;
    min-height: 22px;
}
QComboBox:hover, QComboBox:focus { border-color: #f029be; }
QComboBox::drop-down { border: none; width: 30px; }
QComboBox QAbstractItemView {
    background: #161628;
    color: #f0f0ff;
    border: 1px solid rgba(240, 41, 190, 0.4);
    selection-background-color: #f029be;
    outline: 0;
}
QPushButton {
    background: rgba(38, 38, 62, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    padding: 10px 18px;
    color: #ececf8;
    font-weight: 600;
    font-size: 12px;
}
QPushButton:hover {
    border-color: rgba(240, 41, 190, 0.65);
    background: rgba(50, 50, 78, 0.98);
}
QPushButton:disabled {
    color: rgba(120, 120, 140, 0.8);
    border-color: rgba(255, 255, 255, 0.04);
    background: rgba(28, 28, 42, 0.7);
}
QPushButton#primaryBtn {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #b01590, stop:0.5 #e028b8, stop:1 #ff4fd8);
    border: 1px solid rgba(255, 120, 220, 0.45);
    color: white;
    font-size: 13px;
    padding: 12px 24px;
}
QPushButton#primaryBtn:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #c820a8, stop:0.5 #f040d0, stop:1 #ff70e8);
}
QTextEdit#logView {
    background: rgba(6, 6, 14, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
    color: #b8e8b8;
    padding: 12px;
    font-family: Consolas, 'Cascadia Mono', monospace;
    font-size: 11px;
    selection-background-color: rgba(240, 41, 190, 0.35);
}
QProgressBar {
    background: rgba(14, 14, 28, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 9px;
    text-align: center;
    color: transparent;
    min-height: 14px;
    max-height: 14px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2be99a, stop:0.5 #1fd4a0, stop:1 #f029be);
    border-radius: 8px;
}
QDialog#cicadaDialog {
    background: #141428;
    border: 1px solid rgba(240, 41, 190, 0.35);
    border-radius: 14px;
}
QLabel#dialogTitle { color: #2be99a; font-size: 16px; font-weight: 700; }
QLabel#dialogBody  { color: #dcdcf0; font-size: 13px; }
QPushButton#dialogPrimary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #c01898, stop:1 #f029be);
    border: none; color: white; border-radius: 8px; padding: 8px 20px;
}
QPushButton#dialogSecondary {
    background: rgba(40, 40, 62, 0.95);
    border: 1px solid rgba(255,255,255,0.12);
    color: #e0e0f0; border-radius: 8px; padding: 8px 20px;
}
QPushButton#dialogDanger {
    background: rgba(180, 40, 70, 0.85);
    border: 1px solid rgba(255, 100, 130, 0.5);
    color: white; border-radius: 8px; padding: 8px 20px;
}
"""


def _shadow(widget: QWidget, blur: int = 28, y: int = 8) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, y)
    effect.setColor(QColor(0, 0, 0, 110))
    widget.setGraphicsEffect(effect)


class CicadaDialog(QDialog):
    """Стилизованный диалог вместо стандартного QMessageBox."""

    YES = 1
    NO = 0

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        message: str,
        *,
        kind: str = "info",
        confirm: bool = False,
        danger_confirm: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("cicadaDialog")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self._result = self.NO

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        card = QFrame()
        card.setObjectName("cicadaDialog")
        card.setStyleSheet(
            "QFrame#cicadaDialog { background: #141428; border-radius: 14px; "
            "border: 1px solid rgba(240, 41, 190, 0.35); }"
        )
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(14)

        icons = {
            "info": "✓",
            "warning": "⚠",
            "error": "✕",
            "admin": "🛡",
        }
        head = QHBoxLayout()
        icon = QLabel(icons.get(kind, "●"))
        icon.setStyleSheet(
            "font-size: 22px; min-width: 32px; color: #f029be;"
            if kind != "info"
            else "font-size: 22px; min-width: 32px; color: #2be99a;"
        )
        head.addWidget(icon)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("dialogTitle")
        head.addWidget(title_lbl, stretch=1)
        lay.addLayout(head)

        body = QLabel(message)
        body.setObjectName("dialogBody")
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(body)

        btns = QHBoxLayout()
        btns.addStretch()
        if confirm or danger_confirm:
            no_btn = QPushButton("Отмена")
            no_btn.setObjectName("dialogSecondary")
            no_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            no_btn.clicked.connect(self.reject)
            btns.addWidget(no_btn)
            yes_btn = QPushButton("Да, продолжить" if danger_confirm else "Да")
            yes_btn.setObjectName("dialogDanger" if danger_confirm else "dialogPrimary")
            yes_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            yes_btn.clicked.connect(self._accept_yes)
            btns.addWidget(yes_btn)
        elif kind == "admin":
            ok_btn = QPushButton("Запустить от администратора")
            ok_btn.setObjectName("dialogPrimary")
            ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            ok_btn.clicked.connect(self._accept_yes)
            btns.addWidget(ok_btn)
        else:
            ok_btn = QPushButton("OK")
            ok_btn.setObjectName("dialogPrimary")
            ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            ok_btn.clicked.connect(self._accept_yes)
            btns.addWidget(ok_btn)
        lay.addLayout(btns)

        self.setMinimumWidth(420)
        _shadow(card, blur=40, y=12)

    def _accept_yes(self) -> None:
        self._result = self.YES
        self.accept()

    @classmethod
    def ask(
        cls,
        parent: QWidget | None,
        title: str,
        message: str,
        *,
        danger: bool = False,
    ) -> bool:
        dlg = cls(parent, title, message, kind="warning", confirm=True, danger_confirm=danger)
        dlg.exec()
        return dlg._result == cls.YES

    @classmethod
    def inform(cls, parent: QWidget | None, title: str, message: str, kind: str = "info") -> None:
        dlg = cls(parent, title, message, kind=kind)
        dlg.exec()


def ensure_admin_at_startup() -> None:
    """Без прав администратора — только предупреждение и перезапуск UAC (без главного меню)."""
    if is_admin():
        return
    from cicada_errors import classify_admin_error

    classified = classify_admin_error()
    CicadaDialog.inform(None, classified.title, classified.message, kind="admin")
    relaunch_as_admin()
    sys.exit(0)


class CicadaUsbTool(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.base_dir = cicada_temp_dir()
        self.worker: CreateWorker | DeleteCicadaWorker | None = None
        self.selected_disk_is_cicada = False
        self._build_ui()
        self.refresh_disks(initial=True)

    def _present_error(self, exc: object, *, allow_retry: bool = False) -> bool:
        from cicada_errors import classify_exception, exception_log_summary, log_exception

        if isinstance(exc, BaseException):
            error = exc
        else:
            error = RuntimeError(str(exc))
        classified = classify_exception(error)
        log_exception(error, classified.code)
        self.append_log(exception_log_summary(error, classified), "err")
        if allow_retry and classified.retryable:
            return CicadaDialog.ask(
                self,
                classified.title,
                classified.message,
                danger=False,
            )
        CicadaDialog.inform(self, classified.title, classified.message, kind="error")
        return False

    def _build_ui(self) -> None:
        self.setWindowTitle(APP_TITLE)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setMinimumSize(860, 640)
        self.resize(920, 680)

        central = QWidget()
        central.setObjectName("centralRoot")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(18)

        hero = QFrame()
        hero.setObjectName("heroCard")
        hero_lay = QHBoxLayout(hero)
        hero_lay.setContentsMargins(22, 18, 22, 18)
        hero_lay.setSpacing(16)

        hero_text = QVBoxLayout()
        hero_text.setSpacing(6)
        brand = QLabel("Cicada3301")
        brand.setObjectName("brandTitle")
        sub = QLabel("Создание загрузочной USB · agFM / Easy2Boot")
        sub.setObjectName("brandSub")
        hero_text.addWidget(brand)
        hero_text.addWidget(sub)
        hero_lay.addLayout(hero_text, stretch=1)

        badges = QVBoxLayout()
        badges.setSpacing(8)
        self.badge_disks = QLabel("● USB: —")
        self.badge_disks.setObjectName("badge")
        self.badge_7z = QLabel("● 7-Zip: —")
        self.badge_7z.setObjectName("badgePink")
        badges.addWidget(self.badge_disks)
        badges.addWidget(self.badge_7z)
        hero_lay.addLayout(badges)
        _shadow(hero)
        root.addWidget(hero)

        parts_row = QHBoxLayout()
        parts_row.setSpacing(12)
        self.part_ntfs_status = QLabel("Ожидание проверки флага")
        self.part_ntfs_status.setObjectName("partMeta")
        ntfs_card = self._make_part_card(
            "partCardNtfs", "Раздел 1", "NTFS · Cicada3301",
            "Данные, ISO-образы", "≈ весь диск − 1.5 GB",
        )
        ntfs_card.layout().addWidget(self.part_ntfs_status)
        parts_row.addWidget(ntfs_card)
        self.part_fat_status = QLabel("Скрытый раздел BOOT")
        self.part_fat_status.setObjectName("partMeta")
        fat_card = self._make_part_card(
            "partCardFat", "Раздел 2", "FAT32 · BOOT",
            "Загрузчик agFM", "1.5 GB · скрытый",
        )
        fat_card.layout().addWidget(self.part_fat_status)
        parts_row.addWidget(fat_card)
        root.addLayout(parts_row)

        panel = QFrame()
        panel.setObjectName("panelCard")
        panel_lay = QVBoxLayout(panel)
        panel_lay.setContentsMargins(20, 18, 20, 18)
        panel_lay.setSpacing(14)

        field = QLabel("Выберите диск (USB / Removable)")
        field.setObjectName("fieldLabel")
        panel_lay.addWidget(field)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)
        self.disk_combo = QComboBox()
        self.disk_combo.setMinimumHeight(44)
        self.disk_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ctrl.addWidget(self.disk_combo, stretch=1)

        self.refresh_btn = QPushButton("↻  Обновить")
        self.refresh_btn.setMinimumHeight(44)
        self.refresh_btn.setMinimumWidth(130)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(lambda: self.refresh_disks(initial=False))
        ctrl.addWidget(self.refresh_btn)

        self.create_btn = QPushButton("⚡  Создать флешку")
        self.create_btn.setObjectName("primaryBtn")
        self.create_btn.setMinimumHeight(44)
        self.create_btn.setMinimumWidth(190)
        self.create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_btn.clicked.connect(self.start_create)
        ctrl.addWidget(self.create_btn)

        self.delete_cicada_btn = QPushButton("🗑  Удалить CICADA USB BOOT")
        self.delete_cicada_btn.setMinimumHeight(44)
        self.delete_cicada_btn.setMinimumWidth(240)
        self.delete_cicada_btn.setEnabled(False)
        self.delete_cicada_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_cicada_btn.clicked.connect(self.start_delete_cicada)
        ctrl.addWidget(self.delete_cicada_btn)
        panel_lay.addLayout(ctrl)
        self.disk_combo.currentIndexChanged.connect(self._on_disk_changed)
        _shadow(panel)
        root.addWidget(panel)

        log_panel = QFrame()
        log_panel.setObjectName("panelCard")
        log_lay = QVBoxLayout(log_panel)
        log_lay.setContentsMargins(20, 16, 20, 16)
        log_lay.setSpacing(10)

        log_head = QHBoxLayout()
        log_title = QLabel("Журнал операций")
        log_title.setObjectName("sectionTitle")
        log_head.addWidget(log_title)
        log_head.addStretch()
        self.status_label = QLabel("Готов к работе")
        self.status_label.setObjectName("statusOk")
        log_head.addWidget(self.status_label)
        log_lay.addLayout(log_head)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(220)
        log_lay.addWidget(self.log_view, stretch=1)

        prog_row = QHBoxLayout()
        self.progress_label = QLabel("0%")
        self.progress_label.setObjectName("progressLabel")
        self.progress_label.setMinimumWidth(36)
        prog_row.addWidget(self.progress_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        prog_row.addWidget(self.progress, stretch=1)
        log_lay.addLayout(prog_row)
        _shadow(log_panel)
        root.addWidget(log_panel, stretch=1)

        self.setStyleSheet(APP_STYLESHEET)

    def _make_part_card(
        self, obj_name: str, num: str, title: str, desc: str, size: str
    ) -> QFrame:
        card = QFrame()
        card.setObjectName("partCard")
        card.setProperty("class", obj_name)
        card.setStyleSheet(
            f"QFrame#partCard {{ border-left: 3px solid "
            f"{'#2be99a' if 'Ntfs' in obj_name else '#f029be'}; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(4)
        n = QLabel(num)
        n.setObjectName("partMeta")
        t = QLabel(title)
        t.setObjectName("partTitle")
        d = QLabel(desc)
        d.setObjectName("partMeta")
        s = QLabel(size)
        s.setStyleSheet("color: rgba(150,150,180,0.75); font-size: 10px;")
        lay.addWidget(n)
        lay.addWidget(t)
        lay.addWidget(d)
        lay.addWidget(s)
        return card

    def append_log(self, text: str, level: str = "info") -> None:
        colors = {
            "info": "#9ed4ff",
            "ok": "#2be99a",
            "warn": "#ffd166",
            "err": "#ff6b8a",
            "dim": "#6a6a88",
        }
        ts = time.strftime("%H:%M:%S")
        color = colors.get(level, colors["info"])
        safe = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        self.log_view.append(
            f'<span style="color:#555570">[{ts}]</span> '
            f'<span style="color:{color}">{safe}</span>'
        )
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )

    def _set_status(self, text: str, ok: bool = True) -> None:
        self.status_label.setText(text)
        self.status_label.setObjectName("statusOk" if ok else "statusWarn")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _set_progress(self, value: int) -> None:
        self.progress.setValue(value)
        self.progress_label.setText(f"{value}%")

    def _update_cicada_ui_state(self) -> None:
        disk = self._selected_disk()
        self.selected_disk_is_cicada = bool(disk and disk.is_cicada)
        busy = bool(self.worker and self.worker.isRunning())
        if self.selected_disk_is_cicada:
            self.create_btn.setEnabled(False)
            self.create_btn.setText("✓  ФЛЕШКА УЖЕ СОЗДАНА")
            self.part_ntfs_status.setText("Флаг Cicada найден · защищён")
            self.part_fat_status.setText("BOOT скрыт (ID 17)")
            self._set_status("Cicada USB Boot обнаружен · Режим управления образами", ok=True)
        else:
            if not busy:
                self.create_btn.setEnabled(True)
                self.create_btn.setText("⚡  Создать флешку")
            self.part_ntfs_status.setText("Флаг не найден")
            self.part_fat_status.setText("Скрытый раздел BOOT")
            if not busy:
                self._set_status("Готов к работе", ok=True)
        self.delete_cicada_btn.setEnabled(self.selected_disk_is_cicada and not busy)

    def _on_disk_changed(self) -> None:
        self._update_cicada_ui_state()

    def refresh_disks(self, initial: bool = False) -> None:
        if not initial:
            self.append_log("Обновление списка дисков...", "info")
        self.refresh_btn.setEnabled(False)
        QApplication.processEvents()
        try:
            disks = list_usb_disks_with_cicada_flags()
            self.disk_combo.clear()
            for disk in disks:
                self.disk_combo.addItem(f"💾  {disk.label()}", disk)
                if disk.is_cicada:
                    self.append_log(f"Cicada USB Boot обнаружен на диске {disk.number}", "ok")
            self.badge_disks.setText(f"● USB: {len(disks)}")
            cicada_count = sum(1 for d in disks if d.is_cicada)
            self.append_log(
                f"Найдено дисков: {len(disks)} (Cicada: {cicada_count}).",
                "ok" if disks else "warn",
            )
            self.badge_7z.setText("● 7-Zip: GitHub")
            if not initial:
                self.append_log("7-Zip будет загружен при создании флешки.", "ok")
            self._on_disk_changed()
        except Exception as exc:
            self._present_error(exc)
            self._set_status("Ошибка", ok=False)
        finally:
            self.refresh_btn.setEnabled(True)

    def _selected_disk(self) -> UsbDisk | None:
        data = self.disk_combo.currentData()
        return data if isinstance(data, UsbDisk) else None

    def start_delete_cicada(self, *, skip_confirm: bool = False) -> None:
        if self.worker and self.worker.isRunning():
            return
        disk = self._selected_disk()
        if disk is None or not disk.is_cicada:
            CicadaDialog.inform(
                self, "Нет Cicada USB", "На выбранном диске нет флага Cicada3301.", kind="warning"
            )
            return
        if not skip_confirm and not CicadaDialog.ask(
            self,
            "Удалить CICADA USB BOOT",
            "Это полностью удалит структуру Cicada USB Boot с выбранной флешки.\n"
            "Все разделы будут удалены.\n"
            "Флешка будет очищена и создан один обычный раздел.\n\n"
            "Продолжить?",
            danger=True,
        ):
            return
        self._set_status("Удаление Cicada USB Boot...", ok=False)
        self.create_btn.setEnabled(False)
        self.delete_cicada_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.disk_combo.setEnabled(False)
        self.worker = DeleteCicadaWorker(disk)
        self.worker.log.connect(lambda t: self.append_log(t, "info"))
        self.worker.finished_ok.connect(self._on_delete_success)
        self.worker.finished_err.connect(self._on_delete_error)
        self.worker.start()

    def _on_delete_success(self) -> None:
        self.create_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.disk_combo.setEnabled(True)
        self._set_status("Флешка возвращена в обычный режим", ok=True)
        invalidate_usb_scan_cache()
        self.refresh_disks(initial=False)

    def _on_delete_error(self, exc: object) -> None:
        self.create_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.disk_combo.setEnabled(True)
        self._set_status("Ошибка", ok=False)
        self._update_cicada_ui_state()
        if self._present_error(exc, allow_retry=True):
            self.start_delete_cicada(skip_confirm=True)

    def start_create(self, *, skip_confirm: bool = False) -> None:
        if self.worker and self.worker.isRunning():
            return

        disk = self._selected_disk()
        if disk is None:
            CicadaDialog.inform(
                self, "Нет диска", "Выберите USB-диск из списка.", kind="warning"
            )
            return
        if disk.is_cicada:
            CicadaDialog.inform(
                self,
                "Флешка уже создана",
                "На выбранном диске уже есть Cicada USB Boot.",
                kind="info",
            )
            return

        if not skip_confirm and not CicadaDialog.ask(
            self,
            "Подтверждение",
            f"Все данные на диске {disk.number} будут безвозвратно удалены.\n\n"
            f"{disk.label()}\n\n"
            "Продолжить создание загрузочной флешки?",
            danger=True,
        ):
            return

        self._set_progress(0)
        self._set_status("Выполняется...", ok=False)
        self.create_btn.setEnabled(False)
        self.create_btn.setText("⏳  Работа...")
        self.refresh_btn.setEnabled(False)
        self.disk_combo.setEnabled(False)

        assets_dir, download_missing = resolve_assets_dir()
        self.worker = CreateWorker(
            disk, assets_dir, download_missing=download_missing
        )
        self.worker.log.connect(lambda t: self.append_log(t, "info"))
        self.worker.progress.connect(self._set_progress)
        self.worker.finished_ok.connect(self._on_success)
        self.worker.finished_err.connect(self._on_error)
        self.worker.start()

    def _on_success(self) -> None:
        self.refresh_btn.setEnabled(True)
        self.disk_combo.setEnabled(True)
        self._set_status("Готово!", ok=True)
        self.append_log("Загрузочная флешка успешно создана.", "ok")
        self.refresh_disks(initial=False)
        CicadaDialog.inform(
            self,
            "Готово",
            "Загрузочная флешка Cicada3301 успешно создана!\n\n"
            "• Раздел 1 — NTFS Cicada3301 (данные и ISO, видимый)\n"
            "• Раздел 2 — FAT32 BOOT (загрузчик, скрытый)\n\n"
            "Безопасно извлеките USB через значок в трее Windows.",
            kind="info",
        )

    def _on_error(self, exc: object) -> None:
        self.refresh_btn.setEnabled(True)
        self.disk_combo.setEnabled(True)
        self._set_status("Ошибка", ok=False)
        self._update_cicada_ui_state()
        if self._present_error(exc, allow_retry=True):
            self.start_create(skip_confirm=True)


def main() -> int:
    from cicada_usb_tool_frosted import main as frosted_main

    return frosted_main()


if __name__ == "__main__":
    sys.exit(main())
