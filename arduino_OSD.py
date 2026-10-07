import sys
import time
import serial
import keyboard
import screen_brightness_control as sbc
import os
import json
import threading
import serial.tools.list_ports
import ctypes
from ctypes import wintypes

from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QProgressBar, QPushButton, QSpinBox, QLineEdit, QComboBox, 
                             QGroupBox, QFormLayout, QMessageBox, QFileDialog, QCheckBox, 
                             QScrollArea, QSystemTrayIcon, QMenu)
from PySide6.QtCore import Qt, QTimer, Signal, QPropertyAnimation, QPoint, QEasingCurve
from PySide6.QtGui import QFont, QCloseEvent, QIcon, QAction, QPixmap, QPainter, QColor
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
from comtypes import CLSCTX_ALL, GUID, COMMETHOD, IUnknown, HRESULT
import comtypes


# 1. КОНФИГУРАЦИЯ И НАСТРОЙКИ (json)

CONFIG_FILE = "app_config.json"

DEFAULT_CONFIG = {
    "baudrate": 115200,
    "default_port": "AUTO",
    "brightness_step": 5,
    "volume_step": 2,
    "connection_timeout": 60,  # Таймаут ожидания подключения (в секундах)
    "target_app_exe": "",
    "single_hold_action": "win+5",
    "enable_hold_rotations": True,
    "hold_actions": {
        "-5": "",
        "-4": "",
        "-3": "",
        "-2": "win+2",
        "-1": "win+1",
        "1": "win+3",
        "2": "win+4",
        "3": "",
        "4": "",
        "5": ""
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in cfg:
                        cfg[k] = v
                return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Ошибка сохранения конфига: {e}")

config = load_config()


# 2. ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ И WIN32 API

arduino_enabled = True
ser = None
is_connected = False
user32 = ctypes.WinDLL('user32', use_last_error=True)

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

WM_APPCOMMAND = 0x0319
APPCOMMAND_MEDIA_NEXTTRACK = 11
APPCOMMAND_MEDIA_PREVTRACK = 12
APPCOMMAND_MEDIA_PLAY_PAUSE = 14

MODE_BRIGHTNESS = 0
MODE_VOLUME = 1
mode = MODE_BRIGHTNESS
hold_steps = 0

monitors = sbc.list_monitors()
internal_display = next((m for m in monitors if "boe" in m.lower()), None)
if internal_display is None:
    internal_display = monitors[0] if monitors else 0

current_device_idx = 0
try:
    brightness_value = sbc.get_brightness(display=internal_display)[0]
except:
    brightness_value = 50

target_brightness = brightness_value
brightness_lock = threading.Lock()
brightness_update_event = threading.Event()
running = True


# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ УПРАВЛЕНИЯ

def send_global_vk(vk):
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    time.sleep(0.01)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

def get_hwnd_for_process(exe_name):
    if not exe_name:
        return None
    
    clean_exe_name = os.path.basename(exe_name).strip().lower()
    found_hwnd = None
    
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    
    def enum_windows_callback(hwnd, lparam):
        nonlocal found_hwnd
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            
            PROCESS_QUERY_INFORMATION = 0x0400
            PROCESS_VM_READ = 0x0010
            process_handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
            if process_handle:
                buffer = ctypes.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if ctypes.windll.psapi.GetModuleFileNameExW(process_handle, 0, buffer, size):
                    proc_exe = os.path.basename(buffer.value).lower()
                    if proc_exe == clean_exe_name:
                        found_hwnd = hwnd
                        ctypes.windll.kernel32.CloseHandle(process_handle)
                        return False
                ctypes.windll.kernel32.CloseHandle(process_handle)
        return True

    user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)
    return found_hwnd

def send_media_command(app_command, global_vk, label_text):
    target_exe = config.get("target_app_exe", "").strip()
    hwnd = get_hwnd_for_process(target_exe) if target_exe else None
    
    if hwnd:
        cmd_param = app_command << 16
        user32.PostMessageW(hwnd, WM_APPCOMMAND, 0, cmd_param)
    else:
        send_global_vk(global_vk)
    
    if osd:
        osd.trigger_signal.emit(label_text)

def media_next(): 
    send_media_command(APPCOMMAND_MEDIA_NEXTTRACK, VK_MEDIA_NEXT_TRACK, "Трек >>")

def media_prev(): 
    send_media_command(APPCOMMAND_MEDIA_PREVTRACK, VK_MEDIA_PREV_TRACK, "<< Трек")

def media_play_pause(): 
    send_media_command(APPCOMMAND_MEDIA_PLAY_PAUSE, VK_MEDIA_PLAY_PAUSE, "Пауза / Плей")

def change_brightness(delta):
    global target_brightness
    with brightness_lock:
        target_brightness = max(0, min(100, target_brightness + delta))
    brightness_update_event.set()

def brightness_worker():
    global brightness_value
    while running:
        if brightness_update_event.wait(timeout=0.1):
            with brightness_lock:
                new_val = target_brightness
            try:
                sbc.set_brightness(new_val, display=internal_display)
                brightness_value = new_val
                if osd: osd.trigger_signal.emit(f"Яркость {brightness_value}%")
            except Exception:
                pass
            brightness_update_event.clear()

threading.Thread(target=brightness_worker, daemon=True).start()

def set_volume(delta):
    try:
        devices = AudioUtilities.GetDeviceEnumerator()
        default_device = devices.GetDefaultAudioEndpoint(0, 1)
        interface = default_device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = comtypes.cast(interface, comtypes.POINTER(IAudioEndpointVolume))
        
        current = volume.GetMasterVolumeLevelScalar() * 100
        new_val = max(0, min(100, current + delta))
        volume.SetMasterVolumeLevelScalar(new_val / 100, None)
        osd.trigger_signal.emit(f"Громкость {int(new_val)}%")
    except Exception as e:
        osd.trigger_signal.emit("Ошибка звука")

def execute_action(action):
    action = action.strip()
    if not action:
        return
        
    if action.lower().endswith(".exe") or os.path.exists(action):
        try:
            os.startfile(action)
            exe_name = os.path.basename(action)
            osd.trigger_signal.emit(f"Запуск: {exe_name}")
        except Exception:
            osd.trigger_signal.emit("Ошибка запуска")
    else:
        try:
            keyboard.send(action)
            osd.trigger_signal.emit(f"Клавиши: {action}")
        except Exception:
            osd.trigger_signal.emit(f"Ошибка: {action}")

def execute_hold_rotation(step):
    if not config.get("enable_hold_rotations", True):
        return
    action = config.get("hold_actions", {}).get(str(step), "").strip()
    execute_action(action)

class IPolicyConfig(IUnknown):
    _iid_ = GUID('{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetMixFormat'),
        COMMETHOD([], HRESULT, 'GetDeviceFormat'),
        COMMETHOD([], HRESULT, 'ResetDeviceFormat'),
        COMMETHOD([], HRESULT, 'SetDeviceFormat'),
        COMMETHOD([], HRESULT, 'GetProcessingPeriod'),
        COMMETHOD([], HRESULT, 'SetProcessingPeriod'),
        COMMETHOD([], HRESULT, 'GetShareMode'),
        COMMETHOD([], HRESULT, 'SetShareMode'),
        COMMETHOD([], HRESULT, 'GetPropertyValue'),
        COMMETHOD([], HRESULT, 'SetPropertyValue'),
        COMMETHOD([], HRESULT, 'SetTemplate'),
        COMMETHOD([], HRESULT, 'SetEndpointVisibility'),
        COMMETHOD([], HRESULT, 'SetDefaultEndpoint',
                  (['in'], comtypes.wintypes.LPWSTR, 'pwszDeviceId'),
                  (['in'], comtypes.DWORD, 'role')),
        COMMETHOD([], HRESULT, 'SetEndpointProperties')
    ]

