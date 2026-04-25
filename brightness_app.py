from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import winreg
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import win32api
import win32con

try:
    from PIL import Image, ImageDraw
    import pystray
except Exception:
    Image = None
    ImageDraw = None
    pystray = None


APP_TITLE = "Brightness Control"
APP_VERSION = "1.0.0"
LOG_DIR = Path.home() / "AppData" / "Local" / "BrightnessControl"
LOG_FILE = LOG_DIR / "app.log"
STATE_FILE = LOG_DIR / "state.json"
STATE_BACKUP_FILE = LOG_DIR / "state.json.bak"
MIN_BRIGHTNESS = 50
SLIDER_THROTTLE_MS = 25
CARD_REVEAL_STAGGER_MS = 55
RUN_AT_STARTUP_VALUE_NAME = "ExternalMonitorBrightness"
HOTKEY_ID_BRIGHTER = 1
HOTKEY_ID_DIMMER = 2
HOTKEY_POLL_MS = 120
APP_WINDOW_TITLE = "External Monitor Brightness"
SINGLE_INSTANCE_MUTEX = "ExternalMonitorBrightness_SingleInstance_v1"
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9

LIGHT_THEME = {
    "root_bg": "#eef2f7",
    "hero_bg": "#ffffff",
    "hero_border": "#d9e3ef",
    "hero_title_fg": "#0f172a",
    "hero_sub_fg": "#475569",
    "hero_check_fg": "#0f172a",
    "hero_check_disabled": "#94a3b8",
    "card_bg": "#ffffff",
    "card_border": "#dbe4ef",
    "card_title_fg": "#0f172a",
    "card_meta_fg": "#64748b",
    "value_fg": "#2563eb",
    "error_fg": "#b91c1c",
    "badge_bg": "#2563eb",
    "badge_fg": "#ffffff",
    "bar_bg": "#2563eb",
    "bar_trough": "#dbe3ef",
    "accent_bg": "#2563eb",
    "accent_active": "#1d4ed8",
    "secondary_bg": "#e2e8f0",
    "secondary_active": "#d4dce8",
    "secondary_fg": "#0f172a",
    "header_primary_bg": "#ffffff",
    "header_primary_active": "#e9f1fb",
    "header_primary_fg": "#0f172a",
    "header_secondary_bg": "#eff6ff",
    "header_secondary_active": "#dbeafe",
    "header_secondary_fg": "#1d4ed8",
    "footer_bg": "#ffffff",
    "footer_fg": "#475569",
    "scale_bg": "#ffffff",
    "scale_trough": "#dbe3ef",
    "scale_active": "#2563eb",
}

DARK_THEME = {
    "root_bg": "#111827",
    "hero_bg": "#171f2f",
    "hero_border": "#233048",
    "hero_title_fg": "#f8fafc",
    "hero_sub_fg": "#a8b7cc",
    "hero_check_fg": "#e2e8f0",
    "hero_check_disabled": "#6b7280",
    "card_bg": "#1b2434",
    "card_border": "#2b3648",
    "card_title_fg": "#f8fafc",
    "card_meta_fg": "#a5b4c7",
    "value_fg": "#60a5fa",
    "error_fg": "#fca5a5",
    "badge_bg": "#3b82f6",
    "badge_fg": "#ffffff",
    "bar_bg": "#3b82f6",
    "bar_trough": "#293243",
    "accent_bg": "#3b82f6",
    "accent_active": "#60a5fa",
    "secondary_bg": "#243044",
    "secondary_active": "#2d3a51",
    "secondary_fg": "#e5e7eb",
    "header_primary_bg": "#dbeafe",
    "header_primary_active": "#bfdbfe",
    "header_primary_fg": "#0f172a",
    "header_secondary_bg": "#26324a",
    "header_secondary_active": "#334463",
    "header_secondary_fg": "#e5eefb",
    "footer_bg": "#131a28",
    "footer_fg": "#b3c1d6",
    "scale_bg": "#1b2434",
    "scale_trough": "#2b3648",
    "scale_active": "#3b82f6",
}


