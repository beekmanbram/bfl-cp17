import copy
import json
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

try:
    import pwd
except ImportError:
    pwd = None

try:
    from gpiozero import DigitalOutputDevice, DigitalInputDevice, Button
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False
    Button = None


# ============================================================
# JOBO FILM PROCESSOR
#
# DFR0601
# PWM1 -> GPIO12
# INA1 -> GPIO6
# INB1 -> GPIO13
#
# ENCODER
# A    -> GPIO16
# B    -> GPIO20
#
# KNOPPEN
# OK    -> GPIO23
# TERUG -> GPIO24
#
# GPIO12 hardware PWM @ 20 kHz
# ============================================================


# ============================================================
# GPIO
# ============================================================

INA_GPIO = 6
INB_GPIO = 13

ENCODER_A_GPIO = 16
ENCODER_B_GPIO = 20

EXTRA_OK_GPIO = 23
EXTRA_BACK_GPIO = 24

PWM_FREQUENCY = 20_000
PWM_PERIOD_NS = 50_000
PWM_CHANNEL = 0


# ============================================================
# GUI
# ============================================================

WIDTH = 800
HEIGHT = 480

BG = "#121212"
TEXT = "#ffffff"
MUTED = "#aaaaaa"

BUTTON_BG = "#333333"

GREEN = "#2e7d32"
RED = "#b71c1c"
BLUE = "#1565c0"
ORANGE = "#ef6c00"
SELECT = "#ffd54f"


# ============================================================
# DUMMY HARDWARE (desktop / zonder GPIO)
# ============================================================

class DummyDigitalOutputDevice:
    def __init__(self, pin, initial_value=False):
        self.pin = pin
        self.value = bool(initial_value)

    def on(self):
        self.value = True

    def off(self):
        self.value = False

    def close(self):
        pass


class DummyInputDevice:
    value = False
    is_pressed = False
    when_activated = None
    when_deactivated = None
    when_pressed = None
    when_released = None
    when_held = None

    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


# ============================================================
# FILE LOCATIONS
# ============================================================

def get_real_home():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and pwd is not None:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except Exception:
            pass
    return Path.home()


def is_raspberry_pi():
    model = Path("/sys/firmware/devicetree/base/model")
    if model.exists():
        try:
            return b"raspberry" in model.read_bytes().lower()
        except Exception:
            pass
    return Path("/sys/class/gpio").exists()


HOME = get_real_home()
RECIPES_FILE = HOME / "jobo-recipes.json"
SETTINGS_FILE = HOME / "jobo-settings.json"
ON_PI = is_raspberry_pi()


# ============================================================
# DEFAULTS
# ============================================================

DEFAULT_SETTINGS = {
    "speed": 30,
    "left_seconds": 10.0,
    "right_seconds": 10.0,
    "direction_delay_ms": 50,
    "encoder_step": 1,
}

DEFAULT_RECIPES = [
    {
        "name": "B&W voorbeeld",
        "speed": 30,
        "left_seconds": 10.0,
        "right_seconds": 10.0,
        "steps": [
            {"name": "Develop", "seconds": 300, "motor": True},
            {"name": "Stop", "seconds": 30, "motor": True},
            {"name": "Fix", "seconds": 300, "motor": True},
            {"name": "Wash", "seconds": 60, "motor": True},
            {"name": "Wash", "seconds": 60, "motor": True},
        ],
    }
]

STEP_NAMES = [
    "Prewash",
    "Develop",
    "Stop",
    "Fix",
    "Bleach",
    "Blix",
    "Wash",
    "Stabilizer",
    "Final rinse",
    "Dry",
    "Custom",
]


# ============================================================
# JSON
# ============================================================

def chown_if_sudo(filename):
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user or pwd is None:
        return
    try:
        user = pwd.getpwnam(sudo_user)
        os.chown(filename, user.pw_uid, user.pw_gid)
    except Exception:
        pass


def load_json(filename, default):
    try:
        if filename.exists():
            with open(filename, "r", encoding="utf-8") as handle:
                return json.load(handle)
    except Exception:
        pass
    return copy.deepcopy(default)


def save_json(filename, data):
    filename = Path(filename)
    tmp = filename.with_name(filename.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, filename)
        chown_if_sudo(filename)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


def clamp(value, minimum, maximum):
    return max(minimum, min(value, maximum))


def snap_to_step(value, minimum, maximum, step):
    value = float(value)
    if step:
        steps = round((value - minimum) / step)
        value = minimum + steps * step
    if isinstance(step, int) and step >= 1:
        value = int(round(value))
    else:
        value = round(value, 4)
    return clamp(value, minimum, maximum)


