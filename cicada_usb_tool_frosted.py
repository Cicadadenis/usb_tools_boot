#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cicada USB Boot Tool — Frosted Glass Edition v2.1
Интерфейс программы. Логика (диски, загрузка, 7z) — в cicada_usb_tool.py.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import (
    Qt,
    QTimer,
    QRectF,
    QSize,
    QRect,
    QThread,
    pyqtSignal,
    QEvent,
    QEventLoop,
    QPropertyAnimation,
    QEasingCurve,
    QAbstractAnimation,
    QSequentialAnimationGroup,
)
from PyQt6.QtGui import (
    QColor, QFont, QIcon, QPalette, QPainter, QPixmap,
    QPen, QBrush, QTransform, QShowEvent, QMouseEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
    QFileDialog,
)

from cicada_errors import (
    ClassifiedError,
    ErrorCategory,
    classify_admin_error,
    classify_exception,
    debug_log,
    exception_log_summary,
    is_logging_enabled,
    log_exception,
    set_logging_enabled,
)
from cicada_usb_tool import (
    CreateWorker,
    DeleteCicadaWorker,
    ImageEntry,
    ImageImportWorker,
    ImageDeleteWorker,
    UsbDisk,
    cicada_temp_dir,
    migrate_runtime_files_to_temp,
    apply_windows_taskbar_icon,
    resource_path,
    cicada_mbr_signature_display,
    CICADA_MBR_COLLISION_UI_MESSAGE,
    CICADA_MBR_COLLISION_UI_MESSAGE_MULTILINE,
    clear_cached_partition_stats,
    clear_cicada_flag_verified_cache,
    disk_identity_key,
    is_cicada_flag_verified_cached,
    mark_cicada_flag_verified_cached,
    DISK_PROBE_TIMEOUT_SEC,
    format_file_size,
    disable_fast_mount_for_device,
    is_fast_mount_diagnostic_error,
    apply_disk_cache_after_delete,
    apply_disk_cache_after_import,
    get_validated_partition_stats,
    hide_console_window,
    is_admin,
    format_user_error_message,
    invalidate_disk_stats_cache,
    invalidate_usb_scan_cache,
    list_usb_disks_fast_cached,
    load_app_icon,
    perf_log,
    relaunch_as_admin,
    resolve_assets_dir,
    refresh_disk_cache_from_ntfs,
    refresh_disk_cache_for_usb,
    restore_cicada_usb_flag,
    setup_windows_app_id,
    verify_cicada_usb_flag,
)

APP_TITLE = "Cicada USB Boot Tool"
APP_VERSION = "2.1"

# === ЦВЕТОВАЯ ПАЛИТРА ===
COLORS = {
    "bg_primary":   "#050914",
    "bg_panel":     "#0B1220",
    "bg_card":      "#0E172A",
    "bg_sidebar":   "#0B1220",
    "border":       "#00E5FF",
    "text_primary": "#E5E7EB",
    "text_muted":   "#94A3B8",
    "accent_cyan":  "#00E5FF",
    "accent_blue":  "#2563EB",
    "accent_purple":"#7C3AED",
    "accent_green": "#22C55E",
    "accent_yellow":"#FBBF24",
    "accent_red":   "#FB7185",
    "shadow":       "#000000",
}

PANEL_BORDER = "rgba(0,229,255,0.22)"
LABEL_PLAIN = "background: transparent; border: none; padding: 0; margin: 0;"

UI_SCALE = 0.75
SIDEBAR_WIDTH = max(300, round(400 * UI_SCALE))


def _s(value: int | float) -> int:
    return max(10 if value >= 10 else 8, round(value * UI_SCALE))


CONTENT_GAP_FROM_SIDEBAR = _s(10)


FONTS = {
    "hero":       _s(32),
    "card_title": _s(20),
    "body":       _s(16),
    "nav":        _s(18),
    "small":      _s(13),
    "button":     _s(24),
    "step_title": _s(22),
}

WINDOWS_VARIANTS = (
    ("XP", "XP"),
    ("VISTA", "VISTA"),
    ("Windows 7", "WIN7"),
    ("Windows 8", "WIN8"),
    ("Windows 10", "WIN10"),
    ("Windows 11", "WIN11"),
)

# (отображаемое имя, папка, описание)
WINDOWS_VARIANT_CARDS = (
    ("Windows XP", "XP", "Legacy BIOS"),
    ("Windows Vista", "VISTA", "Legacy Systems"),
    ("Windows 7", "WIN7", "Legacy + UEFI"),
    ("Windows 8", "WIN8", "UEFI Ready"),
    ("Windows 10", "WIN10", "Modern Systems"),
    ("Windows 11", "WIN11", "TPM / Secure Boot"),
)

LINUX_VARIANTS = (
    ("DEBIAN", "DEBIAN"),
    ("KALI", "KALI"),
    ("UBUNTU", "UBUNTU"),
)

LINUX_VARIANT_CARDS = (
    ("Debian", "DEBIAN", "Stable / Server"),
    ("Kali Linux", "KALI", "Security / NetHunter"),
    ("Ubuntu", "UBUNTU", "Desktop / Server"),
)

BUILD_STEPS = (
    ("Поиск USB",           "Устройство найдено"),
    ("Создание разделов",   "Разделы созданы"),
    ("Форм-ние",      "Диск отформ-ван"),
    ("Установка загрузчика","Загрузчик установлен"),
    ("Коп-ние файлов",  "Файлы скопированы"),
    ("Создание меню",       "Меню создано"),
    ("Завер-ние",          "Готово к использованию"),
)


class AppState(str, Enum):
    IDLE = "idle"
    PICKING_FILE = "picking_file"
    IMPORTING = "importing"
    DELETING = "deleting"
    CREATING = "creating"
    SCANNING = "scanning"


_BUSY_STATES = frozenset({
    AppState.IMPORTING,
    AppState.DELETING,
    AppState.CREATING,
    AppState.SCANNING,
})

# (title, icon_key, action, subtitle, counter_key|None)
MEDIA_NAV_ITEMS = (
    ("WINDOWS", "windows", "add_windows", "ISO · WIM · ESD", "windows"),
    ("LINUX",   "linux",   "add_linux",   "ISO · IMG",       "linux"),
    ("WINPE",   "winpe",   "add_winpe",   "WinPE · WIM",     "winpe"),
)

MANAGEMENT_NAV_ITEMS = (
    ("Удалить образ",       "trash",         "delete_image",  "Удаление с флешки"),
    ("Сбросить USB Boot", "delete_cicada", "delete_cicada", "Полный сброс разметки"),
)

NAV_FLASH_ACTIONS = frozenset({
    "add_windows",
    "add_linux",
    "add_winpe",
    "delete_image",
    "delete_cicada",
})

# Иконки для кружков этапов
STEP_ICON_KEYS = (
    "search",
    "partition",
    "format_hdd",
    "download",
    "copy_file",
    "menu_lines",
    "flag",
)


# ═══════════════════════════════════════════════════════════════════════
#  Icons — PNG из img/256/
# ═══════════════════════════════════════════════════════════════════════

ICON_SIZE_NAV = _s(30)
ICON_SIZE_SIDEBAR = _s(38)
ICON_SIZE_CARD = _s(44)
ICON_SIZE_FOOTER = _s(80)
ICON_SIZE_STEP = _s(76)
ICON_SIZE_BTN = _s(38)

PNG_ICON_FILES: dict[str, str] = {
    "home": "img/256/01_home.png",
    "windows": "img/256/win.png",
    "linux": "img/256/03_linux.png",
    "winpe": "img/256/04_winpe.png",
    "trash": "img/256/05_delete_image.png",
    "delete_cicada": "img/256/21_error.png",
    "about": "img/256/07_about.png",
    "usb_device": "img/256/08_usb_device.png",
    "cicada": "img/256/cicada.png",
    "usb_drive": "img/256/dc3a2352-f3f0-4324-a510-f348d5d404d0.png",
    "ntfs": "img/256/11_ntfs.png",
    "fat32": "img/256/12_fat32.png",
    "create_bootable": "img/256/13_create_bootable_usb.png",
    "bootloader": "img/256/14_bootloader.png",
    "copy_file": "img/256/15_copy_files.png",
    "menu_lines": "img/256/16_create_menu.png",
    "search": "img/256/17_search_usb.png",
    "format_hdd": "img/256/18_format_drive.png",
    "success": "img/256/19_success.png",
    "warning": "img/256/20_warning.png",
    "error": "img/256/21_error.png",
    "folder": "img/256/22_folder.png",
    "iso": "img/256/23_iso_image.png",
    "disk": "img/256/24_disk.png",
    "free_space": "img/256/25_free_space.png",
    "refresh": "img/256/26_refresh.png",
    "tools": "img/256/27_tools.png",
    "backup": "img/256/28_backup.png",
    "antivirus": "img/256/29_antivirus.png",
    "recovery": "img/256/30_recovery.png",
    "windows_colored": "img/256/win.png",
    "linux_colored": "img/256/03_linux.png",
    "winpe_colored": "img/256/04_winpe.png",
    "pie_chart": "img/256/25_free_space.png",
    "partition": "img/256/24_disk.png",
    "download": "img/256/14_bootloader.png",
    "flag": "img/256/19_success.png",
    "shield": "img/256/22_folder.png",
}

DIALOG_ICON_KEYS = {
    "info": "success",
    "warning": "warning",
    "error": "error",
    "retry": "error",
    "admin": "antivirus",
    "question": "warning",
}


class Icons:
    """Загрузка PNG-иконок интерфейса с кэшированием."""

    _cache: dict[tuple[str, int], QPixmap] = {}
    _warned: set[str] = set()

    @staticmethod
    def _c(hex_color: str) -> QColor:
        return QColor(hex_color)

    @classmethod
    def _empty(cls, size: int) -> QPixmap:
        px = QPixmap(max(1, size), max(1, size))
        px.fill(Qt.GlobalColor.transparent)
        return px

    @classmethod
    def pixmap(cls, key: str, size: int) -> QPixmap:
        if size <= 0:
            size = ICON_SIZE_NAV
        cache_key = (key, size)
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        rel = PNG_ICON_FILES.get(key)
        if rel is None:
            if key not in cls._warned:
                print(f"[Icons] Неизвестный ключ иконки: {key}", file=sys.stderr)
                cls._warned.add(key)
            px = cls._empty(size)
            cls._cache[cache_key] = px
            return px

        file_path = resource_path(rel)
        if not file_path.is_file():
            warn_key = str(file_path)
            if warn_key not in cls._warned:
                print(f"[Icons] Файл не найден: {file_path}", file=sys.stderr)
                cls._warned.add(warn_key)
            px = cls._empty(size)
            cls._cache[cache_key] = px
            return px

        source = QPixmap(str(file_path))
        if source.isNull():
            if str(file_path) not in cls._warned:
                print(f"[Icons] Не удалось загрузить: {file_path}", file=sys.stderr)
                cls._warned.add(str(file_path))
            px = cls._empty(size)
            cls._cache[cache_key] = px
            return px

        scaled = source.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        cls._cache[cache_key] = scaled
        return scaled

    @classmethod
    def get(cls, key: str, color: str = "", size: int = ICON_SIZE_NAV) -> QPixmap:
        del color
        return cls.pixmap(key, size)

    @classmethod
    def windows_colored(cls, size: int = ICON_SIZE_CARD) -> QPixmap:
        return cls.pixmap("windows", size)

    @classmethod
    def linux_colored(cls, size: int = ICON_SIZE_CARD) -> QPixmap:
        return cls.pixmap("linux", size)

    @classmethod
    def winpe_colored(cls, size: int = ICON_SIZE_CARD) -> QPixmap:
        return cls.pixmap("winpe", size)


# ═══════════════════════════════════════════════════════════════════════
# #  Helper functions
# ═══════════════════════════════════════════════════════════════════════

def _shadow(widget: QWidget, blur: int = 12, y: int = 2) -> None:
    eff = QGraphicsDropShadowEffect()
    eff.setBlurRadius(blur)
    eff.setColor(QColor(0, 0, 0, 120))
    eff.setOffset(0, y)
    widget.setGraphicsEffect(eff)


def _rgba(hex_color: str, alpha: float) -> str:
    return (
        f"rgba({int(hex_color[1:3], 16)}, "
        f"{int(hex_color[3:5], 16)}, "
        f"{int(hex_color[5:7], 16)}, {alpha})"
    )


def _make_icon_label(icon_key: str, color: str = "", size: int = ICON_SIZE_NAV) -> QLabel:
    del color
    px = Icons.pixmap(icon_key, size)
    lbl = QLabel()
    lbl.setFixedSize(size, size)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setPixmap(px)
    return lbl


def _make_icon_badge(accent: str, size: int = 48) -> QLabel:
    badge = QLabel()
    badge.setFixedSize(size, size)
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    badge.setStyleSheet(f"""
        QLabel {{
            background-color: {_rgba(accent, 0.12)};
            border: 1.5px solid {_rgba(accent, 0.45)};
            border-radius: {size // 2}px;
        }}
    """)
    icon_size = max(ICON_SIZE_CARD, size - 16)
    badge.setPixmap(Icons.pixmap("cicada", icon_size))
    return badge


def _make_app_logo(icon_size: int | None = None) -> QLabel:
    if icon_size is None:
        icon_size = max(ICON_SIZE_CARD, _s(126) - _s(10)) * 2
    lbl = QLabel()
    lbl.setFixedSize(icon_size, icon_size)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(LABEL_PLAIN)
    lbl.setPixmap(Icons.pixmap("cicada", icon_size))
    return lbl


def _make_accent_button(
    text: str, color: str, *, dark: bool = False, primary: bool = False, min_height: int = 38
) -> QPushButton:
    btn = QPushButton(text)
    btn.setMinimumHeight(min_height)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if dark:
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(22,22,43,0.7);
                color: {COLORS['text_muted']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 8px 14px;
                font-weight: 600; font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: rgba(32,32,58,0.9);
                color: {COLORS['text_primary']};
                border-color: rgba(255,255,255,0.15);
            }}
        """)
    elif primary:
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {_rgba(color, 0.18)};
                color: {color};
                border: 1.5px solid {color};
                border-radius: 10px;
                padding: 10px 22px;
                font-weight: 700; font-size: 13px;
            }}
            QPushButton:hover {{ background-color: {_rgba(color, 0.32)}; }}
            QPushButton:disabled {{
                color: {COLORS['text_muted']};
                border: 1px solid {COLORS['border']};
                background-color: rgba(22,22,43,0.4);
            }}
        """)
    else:
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {_rgba(color, 0.15)};
                color: {color};
                border: 1px solid {color};
                border-radius: 8px;
                padding: 8px 18px;
                font-weight: 600; font-size: 12px;
            }}
            QPushButton:hover {{ background-color: {_rgba(color, 0.28)}; }}
            QPushButton:disabled {{
                background-color: rgba(6,10,18,0.72);
                color: rgba(148,163,184,0.34);
                border: 1px solid rgba(148,163,184,0.12);
            }}
        """)
    return btn


def _make_badge(text: str, color: str, *, font_size: int = FONTS["small"]) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"""
        QLabel {{
            background-color: {_rgba(color, 0.1)};
            color: {color};
            border: 1px solid {_rgba(color, 0.35)};
            border-radius: 12px;
            padding: 6px 14px;
            font-size: {font_size}px;
            font-weight: 700;
        }}
    """)
    return lbl


def _make_window_control_button(kind: str) -> QPushButton:
    return _HudWindowButton(kind)


HEADER_BAR_HEIGHT = 86
HEADER_BTN_SIZE = 28
HEADER_BADGE_W = 76
HEADER_BADGE_H = 28
HEADER_LOGO_SIZE = 36


def _make_pro_version_badge(version: str) -> QLabel:
    lbl = QLabel(f"v{version} PRO")
    lbl.setFixedSize(HEADER_BADGE_W, HEADER_BADGE_H)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(f"""
        QLabel {{
            {LABEL_PLAIN}
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #00d9ff, stop:1 #7b4dff);
            color: #ffffff;
            border: 1px solid rgba(0,217,255,0.45);
            border-radius: 8px;
            padding: 0;
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 0.2px;
        }}
    """)
    return lbl


class _HudWindowButton(QPushButton):
    """Кнопки окна — всегда видимый текст/иконка, ярче при hover."""

    _SPECS: dict[str, dict[str, str]] = {
        "minimize": {
            "objectName": "windowMinButton",
            "symbol": "—",
            "color": "#ffcc33",
            "border": "#ffcc33",
        },
        "maximize": {
            "objectName": "windowMaxButton",
            "symbol": "□",
            "color": "#00d9ff",
            "border": "#00d9ff",
        },
        "close": {
            "objectName": "windowCloseButton",
            "symbol": "×",
            "color": "#ff4d4d",
            "border": "#ff4d4d",
        },
    }

    def __init__(self, kind: str, parent: QWidget | None = None):
        spec = self._SPECS[kind]
        super().__init__(spec["symbol"], parent)
        self._kind = kind
        self._fg = spec["color"]
        self._border = spec["border"]
        self._btn_size = HEADER_BTN_SIZE
        self._font_size = 15
        self.setObjectName(spec["objectName"])
        self.setFixedSize(self._btn_size, self._btn_size)
        self.setMinimumSize(self._btn_size, self._btn_size)
        self.setMaximumSize(self._btn_size, self._btn_size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFlat(False)
        self._glow: QGraphicsDropShadowEffect | None = None
        self._apply_style(hover=False)

    def _apply_style(self, *, hover: bool) -> None:
        obj = self.objectName()
        fg = "#ffffff" if hover else self._fg
        bg = _rgba(self._fg, 0.32) if hover else "rgba(255,255,255,0.03)"
        border = self._border
        sz = self._btn_size
        fs = self._font_size
        self.setStyleSheet(f"""
            QPushButton#{obj} {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: {_s(8)}px;
                color: {fg};
                font-size: {fs}px;
                font-weight: 800;
                padding: 0;
                margin: 0;
                min-width: {sz}px;
                max-width: {sz}px;
                min-height: {sz}px;
                max-height: {sz}px;
            }}
        """)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.ButtonText, QColor(fg))
        self.setPalette(pal)

    def _ensure_glow(self) -> QGraphicsDropShadowEffect:
        if self._glow is None:
            self._glow = QGraphicsDropShadowEffect(self)
            self._glow.setOffset(0, 0)
            self.setGraphicsEffect(self._glow)
        return self._glow

    def enterEvent(self, event) -> None:
        self._apply_style(hover=True)
        glow = self._ensure_glow()
        accent = QColor(self._fg)
        glow.setBlurRadius(_s(14))
        glow.setColor(QColor(accent.red(), accent.green(), accent.blue(), 160))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._apply_style(hover=False)
        self.setGraphicsEffect(None)
        self._glow = None
        super().leaveEvent(event)


DEVICE_CONTROL_H = max(48, _s(64))
REFRESH_BTN_H = DEVICE_CONTROL_H
REFRESH_BTN_W = max(140, _s(200))
REFRESH_ICON_SZ = max(22, _s(32))
USB_DROPDOWN_ITEM_H = max(48, _s(56))
USB_SELECTOR_ICON_SZ = max(20, DEVICE_CONTROL_H - _s(12))


def _disk_selector_subtitle_closed(disk: UsbDisk) -> str:
    text = f"{disk.size_gb:.0f} GB · USB"
    if disk.is_cicada:
        text += " · Cicada USB Boot"
    return text


def _disk_selector_subtitle_dropdown(disk: UsbDisk) -> str:
    text = f"{disk.size_gb:.0f} GB · Disk {disk.number}"
    if disk.is_cicada:
        text += " · Cicada USB Boot"
    return text


class UsbDiskItemDelegate(QStyledItemDelegate):

    def __init__(self, combo: QComboBox, parent=None):
        super().__init__(parent)
        self._combo = combo

    def sizeHint(self, option, index):  # noqa: ANN001
        return QSize(option.rect.width(), USB_DROPDOWN_ITEM_H)

    def paint(self, painter, option, index):  # noqa: ANN001
        disk = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(disk, UsbDisk):
            super().paint(painter, option, index)
            return

        painter.save()
        rect = option.rect.adjusted(_s(4), _s(2), -_s(4), -_s(2))
        is_active = index.row() == self._combo.currentIndex()
        is_hover = (
            not is_active
            and bool(option.state & QStyle.StateFlag.State_MouseOver)
        )
        if is_active:
            painter.fillRect(rect, QColor(0, 229, 255, 55))
            painter.setPen(QPen(QColor(0, 229, 255, 200), 1))
            painter.drawRoundedRect(rect, _s(8), _s(8))
        elif is_hover:
            painter.fillRect(rect, QColor(0, 229, 255, 30))

        icon_size = USB_SELECTOR_ICON_SZ
        icon_x = rect.left() + _s(8)
        icon_y = rect.top() + (rect.height() - icon_size) // 2
        icon_px = Icons.pixmap("usb_device", icon_size)
        painter.drawPixmap(icon_x, icon_y, icon_px)

        text_left = icon_x + icon_size + _s(12)
        text_w = rect.right() - text_left
        model_rect = QRect(text_left, rect.top() + _s(6), text_w, _s(20))
        sub_rect = QRect(text_left, rect.top() + _s(26), text_w, _s(18))

        model_font = QFont("Segoe UI", FONTS["small"])
        model_font.setWeight(QFont.Weight.Bold)
        painter.setFont(model_font)
        painter.setPen(QColor(COLORS["text_primary"]))
        painter.drawText(
            model_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            disk.model,
        )

        sub_font = QFont("Segoe UI", FONTS["small"] - 1)
        painter.setFont(sub_font)
        painter.setPen(QColor(COLORS["text_muted"]))
        painter.drawText(
            sub_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            _disk_selector_subtitle_dropdown(disk),
        )
        painter.restore()


class UsbDiskComboBox(QComboBox):
    """QComboBox: при открытии списка подсветка на текущем диске, а не под курсором."""

    def showPopup(self) -> None:
        current = self.currentIndex()
        super().showPopup()
        if current < 0:
            return
        view = self.view()
        model_index = self.model().index(current, 0)
        view.setCurrentIndex(model_index)
        view.scrollTo(model_index)


class UsbDeviceSelector(QFrame):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("usbSelectorCard")
        self.setFixedHeight(DEVICE_CONTROL_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._hovered = False
        self._apply_card_style()

        root = QHBoxLayout(self)
        root.setContentsMargins(_s(10), 0, _s(32), 0)
        root.setSpacing(_s(8))

        self._icon = QLabel()
        self._icon.setFixedSize(USB_SELECTOR_ICON_SZ, USB_SELECTOR_ICON_SZ)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setPixmap(Icons.pixmap("usb_device", USB_SELECTOR_ICON_SZ))

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        text_col.setContentsMargins(0, 0, 0, 0)
        self._model_lbl = QLabel("USB не выбран")
        self._model_lbl.setWordWrap(False)
        self._model_lbl.setFixedHeight(_s(18))
        self._model_lbl.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['text_primary']};"
            f" font-size: {FONTS['small']}px; font-weight: 800;"
        )
        self._sub_lbl = QLabel("Подключите накопитель")
        self._sub_lbl.setWordWrap(False)
        self._sub_lbl.setFixedHeight(_s(16))
        self._sub_lbl.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['text_muted']}; font-size: {_s(11)}px;"
        )
        text_col.addStretch()
        text_col.addWidget(self._model_lbl)
        text_col.addWidget(self._sub_lbl)
        text_col.addStretch()

        self._arrow = QLabel("▼")
        self._arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._arrow.setFixedWidth(_s(20))
        self._arrow.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['accent_cyan']};"
            f" font-size: {FONTS['small']}px; font-weight: 700;"
        )

        root.addWidget(self._icon)
        root.addLayout(text_col, stretch=1)
        root.addWidget(self._arrow)

        for widget in (self._icon, self._model_lbl, self._sub_lbl, self._arrow):
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._combo = UsbDiskComboBox(self)
        self._combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._combo.setItemDelegate(UsbDiskItemDelegate(self._combo))
        self._apply_combo_overlay_style()
        self._combo.currentIndexChanged.connect(lambda _index=0: self.sync_display())
        self._combo.installEventFilter(self)

    @property
    def combo(self) -> QComboBox:
        return self._combo

    def _apply_card_style(self) -> None:
        border_color = _rgba(COLORS["accent_cyan"], 0.85 if self._hovered else 0.55)
        self.setStyleSheet(f"""
            QFrame#usbSelectorCard {{
                background-color: #07111F;
                border: 1px solid {border_color};
                border-radius: {_s(12)}px;
            }}
            QFrame#usbSelectorCard QLabel {{
                {LABEL_PLAIN}
            }}
        """)

    def _apply_combo_overlay_style(self) -> None:
        cyan = COLORS["accent_cyan"]
        self._combo.setStyleSheet(f"""
            QComboBox {{
                background: transparent;
                border: none;
                color: transparent;
                padding: 0;
                margin: 0;
            }}
            QComboBox::drop-down {{
                border: none;
                width: {_s(36)}px;
            }}
            QComboBox::down-arrow {{
                image: none;
                width: 0;
                height: 0;
            }}
            QComboBox QAbstractItemView {{
                background-color: #07111F;
                color: {COLORS['text_primary']};
                border: 1px solid {cyan};
                border-radius: {_s(12)}px;
                padding: {_s(4)}px;
                outline: none;
                selection-background-color: {_rgba(cyan, 0.25)};
            }}
            QComboBox QAbstractItemView::item {{
                min-height: {USB_DROPDOWN_ITEM_H}px;
                padding: 0;
                border: none;
                background: transparent;
            }}
        """)

    def resizeEvent(self, event):  # noqa: ANN001
        super().resizeEvent(event)
        self._combo.setGeometry(0, 0, self.width(), self.height())
        self._combo.raise_()

    def eventFilter(self, obj, event):  # noqa: ANN001
        if obj is self._combo:
            if event.type() == QEvent.Type.Enter:
                self._hovered = True
                self._apply_card_style()
            elif event.type() == QEvent.Type.Leave:
                self._hovered = False
                self._apply_card_style()
        return super().eventFilter(obj, event)

    def sync_display(self) -> None:
        disk = self._combo.currentData(Qt.ItemDataRole.UserRole)
        if not isinstance(disk, UsbDisk):
            self._model_lbl.setText("USB не выбран")
            self._sub_lbl.setText("Подключите накопитель")
            return
        self._model_lbl.setText(disk.model)
        self._sub_lbl.setText(_disk_selector_subtitle_closed(disk))

    def set_scanning(self, title: str, subtitle: str = "") -> None:
        self._model_lbl.setText(title)
        self._sub_lbl.setText(subtitle)
        self.setEnabled(False)

    def set_no_disks(self) -> None:
        self._model_lbl.setText("USB-накопители не найдены")
        self._sub_lbl.setText("Подключите накопитель и нажмите «Обновить»")
        self.setEnabled(True)


class ScanningDotsLabel(QLabel):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base_text = "Сканирование"
        self._dot_phase = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['accent_cyan']};"
            f" font-size: {FONTS['small']}px; font-weight: 700;"
        )
        self.hide()

    def start(self, base_text: str = "Сканирование") -> None:
        self._base_text = base_text
        self._dot_phase = 0
        self._on_tick()
        self.show()
        self._timer.start(420)

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _on_tick(self) -> None:
        self._dot_phase = (self._dot_phase + 1) % 3
        self.setText(self._base_text + "." * (self._dot_phase + 1))


@dataclass
class UsbScanResult:
    disks: list[UsbDisk]


@dataclass
class StatsCacheResult:
    disk_number: int
    disk_identity_key: str
    target_unique_id: str | None
    stats: dict[str, float | int]
    request_id: int = 0


class StatsCacheWorker(QThread):
    """Однократная загрузка статистики в кеш. Останавливается перед IMPORT/DELETE."""

    finished = pyqtSignal(object)
    error = pyqtSignal(int, str)

    def __init__(
        self,
        disk: UsbDisk,
        request_id: int = 0,
        *,
        force_refresh: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.disk = disk
        self.request_id = request_id
        self.target_disk_number = disk.number
        self.target_unique_id = disk.unique_id
        self.target_identity_key = disk_identity_key(disk)
        self.force_refresh = force_refresh

    def run(self) -> None:
        started = time.perf_counter()
        identity = self.target_identity_key
        try:
            if not self.force_refresh:
                stats = get_validated_partition_stats(self.disk)
                if stats is not None:
                    perf_log("stats load", started)
                    self.finished.emit(
                        StatsCacheResult(
                            self.target_disk_number,
                            identity,
                            self.target_unique_id,
                            stats,
                            self.request_id,
                        )
                    )
                    return

            refresh_disk_cache_for_usb(
                self.disk,
                timeout=DISK_PROBE_TIMEOUT_SEC,
                device_key=identity,
            )
            stats = get_validated_partition_stats(self.disk)
            if stats is None:
                raise RuntimeError("Не удалось загрузить статистику раздела")
            perf_log("stats load", started)
            self.finished.emit(
                StatsCacheResult(
                    self.target_disk_number,
                    identity,
                    self.target_unique_id,
                    stats,
                    self.request_id,
                )
            )
        except Exception as exc:
            perf_log("stats load", started)
            debug_log(f"[STATS] cache load failed: {exc}")
            self.error.emit(self.disk.number, format_user_error_message(str(exc)))


@dataclass
class StatsScanResult:
    disk_number: int
    disk_identity_key: str
    stats: dict[str, float | int]
    scan_id: int


class StatsScanWorker(QThread):
    """Пересчёт статистики с NTFS-раздела (force scan после IMPORT/DELETE)."""

    finished = pyqtSignal(object)
    error = pyqtSignal(int, int, str)

    def __init__(self, disk: UsbDisk, scan_id: int, parent=None):
        super().__init__(parent)
        self.disk = disk
        self.scan_id = scan_id

    def run(self) -> None:
        started = time.perf_counter()
        try:
            refresh_disk_cache_for_usb(self.disk)
            stats = get_validated_partition_stats(self.disk)
            if stats is None:
                raise RuntimeError("Не удалось пересчитать статистику раздела")
            perf_log("stats scan", started)
            self.finished.emit(
                StatsScanResult(
                    self.disk.number,
                    disk_identity_key(self.disk),
                    stats,
                    self.scan_id,
                )
            )
        except Exception as exc:
            perf_log("stats scan", started)
            debug_log(f"[STATS] force scan failed: {exc}")
            self.error.emit(
                self.disk.number,
                self.scan_id,
                format_user_error_message(str(exc)),
            )


class FlagRestoreWorker(QThread):
    finished_ok = pyqtSignal()
    finished_err = pyqtSignal(str)

    def __init__(self, disk_number: int, parent=None):
        super().__init__(parent)
        self.disk_number = disk_number

    def run(self) -> None:
        try:
            restore_cicada_usb_flag(self.disk_number)
            self.finished_ok.emit()
        except Exception as exc:
            self.finished_err.emit(format_user_error_message(str(exc)))


@dataclass
class FlagAutoCheckResult:
    disk_number: int
    disk_identity_key: str
    target_unique_id: str | None
    request_id: int
    was_restored: bool


class FlagAutoCheckWorker(QThread):
    finished_ok = pyqtSignal(object)
    finished_err = pyqtSignal(int, str)

    def __init__(
        self,
        disk_number: int,
        target_unique_id: str | None,
        target_identity_key: str,
        request_id: int,
        parent=None,
    ):
        super().__init__(parent)
        self.disk_number = disk_number
        self.target_unique_id = target_unique_id
        self.target_identity_key = target_identity_key
        self.request_id = request_id

    def run(self) -> None:
        probe_kw = {
            "timeout": DISK_PROBE_TIMEOUT_SEC,
            "device_key": self.target_identity_key,
        }
        try:
            if is_cicada_flag_verified_cached(self.target_identity_key):
                self.finished_ok.emit(
                    FlagAutoCheckResult(
                        self.disk_number,
                        self.target_identity_key,
                        self.target_unique_id,
                        self.request_id,
                        was_restored=False,
                    )
                )
                return
            if verify_cicada_usb_flag(self.disk_number, **probe_kw):
                self.finished_ok.emit(
                    FlagAutoCheckResult(
                        self.disk_number,
                        self.target_identity_key,
                        self.target_unique_id,
                        self.request_id,
                        was_restored=False,
                    )
                )
                return
            restore_cicada_usb_flag(self.disk_number, **probe_kw)
            self.finished_ok.emit(
                FlagAutoCheckResult(
                    self.disk_number,
                    self.target_identity_key,
                    self.target_unique_id,
                    self.request_id,
                    was_restored=True,
                )
            )
        except Exception as exc:
            self.finished_err.emit(
                self.disk_number,
                format_user_error_message(str(exc)),
            )


class UsbScanWorker(QThread):
    """Фоновое сканирование USB: только list_usb_disks_fast_cached(), без доступа к разделам."""

    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def run(self) -> None:
        debug_log("[SCAN] fast scan started")
        started = time.perf_counter()
        try:
            disks, cached = list_usb_disks_fast_cached()
            elapsed = time.perf_counter() - started
            cache_note = " (cached)" if cached else ""
            debug_log(f"[SCAN] fast scan finished in {elapsed:.2f} sec{cache_note}")
            self.finished.emit(UsbScanResult(disks))
        except Exception as exc:
            elapsed = time.perf_counter() - started
            debug_log(f"[SCAN] fast scan failed in {elapsed:.2f} sec: {exc}")
            self.error.emit(format_user_error_message(str(exc)))


class RefreshDiskButton(QPushButton):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(REFRESH_BTN_W, REFRESH_BTN_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setText("Обновить")
        self._icon_px = Icons.pixmap("refresh", REFRESH_ICON_SZ)
        self._normal_icon = QIcon(self._icon_px)
        self.setIcon(self._normal_icon)
        self.setIconSize(QSize(REFRESH_ICON_SZ, REFRESH_ICON_SZ))
        self._spin_angle = 0
        self._loading = False
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._on_spin_tick)
        self._apply_style()

    def _apply_style(self) -> None:
        cyan = COLORS["accent_cyan"]
        hover_bg = "0.22" if not self._loading else "0.16"
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(0,229,255,0.12);
                color: {cyan};
                border: 1px solid {cyan};
                border-radius: {_s(12)}px;
                padding: 0 {_s(14)}px;
                font-size: {FONTS['body']}px;
                font-weight: 700;
                letter-spacing: 0.3px;
            }}
            QPushButton:hover {{
                background-color: rgba(0,229,255,{hover_bg});
            }}
            QPushButton:disabled {{
                background-color: rgba(0,229,255,0.08);
                color: {_rgba(cyan, 0.45)};
                border: 1px solid {_rgba(cyan, 0.35)};
            }}
        """)

    def start_loading(self) -> None:
        self._loading = True
        self._spin_angle = 0
        self.setText("Сканирование...")
        self._spin_timer.start(45)
        self._apply_style()

    def stop_loading(self) -> None:
        self._loading = False
        self._spin_timer.stop()
        self._spin_angle = 0
        self.setText("Обновить")
        self.setIcon(self._normal_icon)
        self._apply_style()

    def _on_spin_tick(self) -> None:
        self._spin_angle = (self._spin_angle + 30) % 360
        transform = QTransform()
        transform.translate(REFRESH_ICON_SZ / 2, REFRESH_ICON_SZ / 2)
        transform.rotate(self._spin_angle)
        transform.translate(-REFRESH_ICON_SZ / 2, -REFRESH_ICON_SZ / 2)
        rotated = self._icon_px.transformed(
            transform, Qt.TransformationMode.SmoothTransformation
        )
        self.setIcon(QIcon(rotated))