def set_windows_audio_device(device_id: str):
    CLSID_PolicyConfig = GUID('{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}')
    policy_config = comtypes.CoCreateInstance(
        CLSID_PolicyConfig,
        interface=IPolicyConfig,
        clsctx=comtypes.CLSCTX_ALL
    )
    policy_config.SetDefaultEndpoint(device_id, 0)
    policy_config.SetDefaultEndpoint(device_id, 2)

def switch_audio_device():
    global current_device_idx
    try:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except Exception:
            comtypes.CoInitialize()

        device_enumerator = AudioUtilities.GetDeviceEnumerator()
        devices = device_enumerator.EnumAudioEndpoints(0, 0x1)
        total_devices = devices.GetCount()
        
        if total_devices == 0:
            osd.trigger_signal.emit("Устройств нет")
            return

        current_device_idx += 1
        if current_device_idx >= total_devices:
            current_device_idx = 0

        target_device = devices.Item(current_device_idx)
        target_id = target_device.GetId()

        set_windows_audio_device(target_id)
        osd.trigger_signal.emit(f"Аудио {current_device_idx + 1}/{total_devices}")

    except Exception as e:
        print(f"Ошибка переключения: {e}")
        osd.trigger_signal.emit("Ошибка переключения")
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


# 4. РАБОТА С ARDUINO / COM ПОРТАМИ