def normalize_settings(data):
    settings_data = copy.deepcopy(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return settings_data

    try:
        settings_data["speed"] = int(clamp(int(data.get("speed", 30)), 0, 100))
    except (TypeError, ValueError):
        pass

    try:
        settings_data["left_seconds"] = float(
            clamp(float(data.get("left_seconds", 10.0)), 0.5, 60)
        )
    except (TypeError, ValueError):
        pass

    try:
        settings_data["right_seconds"] = float(
            clamp(float(data.get("right_seconds", 10.0)), 0.5, 60)
        )
    except (TypeError, ValueError):
        pass

    try:
        settings_data["direction_delay_ms"] = int(
            clamp(int(data.get("direction_delay_ms", 50)), 20, 500)
        )
    except (TypeError, ValueError):
        pass

    try:
        settings_data["encoder_step"] = int(
            clamp(int(data.get("encoder_step", 1)), 1, 10)
        )
    except (TypeError, ValueError):
        pass

    return settings_data


def normalize_step(step):
    if not isinstance(step, dict):
        return {"name": "Wash", "seconds": 60, "motor": True}

    name = str(step.get("name") or "Custom")
    try:
        seconds = int(clamp(int(step.get("seconds", 60)), 1, 3600))
    except (TypeError, ValueError):
        seconds = 60

    return {
        "name": name,
        "seconds": seconds,
        "motor": bool(step.get("motor", True)),
    }


def normalize_recipe(recipe, fallback_name="Proces"):
    if not isinstance(recipe, dict):
        recipe = {}

    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps:
        steps = [{"name": "Develop", "seconds": 300, "motor": True}]

    try:
        speed = int(clamp(int(recipe.get("speed", settings["speed"])), 0, 100))
    except (TypeError, ValueError):
        speed = settings["speed"]

    try:
        left_seconds = float(
            clamp(float(recipe.get("left_seconds", settings["left_seconds"])), 0.5, 60)
        )
    except (TypeError, ValueError):
        left_seconds = settings["left_seconds"]

    try:
        right_seconds = float(
            clamp(float(recipe.get("right_seconds", settings["right_seconds"])), 0.5, 60)
        )
    except (TypeError, ValueError):
        right_seconds = settings["right_seconds"]

    return {
        "name": str(recipe.get("name") or fallback_name),
        "speed": speed,
        "left_seconds": round(left_seconds, 1),
        "right_seconds": round(right_seconds, 1),
        "steps": [normalize_step(step) for step in steps],
    }


def normalize_recipes(data):
    if not isinstance(data, list) or not data:
        return copy.deepcopy(DEFAULT_RECIPES)
    recipes_data = [
        normalize_recipe(recipe, f"Proces {index + 1}")
        for index, recipe in enumerate(data)
    ]
    return recipes_data or copy.deepcopy(DEFAULT_RECIPES)


settings = normalize_settings(load_json(SETTINGS_FILE, DEFAULT_SETTINGS))
recipes = normalize_recipes(load_json(RECIPES_FILE, DEFAULT_RECIPES))


# ============================================================
# HARDWARE PWM
# ============================================================

class HardwarePWM:
    def __init__(self, channel=0, period_ns=50_000):
        self.channel = channel
        self.period_ns = period_ns
        self._last_duty = None
        self.chip = self.find_pwm_chip()
        self.pwm = self.chip / f"pwm{channel}"
        self.export()
        self.enable = self.pwm / "enable"
        self.period = self.pwm / "period"
        self.duty = self.pwm / "duty_cycle"

        self.write(self.enable, 0)
        self.write(self.duty, 0)
        self.write(self.period, self.period_ns)
        self.write(self.duty, 0)
        self.write(self.enable, 1)
        self._last_duty = 0

    def find_pwm_chip(self):
        chips = sorted(Path("/sys/class/pwm").glob("pwmchip*"))
        for chip in chips:
            try:
                npwm = int((chip / "npwm").read_text().strip())
                if npwm > self.channel:
                    return chip
            except Exception:
                continue
        raise RuntimeError(
            "Hardware PWM niet gevonden.\n"
            "Controleer:\n"
            "dtoverlay=pwm,pin=12,func=4"
        )

    def export(self):
        if self.pwm.exists():
            return
        try:
            self.write(self.chip / "export", self.channel)
        except Exception:
            pass
        for _ in range(50):
            if self.pwm.exists():
                return
            time.sleep(0.02)
        raise RuntimeError("PWM kanaal kon niet worden aangemaakt.")

    @staticmethod
    def write(path, value):
        payload = str(value)
        last_error = None
        for _ in range(5):
            try:
                with open(path, "w") as handle:
                    handle.write(payload)
                return
            except OSError as error:
                last_error = error
                time.sleep(0.01)
        if last_error is not None:
            raise last_error

    def set_percent(self, percent):
        percent = clamp(float(percent), 0.0, 100.0)
        duty = int(self.period_ns * percent / 100.0)
        duty = clamp(duty, 0, self.period_ns)
        if duty == self._last_duty:
            return
        self.write(self.duty, duty)
        self._last_duty = duty

    def stop(self):
        self.set_percent(0)

    def close(self):
        try:
            self.stop()
        except Exception:
            pass
        try:
            self.write(self.enable, 0)
        except Exception:
            pass


class DummyPWM:
    def set_percent(self, percent):
        pass

    def stop(self):
        pass

    def close(self):
        pass


# ============================================================
# MOTOR
# ============================================================

class MotorController:
    def __init__(self, simulated=False):
        self.simulated = simulated
        pin_class = DummyDigitalOutputDevice if simulated else DigitalOutputDevice
        self.ina = pin_class(INA_GPIO, initial_value=False)
        self.inb = pin_class(INB_GPIO, initial_value=False)
        self.pwm = DummyPWM() if simulated else HardwarePWM(PWM_CHANNEL, PWM_PERIOD_NS)

        self.running = False
        self.speed = 0
        self.direction = "LEFT"
        self.switching = False
        self._switch_token = 0
        self._switch_after = None
        self._restore_after = None
        self._pwm_after = None

    def left(self):
        self.ina.off()
        self.inb.on()
        self.direction = "LEFT"

    def right(self):
        self.ina.on()
        self.inb.off()
        self.direction = "RIGHT"

    def coast(self):
        self.ina.off()
        self.inb.off()

    def _cancel_after(self, attr):
        handle = getattr(self, attr)
        if handle is not None:
            try:
                root.after_cancel(handle)
            except Exception:
                pass
            setattr(self, attr, None)

    def _cancel_pending(self):
        self._switch_token += 1
        self.switching = False
        self._cancel_after("_switch_after")
        self._cancel_after("_restore_after")
        self._cancel_after("_pwm_after")

    def _apply_direction(self, direction=None):
        chosen = direction or self.direction or "LEFT"
        if chosen == "LEFT":
            self.left()
        else:
            self.right()

    def start(self, speed, direction=None):
        self._cancel_pending()
        self._apply_direction(direction)
        self.speed = int(clamp(int(speed), 0, 100))
        self.running = True
        self.pwm.set_percent(self.speed)

    def stop(self):
        self._cancel_pending()
        self.running = False
        try:
            self.pwm.stop()
        except Exception:
            pass
        self.coast()

    def set_speed(self, speed):
        self.speed = int(clamp(int(speed), 0, 100))
        if not self.running:
            return
        if self._pwm_after is not None:
            return
        self._pwm_after = root.after(5, self._flush_pwm)

    def _flush_pwm(self):
        self._pwm_after = None
        if self.running and not self.switching:
            try:
                self.pwm.set_percent(self.speed)
            except Exception:
                pass

    def switch_direction(self, direction, finished=None):
        if self.switching or not self.running:
            return

        self.switching = True
        self._switch_token += 1
        token = self._switch_token
        delay = int(settings["direction_delay_ms"])

        try:
            self.pwm.stop()
        except Exception:
            pass

        def restore():
            self._restore_after = None
            if token != self._switch_token:
                return
            if self.running:
                try:
                    self.pwm.set_percent(self.speed)
                except Exception:
                    pass
            self.switching = False
            if finished:
                finished()

        def change():
            self._switch_after = None
            if token != self._switch_token or not self.running:
                self.switching = False
                return
            self._apply_direction(direction)
            self._restore_after = root.after(10, restore)

        self._switch_after = root.after(delay, change)

    def close(self):
        try:
            self.stop()
        except Exception:
            pass
        try:
            self.pwm.close()
        except Exception:
            pass
        try:
            self.ina.close()
            self.inb.close()
        except Exception:
            pass


# ============================================================
# GLOBAL STATE
# ============================================================

root = None
content = None
temp_label = None
motor = None
simulated_hardware = False
hardware_error = None

shutdown_event = threading.Event()
temp_thread = None
after_ids = {}

nav_items = []
nav_index = 0

adjust_mode = False
adjust_value = 0
adjust_min = 0
adjust_max = 100
adjust_step = 1
adjust_callback = None
adjust_cancel_callback = None
adjust_formatter = None
adjust_label = None

choice_mode = False
choice_values = []
choice_index = 0
choice_callback = None
choice_cancel_callback = None
choice_label = None

encoder_lock = threading.Lock()
pending_encoder_steps = 0
encoder_flush_scheduled = False
encoder_quad_state = 0
encoder_quad_accum = 0

# KY-040: 4 flanken per detent = 1 draaistap.
ENCODER_DETENT_TICKS = 4

QUAD_DELTA = {
    0b0001: 1,
    0b0111: 1,
    0b1110: 1,
    0b1000: 1,
    0b0010: -1,
    0b1011: -1,
    0b1101: -1,
    0b0100: -1,
}

manual_active = False
manual_next_switch = None
manual_speed = settings["speed"]
manual_direction_label = None
manual_speed_label = None
manual_start_button = None

current_recipe_index = 0
editor_recipe_index = 0
editor_step_index = 0

run_recipe = None
run_step_index = 0
run_active = False
run_paused = False
run_step_finished = False
run_remaining = 0.0
run_last_tick = 0.0
run_next_direction = None
run_switch_remaining = None

process_timer_label = None
process_motor_label = None
process_status_label = None
process_main_button = None

encoder_a = None
encoder_b = None
extra_ok_button = None
extra_back_button = None


# ============================================================
# HELPERS
# ============================================================

def cancel_after(key):
    handle = after_ids.pop(key, None)
    if handle is not None:
        try:
            root.after_cancel(handle)
        except Exception:
            pass


def schedule(key, delay_ms, callback):
    cancel_after(key)
    after_ids[key] = root.after(delay_ms, callback)


def safe_config(widget, **kwargs):
    try:
        if widget is not None and widget.winfo_exists():
            widget.config(**kwargs)
            return True
    except tk.TclError:
        pass
    return False


def stop_all_activity():
    global manual_active, run_active, run_paused, run_step_finished
    global run_next_direction, run_switch_remaining, manual_next_switch

    manual_active = False
    run_active = False
    run_paused = False
    run_step_finished = False
    run_next_direction = None
    run_switch_remaining = None
    manual_next_switch = None

    cancel_after("manual_tick")
    cancel_after("process_tick")

    if motor is not None:
        motor.stop()


# ============================================================
# TEMPERATURE
# ============================================================

def read_temperature():
    sensors = list(Path("/sys/bus/w1/devices").glob("28-*"))
    if not sensors:
        return None
    try:
        text = (sensors[0] / "w1_slave").read_text()
        if "YES" not in text:
            return None
        temp_text = text.split("t=")[-1]
        return float(temp_text) / 1000.0
    except Exception:
        return None


def apply_temperature_text(text):
    safe_config(temp_label, text=text)


def temperature_loop():
    while not shutdown_event.is_set():
        temp = read_temperature()
        if temp is None:
            text = "Temp. --.-°C"
        else:
            text = f"Temp. {temp:.1f}°C"
        try:
            if root is not None:
                root.after(0, apply_temperature_text, text)
        except Exception:
            break
        shutdown_event.wait(2.0)


# ============================================================
# GUI HELPERS
# ============================================================

def clear_screen():
    global nav_items, nav_index
    global adjust_mode, choice_mode

    for widget in content.winfo_children():
        widget.destroy()

    nav_items = []
    nav_index = 0
    adjust_mode = False
    choice_mode = False


def make_button(
    parent,
    text,
    command,
    width=16,
    bg=BUTTON_BG,
    font_size=16,
    height=2,
):
    return tk.Button(
        parent,
        text=text,
        command=command,
        font=("Arial", font_size, "bold"),
        width=width,
        height=height,
        bg=bg,
        fg=TEXT,
        activebackground="#555555",
        activeforeground=TEXT,
        relief=tk.FLAT,
        highlightthickness=0,
        takefocus=0,
    )


def set_navigation(items, selected=0):
    global nav_items, nav_index
    nav_items = [item for item in items if item is not None]
    if not nav_items:
        return
    nav_index = clamp(selected, 0, len(nav_items) - 1)
    update_navigation()


def update_navigation():
    for index, widget in enumerate(nav_items):
        if index == nav_index:
            safe_config(
                widget,
                highlightthickness=4,
                highlightbackground=SELECT,
                highlightcolor=SELECT,
            )
        else:
            safe_config(widget, highlightthickness=0)


def nav_move(steps):
    global nav_index
    if not nav_items or not steps:
        return
    nav_index = (nav_index + int(steps)) % len(nav_items)
    update_navigation()


def nav_activate():
    if not nav_items:
        return
    try:
        nav_items[nav_index].invoke()
    except Exception:
        pass


# ============================================================
# FORMAT
# ============================================================

def format_time(seconds):
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_percent(value):
    return f"{int(value)}%"


def format_seconds(value):
    return f"{value:.1f} sec"


def format_ms(value):
    return f"{int(value)} ms"


# ============================================================
# VALUE ADJUSTMENT
# ============================================================

def open_adjust(
    title,
    value,
    minimum,
    maximum,
    step,
    callback,
    cancel_callback,
    formatter=None,
):
    global adjust_mode, adjust_value, adjust_min, adjust_max, adjust_step
    global adjust_callback, adjust_cancel_callback, adjust_formatter, adjust_label

    adjust_mode = True
    adjust_min = minimum
    adjust_max = maximum
    adjust_step = step
    adjust_value = snap_to_step(value, minimum, maximum, step)
    adjust_callback = callback
    adjust_cancel_callback = cancel_callback
    adjust_formatter = formatter if formatter else str

    clear_screen()
    adjust_mode = True

    tk.Label(
        content,
        text=title,
        font=("Arial", 25, "bold"),
        bg=BG,
        fg=TEXT,
    ).pack(pady=(60, 25))

    adjust_label = tk.Label(
        content,
        text=adjust_formatter(adjust_value),
        font=("Arial", 50, "bold"),
        bg=BG,
        fg=SELECT,
    )
    adjust_label.pack(pady=20)

    row = tk.Frame(content, bg=BG)
    row.pack(pady=20)

    minus = make_button(row, "−", lambda: adjust_change(-1), width=5, font_size=26)
    minus.pack(side=tk.LEFT, padx=10)

    okay = make_button(row, "OK", adjust_confirm, width=8, bg=GREEN)
    okay.pack(side=tk.LEFT, padx=10)

    plus = make_button(row, "+", lambda: adjust_change(1), width=5, font_size=26)
    plus.pack(side=tk.LEFT, padx=10)

    cancel = make_button(content, "ANNULEREN", adjust_cancel, width=12)
    cancel.pack(pady=5)

    set_navigation([minus, okay, plus, cancel], 1)


def adjust_change(steps):
    global adjust_value
    if not adjust_mode:
        return
    adjust_value = snap_to_step(
        adjust_value + (steps * adjust_step),
        adjust_min,
        adjust_max,
        adjust_step,
    )
    safe_config(adjust_label, text=adjust_formatter(adjust_value))


def adjust_confirm():
    global adjust_mode
    callback = adjust_callback
    value = adjust_value
    adjust_mode = False
    if callback:
        callback(value)


def adjust_cancel():
    global adjust_mode
    callback = adjust_cancel_callback
    adjust_mode = False
    if callback:
        callback()


# ============================================================
# CHOICE
# ============================================================

def open_choice(title, values, current_value, callback, cancel_callback):
    global choice_mode, choice_values, choice_index
    global choice_callback, choice_cancel_callback, choice_label

    choice_mode = True
    choice_values = list(values)
    choice_callback = callback
    choice_cancel_callback = cancel_callback

    try:
        choice_index = choice_values.index(current_value)
    except ValueError:
        choice_index = 0

    clear_screen()
    choice_mode = True

    tk.Label(
        content,
        text=title,
        font=("Arial", 25, "bold"),
        bg=BG,
        fg=TEXT,
    ).pack(pady=(60, 25))

    choice_label = tk.Label(
        content,
        text=choice_values[choice_index] if choice_values else "",
        font=("Arial", 42, "bold"),
        bg=BG,
        fg=SELECT,
    )
    choice_label.pack(pady=30)

    okay = make_button(content, "OK", choice_confirm, width=12, bg=GREEN)
    okay.pack(pady=8)

    cancel = make_button(content, "ANNULEREN", choice_cancel, width=12)
    cancel.pack(pady=5)

    set_navigation([okay, cancel])


def choice_change(steps):
    global choice_index
    if not choice_mode or not choice_values:
        return
    choice_index = (choice_index + int(steps)) % len(choice_values)
    safe_config(choice_label, text=choice_values[choice_index])


def choice_confirm():
    global choice_mode
    callback = choice_callback
    value = choice_values[choice_index]
    choice_mode = False
    if callback:
        callback(value)


def choice_cancel():
    global choice_mode
    callback = choice_cancel_callback
    choice_mode = False
    if callback:
        callback()


# ============================================================
# MAIN MENU
# ============================================================

def show_main_menu():
    stop_all_activity()
    clear_screen()

    tk.Label(
        content,
        text="JOBO PROCESSOR",
        font=("Arial", 32, "bold"),
        fg=TEXT,
        bg=BG,
    ).pack(pady=(35, 25))

    if simulated_hardware:
        message = "SIMULATIE — geen GPIO/PWM"
        if hardware_error:
            message = f"HARDWARE FOUT: {hardware_error}"
        tk.Label(
            content,
            text=message,
            font=("Arial", 12, "bold"),
            fg=ORANGE,
            bg=BG,
            wraplength=720,
            justify=tk.CENTER,
        ).pack(pady=(0, 8))

    manual = make_button(
        content, "▶ ONTWIKKELEN", show_manual, width=22, bg=GREEN, font_size=19
    )
    manual.pack(pady=7)

    process = make_button(
        content, "PROCESSEN", show_process_select, width=22, bg=BLUE, font_size=19
    )
    process.pack(pady=7)

    setting = make_button(
        content, "INSTELLINGEN", show_settings, width=22, font_size=19
    )
    setting.pack(pady=7)

    quit_button = make_button(
        content, "AFSLUITEN", close_app, width=15, bg=RED, font_size=13
    )
    quit_button.pack(pady=16)

    set_navigation([manual, process, setting, quit_button])


# ============================================================
# SETTINGS
# ============================================================

def show_settings():
    clear_screen()

    tk.Label(
        content,
        text="INSTELLINGEN",
        font=("Arial", 27, "bold"),
        bg=BG,
        fg=TEXT,
    ).pack(pady=(15, 12))

    speed = make_button(
        content,
        f"Standaard snelheid   {settings['speed']}%",
        settings_speed,
        width=28,
        font_size=14,
    )
    speed.pack(pady=4)

    left = make_button(
        content,
        f"Linksom tijd   {settings['left_seconds']} sec",
        settings_left,
        width=28,
        font_size=14,
    )
    left.pack(pady=4)

    right = make_button(
        content,
        f"Rechtsom tijd   {settings['right_seconds']} sec",
        settings_right,
        width=28,
        font_size=14,
    )
    right.pack(pady=4)

    delay = make_button(
        content,
        f"Richtingswissel   {settings['direction_delay_ms']} ms",
        settings_delay,
        width=28,
        font_size=14,
    )
    delay.pack(pady=4)

    encoder = make_button(
        content,
        f"Encoder stap   {settings['encoder_step']}%",
        settings_encoder_step,
        width=28,
        font_size=14,
    )
    encoder.pack(pady=4)

    back = make_button(
        content,
        "OPSLAAN & TERUG",
        save_settings_and_back,
        width=18,
        bg=GREEN,
        font_size=13,
    )
    back.pack(pady=12)

    set_navigation([speed, left, right, delay, encoder, back])


def settings_speed():
    open_adjust(
        "Standaard motorsnelheid",
        settings["speed"],
        0,
        100,
        1,
        save_setting_speed,
        show_settings,
        format_percent,
    )


def save_setting_speed(value):
    settings["speed"] = int(value)
    show_settings()


def settings_left():
    open_adjust(
        "Standaard linksom",
        settings["left_seconds"],
        0.5,
        60,
        0.5,
        save_setting_left,
        show_settings,
        format_seconds,
    )


def save_setting_left(value):
    settings["left_seconds"] = round(value, 1)
    show_settings()


def settings_right():
    open_adjust(
        "Standaard rechtsom",
        settings["right_seconds"],
        0.5,
        60,
        0.5,
        save_setting_right,
        show_settings,
        format_seconds,
    )


def save_setting_right(value):
    settings["right_seconds"] = round(value, 1)
    show_settings()


def settings_delay():
    open_adjust(
        "Richtingswissel",
        settings["direction_delay_ms"],
        20,
        500,
        10,
        save_setting_delay,
        show_settings,
        format_ms,
    )


def save_setting_delay(value):
    settings["direction_delay_ms"] = int(value)
    show_settings()


def settings_encoder_step():
    open_adjust(
        "Encoder snelheidsstap",
        settings["encoder_step"],
        1,
        10,
        1,
        save_setting_encoder,
        show_settings,
        format_percent,
    )


def save_setting_encoder(value):
    settings["encoder_step"] = int(value)
    show_settings()


def save_settings_and_back():
    save_json(SETTINGS_FILE, settings)
    show_main_menu()


# ============================================================
# MANUAL DEVELOP MODE
# ============================================================

def show_manual():
    global manual_active, manual_next_switch, manual_speed
    global manual_direction_label, manual_speed_label, manual_start_button

    stop_all_activity()
    manual_speed = settings["speed"]

    clear_screen()

    tk.Label(
        content,
        text="HANDMATIG ONTWIKKELEN",
        font=("Arial", 25, "bold"),
        fg=TEXT,
        bg=BG,
    ).pack(pady=(20, 8))

    manual_direction_label = tk.Label(
        content,
        text="MOTOR UIT",
        font=("Arial", 18, "bold"),
        fg=MUTED,
        bg=BG,
    )
    manual_direction_label.pack(pady=5)

    manual_speed_label = tk.Label(
        content,
        text=f"{manual_speed}%",
        font=("Arial", 48, "bold"),
        fg=SELECT,
        bg=BG,
    )
    manual_speed_label.pack(pady=10)

    tk.Label(
        content,
        text=f"◀ {settings['left_seconds']} sec     ▶ {settings['right_seconds']} sec",
        font=("Arial", 15),
        fg=TEXT,
        bg=BG,
    ).pack(pady=4)

    manual_start_button = make_button(
        content, "START", toggle_manual_motor, width=15, bg=GREEN, font_size=20
    )
    manual_start_button.pack(pady=15)

    back = make_button(content, "TERUG", leave_manual, width=12, font_size=13)
    back.pack(pady=5)

    set_navigation([manual_start_button, back])


def toggle_manual_motor():
    global manual_active, manual_next_switch

    if manual_active:
        manual_active = False
        manual_next_switch = None
        cancel_after("manual_tick")
        motor.stop()
        safe_config(manual_start_button, text="START", bg=GREEN)
        safe_config(manual_direction_label, text="MOTOR UIT", fg=MUTED)
        return

    manual_active = True
    motor.start(manual_speed, "LEFT")
    manual_next_switch = time.monotonic() + settings["left_seconds"]
    safe_config(manual_start_button, text="STOP", bg=RED)
    safe_config(manual_direction_label, text="◀ LINKS", fg=TEXT)
    manual_tick()


def update_manual_direction_label():
    if motor.direction == "LEFT":
        safe_config(manual_direction_label, text="◀ LINKS", fg=TEXT)
    else:
        safe_config(manual_direction_label, text="RECHTS ▶", fg=TEXT)


def manual_tick():
    global manual_next_switch

    after_ids.pop("manual_tick", None)
    if not manual_active:
        return

    now = time.monotonic()
    if (
        manual_next_switch
        and now >= manual_next_switch
        and not motor.switching
    ):
        if motor.direction == "LEFT":
            direction = "RIGHT"
            duration = settings["right_seconds"]
        else:
            direction = "LEFT"
            duration = settings["left_seconds"]

        motor.switch_direction(direction, update_manual_direction_label)
        manual_next_switch = now + duration

    schedule("manual_tick", 50, manual_tick)


def manual_speed_change(steps):
    global manual_speed
    manual_speed = int(
        clamp(manual_speed + (steps * settings["encoder_step"]), 0, 100)
    )
    motor.set_speed(manual_speed)
    safe_config(manual_speed_label, text=f"{manual_speed}%")


def leave_manual():
    stop_all_activity()
    show_main_menu()


# ============================================================
# PROCESS SELECT
# ============================================================

def ensure_recipes():
    global recipes, current_recipe_index
    if not recipes:
        recipes.append(copy.deepcopy(DEFAULT_RECIPES[0]))
    current_recipe_index %= len(recipes)


def show_process_select():
    stop_all_activity()
    ensure_recipes()
    clear_screen()

    recipe = recipes[current_recipe_index]

    tk.Label(
        content,
        text="PROCESSEN",
        font=("Arial", 26, "bold"),
        fg=TEXT,
        bg=BG,
    ).pack(pady=(18, 5))

    tk.Label(
        content,
        text=f"{current_recipe_index + 1} / {len(recipes)}",
        font=("Arial", 13),
        fg=MUTED,
        bg=BG,
    ).pack()

    tk.Label(
        content,
        text=recipe["name"],
        font=("Arial", 30, "bold"),
        fg=SELECT,
        bg=BG,
    ).pack(pady=15)

    row = tk.Frame(content, bg=BG)
    row.pack()

    previous = make_button(row, "◀", previous_recipe, width=5)
    previous.pack(side=tk.LEFT, padx=8)

    next_button = make_button(row, "▶", next_recipe, width=5)
    next_button.pack(side=tk.LEFT, padx=8)

    row2 = tk.Frame(content, bg=BG)
    row2.pack(pady=12)

    start = make_button(row2, "START PROCES", start_selected_recipe, width=13, bg=GREEN)
    start.pack(side=tk.LEFT, padx=5)

    edit = make_button(row2, "BEWERK", edit_selected_recipe, width=10, bg=BLUE)
    edit.pack(side=tk.LEFT, padx=5)

    new = make_button(row2, "+ NIEUW", add_recipe, width=10)
    new.pack(side=tk.LEFT, padx=5)

    row3 = tk.Frame(content, bg=BG)
    row3.pack(pady=3)

    delete = make_button(
        row3, "VERWIJDER", delete_recipe, width=10, bg=RED, font_size=12
    )
    delete.pack(side=tk.LEFT, padx=5)

    back = make_button(row3, "TERUG", show_main_menu, width=10, font_size=12)
    back.pack(side=tk.LEFT, padx=5)

    set_navigation([previous, next_button, start, edit, new, delete, back], 2)


def previous_recipe():
    global current_recipe_index
    ensure_recipes()
    current_recipe_index = (current_recipe_index - 1) % len(recipes)
    show_process_select()


def next_recipe():
    global current_recipe_index
    ensure_recipes()
    current_recipe_index = (current_recipe_index + 1) % len(recipes)
    show_process_select()


def add_recipe():
    global current_recipe_index, editor_recipe_index, editor_step_index

    recipe = {
        "name": f"Proces {len(recipes) + 1}",
        "speed": settings["speed"],
        "left_seconds": settings["left_seconds"],
        "right_seconds": settings["right_seconds"],
        "steps": [{"name": "Develop", "seconds": 300, "motor": True}],
    }
    recipes.append(recipe)
    current_recipe_index = len(recipes) - 1
    editor_recipe_index = current_recipe_index
    editor_step_index = 0
    show_process_editor()


def delete_recipe():
    global current_recipe_index
    if len(recipes) <= 1:
        return
    del recipes[current_recipe_index]
    current_recipe_index = min(current_recipe_index, len(recipes) - 1)
    save_json(RECIPES_FILE, recipes)
    show_process_select()


def edit_selected_recipe():
    global editor_recipe_index, editor_step_index
    editor_recipe_index = current_recipe_index
    editor_step_index = 0
    show_process_editor()


# ============================================================
# PROCESS EDITOR
# ============================================================

def show_process_editor():
    global editor_step_index

    recipe = recipes[editor_recipe_index]
    editor_step_index = clamp(editor_step_index, 0, len(recipe["steps"]) - 1)
    step = recipe["steps"][editor_step_index]

    clear_screen()

    tk.Label(
        content,
        text=recipe["name"],
        font=("Arial", 23, "bold"),
        fg=TEXT,
        bg=BG,
    ).pack(pady=(8, 3))

    top = tk.Frame(content, bg=BG)
    top.pack(pady=2)

    speed = make_button(
        top,
        f"Snelheid\n{recipe['speed']}%",
        edit_recipe_speed,
        width=9,
        font_size=12,
    )
    speed.pack(side=tk.LEFT, padx=4)

    left = make_button(
        top,
        f"Links\n{recipe['left_seconds']}s",
        edit_recipe_left,
        width=9,
        font_size=12,
    )
    left.pack(side=tk.LEFT, padx=4)

    right = make_button(
        top,
        f"Rechts\n{recipe['right_seconds']}s",
        edit_recipe_right,
        width=9,
        font_size=12,
    )
    right.pack(side=tk.LEFT, padx=4)

    tk.Label(
        content,
        text=f"STAP {editor_step_index + 1} / {len(recipe['steps'])}",
        font=("Arial", 13),
        fg=MUTED,
        bg=BG,
    ).pack(pady=(8, 2))

    edit_step_button = make_button(
        content,
        f"{step['name']}    {format_time(step['seconds'])}",
        show_step_editor,
        width=24,
        bg=BLUE,
        font_size=17,
    )
    edit_step_button.pack(pady=4)

    nav = tk.Frame(content, bg=BG)
    nav.pack(pady=2)

    previous = make_button(nav, "◀ VORIGE", previous_step, width=10, font_size=11)
    previous.pack(side=tk.LEFT, padx=4)

    next_button = make_button(nav, "VOLGENDE ▶", next_step, width=10, font_size=11)
    next_button.pack(side=tk.LEFT, padx=4)

    actions = tk.Frame(content, bg=BG)
    actions.pack(pady=2)

    add = make_button(actions, "+ STAP", add_step, width=8, bg=GREEN, font_size=11)
    add.pack(side=tk.LEFT, padx=3)

    delete = make_button(actions, "− STAP", delete_step, width=8, bg=RED, font_size=11)
    delete.pack(side=tk.LEFT, padx=3)

    save = make_button(actions, "OPSLAAN", save_process, width=9, bg=ORANGE, font_size=11)
    save.pack(side=tk.LEFT, padx=3)

    back = make_button(content, "TERUG", show_process_select, width=10, font_size=10)
    back.pack(pady=2)

    set_navigation(
        [
            speed,
            left,
            right,
            edit_step_button,
            previous,
            next_button,
            add,
            delete,
            save,
            back,
        ]
    )


def current_editor_recipe():
    return recipes[editor_recipe_index]


def edit_recipe_speed():
    open_adjust(
        "Proces snelheid",
        current_editor_recipe()["speed"],
        0,
        100,
        1,
        save_recipe_speed,
        show_process_editor,
        format_percent,
    )


def save_recipe_speed(value):
    current_editor_recipe()["speed"] = int(value)
    show_process_editor()


def edit_recipe_left():
    open_adjust(
        "Linksom tijd",
        current_editor_recipe()["left_seconds"],
        0.5,
        60,
        0.5,
        save_recipe_left,
        show_process_editor,
        format_seconds,
    )


def save_recipe_left(value):
    current_editor_recipe()["left_seconds"] = round(value, 1)
    show_process_editor()


def edit_recipe_right():
    open_adjust(
        "Rechtsom tijd",
        current_editor_recipe()["right_seconds"],
        0.5,
        60,
        0.5,
        save_recipe_right,
        show_process_editor,
        format_seconds,
    )


def save_recipe_right(value):
    current_editor_recipe()["right_seconds"] = round(value, 1)
    show_process_editor()


def previous_step():
    global editor_step_index
    steps = current_editor_recipe()["steps"]
    editor_step_index = (editor_step_index - 1) % len(steps)
    show_process_editor()


def next_step():
    global editor_step_index
    steps = current_editor_recipe()["steps"]
    editor_step_index = (editor_step_index + 1) % len(steps)
    show_process_editor()


def add_step():
    global editor_step_index
    recipe = current_editor_recipe()
    recipe["steps"].append({"name": "Wash", "seconds": 60, "motor": True})
    editor_step_index = len(recipe["steps"]) - 1
    show_process_editor()


def delete_step():
    global editor_step_index
    recipe = current_editor_recipe()
    if len(recipe["steps"]) <= 1:
        return
    del recipe["steps"][editor_step_index]
    editor_step_index = min(editor_step_index, len(recipe["steps"]) - 1)
    show_process_editor()


def save_process():
    save_json(RECIPES_FILE, recipes)
    show_process_select()


# ============================================================
# STEP EDITOR
# ============================================================

def current_step():
    return recipes[editor_recipe_index]["steps"][editor_step_index]


def show_step_editor():
    step = current_step()
    clear_screen()

    tk.Label(
        content,
        text=f"STAP {editor_step_index + 1}",
        font=("Arial", 26, "bold"),
        bg=BG,
        fg=TEXT,
    ).pack(pady=(35, 15))

    name = make_button(
        content,
        "Naam: " + step["name"],
        edit_step_name,
        width=24,
        bg=BLUE,
    )
    name.pack(pady=6)

    duration = make_button(
        content,
        "Tijd: " + format_time(step["seconds"]),
        edit_step_time,
        width=24,
    )
    duration.pack(pady=6)

    motor_on = step.get("motor", True)
    motor_button = make_button(
        content,
        "Motor: " + ("AAN" if motor_on else "UIT"),
        toggle_step_motor,
        width=24,
    )
    motor_button.pack(pady=6)

    done = make_button(content, "KLAAR", show_process_editor, width=14, bg=GREEN)
    done.pack(pady=15)

    set_navigation([name, duration, motor_button, done])


def edit_step_name():
    open_choice(
        "Stapnaam",
        STEP_NAMES,
        current_step()["name"],
        save_step_name,
        show_step_editor,
    )


def save_step_name(value):
    current_step()["name"] = value
    show_step_editor()


def edit_step_time():
    open_adjust(
        "Stapduur",
        current_step()["seconds"],
        1,
        3600,
        5,
        save_step_time,
        show_step_editor,
        format_time,
    )


def save_step_time(value):
    current_step()["seconds"] = int(value)
    show_step_editor()


def toggle_step_motor():
    step = current_step()
    step["motor"] = not step.get("motor", True)
    show_step_editor()


# ============================================================
# PROCESS RUNNING
# ============================================================

def start_selected_recipe():
    global run_recipe, run_step_index
    run_recipe = copy.deepcopy(recipes[current_recipe_index])
    run_step_index = 0
    prepare_process_step()


def prepare_process_step():
    global run_active, run_paused, run_step_finished
    global run_remaining, run_next_direction, run_switch_remaining

    motor.stop()
    cancel_after("process_tick")

    run_active = False
    run_paused = False
    run_step_finished = False
    run_remaining = float(run_recipe["steps"][run_step_index]["seconds"])
    run_next_direction = None
    run_switch_remaining = None

    show_process_run_screen(waiting=True)


def show_process_run_screen(waiting=False):
    global process_timer_label, process_motor_label
    global process_status_label, process_main_button

    step = run_recipe["steps"][run_step_index]
    clear_screen()

    tk.Label(
        content,
        text=run_recipe["name"],
        font=("Arial", 17, "bold"),
        bg=BG,
        fg=MUTED,
    ).pack(pady=(7, 0))

    tk.Label(
        content,
        text=f"STAP {run_step_index + 1} / {len(run_recipe['steps'])}",
        font=("Arial", 13),
        bg=BG,
        fg=MUTED,
    ).pack()

    tk.Label(
        content,
        text=step["name"].upper(),
        font=("Arial", 28, "bold"),
        bg=BG,
        fg=TEXT,
    ).pack(pady=(5, 0))

    process_timer_label = tk.Label(
        content,
        text=format_time(run_remaining),
        font=("Arial", 60, "bold"),
        bg=BG,
        fg=SELECT,
    )
    process_timer_label.pack(pady=4)

    process_motor_label = tk.Label(
        content,
        text=process_motor_text(),
        font=("Arial", 13),
        bg=BG,
        fg=TEXT,
    )
    process_motor_label.pack(pady=2)

    process_status_label = tk.Label(
        content,
        text="KLAAR OM TE STARTEN" if waiting else "ACTIEF",
        font=("Arial", 15, "bold"),
        bg=BG,
        fg=MUTED,
    )
    process_status_label.pack(pady=3)

    row = tk.Frame(content, bg=BG)
    row.pack(pady=5)

    if waiting:
        process_main_button = make_button(
            row, "START STAP", start_process_step, width=13, bg=GREEN
        )
    else:
        process_main_button = make_button(
            row, "PAUZE", toggle_process_pause, width=13, bg=ORANGE
        )
    process_main_button.pack(side=tk.LEFT, padx=7)

    stop = make_button(row, "STOP PROCES", stop_process, width=13, bg=RED)
    stop.pack(side=tk.LEFT, padx=7)

    set_navigation([process_main_button, stop])


def process_motor_text():
    if not motor.running:
        direction = "UIT"
    elif motor.direction == "LEFT":
        direction = "◀"
    else:
        direction = "▶"

    return (
        f"Motor {direction}   {run_recipe['speed']}%   |   "
        f"◀ {run_recipe['left_seconds']}s   ▶ {run_recipe['right_seconds']}s"
    )


def current_direction_duration():
    if motor.direction == "LEFT":
        return run_recipe["left_seconds"]
    return run_recipe["right_seconds"]


def start_process_step():
    global run_active, run_paused, run_last_tick, run_next_direction

    run_active = True
    run_paused = False
    run_last_tick = time.monotonic()

    step = run_recipe["steps"][run_step_index]
    if step.get("motor", True):
        motor.start(run_recipe["speed"], "LEFT")
        run_next_direction = time.monotonic() + run_recipe["left_seconds"]
    else:
        motor.stop()
        run_next_direction = None

    show_process_run_screen(waiting=False)
    process_tick()


def process_tick():
    global run_remaining, run_last_tick, run_next_direction

    after_ids.pop("process_tick", None)
    if not run_active:
        return

    now = time.monotonic()

    if not run_paused:
        elapsed = now - run_last_tick
        run_last_tick = now
        run_remaining -= elapsed

        if run_remaining <= 0:
            run_remaining = 0
            finish_process_step()
            return

        safe_config(
            process_timer_label,
            text=format_time(run_remaining),
            fg=ORANGE if run_remaining <= 10 else SELECT,
        )

        step = run_recipe["steps"][run_step_index]
        if (
            step.get("motor", True)
            and run_next_direction
            and now >= run_next_direction
            and not motor.switching
        ):
            if motor.direction == "LEFT":
                direction = "RIGHT"
                duration = run_recipe["right_seconds"]
            else:
                direction = "LEFT"
                duration = run_recipe["left_seconds"]

            motor.switch_direction(direction, lambda: safe_config(
                process_motor_label, text=process_motor_text()
            ))
            run_next_direction = now + duration

        safe_config(process_motor_label, text=process_motor_text())

    schedule("process_tick", 50, process_tick)


def toggle_process_pause():
    global run_paused, run_last_tick, run_next_direction, run_switch_remaining

    if not run_active:
        return

    run_paused = not run_paused

    if run_paused:
        if run_next_direction:
            run_switch_remaining = max(0.0, run_next_direction - time.monotonic())
        else:
            run_switch_remaining = None
        motor.stop()
        safe_config(process_status_label, text="GEPAUZEERD", fg=ORANGE)
        safe_config(process_main_button, text="VERDER", bg=GREEN)
        safe_config(process_motor_label, text=process_motor_text())
        return

    run_last_tick = time.monotonic()
    step = run_recipe["steps"][run_step_index]

    if step.get("motor", True):
        motor.start(run_recipe["speed"], motor.direction or "LEFT")
        remaining = run_switch_remaining
        if remaining is None:
            remaining = current_direction_duration()
        run_next_direction = time.monotonic() + remaining
    else:
        run_next_direction = None

    safe_config(process_status_label, text="ACTIEF", fg=TEXT)
    safe_config(process_main_button, text="PAUZE", bg=ORANGE)
    safe_config(process_motor_label, text=process_motor_text())


def finish_process_step():
    global run_active, run_step_finished
    run_active = False
    run_step_finished = True
    cancel_after("process_tick")
    motor.stop()
    show_step_finished()


def process_has_next_step():
    return run_recipe is not None and run_step_index + 1 < len(run_recipe["steps"])


def show_step_finished():
    step = run_recipe["steps"][run_step_index]
    clear_screen()

    tk.Label(
        content,
        text="✓ STAP KLAAR",
        font=("Arial", 32, "bold"),
        bg=BG,
        fg=GREEN,
    ).pack(pady=(55, 12))

    tk.Label(
        content,
        text=step["name"].upper(),
        font=("Arial", 26, "bold"),
        bg=BG,
        fg=TEXT,
    ).pack(pady=5)

    if process_has_next_step():
        next_step_data = run_recipe["steps"][run_step_index + 1]
        tk.Label(
            content,
            text=f"Volgende:\n{next_step_data['name']}   {format_time(next_step_data['seconds'])}",
            font=("Arial", 18),
            bg=BG,
            fg=MUTED,
        ).pack(pady=15)

        next_button = make_button(
            content, "START VOLGENDE", next_process_step, width=18, bg=GREEN
        )
        next_button.pack(pady=7)

        stop = make_button(
            content, "STOP PROCES", stop_process, width=14, bg=RED, font_size=12
        )
        stop.pack(pady=3)
        set_navigation([next_button, stop])
        return

    tk.Label(
        content,
        text="PROCES VOLTOOID",
        font=("Arial", 22, "bold"),
        bg=BG,
        fg=SELECT,
    ).pack(pady=20)

    done = make_button(content, "KLAAR", show_main_menu, width=14, bg=GREEN)
    done.pack()
    set_navigation([done])


def next_process_step():
    global run_step_index, run_step_finished
    if not process_has_next_step():
        show_main_menu()
        return
    run_step_finished = False
    run_step_index += 1
    prepare_process_step()


def stop_process():
    stop_all_activity()
    show_main_menu()


def change_process_speed(steps):
    if run_recipe is None:
        return
    new_speed = int(
        clamp(run_recipe["speed"] + (steps * settings["encoder_step"]), 0, 100)
    )
    run_recipe["speed"] = new_speed
    motor.set_speed(new_speed)
    safe_config(process_motor_label, text=process_motor_text())


# ============================================================
# ENCODER
# ============================================================

def setup_encoder_hardware():
    global encoder_a, encoder_b, encoder_quad_state
    global extra_ok_button, extra_back_button

    if not HAS_GPIO:
        encoder_a = DummyInputDevice()
        encoder_b = DummyInputDevice()
        extra_ok_button = DummyInputDevice()
        extra_back_button = DummyInputDevice()
        return

    try:
        encoder_a = DigitalInputDevice(
            ENCODER_A_GPIO,
            pull_up=True,
            bounce_time=None,
        )
        encoder_b = DigitalInputDevice(
            ENCODER_B_GPIO,
            pull_up=True,
            bounce_time=None,
        )
        extra_ok_button = Button(
            EXTRA_OK_GPIO,
            pull_up=True,
            bounce_time=0.08,
        )
        extra_back_button = Button(
            EXTRA_BACK_GPIO,
            pull_up=True,
            bounce_time=0.08,
        )
    except Exception:
        encoder_a = DummyInputDevice()
        encoder_b = DummyInputDevice()
        extra_ok_button = DummyInputDevice()
        extra_back_button = DummyInputDevice()
        return

    encoder_quad_state = encoder_ab_state()

    encoder_a.when_activated = on_encoder_edge
    encoder_a.when_deactivated = on_encoder_edge

    encoder_b.when_activated = on_encoder_edge
    encoder_b.when_deactivated = on_encoder_edge

    extra_ok_button.when_pressed = lambda: root.after(0, encoder_short_press)
    extra_back_button.when_pressed = lambda: root.after(0, encoder_long_press)


def encoder_ab_state():
    a = 1 if encoder_a.value else 0
    b = 1 if encoder_b.value else 0
    return (a << 1) | b


def on_encoder_edge(*_args):
    global encoder_quad_state, encoder_quad_accum, pending_encoder_steps

    current = encoder_ab_state()
    delta = QUAD_DELTA.get((encoder_quad_state << 2) | current)
    encoder_quad_state = current
    if not delta:
        return

    with encoder_lock:
        encoder_quad_accum += delta
        steps = 0
        while encoder_quad_accum >= ENCODER_DETENT_TICKS:
            steps += 1
            encoder_quad_accum -= ENCODER_DETENT_TICKS
        while encoder_quad_accum <= -ENCODER_DETENT_TICKS:
            steps -= 1
            encoder_quad_accum += ENCODER_DETENT_TICKS
        if not steps:
            return
        pending_encoder_steps += steps

    schedule_encoder_flush()


def schedule_encoder_flush():
    global encoder_flush_scheduled
    with encoder_lock:
        if encoder_flush_scheduled:
            return
        encoder_flush_scheduled = True
    try:
        root.after(0, flush_encoder)
    except Exception:
        with encoder_lock:
            encoder_flush_scheduled = False


def flush_encoder():
    global pending_encoder_steps, encoder_flush_scheduled

    with encoder_lock:
        steps = pending_encoder_steps
        pending_encoder_steps = 0
        encoder_flush_scheduled = False

    if steps:
        encoder_rotate(steps)


def encoder_rotate(steps):
    if not steps:
        return

    if adjust_mode:
        adjust_change(steps)
        return

    if choice_mode:
        choice_change(steps)
        return

    if root_screen_is_manual():
        if manual_active:
            manual_speed_change(steps)
        else:
            nav_move(steps)
        return

    if run_active:
        change_process_speed(steps)
        return

    nav_move(steps)


def encoder_short_press():
    if adjust_mode:
        adjust_confirm()
        return

    if choice_mode:
        choice_confirm()
        return

    if root_screen_is_manual():
        if manual_active:
            toggle_manual_motor()
        else:
            nav_activate()
        return

    if run_active:
        toggle_process_pause()
        return

    if run_step_finished:
        if process_has_next_step():
            next_process_step()
        else:
            show_main_menu()
        return

    nav_activate()


def encoder_long_press():
    if adjust_mode:
        adjust_cancel()
        return

    if choice_mode:
        choice_cancel()
        return

    if manual_active:
        leave_manual()
        return

    if run_active:
        stop_process()
        return

    show_main_menu()


def root_screen_is_manual():
    try:
        return manual_start_button is not None and manual_start_button.winfo_exists()
    except Exception:
        return False


# ============================================================
# CLOSE
# ============================================================

def close_hardware():
    global temp_thread

    shutdown_event.set()

    for device in (encoder_a, encoder_b):
        if device is None:
            continue
        try:
            device.when_activated = None
            device.when_deactivated = None
        except Exception:
            pass

    for device in (extra_ok_button, extra_back_button):
        if device is None:
            continue
        try:
            device.when_pressed = None
        except Exception:
            pass

    if temp_thread is not None and temp_thread.is_alive():
        temp_thread.join(timeout=1.2)
    temp_thread = None

    if motor is not None:
        try:
            motor.close()
        except Exception:
            pass

    for device in (
        encoder_a,
        encoder_b,
        extra_ok_button,
        extra_back_button,
    ):
        if device is not None:
            try:
                device.close()
            except Exception:
                pass


def close_app():
    stop_all_activity()
    for key in list(after_ids):
        cancel_after(key)
    close_hardware()
    try:
        root.destroy()
    except Exception:
        pass


# ============================================================
# START
# ============================================================

def create_root():
    global root, content, temp_label

    root = tk.Tk()
    root.title("JOBO Film Processor")
    root.geometry(f"{WIDTH}x{HEIGHT}")
    root.configure(bg=BG)
    root.protocol("WM_DELETE_WINDOW", close_app)
    root.bind("<Escape>", lambda event: close_app())

    if ON_PI:
        root.attributes("-fullscreen", True)

    header = tk.Frame(root, bg=BG)
    header.pack(fill=tk.X, pady=(6, 0))

    tk.Label(
        header,
        text="JOBO",
        font=("Arial", 12, "bold"),
        bg=BG,
        fg=MUTED,
    ).pack(side=tk.LEFT, padx=16)

    temp_label = tk.Label(
        header,
        text="Temp. --.-°C",
        font=("Arial", 14, "bold"),
        bg=BG,
        fg="white",
    )
    temp_label.pack(side=tk.RIGHT, padx=16)

    content = tk.Frame(root, bg=BG)
    content.pack(fill=tk.BOTH, expand=True)


def start_background():
    global temp_thread
    shutdown_event.clear()
    temp_thread = threading.Thread(
        target=temperature_loop,
        name="temperature",
        daemon=True,
    )
    temp_thread.start()


def main():
    global motor, simulated_hardware, hardware_error

    create_root()

    try:
        if not HAS_GPIO or not ON_PI:
            simulated_hardware = True
            motor = MotorController(simulated=True)
        else:
            motor = MotorController(simulated=False)
    except Exception as error:
        hardware_error = error
        simulated_hardware = True
        motor = MotorController(simulated=True)

    setup_encoder_hardware()
    show_main_menu()
    start_background()

    try:
        root.mainloop()
    finally:
        close_hardware()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        close_hardware()
        sys.exit(0)