# ═══════════════════════════════════════════════════════════════════════
#  Dialogs
# ═══════════════════════════════════════════════════════════════════════

class CicadaDialog(QDialog):

    _KIND_META = {
        "info":     ("✓", COLORS["accent_green"],  COLORS["accent_green"]),
        "warning":  ("⚠", COLORS["accent_yellow"], COLORS["accent_yellow"]),
        "error":    ("✕", COLORS["accent_red"],    COLORS["accent_red"]),
        "retry":    ("✕", COLORS["accent_red"],    COLORS["accent_red"]),
        "admin":    ("🛡", COLORS["accent_cyan"],   COLORS["accent_cyan"]),
        "question": ("?", COLORS["accent_purple"],  COLORS["accent_purple"]),
    }

    def __init__(
        self,
        parent,
        title,
        message,
        kind="info",
        danger=False,
        *,
        yes_text: str | None = None,
        no_text: str | None = None,
    ):
        super().__init__(parent)
        self._yes_text = yes_text
        self._no_text = no_text
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self.setup_ui(title, message, kind, danger)

    def setup_ui(self, title: str, message: str, kind: str, danger: bool) -> None:
        visual_kind = "warning" if danger and kind == "question" else kind
        _icon_char, accent, border = self._KIND_META.get(visual_kind, self._KIND_META["info"])
        if danger:
            accent = COLORS["accent_red"]
            border = COLORS["accent_red"]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        card = QFrame()
        card.setObjectName("dialogCard")
        card.setStyleSheet(f"""
            QFrame#dialogCard {{
                background-color: rgba(18,18,34,0.96);
                border: 1px solid {border};
                border-radius: 14px;
            }}
        """)
        _shadow(card, blur=28)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(14)
        dialog_icon_key = DIALOG_ICON_KEYS.get(visual_kind, "about")
        icon_px = Icons.pixmap(dialog_icon_key, ICON_SIZE_CARD)
        icon_wrap = QLabel()
        icon_wrap.setFixedSize(44, 44)
        icon_wrap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_wrap.setStyleSheet(f"""
            QLabel {{
                background-color: rgba({int(accent[1:3],16)},{int(accent[3:5],16)},{int(accent[5:7],16)},0.12);
                border: 1.5px solid rgba({int(accent[1:3],16)},{int(accent[3:5],16)},{int(accent[5:7],16)},0.45);
                border-radius: 22px;
            }}
        """)
        icon_wrap.setPixmap(icon_px)
        header.addWidget(icon_wrap)

        title_col = QVBoxLayout()
        title_col.setSpacing(4)
        title_label = QLabel(title.lstrip("⚠️ ").strip())
        title_label.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        title_label.setStyleSheet(f"color: {COLORS['text_primary']};")
        title_col.addWidget(title_label)
        if danger:
            sub = QLabel("Действие необратимо")
            sub.setStyleSheet(f"color: {COLORS['accent_red']}; font-size: 11px; font-weight: 600;")
            title_col.addWidget(sub)
        header.addLayout(title_col, stretch=1)
        layout.addLayout(header)

        parts = [part.strip() for part in message.split("\n\n") if part.strip()]
        if danger and len(parts) >= 2:
            intro = QLabel(parts[0])
            intro.setWordWrap(True)
            intro.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 13px; line-height: 140%;")
            layout.addWidget(intro)
            highlight = QFrame()
            highlight.setObjectName("dialogHighlight")
            highlight.setStyleSheet(f"""
                QFrame#dialogHighlight {{
                    background-color: rgba(255,107,138,0.08);
                    border: 1px solid rgba(255,107,138,0.35);
                    border-left: 3px solid {COLORS['accent_red']};
                    border-radius: 8px;
                }}
            """)
            hl = QVBoxLayout(highlight)
            hl.setContentsMargins(14, 12, 14, 12)
            dl = QLabel(parts[1])
            dl.setWordWrap(True)
            dl.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: 600;")
            hl.addWidget(dl)
            layout.addWidget(highlight)
            if len(parts) > 2:
                outro = QLabel("\n\n".join(parts[2:]))
                outro.setWordWrap(True)
                outro.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 13px;")
                layout.addWidget(outro)
        else:
            msg_label = QLabel(message)
            msg_label.setWordWrap(True)
            msg_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 13px; line-height: 140%;")
            layout.addWidget(msg_label)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {COLORS['border']};")
        layout.addWidget(sep)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        if kind == "question":
            cancel_btn = self._make_button(
                self._no_text or "Отмена",
                COLORS["text_muted"],
                dark=True,
            )
            cancel_btn.clicked.connect(self.reject)
            btn_layout.addWidget(cancel_btn)
            yes_label = self._yes_text or ("Да, продолжить" if danger else "Да")
            yes_btn = self._make_button(
                yes_label,
                COLORS["accent_red"] if danger else COLORS["accent_green"],
                primary=True,
            )
            yes_btn.clicked.connect(self.accept)
            btn_layout.addWidget(yes_btn)
        elif kind == "admin":
            cancel_btn = self._make_button("Выход", COLORS["text_muted"], dark=True)
            cancel_btn.clicked.connect(self.reject)
            btn_layout.addWidget(cancel_btn)
            ok_btn = self._make_button("Запустить от администратора", COLORS["accent_cyan"])
            ok_btn.clicked.connect(self.accept)
            btn_layout.addWidget(ok_btn)
        elif kind == "retry":
            cancel_btn = self._make_button(
                self._no_text or "Отмена",
                COLORS["text_muted"],
                dark=True,
            )
            cancel_btn.clicked.connect(self.reject)
            btn_layout.addWidget(cancel_btn)
            retry_btn = self._make_button(
                self._yes_text or "Повторить",
                accent,
                primary=True,
            )
            retry_btn.clicked.connect(self.accept)
            btn_layout.addWidget(retry_btn)
        elif kind in ("warning", "info", "error"):
            ok_btn = self._make_button("OK", accent)
            ok_btn.clicked.connect(self.accept)
            btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)
        outer.addWidget(card)

    @staticmethod
    def _make_button(text: str, color: str, dark: bool = False, primary: bool = False) -> QPushButton:
        btn = QPushButton(text)
        btn.setMinimumHeight(38)
        btn.setMinimumWidth(110)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if dark:
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba(22,22,43,0.7); color: {COLORS['text_muted']};
                    border: 1px solid {COLORS['border']}; border-radius: 8px;
                    padding: 8px 18px; font-weight: 600; font-size: 12px;
                }}
                QPushButton:hover {{ background-color: rgba(32,32,58,0.9); color: {COLORS['text_primary']}; border-color: rgba(255,255,255,0.15); }}
            """)
        elif primary:
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},0.22);
                    color: {color}; border: 1.5px solid {color}; border-radius: 8px;
                    padding: 8px 20px; font-weight: 700; font-size: 12px;
                }}
                QPushButton:hover {{ background-color: rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},0.35); }}
            """)
        else:
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},0.15);
                    color: {color}; border: 1px solid {color}; border-radius: 8px;
                    padding: 8px 18px; font-weight: 600; font-size: 12px;
                }}
                QPushButton:hover {{ background-color: rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},0.28); }}
            """)
        return btn

    @staticmethod
    def ask(
        parent,
        title: str,
        message: str,
        danger: bool = False,
        *,
        yes_text: str | None = None,
        no_text: str | None = None,
    ) -> bool:
        dlg = CicadaDialog(
            parent,
            title,
            message,
            kind="question",
            danger=danger,
            yes_text=yes_text,
            no_text=no_text,
        )
        return dlg.exec() == QDialog.DialogCode.Accepted

    @staticmethod
    def inform(parent, title: str, message: str, kind: str = "info") -> None:
        CicadaDialog(parent, title, message, kind=kind).exec()

    @staticmethod
    def ask_retry(parent, title: str, message: str) -> bool:
        dlg = CicadaDialog(
            parent,
            title,
            message,
            kind="retry",
            yes_text="Повторить",
            no_text="Отмена",
        )
        return dlg.exec() == QDialog.DialogCode.Accepted


class _GradientGlowButton(QPushButton):
    def __init__(
        self,
        text: str,
        glow: QGraphicsDropShadowEffect,
        *,
        blur_hover: int,
        parent: QWidget | None = None,
    ):
        super().__init__(text, parent)
        self._glow = glow
        self._blur_normal = glow.blurRadius()
        self._blur_hover = blur_hover
        self._color_normal = glow.color()
        self._color_hover = QColor(0, 217, 255, 190)

    def enterEvent(self, event) -> None:
        self._glow.setBlurRadius(self._blur_hover)
        self._glow.setColor(self._color_hover)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._glow.setBlurRadius(self._blur_normal)
        self._glow.setColor(self._color_normal)
        super().leaveEvent(event)


class CreateUsbBootConfirmDialog(QDialog):
    """Подтверждение создания Cicada USB Boot — cyber/security стиль."""

    _ACTIONS = (
        "Форматирование накопителя",
        "Создание NTFS раздела",
        "Создание FAT32 раздела",
        "Установка загрузчика",
        "Установка меню CICADA3301",
        "Включение защиты разделов",
    )
    _CYAN = "#00d9ff"
    _BG = "#071221"
    _WARN_ICON = "#ffb347"
    _DANGER = "#ff6b6b"
    _CHECK = "#00ff99"

    def __init__(self, parent, disk: UsbDisk):
        super().__init__(parent)
        self._disk = disk
        self._fade_anim: QPropertyAnimation | None = None
        self._pulse_group: QSequentialAnimationGroup | None = None
        self.setWindowTitle("Создание Cicada USB Boot")
        self.setModal(True)
        self.setFixedSize(_s(540), _s(400))
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self._setup_ui()
        self.setWindowOpacity(0.0)

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_s(14), _s(14), _s(14), _s(14))

        card = QFrame()
        card.setObjectName("createConfirmCard")
        card.setStyleSheet(f"""
            QFrame#createConfirmCard {{
                background-color: {self._BG};
                border: 1px solid {self._CYAN};
                border-radius: 18px;
            }}
        """)
        glow = QGraphicsDropShadowEffect()
        glow.setBlurRadius(_s(42))
        glow.setColor(QColor(0, 217, 255, 70))
        glow.setOffset(0, 0)
        card.setGraphicsEffect(glow)

        root = QVBoxLayout(card)
        root.setContentsMargins(_s(22), _s(20), _s(22), _s(18))
        root.setSpacing(_s(12))

        header = QHBoxLayout()
        header.setSpacing(_s(14))
        warn_icon = QLabel("⚠")
        warn_icon.setFixedSize(_s(52), _s(52))
        warn_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        warn_icon.setStyleSheet(f"""
            QLabel {{
                color: {self._WARN_ICON};
                font-size: {_s(34)}px;
                font-weight: 700;
                background: transparent;
                border: none;
            }}
        """)
        icon_glow = QGraphicsDropShadowEffect()
        icon_glow.setBlurRadius(_s(14))
        icon_glow.setColor(QColor(255, 179, 71, 160))
        icon_glow.setOffset(0, 0)
        warn_icon.setGraphicsEffect(icon_glow)
        header.addWidget(warn_icon, alignment=Qt.AlignmentFlag.AlignTop)

        title_col = QVBoxLayout()
        title_col.setSpacing(_s(4))
        title = QLabel("СОЗДАНИЕ CICADA USB BOOT")
        title.setWordWrap(True)
        title.setStyleSheet(
            f"{LABEL_PLAIN} color: #ffffff; font-size: {_s(24)}px; font-weight: 800;"
            f" letter-spacing: 0.4px;"
        )
        subtitle = QLabel("Действие необратимо")
        subtitle.setStyleSheet(
            f"{LABEL_PLAIN} color: {self._DANGER}; font-size: {_s(12)}px; font-weight: 700;"
        )
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col, stretch=1)
        root.addLayout(header)

        device_card = QFrame()
        device_card.setObjectName("createConfirmDevice")
        device_card.setStyleSheet(f"""
            QFrame#createConfirmDevice {{
                background-color: rgba(0, 220, 255, 0.05);
                border: 1px solid {self._CYAN};
                border-radius: 14px;
            }}
            QFrame#createConfirmDevice QLabel {{
                {LABEL_PLAIN}
            }}
        """)
        device_lay = QVBoxLayout(device_card)
        device_lay.setContentsMargins(_s(14), _s(12), _s(14), _s(12))
        device_lay.setSpacing(_s(4))
        device_name = QLabel(f"🖴  {self._disk.model}")
        device_name.setStyleSheet(
            f"color: #ffffff; font-size: {_s(15)}px; font-weight: 700;"
        )
        device_meta = QLabel(
            f"{self._disk.size_gb:.0f} GB • USB"
        )
        device_meta.setStyleSheet(
            f"color: {_rgba(self._CYAN, 0.85)}; font-size: {_s(12)}px; font-weight: 600;"
        )
        device_warn = QLabel("Будет полностью очищен")
        device_warn.setStyleSheet(
            f"color: {self._DANGER}; font-size: {_s(11)}px; font-weight: 600;"
        )
        device_lay.addWidget(device_name)
        device_lay.addWidget(device_meta)
        device_lay.addWidget(device_warn)
        root.addWidget(device_card)

        actions_card = QFrame()
        actions_card.setObjectName("createConfirmActions")
        actions_card.setStyleSheet(f"""
            QFrame#createConfirmActions {{
                background-color: rgba(255, 255, 255, 0.02);
                border: 1px solid rgba(0, 217, 255, 0.3);
                border-radius: 12px;
            }}
            QFrame#createConfirmActions QLabel {{
                {LABEL_PLAIN}
            }}
        """)
        actions_lay = QVBoxLayout(actions_card)
        actions_lay.setContentsMargins(_s(14), _s(10), _s(14), _s(10))
        actions_lay.setSpacing(_s(3))
        actions_title = QLabel("ЧТО БУДЕТ СДЕЛАНО")
        actions_title.setStyleSheet(
            f"color: {self._CYAN}; font-size: {_s(11)}px; font-weight: 800;"
            f" letter-spacing: 0.8px;"
        )
        actions_lay.addWidget(actions_title)
        for item in self._ACTIONS:
            row = QLabel(
                f'<span style="color:{self._CHECK}; font-weight:700;">✓</span>'
                f'&nbsp;&nbsp;<span style="color:{COLORS["text_primary"]};">{item}</span>'
            )
            row.setTextFormat(Qt.TextFormat.RichText)
            row.setStyleSheet(f"{LABEL_PLAIN} font-size: {_s(11)}px;")
            actions_lay.addWidget(row)
        root.addWidget(actions_card, stretch=1)

        data_warn = QLabel("ВСЕ ДАННЫЕ НА ФЛЕШКЕ БУДУТ УДАЛЕНЫ")
        data_warn.setAlignment(Qt.AlignmentFlag.AlignCenter)
        data_warn.setStyleSheet(
            f"{LABEL_PLAIN} color: {self._DANGER}; font-size: {_s(16)}px; font-weight: 800;"
            f" letter-spacing: 0.3px;"
        )
        root.addWidget(data_warn)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(_s(12))
        btn_row.addStretch()
        cancel_btn = QPushButton("ОТМЕНА")
        cancel_btn.setFixedSize(_s(150), _s(44))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {self._CYAN};
                border: 1px solid {self._CYAN};
                border-radius: 10px;
                font-size: {_s(12)}px;
                font-weight: 700;
                letter-spacing: 0.6px;
            }}
            QPushButton:hover {{
                background-color: rgba(0, 217, 255, 0.08);
                color: #ffffff;
            }}
        """)
        cancel_btn.clicked.connect(self.reject)
        create_glow = QGraphicsDropShadowEffect()
        create_glow.setBlurRadius(_s(18))
        create_glow.setColor(QColor(0, 217, 255, 100))
        create_glow.setOffset(0, 0)
        create_btn = _GradientGlowButton(
            "СОЗДАТЬ USB BOOT",
            create_glow,
            blur_hover=_s(36),
        )
        create_btn.setObjectName("createUsbBootBtn")
        create_btn.setFixedSize(_s(220), _s(44))
        create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        create_btn.setGraphicsEffect(create_glow)
        create_btn.setStyleSheet(f"""
            QPushButton#createUsbBootBtn {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00d9ff, stop:1 #7b4dff
                );
                color: #ffffff;
                border: none;
                border-radius: 10px;
                font-size: {_s(12)}px;
                font-weight: 800;
                letter-spacing: 0.4px;
            }}
            QPushButton#createUsbBootBtn:hover {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #33e8ff, stop:1 #9a72ff
                );
                border: 1px solid rgba(0, 217, 255, 0.85);
            }}
        """)
        create_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(create_btn)
        root.addLayout(btn_row)

        outer.addWidget(card)
        self._setup_pulse_animation(icon_glow)

    def _setup_pulse_animation(self, icon_glow: QGraphicsDropShadowEffect) -> None:
        fwd = QPropertyAnimation(icon_glow, b"blurRadius")
        fwd.setStartValue(_s(10))
        fwd.setEndValue(_s(32))
        fwd.setDuration(1200)
        fwd.setEasingCurve(QEasingCurve.Type.InOutSine)
        bwd = QPropertyAnimation(icon_glow, b"blurRadius")
        bwd.setStartValue(_s(32))
        bwd.setEndValue(_s(10))
        bwd.setDuration(1200)
        bwd.setEasingCurve(QEasingCurve.Type.InOutSine)
        group = QSequentialAnimationGroup(self)
        group.addAnimation(fwd)
        group.addAnimation(bwd)
        group.setLoopCount(-1)
        self._pulse_group = group

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_running = False
        if self._fade_anim is not None:
            try:
                fade_running = (
                    self._fade_anim.state() == QAbstractAnimation.State.Running
                )
            except RuntimeError:
                self._fade_anim = None
        if self._fade_anim is None:
            anim = QPropertyAnimation(self, b"windowOpacity", self)
            anim.setDuration(280)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._fade_anim = anim
            anim.start()
        elif not fade_running:
            self._fade_anim.setStartValue(self.windowOpacity())
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()
        if self._pulse_group is not None and self._pulse_group.state() != QAbstractAnimation.State.Running:
            self._pulse_group.start()

    @staticmethod
    def confirm(parent, disk: UsbDisk) -> bool:
        dlg = CreateUsbBootConfirmDialog(parent, disk)
        return dlg.exec() == QDialog.DialogCode.Accepted