def find_arduino_port():
    pref_port = config.get("default_port", "AUTO")
    ports = serial.tools.list_ports.comports()
    
    if pref_port != "AUTO":
        for p in ports:
            if p.device == pref_port:
                return p.device

    for port in ports:
        hwid = port.hwid.upper() if port.hwid else ""
        desc = port.description.upper() if port.description else ""
        manufacturer = port.manufacturer.upper() if port.manufacturer else ""
        
        if (
            "1A86" in hwid or "0403" in hwid or "2341" in hwid or "2A03" in hwid
            or "ARDUINO" in desc or "USB-SERIAL" in desc or "CH340" in desc
            or "CH341" in desc or "FTDI" in desc or "ARDUINO" in manufacturer
            or "FTDI" in manufacturer or "WCH" in manufacturer
        ):
            return port.device
    return None

last_status = None
def arduino_loop():
    global mode, hold_steps, last_status, ser, is_connected
    while running:
        if not arduino_enabled:
            time.sleep(0.5)
            continue
        try:
            port = find_arduino_port()
            if not port:
                raise serial.SerialException("Порт не найден")

            ser = serial.Serial(port, config.get("baudrate", 115200), timeout=0.1)
            time.sleep(2)
            ser.flushInput()

            if last_status != "connected":
                osd.trigger_signal.emit(f"Порт: {port}")
                last_status = "connected"
                is_connected = True

            while running and ser.is_open and arduino_enabled:
                try:
                    if ser.in_waiting > 0:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()
                        if not line:
                            continue
                        
                        b_step = config.get("brightness_step", 5)
                        v_step = config.get("volume_step", 2)

                        if line == "Right":
                            if mode == MODE_BRIGHTNESS: change_brightness(+b_step)
                            else: set_volume(+v_step)
                        elif line == "Left":
                            if mode == MODE_BRIGHTNESS: change_brightness(-b_step)
                            else: set_volume(-v_step)
                        elif line == "Click":
                            mode = 1 - mode
                            osd.trigger_signal.emit("Режим: " + ("ГРОМКОСТЬ" if mode == MODE_VOLUME else "ЯРКОСТЬ"))
                        elif line == "DoubleClick":
                            switch_audio_device()
                        elif line == "Hold":
                            if mode == MODE_VOLUME:
                                media_play_pause()
                            else:
                                single_action = config.get("single_hold_action", "win+5")
                                execute_action(single_action)
                        elif line == "Hold Right":
                            if mode == MODE_VOLUME:
                                media_next()
                            else:
                                hold_steps = min(5, hold_steps + 1)
                                osd.trigger_signal.emit(f"Шаги (+): {hold_steps}")
                        elif line == "Hold Left":
                            if mode == MODE_VOLUME:
                                media_prev()
                            else:
                                hold_steps = max(-5, hold_steps - 1)
                                osd.trigger_signal.emit(f"Шаги (-): {hold_steps}")
                        elif line == "Hold Release":
                            if mode == MODE_BRIGHTNESS and hold_steps != 0:
                                execute_hold_rotation(hold_steps)
                            hold_steps = 0
                    time.sleep(0.01)

                except (serial.SerialException, OSError):
                    is_connected = False
                    if last_status != "disconnected":
                        osd.trigger_signal.emit("Arduino отключено")
                        last_status = "disconnected"
                    break
        except Exception:
            is_connected = False
            if last_status != "waiting":
                osd.trigger_signal.emit("Ожидание...")
                last_status = "waiting"
            time.sleep(2)


# 5. ИНТЕРФЕЙС (OSD, Окно Настроек и Трей)

