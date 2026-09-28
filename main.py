import copy
import json
import os
import shutil
import subprocess
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
    from gpiozero import Button, DigitalInputDevice, DigitalOutputDevice
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False
    Button = None


# ============================================================
# JOBO FILM PROCESSOR
#
# DFR0601
# PWM1 -> GPIO12  (physical 32)  hardware PWM 20 kHz
# INA1 -> GPIO6   (physical 31)
# INB1 -> GPIO13  (physical 33)
#
# Encoder, rotation only
# A -> GPIO16
# B -> GPIO20
# GPIO21 is unused. The encoder push button is not used.
#
# Separate buttons to GND, internal pull-up
# OK   -> GPIO23
# BACK -> GPIO24
#
# DS18B20 DATA -> GPIO4
# Screen 800x480, no touch
# ============================================================


INA_GPIO = 6
INB_GPIO = 13
ENCODER_A_GPIO = 16
ENCODER_B_GPIO = 20
OK_GPIO = 23
BACK_GPIO = 24

PWM_PERIOD_NS = 50_000
PWM_CHANNEL = 0

WIDTH = 800
HEIGHT = 480

BG = "#0d0d0d"
TEXT = "#f2f2f2"
MUTED = "#8a8a8a"
ACCENT = "#e0a800"
GREEN = "#7dcea0"
RED = "#e06c6c"
ORANGE = "#ef6c00"
TRACK = "#1c1c1c"

FONT = "DejaVu Sans"
MONO = "DejaVu Sans Mono"

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

    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


class NavRow:
    def __init__(self, widget, action, text):
        self.widget = widget
        self.action = action
        self.text = text


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


DEFAULT_SETTINGS = {
    "speed": 30,
    "left_seconds": 10.0,
    "right_seconds": 10.0,
    "direction_delay_ms": 50,
    "encoder_step": 1,
}