def _coerce_exception(exc: BaseException | str) -> BaseException:
    if isinstance(exc, BaseException):
        return exc
    return RuntimeError(str(exc))


def show_network_error(parent, classified: ClassifiedError) -> bool:
    return CicadaDialog.ask_retry(parent, classified.title, classified.message)


def show_usb_locked_error(parent, classified: ClassifiedError) -> bool:
    return CicadaDialog.ask_retry(parent, classified.title, classified.message)


def show_admin_error(parent) -> None:
    classified = classify_admin_error()
    CicadaDialog.inform(parent, classified.title, classified.message, kind="admin")


def show_unknown_error(parent, classified: ClassifiedError) -> None:
    CicadaDialog.inform(parent, classified.title, classified.message, kind="error")


def handle_exception(
    parent,
    exc: BaseException | str,
    *,
    allow_retry: bool = False,
) -> bool:
    """Классифицирует ошибку, пишет диагностику в лог и показывает диалог.

    Возвращает True, если пользователь нажал «Повторить».
    """
    error = _coerce_exception(exc)
    classified = classify_exception(error)
    log_exception(error, classified.code)

    if classified.category in (ErrorCategory.NETWORK, ErrorCategory.TIMEOUT):
        if allow_retry and classified.retryable:
            return show_network_error(parent, classified)
        CicadaDialog.inform(parent, classified.title, classified.message, kind="error")
        return False

    if classified.category in (ErrorCategory.USB_LOCKED, ErrorCategory.USB_ACCESS):
        if allow_retry and classified.retryable:
            return show_usb_locked_error(parent, classified)
        CicadaDialog.inform(parent, classified.title, classified.message, kind="error")
        return False

    show_unknown_error(parent, classified)
    return False


IMPORT_CATEGORY_LABELS = {
    "WINDOWS": "Windows",
    "LINUX": "Linux",
    "WINPE": "WinPE",
}


class ImageImportProgressDialog(QDialog):
    cancel_requested = pyqtSignal()

    def __init__(self, parent, filename: str, category_label: str):
        super().__init__(parent)
        self.setWindowTitle("Добавление образа")
        self.setModal(True)
        self.setMinimumWidth(_s(500))
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self._setup_ui(filename, category_label)

    def _setup_ui(self, filename: str, category_label: str) -> None:
        border = COLORS["accent_cyan"]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        card = QFrame()
        card.setObjectName("importProgressCard")
        card.setStyleSheet(f"""
            QFrame#importProgressCard {{
                background-color: rgba(18,18,34,0.97);
                border: 1.5px solid {border};
                border-radius: 14px;
            }}
        """)
        _shadow(card, blur=28)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        title = QLabel("ДОБАВЛЕНИЕ ОБРАЗА")
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {COLORS['text_primary']}; letter-spacing: 0.5px;")
        layout.addWidget(title)

        self._status_label = QLabel("Подготовка...")
        self._status_label.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: 13px; font-weight: 600;"
        )
        layout.addWidget(self._status_label)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {COLORS['border']};")
        layout.addWidget(sep)

        self._filename_value = QLabel(filename)
        self._filename_value.setWordWrap(True)
        self._category_value = QLabel(category_label)
        self._stage_value = QLabel("—")
        self._stage_value.setStyleSheet(
            f"color: {COLORS['accent_purple']}; font-size: 13px; font-weight: 600;"
        )
        for label_text, value_widget in (
            ("Имя файла", self._filename_value),
            ("Категория", self._category_value),
            ("Текущий этап", self._stage_value),
        ):
            row = QHBoxLayout()
            row.setSpacing(12)
            name = QLabel(label_text)
            name.setMinimumWidth(_s(120))
            name.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px; font-weight: 600;")
            value_widget.setStyleSheet(
                f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: 600;"
            )
            row.addWidget(name)
            row.addWidget(value_widget, stretch=1)
            layout.addLayout(row)

        prog_row = QHBoxLayout()
        prog_row.setSpacing(_s(10))
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        progress_h = _s(28)
        self._progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: rgba(2,6,23,0.85);
                border: 1px solid {PANEL_BORDER};
                border-radius: {_s(12)}px;
                min-height: {progress_h}px; max-height: {progress_h}px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {COLORS['accent_cyan']}, stop:0.5 {COLORS['accent_blue']},
                    stop:1 {COLORS['accent_purple']});
                border-radius: {_s(10)}px;
            }}
        """)
        prog_row.addWidget(self._progress, stretch=1)
        self._progress_pct = QLabel("0%")
        self._progress_pct.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['accent_cyan']}; font-weight: 800;"
            f" font-size: {FONTS['card_title']}px; min-width: {_s(48)}px;"
        )
        self._progress_pct.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        prog_row.addWidget(self._progress_pct)
        layout.addLayout(prog_row)

        self._bytes_label = QLabel("")
        self._bytes_label.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: 12px; font-weight: 600;"
        )
        self._bytes_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._bytes_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = CicadaDialog._make_button("Отмена", COLORS["text_muted"], dark=True)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        btn_row.addWidget(self._cancel_btn)
        layout.addLayout(btn_row)

        outer.addWidget(card)

    def _on_cancel_clicked(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._status_label.setText("Отмена...")
        self.cancel_requested.emit()

    def set_stage(self, stage: str) -> None:
        self._stage_value.setText(stage)
        if stage == "Готово":
            self._status_label.setText("Завершение...")
        elif stage not in ("—", ""):
            self._status_label.setText(stage)

    def set_progress(self, value: int) -> None:
        clamped = max(0, min(100, int(value)))
        self._progress.setValue(clamped)
        self._progress_pct.setText(f"{clamped}%")
        debug_log(f"[UI] progress dialog set value: {clamped}")

    def set_bytes_progress(self, copied: int, total: int) -> None:
        if total > 0:
            self._bytes_label.setText(
                f"{format_file_size(copied)} / {format_file_size(total)}"
            )
        else:
            self._bytes_label.setText("")

    def set_cancelled(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._status_label.setText("ОТМЕНЕНО")

    def set_error(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._status_label.setText("ОШИБКА")


class _WindowsVariantCard(QFrame):
    """Карточка версии Windows в диалоге выбора."""

    clicked = pyqtSignal(str)
    _CARD_H = 72
    _CARD_STYLE = f"""
        QFrame#winVariantCard {{
            border-radius: 14px;
            border: 1px solid #00d9ff;
            background: #081425;
        }}
        QFrame#winVariantCard[winCardState="hover"] {{
            background: rgba(0,217,255,0.08);
            border-color: #4de8ff;
        }}
        QFrame#winVariantCard[winCardState="selected"] {{
            border-color: #00d9ff;
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:1,
                stop:0 #00d9ff, stop:1 #7b4dff
            );
        }}
        QLabel#winVariantIcon {{
            {LABEL_PLAIN}
            background: rgba(0,217,255,0.10);
            border-radius: 12px;
        }}
        QLabel#winVariantTitle {{
            {LABEL_PLAIN}
            color: #ffffff;
            font-size: 19px;
            font-weight: 800;
            letter-spacing: 0.2px;
        }}
        QLabel#winVariantDesc {{
            {LABEL_PLAIN}
            color: rgba(148,163,184,0.95);
            font-size: 12px;
            font-weight: 600;
        }}
        QFrame#winVariantCard[winCardState="selected"] QLabel#winVariantDesc {{
            color: rgba(255,255,255,0.90);
        }}
    """

    def __init__(
        self,
        title: str,
        description: str,
        value: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._value = value
        self._is_selected = False
        self._is_hover = False
        self.setObjectName("winVariantCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self._CARD_H)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(self._CARD_STYLE)
        self.setProperty("winCardState", "normal")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(12)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(48, 48)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setPixmap(Icons.pixmap("windows", 45))
        icon_lbl.setObjectName("winVariantIcon")
        lay.addWidget(icon_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)
        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("winVariantTitle")
        self._desc_lbl = QLabel(description)
        self._desc_lbl.setObjectName("winVariantDesc")
        text_col.addWidget(self._title_lbl)
        text_col.addWidget(self._desc_lbl)
        lay.addLayout(text_col, stretch=1)

    def set_selected(self, selected: bool) -> None:
        self._is_selected = selected
        self._update_card_state()

    def _update_card_state(self) -> None:
        if self._is_selected:
            state = "selected"
        elif self._is_hover:
            state = "hover"
        else:
            state = "normal"
        if self.property("winCardState") == state:
            return
        self.setProperty("winCardState", state)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def enterEvent(self, event) -> None:
        if not self._is_selected:
            self._is_hover = True
            self._update_card_state()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._is_hover = False
        if not self._is_selected:
            self._update_card_state()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._value)
        super().mousePressEvent(event)


class WindowsVariantPickerDialog(QDialog):
    """Диалог выбора версии Windows — вертикальный список карточек."""

    _DIALOG_W = 520
    _DIALOG_H = 620

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected: str | None = None
        self._cards: list[_WindowsVariantCard] = []
        self._scroll_area: QScrollArea | None = None
        self._cards_container: QWidget | None = None
        self._list_h = 0
        self.setWindowTitle("Выберите версию Windows")
        self.setModal(True)
        self.setFixedSize(self._DIALOG_W, self._DIALOG_H)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self._build_ui()

    def _build_ui(self) -> None:
        shell = QFrame()
        shell.setObjectName("windowsPickerCard")
        shell.setStyleSheet(f"""
            QFrame#windowsPickerCard {{
                background-color: #071221;
                border: 1px solid rgba(0,213,255,0.35);
                border-radius: 18px;
            }}
            QFrame#windowsPickerCard QLabel {{ {LABEL_PLAIN} }}
            QScrollArea#windowsPickerScroll {{
                background: transparent;
                border: none;
            }}
            QScrollArea#windowsPickerScroll > QWidget > QWidget {{
                background: transparent;
            }}
        """)
        _shadow(shell, blur=28, y=4)

        main_layout = QVBoxLayout(shell)
        main_layout.setContentsMargins(20, 20, 20, 16)
        main_layout.setSpacing(10)

        header_widget = QWidget()
        header_widget.setFixedHeight(72)
        header_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        head = QVBoxLayout(header_widget)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(2)
        title = QLabel("WINDOWS IMAGE")
        title.setStyleSheet(
            "color: #ffffff; font-size: 20px; font-weight: 900; letter-spacing: 0.5px;"
        )
        subtitle = QLabel("Выберите категорию для добавления образа")
        subtitle.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: 12px; font-weight: 600;"
        )
        variants_line = QLabel(
            f"{len(WINDOWS_VARIANT_CARDS)} доступных вариантов"
        )
        variants_line.setStyleSheet(
            f"color: {_rgba(COLORS['accent_cyan'], 0.80)};"
            " font-size: 11px; font-weight: 700; letter-spacing: 0.2px;"
        )
        head.addWidget(title)
        head.addWidget(subtitle)
        head.addWidget(variants_line)
        main_layout.addWidget(header_widget)

        _card_spacing = 10
        self._list_h = (
            len(WINDOWS_VARIANT_CARDS) * _WindowsVariantCard._CARD_H
            + (len(WINDOWS_VARIANT_CARDS) - 1) * _card_spacing
        )

        scroll_area = QScrollArea()
        scroll_area.setObjectName("windowsPickerScroll")
        scroll_area.setWidgetResizable(False)
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        cards_container = QWidget()
        cards_layout = QVBoxLayout(cards_container)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(_card_spacing)
        for label, value, desc in WINDOWS_VARIANT_CARDS:
            variant_card = _WindowsVariantCard(label, desc, value)
            variant_card.clicked.connect(self._on_card_clicked)
            self._cards.append(variant_card)
            cards_layout.addWidget(variant_card)

        scroll_area.setWidget(cards_container)
        scroll_area.viewport().installEventFilter(self)
        self._scroll_area = scroll_area
        self._cards_container = cards_container
        main_layout.addWidget(scroll_area, stretch=1)

        footer_widget = QFrame()
        footer_widget.setObjectName("windowsPickerFooter")
        footer_widget.setFixedHeight(54)
        footer_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        footer_widget.setStyleSheet("""
            QFrame#windowsPickerFooter {
                background: rgba(0,0,0,0.12);
                border-top: 1px solid rgba(0,217,255,0.35);
                border-bottom-left-radius: 18px;
                border-bottom-right-radius: 18px;
            }
        """)
        footer = QHBoxLayout(footer_widget)
        footer.setContentsMargins(16, 0, 16, 0)
        footer.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setFixedSize(88, 32)
        cancel.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {COLORS['accent_red']};
                border: 1px solid #ff4d4d;
                border-radius: 8px;
                font-size: 11px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background: rgba(255,77,77,0.14);
                color: #ffffff;
                border-color: #ff6b6b;
            }}
        """)
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        main_layout.addWidget(footer_widget)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(shell)

    def _sync_cards_width(self) -> None:
        if self._scroll_area is None or self._cards_container is None:
            return
        viewport_w = self._scroll_area.viewport().width()
        if viewport_w > 0:
            self._cards_container.setFixedSize(viewport_w, self._list_h)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_cards_width)

    def eventFilter(self, obj, event) -> bool:
        if (
            self._scroll_area is not None
            and obj is self._scroll_area.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            self._sync_cards_width()
        return super().eventFilter(obj, event)

    def _on_card_clicked(self, value: str) -> None:
        self._selected = value
        for card in self._cards:
            card.set_selected(card._value == value)
        QTimer.singleShot(80, self.accept)

    @classmethod
    def pick(cls, parent=None) -> str | None:
        dlg = cls(parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg._selected
        return None


class _LinuxVariantCard(QFrame):
    """Карточка дистрибутива Linux в диалоге выбора."""

    clicked = pyqtSignal(str)
    _CARD_H = 78
    _CARD_STYLE = f"""
        QFrame#linuxVariantCard {{
            border-radius: 14px;
            border: 1px solid #00d9ff;
            background: #081425;
        }}
        QFrame#linuxVariantCard[linuxCardState="hover"] {{
            background: rgba(0,217,255,0.08);
            border-color: #4de8ff;
        }}
        QFrame#linuxVariantCard[linuxCardState="selected"] {{
            border-color: #00d9ff;
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:1,
                stop:0 #00d9ff, stop:1 #7b4dff
            );
        }}
        QLabel#linuxVariantIcon {{
            {LABEL_PLAIN}
            background: rgba(0,217,255,0.10);
            border-radius: 12px;
        }}
        QLabel#linuxVariantTitle {{
            {LABEL_PLAIN}
            color: #ffffff;
            font-size: 19px;
            font-weight: 800;
            letter-spacing: 0.2px;
        }}
        QLabel#linuxVariantDesc {{
            {LABEL_PLAIN}
            color: rgba(148,163,184,0.95);
            font-size: 12px;
            font-weight: 600;
        }}
        QLabel#linuxVariantIsoBadge {{
            {LABEL_PLAIN}
            color: rgba(0,217,255,0.95);
            background: rgba(0,217,255,0.10);
            border: 1px solid rgba(0,217,255,0.35);
            border-radius: 6px;
            font-size: 10px;
            font-weight: 700;
            padding: 2px 8px;
        }}
        QFrame#linuxVariantCard[linuxCardState="selected"] QLabel#linuxVariantDesc {{
            color: rgba(255,255,255,0.90);
        }}
        QFrame#linuxVariantCard[linuxCardState="selected"] QLabel#linuxVariantIsoBadge {{
            color: #ffffff;
            background: rgba(255,255,255,0.15);
            border-color: rgba(255,255,255,0.35);
        }}
    """

    def __init__(
        self,
        title: str,
        description: str,
        value: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._value = value
        self._is_selected = False
        self._is_hover = False
        self.setObjectName("linuxVariantCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self._CARD_H)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(self._CARD_STYLE)
        self.setProperty("linuxCardState", "normal")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(12)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(48, 48)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setPixmap(Icons.pixmap("linux", 45))
        icon_lbl.setObjectName("linuxVariantIcon")
        lay.addWidget(icon_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)
        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("linuxVariantTitle")
        self._desc_lbl = QLabel(description)
        self._desc_lbl.setObjectName("linuxVariantDesc")
        text_col.addWidget(self._title_lbl)
        text_col.addWidget(self._desc_lbl)
        lay.addLayout(text_col, stretch=1)

        iso_badge = QLabel("ISO")
        iso_badge.setObjectName("linuxVariantIsoBadge")
        iso_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        iso_badge.setFixedHeight(22)
        iso_badge.setMinimumWidth(42)
        lay.addWidget(iso_badge, alignment=Qt.AlignmentFlag.AlignVCenter)

    def set_selected(self, selected: bool) -> None:
        self._is_selected = selected
        self._update_card_state()

    def _update_card_state(self) -> None:
        if self._is_selected:
            state = "selected"
        elif self._is_hover:
            state = "hover"
        else:
            state = "normal"
        if self.property("linuxCardState") == state:
            return
        self.setProperty("linuxCardState", state)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def enterEvent(self, event) -> None:
        if not self._is_selected:
            self._is_hover = True
            self._update_card_state()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._is_hover = False
        if not self._is_selected:
            self._update_card_state()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._value)
        super().mousePressEvent(event)


class LinuxVariantPickerDialog(QDialog):
    """Диалог выбора дистрибутива Linux — премиум-карточки в стиле Windows picker."""

    _DIALOG_W = 520
    _DIALOG_H = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected: str | None = None
        self._cards: list[_LinuxVariantCard] = []
        self.setWindowTitle("Выберите дистрибутив Linux")
        self.setModal(True)
        self.setFixedSize(self._DIALOG_W, self._DIALOG_H)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self._build_ui()

    def _build_ui(self) -> None:
        shell = QFrame()
        shell.setObjectName("linuxPickerCard")
        shell.setStyleSheet(f"""
            QFrame#linuxPickerCard {{
                background-color: #071221;
                border: 1px solid rgba(0,213,255,0.35);
                border-radius: 18px;
            }}
            QFrame#linuxPickerCard QLabel {{ {LABEL_PLAIN} }}
        """)
        _shadow(shell, blur=28, y=4)

        main_layout = QVBoxLayout(shell)
        main_layout.setContentsMargins(20, 20, 20, 16)
        main_layout.setSpacing(10)

        header_widget = QWidget()
        header_widget.setFixedHeight(72)
        header_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        head = QVBoxLayout(header_widget)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(2)
        title = QLabel("LINUX IMAGE")
        title.setStyleSheet(
            "color: #ffffff; font-size: 20px; font-weight: 900; letter-spacing: 0.5px;"
        )
        subtitle = QLabel("Выберите дистрибутив для добавления образа")
        subtitle.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: 12px; font-weight: 600;"
        )
        variants_line = QLabel(
            f"{len(LINUX_VARIANT_CARDS)} доступных вариантов"
        )
        variants_line.setStyleSheet(
            f"color: {_rgba(COLORS['accent_cyan'], 0.80)};"
            " font-size: 11px; font-weight: 700; letter-spacing: 0.2px;"
        )
        head.addWidget(title)
        head.addWidget(subtitle)
        head.addWidget(variants_line)
        main_layout.addWidget(header_widget)

        _card_spacing = 10
        _list_h = (
            len(LINUX_VARIANT_CARDS) * _LinuxVariantCard._CARD_H
            + (len(LINUX_VARIANT_CARDS) - 1) * _card_spacing
        )

        cards_container = QWidget()
        cards_container.setFixedHeight(_list_h)
        cards_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        cards_layout = QVBoxLayout(cards_container)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(_card_spacing)
        for label, value, desc in LINUX_VARIANT_CARDS:
            variant_card = _LinuxVariantCard(label, desc, value)
            variant_card.clicked.connect(self._on_card_clicked)
            self._cards.append(variant_card)
            cards_layout.addWidget(variant_card)
        main_layout.addWidget(cards_container)
        main_layout.addStretch(1)

        footer_widget = QFrame()
        footer_widget.setObjectName("linuxPickerFooter")
        footer_widget.setFixedHeight(54)
        footer_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        footer_widget.setStyleSheet("""
            QFrame#linuxPickerFooter {
                background: rgba(0,0,0,0.12);
                border-top: 1px solid rgba(0,217,255,0.35);
                border-bottom-left-radius: 18px;
                border-bottom-right-radius: 18px;
            }
        """)
        footer = QHBoxLayout(footer_widget)
        footer.setContentsMargins(16, 0, 16, 0)
        footer.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setFixedSize(88, 32)
        cancel.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {COLORS['accent_red']};
                border: 1px solid #ff4d4d;
                border-radius: 8px;
                font-size: 11px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background: rgba(255,77,77,0.14);
                color: #ffffff;
                border-color: #ff6b6b;
            }}
        """)
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        main_layout.addWidget(footer_widget)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(shell)

    def _on_card_clicked(self, value: str) -> None:
        self._selected = value
        for card in self._cards:
            card.set_selected(card._value == value)
        QTimer.singleShot(80, self.accept)

    @classmethod
    def pick(cls, parent=None) -> str | None:
        dlg = cls(parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg._selected
        return None


class VariantPickerDialog(QDialog):

    def __init__(self, parent, title, options):
        super().__init__(parent)
        self._selected = None
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(_s(420))
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        card = QFrame()
        card.setObjectName("variantCard")
        card.setStyleSheet(f"""
            QFrame#variantCard {{
                background-color: {COLORS['bg_card']};
                border: 1px solid {COLORS['border']};
                border-radius: {_s(18)}px;
            }}
        """)
        _shadow(card, blur=24, y=4)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(_s(22), _s(20), _s(22), _s(18))
        layout.setSpacing(_s(12))

        head = QLabel(title)
        head.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: {FONTS['card_title']}px; font-weight: 800; letter-spacing: 0.5px;")
        layout.addWidget(head)
        hint = QLabel("Выберите вариант для добавления образа")
        hint.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['small']}px;")
        layout.addWidget(hint)

        grid = QVBoxLayout()
        grid.setSpacing(_s(8))
        for label, value in options:
            btn = QPushButton(f"  {label}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(_s(44))
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {_rgba(COLORS['accent_cyan'], 0.08)};
                    border: 1px solid {COLORS['border']};
                    border-radius: {_s(12)}px;
                    color: {COLORS['text_primary']};
                    font-size: {FONTS['body']}px; font-weight: 700;
                    text-align: left; padding: {_s(10)}px {_s(16)}px;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                        stop:0 {_rgba(COLORS['accent_cyan'],0.22)},
                        stop:1 {_rgba(COLORS['accent_purple'],0.12)});
                    border-color: {_rgba(COLORS['accent_cyan'],0.5)};
                    color: #ffffff;
                }}
            """)
            btn.clicked.connect(lambda _=False, v=value: self._choose(v))
            grid.addWidget(btn)
        layout.addLayout(grid)

        cancel = QPushButton("Отмена")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setMinimumHeight(_s(36))
        cancel.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: 1px solid {COLORS['border']};
                border-radius: {_s(10)}px; color: {COLORS['text_muted']};
                font-size: {FONTS['small']}px; font-weight: 600;
                padding: {_s(8)}px {_s(16)}px;
            }}
            QPushButton:hover {{ color: {COLORS['text_primary']}; border-color: {_rgba(COLORS['accent_cyan'],0.4)}; }}
        """)
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel, alignment=Qt.AlignmentFlag.AlignRight)
        outer.addWidget(card)

    def _choose(self, value):
        self._selected = value
        self.accept()

    @classmethod
    def pick(cls, parent, title, options):
        dlg = cls(parent, title, options)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg._selected
        return None


class DeleteImagesDialog(QDialog):

    def __init__(self, parent, entries: list[ImageEntry], on_delete_requested=None):
        super().__init__(parent)
        self._entries = list(entries)
        self._on_delete_requested = on_delete_requested
        self.setWindowTitle("Удаление образов")
        self.setModal(True)
        self.setMinimumSize(_s(560), _s(420))
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)

        card = QFrame()
        card.setObjectName("deleteImagesCard")
        card.setStyleSheet(f"""
            QFrame#deleteImagesCard {{
                background-color: {COLORS['bg_card']};
                border: 1px solid {COLORS['border']};
                border-radius: {_s(18)}px;
            }}
            QFrame#deleteImagesCard QLabel {{
                {LABEL_PLAIN}
            }}
            QListWidget {{
                background-color: rgba(2,6,23,0.55);
                border: 1px solid {PANEL_BORDER};
                border-radius: {_s(12)}px;
                color: {COLORS['text_primary']};
                font-size: {FONTS['body']}px;
                padding: {_s(4)}px;
                outline: none;
            }}
            QListWidget::item {{
                border: none;
                padding: 0;
                margin: 0;
            }}
            QListWidget::item:selected {{
                background: {_rgba(COLORS['accent_cyan'], 0.14)};
            }}
            QListWidget::item:hover {{
                background: {_rgba(COLORS['accent_cyan'], 0.08)};
            }}
        """)
        _shadow(card, blur=24, y=4)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(_s(22), _s(20), _s(22), _s(18))
        layout.setSpacing(_s(12))

        head_row = QHBoxLayout()
        head_row.setSpacing(_s(10))
        head_icon = QLabel()
        head_icon.setFixedSize(ICON_SIZE_NAV, ICON_SIZE_NAV)
        head_icon.setPixmap(Icons.pixmap("trash", ICON_SIZE_NAV))
        head_row.addWidget(head_icon)
        head = QLabel("Удаление образов")
        head.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: {FONTS['card_title']}px; font-weight: 800;"
        )
        head_row.addWidget(head, stretch=1)
        layout.addLayout(head_row)

        self.hint = QLabel("Выберите образ и нажмите «Удалить» или дважды щёлкните по строке")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['small']}px;")
        layout.addWidget(self.hint)

        self.progress_panel = QFrame()
        self.progress_panel.setObjectName("deleteProgressPanel")
        self.progress_panel.setStyleSheet(f"""
            QFrame#deleteProgressPanel {{
                background-color: rgba(2,6,23,0.55);
                border: 1px solid {PANEL_BORDER};
                border-radius: {_s(12)}px;
            }}
        """)
        progress_lay = QVBoxLayout(self.progress_panel)
        progress_lay.setContentsMargins(_s(14), _s(12), _s(14), _s(12))
        progress_lay.setSpacing(_s(8))
        self.delete_status = QLabel("Удаление образа...")
        self.delete_status.setWordWrap(True)
        self.delete_status.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: {FONTS['body']}px; font-weight: 700;"
        )
        progress_lay.addWidget(self.delete_status)
        self.delete_stage = QLabel("Подготовка...")
        self.delete_stage.setStyleSheet(
            f"color: {COLORS['accent_purple']}; font-size: {FONTS['small']}px; font-weight: 600;"
        )
        progress_lay.addWidget(self.delete_stage)
        self.delete_progress = QProgressBar()
        self.delete_progress.setRange(0, 0)
        self.delete_progress.setTextVisible(False)
        self.delete_progress.setFixedHeight(_s(8))
        self.delete_progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: rgba(2,6,23,0.85);
                border: none;
                border-radius: {_s(4)}px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {COLORS['accent_cyan']}, stop:1 {COLORS['accent_purple']});
                border-radius: {_s(4)}px;
            }}
        """)
        progress_lay.addWidget(self.delete_progress)
        self.progress_panel.hide()
        layout.addWidget(self.progress_panel)

        self.list_widget = QListWidget()
        self.list_widget.setSpacing(_s(4))
        self.list_widget.itemDoubleClicked.connect(self._on_item_activated)
        self.list_widget.currentItemChanged.connect(lambda *_: self._update_delete_button())
        layout.addWidget(self.list_widget, stretch=1)

        for entry in self._entries:
            self._add_entry_item(entry)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(_s(10))
        btn_row.addStretch()

        delete_btn = QPushButton("  Удалить")
        delete_btn.setIcon(QIcon(Icons.pixmap("trash", ICON_SIZE_NAV)))
        delete_btn.setIconSize(QSize(ICON_SIZE_NAV, ICON_SIZE_NAV))
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.setMinimumHeight(_s(40))
        delete_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_rgba(COLORS['accent_red'], 0.18)};
                border: 1px solid {COLORS['accent_red']};
                border-radius: {_s(10)}px;
                color: {COLORS['accent_red']};
                font-size: {FONTS['body']}px; font-weight: 800;
                padding: {_s(8)}px {_s(18)}px;
            }}
            QPushButton:hover {{
                background: {_rgba(COLORS['accent_red'], 0.30)};
                color: #ffffff;
            }}
            QPushButton:disabled {{
                color: rgba(148,163,184,0.35);
                border-color: rgba(148,163,184,0.20);
                background: rgba(6,10,18,0.55);
            }}
        """)
        delete_btn.clicked.connect(self._delete_selected)
        self.delete_btn = delete_btn
        btn_row.addWidget(delete_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setMinimumHeight(_s(40))
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {COLORS['border']};
                border-radius: {_s(10)}px;
                color: {COLORS['text_muted']};
                font-size: {FONTS['body']}px; font-weight: 600;
                padding: {_s(8)}px {_s(18)}px;
            }}
            QPushButton:hover {{
                color: {COLORS['text_primary']};
                border-color: {_rgba(COLORS['accent_cyan'], 0.45)};
            }}
        """)
        close_btn.clicked.connect(self.accept)
        self.close_btn = close_btn
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        outer.addWidget(card)
        self._deleting = False
        self._update_delete_button()

    def _make_row_widget(self, entry: ImageEntry) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(_s(10), _s(8), _s(10), _s(8))
        lay.setSpacing(_s(12))

        icon_lbl = QLabel()
        icon_size = _s(40)
        icon_lbl.setFixedSize(icon_size, icon_size)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setPixmap(Icons.pixmap(entry.file_icon_key, icon_size))

        text_col = QVBoxLayout()
        text_col.setSpacing(_s(2))
        text_col.setContentsMargins(0, 0, 0, 0)

        name_lbl = QLabel(entry.boot_title)
        name_lbl.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: {FONTS['body']}px; font-weight: 700;"
        )
        category_lbl = QLabel(entry.category)
        category_lbl.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: {_s(11)}px; font-weight: 700;"
        )
        path_lbl = QLabel(entry.relative_path or entry.name)
        path_lbl.setWordWrap(True)
        path_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: {_s(11)}px;"
        )
        text_col.addWidget(name_lbl)
        text_col.addWidget(category_lbl)
        text_col.addWidget(path_lbl)

        size_lbl = QLabel(format_file_size(entry.size_bytes))
        size_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        size_lbl.setStyleSheet(
            f"color: {COLORS['accent_cyan']}; font-size: {FONTS['body']}px; font-weight: 700; min-width: {_s(72)}px;"
        )

        lay.addWidget(icon_lbl)
        lay.addLayout(text_col, stretch=1)
        lay.addWidget(size_lbl)
        return row

    def _add_entry_item(self, entry: ImageEntry) -> None:
        item = QListWidgetItem(self.list_widget)
        item.setData(Qt.ItemDataRole.UserRole, entry)
        row = self._make_row_widget(entry)
        item.setSizeHint(QSize(0, _s(72)))
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(item, row)

    def _selected_entry(self) -> ImageEntry | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return data if isinstance(data, ImageEntry) else None

    def _update_delete_button(self) -> None:
        self.delete_btn.setEnabled(not self._deleting and self._selected_entry() is not None)

    def begin_delete(self, entry: ImageEntry) -> None:
        debug_log("[DELETE] delete button clicked")
        self._deleting = True
        self.hint.hide()
        self.progress_panel.show()
        self.delete_status.setText(f"Удаление образа...\n{entry.name}")
        self.delete_stage.setText("Подготовка...")
        self.delete_progress.setRange(0, 0)
        self.list_widget.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self.close_btn.setEnabled(False)

    def set_delete_stage(self, stage: str) -> None:
        self.delete_stage.setText(stage)

    def end_delete_success(self) -> None:
        self._deleting = False
        self.progress_panel.hide()
        self.hint.show()
        self.list_widget.setEnabled(True)
        self.close_btn.setEnabled(True)
        self._update_delete_button()

    def end_delete_error(self) -> None:
        self._deleting = False
        self.progress_panel.hide()
        self.hint.show()
        self.list_widget.setEnabled(True)
        self.close_btn.setEnabled(True)
        self._update_delete_button()

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(entry, ImageEntry):
            self._delete_entry(entry)

    def _delete_selected(self) -> None:
        entry = self._selected_entry()
        if entry is not None:
            self._delete_entry(entry)

    def _delete_entry(self, entry: ImageEntry) -> None:
        if not CicadaDialog.ask(
            self,
            "Удалить образ",
            f"Удалить образ:\n\n{entry.name}\n\n{entry.relative_path}\n\n"
            "Файл будет удалён с флешки.\nПродолжить?",
            yes_text="Да",
            no_text="Нет",
        ):
            return
        self.begin_delete(entry)
        if self._on_delete_requested is not None:
            self._on_delete_requested(entry, self)

    def remove_entry(self, entry: ImageEntry) -> None:
        debug_log("[DELETE] refreshing image list")

        def _same_entry(a: ImageEntry, b: ImageEntry) -> bool:
            if a.relative_path and b.relative_path:
                return a.relative_path == b.relative_path
            return a.path == b.path

        for index in range(self.list_widget.count() - 1, -1, -1):
            item = self.list_widget.item(index)
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, ImageEntry) and _same_entry(data, entry):
                self.list_widget.takeItem(index)
                break
        self._entries = [e for e in self._entries if not _same_entry(e, entry)]
        self.end_delete_success()
        self._update_delete_button()
        if not self._entries:
            self.hint.setText("Образов для удаления нет")
            self.hint.show()
            self.delete_btn.setEnabled(False)
            self.list_widget.clear()


