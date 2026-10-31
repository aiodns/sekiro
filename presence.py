import sys
import json
import time
import os
import multiprocessing as mp
from datetime import datetime
from urllib.parse import urlparse

from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont
from pypresence import Presence

BG_DARKEST = "#0a0a0c"
BG_DARK = "#111114"
BG_PANEL = "#17171b"
BG_INPUT = "#1d1d22"
BORDER = "#2a2a30"
TEXT_MAIN = "#f2f2f4"
TEXT_MUTED = "#8b8b94"
ACCENT = "#ff223f"
ACCENT_HOVER = "#e01636"
ACCENT_DIM = "#3a1016"
SUCCESS = "#2ecc71"
FONT_FAMILY = "Segoe UI"

PRESETS_FILE = "presets.json"
MAX_SLOTS = 32
INITIAL_SLOTS = 6

TYPE_MAP = {
    "Playing": 0,
    "Streaming": 1,
    "Listening": 2,
    "Watching": 3,
    "Competing": 5,
}

DEFAULT_APP_ID = "1453358037506199743"

NO_APP_WHITELIST = ("Roblox", "League of Legends")


def _activity_name(value):
    return str(value or "").strip(" \t\r\n")


# Platform presets mirror the working platform/application mappings used by rpc.py.
# These do NOT launch or query Spotify/YouTube/etc.; they only construct a Discord
# Rich Presence payload.
PLATFORM_PRESETS = {
    "None": None,
    "Spotify": {
        "application_id": "3201606009684",
        "asset": "spotify",
    },
    "Xbox": {
        "application_id": "622174530214821906",
        "asset": "xbox",
        "platform": "xbox",
    },
    "PlayStation 5": {
        "application_id": "1470539864909943067",
        "asset": "playstation",
        "platform": "ps5",
    },
    "PlayStation 4": {
        "application_id": "1470539864909943067",
        "asset": "playstation",
        "platform": "ps4",
    },
    "Crunchyroll": {
        "application_id": "981509069309354054",
        "asset": "crunchyroll",
    },
    "YouTube": {
        "asset": "youtube",
    },
    "Twitch": {
        "asset": "twitch",
    },
    "VRChat": {
        "application_id": "1498387526501535835",
        "asset": "vrchat",
        "platform": "meta_quest",
    },
    "Roblox": {
        "application_id": None,
        "asset": "",
        "name": "Roblox",
    },
    "League of Legends": {
        "application_id": None,
        "asset": "",
        "name": "League of Legends",
    },
}


def _rpc_worker(slot_id, client_id, conn):
    """Dedicated Discord IPC worker.

    Each slot runs in its own OS process, which gives Discord a distinct PID
    for each activity. This prevents one slot's SET_ACTIVITY from replacing
    another slot's activity when multiple RPC slots are running at once.
    """
    rpc = None
    try:
        rpc = Presence(str(client_id))
        rpc.connect()
        conn.send({"event": "connected", "slot": slot_id})

        while True:
            if not conn.poll(1.0):
                continue
            message = conn.recv()
            command = message.get("command") if isinstance(message, dict) else None

            if command == "set":
                activity = message.get("activity") or {}
                payload = {
                    "cmd": "SET_ACTIVITY",
                    "args": {
                        "pid": os.getpid(),
                        "activity": activity,
                    },
                    "nonce": str(time.time_ns()),
                }
                try:
                    rpc.send_data(1, payload)
                    conn.send({"event": "sent", "slot": slot_id})
                except Exception as exc:
                    conn.send({"event": "error", "slot": slot_id, "error": f"SET_ACTIVITY: {type(exc).__name__}: {exc}"})

            elif command == "clear":
                try:
                    # Discord can briefly retain an activity when rapidly
                    # switching between separate IPC clients/PIDs.  Clear
                    # several times back-to-back so a stale activity is much
                    # less likely to survive the rotation switch.
                    for _ in range(3):
                        rpc.clear()
                    conn.send({"event": "cleared", "slot": slot_id})
                except Exception as exc:
                    conn.send({"event": "error", "slot": slot_id, "error": f"CLEAR_ACTIVITY: {type(exc).__name__}: {exc}"})

            elif command == "stop":
                try:
                    rpc.clear()
                except Exception:
                    pass
                break

    except Exception as exc:
        try:
            conn.send({"event": "error", "slot": slot_id, "error": f"IPC worker: {type(exc).__name__}: {exc}"})
        except Exception:
            pass
    finally:
        try:
            if rpc is not None:
                rpc.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


class RPCSlot:
    def __init__(self, slot_id):
        self.slot_id = slot_id
        self.rpc = None
        self.worker = None
        self.pipe = None
        self.connected = False
        self.running = False
        self.start_time = None
        self.progress_started_at = None
        self.settings = {
            "client_id": "",
            "name": f"Slot {slot_id + 1}",
            "details": "",
            "state": "",
            "large_image": "",
            "large_text": "",
            "small_image": "",
            "small_text": "",
            "large_image_url": "",
            "small_image_url": "",
            "party_size": None,
            "party_id": "",
            "start_timestamp": True,
            "end_timestamp": False,
            "countdown_minutes": 30,
            "progress_enabled": False,
            "progress_elapsed": 0,
            "progress_duration": 0,
            "buttons": [],
            "activity_type": "Playing",
            "platform": "None",
            "stream_url": "",
        }


class RichPresenceGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.slots = [RPCSlot(i) for i in range(INITIAL_SLOTS)]
        self.current_slot = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_all_presences)
        self.worker_poll_timer = QTimer(self)
        self.worker_poll_timer.timeout.connect(self.poll_workers)
        self.worker_poll_timer.start(100)
        self.rotation_timer = QTimer(self)
        self.rotation_timer.timeout.connect(self.rotate_presence)
        self.rotation_index = -1
        self.rotation_active_slot = None
        self.rotation_pending_clears = set()
        self._loading_slot = False
        self.init_ui()
        self.switch_slot(0)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    def _make_button(self, text, bg, hover, text_color=TEXT_MAIN, border=None):
        border = border or hover
        btn = QPushButton(text)
        btn.setMinimumHeight(36)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {bg};
                color: {text_color};
                border: 1px solid {border};
                border-radius: 7px;
                padding: 7px 12px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {hover};
                border-color: {ACCENT};
            }}
            QPushButton:disabled {{
                color: #5d5d65;
                background: #141418;
                border-color: #222228;
            }}
        """)
        return btn

    def _section_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{ACCENT};font-size:10px;font-weight:700;letter-spacing:1px;padding-top:8px;"
        )
        return lbl

    def _line_edit(self, placeholder=""):
        w = QLineEdit()
        w.setMinimumHeight(36)
        w.setPlaceholderText(placeholder)
        return w

    def _spin(self, minimum=0, maximum=86400, suffix=""):
        w = QSpinBox()
        w.setRange(minimum, maximum)
        w.setMinimumHeight(36)
        if suffix:
            w.setSuffix(suffix)
        w.setButtonSymbols(QAbstractSpinBox.UpDownArrows)
        return w

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def init_ui(self):
        self.setWindowTitle("rpc — presence customizer")
        self.resize(1050, 900)
        self.setMinimumSize(900, 760)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        eyebrow = QLabel("BETA")
        eyebrow.setStyleSheet(f"color:{ACCENT};font-size:11px;font-weight:700;letter-spacing:3px;")
        root.addWidget(eyebrow)

        title = QLabel("rpc customizer")
        title.setStyleSheet(f"font-size:29px;font-weight:700;color:{TEXT_MAIN};")
        root.addWidget(title)

        subtitle = QLabel("shitty rich presence tool made by @seqcst non eof this code is obfuscated because im a silly chdu")
        subtitle.setStyleSheet(f"color:{TEXT_MUTED};font-size:13px;")
        root.addWidget(subtitle)

        slot_row = QHBoxLayout()
        slot_row.addWidget(self._label("SLOT:"))

        self.slot_scroll = QScrollArea()
        self.slot_scroll.setWidgetResizable(True)
        self.slot_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.slot_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.slot_scroll.setFrameShape(QFrame.NoFrame)
        self.slot_scroll.setFixedHeight(44)
        self.slot_scroll.setMinimumWidth(220)
        self.slot_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.slot_container = QWidget()
        self.slot_container.setStyleSheet("background: transparent;")
        self.slot_layout = QHBoxLayout(self.slot_container)
        self.slot_layout.setContentsMargins(0, 0, 0, 0)
        self.slot_layout.setSpacing(4)
        self.slot_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.slot_scroll.setWidget(self.slot_container)
        slot_row.addWidget(self.slot_scroll, 1)

        self.slot_buttons = []
        for i in range(len(self.slots)):
            self._add_slot_button(i)

        self.add_slot_btn = QPushButton("+")
        self.add_slot_btn.setFixedSize(38, 36)
        self.add_slot_btn.setToolTip("Add another RPC slot")
        self.add_slot_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_INPUT};
                color: {TEXT_MAIN};
                border: 1px solid {BORDER};
                border-radius: 7px;
                font-size: 20px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {ACCENT_DIM};
                border-color: {ACCENT};
                color: white;
            }}
        """)
        self.add_slot_btn.clicked.connect(self.add_slot)
        slot_row.addWidget(self.add_slot_btn)
        root.addLayout(slot_row)

        self.status_label = QLabel("● disconnected")
        self.status_label.setMinimumHeight(30)
        root.addWidget(self.status_label)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        main_tab = QWidget()
        images_tab = QWidget()
        party_tab = QWidget()
        buttons_tab = QWidget()
        presets_tab = QWidget()
        self.tabs.addTab(main_tab, "main")
        self.tabs.addTab(images_tab, "images")
        self.tabs.addTab(party_tab, "party & time")
        self.tabs.addTab(buttons_tab, "buttons")
        self.tabs.addTab(presets_tab, "presets")

        self.setup_main_tab(main_tab)
        self.setup_images_tab(images_tab)
        self.setup_party_tab(party_tab)
        self.setup_buttons_tab(buttons_tab)
        self.setup_presets_tab(presets_tab)

        controls = QHBoxLayout()
        self.connect_btn = self._make_button("connect", ACCENT, ACCENT_HOVER)
        self.connect_btn.clicked.connect(self.toggle_connection)
        controls.addWidget(self.connect_btn)

        self.start_btn = self._make_button("start", SUCCESS, "#27ae60")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.toggle_rpc)
        controls.addWidget(self.start_btn)

        b = self._make_button("start all", BG_INPUT, BORDER)
        b.clicked.connect(self.start_all_slots)
        controls.addWidget(b)
        self.rotation_enabled = QCheckBox("rotate")
        self.rotation_enabled.setToolTip("Loop through connected slots and show one presence at a time")
        self.rotation_enabled.stateChanged.connect(self.toggle_rotation)
        controls.addWidget(self.rotation_enabled)
        self.rotation_interval = self._spin(1, 86400, " sec")
        self.rotation_interval.setValue(5)
        self.rotation_interval.setFixedWidth(90)
        self.rotation_interval.setToolTip("How long each slot stays active")
        self.rotation_interval.valueChanged.connect(self.update_rotation_interval)
        controls.addWidget(self.rotation_interval)
        b = self._make_button("stop all", "transparent", ACCENT_DIM, ACCENT, ACCENT)
        b.clicked.connect(self.stop_all_slots)
        controls.addWidget(b)

        b = self._make_button("save config", BG_INPUT, BORDER)
        b.clicked.connect(self.save_config)
        controls.addWidget(b)
        b = self._make_button("load config", BG_INPUT, BORDER)
        b.clicked.connect(self.load_config)
        controls.addWidget(b)
        b = self._make_button("clear", "transparent", ACCENT_DIM, ACCENT, ACCENT)
        b.clicked.connect(self.clear_presence)
        controls.addWidget(b)
        controls.addStretch()
        root.addLayout(controls)

        root.addWidget(self._build_preview_card())

        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("log"))
        log_header.addStretch()
        copy_logs = self._make_button("copy logs", BG_INPUT, BORDER)
        copy_logs.clicked.connect(self.copy_logs)
        log_header.addWidget(copy_logs)
        clear_logs = self._make_button("clear logs", "transparent", ACCENT_DIM, ACCENT, ACCENT)
        clear_logs.clicked.connect(lambda: self.log_text.clear())
        log_header.addWidget(clear_logs)
        root.addLayout(log_header)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(150)
        self.log_text.setFont(QFont("Consolas", 9))
        root.addWidget(self.log_text)

    def _label(self, text):
        l = QLabel(text)
        l.setStyleSheet(f"color:{TEXT_MUTED};font-size:11px;font-weight:700;letter-spacing:1px;")
        return l

    def _build_preview_card(self):
        frame = QFrame()
        frame.setStyleSheet(f"background:{BG_PANEL};border:1px solid {BORDER};border-radius:10px;")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(16, 12, 16, 12)

        self.preview_type = QLabel("Playing")
        self.preview_type.setStyleSheet(f"color:{TEXT_MUTED};font-size:11px;font-weight:700;")
        outer.addWidget(self.preview_type)

        row = QHBoxLayout()
        self.preview_img = QLabel("RPC")
        self.preview_img.setFixedSize(76, 76)
        self.preview_img.setAlignment(Qt.AlignCenter)
        self.preview_img.setStyleSheet(f"background:{BG_INPUT};color:{TEXT_MUTED};border-radius:10px;font-weight:700;")
        row.addWidget(self.preview_img)

        text = QVBoxLayout()
        self.preview_name = QLabel("Name")
        self.preview_name.setStyleSheet(f"color:{TEXT_MAIN};font-size:14px;font-weight:700;")
        self.preview_details = QLabel("Details")
        self.preview_details.setStyleSheet(f"color:{TEXT_MUTED};font-size:12px;")
        self.preview_state = QLabel("State")
        self.preview_state.setStyleSheet(f"color:{TEXT_MUTED};font-size:12px;")
        self.preview_buttons_label = QLabel("")
        self.preview_buttons_label.setStyleSheet(f"color:{ACCENT};font-size:11px;font-weight:600;")
        text.addWidget(self.preview_name)
        text.addWidget(self.preview_details)
        text.addWidget(self.preview_state)
        text.addWidget(self.preview_buttons_label)
        text.addStretch()
        row.addLayout(text, 1)
        outer.addLayout(row)
        return frame

    def _form_layout(self, tab):
        layout = QFormLayout(tab)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(10)
        layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return layout

    def setup_main_tab(self, tab):
        layout = self._form_layout(tab)

        self.client_id = self._line_edit("Discord application/client ID")
        self.client_id.textChanged.connect(self.update_settings)
        layout.addRow("client id", self.client_id)

        layout.addRow(self._section_label("IDENTITY"), QWidget())

        self.activity_type = QComboBox()
        self.activity_type.setMinimumHeight(36)
        self.activity_type.addItems(TYPE_MAP.keys())
        self.activity_type.currentTextChanged.connect(self.on_activity_type_changed)
        layout.addRow("activity type", self.activity_type)

        self.platform_combo = QComboBox()
        self.platform_combo.setMinimumHeight(36)
        self.platform_combo.addItems(PLATFORM_PRESETS.keys())
        self.platform_combo.currentTextChanged.connect(self.on_platform_changed)
        layout.addRow("platform preset", self.platform_combo)

        self.name = self._line_edit("e.g. Valorant, Coding, Watching YouTube")
        self.details = self._line_edit("top line")
        self.state = self._line_edit("bottom line")
        self.stream_url = self._line_edit("https://twitch.tv/... (Streaming only)")
        for w in (self.name, self.details, self.state, self.stream_url):
            w.textChanged.connect(self.update_settings)
            w.textChanged.connect(self.update_preview)
        layout.addRow("name", self.name)
        layout.addRow("details", self.details)
        layout.addRow("state", self.state)
        layout.addRow("stream url", self.stream_url)

    def setup_images_tab(self, tab):
        layout = self._form_layout(tab)
        layout.addRow(self._section_label("CUSTOM IMAGES — NO DISCORD ASSETS REQUIRED"), QWidget())

        info = QLabel(
            "Paste a publicly reachable HTTPS image URL. Discord supports external image URLs here, "
            "so you do not need to upload anything to Rich Presence Art Assets. PNG/JPG/WebP/GIF are supported."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{TEXT_MUTED};font-size:11px;")
        layout.addRow("", info)

        self.large_image = self._line_edit("https://example.com/large.png")
        self.large_image.textChanged.connect(self.update_settings)
        self.large_image.textChanged.connect(self.update_preview)
        layout.addRow("large image URL", self.large_image)

        self.large_text = self._line_edit("tooltip")
        self.large_text.textChanged.connect(self.update_settings)
        layout.addRow("large tooltip", self.large_text)

        self.small_image = self._line_edit("https://example.com/small.png")
        self.small_image.textChanged.connect(self.update_settings)
        self.small_image.textChanged.connect(self.update_preview)
        layout.addRow("small image URL", self.small_image)

        self.small_text = self._line_edit("tooltip")
        self.small_text.textChanged.connect(self.update_settings)
        layout.addRow("small tooltip", self.small_text)

        clear = self._make_button("clear custom images", "transparent", ACCENT_DIM, ACCENT, ACCENT)
        clear.clicked.connect(self.clear_images)
        layout.addRow("", clear)

        note = QLabel("Local file paths are not sent to Discord. The URL must be publicly reachable by Discord.")
        note.setStyleSheet(f"color:{TEXT_MUTED};font-size:11px;")
        note.setWordWrap(True)
        layout.addRow("", note)

    def setup_party_tab(self, tab):
        layout = self._form_layout(tab)

        layout.addRow(self._section_label("PARTY"), QWidget())
        self.party_enabled = QCheckBox("enable party size")
        self.party_enabled.stateChanged.connect(self.on_party_toggle)
        layout.addRow("party", self.party_enabled)

        self.party_size = self._spin(1, 999)
        self.party_max = self._spin(1, 999)
        self.party_size.valueChanged.connect(self.on_party_value_changed)
        self.party_max.valueChanged.connect(self.on_party_value_changed)
        layout.addRow("current size", self.party_size)
        layout.addRow("max size", self.party_max)

        layout.addRow(self._section_label("TIMESTAMPS"), QWidget())
        self.start_timestamp = QCheckBox("show elapsed time")
        self.end_timestamp = QCheckBox("show countdown")
        self.start_timestamp.setChecked(True)
        self.start_timestamp.stateChanged.connect(self.update_settings)
        self.end_timestamp.stateChanged.connect(self.update_settings)
        layout.addRow("start", self.start_timestamp)
        layout.addRow("end", self.end_timestamp)

        self.countdown_minutes = self._spin(1, 1440, " min")
        self.countdown_minutes.setValue(30)
        self.countdown_minutes.valueChanged.connect(self.update_settings)
        layout.addRow("countdown length", self.countdown_minutes)

        layout.addRow(self._section_label("PROGRESS BAR"), QWidget())
        self.progress_enabled = QCheckBox("enable elapsed / total progress")
        self.progress_enabled.stateChanged.connect(self.on_progress_toggle)
        layout.addRow("progress", self.progress_enabled)

        self.progress_elapsed = self._spin(0, 86400, " sec")
        self.progress_duration = self._spin(1, 86400, " sec")
        self.progress_elapsed.valueChanged.connect(self.update_settings)
        self.progress_duration.valueChanged.connect(self.update_settings)
        layout.addRow("elapsed", self.progress_elapsed)
        layout.addRow("total duration", self.progress_duration)

        progress_info = QLabel(
            "When enabled, the progress bar starts at the entered elapsed time and advances automatically while the RPC is running."
        )
        progress_info.setWordWrap(True)
        progress_info.setStyleSheet(f"color:{TEXT_MUTED};font-size:11px;")
        layout.addRow("", progress_info)

        reset = self._make_button("reset progress / timestamp", BG_INPUT, BORDER)
        reset.clicked.connect(self.reset_timestamp)
        layout.addRow("", reset)

    def setup_buttons_tab(self, tab):
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 18, 20, 18)
        info = QLabel("Discord renders up to two Rich Presence buttons.")
        info.setStyleSheet(f"color:{TEXT_MUTED};font-size:11px;")
        layout.addWidget(info)

        self.button_widgets = []
        for i in range(2):
            group = QGroupBox(f"button {i + 1}")
            gl = QFormLayout(group)
            label = self._line_edit("button label")
            url = self._line_edit("https://example.com")
            enabled = QCheckBox("enabled")
            label.textChanged.connect(self.update_settings)
            url.textChanged.connect(self.update_settings)
            enabled.stateChanged.connect(self.update_settings)
            gl.addRow("label", label)
            gl.addRow("url", url)
            gl.addRow("", enabled)
            layout.addWidget(group)
            self.button_widgets.append({"label": label, "url": url, "enable": enabled})
        layout.addStretch()

    def setup_presets_tab(self, tab):
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(self._section_label("QUICK PRESETS"))

        presets = {
            "gaming": {"name": "Valorant", "details": "Competitive", "state": "5v5 Match", "activity_type": "Playing"},
            "coding": {"name": "VS Code", "details": "Writing Python", "state": "Building something", "activity_type": "Playing"},
            "streaming": {"name": "Twitch", "details": "Live on Twitch", "state": "Playing Games", "activity_type": "Streaming"},
            "studying": {"name": "Studying", "details": "Learning Python", "state": "Making progress!", "activity_type": "Watching"},
            "browsing": {"name": "Chrome", "details": "Browsing Web", "state": "Developer Portal", "activity_type": "Watching"},
            "roblox": {"name": "Roblox", "details": "", "state": "", "activity_type": "Playing"},
            "league": {"name": "League of Legends", "details": "", "state": "", "activity_type": "Playing"},
        }
        grid = QGridLayout()
        for n, (name, preset) in enumerate(presets.items()):
            b = self._make_button(name, BG_INPUT, ACCENT_DIM)
            b.clicked.connect(lambda checked, p=preset: self.load_preset(p))
            grid.addWidget(b, n // 3, n % 3)
        layout.addLayout(grid)

        layout.addWidget(self._section_label("PLATFORM RPC PRESETS"))
        platform_grid = QGridLayout()
        for n, platform in enumerate(["Spotify", "Xbox", "PlayStation 5", "PlayStation 4", "Crunchyroll", "YouTube", "Twitch", "VRChat"]):
            b = self._make_button(platform, BG_INPUT, ACCENT_DIM)
            b.clicked.connect(lambda checked, p=platform: self.apply_platform_preset(p))
            platform_grid.addWidget(b, n // 4, n % 4)
        layout.addLayout(platform_grid)

        layout.addWidget(self._section_label("YOUR PRESETS"))
        row = QHBoxLayout()
        self.saved_presets_combo = QComboBox()
        self.saved_presets_combo.setMinimumHeight(36)
        row.addWidget(self.saved_presets_combo, 1)
        b = self._make_button("load", BG_INPUT, ACCENT_DIM)
        b.clicked.connect(self.load_saved_preset)
        row.addWidget(b)
        b = self._make_button("delete", "transparent", ACCENT_DIM, ACCENT, ACCENT)
        b.clicked.connect(self.delete_saved_preset)
        row.addWidget(b)
        layout.addLayout(row)

        layout.addWidget(self._section_label("SAVE CURRENT AS PRESET"))
        row = QHBoxLayout()
        self.preset_name = self._line_edit("preset name")
        row.addWidget(self.preset_name, 1)
        b = self._make_button("save", ACCENT, ACCENT_HOVER)
        b.clicked.connect(self.save_preset)
        row.addWidget(b)
        layout.addLayout(row)
        layout.addStretch()
        self.refresh_saved_presets()

    # ------------------------------------------------------------------
    # Slot state
    # ------------------------------------------------------------------
    def _normalize_slot_settings(self, slot):
        s = slot.settings
        platform = s.get("platform", "None")

        preset = PLATFORM_PRESETS.get(platform)
        if preset:
            if preset.get("application_id"):
                s["client_id"] = str(preset["application_id"])
            if preset.get("name") and (not _activity_name(s.get("name")) or s["name"].startswith("Slot ")):
                s["name"] = preset["name"]

        if platform in NO_APP_WHITELIST:
            s["client_id"] = DEFAULT_APP_ID
            if not s.get("activity_type"):
                s["activity_type"] = "Playing"
        else:
            client_id = str(s.get("client_id", "")).strip()
            if client_id and not client_id.isdigit():
                s["client_id"] = ""

        return s

    def switch_slot(self, slot_id):
        if hasattr(self, "client_id") and not self._loading_slot:
            self.update_settings()
        self.current_slot = slot_id
        self._loading_slot = True
        s = self._normalize_slot_settings(self.slots[slot_id])
        self.client_id.setText(s["client_id"])
        self.activity_type.setCurrentText(s["activity_type"])
        platform = s.get("platform", "None")
        idx = self.platform_combo.findText(platform)
        self.platform_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.name.setText(s["name"])
        self.details.setText(s["details"])
        self.state.setText(s["state"])
        self.stream_url.setText(s["stream_url"])
        self.large_image.setText(s["large_image"])
        self.large_text.setText(s["large_text"])
        self.small_image.setText(s["small_image"])
        self.small_text.setText(s["small_text"])
        self.party_enabled.setChecked(bool(s.get("party_size")))
        ps = s.get("party_size") or [1, 1]
        self.party_size.setValue(max(1, int(ps[0])))
        self.party_max.setValue(max(1, int(ps[1])))
        self.start_timestamp.setChecked(bool(s["start_timestamp"]))
        self.end_timestamp.setChecked(bool(s["end_timestamp"]))
        self.countdown_minutes.setValue(int(s.get("countdown_minutes", 30)))
        self.progress_enabled.setChecked(bool(s.get("progress_enabled", False)))
        self.progress_elapsed.setValue(int(s.get("progress_elapsed", 0)))
        self.progress_duration.setValue(max(1, int(s.get("progress_duration", 1))))
        for i, w in enumerate(self.button_widgets):
            if i < len(s.get("buttons", [])):
                w["label"].setText(s["buttons"][i].get("label", ""))
                w["url"].setText(s["buttons"][i].get("url", ""))
                w["enable"].setChecked(True)
            else:
                w["label"].clear(); w["url"].clear(); w["enable"].setChecked(False)
        self._loading_slot = False
        self.connect_btn.setText("disconnect" if self.slots[slot_id].connected else "connect")
        self.start_btn.setText("stop" if self.slots[slot_id].running else "start")
        self.start_btn.setEnabled(self.slots[slot_id].connected)
        self.update_party_controls()
        self.update_progress_controls()
        self._restyle_slots()
        self.update_slot_status()
        self.update_preview()

    def _add_slot_button(self, slot_id):
        b = QPushButton(str(slot_id + 1))
        b.setFixedSize(38, 36)
        b.setCheckable(True)
        b.setStyleSheet("QPushButton { margin: 0px; padding: 0px; }")
        b.clicked.connect(lambda checked, idx=slot_id: self.switch_slot(idx))
        self.slot_buttons.append(b)
        self.slot_layout.addWidget(b)
        return b

    def add_slot(self):
        if len(self.slots) >= MAX_SLOTS:
            self.log(f"Slot limit reached ({MAX_SLOTS})")
            self.add_slot_btn.setEnabled(False)
            return

        slot_id = len(self.slots)
        self.slots.append(RPCSlot(slot_id))
        self._add_slot_button(slot_id)
        self._restyle_slots()
        self.switch_slot(slot_id)
        self.slot_scroll.ensureWidgetVisible(self.slot_buttons[-1])
        self.log(f"Added slot {slot_id + 1}")

        if len(self.slots) >= MAX_SLOTS:
            self.add_slot_btn.setEnabled(False)

    def _sync_slot_buttons(self):
        for i, button in enumerate(self.slot_buttons):
            button.setText(str(i + 1))
            button.setChecked(i == self.current_slot)
        if hasattr(self, "add_slot_btn"):
            self.add_slot_btn.setEnabled(len(self.slots) < MAX_SLOTS)

    def _restyle_slots(self):
        for i, b in enumerate(self.slot_buttons):
            slot = self.slots[i]
            if i == self.current_slot:
                bg, border, fg = ACCENT, ACCENT, "white"
            elif slot.running:
                bg, border, fg = SUCCESS, SUCCESS, "white"
            elif slot.connected:
                bg, border, fg = BG_INPUT, SUCCESS, TEXT_MAIN
            else:
                bg, border, fg = BG_INPUT, BORDER, TEXT_MAIN
            b.setStyleSheet(
                f"background:{bg};color:{fg};border:1px solid {border};"
                f"border-radius:7px;font-weight:700;margin:0px;padding:0px;"
                f"min-width:38px;max-width:38px;min-height:36px;max-height:36px;"
            )
        self._sync_slot_buttons()

    def update_slot_status(self):
        slot = self.slots[self.current_slot]
        if slot.running:
            text, color = f"● live — slot {self.current_slot + 1}", ACCENT
        elif slot.connected:
            text, color = f"● connected — slot {self.current_slot + 1}", SUCCESS
        else:
            text, color = f"● disconnected — slot {self.current_slot + 1}", TEXT_MUTED
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color:{color};background:{BG_PANEL};border:1px solid {color};border-radius:14px;padding:6px 12px;font-size:12px;font-weight:600;"
        )

    # ------------------------------------------------------------------
    # Settings / preview
    # ------------------------------------------------------------------
    def on_activity_type_changed(self, _):
        if not self._loading_slot:
            self.update_settings(); self.update_preview()

    def update_settings(self):
        if self._loading_slot or not hasattr(self, "client_id"):
            return
        s = self.slots[self.current_slot].settings
        s.update({
            "client_id": self.client_id.text().strip(),
            "activity_type": self.activity_type.currentText(),
            "platform": self.platform_combo.currentText(),
            "name": self.name.text(),
            "details": self.details.text(),
            "state": self.state.text(),
            "stream_url": self.stream_url.text().strip(),
            "large_image": self.large_image.text().strip(),
            "large_text": self.large_text.text(),
            "small_image": self.small_image.text().strip(),
            "small_text": self.small_text.text(),
            "party_size": [self.party_size.value(), max(self.party_size.value(), self.party_max.value())] if self.party_enabled.isChecked() else None,
            "party_id": s.get("party_id") or f"rpc-party-{self.current_slot + 1}-{self.client_id.text().strip()}",
            "start_timestamp": self.start_timestamp.isChecked(),
            "end_timestamp": self.end_timestamp.isChecked(),
            "countdown_minutes": self.countdown_minutes.value(),
            "progress_enabled": self.progress_enabled.isChecked(),
            "progress_elapsed": self.progress_elapsed.value(),
            "progress_duration": self.progress_duration.value(),
            "buttons": [
                {"label": w["label"].text().strip(), "url": w["url"].text().strip()}
                for w in self.button_widgets if w["enable"].isChecked() and w["label"].text().strip() and w["url"].text().strip()
            ],
        })

    def update_preview(self):
        if not hasattr(self, "name"):
            return
        atype = self.activity_type.currentText()
        self.preview_type.setText(atype)
        self.preview_name.setText(self.name.text() or "Name")
        self.preview_details.setText(self.details.text() or "Details")
        self.preview_state.setText(self.state.text() or "State")
        self.preview_img.setText("IMG" if self.large_image.text().strip() else "RPC")
        buttons = [w["label"].text().strip() for w in self.button_widgets if w["enable"].isChecked() and w["label"].text().strip()]
        self.preview_buttons_label.setText("  |  ".join(f"[{x}]" for x in buttons[:2]))

    def on_platform_changed(self, platform):
        if self._loading_slot or platform == "None":
            if not self._loading_slot:
                self.update_settings()
            return
        preset = PLATFORM_PRESETS.get(platform)
        if not preset:
            return
        s = self.slots[self.current_slot].settings
        self._loading_slot = True
        s["platform"] = platform
        # Platform RPCs must connect the IPC worker with the same Discord
        # application ID that owns the built-in asset key.  In particular,
        # Spotify's `spotify` image belongs to Spotify's application.
        # Sending application_id=3201606009684 while the IPC connection is
        # opened with a different client ID results in a successful IPC write
        # but Discord silently dropping/not rendering the activity.
        # Full platform presets replace the IPC client/type. Generic game presets
        # only change the displayed name and keep the user's client/type.
        if preset.get("application_id"):
            s["client_id"] = preset["application_id"]
        if preset.get("asset"):
            s["large_image"] = preset["asset"]
            s["small_image"] = ""
        elif preset.get("asset") == "":
            s["large_image"] = ""
            s["small_image"] = ""
        if platform == "Spotify":
            if not _activity_name(s.get("name")) or s["name"].startswith("Slot "):
                s["name"] = "Spotify"
            if not s.get("details"):
                s["details"] = "Song"
            if not s.get("state"):
                s["state"] = "Artist"
        elif platform == "Crunchyroll":
            if not _activity_name(s.get("name")) or s["name"].startswith("Slot "):
                s["name"] = "Crunchyroll"
        elif platform == "YouTube":
            if not _activity_name(s.get("name")) or s["name"].startswith("Slot "):
                s["name"] = "YouTube"
        elif platform == "Twitch":
            if not _activity_name(s.get("name")) or s["name"].startswith("Slot "):
                s["name"] = "Twitch"
        elif platform in ("Xbox", "PlayStation 4", "PlayStation 5"):
            if not _activity_name(s.get("name")) or s["name"].startswith("Slot "):
                s["name"] = "Xbox" if platform == "Xbox" else platform
        elif platform in NO_APP_WHITELIST:
            # Keep the fixed fallback IPC client ID for these no-app platforms,
            # but do not strip user-provided details/state/images. The user
            # can still customize the RPC payload while the platform name remains
            # visible and the connection uses the default app ID.
            s["client_id"] = DEFAULT_APP_ID
            if not _activity_name(s.get("name")) or s["name"].startswith("Slot "):
                s["name"] = platform
            if not s.get("activity_type"):
                s["activity_type"] = "Playing"
        self.client_id.setText(s["client_id"])
        self.activity_type.setCurrentText(s["activity_type"])
        self.name.setText(s["name"])
        self.details.setText(s["details"])
        self.state.setText(s["state"])
        self.large_image.setText(s["large_image"])
        self.small_image.setText("")
        self._loading_slot = False
        self.update_settings()

        # The IPC handshake is tied to the client ID used when the worker
        # connects. If the platform changes while connected, restart the
        # worker so Spotify actually uses its application ID.
        was_connected = self.slots[self.current_slot].connected
        was_running = self.slots[self.current_slot].running
        if was_connected:
            self._disconnect_slot(self.current_slot)
            self._connect_slot(self.current_slot, s["client_id"].strip())
            if was_running:
                self.slots[self.current_slot].running = True
                now = time.time()
                self.slots[self.current_slot].start_time = now
                self.slots[self.current_slot].progress_started_at = now
                self._send_presence(self.current_slot)

        self.update_preview()
        self.log(f"Platform preset applied: {platform} (IPC client ID retained from slot settings)")

    def on_party_value_changed(self, _):
        # Changing either party field implicitly enables party data so the GUI
        # behaves as expected without requiring a separate checkbox click.
        if not self._loading_slot and not self.party_enabled.isChecked():
            self.party_enabled.setChecked(True)
        if not self._loading_slot:
            self.update_settings()

    def on_party_toggle(self, _):
        if not self._loading_slot:
            self.update_party_controls(); self.update_settings()

    def update_party_controls(self):
        enabled = self.party_enabled.isChecked()
        self.party_size.setEnabled(enabled)
        self.party_max.setEnabled(enabled)

    def on_progress_toggle(self, _):
        if not self._loading_slot:
            self.update_progress_controls(); self.update_settings()

    def update_progress_controls(self):
        enabled = self.progress_enabled.isChecked()
        self.progress_elapsed.setEnabled(enabled)
        self.progress_duration.setEnabled(enabled)
        self.start_timestamp.setEnabled(not enabled)
        self.end_timestamp.setEnabled(not enabled)
        self.countdown_minutes.setEnabled(not enabled and self.end_timestamp.isChecked())

    def clear_images(self):
        self.large_image.clear(); self.large_text.clear(); self.small_image.clear(); self.small_text.clear()
        self.update_settings(); self.update_preview()

    # ------------------------------------------------------------------
    # RPC
    # ------------------------------------------------------------------
    def toggle_connection(self):
        slot = self.slots[self.current_slot]
        self.update_settings()
        platform = slot.settings.get("platform", "None")
        client_id = slot.settings["client_id"].strip()
        if platform in NO_APP_WHITELIST:
            client_id = DEFAULT_APP_ID
            slot.settings["client_id"] = client_id
        if not client_id and platform not in NO_APP_WHITELIST:
            QMessageBox.warning(self, "Missing Client ID", "Enter your Discord application/client ID first.")
            return

        if not slot.connected:
            self._connect_slot(slot.slot_id, client_id)
        else:
            self._disconnect_slot(slot.slot_id)
        self.switch_slot(self.current_slot)

    def _connect_slot(self, slot_id, client_id):
        slot = self.slots[slot_id]
        platform = slot.settings.get("platform", "None")
        if platform in NO_APP_WHITELIST:
            client_id = DEFAULT_APP_ID
            slot.settings["client_id"] = client_id
            if slot_id == self.current_slot:
                self.client_id.setText(client_id)
            self.log(f"Slot {slot_id + 1}: {platform} uses fixed fallback IPC client ID {client_id}.")
        elif not client_id.strip():
            # Only leave genuinely missing IDs blank for non-no-app slots; every
            # other slot must still be a valid Discord app/client ID.
            client_id = ""

        if slot.worker is not None and slot.worker.is_alive():
            slot.connected = True
            return

        try:
            parent_conn, child_conn = mp.Pipe(duplex=True)
            process = mp.Process(
                target=_rpc_worker,
                args=(slot_id, client_id, child_conn),
                name=f"DiscordRPC-Slot-{slot_id + 1}",
                daemon=True,
            )
            process.start()
            child_conn.close()
            slot.pipe = parent_conn
            slot.worker = process
            slot.connected = True
            self.log(f"Slot {slot_id + 1}: started dedicated Discord RPC worker (PID {process.pid})")
        except Exception as exc:
            slot.pipe = None
            slot.worker = None
            slot.connected = False
            self.log(f"Slot {slot_id + 1}: worker start failed: {type(exc).__name__}: {exc}")
            QMessageBox.critical(self, "Connection Error", str(exc))

    def _disconnect_slot(self, slot_id):
        slot = self.slots[slot_id]
        if slot.running:
            self.stop_slot(slot_id)

        try:
            if slot.pipe:
                slot.pipe.send({"command": "stop"})
        except Exception:
            pass

        if slot.worker is not None:
            slot.worker.join(timeout=2.0)
            if slot.worker.is_alive():
                try:
                    slot.worker.terminate()
                    slot.worker.join(timeout=1.0)
                except Exception:
                    pass

        try:
            if slot.pipe:
                slot.pipe.close()
        except Exception:
            pass

        slot.pipe = None
        slot.worker = None
        slot.connected = False
        self.log(f"Slot {slot_id + 1}: disconnected")

    def toggle_rpc(self):
        if self.slots[self.current_slot].running:
            self.stop_slot(self.current_slot)
        else:
            self.start_slot(self.current_slot)

    def start_slot(self, slot_id):
        slot = self.slots[slot_id]
        if not slot.connected:
            QMessageBox.warning(self, "Not Connected", "Connect this slot first.")
            return
        if slot_id == self.current_slot:
            self.update_settings()
        if not _activity_name(slot.settings["name"]):
            QMessageBox.warning(self, "Missing Name", "Enter a name for the activity.")
            return
        if slot.settings["progress_enabled"] and slot.settings["progress_duration"] <= 0:
            QMessageBox.warning(self, "Invalid Progress", "Total duration must be greater than zero.")
            return

        slot.running = True
        now = time.time()
        slot.start_time = now
        slot.progress_started_at = now
        if not self.rotation_timer.isActive() or slot_id == self.rotation_active_slot:
            self._send_presence(slot_id)
        self.log(f"Slot {slot_id + 1}: presence started")
        if not self.timer.isActive():
            self.timer.start(15000)
        self.switch_slot(self.current_slot)

    def stop_slot(self, slot_id):
        slot = self.slots[slot_id]
        if not slot.running:
            return
        try:
            if slot.pipe:
                slot.pipe.send({"command": "clear"})
        except Exception as exc:
            self.log(f"Slot {slot_id + 1}: clear request failed: {exc}")
        slot.running = False
        slot.progress_started_at = None
        self.log(f"Slot {slot_id + 1}: presence stopped")
        if not any(x.running for x in self.slots):
            self.timer.stop()
        self.switch_slot(self.current_slot)

    def start_all_slots(self):
        self.update_settings()
        for slot in self.slots:
            if slot.connected:
                continue
            platform = slot.settings.get("platform", "None")
            client_id = slot.settings["client_id"].strip()
            if not client_id and platform not in NO_APP_WHITELIST:
                continue
            self._connect_slot(slot.slot_id, client_id)

        if self.rotation_enabled.isChecked():
            self.start_rotation()
            for i, slot in enumerate(self.slots):
                if slot.connected and not slot.running and _activity_name(slot.settings["name"]):
                    self.start_slot(i)
            return
        for i, slot in enumerate(self.slots):
            if slot.connected and not slot.running and _activity_name(slot.settings["name"]):
                self.start_slot(i)

    def stop_all_slots(self):
        self.stop_rotation()
        for slot in list(self.slots):
            if slot.running:
                self.stop_slot(slot.slot_id)

    def toggle_rotation(self, state):
        if state:
            self.start_rotation()
        else:
            self.stop_rotation()

    def update_rotation_interval(self, seconds):
        if self.rotation_timer.isActive():
            self.rotation_timer.start(int(seconds) * 1000)

    def start_rotation(self):
        if not hasattr(self, "rotation_enabled"):
            return
        eligible = [
            slot for slot in self.slots
            if slot.connected and _activity_name(slot.settings["name"])
        ]
        if not eligible:
            self.rotation_enabled.blockSignals(True)
            self.rotation_enabled.setChecked(False)
            self.rotation_enabled.blockSignals(False)
            QMessageBox.warning(self, "No Ready Slots", "Connect at least one slot with a name before starting rotation.")
            return

        # Before rotation begins, clear EVERY running slot.  Do not let the
        # first slot get sent until all old activities have acknowledged the
        # clear request.
        self.rotation_pending_clears = {
            slot.slot_id for slot in self.slots
            if slot.running and self._clear_slot_presence(slot.slot_id)
        }
        self.rotation_index = -1
        self.rotation_active_slot = None
        self.rotation_timer.start(self.rotation_interval.value() * 1000)
        if not self.rotation_pending_clears:
            self._finish_rotation_switch()
        self.log(f"Rotation started ({self.rotation_interval.value()} second interval)")

    def stop_rotation(self):
        if not hasattr(self, "rotation_timer"):
            return
        was_active = self.rotation_timer.isActive()
        self.rotation_timer.stop()
        self.rotation_index = -1
        self.rotation_active_slot = None
        self.rotation_pending_clears.clear()
        if was_active:
            self.log("Rotation stopped")

    def rotate_presence(self):
        eligible = [
            slot for slot in self.slots
            if slot.connected and _activity_name(slot.settings["name"])
        ]
        if not eligible:
            self.stop_rotation()
            return

        # Never send the next RPC while another slot may still be showing.
        # Clear all running slots, not just the active one, so a stale RPC
        # cannot stack with the next activity.
        if self.rotation_pending_clears:
            return

        self.rotation_pending_clears = {
            slot.slot_id for slot in self.slots
            if slot.running and self._clear_slot_presence(slot.slot_id)
        }

        if self.rotation_pending_clears:
            return

        self._finish_rotation_switch()

    def _finish_rotation_switch(self):
        if not self.rotation_timer.isActive():
            return
        eligible = [
            slot for slot in self.slots
            if slot.connected and _activity_name(slot.settings["name"])
        ]
        if not eligible:
            self.stop_rotation()
            return
        self.rotation_index = (self.rotation_index + 1) % len(eligible)
        next_slot = eligible[self.rotation_index]
        self.rotation_active_slot = next_slot.slot_id
        if next_slot.running:
            self._send_presence(next_slot.slot_id)
        else:
            self.start_slot(next_slot.slot_id)

    def _clear_slot_presence(self, slot_id):
        slot = self.slots[slot_id]
        if not slot.pipe or not slot.worker or not slot.worker.is_alive():
            return False
        try:
            slot.pipe.send({"command": "clear"})
            self.log(f"Slot {slot_id + 1}: rotation clear queued (3x)")
            return True
        except Exception as exc:
            self.log(f"Slot {slot_id + 1}: rotation clear failed: {type(exc).__name__}: {exc}")
            return False

    def _send_presence(self, slot_id):
        slot = self.slots[slot_id]
        s = slot.settings

        # Roblox/League of Legends still use the default Discord client ID, but
        # they should not be stripped down to a bare name-only payload. Keep the
        # user's details/state/assets so the RPC fields work for those presets.
        platform = s.get("platform", "None")
        if platform in NO_APP_WHITELIST:
            s["client_id"] = DEFAULT_APP_ID
            if not s.get("activity_type"):
                s["activity_type"] = "Playing"

        # All activity types use the same top-level Discord "name" field.
        # In particular, Playing must use the configured name exactly like
        # Streaming / Listening / Watching / Competing.
        activity_name = _activity_name(s.get("name"))[:128]
        activity_type = s.get("activity_type", "Playing")
        activity_type_value = TYPE_MAP.get(activity_type, TYPE_MAP["Playing"])
        if activity_type == "Listening":
            activity_type_value = 2
        activity = {
            "type": activity_type_value,
            "name": activity_name,
        }

        # rpc.py-compatible platform payloads. This is deliberately local: no
        # Spotify/Web API/desktop client is queried or required.
        preset = PLATFORM_PRESETS.get(platform)
        if preset:
            if preset.get("application_id"):
                activity["application_id"] = preset["application_id"]
            if preset.get("platform"):
                activity["platform"] = preset["platform"]

        if s["details"]:
            activity["details"] = s["details"][:128]
        if s["state"]:
            activity["state"] = s["state"][:128]

        # External image URLs: no Developer Portal assets are needed.
        assets = {}
        large = self._image_value(s["large_image"])
        small = self._image_value(s["small_image"])
        if large:
            assets["large_image"] = large
            if s["large_text"]:
                assets["large_text"] = s["large_text"][:128]
        if small:
            assets["small_image"] = small
            if s["small_text"]:
                assets["small_text"] = s["small_text"][:128]
        if assets:
            activity["assets"] = assets

        now = time.time()
        if s["progress_enabled"] and s["progress_duration"] > 0:
            base = s["progress_elapsed"]
            running_for = max(0, now - (slot.progress_started_at or now))
            elapsed = min(s["progress_duration"], base + int(running_for))
            start = now - elapsed
            activity["timestamps"] = {
                "start": int(start),
                "end": int(start + s["progress_duration"]),
            }
        elif s["start_timestamp"]:
            activity["timestamps"] = {"start": int(slot.start_time or now)}
            if s["end_timestamp"]:
                activity["timestamps"]["end"] = int(now + s["countdown_minutes"] * 60)
        elif s["end_timestamp"]:
            activity["timestamps"] = {"end": int(now + s["countdown_minutes"] * 60)}

        if s["party_size"]:
            current_size, max_size = s["party_size"]
            current_size = max(1, int(current_size))
            max_size = max(current_size, int(max_size))
            party_id = (s.get("party_id") or f"rpc-party-{slot_id + 1}").strip()[:128]
            activity["party"] = {
                "id": party_id,
                "size": [current_size, max_size],
            }
            self.log(f"Slot {slot_id + 1}: party = {current_size}/{max_size} (id={party_id})")
        if s["buttons"]:
            activity["buttons"] = s["buttons"][:2]
        if s["activity_type"] == "Streaming" and s["stream_url"]:
            activity["url"] = s["stream_url"][:512]

        # Spotify needs a true Spotify activity payload with the standard sync,
        # session, party, and secret fields so Discord treats it as a Spotify
        # listening state instead of a generic game/playing status.
        if platform == "Spotify":
            asset_key = self._image_value(s.get("large_image")) or "spotify"
            small_key = self._image_value(s.get("small_image")) or ""
            duration_minutes = float(s.get("progress_duration", 210) / 60.0) if s.get("progress_duration") else 3.5
            elapsed_minutes = float(s.get("progress_elapsed", 0) / 60.0) if s.get("progress_elapsed") else 0.0
            cur_ms = int(elapsed_minutes * 60000)
            tot_ms = int(duration_minutes * 60000)
            start_ms = int(time.time() * 1000) - cur_ms
            tid = "0VjIjW4GlUZAMYd2vXMi3b"
            activity = {
                "type": 2,
                "name": "Spotify",
                "details": (s.get("details") or s.get("name") or "Unknown")[:128],
                "state": (s.get("state") or "Unknown")[:128],
                "timestamps": {"start": start_ms, "end": start_ms + tot_ms},
                "application_id": "3201606009684",
                "sync_id": tid,
                "session_id": f"spotify:{tid}",
                "party": {"id": f"spotify:{tid}", "size": [1, 1]},
                "secrets": {"join": tid, "spectate": tid, "match": tid},
                "instance": True,
                "flags": 48,
            }
            assets = {}
            if asset_key:
                assets["large_image"] = asset_key
                if s.get("large_text"):
                    assets["large_text"] = s["large_text"][:128]
            if small_key:
                assets["small_image"] = small_key
                if s.get("small_text"):
                    assets["small_text"] = s["small_text"][:128]
            if assets:
                activity["assets"] = assets
            if s["buttons"]:
                activity["buttons"] = s["buttons"][:2]
            self.log(
                f"Slot {slot_id + 1}: Spotify payload prepared "
                f"(tid={tid}, application_id=3201606009684)"
            )

        try:
            if slot.pipe and slot.worker and slot.worker.is_alive():
                slot.pipe.send({"command": "set", "activity": activity})
                self.log(f"Slot {slot_id + 1}: SET_ACTIVITY queued (worker PID {slot.worker.pid}, platform={platform})")
                return True
            self.log(f"Slot {slot_id + 1}: SET_ACTIVITY skipped — worker is not running")
        except Exception as exc:
            self.log(f"Slot {slot_id + 1}: SET_ACTIVITY send failed: {type(exc).__name__}: {exc}")
        return False

    @staticmethod
    def _image_value(value):
        value = (value or "").strip()
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return value
        if value.startswith("mp:"):
            return value
        return value

    def clear_presence(self):
        slot = self.slots[self.current_slot]
        if slot.connected and slot.pipe:
            try:
                slot.pipe.send({"command": "clear"})
                self.log(f"Slot {self.current_slot + 1}: presence clear queued")
            except Exception as exc:
                self.log(f"Clear failed: {type(exc).__name__}: {exc}")

    def reset_timestamp(self):
        slot = self.slots[self.current_slot]
        now = time.time()
        slot.start_time = now
        slot.progress_started_at = now
        if slot.running and (
            not self.rotation_timer.isActive()
            or self.rotation_active_slot == self.current_slot
        ):
            self._send_presence(self.current_slot)
        self.log(f"Slot {self.current_slot + 1}: timestamp/progress reset")

    def update_all_presences(self):
        if self.rotation_timer.isActive():
            if self.rotation_active_slot is not None:
                slot = self.slots[self.rotation_active_slot]
                if slot.running and slot.connected:
                    self._send_presence(slot.slot_id)
            return
        for i, slot in enumerate(self.slots):
            if slot.running and slot.connected:
                self._send_presence(i)

    def poll_workers(self):
        for i, slot in enumerate(self.slots):
            pipe = slot.pipe
            if pipe is None:
                continue
            try:
                while pipe.poll():
                    message = pipe.recv()
                    event = message.get("event") if isinstance(message, dict) else None
                    if event == "connected":
                        self.log(f"Slot {i + 1}: Discord IPC connected (dedicated PID {slot.worker.pid if slot.worker else 'unknown'})")
                    elif event == "sent":
                        pass
                    elif event == "cleared":
                        self.log(f"Slot {i + 1}: Discord activity cleared")
                        self.rotation_pending_clears.discard(i)
                        if not self.rotation_pending_clears:
                            self._finish_rotation_switch()
                    elif event == "error":
                        self.log(f"Slot {i + 1}: {message.get('error', 'unknown worker error')}")
            except (EOFError, BrokenPipeError, OSError) as exc:
                if slot.connected:
                    self.log(f"Slot {i + 1}: worker pipe closed: {exc}")
                slot.connected = False

            if slot.worker is not None and not slot.worker.is_alive():
                self.rotation_pending_clears.discard(i)
                if slot.running:
                    slot.running = False
                    slot.progress_started_at = None
                    self.log(f"Slot {i + 1}: RPC worker exited; presence stopped")
                slot.connected = False
                try:
                    slot.worker.join(timeout=0.1)
                except Exception:
                    pass
                slot.worker = None
                try:
                    pipe.close()
                except Exception:
                    pass
                slot.pipe = None

                if not self.rotation_pending_clears:
                    self._finish_rotation_switch()

        self._restyle_slots()
        self.update_slot_status()
        if hasattr(self, "start_btn"):
            self.start_btn.setEnabled(self.slots[self.current_slot].connected)
            self.start_btn.setText("stop" if self.slots[self.current_slot].running else "start")
            self.connect_btn.setText("disconnect" if self.slots[self.current_slot].connected else "connect")

    # ------------------------------------------------------------------
    # Config / presets
    # ------------------------------------------------------------------
    def apply_platform_preset(self, platform):
        idx = self.platform_combo.findText(platform)
        if idx >= 0:
            self.platform_combo.setCurrentIndex(idx)

    def load_preset(self, preset):
        self._apply_settings_dict(preset)
        self.log(f"Loaded preset: {preset.get('name', 'custom')}")

    def _apply_settings_dict(self, data):
        s = self.slots[self.current_slot].settings
        self._loading_slot = True
        s.update({k: v for k, v in data.items() if k in s})
        self._normalize_slot_settings(self.slots[self.current_slot])
        self.switch_slot(self.current_slot)

    def _read_presets_file(self):
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def refresh_saved_presets(self):
        self.saved_presets_combo.clear()
        presets = self._read_presets_file()
        if not presets:
            self.saved_presets_combo.addItem("(none saved yet)")
            self.saved_presets_combo.setEnabled(False)
        else:
            self.saved_presets_combo.setEnabled(True)
            self.saved_presets_combo.addItems(presets.keys())

    def load_saved_preset(self):
        presets = self._read_presets_file()
        name = self.saved_presets_combo.currentText()
        if name in presets:
            self._apply_settings_dict(presets[name])
            self.log(f"Loaded saved preset: {name}")

    def delete_saved_preset(self):
        presets = self._read_presets_file()
        name = self.saved_presets_combo.currentText()
        if name in presets:
            del presets[name]
            with open(PRESETS_FILE, "w", encoding="utf-8") as f:
                json.dump(presets, f, indent=2)
            self.refresh_saved_presets()
            self.log(f"Deleted preset: {name}")

    def save_preset(self):
        name = self.preset_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Error", "Enter a preset name.")
            return
        self.update_settings()
        preset = dict(self.slots[self.current_slot].settings)
        preset["client_id"] = ""
        presets = self._read_presets_file()
        presets[name] = preset
        with open(PRESETS_FILE, "w", encoding="utf-8") as f:
            json.dump(presets, f, indent=2)
        self.refresh_saved_presets()
        self.log(f"Saved preset: {name}")

    def save_config(self):
        self.update_settings()
        file, _ = QFileDialog.getSaveFileName(self, "Save Configuration", "", "JSON Files (*.json)")
        if file:
            with open(file, "w", encoding="utf-8") as f:
                json.dump({"slots": [x.settings for x in self.slots]}, f, indent=2)
            self.log(f"Config saved: {file}")

    def load_config(self):
        file, _ = QFileDialog.getOpenFileName(self, "Load Configuration", "", "JSON Files (*.json)")
        if not file:
            return
        try:
            with open(file, "r", encoding="utf-8") as f:
                config = json.load(f)
            if "slots" in config:
                saved_slots = config["slots"][:MAX_SLOTS]

                # Expand the GUI to match a saved configuration.
                while len(self.slots) < len(saved_slots):
                    slot_id = len(self.slots)
                    self.slots.append(RPCSlot(slot_id))
                    self._add_slot_button(slot_id)

                # Keep the loading gate active while the slot dictionaries are
                # restored and the focused slot is redrawn from them. That
                # prevents switch_slot() from doing its initial
                # update_settings() saveback from the old form widgets.
                self._loading_slot = True
                for i, data in enumerate(saved_slots):
                    if isinstance(data, dict):
                        self.slots[i].settings.update(data)

                self.switch_slot(min(self.current_slot, len(self.slots) - 1))
                self._restyle_slots()
            else:
                self._apply_settings_dict(config)
            self.log(f"Config loaded: {file}")
        except Exception as e:
            self.log(f"Config load failed: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # Logs / close
    # ------------------------------------------------------------------
    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")
        bar = self.log_text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def copy_logs(self):
        QApplication.clipboard().setText(self.log_text.toPlainText())
        self.log("Logs copied to clipboard")

    def closeEvent(self, event):
        self.worker_poll_timer.stop()
        self.timer.stop()
        self.stop_rotation()
        for slot in self.slots:
            try:
                if slot.pipe:
                    slot.pipe.send({"command": "stop"})
            except Exception:
                pass
            try:
                if slot.worker is not None:
                    slot.worker.join(timeout=1.5)
                    if slot.worker.is_alive():
                        slot.worker.terminate()
                        slot.worker.join(timeout=0.5)
            except Exception:
                pass
            try:
                if slot.pipe:
                    slot.pipe.close()
            except Exception:
                pass
            slot.pipe = None
            slot.worker = None
            slot.rpc = None
        event.accept()



def main():
    mp.freeze_support()
    try:
        mp.set_start_method("spawn", force=True)
    except RuntimeError:
        pass
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(f"""
        QMainWindow, QWidget {{ background:{BG_DARK}; color:{TEXT_MAIN}; font-family:{FONT_FAMILY}; }}
        QLabel {{ color:{TEXT_MAIN}; }}
        QLineEdit, QComboBox, QSpinBox, QTextEdit {{
            background:{BG_INPUT}; color:{TEXT_MAIN};
            border:1px solid {BORDER}; border-radius:7px;
            padding:7px 9px; min-height:20px;
            selection-background-color:{ACCENT};
        }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border:1px solid {ACCENT}; }}
        QLineEdit:disabled, QSpinBox:disabled {{ color:#66666e; background:#151519; }}
        QComboBox QAbstractItemView {{ background:{BG_INPUT}; color:{TEXT_MAIN}; selection-background-color:{ACCENT_DIM}; }}
        QTabWidget::pane {{ background:{BG_PANEL}; border:1px solid {BORDER}; border-radius:9px; }}
        QTabBar::tab {{ color:{TEXT_MUTED}; background:transparent; padding:10px 16px; font-weight:600; }}
        QTabBar::tab:selected {{ color:{ACCENT}; border-bottom:2px solid {ACCENT}; }}
        QGroupBox {{ color:{TEXT_MAIN}; border:1px solid {BORDER}; border-radius:8px; margin-top:10px; padding-top:8px; }}
        QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 6px; color:{ACCENT}; font-weight:700; }}
        QCheckBox {{ color:{TEXT_MAIN}; spacing:8px; min-height:30px; }}
        QCheckBox::indicator {{ width:17px; height:17px; border:1px solid {BORDER}; border-radius:4px; background:{BG_INPUT}; }}
        QCheckBox::indicator:checked {{ background:{ACCENT}; border:1px solid {ACCENT}; }}
        QScrollBar:vertical {{ background:{BG_DARK}; width:10px; }}
        QScrollBar::handle:vertical {{ background:{BORDER}; border-radius:5px; min-height:20px; }}
    """)
    window = RichPresenceGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