def normalize_state_payload(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    values = raw.get("values") if isinstance(raw.get("values"), dict) else raw
    if not isinstance(values, dict):
        return {}

    cleaned: dict[str, int] = {}
    for device, value in values.items():
        try:
            cleaned[str(device)] = max(MIN_BRIGHTNESS, min(100, int(value)))
        except (TypeError, ValueError):
            continue
    return cleaned


def normalize_settings_payload(raw: object) -> dict[str, bool]:
    defaults = {"hotkeys_enabled": False, "dark_mode_enabled": False}
    if not isinstance(raw, dict):
        return defaults
    incoming = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    return {
        "hotkeys_enabled": bool(incoming.get("hotkeys_enabled", defaults["hotkeys_enabled"])),
        "dark_mode_enabled": bool(incoming.get("dark_mode_enabled", defaults["dark_mode_enabled"])),
    }


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    def handle_exception(exc_type, exc, tb) -> None:
        logging.error("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = handle_exception


@dataclass(frozen=True)
class Display:
    index: int
    device: str
    rect: tuple[int, int, int, int]
    primary: bool

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    @property
    def title(self) -> str:
        return "Primary display" if self.primary else f"Display {self.index + 1}"

    @property
    def subtitle(self) -> str:
        left, top, _right, _bottom = self.rect
        if left < 0:
            position = "left of primary"
        elif left > 0:
            position = "right of primary"
        elif top < 0:
            position = "above primary"
        elif top > 0:
            position = "below primary"
        else:
            position = "main area"
        return f"{self.width} x {self.height}  |  {position}"


class GammaDimmer:
    def __init__(self) -> None:
        self.gdi32 = ctypes.windll.gdi32
        self.user32 = ctypes.windll.user32
        self.values: dict[str, int] = {}
        self.handles: dict[str, int] = {}
        self.ramps: dict[int, object] = {}

    def set_brightness(self, display: Display, value: int) -> bool:
        value = max(MIN_BRIGHTNESS, min(100, int(value)))
        self.values[display.device] = value
        logging.debug("Set brightness requested: %s -> %s", display.device, value)

        hdc = self._get_dc(display.device)
        if not hdc:
            logging.error("CreateDC failed for %s", display.device)
            return False

        try:
            ramp = self._ramp_for_value(value)
            ok = bool(self.gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)))
            if not ok:
                logging.warning("SetDeviceGammaRamp failed for %s at %s", display.device, value)
            return ok
        except Exception:
            logging.error("Gamma update failed for %s\n%s", display.device, traceback.format_exc())
            return False

    def get_value(self, display: Display) -> int:
        return self.values.get(display.device, 100)

    def restore_all(self) -> None:
        normal = self._ramp_for_value(100)
        for device in list(self.handles):
            hdc = self._get_dc(device)
            if not hdc:
                continue
            self.gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(normal))
            logging.info("Restored normal gamma for %s", device)
        self.values.clear()

    def close(self) -> None:
        self.restore_all()
        for hdc in self.handles.values():
            self.gdi32.DeleteDC(hdc)
        self.handles.clear()

    def _create_dc(self, device: str) -> int:
        return self.gdi32.CreateDCW("DISPLAY", device, None, None)

    def _get_dc(self, device: str) -> int:
        if device not in self.handles:
            self.handles[device] = self._create_dc(device)
        return self.handles[device]

    @staticmethod
    def _ramp_type():
        return (ctypes.c_ushort * 256) * 3

    def _build_ramp(self, value: int):
        ramp = self._ramp_type()()
        factor = value / 100
        for channel in range(3):
            for i in range(256):
                ramp[channel][i] = min(65535, int(i * 257 * factor))
        return ramp

    def _ramp_for_value(self, value: int):
        if value not in self.ramps:
            self.ramps[value] = self._build_ramp(value)
        return self.ramps[value]