_ABOUT_READONLY = Qt.TextInteractionFlag.NoTextInteraction
_ABOUT_RULE = "━━━━━━━━━━━━━━━━━━━━━━"


def _about_gradient_sep() -> QFrame:
    sep = QFrame()
    sep.setFixedHeight(1)
    sep.setStyleSheet(
        f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
        f"stop:0 transparent, stop:0.35 {COLORS['accent_purple']},"
        f"stop:0.65 {COLORS['accent_cyan']}, stop:1 transparent);"
    )
    return sep


def _about_text_rule() -> QLabel:
    lbl = QLabel(_ABOUT_RULE)
    lbl.setTextInteractionFlags(_ABOUT_READONLY)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(
        f"{LABEL_PLAIN} color: {_rgba(COLORS['accent_cyan'], 0.38)};"
        f" font-size: {_s(11)}px; letter-spacing: 0.5px; padding: {_s(2)}px 0;"
    )
    return lbl


def _about_readonly_label(
    text: str,
    *,
    color: str = "",
    font_size: int = FONTS["small"],
    weight: int = 400,
    line_height: str = "150%",
    align: Qt.AlignmentFlag | None = None,
) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(_ABOUT_READONLY)
    lbl.setStyleSheet(
        f"{LABEL_PLAIN} color: {color or COLORS['text_muted']};"
        f" font-size: {font_size}px; font-weight: {weight}; line-height: {line_height};"
    )
    if align is not None:
        lbl.setAlignment(align)
    return lbl


def _about_section_heading(text: str) -> QLabel:
    lbl = _about_readonly_label(
        text,
        color=COLORS["accent_cyan"],
        font_size=FONTS["step_title"],
        weight=800,
    )
    lbl.setStyleSheet(
        lbl.styleSheet()
        + f" letter-spacing: 1.4px; padding-top: {_s(2)}px;"
    )
    return lbl


def _about_glass_panel() -> tuple[QFrame, QVBoxLayout]:
    panel = QFrame()
    panel.setObjectName("aboutGlassPanel")
    panel.setStyleSheet(f"""
        QFrame#aboutGlassPanel {{
            background-color: {_rgba(COLORS['bg_card'], 0.55)};
            border: 1px solid {_rgba(COLORS['accent_purple'], 0.32)};
            border-radius: {_s(14)}px;
        }}
        QFrame#aboutGlassPanel QLabel {{
            {LABEL_PLAIN}
        }}
    """)
    _shadow(panel, blur=18, y=4)
    lay = QVBoxLayout(panel)
    lay.setContentsMargins(_s(18), _s(16), _s(18), _s(16))
    lay.setSpacing(_s(10))
    return panel, lay


def _about_feature_row(emoji: str, text: str) -> QWidget:
    row = QWidget()
    row.setStyleSheet("background: transparent; border: none;")
    row_lay = QHBoxLayout(row)
    row_lay.setContentsMargins(0, _s(2), 0, _s(2))
    row_lay.setSpacing(_s(12))
    icon = _about_readonly_label(emoji, font_size=FONTS["body"])
    icon.setFixedWidth(_s(30))
    icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
    row_lay.addWidget(icon)
    row_lay.addWidget(
        _about_readonly_label(text, color=COLORS["text_primary"], line_height="155%"),
        stretch=1,
    )
    return row


def _about_status_row(label: str, value: str, value_color: str) -> QWidget:
    row = QWidget()
    row.setStyleSheet("background: transparent; border: none;")
    row_lay = QHBoxLayout(row)
    row_lay.setContentsMargins(0, _s(3), 0, _s(3))
    row_lay.setSpacing(_s(8))
    row_lay.addWidget(_about_readonly_label(label, color=COLORS["text_muted"]))
    row_lay.addStretch()
    val = _about_readonly_label(value, color=value_color, weight=700)
    val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    row_lay.addWidget(val)
    return row


class AboutDialog(QDialog):

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("О программе")
        self.setModal(True)
        self.resize(_s(760), _s(680))
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_s(12), _s(12), _s(12), _s(12))

        card = QFrame()
        card.setObjectName("aboutCard")
        card.setStyleSheet(f"""
            QFrame#aboutCard {{
                background: qlineargradient(x1:0,y1:0,x2:0.85,y2:1,
                    stop:0 #0E1628, stop:0.45 #0B1220, stop:1 #050914);
                border: 1px solid {_rgba(COLORS['accent_cyan'], 0.4)};
                border-radius: {_s(20)}px;
            }}
            QScrollArea#aboutScroll {{
                background: transparent;
                border: none;
            }}
            QScrollArea#aboutScroll > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: {_s(6)}px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {_rgba(COLORS['accent_purple'], 0.45)};
                border-radius: {_s(3)}px;
                min-height: {_s(24)}px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)
        _shadow(card, blur=32, y=8)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(_s(28), _s(24), _s(28), _s(20))
        layout.setSpacing(_s(14))

        header = QHBoxLayout()
        header.setSpacing(_s(16))
        header.addWidget(_make_icon_badge(COLORS["accent_cyan"], _s(80)), alignment=Qt.AlignmentFlag.AlignTop)

        title_col = QVBoxLayout()
        title_col.setSpacing(_s(8))
        title = QLabel("CICADA USB BOOT")
        title.setTextInteractionFlags(_ABOUT_READONLY)
        title.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['text_primary']};"
            f" font-size: {FONTS['hero']}px; font-weight: 900; letter-spacing: 2px;"
        )
        title_col.addWidget(title)

        version_lbl = QLabel(f"v{APP_VERSION} PRO")
        version_lbl.setTextInteractionFlags(_ABOUT_READONLY)
        version_lbl.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['accent_cyan']};"
            f" font-size: {FONTS['card_title']}px; font-weight: 800; letter-spacing: 1px;"
        )
        title_col.addWidget(version_lbl)

        subtitle = QLabel("Расширенный мастер загрузочных носителей")
        subtitle.setTextInteractionFlags(_ABOUT_READONLY)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['text_muted']};"
            f" font-size: {FONTS['body']}px; letter-spacing: 0.3px; line-height: 145%;"
        )
        title_col.addWidget(subtitle)
        header.addLayout(title_col, stretch=1)

        close_hdr = _make_window_control_button("✕", "#EF4444")
        close_hdr.clicked.connect(self.accept)
        header.addWidget(close_hdr, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        layout.addWidget(_about_text_rule())

        scroll = QScrollArea()
        scroll.setObjectName("aboutScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        scroll_body = QWidget()
        scroll_body.setStyleSheet("background: transparent; border: none;")
        body_lay = QVBoxLayout(scroll_body)
        body_lay.setContentsMargins(0, _s(6), _s(2), 0)
        body_lay.setSpacing(_s(14))

        about_panel, about_lay = _about_glass_panel()
        about_lay.addWidget(_about_section_heading("О ПРОГРАММЕ"))
        about_lay.addWidget(_about_text_rule())
        about_lay.addWidget(_about_readonly_label(
            "CICADA USB BOOT — инструмент для создания и управления "
            "защищёнными мультизагрузочными USB-накопителями.",
            color=COLORS["text_primary"],
            line_height="170%",
        ))
        about_lay.addWidget(_about_readonly_label(
            "Программа позволяет создавать профессиональные загрузочные флешки "
            "с поддержкой Windows, Linux, WinPE, UEFI и Legacy BIOS.",
            line_height="170%",
        ))
        body_lay.addWidget(about_panel)

        features_panel, features_lay = _about_glass_panel()
        features_lay.addWidget(_about_section_heading("ВОЗМОЖНОСТИ"))
        features_lay.addWidget(_about_text_rule())
        for emoji, feat in (
            ("🪟", "Добавление образов Windows"),
            ("🐧", "Добавление образов Linux"),
            ("💻", "Добавление образов WinPE"),
            ("🛡", "Защищённая структура разделов"),
            ("🔒", "Скрытие загрузочных разделов"),
            ("📂", "Управление образами без доступа к файловой системе"),
            ("⚡", "Автоматическое создание загрузочного меню"),
            ("💾", "Поддержка NTFS + FAT32"),
        ):
            features_lay.addWidget(_about_feature_row(emoji, feat))
        body_lay.addWidget(features_panel)

        status_panel, status_lay = _about_glass_panel()
        status_lay.addWidget(_about_section_heading("СТАТУС СИСТЕМЫ"))
        status_lay.addWidget(_about_text_rule())
        for label, value, color in (
            ("Защита разделов:", "Активна", COLORS["accent_green"]),
            ("Режим безопасности:", "Black Vault", COLORS["accent_purple"]),
            ("Загрузочный раздел:", "Скрыт", COLORS["accent_cyan"]),
            ("Поддержка UEFI:", "Включена", COLORS["accent_green"]),
        ):
            status_lay.addWidget(_about_status_row(label, value, color))
        body_lay.addWidget(status_panel)

        project_panel, project_lay = _about_glass_panel()
        project_lay.addWidget(_about_section_heading("ПРОЕКТ"))
        project_lay.addWidget(_about_text_rule())
        project_lay.addWidget(_about_readonly_label(
            "CICADA3301",
            color=COLORS["text_primary"],
            font_size=FONTS["card_title"],
            weight=800,
            align=Qt.AlignmentFlag.AlignCenter,
        ))
        project_lay.addWidget(_about_readonly_label(
            "Black Vault Edition",
            color=COLORS["accent_purple"],
            font_size=FONTS["body"],
            weight=700,
            align=Qt.AlignmentFlag.AlignCenter,
        ))
        body_lay.addWidget(project_panel)

        body_lay.addStretch()
        scroll.setWidget(scroll_body)
        layout.addWidget(scroll, stretch=1)

        layout.addWidget(_about_text_rule())

        footer_col = QVBoxLayout()
        footer_col.setSpacing(_s(4))
        footer_col.addWidget(_about_readonly_label(
            f"CICADA USB BOOT v{APP_VERSION} PRO",
            color=COLORS["text_primary"],
            font_size=FONTS["body"],
            weight=700,
            align=Qt.AlignmentFlag.AlignCenter,
        ))
        footer_col.addWidget(_about_readonly_label(
            "© Cicada3301 Project",
            color=COLORS["text_muted"],
            font_size=FONTS["small"],
            align=Qt.AlignmentFlag.AlignCenter,
        ))
        layout.addLayout(footer_col)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = _make_accent_button("Закрыть", COLORS["accent_cyan"], min_height=_s(40))
        ok_btn.setMinimumWidth(_s(130))
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)
        outer.addWidget(card)

    def showEvent(self, event):
        super().showEvent(event)
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            frame = self.frameGeometry()
            self.move(
                geo.x() + (geo.width() - frame.width()) // 2,
                geo.y() + (geo.height() - frame.height()) // 2,
            )


def require_admin_startup() -> None:
    classified = classify_admin_error()
    dlg = CicadaDialog(None, classified.title, classified.message, kind="admin")
    if dlg.exec() == QDialog.DialogCode.Accepted:
        relaunch_as_admin()
    sys.exit(0)


# ═══════════════════════════════════════════════════════════════════════
#  Drag title bar
# ═══════════════════════════════════════════════════════════════════════

class DragTitleBar(QFrame):

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._drag_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()


class PremiumHeaderBar(DragTitleBar):
    """Верхняя шапка — cyber-security HUD (компактная)."""

    def __init__(self, window, version: str):
        super().__init__(window)
        self._max_btn: _HudWindowButton | None = None
        self.status_labels: dict[str, QLabel] = {}
        self.setObjectName("premiumHeaderBar")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.setFixedHeight(HEADER_BAR_HEIGHT)
        self.setStyleSheet("background: transparent;")
        self._build(version)

    def _build(self, version: str) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        card = QFrame()
        card.setObjectName("premiumHeaderCard")
        card.setFixedHeight(HEADER_BAR_HEIGHT)
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        card.setStyleSheet(f"""
            QFrame#premiumHeaderCard {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 rgba(6,17,31,0.55),
                    stop:0.5 rgba(8,14,28,0.35),
                    stop:1 rgba(20,10,40,0.40));
                border: 1px solid rgba(0,217,255,0.12);
                border-radius: 12px;
            }}
            QFrame#premiumHeaderCard QLabel {{ {LABEL_PLAIN} }}
        """)
        card_lay = QHBoxLayout(card)
        card_lay.setContentsMargins(14, 9, 14, 9)
        card_lay.setSpacing(10)

        logo = QLabel()
        logo.setFixedSize(HEADER_LOGO_SIZE, HEADER_LOGO_SIZE)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(Icons.pixmap("cicada", HEADER_LOGO_SIZE - 8))
        logo.setStyleSheet(f"""
            QLabel {{
                {LABEL_PLAIN}
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 {_rgba(COLORS['accent_cyan'], 0.14)},
                    stop:1 {_rgba(COLORS['accent_purple'], 0.10)});
                border: 1px solid {_rgba(COLORS['accent_cyan'], 0.35)};
                border-radius: {HEADER_LOGO_SIZE // 2}px;
            }}
        """)
        card_lay.addWidget(logo, alignment=Qt.AlignmentFlag.AlignVCenter)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title_col.setContentsMargins(0, 0, 0, 0)

        title = QLabel("CICADA USB BOOT")
        title.setWordWrap(False)
        title.setStyleSheet(
            "color: #ffffff; font-size: 20px; font-weight: 900;"
            " letter-spacing: 0.35px; margin: 0; padding: 0;"
        )
        title_glow = QGraphicsDropShadowEffect()
        title_glow.setBlurRadius(8)
        title_glow.setColor(QColor(0, 217, 255, 60))
        title_glow.setOffset(0, 0)
        title.setGraphicsEffect(title_glow)
        title_col.addWidget(title)

        subtitle = QLabel("Secure Boot Environment • Premium Edition")
        subtitle.setWordWrap(False)
        subtitle.setStyleSheet(
            f"color: {_rgba(COLORS['accent_cyan'], 0.72)};"
            " font-size: 10px; font-weight: 600; letter-spacing: 0.1px;"
            " margin: 0; padding: 0;"
        )
        title_col.addWidget(subtitle)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.setContentsMargins(0, 2, 0, 0)
        for key, text, color in (
            ("ready", "● READY", "#00ff99"),
            ("usb", "● USB DETECTED", "#00d9ff"),
            ("protected", "● PROTECTED", "#b36bff"),
        ):
            lbl = QLabel(text)
            lbl.setWordWrap(False)
            lbl.setStyleSheet(
                f"color: {_rgba(color, 0.35)}; font-size: 10px; font-weight: 800;"
                " letter-spacing: 0.15px; margin: 0; padding: 0;"
            )
            lbl.setProperty("status_color", color)
            self.status_labels[key] = lbl
            status_row.addWidget(lbl)
        title_col.addLayout(status_row)
        card_lay.addLayout(title_col, stretch=1)

        controls = QWidget()
        controls.setObjectName("premiumHeaderControls")
        controls.setFixedWidth(HEADER_BADGE_W + HEADER_BTN_SIZE * 3 + 5 * 3 + 4)
        controls.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        controls.setStyleSheet("background: transparent;")
        ctrl_lay = QHBoxLayout(controls)
        ctrl_lay.setContentsMargins(0, 0, 0, 0)
        ctrl_lay.setSpacing(5)
        ctrl_lay.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        ctrl_lay.addWidget(_make_pro_version_badge(version))

        self._min_btn = _HudWindowButton("minimize")
        self._min_btn.clicked.connect(self._window.showMinimized)
        self._max_btn = _HudWindowButton("maximize")
        self._max_btn.clicked.connect(self._toggle_maximize)
        self._close_btn = _HudWindowButton("close")
        self._close_btn.clicked.connect(self._window.close)
        for btn in (self._min_btn, self._max_btn, self._close_btn):
            ctrl_lay.addWidget(btn)

        card_lay.addWidget(controls, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(card)

    def _toggle_maximize(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
            if self._max_btn is not None:
                self._max_btn.setText("□")
        else:
            self._window.showMaximized()
            if self._max_btn is not None:
                self._max_btn.setText("❐")


# ═══════════════════════════════════════════════════════════════════════
#  Build steps widget — с векторными иконками в кружках
# ═══════════════════════════════════════════════════════════════════════

def _step_title_multiline(title: str) -> str:
    return "\n".join(title.split())


def _step_title_line_count(title: str) -> int:
    return max(1, len(title.split()))


class BuildStepsWidget(QFrame):

    def __init__(self):
        super().__init__()
        self.setObjectName("stepsPanel")
        self._circles: list[QLabel] = []
        self._titles: list[QLabel] = []
        self._active_step = 0
        self._build_ui()

    def _build_ui(self) -> None:
        self.setStyleSheet(f"""
            QFrame#stepsPanel {{
                background-color: {COLORS['bg_card']};
                border: 1px solid {PANEL_BORDER};
                border-radius: {_s(16)}px;
            }}
            QFrame#stepsPanel QLabel {{
                {LABEL_PLAIN}
            }}
        """)
        _shadow(self, blur=24, y=6)

        root = QVBoxLayout(self)
        root.setContentsMargins(_s(18), _s(18), _s(18), _s(16))
        root.setSpacing(_s(14))

        step_col_min = _s(96)
        circle_outer = ICON_SIZE_STEP + _s(10)
        step_line_h = _s(20)
        max_title_lines = max(_step_title_line_count(title) for title, _ in BUILD_STEPS)
        title_block_height = step_line_h * max_title_lines + _s(4)
        row = QHBoxLayout()
        row.setSpacing(0)
        row.setContentsMargins(0, 0, 0, 0)
        for index, (title, _subtitle) in enumerate(BUILD_STEPS):
            if index > 0:
                dash = QFrame()
                dash.setFixedSize(_s(10), 2)
                dash.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                dash.setStyleSheet(
                    f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                    f"stop:0 {COLORS['border']}, stop:0.5 {_rgba(COLORS['accent_cyan'],0.35)},"
                    f"stop:1 {COLORS['border']}); margin-bottom: {title_block_height + _s(10)}px;"
                )
                row.addWidget(dash, alignment=Qt.AlignmentFlag.AlignTop)

            col_wrap = QWidget()
            col_wrap.setMinimumWidth(step_col_min)
            col_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            col = QVBoxLayout(col_wrap)
            col.setSpacing(_s(8))
            col.setContentsMargins(0, 0, 0, 0)
            col.setAlignment(Qt.AlignmentFlag.AlignHCenter)

            circle = QLabel()
            circle.setFixedSize(circle_outer, circle_outer)
            circle.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._circles.append(circle)
            col.addWidget(circle, alignment=Qt.AlignmentFlag.AlignHCenter)

            lines = _step_title_line_count(title)
            title_lbl = QLabel(_step_title_multiline(title))
            title_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            title_lbl.setWordWrap(False)
            title_lbl.setMinimumHeight(step_line_h * lines + _s(2))
            title_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            title_lbl.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['text_muted']}; font-size: {FONTS['step_title']}px; font-weight: 700;"
            )
            self._titles.append(title_lbl)
            col.addWidget(title_lbl)

            row.addWidget(col_wrap, stretch=1, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(row)

        prog_row = QHBoxLayout()
        prog_row.setSpacing(_s(10))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        progress_h = _s(36)
        self.progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: rgba(2,6,23,0.85);
                border: 1px solid {PANEL_BORDER};
                border-radius: {_s(14)}px;
                min-height: {progress_h}px; max-height: {progress_h}px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {COLORS['accent_cyan']}, stop:0.5 {COLORS['accent_blue']},
                    stop:1 {COLORS['accent_purple']});
                border-radius: {_s(12)}px;
            }}
        """)
        prog_row.addWidget(self.progress, stretch=1)
        self.progress_label = QLabel("0%")
        self.progress_label.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['accent_cyan']}; font-weight: 800; font-size: {FONTS['card_title']}px; min-width: {_s(48)}px;"
        )
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        prog_row.addWidget(self.progress_label)
        root.addLayout(prog_row)

        panel_min_h = circle_outer + title_block_height + _s(56) + progress_h + _s(44)
        self.setMinimumHeight(panel_min_h)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.set_step(0)

    def _render_circle(self, index: int, color: str, bg_alpha: float, border_color: str) -> None:
        icon_key = STEP_ICON_KEYS[index]
        cs = ICON_SIZE_STEP + _s(10)
        icon_size = ICON_SIZE_STEP
        px = QPixmap(cs, cs)
        px.fill(Qt.GlobalColor.transparent)
        painter = QPainter(px)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fc = Icons._c(color)
        fc.setAlphaF(bg_alpha)
        painter.setBrush(QBrush(fc))
        bc = Icons._c(border_color)
        painter.setPen(QPen(bc, max(1.5, cs / 18)))
        painter.drawEllipse(QRectF(1, 1, cs - 2, cs - 2))
        icon_px = Icons.pixmap(icon_key, icon_size)
        ix = (cs - icon_size) // 2
        painter.drawPixmap(ix, ix, icon_px)
        painter.end()
        self._circles[index].setPixmap(px)
        self._circles[index].setStyleSheet("")

    def set_progress(self, value: int) -> None:
        self.progress.setValue(value)
        self.progress_label.setText(f"{value}%")

    def set_step(self, step: int) -> None:
        self._active_step = max(0, min(step, len(BUILD_STEPS) - 1))
        for index in range(len(BUILD_STEPS)):
            if index < self._active_step:
                self._render_circle(index, COLORS["accent_green"], 0.12, COLORS["accent_green"])
            elif index == self._active_step:
                self._render_circle(index, COLORS["accent_cyan"], 0.14, COLORS["accent_cyan"])
            else:
                self._render_circle(index, COLORS["text_muted"], 0.05, COLORS["border"])
            tc = COLORS["accent_cyan"] if index <= self._active_step else COLORS["text_muted"]
            if index == self._active_step:
                tc = COLORS["text_primary"]
            self._titles[index].setStyleSheet(
                f"{LABEL_PLAIN} color: {tc}; font-size: {FONTS['step_title']}px; font-weight: 700;"
            )