class TestOSD(QWidget):
    trigger_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.trigger_signal.connect(self.show_osd)
        self.visible_state = False

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.anim = QPropertyAnimation(self, b"pos")
        self.anim.setDuration(250)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)

        self.hide_anim = QPropertyAnimation(self, b"pos")
        self.hide_anim.setDuration(250)
        self.hide_anim.setEasingCurve(QEasingCurve.InCubic)
        self.hide_anim.finished.connect(self.hide)

        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(5)

        self.label = QLabel("")
        self.label.setFont(QFont("Segoe UI", 13, QFont.Bold))
        self.label.setStyleSheet("color: white;")
        self.label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.label)

        self.brightness_bar = QProgressBar()
        self.brightness_bar.setMaximum(100)
        self.brightness_bar.setTextVisible(False)
        self.brightness_bar.setStyleSheet(
            "QProgressBar {background: rgba(0,0,0,0.7); border-radius: 10px;} "
            "QProgressBar::chunk {background: #1E90FF; border-radius: 10px;}"
        )
        layout.addWidget(self.brightness_bar)

        self.volume_bar = QProgressBar()
        self.volume_bar.setMaximum(100)
        self.volume_bar.setTextVisible(False)
        self.volume_bar.setStyleSheet(
            "QProgressBar {background: rgba(0,0,0,0.7); border-radius: 10px;} "
            "QProgressBar::chunk {background: #DE2622; border-radius: 10px;}"
        )
        layout.addWidget(self.volume_bar)

        self.setLayout(layout)
        self.setStyleSheet("QWidget { background-color: rgba(15,15,15,0.7); border-radius: 15px; padding: 10px; }")

        self.hide_timer = QTimer()
        self.hide_timer.setInterval(2000)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_osd_animated)
        self.hide()

    def show_osd(self, text: str):
        self.label.setText(text)
        
        if "Яркость" in text:
            self.brightness_bar.show()
            self.volume_bar.hide()
            try: self.brightness_bar.setValue(int(text.split()[-1].replace("%","")))
            except: pass
        elif "Громкость" in text:
            self.brightness_bar.hide()
            self.volume_bar.show()
            try: self.volume_bar.setValue(int(text.split()[-1].replace("%","")))
            except: pass
        else:
            self.brightness_bar.hide()
            self.volume_bar.hide()

        screen = QApplication.primaryScreen().geometry()
        
        text_width = self.label.fontMetrics().horizontalAdvance(text)
        width = max(220, text_width + 50)
        height = 100 if (self.brightness_bar.isVisible() or self.volume_bar.isVisible()) else 60
        
        y = (screen.height() - height) // 2
        hidden_pos = QPoint(screen.width(), y)
        visible_pos = QPoint(screen.width() - width - 20, y)

        if not self.visible_state:
            self.setGeometry(hidden_pos.x(), hidden_pos.y(), width, height)
            self.show()
            self.anim.stop()
            self.anim.setStartValue(hidden_pos)
            self.anim.setEndValue(visible_pos)
            self.anim.start()
            self.visible_state = True
        else:
            self.setGeometry(visible_pos.x(), visible_pos.y(), width, height)

        self.hide_timer.stop()
        self.hide_timer.start()

    def hide_osd_animated(self):
        if not self.visible_state: return
        screen = QApplication.primaryScreen().geometry()
        self.hide_anim.stop()
        self.hide_anim.setStartValue(self.pos())
        self.hide_anim.setEndValue(QPoint(screen.width(), self.y()))
        self.hide_anim.start()
        self.visible_state = False

def create_default_icon():
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setBrush(QColor("#1E90FF"))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(2, 2, 28, 28)
    painter.end()
    return QIcon(pixmap)

class SettingsWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Настройки контроллера")
        self.resize(520, 650)
        
        main_layout = QVBoxLayout()

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        layout = QVBoxLayout(scroll_content)
        
        # --- Блок COM-порта и Таймаута ---
        port_group = QGroupBox("Настройки подключения (COM-порт)")
        port_layout = QFormLayout()
        
        self.port_combo = QComboBox()
        self.refresh_ports()
        port_layout.addRow("Выберите порт:", self.port_combo)
        
        self.btn_set_default_port = QPushButton("Сделать портом по умолчанию")
        self.btn_set_default_port.clicked.connect(self.set_default_port)
        port_layout.addRow(self.btn_set_default_port)

        # Выбор времени отключения при старте
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(3, 120)
        self.timeout_spin.setSuffix(" сек")
        self.timeout_spin.setValue(config.get("connection_timeout", 15))
        port_layout.addRow("Время ожидания при запуске:", self.timeout_spin)

        port_group.setLayout(port_layout)
        layout.addWidget(port_group)

        # --- Режим 1: Яркость ---
        mode1_group = QGroupBox("Режим 1 (Яркость и Удержания)")
        mode1_layout = QFormLayout()
        
        self.bright_step_spin = QSpinBox()
        self.bright_step_spin.setRange(1, 50)
        self.bright_step_spin.setValue(config.get("brightness_step", 5))
        mode1_layout.addRow("Шаг изменения яркости:", self.bright_step_spin)

        single_hold_layout = QHBoxLayout()
        self.single_hold_input = QLineEdit(config.get("single_hold_action", "win+5"))
        self.single_hold_input.setPlaceholderText("Комбинация (win+5) или путь к .exe")
        
        btn_single_browse = QPushButton("Обзор...")
        btn_single_browse.clicked.connect(lambda: self.browse_file_for_input(self.single_hold_input))
        
        single_hold_layout.addWidget(self.single_hold_input)
        single_hold_layout.addWidget(btn_single_browse)
        
        mode1_layout.addRow("Одиночное удержание (Обязательное):", single_hold_layout)

        self.chk_enable_rotations = QCheckBox("Включить действия при удержании + повороте")
        self.chk_enable_rotations.setChecked(config.get("enable_hold_rotations", True))
        mode1_layout.addRow(self.chk_enable_rotations)

        self.hold_inputs = {}
        actions = config.get("hold_actions", {})
        
        steps = [str(i) for i in range(-5, 6) if i != 0]
        for step in steps:
            lbl = f"Шаг {step}:"
            row_layout = QHBoxLayout()
            
            inp = QLineEdit(actions.get(step, ""))
            inp.setPlaceholderText("Клавиши (win+1) или путь к .exe")
            self.hold_inputs[step] = inp
            
            btn_browse = QPushButton("Обзор...")
            btn_browse.clicked.connect(lambda _, input_field=inp: self.browse_file_for_input(input_field))
            
            row_layout.addWidget(inp)
            row_layout.addWidget(btn_browse)
            
            mode1_layout.addRow(lbl, row_layout)

        mode1_group.setLayout(mode1_layout)
        layout.addWidget(mode1_group)

        # --- Режим 2: Громкость ---
        mode2_group = QGroupBox("Режим 2 (Громкость и Плеер)")
        mode2_layout = QFormLayout()
        
        self.vol_step_spin = QSpinBox()
        self.vol_step_spin.setRange(1, 50)
        self.vol_step_spin.setValue(config.get("volume_step", 2))
        mode2_layout.addRow("Шаг громкости (системный):", self.vol_step_spin)

        exe_layout = QHBoxLayout()
        self.app_exe_input = QLineEdit(config.get("target_app_exe", ""))
        self.app_exe_input.setPlaceholderText("Имя или путь (например YandexMusic.exe)")
        
        self.btn_browse_exe = QPushButton("Обзор...")
        self.btn_browse_exe.clicked.connect(lambda: self.browse_file_for_input(self.app_exe_input))
        
        exe_layout.addWidget(self.app_exe_input)
        exe_layout.addWidget(self.btn_browse_exe)
        
        mode2_layout.addRow("Привязать медиа к .exe:", exe_layout)

        mode2_group.setLayout(mode2_layout)
        layout.addWidget(mode2_group)

        scroll_area.setWidget(scroll_content)
        main_layout.addWidget(scroll_area)

        # --- Сохранить ---
        self.btn_save = QPushButton("Сохранить настройки")
        self.btn_save.setStyleSheet("background-color: #1E90FF; color: white; font-weight: bold; padding: 10px;")
        self.btn_save.clicked.connect(self.save_settings)
        main_layout.addWidget(self.btn_save)

        self.setLayout(main_layout)

        # --- ИНИЦИАЛИЗАЦИЯ ИКОНКИ В ТРЕЕ ---
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(create_default_icon())
        self.tray_icon.setToolTip("Arduino Controller")

        tray_menu = QMenu()
        open_action = QAction("Открыть настройки", self)
        open_action.triggered.connect(self.show_normal_window)
        
        exit_action = QAction("Выйти из программы", self)
        exit_action.triggered.connect(self.force_quit)

        tray_menu.addAction(open_action)
        tray_menu.addSeparator()
        tray_menu.addAction(exit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        self.tray_icon.show()

    def show_normal_window(self):
        self.showNormal()
        self.activateWindow()

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.show_normal_window()

    def changeEvent(self, event):
        if event.type() == event.Type.WindowStateChange:
            if self.windowState() & Qt.WindowMinimized:
                QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent):
        event.ignore()
        self.hide()
        self.tray_icon.showMessage(
            "Arduino Controller",
            "Приложение продолжает работу в фоновом режиме.",
            QSystemTrayIcon.Information,
            2000
        )

    def force_quit(self):
        global running, ser
        running = False
        try:
            if ser and ser.is_open:
                ser.close()
        except: pass
        self.tray_icon.hide()
        QApplication.quit()
        sys.exit(0)

    def browse_file_for_input(self, input_field: QLineEdit):
        file_path, _ = QFileDialog.getOpenFileName(self, "Выберите файл или приложение", "", "Исполняемые файлы (*.exe);;Все файлы (*.*)")
        if file_path:
            input_field.setText(file_path)

    def refresh_ports(self):
        self.port_combo.clear()
        self.port_combo.addItem("Автовыбор (AUTO)", "AUTO")
        ports = serial.tools.list_ports.comports()
        default_p = config.get("default_port", "AUTO")
        
        for p in ports:
            self.port_combo.addItem(f"{p.device} ({p.description})", p.device)
            
        index = self.port_combo.findData(default_p)
        if index != -1:
            self.port_combo.setCurrentIndex(index)

    def set_default_port(self):
        selected_port = self.port_combo.currentData()
        config["default_port"] = selected_port
        save_config(config)
        QMessageBox.information(self, "Успешно", f"Порт {selected_port} установлен по умолчанию!")

    def save_settings(self):
        config["brightness_step"] = self.bright_step_spin.value()
        config["volume_step"] = self.vol_step_spin.value()
        config["connection_timeout"] = self.timeout_spin.value()
        config["target_app_exe"] = self.app_exe_input.text().strip()
        config["default_port"] = self.port_combo.currentData()
        config["single_hold_action"] = self.single_hold_input.text().strip()
        config["enable_hold_rotations"] = self.chk_enable_rotations.isChecked()
        
        for step, inp in self.hold_inputs.items():
            config["hold_actions"][step] = inp.text().strip()

        save_config(config)
        
        global ser
        if ser and ser.is_open:
            try: ser.close()
            except: pass
            
        QMessageBox.information(self, "Успешно", "Настройки сохранены!")
        self.hide()