class DisplayCard(ttk.Frame):
    def __init__(self, parent, display: Display, value: int, on_change, on_identify, theme: dict[str, str]) -> None:
        super().__init__(parent, style="Card.TFrame", padding=16)
        self.display = display
        self.on_change = on_change
        self.on_identify = on_identify
        self._commit_after_id: str | None = None
        self._queued_value = value
        self._last_commit_at = 0.0

        self.columnconfigure(1, weight=1)

        badge = ttk.Label(self, text=str(display.index + 1), style="Badge.TLabel", anchor="center")
        badge.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 14))

        ttk.Label(self, text=display.title, style="CardTitle.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(self, text=display.subtitle, style="CardMeta.TLabel").grid(row=1, column=1, sticky="w", pady=(3, 0))

        self.value_label = ttk.Label(self, text=f"{value}%", style="Value.TLabel")
        self.value_label.grid(row=0, column=2, rowspan=2, sticky="e", padx=(12, 0))

        self.slider = tk.Scale(
            self,
            from_=MIN_BRIGHTNESS,
            to=100,
            orient="horizontal",
            resolution=1,
            showvalue=False,
            highlightthickness=0,
            relief="flat",
            bd=0,
            bg=theme["scale_bg"],
            troughcolor=theme["scale_trough"],
            activebackground=theme["scale_active"],
            command=self._changed,
        )
        self.slider.set(value)
        self.slider.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.slider.bind("<ButtonRelease-1>", lambda _event: self._flush_commit())

        self.level_bar = ttk.Progressbar(
            self,
            style="Level.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
            maximum=100,
            value=value,
        )
        self.level_bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        footer = ttk.Frame(self, style="Card.TFrame")
        footer.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        footer.columnconfigure(1, weight=1)
        ttk.Label(footer, text=f"Dim ({MIN_BRIGHTNESS}%)", style="CardMeta.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(footer, text="Normal (100%)", style="CardMeta.TLabel").grid(row=0, column=2, sticky="e")

        actions = ttk.Frame(self, style="Card.TFrame")
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, style="Secondary.TButton", text="Identify", command=lambda: on_identify(display)).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(actions, style="Accent.TButton", text="Reset", command=self.reset).grid(row=0, column=1, sticky="e")

        self.feedback_label = ttk.Label(self, text="", style="Error.TLabel")
        self.feedback_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def reset(self) -> None:
        self.slider.set(100)
        self._changed("100")

    def _changed(self, raw_value: str) -> None:
        value = int(float(raw_value))
        self._queued_value = value
        self.value_label.config(text=f"{value}%")
        self.level_bar["value"] = value
        now = time.monotonic() * 1000
        elapsed = now - self._last_commit_at

        if elapsed >= SLIDER_THROTTLE_MS:
            if self._commit_after_id:
                self.after_cancel(self._commit_after_id)
                self._commit_after_id = None
            self._commit(value)
            return

        if not self._commit_after_id:
            delay = max(1, int(SLIDER_THROTTLE_MS - elapsed))
            self._commit_after_id = self.after(delay, self._flush_commit)

    def _flush_commit(self) -> None:
        if self._commit_after_id:
            self.after_cancel(self._commit_after_id)
            self._commit_after_id = None
        self._commit(self._queued_value)

    def _commit(self, value: int) -> None:
        self._last_commit_at = time.monotonic() * 1000
        ok = self.on_change(self.display, value)
        if not ok:
            self.value_label.config(text="failed")
            self.feedback_label.config(text="Could not apply this level. Try Refresh.")
            return
        self.value_label.config(text=f"{value}%")
        self.feedback_label.config(text="")


class BrightnessApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        logging.info("Starting app")
        self.title(APP_WINDOW_TITLE)
        self.geometry("840x580")
        self.minsize(680, 460)
        self.dimmer = GammaDimmer()
        self.cards: list[DisplayCard] = []
        self.saved_values, self.settings = self._load_state_bundle()
        self.hotkeys_enabled = tk.BooleanVar(value=bool(self.settings.get("hotkeys_enabled", False)))
        self.dark_mode_enabled = tk.BooleanVar(value=bool(self.settings.get("dark_mode_enabled", False)))
        self.theme = DARK_THEME if self.dark_mode_enabled.get() else LIGHT_THEME
        self.startup_enabled = tk.BooleanVar(value=self._is_startup_enabled())
        self.empty_label: ttk.Label | None = None
        self.tray_icon = None
        self._tray_thread: threading.Thread | None = None
        self._hotkeys_registered = False
        self._scrollbar_visible = False

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self.bind("<Unmap>", self._on_window_state_change)
        self.bind("<Map>", self._on_window_state_change)
        self.refresh()
        self.after(500, self._start_tray)
        self.after(700, self._apply_hotkey_registration)
        self.after(HOTKEY_POLL_MS, self._poll_hotkeys)

    def _style(self) -> None:
        self.theme = DARK_THEME if self.dark_mode_enabled.get() else LIGHT_THEME
        self.configure(background=self.theme["root_bg"])
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        title_font = ("Segoe UI Variable Display", 22, "bold")
        body_font = ("Segoe UI", 10)
        style.configure("Root.TFrame", background=self.theme["root_bg"])
        style.configure("Hero.TFrame", background=self.theme["hero_bg"])
        style.configure("Footer.TFrame", background=self.theme["footer_bg"])
        style.configure("HeroTitle.TLabel", background=self.theme["hero_bg"], foreground=self.theme["hero_title_fg"], font=title_font)
        style.configure("HeroSub.TLabel", background=self.theme["hero_bg"], foreground=self.theme["hero_sub_fg"], font=body_font)
        style.configure("FooterText.TLabel", background=self.theme["footer_bg"], foreground=self.theme["footer_fg"], font=("Segoe UI", 9))
        style.configure("Card.TFrame", background=self.theme["card_bg"])
        style.configure("CardShell.TFrame", background=self.theme["card_bg"])
        style.configure("CardTitle.TLabel", background=self.theme["card_bg"], foreground=self.theme["card_title_fg"], font=("Segoe UI Semibold", 13))
        style.configure("CardMeta.TLabel", background=self.theme["card_bg"], foreground=self.theme["card_meta_fg"], font=("Segoe UI", 9))
        style.configure("Value.TLabel", background=self.theme["card_bg"], foreground=self.theme["value_fg"], font=("Segoe UI Variable Display", 19, "bold"))
        style.configure("Error.TLabel", background=self.theme["card_bg"], foreground=self.theme["error_fg"], font=("Segoe UI", 9, "bold"))
        style.configure("HeroCheck.TCheckbutton", background=self.theme["hero_bg"], foreground=self.theme["hero_check_fg"], font=("Segoe UI", 9))
        style.map(
            "HeroCheck.TCheckbutton",
            background=[("active", self.theme["hero_bg"])],
            foreground=[("disabled", self.theme["hero_check_disabled"])],
        )
        style.configure("Badge.TLabel", background=self.theme["badge_bg"], foreground=self.theme["badge_fg"], font=("Segoe UI", 12, "bold"))
        style.configure("Level.Horizontal.TProgressbar", background=self.theme["bar_bg"], troughcolor=self.theme["bar_trough"], borderwidth=0)
        style.configure(
            "Accent.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(12, 6),
            background=self.theme["accent_bg"],
            foreground=self.theme["badge_fg"],
            borderwidth=0,
        )
        style.map("Accent.TButton", background=[("active", self.theme["accent_active"])])
        style.configure(
            "Secondary.TButton",
            font=("Segoe UI", 10),
            padding=(12, 6),
            background=self.theme["secondary_bg"],
            foreground=self.theme["secondary_fg"],
            borderwidth=0,
        )
        style.map("Secondary.TButton", background=[("active", self.theme["secondary_active"])])
        style.configure(
            "PrimaryHeader.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(14, 7),
            background=self.theme["header_primary_bg"],
            foreground=self.theme["header_primary_fg"],
            borderwidth=0,
        )
        style.map("PrimaryHeader.TButton", background=[("active", self.theme["header_primary_active"])])
        style.configure(
            "SecondaryHeader.TButton",
            font=("Segoe UI", 10),
            padding=(14, 7),
            background=self.theme["header_secondary_bg"],
            foreground=self.theme["header_secondary_fg"],
            borderwidth=0,
        )
        style.map("SecondaryHeader.TButton", background=[("active", self.theme["header_secondary_active"])])
        style.configure("Vertical.TScrollbar", background=self.theme["secondary_bg"], troughcolor=self.theme["root_bg"], borderwidth=0, arrowcolor=self.theme["secondary_fg"])
        if hasattr(self, "canvas"):
            self.canvas.configure(background=self.theme["root_bg"])

    def _build(self) -> None:
        root = ttk.Frame(self, style="Root.TFrame", padding=0)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        hero = ttk.Frame(root, style="Hero.TFrame", padding=(20, 16, 20, 16))
        hero.grid(row=0, column=0, sticky="ew")
        hero.columnconfigure(0, weight=1)
        ttk.Label(hero, text=APP_TITLE, style="HeroTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(hero, style="PrimaryHeader.TButton", text="Refresh", command=self.refresh).grid(
            row=0, column=1, sticky="e", padx=(10, 0)
        )
        ttk.Button(hero, style="SecondaryHeader.TButton", text="Reset All", command=self.reset_all).grid(
            row=1, column=1, sticky="e", padx=(10, 0), pady=(6, 0)
        )
        controls = ttk.Frame(hero, style="Hero.TFrame")
        controls.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            controls,
            text="Run at startup",
            variable=self.startup_enabled,
            command=self._toggle_startup,
            style="HeroCheck.TCheckbutton",
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            controls,
            text="Hotkeys (Ctrl+Alt+Up/Down)",
            variable=self.hotkeys_enabled,
            command=self._toggle_hotkeys,
            style="HeroCheck.TCheckbutton",
        ).grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Checkbutton(
            controls,
            text="Dark theme",
            variable=self.dark_mode_enabled,
            command=self._toggle_theme,
            style="HeroCheck.TCheckbutton",
        ).grid(row=0, column=2, sticky="w", padx=(16, 0))
        ttk.Button(
            controls,
            style="SecondaryHeader.TButton",
            text="About / Help",
            command=self._show_about,
        ).grid(row=0, column=3, sticky="w", padx=(16, 0))

        content = ttk.Frame(root, style="Root.TFrame", padding=(12, 10, 12, 6))
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(content, highlightthickness=0, borderwidth=0, background=self.theme["root_bg"])
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(content, orient="vertical", command=self.canvas.yview, style="Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._on_canvas_scroll)

        self.panel = ttk.Frame(self.canvas, style="Root.TFrame", padding=(2, 2, 2, 10))
        self.panel_window = self.canvas.create_window((0, 0), window=self.panel, anchor="nw")
        self.panel.bind("<Configure>", self._on_panel_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mouse_wheel)

        footer = ttk.Frame(root, style="Footer.TFrame", padding=(18, 10, 18, 10))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.footer_status = ttk.Label(
            footer,
            text=f"Ready for Windows release | Version {APP_VERSION}",
            style="FooterText.TLabel",
        )
        self.footer_status.grid(row=0, column=0, sticky="w")
        self.footer_count = ttk.Label(footer, text="0 displays", style="FooterText.TLabel")
        self.footer_count.grid(row=0, column=1, sticky="e")
        self._update_scrollbar_visibility()

    def _on_panel_configure(self, _event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_scrollbar_visibility()

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.panel_window, width=event.width)
        self._update_scrollbar_visibility()

    def _on_canvas_scroll(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)
        self._update_scrollbar_visibility()

    def _update_scrollbar_visibility(self) -> None:
        if not hasattr(self, "canvas"):
            return
        self.update_idletasks()
        content_height = self.panel.winfo_reqheight()
        viewport_height = self.canvas.winfo_height()
        should_show = content_height > viewport_height + 8
        if should_show and not self._scrollbar_visible:
            self.scrollbar.grid(row=0, column=1, sticky="ns")
            self._scrollbar_visible = True
        elif not should_show and self._scrollbar_visible:
            self.scrollbar.grid_remove()
            self._scrollbar_visible = False

    def _on_mouse_wheel(self, event) -> None:
        if self._scrollbar_visible and self.canvas.winfo_exists():
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _load_state_bundle(self) -> tuple[dict[str, int], dict[str, bool]]:
        for candidate in (STATE_FILE, STATE_BACKUP_FILE):
            if not candidate.exists():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                cleaned = normalize_state_payload(raw)
                settings = normalize_settings_payload(raw)
                if candidate == STATE_BACKUP_FILE and cleaned:
                    logging.warning("Recovered brightness state from backup file")
                return cleaned, settings
            except Exception:
                logging.error("Failed to load state file: %s\n%s", candidate, traceback.format_exc())
        return {}, normalize_settings_payload({})

    def _save_state(self) -> None:
        temp_path: Path | None = None
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 2,
                "updated_at": int(time.time()),
                "values": dict(sorted(self.dimmer.values.items())),
                "settings": {
                    "hotkeys_enabled": self.hotkeys_enabled.get(),
                    "dark_mode_enabled": self.dark_mode_enabled.get(),
                },
            }

            if STATE_FILE.exists():
                shutil.copy2(STATE_FILE, STATE_BACKUP_FILE)

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=LOG_DIR,
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                json.dump(payload, temp_file, indent=2)
                temp_path = Path(temp_file.name)

            os.replace(temp_path, STATE_FILE)
        except Exception:
            logging.error("Failed to save state file\n%s", traceback.format_exc())
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def refresh(self) -> None:
        logging.info("Refreshing display cards")
        for card in self.cards:
            card.destroy()
        self.cards.clear()
        if self.empty_label:
            self.empty_label.destroy()
            self.empty_label = None

        displays = get_displays()
        if not displays:
            self.empty_label = ttk.Label(
                self.panel,
                text="No displays detected. Connect a monitor and press Refresh.",
                style="CardMeta.TLabel",
            )
            self.empty_label.pack(fill="x", pady=(24, 10), ipadx=14, ipady=24)
            self.footer_count.config(text="0 displays")
            self.footer_status.config(text=f"No displays detected | Version {APP_VERSION}")
            self._update_scrollbar_visibility()
            return

        for index, display in enumerate(displays):
            logging.info("Display found: %s", display)
            if display.device not in self.dimmer.values:
                desired = self.saved_values.get(display.device, 100)
                self.dimmer.set_brightness(display, desired)
            card = DisplayCard(
                self.panel,
                display,
                self.dimmer.get_value(display),
                self.set_brightness,
                self.identify,
                self.theme,
            )
            self.after(index * CARD_REVEAL_STAGGER_MS, lambda c=card: c.pack(fill="x", pady=(0, 10)))
            self.cards.append(card)

        self.footer_count.config(text=f"{len(displays)} display{'s' if len(displays) != 1 else ''}")
        self.footer_status.config(text=f"Ready for Windows release | Version {APP_VERSION}")
        self._update_scrollbar_visibility()

    def set_brightness(self, display: Display, value: int) -> bool:
        ok = self.dimmer.set_brightness(display, value)
        self._save_state()
        if not ok:
            self.bell()
        return ok

    def reset_all(self) -> None:
        self.dimmer.restore_all()
        for card in self.cards:
            card.slider.set(100)
            card.value_label.config(text="100%")
        self.footer_status.config(text=f"Brightness reset on {len(self.cards)} display{'s' if len(self.cards) != 1 else ''}")

    def identify(self, display: Display) -> None:
        left, top, right, bottom = display.rect
        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(background="#0a2234")
        width = max(500, display.width // 2)
        height = max(260, display.height // 3)
        x = left + max(10, (display.width - width) // 2)
        y = top + max(10, (display.height - height) // 2)
        x = max(left, min(x, right - width))
        y = max(top, min(y, bottom - height))
        popup.geometry(f"{width}x{height}+{x}+{y}")

        heading = tk.Label(
            popup,
            text=f"Display {display.index + 1}",
            font=("Segoe UI Semibold", 24, "bold"),
            fg="#e7f5ff",
            bg="#0a2234",
        )
        heading.pack(pady=(34, 8))
        subtitle = tk.Label(
            popup,
            text=f"{display.device}  |  {display.width} x {display.height}",
            font=("Segoe UI", 12),
            fg="#a9cfdf",
            bg="#0a2234",
        )
        subtitle.pack(pady=(0, 8))
        number = tk.Label(
            popup,
            text=str(display.index + 1),
            font=("Segoe UI Variable Display", 82, "bold"),
            fg="#2dd4bf",
            bg="#0a2234",
        )
        number.pack(expand=True)
        self.bell()
        self.after(3000, popup.destroy)

    def _toggle_theme(self) -> None:
        self._style()
        self._update_scrollbar_visibility()
        self.refresh()
        self._save_state()

    def _show_about(self) -> None:
        about = tk.Toplevel(self)
        about.title("About / Help")
        about.geometry("560x420")
        about.minsize(520, 360)
        about.configure(background=self.theme["root_bg"])

        container = ttk.Frame(about, style="Root.TFrame", padding=(18, 16, 18, 16))
        container.pack(fill="both", expand=True)

        title = ttk.Label(container, text="External Monitor Brightness", style="CardTitle.TLabel")
        title.pack(anchor="w")
        subtitle = ttk.Label(
            container,
            text=f"A small Windows utility for gamma-based visible brightness control. Version {APP_VERSION}.",
            style="CardMeta.TLabel",
        )
        subtitle.pack(anchor="w", pady=(4, 12))

        help_text = (
            "Quick Help\n"
            "- Use sliders or tray presets (50/75/100) to adjust displays.\n"
            "- Hotkeys: Ctrl+Alt+Up / Ctrl+Alt+Down (if enabled).\n"
            "- Use Identify to map a card to a physical monitor.\n"
            "- Reset All restores all displays to normal gamma.\n\n"
            "Storage\n"
            f"- State: {STATE_FILE}\n"
            f"- Backup: {STATE_BACKUP_FILE}\n"
            f"- Logs: {LOG_FILE}\n\n"
            "Notes\n"
            "- This changes visible brightness via gamma ramps, not monitor hardware backlight.\n"
            f"- Minimum level is constrained to {MIN_BRIGHTNESS}% for this driver setup."
        )

        text = tk.Text(
            container,
            wrap="word",
            height=16,
            relief="flat",
            bd=0,
            padx=10,
            pady=10,
            bg=self.theme["card_bg"],
            fg=self.theme["card_title_fg"],
            insertbackground=self.theme["card_title_fg"],
        )
        text.pack(fill="both", expand=True)
        text.insert("1.0", help_text)
        text.configure(state="disabled")

        ttk.Button(container, text="Close", style="PrimaryHeader.TButton", command=about.destroy).pack(anchor="e", pady=(12, 0))

    def _set_all_displays(self, value: int) -> None:
        for card in self.cards:
            card.slider.set(value)
            card._changed(str(value))

    def _step_all_displays(self, delta: int) -> None:
        for card in self.cards:
            current = int(float(card.slider.get()))
            target = max(MIN_BRIGHTNESS, min(100, current + delta))
            card.slider.set(target)
            card._changed(str(target))

    def _is_startup_enabled(self) -> bool:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ,
            ) as key:
                value, _ = winreg.QueryValueEx(key, RUN_AT_STARTUP_VALUE_NAME)
                return bool(value)
        except OSError:
            return False

    def _toggle_startup(self) -> None:
        exe = Path(sys.executable)
        target = f'"{exe}" "{Path(__file__).resolve()}"' if exe.suffix.lower() == ".exe" and "python" in exe.name.lower() else f'"{exe}"'
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                if self.startup_enabled.get():
                    winreg.SetValueEx(key, RUN_AT_STARTUP_VALUE_NAME, 0, winreg.REG_SZ, target)
                else:
                    try:
                        winreg.DeleteValue(key, RUN_AT_STARTUP_VALUE_NAME)
                    except FileNotFoundError:
                        pass
        except OSError:
            self.startup_enabled.set(self._is_startup_enabled())
            messagebox.showwarning("Startup Setting", "Could not update startup setting.")

    def _toggle_hotkeys(self) -> None:
        self._apply_hotkey_registration()
        self._save_state()

    def _apply_hotkey_registration(self) -> None:
        hwnd = self.winfo_id()
        if self.hotkeys_enabled.get() and not self._hotkeys_registered:
            ok_bright = ctypes.windll.user32.RegisterHotKey(
                hwnd, HOTKEY_ID_BRIGHTER, win32con.MOD_CONTROL | win32con.MOD_ALT, win32con.VK_UP
            )
            ok_dim = ctypes.windll.user32.RegisterHotKey(
                hwnd, HOTKEY_ID_DIMMER, win32con.MOD_CONTROL | win32con.MOD_ALT, win32con.VK_DOWN
            )
            self._hotkeys_registered = bool(ok_bright and ok_dim)
            if not self._hotkeys_registered:
                self.hotkeys_enabled.set(False)
                messagebox.showwarning("Hotkeys", "Could not register global hotkeys.")
                ctypes.windll.user32.UnregisterHotKey(hwnd, HOTKEY_ID_BRIGHTER)
                ctypes.windll.user32.UnregisterHotKey(hwnd, HOTKEY_ID_DIMMER)
        elif not self.hotkeys_enabled.get() and self._hotkeys_registered:
            ctypes.windll.user32.UnregisterHotKey(hwnd, HOTKEY_ID_BRIGHTER)
            ctypes.windll.user32.UnregisterHotKey(hwnd, HOTKEY_ID_DIMMER)
            self._hotkeys_registered = False

    def _poll_hotkeys(self) -> None:
        msg = ctypes.wintypes.MSG()
        while ctypes.windll.user32.PeekMessageW(
            ctypes.byref(msg), None, win32con.WM_HOTKEY, win32con.WM_HOTKEY, win32con.PM_REMOVE
        ):
            if msg.wParam == HOTKEY_ID_BRIGHTER:
                self._step_all_displays(5)
            elif msg.wParam == HOTKEY_ID_DIMMER:
                self._step_all_displays(-5)
        self.after(HOTKEY_POLL_MS, self._poll_hotkeys)

    def _create_tray_icon_image(self):
        if Image is None or ImageDraw is None:
            return None
        image = Image.new("RGB", (64, 64), "#0f2f4a")
        draw = ImageDraw.Draw(image)
        draw.ellipse((10, 10, 54, 54), fill="#2dd4bf")
        draw.rectangle((30, 4, 34, 60), fill="#f8fbff")
        draw.rectangle((4, 30, 60, 34), fill="#f8fbff")
        return image

    def _start_tray(self) -> None:
        if pystray is None:
            return
        if self.tray_icon is not None:
            return

        def on_show(_icon=None, _item=None):
            self.after(0, self._show_window)

        def on_hide(_icon=None, _item=None):
            self.after(0, self._hide_window)

        def on_refresh(_icon=None, _item=None):
            self.after(0, self.refresh)

        def on_set_50(_icon=None, _item=None):
            self.after(0, lambda: self._set_all_displays(50))

        def on_set_75(_icon=None, _item=None):
            self.after(0, lambda: self._set_all_displays(75))

        def on_set_100(_icon=None, _item=None):
            self.after(0, lambda: self._set_all_displays(100))

        def on_exit(_icon=None, _item=None):
            self.after(0, self._close)

        menu = pystray.Menu(
            pystray.MenuItem("Show", on_show, default=True),
            pystray.MenuItem("Hide", on_hide),
            pystray.MenuItem("Refresh", on_refresh),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Set 50%", on_set_50),
            pystray.MenuItem("Set 75%", on_set_75),
            pystray.MenuItem("Set 100%", on_set_100),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", on_exit),
        )
        self.tray_icon = pystray.Icon(
            "External Monitor Brightness",
            self._create_tray_icon_image(),
            "External Monitor Brightness",
            menu,
        )
        self._tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self._tray_thread.start()

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _hide_window(self) -> None:
        self.withdraw()

    def _on_window_state_change(self, _event) -> None:
        if self.state() == "iconic" and self.tray_icon is not None:
            self.after(0, self._hide_window)

    def _on_close_request(self) -> None:
        # Gamma ramps die with the process, so hide instead of exit.
        if pystray is not None:
            self._hide_window()
            return
        confirm = messagebox.askyesno(
            "Exit",
            "Closing will exit the app and reset all displays to 100% brightness.\n\nExit anyway?",
        )
        if confirm:
            self._close()

    def _close(self) -> None:
        logging.info("Closing app")
        if self._hotkeys_registered:
            hwnd = self.winfo_id()
            ctypes.windll.user32.UnregisterHotKey(hwnd, HOTKEY_ID_BRIGHTER)
            ctypes.windll.user32.UnregisterHotKey(hwnd, HOTKEY_ID_DIMMER)
            self._hotkeys_registered = False
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self._save_state()
        self.dimmer.close()
        self.destroy()


_single_instance_handle: int | None = None


def acquire_single_instance() -> bool:
    global _single_instance_handle
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        if handle:
            kernel32.CloseHandle(handle)
        hwnd = user32.FindWindowW(None, APP_WINDOW_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
        return False
    _single_instance_handle = handle
    return True


def get_displays() -> list[Display]:
    displays: list[Display] = []
    for index, monitor in enumerate(win32api.EnumDisplayMonitors()):
        handle, _dc, rect = monitor
        info = win32api.GetMonitorInfo(handle)
        primary = bool(info.get("Flags", 0) & win32con.MONITORINFOF_PRIMARY)
        displays.append(Display(index=index, device=info.get("Device", ""), rect=rect, primary=primary))
    return displays


if __name__ == "__main__":
    setup_logging()
    if not acquire_single_instance():
        sys.exit(0)
    try:
        app = BrightnessApp()
        app.mainloop()
    except Exception as exc:
        logging.error("Fatal startup/runtime exception\n%s", traceback.format_exc())
        messagebox.showerror("External Monitor Brightness", f"The app hit an unexpected error:\n\n{exc}")