# ═══════════════════════════════════════════════════════════════════════
#  Sidebar widgets
# ═══════════════════════════════════════════════════════════════════════

def _sidebar_section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"{LABEL_PLAIN} color: {_rgba(COLORS['accent_cyan'], 0.65)};"
        f" font-size: {_s(10)}px; font-weight: 800; letter-spacing: 1.2px;"
        f" padding: {_s(2)}px {_s(4)}px {_s(0)}px;"
    )
    return lbl


class _ToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(_s(46), _s(26))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        checked = bool(checked)
        if self._checked == checked:
            return
        self._checked = checked
        self.update()
        self.toggled.emit(checked)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if self._checked:
            painter.setBrush(QColor(0, 217, 255, 200))
        else:
            painter.setBrush(QColor(30, 41, 59, 220))
        painter.setPen(QPen(QColor(0, 217, 255, 90 if self._checked else 40), 1))
        painter.drawRoundedRect(0, 0, w, h, h / 2, h / 2)
        knob = h - 6
        knob_x = w - knob - 3 if self._checked else 3
        painter.setBrush(QColor(255, 255, 255))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(knob_x, 3, knob, knob)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
        super().mousePressEvent(event)


class _SidebarNavButton(QFrame):
    clicked = pyqtSignal()

    def __init__(
        self,
        title: str,
        subtitle: str,
        icon_key: str,
        *,
        counter_key: str | None = None,
        accent: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._active = False
        self._hover = False
        self._accent = accent or COLORS["accent_cyan"]
        self.counter_key = counter_key
        self.nav_icon_key = icon_key
        self.setObjectName("sidebarNavBtn")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(_s(56))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._glow = QGraphicsDropShadowEffect()
        self._glow.setBlurRadius(_s(0))
        self._glow.setColor(QColor(0, 229, 255, 0))
        self._glow.setOffset(0, 0)
        self.setGraphicsEffect(self._glow)

        root = QHBoxLayout(self)
        root.setContentsMargins(_s(10), _s(8), _s(10), _s(8))
        root.setSpacing(_s(10))

        icon_sz = _s(32)
        self._icon = QLabel()
        self._icon.setFixedSize(icon_sz, icon_sz)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setPixmap(Icons.pixmap(icon_key, icon_sz))
        root.addWidget(self._icon, alignment=Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(_s(1))
        self._title = QLabel(title)
        self._title.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['text_primary']};"
            f" font-size: {_s(13)}px; font-weight: 800; letter-spacing: 0.3px;"
        )
        self._subtitle = QLabel(subtitle)
        self._subtitle.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['text_muted']};"
            f" font-size: {_s(10)}px; font-weight: 600;"
        )
        text_col.addWidget(self._title)
        text_col.addWidget(self._subtitle)
        root.addLayout(text_col, stretch=1)

        self._counter = QLabel("0")
        self._counter.setFixedSize(_s(28), _s(22))
        self._counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if counter_key is None:
            self._counter.hide()
        root.addWidget(self._counter, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._apply_style()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._apply_style()

    def set_count(self, count: int) -> None:
        self._counter.setText(str(count))

    def enterEvent(self, event) -> None:
        self._hover = True
        if self.isEnabled():
            self._glow.setBlurRadius(_s(18))
            self._glow.setColor(QColor(0, 229, 255, 70))
        self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self._glow.setBlurRadius(_s(0))
        self._glow.setColor(QColor(0, 229, 255, 0))
        self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.clicked.emit()
        super().mousePressEvent(event)

    def _apply_style(self) -> None:
        if not self.isEnabled():
            bg = "rgba(6,10,18,0.55)"
            border = "rgba(148,163,184,0.12)"
            title_color = "rgba(148,163,184,0.35)"
            sub_color = "rgba(148,163,184,0.25)"
            counter_bg = "rgba(148,163,184,0.08)"
            counter_color = "rgba(148,163,184,0.30)"
        elif self._active:
            bg = (
                f"qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                f"stop:0 {_rgba(self._accent, 0.22)},"
                f"stop:1 {_rgba(COLORS['accent_purple'], 0.10)})"
            )
            border = _rgba(self._accent, 0.50)
            title_color = "#ffffff"
            sub_color = _rgba(self._accent, 0.85)
            counter_bg = _rgba(self._accent, 0.18)
            counter_color = self._accent
        elif self._hover:
            bg = _rgba(self._accent, 0.10)
            border = _rgba(self._accent, 0.35)
            title_color = self._accent
            sub_color = COLORS["text_muted"]
            counter_bg = _rgba(self._accent, 0.14)
            counter_color = self._accent
        else:
            bg = "rgba(255,255,255,0.02)"
            border = "rgba(0,229,255,0.14)"
            title_color = COLORS["text_primary"]
            sub_color = COLORS["text_muted"]
            counter_bg = "rgba(0,229,255,0.08)"
            counter_color = COLORS["accent_cyan"]

        self.setStyleSheet(f"""
            QFrame#sidebarNavBtn {{
                background: {bg};
                border: 1px solid {border};
                border-radius: {_s(12)}px;
            }}
        """)
        self._title.setStyleSheet(
            f"{LABEL_PLAIN} color: {title_color};"
            f" font-size: {_s(13)}px; font-weight: 800; letter-spacing: 0.3px;"
        )
        self._subtitle.setStyleSheet(
            f"{LABEL_PLAIN} color: {sub_color};"
            f" font-size: {_s(10)}px; font-weight: 600;"
        )
        counter_border = (
            _rgba(counter_color, 0.30)
            if counter_color.startswith("#")
            else "rgba(0,229,255,0.22)"
        )
        self._counter.setStyleSheet(f"""
            QLabel {{
                {LABEL_PLAIN}
                background-color: {counter_bg};
                color: {counter_color};
                border: 1px solid {counter_border};
                border-radius: {_s(6)}px;
                font-size: {_s(11)}px;
                font-weight: 800;
            }}
        """)


# ═══════════════════════════════════════════════════════════════════════
#  Main window
# ═══════════════════════════════════════════════════════════════════════

class CicadaUsbTool(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.setMinimumSize(_s(1360), _s(800))
        self.resize(_s(1420), _s(860))
        self._centered = False
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self.base_dir = cicada_temp_dir()
        self.worker = None
        self._usb_scan_worker: UsbScanWorker | None = None
        self._flag_auto_worker: FlagAutoCheckWorker | None = None
        self._last_selected_disk_index = -1
        self.selected_disk_number: int | None = None
        self.selected_usb_unique_id: str | None = None
        self.selected_disk_id: str | None = None
        self.current_request_id = 0
        self._pipeline_retry_timer = QTimer(self)
        self._pipeline_retry_timer.setSingleShot(True)
        self._pipeline_retry_timer.timeout.connect(self._retry_disk_pipeline)
        self._stats_cache_worker: StatsCacheWorker | None = None
        self._stats_scan_worker: StatsScanWorker | None = None
        self._stats_scan_id = 0
        self._delete_list_worker: ImageDeleteWorker | None = None
        self._pending_delete_entry: ImageEntry | None = None
        self._pending_delete_dialog: DeleteImagesDialog | None = None
        self._import_dialog: ImageImportProgressDialog | None = None
        self._partition_lock = threading.RLock()
        self._scanning = False
        self._app_state = AppState.IDLE
        self.ready_for_view = False
        self.flag_verified = False
        self.stats_loaded = False
        self.ready_for_actions = False
        self._last_logged_ui_state: tuple[bool, bool, bool, bool] | None = None
        self.selected_disk_is_cicada = False
        self._image_count = 0
        self._nav_buttons: list[_SidebarNavButton] = []
        self._active_nav_index = -1
        self._footer_stats: dict[str, QLabel] = {}
        self._sidebar_system_status: QLabel | None = None
        self._sidebar_usb_lines: dict[str, QLabel] = {}
        self._header_status: dict[str, QLabel] = {}
        self.setup_ui()
        self._enter_scanning_state(initial=True)
        self.disk_combo.currentIndexChanged.connect(self._on_disk_index_changed)
        self.disk_combo.activated.connect(self._on_disk_index_changed)
        QTimer.singleShot(100, lambda: self.refresh_disks(initial=True))

    def showEvent(self, event):
        super().showEvent(event)
        if not self._centered:
            self._center_on_screen()
            self._centered = True

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        frame = self.frameGeometry()
        self.move(
            geo.x() + (geo.width() - frame.width()) // 2,
            geo.y() + (geo.height() - frame.height()) // 2,
        )

    def _panel(
        self, title: str, icon_key: str = "", icon_color: str = "", *, header: bool = True
    ) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("contentPanel")
        frame.setStyleSheet(f"""
            QFrame#contentPanel {{
                background-color: {COLORS['bg_card']};
                border: 1px solid {PANEL_BORDER};
                border-radius: {_s(16)}px;
            }}
            QFrame#contentPanel QLabel {{
                {LABEL_PLAIN}
            }}
        """)
        _shadow(frame, blur=20, y=3)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(_s(16), _s(12), _s(16), _s(12))
        layout.setSpacing(_s(10))
        if header and (title or icon_key):
            head_row = QHBoxLayout()
            head_row.setSpacing(8)
            if icon_key:
                head_row.addWidget(
                    _make_icon_label(icon_key, icon_color or COLORS["accent_cyan"], ICON_SIZE_NAV)
                )
            heading = QLabel(title)
            heading.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['text_primary']}; font-size: {FONTS['card_title']}px; font-weight: 800; letter-spacing: 1px;"
            )
            head_row.addWidget(heading)
            head_row.addStretch()
            layout.addLayout(head_row)
        return frame, layout

    def _set_active_nav(self, index: int = 0) -> None:
        self._active_nav_index = index
        self._refresh_nav_styles()

    def _refresh_nav_styles(self) -> None:
        for i, btn in enumerate(self._nav_buttons):
            btn.set_active(i == self._active_nav_index)
            icon_key = btn.nav_icon_key
            icon_sz = _s(32)
            btn._icon.setPixmap(Icons.pixmap(icon_key, icon_sz))

    def setup_ui(self) -> None:
        outer = QWidget()
        self.setCentralWidget(outer)
        outer_layout = QHBoxLayout(outer)
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(0)

        shell = QFrame()
        shell.setObjectName("mainShell")
        shell.setStyleSheet(f"""
            QFrame#mainShell {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #050914, stop:0.55 #07111f, stop:1 #020617);
                border: 1px solid rgba(0,229,255,0.25);
                border-radius: 20px;
            }}
        """)
        _shadow(shell, blur=32, y=6)
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        # ── Сайдбар ──────────────────────────────────────────────────
        sidebar = QFrame()
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        sidebar.setStyleSheet(f"""
            QFrame {{
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #06111f, stop:1 #020712);
                border-right: 1px solid rgba(0,229,255,0.22);
                border-top-left-radius: 20px;
                border-bottom-left-radius: 20px;
            }}
        """)
        side_lay = QVBoxLayout(sidebar)
        side_lay.setContentsMargins(_s(10), _s(10), _s(10), _s(10))
        side_lay.setSpacing(_s(4))

        brand_card = QFrame()
        brand_card.setObjectName("sidebarBrandCard")
        brand_card.setStyleSheet(f"""
            QFrame#sidebarBrandCard {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 {_rgba(COLORS['accent_cyan'], 0.10)},
                    stop:1 {_rgba(COLORS['accent_purple'], 0.08)});
                border: 1px solid {_rgba(COLORS['accent_cyan'], 0.35)};
                border-radius: {_s(14)}px;
            }}
            QFrame#sidebarBrandCard QLabel {{ {LABEL_PLAIN} }}
        """)
        brand_lay = QVBoxLayout(brand_card)
        brand_lay.setContentsMargins(_s(12), _s(10), _s(12), _s(10))
        brand_lay.setSpacing(_s(4))
        brand_top = QHBoxLayout()
        brand_top.setSpacing(_s(10))
        brand_top.addWidget(_make_icon_badge(COLORS["accent_cyan"], _s(52)))
        brand_text = QVBoxLayout()
        brand_text.setSpacing(_s(1))
        brand_name = QLabel("CICADA3301")
        brand_name.setStyleSheet(
            f"color: #ffffff; font-size: {_s(15)}px; font-weight: 900; letter-spacing: 0.6px;"
        )
        brand_suite = QLabel("Secure Boot Suite")
        brand_suite.setStyleSheet(
            f"color: {_rgba(COLORS['accent_cyan'], 0.90)};"
            f" font-size: {_s(10)}px; font-weight: 600;"
        )
        brand_ver = QLabel(f"v{APP_VERSION} PRO")
        brand_ver.setStyleSheet(
            f"color: {_rgba(COLORS['accent_purple'], 1.0)};"
            f" font-size: {_s(10)}px; font-weight: 800; letter-spacing: 0.4px;"
        )
        brand_text.addWidget(brand_name)
        brand_text.addWidget(brand_suite)
        brand_text.addWidget(brand_ver)
        brand_top.addLayout(brand_text, stretch=1)
        brand_lay.addLayout(brand_top)
        self._sidebar_system_status = QLabel("● SYSTEM READY")
        self._sidebar_system_status.setStyleSheet(
            f"color: {COLORS['accent_green']}; font-size: {_s(10)}px; font-weight: 800;"
            f" letter-spacing: 0.5px;"
        )
        brand_lay.addWidget(self._sidebar_system_status)
        side_lay.addWidget(brand_card)

        nav_handlers = {
            "add_windows":   self._add_windows_image,
            "add_linux":     self._add_linux_image,
            "add_winpe":     self._add_winpe_image,
            "delete_image":  self._delete_image,
            "delete_cicada": self._delete_cicada_usb,
        }
        media_accents = {
            "add_windows": "#00ADEF",
            "add_linux": "#F97316",
            "add_winpe": COLORS["accent_purple"],
        }

        side_lay.addWidget(_sidebar_section_label("ОБРАЗЫ"))
        for nav_index, (title, icon_key, action, subtitle, counter_key) in enumerate(MEDIA_NAV_ITEMS):
            btn = _SidebarNavButton(
                title,
                subtitle,
                icon_key,
                counter_key=counter_key,
                accent=media_accents.get(action, COLORS["accent_cyan"]),
            )
            btn.setProperty("nav_action", action)
            btn.clicked.connect(nav_handlers[action])
            btn.clicked.connect(lambda idx=nav_index: self._set_active_nav(idx))
            self._nav_buttons.append(btn)
            side_lay.addWidget(btn)

        side_lay.addSpacing(_s(2))
        side_lay.addWidget(_sidebar_section_label("УПРАВЛЕНИЕ"))
        mgmt_offset = len(MEDIA_NAV_ITEMS)
        for nav_index, (title, icon_key, action, subtitle) in enumerate(MANAGEMENT_NAV_ITEMS):
            btn = _SidebarNavButton(
                title,
                subtitle,
                icon_key,
                accent=COLORS["accent_red"] if action == "delete_cicada" else COLORS["accent_yellow"],
            )
            btn.setProperty("nav_action", action)
            btn.clicked.connect(nav_handlers[action])
            btn.clicked.connect(
                lambda idx=mgmt_offset + nav_index: self._set_active_nav(idx)
            )
            self._nav_buttons.append(btn)
            side_lay.addWidget(btn)

        about_btn = QPushButton("О программе")
        about_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        about_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {COLORS['text_muted']};
                border: none;
                font-size: {_s(10)}px;
                font-weight: 600;
                text-align: left;
                padding: {_s(2)}px {_s(6)}px;
            }}
            QPushButton:hover {{
                color: {COLORS['accent_cyan']};
            }}
        """)
        about_btn.clicked.connect(self._show_about)
        side_lay.addWidget(about_btn)

        side_lay.addStretch()

        logging_row = QFrame()
        logging_row.setObjectName("sidebarLoggingRow")
        logging_row.setStyleSheet(f"""
            QFrame#sidebarLoggingRow {{
                background: rgba(255,255,255,0.02);
                border: 1px solid {_rgba(COLORS['accent_cyan'], 0.18)};
                border-radius: {_s(10)}px;
            }}
            QFrame#sidebarLoggingRow QLabel {{ {LABEL_PLAIN} }}
        """)
        logging_lay = QHBoxLayout(logging_row)
        logging_lay.setContentsMargins(_s(10), _s(6), _s(10), _s(6))
        logging_lay.setSpacing(_s(8))
        logging_lbl = QLabel("Логирование")
        logging_lbl.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: {_s(11)}px; font-weight: 700;"
        )
        logging_lay.addWidget(logging_lbl, stretch=1)
        self.logging_toggle = _ToggleSwitch(is_logging_enabled())
        self.logging_toggle.toggled.connect(self._on_logging_toggled)
        logging_lay.addWidget(self.logging_toggle)
        side_lay.addWidget(logging_row)

        usb_status = QFrame()
        usb_status.setObjectName("sidebarUsbStatus")
        usb_status.setStyleSheet(f"""
            QFrame#sidebarUsbStatus {{
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 {_rgba(COLORS['accent_purple'], 0.10)},
                    stop:1 {_rgba(COLORS['accent_cyan'], 0.06)});
                border: 1px solid {_rgba(COLORS['accent_cyan'], 0.30)};
                border-radius: {_s(12)}px;
            }}
            QFrame#sidebarUsbStatus QLabel {{ {LABEL_PLAIN} }}
        """)
        usb_lay = QVBoxLayout(usb_status)
        usb_lay.setContentsMargins(_s(10), _s(8), _s(10), _s(8))
        usb_lay.setSpacing(_s(3))
        usb_title = QLabel("USB STATUS")
        usb_title.setStyleSheet(
            f"color: {_rgba(COLORS['accent_cyan'], 0.85)};"
            f" font-size: {_s(10)}px; font-weight: 800; letter-spacing: 1px;"
        )
        usb_lay.addWidget(usb_title)
        for key, text, color in (
            ("boot", "CICADA USB BOOT", COLORS["text_muted"]),
            ("protected", "Protected Mode", COLORS["text_muted"]),
            ("mbr", "MBR Signature —", COLORS["text_muted"]),
        ):
            line = QLabel(text)
            line.setStyleSheet(
                f"color: {color}; font-size: {_s(10)}px; font-weight: 700;"
            )
            self._sidebar_usb_lines[key] = line
            usb_lay.addWidget(line)
        side_lay.addWidget(usb_status)

        shell_layout.addWidget(sidebar)

        # ── Контент ───────────────────────────────────────────────────
        content_wrap = QWidget()
        content_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        content_layout = QVBoxLayout(content_wrap)
        content_layout.setContentsMargins(CONTENT_GAP_FROM_SIDEBAR, _s(6), _s(14), _s(10))
        content_layout.setSpacing(_s(10))

        header_wrap = QWidget()
        header_wrap.setStyleSheet("background: transparent;")
        header_lay = QVBoxLayout(header_wrap)
        header_lay.setContentsMargins(0, 0, 0, 0)
        header_lay.setSpacing(4)
        premium_header = PremiumHeaderBar(self, APP_VERSION)
        self._header_status = premium_header.status_labels
        header_lay.addWidget(premium_header)
        header_glow_line = QFrame()
        header_glow_line.setFixedHeight(1)
        header_glow_line.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 rgba(0,217,255,0),
                    stop:0.15 rgba(0,217,255,0.85),
                    stop:0.5 rgba(123,77,255,0.90),
                    stop:0.85 rgba(0,217,255,0.85),
                    stop:1 rgba(0,217,255,0));
                border: none;
            }
        """)
        header_lay.addWidget(header_glow_line)
        content_layout.addWidget(header_wrap)

        # Top row
        top_row = QHBoxLayout()
        top_row.setSpacing(20)

        # Устройство (на всю ширину, без заголовка)
        device_frame, device_lay = self._panel("", "", header=False)
        device_lay.setContentsMargins(_s(10), _s(8), _s(10), _s(8))
        device_lay.setSpacing(0)

        device_row = QHBoxLayout()
        device_row.setSpacing(_s(10))
        device_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        usb_lbl = QLabel()
        usb_icon_size = _s(84)
        usb_lbl.setFixedSize(usb_icon_size, usb_icon_size)
        usb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        usb_lbl.setPixmap(Icons.pixmap("usb_drive", usb_icon_size))
        device_row.addWidget(usb_lbl)

        device_info_wrap = QWidget()
        device_info_wrap.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        device_info_wrap.setMinimumWidth(_s(240))
        device_info_wrap.setMaximumWidth(_s(300))
        device_info = QVBoxLayout(device_info_wrap)
        device_info.setSpacing(_s(2))
        device_info.setContentsMargins(0, 0, 0, 0)
        self.device_name = QLabel("USB не выбран")
        self.device_name.setWordWrap(True)
        self.device_name.setStyleSheet(f"{LABEL_PLAIN} color: {COLORS['text_primary']}; font-size: {FONTS['body']}px; font-weight: 800;")
        self.device_size = QLabel("—")
        self.device_size.setStyleSheet(f"{LABEL_PLAIN} color: {COLORS['text_muted']}; font-size: {FONTS['small']}px;")
        self.device_ready = QLabel("✓ ГОТОВО К РАБОТЕ")
        self.device_ready.setWordWrap(True)
        self.device_ready.setStyleSheet(f"{LABEL_PLAIN} color: {COLORS['accent_green']}; font-size: {_s(11)}px; font-weight: 800;")
        self.device_meta = QLabel("USB 2.0  •  MBR")
        self.device_meta.setWordWrap(True)
        self.device_meta.setStyleSheet(f"{LABEL_PLAIN} color: {COLORS['text_muted']}; font-size: {_s(11)}px;")
        self.device_flag_status = QLabel("Флаг Cicada: не проверен")
        self.device_flag_status.setWordWrap(True)
        self.device_flag_status.setStyleSheet(f"{LABEL_PLAIN} color: {COLORS['text_muted']}; font-size: {_s(11)}px;")
        self.restore_flag_btn = QPushButton("Восстановить флаг")
        self.restore_flag_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restore_flag_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba({int(COLORS['accent_yellow'][1:3],16)},{int(COLORS['accent_yellow'][3:5],16)},{int(COLORS['accent_yellow'][5:7],16)},0.18);
                color: {COLORS['accent_yellow']};
                border: 1px solid {_rgba(COLORS['accent_yellow'],0.45)};
                border-radius: 8px;
                font-size: {_s(11)}px;
                font-weight: 700;
                padding: {_s(4)}px {_s(10)}px;
            }}
            QPushButton:hover {{
                background-color: rgba({int(COLORS['accent_yellow'][1:3],16)},{int(COLORS['accent_yellow'][3:5],16)},{int(COLORS['accent_yellow'][5:7],16)},0.28);
            }}
            QPushButton:disabled {{
                color: {COLORS['text_muted']};
                border-color: {COLORS['border']};
            }}
        """)
        self.restore_flag_btn.hide()
        self.restore_flag_btn.clicked.connect(self._restore_cicada_flag_clicked)
        for w in (self.device_name, self.device_size, self.device_ready, self.device_meta, self.device_flag_status):
            device_info.addWidget(w)
        device_info.addWidget(self.restore_flag_btn)
        device_row.addWidget(device_info_wrap)

        self.disk_selector = UsbDeviceSelector()
        self.disk_selector.setMinimumWidth(_s(360))
        self.disk_selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.disk_combo = self.disk_selector.combo
        device_row.addWidget(self.disk_selector, stretch=1)
        self.refresh_btn = RefreshDiskButton()
        self.refresh_btn.clicked.connect(lambda: self.refresh_disks(initial=False))
        device_row.addWidget(self.refresh_btn)
        device_lay.addLayout(device_row)

        scan_row = QHBoxLayout()
        scan_row.setContentsMargins(_s(4), 0, _s(4), 0)
        scan_row.setSpacing(_s(8))
        self.scan_dots = ScanningDotsLabel()
        self.scan_progress = QProgressBar()
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setTextVisible(False)
        self.scan_progress.setFixedHeight(_s(5))
        self.scan_progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: rgba(2,6,23,0.85);
                border: none;
                border-radius: {_s(3)}px;
            }}
            QProgressBar::chunk {{
                background-color: {COLORS['accent_cyan']};
                border-radius: {_s(3)}px;
            }}
        """)
        self.scan_progress.hide()
        scan_row.addWidget(self.scan_dots)
        scan_row.addWidget(self.scan_progress, stretch=1)
        device_lay.addLayout(scan_row)

        top_row.addWidget(device_frame, stretch=1)
        content_layout.addLayout(top_row)

        # СОЗДАТЬ ЗАГРУЗОЧНУЮ ФЛЕШКУ
        self.create_btn = QPushButton()
        self.create_btn.setMinimumHeight(_s(60))
        self.create_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {COLORS['accent_cyan']}, stop:0.5 {COLORS['accent_blue']},
                    stop:1 {COLORS['accent_purple']});
                color: #ffffff;
                border: 1px solid {_rgba(COLORS['accent_cyan'],0.55)};
                border-radius: 16px;
                font-size: {FONTS['card_title']}px; font-weight: 900; letter-spacing: 0.5px;
                padding: {_s(10)}px {_s(16)}px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #22eaff, stop:0.5 #3b82f6, stop:1 #8b5cf6);
            }}
            QPushButton:disabled {{
                background: rgba(30,40,55,0.9);
                color: {COLORS['text_muted']};
                border-color: {COLORS['border']};
            }}
        """)
        self._set_create_btn_normal()
        self.create_btn.clicked.connect(self.start_create)
        content_layout.addWidget(self.create_btn)

        self.build_steps = BuildStepsWidget()
        content_layout.addWidget(self.build_steps, stretch=1)

        # Footer stats
        footer = QHBoxLayout()
        footer.setSpacing(_s(8))
        footer_specs = (
            ("windows", "windows",   "WINDOWS",  "—", "#00ADEF"),
            ("linux",   "linux",     "LINUX",    "—", "#F97316"),
            ("winpe",   "winpe",     "WINPE",    "—", "#A855F7"),
            ("free",    "free_space","СВОБОДНО", "—",         COLORS["accent_cyan"]),
        )
        for key, icon_key, title, sub, color in footer_specs:
            card, sub_lbl = self._make_footer_card(icon_key, title, sub, color)
            self._footer_stats[key] = sub_lbl
            footer.addWidget(card, stretch=1)
        content_layout.addLayout(footer)

        shell_layout.addWidget(content_wrap, stretch=1)
        outer_layout.addWidget(shell)

        self.setStyleSheet(f"""
            QMainWindow {{ background: transparent; color: {COLORS['text_primary']}; }}
            QLabel {{
                {LABEL_PLAIN}
            }}
        """)
        self._set_active_nav(0)
        self._update_sidebar_usb_status()
        self._update_sidebar_system_status()
        self._update_header_status()

    def _set_create_btn_normal(self) -> None:
        px = Icons.pixmap("create_bootable", ICON_SIZE_BTN)
        self.create_btn.setIcon(QIcon(px))
        self.create_btn.setIconSize(QSize(ICON_SIZE_BTN, ICON_SIZE_BTN))
        self.create_btn.setText("  СОЗДАТЬ ЗАГРУЗОЧНУЮ ФЛЕШКУ  →")

    def _set_create_btn_created(self) -> None:
        px = Icons.pixmap("success", ICON_SIZE_BTN)
        self.create_btn.setIcon(QIcon(px))
        self.create_btn.setIconSize(QSize(ICON_SIZE_BTN, ICON_SIZE_BTN))
        self.create_btn.setText("  ФЛЕШКА УЖЕ СОЗДАНА")

    def _set_create_btn_busy(self) -> None:
        self.create_btn.setIcon(QIcon())
        self.create_btn.setText("  СОЗДАНИЕ ЗАГРУЗОЧНОЙ ФЛЕШКИ...  →")

    def _make_footer_card(self, icon_key: str, title: str, sub: str, color: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("footerCard")
        card.setMinimumHeight(_s(104))
        card.setStyleSheet(f"""
            QFrame#footerCard {{
                background-color: {COLORS['bg_card']};
                border: 1px solid {PANEL_BORDER};
                border-radius: {_s(14)}px;
            }}
            QFrame#footerCard QLabel {{
                {LABEL_PLAIN}
            }}
        """)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(_s(12), _s(12), _s(12), _s(12))
        lay.setSpacing(_s(10))
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(ICON_SIZE_FOOTER, ICON_SIZE_FOOTER)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setPixmap(Icons.pixmap(icon_key, ICON_SIZE_FOOTER))
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(f"{LABEL_PLAIN} color: {color}; font-size: {FONTS['card_title']}px; font-weight: 800;")
        s = QLabel(sub)
        s.setWordWrap(True)
        s.setStyleSheet(f"{LABEL_PLAIN} color: {COLORS['text_muted']}; font-size: {FONTS['body']}px;")
        text_col.addWidget(t)
        text_col.addWidget(s)
        lay.addWidget(icon_lbl)
        lay.addLayout(text_col)
        lay.addStretch()
        return card, s

    @staticmethod
    def _disk_fast_cicada(disk: UsbDisk | None) -> bool:
        return bool(disk and disk.fast_cicada_detected)

    @staticmethod
    def _disk_bad_layout(disk: UsbDisk | None) -> bool:
        return bool(disk and disk.cicada_bad_layout)

    def _merge_disk_scan_state(self, disk: UsbDisk, prev: UsbDisk | None) -> None:
        if prev is None:
            self._apply_flag_verified_cache_to_disk(disk)
            return
        if disk_identity_key(disk) != disk_identity_key(prev):
            self._apply_flag_verified_cache_to_disk(disk)
            return
        if prev.cicada_verified:
            disk.cicada_verified = True
        else:
            self._apply_flag_verified_cache_to_disk(disk)
        if disk.fast_is_cicada_signature and disk.fast_is_cicada_layout:
            disk.is_cicada = True
        elif (
            disk.fast_is_cicada_signature
            and not disk.fast_is_cicada_layout
            and prev.fast_is_cicada_layout
            and prev.is_cicada
        ):
            disk.fast_is_cicada_layout = True
            disk.is_cicada = True
            debug_log("[SCAN] preserved cicada layout from previous scan")

    def _apply_flag_verified_cache_to_disk(self, disk: UsbDisk | None) -> None:
        if disk is None or disk.cicada_verified:
            return
        identity = disk_identity_key(disk)
        if is_cicada_flag_verified_cached(identity):
            disk.cicada_verified = True
            debug_log(f"[FLAG] restored verified state from cache for {identity}")

    def _recompute_disk_ui_state(self, disk: UsbDisk | None) -> None:
        signature_ok = bool(disk and disk.fast_is_cicada_signature)
        layout_ok = bool(disk and disk.fast_is_cicada_layout)
        busy = self._ui_interaction_blocked()
        self.ready_for_view = signature_ok and layout_ok
        self.flag_verified = bool(disk and disk.cicada_verified)
        self.ready_for_actions = (
            self.ready_for_view
            and self.flag_verified
            and self.stats_loaded
            and not busy
        )
        self._log_ui_state_if_changed()

    def _log_ui_state_if_changed(self) -> None:
        state = (
            self.ready_for_view,
            self.flag_verified,
            self.stats_loaded,
            self.ready_for_actions,
        )
        if state == self._last_logged_ui_state:
            return
        self._last_logged_ui_state = state
        debug_log(f"[UI_STATE] ready_for_view={self.ready_for_view}")
        debug_log(f"[UI_STATE] flag_verified={self.flag_verified}")
        debug_log(f"[UI_STATE] stats_loaded={self.stats_loaded}")
        debug_log(f"[UI_STATE] ready_for_actions={self.ready_for_actions}")

    def _log_cicada_ui_state(self, disk: UsbDisk | None) -> None:
        fast = self._disk_fast_cicada(disk)
        signature_ok = bool(disk and disk.fast_is_cicada_signature)
        layout_ok = bool(disk and disk.fast_is_cicada_layout)
        create_offer = not fast and not self._disk_bad_layout(disk)
        debug_log(f"[UI] selected disk cicada fast = {str(fast).lower()}")
        debug_log(f"[UI] signature_ok = {str(signature_ok).lower()}")
        debug_log(f"[UI] layout_ok = {str(layout_ok).lower()}")
        debug_log(f"[UI] create button visible = {str(create_offer).lower()}")

    def _sync_selected_disk_identity(self, disk: UsbDisk | None) -> None:
        if disk is None:
            self.selected_disk_number = None
            self.selected_usb_unique_id = None
            self.selected_disk_id = None
            return
        self.selected_disk_number = disk.number
        self.selected_usb_unique_id = disk.unique_id
        self.selected_disk_id = disk_identity_key(disk)

    def _matches_selected_disk(
        self,
        disk_number: int,
        identity_key: str,
        request_id: int,
        *,
        unique_id: str | None = None,
    ) -> bool:
        if request_id != self.current_request_id:
            return False
        if self.selected_disk_number != disk_number:
            return False
        if self.selected_disk_id != identity_key:
            return False
        if (
            unique_id is not None
            and self.selected_usb_unique_id is not None
            and unique_id != self.selected_usb_unique_id
        ):
            return False
        return True

    def _is_stale_request(self, request_id: int) -> bool:
        return request_id != self.current_request_id

    def _disk_from_identity(self, identity_key: str) -> UsbDisk | None:
        for index in range(self.disk_combo.count()):
            item = self._combo_disk_at(index)
            if item is not None and disk_identity_key(item) == identity_key:
                return item
        return None

    def _cancel_stale_workers(self) -> None:
        had_flag = self._is_flag_auto_check_running()
        had_stats = (
            self._stats_cache_worker is not None
            and self._stats_cache_worker.isRunning()
        )
        self.current_request_id += 1
        if had_flag:
            debug_log("[WORKER] cancel stale FLAG request")
        if had_stats:
            debug_log("[WORKER] cancel stale STATS request")
        self._stop_flag_auto_worker(wait=False)
        self._stop_stats_cache_worker(wait=False)
        self._stop_stats_scan_worker(wait=False)

    def _stop_flag_auto_worker(self, *, wait: bool = False) -> None:
        worker = self._flag_auto_worker
        if worker is None:
            return
        if wait and worker.isRunning():
            worker.wait(120_000)
        if self._flag_auto_worker is worker:
            self._flag_auto_worker = None

    def _is_background_disk_probe_running(self) -> bool:
        if self._is_flag_auto_check_running():
            return True
        stats_worker = self._stats_cache_worker
        if stats_worker is not None and stats_worker.isRunning():
            return True
        scan_worker = self._stats_scan_worker
        return scan_worker is not None and scan_worker.isRunning()

    def _ui_interaction_blocked(self) -> bool:
        """Блокировка меню: только сканирование и явные операции, не фоновый FLAG/STATS."""
        if self._scanning:
            return True
        if self._is_ui_locked():
            return True
        if self._usb_scan_worker is not None and self._usb_scan_worker.isRunning():
            return True
        if self.worker is not None and self.worker.isRunning():
            return True
        if self._delete_list_worker is not None and self._delete_list_worker.isRunning():
            return True
        return False

    def _device_card_operation_busy(self) -> bool:
        return self._is_ui_locked() or bool(
            self.worker is not None and self.worker.isRunning()
        )

    def _reset_footer_stats_ui(self) -> None:
        self._apply_footer_stats(None, unknown=True)

    def update_ui_for_selected_disk(self) -> None:
        if self._scanning:
            return
        disk = self._selected_disk()
        self._sync_selected_disk_identity(disk)
        self._update_device_card()
        if disk is not None and disk.fast_cicada_detected:
            disk.is_cicada = True
        self.selected_disk_is_cicada = self._disk_fast_cicada(disk)
        card_busy = self._device_card_operation_busy()
        bad_layout = self._disk_bad_layout(disk)
        self._recompute_disk_ui_state(disk)
        if self.selected_disk_is_cicada:
            if not card_busy:
                self._set_create_btn_created()
            if self.ready_for_actions:
                self.device_ready.setText("✓ ГОТОВО К РАБОТЕ")
                self.device_ready.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_green']};"
                    f" font-size: {_s(11)}px; font-weight: 800;"
                )
            elif self.flag_verified and not self.stats_loaded:
                self.device_ready.setText("Флаг найден, идёт подсчёт...")
                self.device_ready.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_cyan']};"
                    f" font-size: {_s(11)}px; font-weight: 800;"
                )
            else:
                self.device_ready.setText("Проверка и подсчёт...")
                self.device_ready.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_cyan']};"
                    f" font-size: {_s(11)}px; font-weight: 800;"
                )
            if disk and disk.mbr_collision_offline:
                self.device_flag_status.setText(
                    CICADA_MBR_COLLISION_UI_MESSAGE_MULTILINE
                )
                self.device_flag_status.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_yellow']}; font-size: {_s(11)}px;"
                )
            elif disk and disk.cicada_verified:
                self.device_flag_status.setText("Флаг Cicada найден\nраздел защищён")
                self.device_flag_status.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_green']}; font-size: {_s(11)}px;"
                )
            elif self._is_flag_check_running_for_selected():
                self.device_flag_status.setText("Проверка...")
                self.device_flag_status.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_cyan']}; font-size: {_s(11)}px;"
                )
            else:
                sig_label = cicada_mbr_signature_display(
                    disk.signature if disk else None
                )
                self.device_flag_status.setText(
                    f"MBR Signature {sig_label}\nструктура разделов Cicada"
                )
                self.device_flag_status.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_green']}; font-size: {_s(11)}px;"
                )
            self.restore_flag_btn.hide()
            self.device_meta.setText("Режим управления образами")
        elif bad_layout:
            if not card_busy:
                self.create_btn.setText("  НЕКОРРЕКТНАЯ РАЗМЕТКА")
                self.device_ready.setText("Некорректная разметка Cicada USB Boot")
                self.device_ready.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_red']}; font-size: {_s(11)}px; font-weight: 800;"
                )
                self.device_meta.setText("Сначала удалите USB Boot")
                self.device_flag_status.setText(
                    "Некорректная разметка Cicada USB Boot"
                )
                self.device_flag_status.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_red']}; font-size: {_s(11)}px;"
                )
            self.restore_flag_btn.hide()
        else:
            if not card_busy:
                self._set_create_btn_normal()
                self.device_ready.setText("✓ ГОТОВО К РАБОТЕ")
                self.device_ready.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['accent_green']}; font-size: {_s(11)}px; font-weight: 800;"
                )
                self.device_meta.setText("USB 2.0  •  MBR")
                self.device_flag_status.setText("Cicada USB Boot не обнаружен")
                self.device_flag_status.setStyleSheet(
                    f"{LABEL_PLAIN} color: {COLORS['text_muted']}; font-size: {_s(11)}px;"
                )
            self.restore_flag_btn.hide()
        self._log_cicada_ui_state(disk)
        self._update_sidebar_usb_status()
        self._update_header_status()
        self._update_action_buttons_state()

    def _update_cicada_ui_state(self) -> None:
        self.update_ui_for_selected_disk()

    def _flash_actions_enabled(self) -> bool:
        return self.ready_for_actions

    def _update_sidebar_counters(
        self,
        stats: dict[str, float | int] | None = None,
        *,
        unknown: bool = False,
    ) -> None:
        for btn in self._nav_buttons:
            key = btn.counter_key
            if not key:
                continue
            if stats is None or unknown:
                btn.set_count(0)
            else:
                btn.set_count(int(stats.get(key, 0)))

    def _update_sidebar_usb_status(self) -> None:
        if not self._sidebar_usb_lines:
            return
        disk = self._selected_disk()
        boot = self._sidebar_usb_lines.get("boot")
        protected = self._sidebar_usb_lines.get("protected")
        mbr = self._sidebar_usb_lines.get("mbr")
        if boot is None or protected is None or mbr is None:
            return
        if disk is None:
            boot.setText("CICADA USB BOOT")
            boot.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['text_muted']};"
                f" font-size: {_s(10)}px; font-weight: 700;"
            )
            protected.setText("Protected Mode —")
            protected.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['text_muted']};"
                f" font-size: {_s(10)}px; font-weight: 700;"
            )
            mbr.setText("MBR Signature —")
            mbr.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['text_muted']};"
                f" font-size: {_s(10)}px; font-weight: 700;"
            )
            return
        if self.selected_disk_is_cicada:
            boot.setText("CICADA USB BOOT")
            boot.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['accent_green']};"
                f" font-size: {_s(10)}px; font-weight: 800;"
            )
        else:
            boot.setText("CICADA USB BOOT —")
            boot.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['text_muted']};"
                f" font-size: {_s(10)}px; font-weight: 700;"
            )
        if disk.cicada_verified:
            protected.setText("Protected Mode")
            protected.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['accent_cyan']};"
                f" font-size: {_s(10)}px; font-weight: 800;"
            )
        elif self.selected_disk_is_cicada:
            protected.setText("Protected Mode · sync")
            protected.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['accent_yellow']};"
                f" font-size: {_s(10)}px; font-weight: 700;"
            )
        else:
            protected.setText("Protected Mode —")
            protected.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['text_muted']};"
                f" font-size: {_s(10)}px; font-weight: 700;"
            )
        if disk.fast_is_cicada_signature:
            mbr.setText("MBR Signature OK")
            mbr.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['accent_green']};"
                f" font-size: {_s(10)}px; font-weight: 800;"
            )
        else:
            mbr.setText("MBR Signature —")
            mbr.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['text_muted']};"
                f" font-size: {_s(10)}px; font-weight: 700;"
            )

    def _set_header_status_label(self, key: str, text: str, color: str, *, active: bool) -> None:
        lbl = self._header_status.get(key)
        if lbl is None:
            return
        lbl.setText(text)
        tone = color if active else _rgba(color, 0.32)
        lbl.setStyleSheet(
            f"{LABEL_PLAIN} color: {tone}; font-size: 10px; font-weight: 800;"
            f" letter-spacing: 0.15px; margin: 0; padding: 0;"
        )

    def _update_header_status(self) -> None:
        if not self._header_status:
            return
        busy = self._ui_interaction_blocked()
        disk = self._selected_disk()
        if busy:
            self._set_header_status_label("ready", "● BUSY", "#FBBF24", active=True)
        else:
            self._set_header_status_label("ready", "● READY", "#00ff99", active=True)
        if disk is not None:
            self._set_header_status_label("usb", "● USB DETECTED", "#00d9ff", active=True)
        else:
            self._set_header_status_label("usb", "● USB —", "#00d9ff", active=False)
        if disk is not None and disk.cicada_verified:
            self._set_header_status_label("protected", "● PROTECTED", "#b36bff", active=True)
        elif disk is not None and self.selected_disk_is_cicada:
            self._set_header_status_label(
                "protected", "● PROTECTED · SYNC", "#b36bff", active=True
            )
        else:
            self._set_header_status_label("protected", "● PROTECTED —", "#b36bff", active=False)

    def _update_sidebar_system_status(self) -> None:
        if self._sidebar_system_status is None:
            return
        if self._scanning:
            self._sidebar_system_status.setText("● SCANNING")
            self._sidebar_system_status.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['accent_cyan']};"
                f" font-size: {_s(10)}px; font-weight: 800; letter-spacing: 0.5px;"
            )
        elif self._is_ui_locked():
            self._sidebar_system_status.setText("● SYSTEM BUSY")
            self._sidebar_system_status.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['accent_yellow']};"
                f" font-size: {_s(10)}px; font-weight: 800; letter-spacing: 0.5px;"
            )
        else:
            self._sidebar_system_status.setText("● SYSTEM READY")
            self._sidebar_system_status.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['accent_green']};"
                f" font-size: {_s(10)}px; font-weight: 800; letter-spacing: 0.5px;"
            )

    def _update_action_buttons_state(self) -> None:
        disk = self._selected_disk()
        self._recompute_disk_ui_state(disk)
        busy = self._ui_interaction_blocked()
        locked = self._is_ui_locked()
        signature_ok = bool(disk and disk.fast_is_cicada_signature)
        layout_ok = bool(disk and disk.fast_is_cicada_layout)
        bad_layout = self._disk_bad_layout(disk)

        for btn in self._nav_buttons:
            action = btn.property("nav_action")
            if action not in NAV_FLASH_ACTIONS:
                enabled = False
            elif action == "delete_image":
                enabled = self.ready_for_actions and self._image_count > 0
            elif action == "delete_cicada":
                enabled = self.ready_for_actions or (
                    not busy and disk is not None and bad_layout
                )
            else:
                enabled = self.ready_for_actions
            btn.setEnabled(enabled)
            btn.setCursor(
                Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
            )

        if locked or self._scanning:
            self.create_btn.setEnabled(False)
        elif bad_layout:
            self.create_btn.setEnabled(False)
        elif self.ready_for_view:
            self.create_btn.setEnabled(False)
        elif disk is None:
            self.create_btn.setEnabled(False)
        else:
            self.create_btn.setEnabled(
                (not signature_ok or not layout_ok) and not busy
            )

        self.refresh_btn.setEnabled(not locked and not self._scanning)

        self._refresh_nav_styles()
        self._update_sidebar_system_status()
        self._update_header_status()

    def _update_action_availability(self) -> None:
        self._update_action_buttons_state()

    def _on_disk_index_changed(self, index: int = -1) -> None:
        if index < 0:
            index = self.disk_combo.currentIndex()
        if index == self._last_selected_disk_index:
            return
        self._last_selected_disk_index = index
        self._on_disk_changed()

    def _snapshot_combo_disks(self) -> dict[str, UsbDisk]:
        disks: dict[str, UsbDisk] = {}
        for index in range(self.disk_combo.count()):
            item = self._combo_disk_at(index)
            if item is not None:
                disks[disk_identity_key(item)] = item
        return disks

    def _combo_disk_at(self, index: int) -> UsbDisk | None:
        if index < 0:
            return None
        item = self.disk_combo.itemData(index, Qt.ItemDataRole.UserRole)
        return item if isinstance(item, UsbDisk) else None

    def _add_disk_combo_item(self, disk: UsbDisk) -> None:
        index = self.disk_combo.count()
        self.disk_combo.addItem("")
        self.disk_combo.setItemData(index, disk, Qt.ItemDataRole.UserRole)

    def _invalidate_stats_cache_for_disk(self, disk: UsbDisk) -> None:
        invalidate_disk_stats_cache(
            disk.number,
            unique_id=disk.unique_id,
            model=disk.model,
            size_bytes=disk.size_bytes,
        )

    def _apply_cached_footer_stats_if_any(self, disk: UsbDisk) -> None:
        if not self._disk_fast_cicada(disk):
            return
        stats = get_validated_partition_stats(disk)
        if stats is not None:
            self.stats_loaded = True
            self._apply_footer_stats(stats)

    def _retry_disk_pipeline(self) -> None:
        disk = self._selected_disk()
        if disk is None:
            return
        if self._scanning or self._is_ui_locked():
            self._pipeline_retry_timer.start(400)
            return
        if self._is_partition_lock_busy():
            self._pipeline_retry_timer.start(400)
            return
        self._start_disk_pipeline(disk)

    def _start_disk_pipeline(self, disk: UsbDisk) -> None:
        if self._scanning or self._is_ui_locked():
            return
        if not self._matches_selected_disk(
            disk.number,
            disk_identity_key(disk),
            self.current_request_id,
            unique_id=disk.unique_id,
        ):
            return
        request_id = self.current_request_id
        self._apply_flag_verified_cache_to_disk(disk)
        if self._disk_fast_cicada(disk) and not disk.cicada_verified:
            self._start_flag_worker(disk, request_id)
        if self._disk_fast_cicada(disk):
            self._start_stats_worker(disk, request_id)

    def _schedule_disk_pipeline(self, disk: UsbDisk) -> None:
        QTimer.singleShot(0, lambda: self._start_disk_pipeline(disk))

    def _on_disk_changed(self, *, refresh_stats: bool = True) -> None:
        self._pipeline_retry_timer.stop()
        old_number = self.selected_disk_number
        self._cancel_stale_workers()
        self.disk_selector.sync_display()
        disk = self._selected_disk()
        self._apply_flag_verified_cache_to_disk(disk)
        self._sync_selected_disk_identity(disk)
        self.stats_loaded = False
        self._last_logged_ui_state = None
        debug_log(
            f"[SELECT] disk changed old={old_number} new={self.selected_disk_number}"
        )
        self._reset_footer_stats_ui()
        self.update_ui_for_selected_disk()
        if refresh_stats and disk is not None:
            self._apply_cached_footer_stats_if_any(disk)
            self._schedule_disk_pipeline(disk)

    def _update_device_card(self) -> None:
        disk = self._selected_disk()
        if disk is None:
            self.device_name.setText("USB не выбран")
            self.device_size.setText("—")
            return
        self.device_name.setText(disk.model)
        self.device_size.setText(f"{disk.size_gb:.0f} ГБ")

    def _apply_footer_stats(
        self,
        stats: dict[str, float | int] | None,
        *,
        unknown: bool = False,
    ) -> None:
        if stats is None or unknown:
            self.stats_loaded = False
            self._footer_stats["windows"].setText("—")
            self._footer_stats["linux"].setText("—")
            self._footer_stats["winpe"].setText("—")
            self._footer_stats["free"].setText("—")
            self._image_count = 0
            self._update_sidebar_counters(None, unknown=True)
        else:
            self.stats_loaded = True
            for key in ("windows", "linux", "winpe"):
                n = int(stats[key])
                self._footer_stats[key].setText(f"{n} образ" if n == 1 else f"{n} образов")
            self._footer_stats["free"].setText(
                f"{stats['free_gb']:.1f} ГБ из {stats['total_gb']:.0f} ГБ"
            )
            self._image_count = sum(int(stats[k]) for k in ("windows", "linux", "winpe"))
            self._update_sidebar_counters(stats)
        self.update_ui_for_selected_disk()

    def _set_footer_stats_counting(self) -> None:
        self.stats_loaded = False
        for key in ("windows", "linux", "winpe", "free"):
            self._footer_stats[key].setText("Подсчёт...")
        self._update_sidebar_counters(None, unknown=True)
        self.update_ui_for_selected_disk()

    def _request_stats_force_refresh(self, disk_number: int) -> None:
        """Полный пересчёт с NTFS (mount + rescan)."""
        disk = self._flag_disk_from_combo(disk_number)
        if disk is not None:
            self._invalidate_stats_cache_for_disk(disk)
        else:
            invalidate_disk_stats_cache(disk_number)
        self._set_footer_stats_counting()
        self._start_stats_scan(disk_number=disk_number, force=True)

    def _apply_stats_after_import(
        self,
        disk: UsbDisk,
        category: str,
        size_bytes: int,
        dest_name: str,
        *,
        subfolder: str | None = None,
    ) -> None:
        debug_log("[STATS] incremental update after import")
        stats = apply_disk_cache_after_import(
            disk,
            category,
            size_bytes,
            dest_name=dest_name,
            subfolder=subfolder,
        )
        if stats is not None:
            self._apply_footer_stats(stats)
            debug_log("[STATS] UI cards updated (incremental)")
            return
        debug_log("[STATS] cache missing/damaged, full refresh required")
        self._request_stats_force_refresh(disk.number)

    def _apply_stats_after_delete(self, disk: UsbDisk, entry: ImageEntry) -> None:
        debug_log("[STATS] incremental update after delete")
        stats = apply_disk_cache_after_delete(disk, entry)
        if stats is not None:
            self._apply_footer_stats(stats)
            debug_log("[STATS] UI cards updated (incremental)")
            return
        debug_log("[STATS] cache missing/damaged, full refresh required")
        self._request_stats_force_refresh(disk.number)

    def _is_partition_lock_busy(self) -> bool:
        acquired = self._partition_lock.acquire(blocking=False)
        if acquired:
            self._partition_lock.release()
            return False
        return True

    def _acquire_partition_lock(self, owner: str) -> None:
        self._partition_lock.acquire()
        debug_log(f"[LOCK] acquired by {owner}")

    def _release_partition_lock(self, owner: str) -> None:
        debug_log(f"[TRACE] before lock release ({owner})")
        debug_log(f"[LOCK] released by {owner}")
        self._partition_lock.release()
        debug_log(f"[TRACE] after lock release ({owner})")

    def _start_stats_scan(
        self,
        disk_number: int | None = None,
        *,
        force: bool = False,
    ) -> None:
        disk = self._selected_disk()
        if disk is None:
            return
        if disk_number is not None and disk.number != disk_number:
            for index in range(self.disk_combo.count()):
                item = self.disk_combo.itemData(index)
                if isinstance(item, UsbDisk) and item.number == disk_number:
                    disk = item
                    break
        if self._is_partition_lock_busy():
            debug_log("[STATS] refresh deferred: partition lock busy")
            target_number = disk.number
            QTimer.singleShot(
                1500,
                lambda: self._start_stats_scan(disk_number=target_number, force=force),
            )
            return
        if force:
            debug_log("[STATS] force scan started")
            self._stop_stats_cache_worker(wait=False)
            self._stop_stats_scan_worker(wait=False)
            self._stats_scan_id += 1
            scan_id = self._stats_scan_id
            self._set_footer_stats_counting()
            self._acquire_partition_lock("STATS")
            worker = StatsScanWorker(disk, scan_id, self)
            self._stats_scan_worker = worker
            worker.finished.connect(self._on_stats_scan_finished)
            worker.error.connect(self._on_stats_scan_error)
            worker.finished.connect(worker.deleteLater)
            worker.error.connect(worker.deleteLater)
            worker.start()
            return
        self._start_stats_cache_load(disk)

    def _stop_stats_scan_worker(self, *, wait: bool = False) -> None:
        self._stats_scan_id += 1
        worker = self._stats_scan_worker
        if worker is None:
            return
        if wait and worker.isRunning():
            worker.wait(120_000)
        if self._stats_scan_worker is worker:
            self._stats_scan_worker = None

    def _on_stats_scan_finished(self, result: StatsScanResult) -> None:
        worker = self.sender()
        if not isinstance(worker, StatsScanWorker):
            return
        if result.scan_id != self._stats_scan_id:
            debug_log("[STATS] ignoring stale scan result")
            self._release_partition_lock("STATS")
            return
        self._stats_scan_worker = None
        self._release_partition_lock("STATS")
        debug_log("[STATS] force scan finished")
        disk = self._selected_disk()
        if (
            disk is None
            or disk_identity_key(disk) != result.disk_identity_key
        ):
            return
        self.stats_loaded = True
        self._apply_footer_stats(result.stats)
        debug_log("[STATS] UI cards updated")

    def _on_stats_scan_error(self, disk_number: int, scan_id: int, message: str) -> None:
        worker = self.sender()
        if not isinstance(worker, StatsScanWorker):
            return
        if scan_id != self._stats_scan_id:
            debug_log("[STATS] ignoring stale scan error")
            self._release_partition_lock("STATS")
            return
        self._stats_scan_worker = None
        self._release_partition_lock("STATS")
        disk = self._selected_disk()
        if disk is None or disk.number != disk_number:
            return
        self.append_log(f"Статистика образов: {message}", "warn")
        self.stats_loaded = False
        CicadaDialog.inform(
            self,
            "Статистика",
            "Не удалось прочитать статистику образов",
            kind="warning",
        )
        self._apply_footer_stats(None, unknown=True)

    def _stop_stats_cache_worker(self, *, wait: bool = False) -> None:
        worker = self._stats_cache_worker
        if worker is None:
            return
        if wait and worker.isRunning():
            worker.wait(5_000)
        if self._stats_cache_worker is worker:
            self._stats_cache_worker = None

    def _start_stats_worker(self, disk: UsbDisk, request_id: int) -> bool:
        if not self._matches_selected_disk(
            disk.number,
            disk_identity_key(disk),
            request_id,
            unique_id=disk.unique_id,
        ):
            return False
        if self._is_flag_check_running_for_selected():
            self._pipeline_retry_timer.start(400)
            return False
        if self._is_partition_lock_busy():
            self._pipeline_retry_timer.start(400)
            return False
        worker = self._stats_cache_worker
        if worker is not None and worker.isRunning():
            if (
                worker.request_id == request_id
                and worker.target_identity_key == disk_identity_key(disk)
            ):
                return True
            self._stop_stats_cache_worker(wait=False)
        stats = get_validated_partition_stats(disk)
        if stats is not None:
            self.stats_loaded = True
            self._apply_footer_stats(stats)
            return True
        self._set_footer_stats_counting()
        self._acquire_partition_lock("STATS")
        worker = StatsCacheWorker(
            disk,
            request_id,
            force_refresh=False,
            parent=self,
        )
        self._stats_cache_worker = worker
        worker.finished.connect(self._on_stats_cache_finished)
        worker.error.connect(self._on_stats_cache_error)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        worker.start()
        return True

    def _start_stats_cache_load(
        self, disk: UsbDisk, *, force_refresh: bool = False
    ) -> None:
        self._start_stats_worker(disk, self.current_request_id)

    def _on_stats_cache_finished(self, result: StatsCacheResult) -> None:
        worker = self.sender()
        if not isinstance(worker, StatsCacheWorker):
            return
        self._stats_cache_worker = None
        self._release_partition_lock("STATS")
        if self._is_stale_request(result.request_id):
            debug_log(
                f"[WORKER] ignore stale STATS result disk={result.disk_number}"
            )
            return
        if not self._matches_selected_disk(
            result.disk_number,
            result.disk_identity_key,
            result.request_id,
            unique_id=result.target_unique_id,
        ):
            debug_log(
                f"[WORKER] ignore stale STATS result disk={result.disk_number}"
            )
            return
        self.stats_loaded = True
        self._apply_footer_stats(result.stats)

    def _on_stats_cache_error(self, disk_number: int, message: str) -> None:
        worker = self.sender()
        if not isinstance(worker, StatsCacheWorker):
            return
        self._stats_cache_worker = None
        self._release_partition_lock("STATS")
        if worker.request_id != self.current_request_id:
            debug_log(
                f"[WORKER] ignore stale STATS result disk={disk_number}"
            )
            return
        if not self._matches_selected_disk(
            worker.target_disk_number,
            worker.target_identity_key,
            worker.request_id,
            unique_id=worker.target_unique_id,
        ):
            debug_log(
                f"[WORKER] ignore stale STATS result disk={disk_number}"
            )
            return
        if is_fast_mount_diagnostic_error(message):
            debug_log(f"[STATS] {message}")
            self.stats_loaded = False
            self._apply_footer_stats(None, unknown=True)
            return
        if "not supported" in message.lower():
            disable_fast_mount_for_device(worker.target_identity_key)
        self.append_log(f"Статистика образов: {message}", "warn")
        self.stats_loaded = False
        CicadaDialog.inform(
            self,
            "Статистика",
            "Не удалось прочитать статистику образов",
            kind="warning",
        )
        self._apply_footer_stats(None, unknown=True)

    def _update_footer_stats(self, *, load_mount: bool = False) -> None:
        if self._scanning or self._is_ui_locked():
            return
        disk = self._selected_disk()
        if disk is None or not self._disk_fast_cicada(disk):
            self._apply_footer_stats(None, unknown=True)
            return
        stats = get_validated_partition_stats(disk)
        if stats is not None:
            self._apply_footer_stats(stats)
            return
        if load_mount:
            self._start_stats_worker(disk, self.current_request_id)
        else:
            self._apply_footer_stats(None, unknown=True)

    def _is_flag_auto_check_running(self) -> bool:
        worker = self._flag_auto_worker
        return worker is not None and worker.isRunning()

    def _is_flag_check_running_for_selected(self) -> bool:
        worker = self._flag_auto_worker
        if worker is None or not worker.isRunning():
            return False
        disk = self._selected_disk()
        if disk is None:
            return False
        return (
            worker.request_id == self.current_request_id
            and worker.target_identity_key == disk_identity_key(disk)
            and worker.target_unique_id == disk.unique_id
        )

    def _wait_for_flag_auto_check(self) -> None:
        worker = self._flag_auto_worker
        if worker is not None and worker.isRunning():
            worker.wait(120_000)

    def _start_flag_worker(self, disk: UsbDisk, request_id: int) -> bool:
        if not self._disk_fast_cicada(disk) or disk.cicada_verified:
            return False
        identity = disk_identity_key(disk)
        if is_cicada_flag_verified_cached(identity):
            disk.cicada_verified = True
            debug_log(f"[FLAG] skip mount, restored from cache for {identity}")
            self.update_ui_for_selected_disk()
            return False
        if not self._matches_selected_disk(
            disk.number,
            disk_identity_key(disk),
            request_id,
            unique_id=disk.unique_id,
        ):
            return False
        if self._is_flag_auto_check_running():
            self._pipeline_retry_timer.start(400)
            return False
        if self._is_partition_lock_busy():
            self._pipeline_retry_timer.start(400)
            return False
        self._acquire_partition_lock("FLAG")
        worker = FlagAutoCheckWorker(
            disk.number,
            disk.unique_id,
            disk_identity_key(disk),
            request_id,
            self,
        )
        self._flag_auto_worker = worker
        worker.finished_ok.connect(self._on_flag_auto_check_ok)
        worker.finished_err.connect(self._on_flag_auto_check_err)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self.update_ui_for_selected_disk()
        return True

    def _start_auto_flag_check(self, disk: UsbDisk, *, refresh_stats: bool) -> bool:
        del refresh_stats
        return self._start_flag_worker(disk, self.current_request_id)

    def _flag_disk_from_combo(self, disk_number: int) -> UsbDisk | None:
        for index in range(self.disk_combo.count()):
            item = self._combo_disk_at(index)
            if item is not None and item.number == disk_number:
                return item
        return None

    def _apply_flag_verified(self, result: FlagAutoCheckResult) -> None:
        disk = self._disk_from_identity(result.disk_identity_key)
        if disk is None:
            return
        disk.cicada_verified = True
        mark_cicada_flag_verified_cached(result.disk_identity_key)
        if result.was_restored:
            self.append_log("Флаг Cicada восстановлен автоматически", "ok")
        else:
            self.append_log("Флаг Cicada найден", "ok")

    def _on_flag_auto_check_ok(self, result: FlagAutoCheckResult) -> None:
        worker = self.sender()
        if not isinstance(worker, FlagAutoCheckWorker):
            return
        self._flag_auto_worker = None
        self._release_partition_lock("FLAG")
        if self._is_stale_request(result.request_id):
            debug_log(
                f"[WORKER] ignore stale FLAG result disk={result.disk_number}"
            )
            return
        if not self._matches_selected_disk(
            result.disk_number,
            result.disk_identity_key,
            result.request_id,
            unique_id=result.target_unique_id,
        ):
            debug_log(
                f"[WORKER] ignore stale FLAG result disk={result.disk_number}"
            )
            return
        self._apply_flag_verified(result)
        self.update_ui_for_selected_disk()
        disk = self._selected_disk()
        if disk is not None:
            self._apply_cached_footer_stats_if_any(disk)
            if get_validated_partition_stats(disk) is None:
                self._start_stats_worker(disk, result.request_id)

    def _on_flag_auto_check_err(self, disk_number: int, message: str) -> None:
        worker = self.sender()
        if not isinstance(worker, FlagAutoCheckWorker):
            return
        self._flag_auto_worker = None
        self._release_partition_lock("FLAG")
        if worker.request_id != self.current_request_id:
            debug_log(
                f"[WORKER] ignore stale FLAG result disk={disk_number}"
            )
            return
        if not self._matches_selected_disk(
            worker.disk_number,
            worker.target_identity_key,
            worker.request_id,
            unique_id=worker.target_unique_id,
        ):
            debug_log(
                f"[WORKER] ignore stale FLAG result disk={disk_number}"
            )
            return
        if is_fast_mount_diagnostic_error(message):
            debug_log(f"[FLAG] {message}")
            return
        if "not supported" in message.lower():
            disable_fast_mount_for_device(worker.target_identity_key)
        self.append_log(f"Ошибка проверки флага: {message}", "err")
        self.update_ui_for_selected_disk()
        disk = self._selected_disk()
        if disk is not None:
            self._start_stats_worker(disk, worker.request_id)

    def _ensure_flag_for_write(self, disk: UsbDisk) -> bool:
        if not self._disk_fast_cicada(disk):
            return False
        if disk.cicada_verified:
            return True
        self._wait_for_flag_auto_check()
        if disk.cicada_verified:
            return True
        try:
            verified = verify_cicada_usb_flag(
                disk.number,
                device_key=disk_identity_key(disk),
            )
        except Exception as exc:
            summary = exception_log_summary(exc, classify_exception(exc))
            self.append_log(summary, "err")
            CicadaDialog.inform(
                self,
                "Cicada USB не подтверждён",
                format_user_error_message(str(exc)),
                kind="warning",
            )
            return False
        if verified:
            disk.cicada_verified = True
            mark_cicada_flag_verified_cached(disk_identity_key(disk))
            self.update_ui_for_selected_disk()
            return True
        return self._restore_cicada_flag(disk, silent=True)

    def _restore_cicada_flag(self, disk: UsbDisk, *, silent: bool = False) -> bool:
        if not self._disk_fast_cicada(disk):
            return False
        self._wait_for_flag_auto_check()
        if self.worker and self.worker.isRunning():
            self._show_busy_dialog()
            return False
        if self._is_ui_locked() or self._is_partition_lock_busy():
            if not silent:
                self._show_busy_dialog()
            return False
        self._set_app_state(AppState.DELETING)
        self._set_status("Восстановление флага...", ok=False)
        self._acquire_partition_lock("FLAG")
        worker = FlagRestoreWorker(disk.number, self)
        self.worker = worker
        restored = {"ok": False}
        loop = QEventLoop()

        def _finish() -> None:
            self._release_partition_lock("FLAG")
            self.worker = None
            loop.quit()

        def _on_ok() -> None:
            restored["ok"] = True
            disk.cicada_verified = True
            mark_cicada_flag_verified_cached(disk_identity_key(disk))
            disk.is_cicada = True
            self._set_app_state(AppState.IDLE)
            self._set_status("Готово к работе", ok=True)
            self._update_cicada_ui_state()
            if not silent:
                CicadaDialog.inform(self, "Готово", "Флаг Cicada восстановлен.", kind="info")
            else:
                self.append_log("Флаг Cicada восстановлен автоматически", "ok")
            _finish()

        def _on_err(message: str) -> None:
            self._set_app_state(AppState.IDLE)
            self._set_status("Ошибка", ok=False)
            CicadaDialog.inform(
                self,
                "Ошибка",
                format_user_error_message(message),
                kind="error",
            )
            _finish()

        worker.finished_ok.connect(_on_ok)
        worker.finished_err.connect(_on_err)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        loop.exec()
        return restored["ok"]

    def _restore_cicada_flag_clicked(self) -> None:
        if not self._guard_not_busy():
            return
        disk = self._selected_disk()
        if disk is None or not self._disk_fast_cicada(disk):
            return
        self._restore_cicada_flag(disk)

    def _is_ui_locked(self) -> bool:
        return self._app_state in _BUSY_STATES

    def _show_busy_dialog(self) -> None:
        CicadaDialog.inform(
            self,
            "Занято",
            "Дождитесь завершения текущей операции.",
            kind="warning",
        )

    def _guard_not_busy(self) -> bool:
        if self._operation_running():
            self._show_busy_dialog()
            return False
        return True

    def _set_app_state(self, state: AppState) -> None:
        prev = self._app_state
        self._app_state = state
        debug_log(f"[STATE] {prev.value} -> {state.value}")
        if state in _BUSY_STATES:
            debug_log("[STATE] busy set true")
        elif state == AppState.IDLE:
            debug_log("[STATE] busy set false")
        locked = self._is_ui_locked()
        if not self._scanning:
            self.disk_combo.setEnabled(not locked)
            self.disk_selector.setEnabled(not locked)
        self._update_sidebar_system_status()
        self._update_header_status()
        self._update_cicada_ui_state()
        self._update_action_buttons_state()

    def _enter_scanning_state(self, *, initial: bool = False) -> None:
        self._scanning = True
        self._app_state = AppState.SCANNING
        self.refresh_btn.start_loading()
        self.disk_combo.setEnabled(False)
        self.disk_selector.setEnabled(False)
        scan_title = (
            "Сканирование USB-накопителей..."
            if initial
            else "Сканирование..."
        )
        self.disk_selector.set_scanning(scan_title, "Подождите")
        self.device_name.setText("—")
        self.device_size.setText("—")
        self.device_ready.setText("СКАНИРОВАНИЕ USB...")
        self.device_ready.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['accent_cyan']};"
            f" font-size: {_s(11)}px; font-weight: 800;"
        )
        self.device_meta.setText("Поиск подключённых устройств")
        self.device_flag_status.setText("Проверка флага Cicada и разделов")
        self.device_flag_status.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['text_muted']}; font-size: {_s(11)}px;"
        )
        dots_text = (
            "Сканирование USB-накопителей"
            if initial
            else "Сканирование"
        )
        self.scan_dots.start(dots_text)
        self.scan_progress.show()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._update_sidebar_system_status()
        self._update_header_status()
        self._update_action_buttons_state()

    def _leave_scanning_state(self) -> None:
        self._scanning = False
        if self._app_state == AppState.SCANNING:
            self._set_app_state(AppState.IDLE)
        self.scan_dots.stop()
        self.scan_progress.hide()
        if QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        self.refresh_btn.stop_loading()
        if not self._is_ui_locked():
            self.disk_combo.setEnabled(True)
            self.disk_selector.setEnabled(True)
        self._update_sidebar_system_status()
        self._update_header_status()
        self._update_action_buttons_state()

    def _close_import_dialog(self) -> None:
        if self._import_dialog is not None:
            self._import_dialog.close()
            self._import_dialog.deleteLater()
            self._import_dialog = None

    def _finish_image_import(self) -> None:
        debug_log("[TRACE] before progress dialog close")
        self._close_import_dialog()
        debug_log("[TRACE] after progress dialog close")
        self._set_app_state(AppState.IDLE)
        self.worker = None
        self._update_cicada_ui_state()

    def _start_image_import(
        self,
        source: Path,
        category: str,
        *,
        subfolder: str | None = None,
    ) -> None:
        disk = self._selected_disk()
        if disk is None or not self._disk_fast_cicada(disk):
            CicadaDialog.inform(
                self,
                "Ошибка",
                "Cicada USB Boot не обнаружен.\nСначала создайте загрузочную флешку.",
                kind="warning",
            )
            return
        if not self._ensure_flag_for_write(disk):
            return
        disk.is_cicada = True
        self._stop_stats_cache_worker(wait=True)
        self._stop_stats_scan_worker(wait=True)
        debug_log("[STATE] start import")
        debug_log(
            f"[IMPORT] image selected: {source.name} "
            f"({source.stat().st_size // (1024 * 1024)} MB)"
        )
        category_label = IMPORT_CATEGORY_LABELS.get(category, category)
        self._set_app_state(AppState.IMPORTING)
        self._set_status("Добавление образа...", ok=False)
        self._acquire_partition_lock("IMPORT")

        dialog = ImageImportProgressDialog(self, source.name, category_label)
        self._import_dialog = dialog
        debug_log("[IMPORT] dialog opened")
        dialog.show()
        QApplication.processEvents()

        worker = ImageImportWorker(disk, source, category, subfolder=subfolder)
        self.worker = worker
        worker.progress_changed.connect(dialog.set_progress)
        worker.stage_changed.connect(dialog.set_stage)
        worker.bytes_changed.connect(dialog.set_bytes_progress)
        worker.finished_ok.connect(lambda dest_name: self._on_image_import_ok(dest_name, disk))
        worker.finished_err.connect(lambda message: self._on_image_import_err(message))
        worker.import_cancelled.connect(self._on_image_import_cancelled)
        dialog.cancel_requested.connect(self._on_image_import_cancel_requested)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda: self._release_partition_lock("IMPORT"))
        worker.start()

    def _on_image_import_cancel_requested(self) -> None:
        debug_log("[IMPORT] cancel requested")
        worker = self.worker
        if isinstance(worker, ImageImportWorker) and worker.isRunning():
            worker.request_cancel()

    def _on_image_import_ok(self, dest_name: str, disk: UsbDisk) -> None:
        debug_log("[TRACE] finished_ok slot entered")
        mount_warning = None
        worker = self.worker
        if isinstance(worker, ImageImportWorker):
            mount_warning = worker.mount_close_warning
        QApplication.processEvents()
        self._finish_image_import()
        disk.cicada_verified = True
        mark_cicada_flag_verified_cached(disk_identity_key(disk))
        self.append_log(f"Добавление образа: {dest_name} — успешно", "ok")
        self._set_status("Готово к работе", ok=True)
        if isinstance(worker, ImageImportWorker):
            self._apply_stats_after_import(
                disk,
                worker.category,
                worker.source.stat().st_size,
                dest_name,
                subfolder=worker.subfolder,
            )
        if mount_warning:
            CicadaDialog.inform(
                self,
                "Готово",
                f"Образ успешно добавлен.\n\n{mount_warning}",
                kind="warning",
            )
        else:
            CicadaDialog.inform(self, "Готово", "Образ успешно добавлен", kind="info")
        debug_log("[TRACE] finished_ok slot finished")

    def _on_image_import_err(self, message: str) -> None:
        if self._import_dialog is not None:
            self._import_dialog.set_error()
            QApplication.processEvents()
        self._finish_image_import()
        error = _coerce_exception(message)
        classified = classify_exception(error)
        log_exception(error, classified.code)
        self.append_log(exception_log_summary(error, classified), "err")
        self._set_status("Ошибка", ok=False)
        CicadaDialog.inform(
            self,
            classified.title,
            classified.message,
            kind="error",
        )

    def _on_image_import_cancelled(self) -> None:
        debug_log("[TRACE] import_cancelled slot entered")
        if self._import_dialog is not None:
            self._import_dialog.set_cancelled()
            QApplication.processEvents()
        self._finish_image_import()
        self.append_log("Добавление образа отменено", "warn")
        self._set_status("Готово к работе", ok=True)
        CicadaDialog.inform(
            self,
            "Отменено",
            "Добавление образа отменено",
            kind="info",
        )
        debug_log("[TRACE] import_cancelled slot finished")

    def _pick_image_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите образ", "",
            "Образы (*.iso *.img *.wim *.esd *.vhd *.vhdx);;Все файлы (*.*)",
        )
        return Path(path) if path else None

    def _add_windows_image(self) -> None:
        if not self._guard_not_busy():
            return
        subfolder = WindowsVariantPickerDialog.pick(self)
        if subfolder is None:
            return
        source = self._pick_image_for_import()
        if source is None:
            return
        self._start_image_import(source, "WINDOWS", subfolder=subfolder)

    def _add_linux_image(self) -> None:
        if not self._guard_not_busy():
            return
        subfolder = LinuxVariantPickerDialog.pick(self)
        if subfolder is None:
            return
        source = self._pick_image_for_import()
        if source is None:
            return
        self._start_image_import(source, "LINUX", subfolder=subfolder)

    def _add_winpe_image(self) -> None:
        if not self._guard_not_busy():
            return
        source = self._pick_image_for_import()
        if source is None:
            return
        self._start_image_import(source, "WINPE")

    def _operation_running(self) -> bool:
        if self._ui_interaction_blocked():
            return True
        if self._is_partition_lock_busy():
            return True
        return False

    def _pick_image_for_import(self) -> Path | None:
        debug_log(f"[STATE] before pick image: {self._app_state.value}")
        self._app_state = AppState.PICKING_FILE
        source = self._pick_image_file()
        debug_log(
            f"[STATE] after pick image: {source.name if source else 'none'}"
        )
        if source is None:
            self._set_app_state(AppState.IDLE)
            self._set_status("Готово к работе", ok=True)
        else:
            self._app_state = AppState.IDLE
        return source

    def _delete_image(self) -> None:
        if not self._guard_not_busy():
            return
        disk = self._selected_disk()
        if disk is None or not self._disk_fast_cicada(disk):
            CicadaDialog.inform(
                self,
                "Ошибка",
                "Cicada USB Boot не обнаружен.\nСначала создайте загрузочную флешку.",
                kind="warning",
            )
            return
        if not self._ensure_flag_for_write(disk):
            return
        stats = get_validated_partition_stats(disk)

        if stats is not None and sum(
            int(stats[k]) for k in ("windows", "linux", "winpe")
        ) == 0:
            CicadaDialog.inform(
                self,
                "Удаление образов",
                "На флешке нет образов для удаления.",
                kind="info",
            )
            return

        self._stop_stats_cache_worker(wait=True)
        self._stop_stats_scan_worker(wait=True)
        self._set_app_state(AppState.DELETING)
        self._set_status("Поиск образов...", ok=False)
        self._acquire_partition_lock("DELETE")
        worker = ImageDeleteWorker(disk)
        self._delete_list_worker = worker
        worker.list_finished.connect(self._on_delete_list_finished)
        worker.finished_err.connect(self._on_delete_list_error)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_delete_list_finished(self, entries: list) -> None:
        self._delete_list_worker = None
        self._release_partition_lock("DELETE")
        self._set_app_state(AppState.IDLE)
        disk = self._selected_disk()
        if disk is not None:
            disk.cicada_verified = True
            mark_cicada_flag_verified_cached(disk_identity_key(disk))
            self._update_cicada_ui_state()
        if not entries:
            self._set_status("Готово к работе", ok=True)
            CicadaDialog.inform(
                self,
                "Удаление образов",
                "На флешке нет образов для удаления.",
                kind="info",
            )
            return
        self._set_status("Готово к работе", ok=True)
        dialog = DeleteImagesDialog(
            self,
            entries,
            on_delete_requested=self._start_image_delete,
        )
        dialog.exec()

    def _on_delete_list_error(self, exc: object) -> None:
        self._delete_list_worker = None
        self._release_partition_lock("DELETE")
        self._set_app_state(AppState.IDLE)
        error = _coerce_exception(exc)  # type: ignore[arg-type]
        classified = classify_exception(error)
        self.append_log(exception_log_summary(error, classified), "err")
        self._set_status("Ошибка", ok=False)
        handle_exception(self, error)

    def _start_image_delete(self, entry: ImageEntry, dialog: DeleteImagesDialog) -> None:
        if not self._guard_not_busy():
            return
        disk = self._selected_disk()
        if disk is None or not self._disk_fast_cicada(disk):
            return
        disk.is_cicada = True
        self._pending_delete_entry = entry
        self._pending_delete_dialog = dialog
        self._set_app_state(AppState.DELETING)
        self._set_status("Удаление образа...", ok=False)
        self._acquire_partition_lock("DELETE")
        worker = ImageDeleteWorker(disk, entry=entry)
        self.worker = worker
        worker.delete_progress.connect(dialog.set_delete_stage)
        worker.delete_finished.connect(self._on_image_delete_ok)
        worker.delete_error.connect(self._on_image_delete_err)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda: self._release_partition_lock("DELETE"))
        worker.start()

    def _on_image_delete_ok(self) -> None:
        entry = self._pending_delete_entry
        dialog = self._pending_delete_dialog
        deleted_name = entry.name if entry is not None else "образ"
        disk = self._selected_disk()
        if disk is not None:
            disk.cicada_verified = True
        if dialog is not None:
            dialog.set_delete_stage("Обновление статистики")
        self.worker = None
        self._set_app_state(AppState.IDLE)
        if entry is not None and dialog is not None:
            dialog.set_delete_stage("Готово")
            dialog.remove_entry(entry)
        self._pending_delete_entry = None
        self._pending_delete_dialog = None
        self.append_log(f"Образ удалён: {deleted_name}", "ok")
        self._set_status("Готово к работе", ok=True)
        self._update_cicada_ui_state()
        if disk is not None:
            stats = get_validated_partition_stats(disk)
            if stats is not None:
                self._apply_footer_stats(stats)
                debug_log("[STATS] UI cards updated (incremental)")
            elif entry is not None:
                self._apply_stats_after_delete(disk, entry)
        CicadaDialog.inform(self, "Готово", "Образ успешно удалён", kind="info")

    def _on_image_delete_err(self, message: str) -> None:
        entry = self._pending_delete_entry
        dialog = self._pending_delete_dialog
        error = _coerce_exception(message)
        classified = classify_exception(error)
        log_exception(error, classified.code)
        self.append_log(exception_log_summary(error, classified), "err")
        self._set_status("Ошибка", ok=False)
        self.worker = None
        if dialog is not None:
            dialog.end_delete_error()
        if handle_exception(self, error, allow_retry=True) and entry is not None:
            disk = self._selected_disk()
            self._pending_delete_entry = None
            self._pending_delete_dialog = None
            self._set_app_state(AppState.IDLE)
            if disk is not None and dialog is not None:
                if entry is not None:
                    dialog.begin_delete(entry)
                self._start_image_delete(entry, dialog)
            return
        self._pending_delete_entry = None
        self._pending_delete_dialog = None
        self._set_app_state(AppState.IDLE)

    def _delete_cicada_usb(self, *, skip_confirm: bool = False) -> None:
        if not self._guard_not_busy():
            return
        disk = self._selected_disk()
        if disk is None or not self._disk_fast_cicada(disk):
            CicadaDialog.inform(
                self,
                "Нет Cicada USB",
                "На выбранном диске не обнаружен Cicada USB Boot.",
                kind="warning",
            )
            return
        if not skip_confirm and not CicadaDialog.ask(
            self,
            "Удалить USB BOOT",
            "Это полностью удалит структуру Cicada USB Boot с выбранной флешки.\n"
            "Все разделы будут удалены.\n"
            "Флешка будет очищена и создан один обычный раздел.\n\n"
            "Продолжить?",
            danger=True,
        ):
            return
        if not self._ensure_flag_for_write(disk):
            return
        self._set_app_state(AppState.DELETING)
        self._set_progress(0)
        self.build_steps.set_step(1)
        self._set_status("Удаление Cicada USB Boot...", ok=False)
        self.worker = DeleteCicadaWorker(disk)
        self.worker.log.connect(lambda t: self.append_log(t, "info"))
        self.worker.progress.connect(self._set_progress)
        self.worker.finished_ok.connect(self._on_delete_cicada_success)
        self.worker.finished_err.connect(self._on_delete_cicada_error)
        self.worker.start()

    def _on_delete_cicada_success(self) -> None:
        self._set_app_state(AppState.IDLE)
        self._set_progress(100)
        self.build_steps.set_step(6)
        self._set_status("Флешка возвращена в обычный режим", ok=True)
        self.append_log("Cicada USB Boot удалён", "ok")
        disk = self._selected_disk()
        if disk is not None:
            clear_cached_partition_stats(
                disk.number,
                unique_id=disk.unique_id,
                model=disk.model,
                size_bytes=disk.size_bytes,
            )
            clear_cicada_flag_verified_cache(disk_identity_key(disk))
        invalidate_usb_scan_cache()
        self.refresh_disks(initial=False)
        self._reset_build_progress()

    def _on_delete_cicada_error(self, exc: object) -> None:
        self._set_app_state(AppState.IDLE)
        self._set_status("Ошибка", ok=False)
        error = _coerce_exception(exc)  # type: ignore[arg-type]
        classified = classify_exception(error)
        self.append_log(exception_log_summary(error, classified), "err")
        if handle_exception(self, error, allow_retry=True):
            self._delete_cicada_usb(skip_confirm=True)
            return
        self._reset_build_progress()
        self._update_cicada_ui_state()

    def _show_about(self) -> None:
        AboutDialog(self).exec()

    def _on_logging_toggled(self, enabled: bool) -> None:
        set_logging_enabled(enabled)
        state = "включено" if enabled else "выключено"
        if enabled:
            debug_log(f"[UI] Логирование {state}")

    def append_log(self, text: str, level: str = "info") -> None:
        if not is_logging_enabled():
            return
        prefix = {
            "ok": "OK",
            "err": "ERR",
            "warn": "WARN",
            "info": "INFO",
        }.get(level, "INFO")
        debug_log(f"[UI][{prefix}] {text}")

    def _set_status(self, text: str, ok: bool = True) -> None:
        color = COLORS["accent_green"] if ok else COLORS["accent_red"]
        if ok:
            self.device_ready.setText("✓ ГОТОВО К РАБОТЕ")
        elif "..." in text or "Выполня" in text:
            self.device_ready.setText(f"⏳ {text.strip().upper()}")
        else:
            self.device_ready.setText("✕ ОШИБКА")
        self.device_ready.setStyleSheet(f"color: {color}; font-size: {FONTS['small']}px; font-weight: 800;")

    def _set_progress(self, value: int) -> None:
        self.build_steps.set_progress(value)

    def _reset_build_progress(self) -> None:
        self._set_progress(0)
        self.build_steps.set_step(0)

    def refresh_disks(self, initial: bool = False) -> None:
        """Быстрое сканирование USB в фоне. Без доступа к разделам и без проверки флага."""
        if self._usb_scan_worker and self._usb_scan_worker.isRunning():
            return
        if not self._scanning:
            self._enter_scanning_state(initial=initial)
        if not initial:
            self.append_log("Сканирование дисков...", "info")
        prev = self._selected_disk()
        prev_number = prev.number if prev is not None else None
        prev_by_number = self._snapshot_combo_disks()

        worker = UsbScanWorker(self)
        self._usb_scan_worker = worker
        worker.finished.connect(
            lambda result: self._on_usb_scan_finished(
                result, initial, prev_number, prev, prev_by_number
            )
        )
        worker.error.connect(self._on_usb_scan_error)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        worker.start()

    def _on_usb_scan_finished(
        self,
        result: UsbScanResult,
        initial: bool,
        prev_number: int | None,
        prev_disk: UsbDisk | None,
        prev_by_number: dict[str, UsbDisk] | None = None,
    ) -> None:
        self._usb_scan_worker = None
        disks = result.disks
        known_disks = prev_by_number or {}
        prev_identity = (
            disk_identity_key(prev_disk) if prev_disk is not None else None
        )
        try:
            self.disk_combo.blockSignals(True)
            try:
                self.disk_combo.clear()
                select_index = -1
                for index, disk in enumerate(disks):
                    identity = disk_identity_key(disk)
                    prev_match = known_disks.get(identity)
                    if prev_match is None and prev_identity == identity and prev_disk:
                        prev_match = prev_disk
                    self._merge_disk_scan_state(disk, prev_match)
                    self._add_disk_combo_item(disk)
                    if prev_identity is not None and identity == prev_identity:
                        select_index = index
                    elif prev_number is not None and disk.number == prev_number:
                        select_index = index
                    if disk.is_cicada and not initial:
                        self.append_log(
                            f"Cicada USB Boot обнаружен на диске {disk.number}",
                            "ok",
                        )
                    if disk.mbr_collision_offline and not initial:
                        self.append_log(
                            CICADA_MBR_COLLISION_UI_MESSAGE,
                            "warn",
                        )
                if disks:
                    new_index = select_index if select_index >= 0 else 0
                    self._last_selected_disk_index = -1
                    self.disk_combo.setCurrentIndex(new_index)
                    self._last_selected_disk_index = new_index
                    self.disk_selector.sync_display()
                    self.build_steps.set_step(0)
                else:
                    self.disk_selector.set_no_disks()
            finally:
                self.disk_combo.blockSignals(False)
            cicada_count = sum(1 for d in disks if d.is_cicada)
            if not initial:
                self.append_log(
                    f"Найдено дисков: {len(disks)} (Cicada: {cicada_count})",
                    "ok" if disks else "warn",
                )
        except Exception as exc:
            handle_exception(self, exc)
            self.device_ready.setText("Ошибка сканирования")
            self.device_ready.setStyleSheet(
                f"{LABEL_PLAIN} color: {COLORS['accent_red']};"
                f" font-size: {_s(11)}px; font-weight: 800;"
            )
        finally:
            self._leave_scanning_state()
            self._on_disk_changed(refresh_stats=initial)
            if not initial:
                disk = self._selected_disk()
                if disk is not None and self._disk_fast_cicada(disk):
                    self._request_stats_force_refresh(disk.number)
            if not disks:
                self._update_action_buttons_state()

    def _on_usb_scan_error(self, message: str) -> None:
        self._usb_scan_worker = None
        short = format_user_error_message(message)
        CicadaDialog.inform(
            self,
            "Ошибка сканирования",
            f"Не удалось получить список USB-накопителей.\n\n{short}",
            kind="error",
        )
        self.append_log(f"Ошибка сканирования: {message}", "err")
        self.device_ready.setText("Ошибка сканирования")
        self.device_ready.setStyleSheet(
            f"{LABEL_PLAIN} color: {COLORS['accent_red']};"
            f" font-size: {_s(11)}px; font-weight: 800;"
        )
        self.disk_selector.set_no_disks()
        self._leave_scanning_state()

    def _selected_disk(self):
        combo = getattr(self, "disk_combo", None)
        if combo is None:
            return None
        return self._combo_disk_at(combo.currentIndex())

    def start_create(self, *, skip_confirm: bool = False) -> None:
        if self.worker and self.worker.isRunning():
            return
        disk = self._selected_disk()
        if disk is None:
            CicadaDialog.inform(self, "Ошибка", "Выберите USB-диск", kind="warning")
            return
        if self._disk_fast_cicada(disk):
            CicadaDialog.inform(
                self,
                "Флешка уже создана",
                "На выбранном диске уже есть Cicada USB Boot.",
                kind="info",
            )
            return
        if self._disk_bad_layout(disk):
            CicadaDialog.inform(
                self,
                "Некорректная разметка",
                "На диске найдена сигнатура Cicada, но разметка повреждена.\n"
                "Сначала удалите USB Boot.",
                kind="warning",
            )
            return
        if not skip_confirm and not CreateUsbBootConfirmDialog.confirm(self, disk):
            return
        self._set_progress(0)
        self.build_steps.set_step(0)
        self._set_status("Выполняется...", ok=False)
        self._set_app_state(AppState.CREATING)
        self._set_create_btn_busy()
        assets_dir, download_missing = resolve_assets_dir()
        self.worker = CreateWorker(
            disk, assets_dir, download_missing=download_missing
        )
        self.worker.log.connect(lambda t: self.append_log(t, "info"))
        self.worker.progress.connect(self._set_progress)
        self.worker.step_changed.connect(self.build_steps.set_step)
        self.worker.finished_ok.connect(self._on_success)
        self.worker.finished_err.connect(self._on_error)
        self.worker.start()

    def _on_success(self) -> None:
        self._set_app_state(AppState.IDLE)
        self.build_steps.set_step(6)
        self.build_steps.set_progress(100)
        self._set_status("Готово!", ok=True)
        self.append_log("Загрузочная флешка успешно создана!", "ok")
        invalidate_usb_scan_cache()
        self.refresh_disks(initial=False)
        CicadaDialog.inform(
            self, "Готово",
            "Загрузочная флешка создана!\n\nБезопасно извлеките USB.",
            kind="info",
        )
        self._reset_build_progress()

    def _on_error(self, exc: object) -> None:
        self._set_app_state(AppState.IDLE)
        self._set_status("Ошибка", ok=False)
        error = _coerce_exception(exc)  # type: ignore[arg-type]
        classified = classify_exception(error)
        self.append_log(exception_log_summary(error, classified), "err")
        if handle_exception(self, error, allow_retry=True):
            self.start_create(skip_confirm=True)
            return
        self._reset_build_progress()
        self._update_cicada_ui_state()


# ═══════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════

def _write_crash_stdout(exc_type, exc, tb) -> None:
    import traceback

    from cicada_usb_tool import cicada_temp_dir, ensure_runtime_dir

    path = cicada_temp_dir() / "crash_stdout.txt"
    try:
        ensure_runtime_dir(path.parent)
        path.write_text(
            "".join(traceback.format_exception(exc_type, exc, tb)),
            encoding="utf-8",
        )
    except OSError:
        pass


def _install_unhandled_exception_logger() -> None:
    def _hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        _write_crash_stdout(exc_type, exc, tb)
        log_exception(exc, "CICADA-000")
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook


def main() -> int:
    hide_console_window()
    setup_windows_app_id()
    migrate_runtime_files_to_temp()
    _install_unhandled_exception_logger()
    if is_logging_enabled():
        debug_log(f"[APP] temp dir:\n{cicada_temp_dir()}")

    if not is_admin():
        app = QApplication(sys.argv)
        app.setApplicationName(APP_TITLE)
        icon = load_app_icon()
        if icon is not None:
            app.setWindowIcon(icon)
        require_admin_startup()
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setFont(QFont("Segoe UI", FONTS["body"]))
    icon = load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["bg_primary"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text_primary"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["bg_card"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text_primary"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["accent_purple"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)

    window = CicadaUsbTool()
    window.show()
    apply_windows_taskbar_icon(window)
    QTimer.singleShot(0, lambda: apply_windows_taskbar_icon(window))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