DEFAULT_RECIPES = [
    {
        "name": "B&W example",
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
    return {"name": name, "seconds": seconds, "motor": bool(step.get("motor", True))}


def normalize_recipe(recipe, fallback_name="Process"):
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
        normalize_recipe(recipe, f"Process {index + 1}")
        for index, recipe in enumerate(data)
    ]
    return recipes_data or copy.deepcopy(DEFAULT_RECIPES)


settings = normalize_settings(load_json(SETTINGS_FILE, DEFAULT_SETTINGS))
recipes = normalize_recipes(load_json(RECIPES_FILE, DEFAULT_RECIPES))


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
            "Hardware PWM not found. Check dtoverlay=pwm,pin=12,func=4"
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
        raise RuntimeError("PWM channel could not be created.")

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
        duty = int(clamp(self.period_ns * percent / 100.0, 0, self.period_ns))
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

        self._switch_after = root.after(max(1, delay), change)

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


root = None
content = None
temp_label = None
list_canvas = None
list_inner = None
list_window = None
motor = None
simulated_hardware = False
hardware_error = None

shutdown_event = threading.Event()
temp_thread = None
after_ids = {}

current_screen = "menu"
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
choice_callback = None
choice_cancel_callback = None

encoder_lock = threading.Lock()
pending_encoder_steps = 0
encoder_flush_scheduled = False
encoder_quad_state = 0
encoder_quad_accum = 0

manual_active = False
manual_next_switch = None
manual_speed = settings["speed"]
manual_motor_label = None
manual_rows = {}

settings_snapshot = None

current_recipe_index = 0
editor_recipe_index = 0
editor_step_index = 0

run_recipe = None
run_step_index = 0
run_active = False
run_paused = False
run_step_finished = False
run_remaining = 0.0
run_step_total = 1.0
run_last_tick = 0.0
run_next_direction = None
run_switch_remaining = None

process_timer_label = None
process_motor_label = None
process_status_label = None
progress_canvas = None
pause_row = None

encoder_a = None
encoder_b = None
ok_button = None
back_button = None


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


def field(label, value):
    return f"{label:<18}{value}"


def format_time(seconds):
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_percent(value):
    return f"{int(value)}%"


def format_seconds(value):
    return f"{float(value):.1f}s"


def format_ms(value):
    return f"{int(value)} ms"


def read_temperature():
    base = Path("/sys/bus/w1/devices")
    if not base.exists():
        return None
    sensors = list(base.glob("28-*"))
    if not sensors:
        return None
    try:
        text = (sensors[0] / "w1_slave").read_text()
        if "YES" not in text:
            return None
        return float(text.split("t=")[-1]) / 1000.0
    except Exception:
        return None


def apply_temperature_text(text):
    safe_config(temp_label, text=text)


def temperature_loop():
    while not shutdown_event.is_set():
        temp = read_temperature()
        text = "Temp. --.-°C" if temp is None else f"Temp. {temp:.1f}°C"
        try:
            if root is not None:
                root.after(0, apply_temperature_text, text)
        except Exception:
            break
        shutdown_event.wait(1.5)


def clear_screen():
    global nav_items, nav_index, list_canvas, list_inner, list_window
    for widget in content.winfo_children():
        widget.destroy()
    nav_items = []
    nav_index = 0
    list_canvas = None
    list_inner = None
    list_window = None


def add_title(text, size=26):
    tk.Label(
        content,
        text=text,
        font=(FONT, size, "bold"),
        bg=BG,
        fg=TEXT,
        anchor="w",
    ).pack(fill=tk.X, padx=36, pady=(10, 4))


def add_info(text, size=16, color=MUTED):
    label = tk.Label(
        content,
        text=text,
        font=(FONT, size),
        bg=BG,
        fg=color,
        anchor="w",
        justify=tk.LEFT,
    )
    label.pack(fill=tk.X, padx=40, pady=1)
    return label


def begin_rows():
    global list_canvas, list_inner, list_window
    list_canvas = tk.Canvas(content, bg=BG, highlightthickness=0, bd=0)
    list_canvas.pack(fill=tk.BOTH, expand=True, pady=(6, 8))
    list_inner = tk.Frame(list_canvas, bg=BG)
    list_window = list_canvas.create_window((0, 0), window=list_inner, anchor="nw")

    def sync(_event=None):
        if list_canvas is None or list_inner is None:
            return
        try:
            list_canvas.configure(scrollregion=list_canvas.bbox("all"))
            list_canvas.itemconfigure(list_window, width=list_canvas.winfo_width())
        except tk.TclError:
            pass

    list_inner.bind("<Configure>", sync)
    list_canvas.bind("<Configure>", sync)
    return list_inner


def add_row(parent, text, action, size=20):
    label = tk.Label(
        parent,
        text="  " + text,
        font=(MONO, size),
        bg=BG,
        fg=TEXT,
        anchor="w",
        padx=32,
    )
    label.pack(fill=tk.X, pady=3)
    return NavRow(label, action, text)


def set_navigation(items, selected=0):
    global nav_items, nav_index
    nav_items = [item for item in items if item is not None]
    if not nav_items:
        nav_index = 0
        return
    nav_index = int(clamp(selected, 0, len(nav_items) - 1))
    update_navigation()


def update_navigation():
    for index, row in enumerate(nav_items):
        selected = index == nav_index
        prefix = "> " if selected else "  "
        safe_config(
            row.widget,
            text=prefix + row.text,
            fg=ACCENT if selected else TEXT,
        )
    reveal_selection()


def reveal_selection():
    if list_canvas is None or not nav_items:
        return
    widget = nav_items[nav_index].widget
    try:
        list_canvas.update_idletasks()
        inner_h = max(1, list_inner.winfo_height())
        view_h = max(1, list_canvas.winfo_height())
        if inner_h <= view_h:
            list_canvas.yview_moveto(0)
            return
        target = max(0, widget.winfo_y() - (view_h * 0.35))
        list_canvas.yview_moveto(target / inner_h)
    except tk.TclError:
        pass


def nav_move(steps):
    global nav_index
    if not nav_items or not steps:
        return
    nav_index = (nav_index + int(steps)) % len(nav_items)
    update_navigation()


def nav_activate():
    if not nav_items:
        return
    action = nav_items[nav_index].action
    if action:
        action()


def open_adjust(title, value, minimum, maximum, step, callback, cancel_callback, formatter):
    global adjust_mode, choice_mode, current_screen
    global adjust_value, adjust_min, adjust_max, adjust_step
    global adjust_callback, adjust_cancel_callback, adjust_formatter, adjust_label

    adjust_mode = True
    choice_mode = False
    current_screen = "adjust"
    adjust_min = minimum
    adjust_max = maximum
    adjust_step = step
    adjust_value = snap_to_step(value, minimum, maximum, step)
    adjust_callback = callback
    adjust_cancel_callback = cancel_callback
    adjust_formatter = formatter

    clear_screen()
    add_title(title.upper(), 24)
    adjust_label = tk.Label(
        content,
        text=formatter(adjust_value),
        font=(FONT, 54, "bold"),
        bg=BG,
        fg=ACCENT,
    )
    adjust_label.pack(pady=(18, 16))
    parent = begin_rows()
    set_navigation([
        add_row(parent, "OK", adjust_confirm),
        add_row(parent, "CANCEL", adjust_cancel),
    ])


def adjust_change(steps):
    global adjust_value
    if not adjust_mode or not steps:
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


def open_choice(title, values, current_value, callback, cancel_callback):
    global choice_mode, adjust_mode, current_screen
    global choice_values, choice_callback, choice_cancel_callback

    choice_mode = True
    adjust_mode = False
    current_screen = "choice"
    choice_values = list(values)
    choice_callback = callback
    choice_cancel_callback = cancel_callback
    try:
        selected = choice_values.index(current_value)
    except ValueError:
        selected = 0

    clear_screen()
    add_title(title.upper(), 24)
    parent = begin_rows()
    rows = [
        add_row(parent, value, lambda chosen=value: finish_choice(chosen))
        for value in choice_values
    ]
    set_navigation(rows, selected)


def finish_choice(value):
    global choice_mode
    callback = choice_callback
    choice_mode = False
    if callback:
        callback(value)


def choice_cancel():
    global choice_mode
    callback = choice_cancel_callback
    choice_mode = False
    if callback:
        callback()


def show_main_menu():
    global current_screen, adjust_mode, choice_mode
    stop_all_activity()
    adjust_mode = False
    choice_mode = False
    current_screen = "menu"
    clear_screen()
    if hardware_error:
        add_info(str(hardware_error), 13, RED)
    elif simulated_hardware and ON_PI:
        add_info("Motor in simulation", 13, ORANGE)
    parent = begin_rows()
    set_navigation([
        add_row(parent, "DEVELOP", enter_manual),
        add_row(parent, "PROCESSES", show_process_select),
        add_row(parent, "SETTINGS", enter_settings),
        add_row(parent, "SHUTDOWN", shutdown_pi),
    ])


def enter_settings():
    global settings_snapshot
    settings_snapshot = copy.deepcopy(settings)
    show_settings()


def show_settings():
    global current_screen, adjust_mode, choice_mode
    adjust_mode = False
    choice_mode = False
    current_screen = "settings"
    clear_screen()
    add_title("SETTINGS")
    parent = begin_rows()
    set_navigation([
        add_row(parent, field("Speed", f"{settings['speed']}%"), settings_speed, 18),
        add_row(parent, field("Left time", format_seconds(settings["left_seconds"])), settings_left, 18),
        add_row(parent, field("Right time", format_seconds(settings["right_seconds"])), settings_right, 18),
        add_row(parent, field("Turn delay", format_ms(settings["direction_delay_ms"])), settings_delay, 18),
        add_row(parent, field("Encoder step", f"{settings['encoder_step']}%"), settings_encoder_step, 18),
        add_row(parent, "SAVE & BACK", save_settings_and_back, 18),
    ])


def settings_speed():
    open_adjust(
        "Default speed", settings["speed"], 0, 100, 1,
        save_setting_speed, show_settings, format_percent,
    )


def save_setting_speed(value):
    settings["speed"] = int(value)
    show_settings()


def settings_left():
    open_adjust(
        "Left time", settings["left_seconds"], 0.5, 60, 0.5,
        save_setting_left, show_settings, format_seconds,
    )


def save_setting_left(value):
    settings["left_seconds"] = round(float(value), 1)
    show_settings()


def settings_right():
    open_adjust(
        "Right time", settings["right_seconds"], 0.5, 60, 0.5,
        save_setting_right, show_settings, format_seconds,
    )


def save_setting_right(value):
    settings["right_seconds"] = round(float(value), 1)
    show_settings()


def settings_delay():
    open_adjust(
        "Turn delay", settings["direction_delay_ms"], 20, 500, 10,
        save_setting_delay, show_settings, format_ms,
    )


def save_setting_delay(value):
    settings["direction_delay_ms"] = int(value)
    show_settings()


def settings_encoder_step():
    open_adjust(
        "Encoder step", settings["encoder_step"], 1, 10, 1,
        save_setting_encoder, show_settings, format_percent,
    )


def save_setting_encoder(value):
    settings["encoder_step"] = int(value)
    show_settings()


def save_settings_and_back():
    global settings_snapshot
    save_json(SETTINGS_FILE, settings)
    settings_snapshot = None
    show_main_menu()


def abandon_settings():
    global settings_snapshot
    if settings_snapshot is not None:
        settings.clear()
        settings.update(settings_snapshot)
    settings_snapshot = None
    show_main_menu()


def enter_manual():
    global manual_active, manual_next_switch, manual_speed
    stop_all_activity()
    manual_speed = int(settings["speed"])
    manual_next_switch = None
    render_manual()


def render_manual():
    global current_screen, adjust_mode, choice_mode
    global manual_motor_label, manual_rows
    adjust_mode = False
    choice_mode = False
    current_screen = "manual"
    clear_screen()
    add_title("DEVELOP")
    manual_motor_label = add_info(manual_status_text(), 18, TEXT)
    parent = begin_rows()
    speed_row = add_row(parent, field("Speed", f"{manual_speed}%"), edit_manual_speed, 18)
    left_row = add_row(
        parent, field("Left time", format_seconds(settings["left_seconds"])), edit_manual_left, 18
    )
    right_row = add_row(
        parent, field("Right time", format_seconds(settings["right_seconds"])), edit_manual_right, 18
    )
    action = "STOP" if manual_active else "START"
    action_row = add_row(parent, action, toggle_manual_motor, 18)
    back_row = add_row(parent, "BACK", leave_manual, 18)
    manual_rows = {
        "speed": speed_row,
        "left": left_row,
        "right": right_row,
        "action": action_row,
    }
    set_navigation([speed_row, left_row, right_row, action_row, back_row], 3 if manual_active else 0)


def manual_status_text():
    if not manual_active or motor is None or not motor.running:
        return "Motor           OFF"
    if motor.direction == "LEFT":
        return "Motor           ◀ LEFT"
    return "Motor           RIGHT ▶"


def refresh_manual_rows():
    if "speed" in manual_rows:
        manual_rows["speed"].text = field("Speed", f"{manual_speed}%")
    if "action" in manual_rows:
        manual_rows["action"].text = "STOP" if manual_active else "START"
    safe_config(manual_motor_label, text=manual_status_text(), fg=ACCENT if manual_active else TEXT)
    update_navigation()


def edit_manual_speed():
    if manual_active:
        return
    open_adjust(
        "Speed", manual_speed, 0, 100, 1,
        save_manual_speed, render_manual, format_percent,
    )


def save_manual_speed(value):
    global manual_speed
    manual_speed = int(value)
    render_manual()


def edit_manual_left():
    if manual_active:
        return
    open_adjust(
        "Left time", settings["left_seconds"], 0.5, 60, 0.5,
        save_manual_left, render_manual, format_seconds,
    )


def save_manual_left(value):
    settings["left_seconds"] = round(float(value), 1)
    save_json(SETTINGS_FILE, settings)
    render_manual()


def edit_manual_right():
    if manual_active:
        return
    open_adjust(
        "Right time", settings["right_seconds"], 0.5, 60, 0.5,
        save_manual_right, render_manual, format_seconds,
    )


def save_manual_right(value):
    settings["right_seconds"] = round(float(value), 1)
    save_json(SETTINGS_FILE, settings)
    render_manual()


def toggle_manual_motor():
    global manual_active, manual_next_switch
    if manual_active:
        manual_active = False
        manual_next_switch = None
        cancel_after("manual_tick")
        motor.stop()
        refresh_manual_rows()
        return

    manual_active = True
    motor.start(manual_speed, "LEFT")
    manual_next_switch = time.monotonic() + settings["left_seconds"]
    refresh_manual_rows()
    manual_tick()


def manual_tick():
    global manual_next_switch
    after_ids.pop("manual_tick", None)
    if not manual_active:
        return
    now = time.monotonic()
    if manual_next_switch and now >= manual_next_switch and not motor.switching:
        if motor.direction == "LEFT":
            direction = "RIGHT"
            duration = settings["right_seconds"]
        else:
            direction = "LEFT"
            duration = settings["left_seconds"]
        motor.switch_direction(direction, refresh_manual_rows)
        manual_next_switch = now + duration
    safe_config(manual_motor_label, text=manual_status_text(), fg=ACCENT)
    schedule("manual_tick", 50, manual_tick)


def manual_speed_change(steps):
    global manual_speed
    if not steps:
        return
    manual_speed = int(clamp(manual_speed + (steps * settings["encoder_step"]), 0, 100))
    motor.set_speed(manual_speed)
    refresh_manual_rows()


def leave_manual():
    stop_all_activity()
    show_main_menu()


def ensure_recipes():
    global recipes, current_recipe_index
    if not recipes:
        recipes.append(copy.deepcopy(DEFAULT_RECIPES[0]))
    current_recipe_index %= len(recipes)


def show_process_select():
    global current_screen, adjust_mode, choice_mode
    stop_all_activity()
    ensure_recipes()
    adjust_mode = False
    choice_mode = False
    current_screen = "processes"
    clear_screen()
    add_title("PROCESSES")
    parent = begin_rows()
    rows = []
    for index, recipe in enumerate(recipes):
        rows.append(add_row(parent, recipe["name"], lambda i=index: open_recipe(i), 20))
    rows.append(add_row(parent, "+ NEW", add_recipe, 20))
    rows.append(add_row(parent, "BACK", show_main_menu, 20))
    selected = min(current_recipe_index, len(rows) - 1)
    set_navigation(rows, selected)


def open_recipe(index):
    global current_recipe_index
    current_recipe_index = index
    show_recipe_actions()


def show_recipe_actions():
    global current_screen
    ensure_recipes()
    current_screen = "recipe"
    recipe = recipes[current_recipe_index]
    clear_screen()
    add_title(recipe["name"])
    add_info(f"{len(recipe['steps'])} steps   {recipe['speed']}%", 15, MUTED)
    parent = begin_rows()
    set_navigation([
        add_row(parent, "START PROCESS", start_selected_recipe),
        add_row(parent, "EDIT", edit_selected_recipe),
        add_row(parent, "DELETE", delete_recipe),
        add_row(parent, "BACK", show_process_select),
    ])


def add_recipe():
    global current_recipe_index, editor_recipe_index, editor_step_index
    recipe = {
        "name": f"Process {len(recipes) + 1}",
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


def current_editor_recipe():
    return recipes[editor_recipe_index]


def show_process_editor():
    global current_screen, editor_step_index, adjust_mode, choice_mode
    adjust_mode = False
    choice_mode = False
    current_screen = "editor"
    recipe = current_editor_recipe()
    editor_step_index = int(clamp(editor_step_index, 0, len(recipe["steps"]) - 1))
    step = recipe["steps"][editor_step_index]
    clear_screen()
    add_title(recipe["name"], 24)
    add_info(
        f"Step {editor_step_index + 1}/{len(recipe['steps'])}    "
        f"{step['name']}    {format_time(step['seconds'])}",
        16,
        ACCENT,
    )
    parent = begin_rows()
    set_navigation([
        add_row(parent, field("Speed", f"{recipe['speed']}%"), edit_recipe_speed, 17),
        add_row(parent, field("Left", format_seconds(recipe["left_seconds"])), edit_recipe_left, 17),
        add_row(parent, field("Right", format_seconds(recipe["right_seconds"])), edit_recipe_right, 17),
        add_row(parent, "PREVIOUS STEP", previous_step, 17),
        add_row(parent, "NEXT STEP", next_step, 17),
        add_row(parent, "EDIT STEP", show_step_editor, 17),
        add_row(parent, "+ STEP", add_step, 17),
        add_row(parent, "- STEP", delete_step, 17),
        add_row(parent, "SAVE", save_process, 17),
        add_row(parent, "BACK", show_process_select, 17),
    ])


def edit_recipe_speed():
    open_adjust(
        "Process speed", current_editor_recipe()["speed"], 0, 100, 1,
        save_recipe_speed, show_process_editor, format_percent,
    )


def save_recipe_speed(value):
    current_editor_recipe()["speed"] = int(value)
    show_process_editor()


def edit_recipe_left():
    open_adjust(
        "Left time", current_editor_recipe()["left_seconds"], 0.5, 60, 0.5,
        save_recipe_left, show_process_editor, format_seconds,
    )


def save_recipe_left(value):
    current_editor_recipe()["left_seconds"] = round(float(value), 1)
    show_process_editor()


def edit_recipe_right():
    open_adjust(
        "Right time", current_editor_recipe()["right_seconds"], 0.5, 60, 0.5,
        save_recipe_right, show_process_editor, format_seconds,
    )


def save_recipe_right(value):
    current_editor_recipe()["right_seconds"] = round(float(value), 1)
    show_process_editor()


def previous_step():
    global editor_step_index
    count = len(current_editor_recipe()["steps"])
    editor_step_index = (editor_step_index - 1) % count
    show_process_editor()


def next_step():
    global editor_step_index
    count = len(current_editor_recipe()["steps"])
    editor_step_index = (editor_step_index + 1) % count
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


def current_step():
    return recipes[editor_recipe_index]["steps"][editor_step_index]


def show_step_editor():
    global current_screen, adjust_mode, choice_mode
    adjust_mode = False
    choice_mode = False
    current_screen = "step"
    step = current_step()
    clear_screen()
    add_title(f"STEP {editor_step_index + 1}")
    parent = begin_rows()
    motor_text = "ON" if step.get("motor", True) else "OFF"
    set_navigation([
        add_row(parent, field("Name", step["name"]), edit_step_name, 18),
        add_row(parent, field("Time", format_time(step["seconds"])), edit_step_time, 18),
        add_row(parent, field("Motor", motor_text), toggle_step_motor, 18),
        add_row(parent, "DONE", show_process_editor, 18),
        add_row(parent, "BACK", show_process_editor, 18),
    ])


def edit_step_name():
    open_choice("Step name", STEP_NAMES, current_step()["name"], save_step_name, show_step_editor)


def save_step_name(value):
    current_step()["name"] = value
    show_step_editor()


def edit_step_time():
    open_adjust(
        "Step duration", current_step()["seconds"], 1, 3600, 5,
        save_step_time, show_step_editor, format_time,
    )


def save_step_time(value):
    current_step()["seconds"] = int(value)
    show_step_editor()


def toggle_step_motor():
    step = current_step()
    step["motor"] = not step.get("motor", True)
    show_step_editor()


def start_selected_recipe():
    global run_recipe, run_step_index
    run_recipe = copy.deepcopy(recipes[current_recipe_index])
    run_step_index = 0
    prepare_process_step()


def prepare_process_step():
    global run_active, run_paused, run_step_finished
    global run_remaining, run_step_total, run_next_direction, run_switch_remaining

    motor.stop()
    cancel_after("process_tick")
    run_active = False
    run_paused = False
    run_step_finished = False
    run_step_total = float(run_recipe["steps"][run_step_index]["seconds"])
    run_remaining = run_step_total
    run_next_direction = None
    run_switch_remaining = None
    show_process_run_screen(waiting=True)


def show_process_run_screen(waiting=False):
    global current_screen, process_timer_label, process_motor_label
    global process_status_label, progress_canvas, pause_row
    global adjust_mode, choice_mode

    adjust_mode = False
    choice_mode = False
    current_screen = "run"
    step = run_recipe["steps"][run_step_index]
    clear_screen()

    add_info(run_recipe["name"], 16, MUTED)
    add_title(step["name"].upper(), 28)
    process_timer_label = tk.Label(
        content,
        text=format_time(run_remaining),
        font=(FONT, 58, "bold"),
        bg=BG,
        fg=ORANGE if run_remaining <= 10 else ACCENT,
    )
    process_timer_label.pack(pady=(2, 0))
    progress_canvas = tk.Canvas(
        content, width=680, height=18, bg=TRACK, highlightthickness=0, bd=0,
    )
    progress_canvas.pack(pady=(8, 6))
    draw_progress()
    process_motor_label = add_info(process_motor_text(), 16, TEXT)
    if waiting:
        status = "READY TO START"
    elif run_paused:
        status = "PAUSED"
    else:
        status = "RUNNING"
    process_status_label = add_info(status, 15, ORANGE if run_paused else MUTED)

    parent = begin_rows()
    rows = [
        add_row(parent, field("Speed", f"{run_recipe['speed']}%"), edit_run_speed, 18),
    ]
    pause_row = None
    if waiting:
        rows.append(add_row(parent, "START", start_process_step))
    else:
        pause_row = add_row(parent, "RESUME" if run_paused else "PAUSE", toggle_process_pause)
        rows.append(pause_row)
        rows.append(add_row(parent, "STOP", stop_process))
    set_navigation(rows, 1)


def draw_progress():
    if progress_canvas is None:
        return
    try:
        if not progress_canvas.winfo_exists():
            return
        width = 680
        fraction = 0.0
        if run_step_total > 0:
            fraction = clamp(1.0 - (run_remaining / run_step_total), 0.0, 1.0)
        color = ORANGE if 0 < run_remaining <= 10 else ACCENT
        progress_canvas.delete("bar")
        fill = int(width * fraction)
        if fill > 0:
            progress_canvas.create_rectangle(0, 0, fill, 18, fill=color, width=0, tags="bar")
    except tk.TclError:
        pass


def process_motor_text():
    if motor is None or not motor.running:
        arrow = "OFF"
    elif motor.direction == "LEFT":
        arrow = "◀"
    else:
        arrow = "▶"
    speed = 0 if run_recipe is None else run_recipe["speed"]
    return f"Motor {arrow}  {speed}%"


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
    if not run_active or run_paused:
        if run_active:
            schedule("process_tick", 50, process_tick)
        return

    now = time.monotonic()
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
        fg=ORANGE if run_remaining <= 10 else ACCENT,
    )
    draw_progress()

    step = run_recipe["steps"][run_step_index]
    if (
        step.get("motor", True)
        and run_next_direction
        and now >= run_next_direction
        and motor is not None
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
        safe_config(process_status_label, text="PAUSED", fg=ORANGE)
        if pause_row is not None:
            pause_row.text = "RESUME"
        update_navigation()
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
    safe_config(process_status_label, text="RUNNING", fg=MUTED)
    if pause_row is not None:
        pause_row.text = "PAUSE"
    update_navigation()
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
    global current_screen, process_timer_label, progress_canvas, process_status_label
    current_screen = "step_done"
    step = run_recipe["steps"][run_step_index]
    clear_screen()
    add_info(run_recipe["name"], 16, MUTED)
    add_title(step["name"].upper(), 28)
    process_timer_label = tk.Label(
        content,
        text="00:00",
        font=(FONT, 58, "bold"),
        bg=BG,
        fg=ACCENT,
    )
    process_timer_label.pack(pady=(2, 0))
    progress_canvas = tk.Canvas(
        content, width=680, height=18, bg=TRACK, highlightthickness=0, bd=0,
    )
    progress_canvas.pack(pady=(8, 6))
    draw_progress_full()
    if process_has_next_step():
        nxt = run_recipe["steps"][run_step_index + 1]
        process_status_label = add_info("STEP DONE", 18, GREEN)
        add_info(f"Next   {nxt['name']}   {format_time(nxt['seconds'])}", 15, MUTED)
        parent = begin_rows()
        set_navigation([
            add_row(parent, "NEXT STEP", next_process_step),
            add_row(parent, "STOP", stop_process),
        ])
    else:
        process_status_label = add_info("PROCESS DONE", 18, ACCENT)
        parent = begin_rows()
        set_navigation([
            add_row(parent, "DONE", show_main_menu),
        ])


def draw_progress_full():
    if progress_canvas is None:
        return
    try:
        progress_canvas.delete("bar")
        progress_canvas.create_rectangle(0, 0, 680, 18, fill=ACCENT, width=0, tags="bar")
    except tk.TclError:
        pass


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


def edit_run_speed():
    if run_recipe is None:
        return
    open_adjust(
        "Process speed",
        run_recipe["speed"],
        0,
        100,
        1,
        save_run_speed,
        lambda: show_process_run_screen(waiting=not run_active),
        format_percent,
    )


def save_run_speed(value):
    if run_recipe is None:
        show_main_menu()
        return
    run_recipe["speed"] = int(value)
    motor.set_speed(value)
    show_process_run_screen(waiting=not run_active)


def handle_ok():
    if adjust_mode:
        adjust_confirm()
        return
    if current_screen == "manual" and manual_active:
        toggle_manual_motor()
        return
    if run_step_finished and current_screen == "step_done":
        nav_activate()
        return
    nav_activate()


def handle_back():
    if adjust_mode:
        adjust_cancel()
        return
    if choice_mode:
        choice_cancel()
        return
    if current_screen == "manual":
        leave_manual()
        return
    if current_screen in ("run", "step_done"):
        stop_process()
        return
    if current_screen == "step":
        show_process_editor()
        return
    if current_screen == "editor":
        show_process_select()
        return
    if current_screen == "recipe":
        show_process_select()
        return
    if current_screen == "processes":
        show_main_menu()
        return
    if current_screen == "settings":
        abandon_settings()
        return


def encoder_rotate(steps):
    if not steps:
        return
    if adjust_mode:
        adjust_change(steps)
        return
    if current_screen == "manual" and manual_active:
        manual_speed_change(steps)
        return
    nav_move(steps)


def setup_input_hardware():
    global encoder_a, encoder_b, encoder_quad_state, ok_button, back_button, hardware_error

    encoder_a = DummyInputDevice()
    encoder_b = DummyInputDevice()
    ok_button = DummyInputDevice()
    back_button = DummyInputDevice()
    if not HAS_GPIO or Button is None:
        return

    try:
        encoder_a = DigitalInputDevice(ENCODER_A_GPIO, pull_up=True, bounce_time=None)
        encoder_b = DigitalInputDevice(ENCODER_B_GPIO, pull_up=True, bounce_time=None)
        encoder_quad_state = encoder_ab_state()
        encoder_a.when_activated = on_encoder_edge
        encoder_a.when_deactivated = on_encoder_edge
        encoder_b.when_activated = on_encoder_edge
        encoder_b.when_deactivated = on_encoder_edge
    except Exception as error:
        encoder_a = DummyInputDevice()
        encoder_b = DummyInputDevice()
        hardware_error = hardware_error or f"Encoder: {error}"

    try:
        ok_button = Button(OK_GPIO, pull_up=True, bounce_time=0.08)
        back_button = Button(BACK_GPIO, pull_up=True, bounce_time=0.08)
        ok_button.when_pressed = lambda: root.after(0, handle_ok)
        back_button.when_pressed = lambda: root.after(0, handle_back)
    except Exception as error:
        ok_button = DummyInputDevice()
        back_button = DummyInputDevice()
        hardware_error = hardware_error or f"Buttons: {error}"


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
    for device in (ok_button, back_button):
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
    for device in (encoder_a, encoder_b, ok_button, back_button):
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


def shutdown_pi():
    stop_all_activity()
    for key in list(after_ids):
        cancel_after(key)
    close_hardware()

    # Same command as in the terminal. -n means: never wait for a password.
    # Passwordless sudo for this command is installed once on the Pi.
    if os.geteuid() == 0:
        command = ["/usr/sbin/shutdown", "-h", "now"]
    else:
        command = ["sudo", "-n", "/usr/sbin/shutdown", "-h", "now"]

    subprocess.Popen(
        command,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def attach_xauthority():
    current = os.environ.get("XAUTHORITY")
    if current and Path(current).is_file():
        return

    candidates = []
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and pwd is not None:
        try:
            candidates.append(Path(pwd.getpwnam(sudo_user).pw_dir) / ".Xauthority")
        except Exception:
            pass
    candidates.append(Path.home() / ".Xauthority")

    runtime = Path("/run/user")
    if runtime.is_dir():
        try:
            for folder in runtime.iterdir():
                if not folder.is_dir():
                    continue
                for path in folder.iterdir():
                    if path.is_file() and "auth" in path.name.lower():
                        candidates.append(path)
        except Exception:
            pass

    for path in candidates:
        try:
            if path.is_file():
                os.environ["XAUTHORITY"] = str(path)
                return
        except Exception:
            continue


def first_x_socket():
    folder = Path("/tmp/.X11-unix")
    if not folder.is_dir():
        return None
    sockets = []
    try:
        for path in folder.iterdir():
            number = path.name[1:]
            if path.name.startswith("X") and number.isdigit():
                sockets.append(path)
    except Exception:
        return None
    if not sockets:
        return None
    requested = os.environ.get("DISPLAY", "")
    if requested.startswith(":"):
        number = requested[1:].split(".")[0]
        for path in sockets:
            if path.name == f"X{number}":
                return path
    return sorted(sockets, key=lambda path: path.name)[0]


def relaunch_with_xinit():
    if os.environ.get("JOBO_XINIT") == "1":
        return False
    xinit = shutil.which("xinit")
    if not xinit:
        return False
    env = os.environ.copy()
    env["JOBO_XINIT"] = "1"
    env.pop("DISPLAY", None)
    script = os.path.abspath(__file__)
    try:
        os.execve(
            xinit,
            [xinit, sys.executable, script, "--", ":0", "-nocursor"],
            env,
        )
    except OSError:
        return False
    return True


def prepare_display():
    attach_xauthority()
    socket = first_x_socket()
    if socket is not None:
        os.environ["DISPLAY"] = f":{socket.name[1:]}"
        return
    if relaunch_with_xinit():
        return
    sys.stderr.write(
        "No graphical display found.\n"
        "Tkinter cannot draw on DISPLAY=:0 because no X server is running there.\n"
        "That happens on Raspberry Pi OS Lite and on a Wayland desktop.\n\n"
        "Install once:\n"
        "  sudo apt install xserver-xorg xinit\n\n"
        "Then start, without DISPLAY=:0:\n"
        "  sudo python3 jobo-motor.py\n"
    )
    raise SystemExit(1)


def create_root():
    global root, content, temp_label
    prepare_display()
    try:
        root = tk.Tk()
    except tk.TclError as error:
        message = str(error).lower()
        if "connect" in message and relaunch_with_xinit():
            return
        sys.stderr.write(
            "Tkinter could not open the display.\n"
            f"{error}\n\n"
            "Start without a forced display:\n"
            "  sudo python3 jobo-motor.py\n"
        )
        raise SystemExit(1)
    root.title("JOBO Film Processor")
    root.geometry(f"{WIDTH}x{HEIGHT}")
    root.configure(bg=BG)
    root.protocol("WM_DELETE_WINDOW", close_app)
    if ON_PI:
        root.attributes("-fullscreen", True)
        root.config(cursor="none")

    header = tk.Frame(root, bg=BG, height=42)
    header.pack(fill=tk.X)
    header.pack_propagate(False)
    temp_label = tk.Label(
        header,
        text="Temp. --.-°C",
        font=(FONT, 18, "bold"),
        bg=BG,
        fg=TEXT,
        anchor="w",
    )
    temp_label.pack(fill=tk.BOTH, expand=True, padx=36, pady=(8, 0))

    content = tk.Frame(root, bg=BG)
    content.pack(fill=tk.BOTH, expand=True)

    root.bind("<Up>", lambda _event: encoder_rotate(-1))
    root.bind("<Down>", lambda _event: encoder_rotate(1))
    root.bind("<Return>", lambda _event: handle_ok())
    root.bind("<KP_Enter>", lambda _event: handle_ok())
    root.bind("<BackSpace>", lambda _event: handle_back())
    root.bind("<Escape>", lambda _event: handle_back())
    root.focus_set()


def start_background():
    global temp_thread
    shutdown_event.clear()
    temp_thread = threading.Thread(target=temperature_loop, name="temperature", daemon=True)
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
        hardware_error = str(error)
        simulated_hardware = True
        motor = MotorController(simulated=True)

    setup_input_hardware()
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