# 6. ОДНОЭКЗЕМПЛЯРНЫЙ ЗАПУСК И ПРОВЕРКА ТАЙМАУТА

SERVER_NAME = "ArduinoControllerSingleInstanceServer"

class SingleInstanceApp:
    def __init__(self):
        self.socket = QLocalSocket()
        self.socket.connectToServer(SERVER_NAME)
        self.is_running = self.socket.waitForConnected(500)

    def send_show_settings_signal(self):
        if self.is_running:
            self.socket.write(b"SHOW_SETTINGS")
            self.socket.waitForBytesWritten(1000)
            self.socket.disconnectFromServer()

def check_initial_connection():
    if not is_connected:
        global running
        running = False
        osd.trigger_signal.emit("Arduino не найдено. Выход...")
        timeout_val = config.get("connection_timeout", 15)
        print(f"Ошибка: Arduino не подключилось в течение {timeout_val} секунд.")
        QTimer.singleShot(1500, QApplication.quit)


# 7. MAIN RUN

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    single_check = SingleInstanceApp()
    if single_check.is_running:
        single_check.send_show_settings_signal()
        sys.exit(0)

    server = QLocalServer()
    server.listen(SERVER_NAME)

    osd = TestOSD()
    settings_win = SettingsWindow()

    def handle_client_connection():
        client_socket = server.nextPendingConnection()
        if client_socket:
            client_socket.waitForReadyRead(1000)
            msg = client_socket.readAll().data().decode('utf-8')
            if msg == "SHOW_SETTINGS":
                settings_win.refresh_ports()
                settings_win.show_normal_window()
            client_socket.disconnectFromServer()

    server.newConnection.connect(handle_client_connection)

    arduino_thread = threading.Thread(target=arduino_loop, daemon=True)
    arduino_thread.start()

    # Запуск таймера проверки соединения с таймаутом из конфига
    connection_timeout_ms = config.get("connection_timeout", 15) * 1000
    QTimer.singleShot(connection_timeout_ms, check_initial_connection)

    sys.exit(app.exec())
