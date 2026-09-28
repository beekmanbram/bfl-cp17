import os
import shutil
import sys
import tkinter as tk
from pathlib import Path
from PIL import Image, ImageTk

try:
    import pwd
except ImportError:
    pwd = None

BASE_DIR = "/home/cp17/bfl-cp17"
LOGO = os.path.join(BASE_DIR, "assets", "logo.png")
MAIN = os.path.join(BASE_DIR, "main.py")


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
        "Install once:\n"
        "  sudo apt install xserver-xorg xinit\n\n"
        "Then start:\n"
        "  sudo python3 splash.py\n"
    )
    raise SystemExit(1)


prepare_display()

try:
    root = tk.Tk()
except tk.TclError as error:
    message = str(error).lower()
    if "connect" in message and relaunch_with_xinit():
        raise SystemExit(0)
    sys.stderr.write(
        "Tkinter could not open the display.\n"
        f"{error}\n\n"
        "Start with:\n"
        "  sudo python3 splash.py\n"
    )
    raise SystemExit(1)

root.configure(bg="black")
root.attributes("-fullscreen", True)
root.config(cursor="none")

screen_w = root.winfo_screenwidth()
screen_h = root.winfo_screenheight()

image = Image.open(LOGO).convert("RGBA")

# Logo passend maken met behoud van verhouding
image.thumbnail((screen_w, screen_h), Image.Resampling.LANCZOS)

photo = ImageTk.PhotoImage(image)

label = tk.Label(
    root,
    image=photo,
    bg="black",
    borderwidth=0,
    highlightthickness=0
)
label.place(relx=0.5, rely=0.5, anchor="center")


def start_jobo():
    root.destroy()

    # splash.py wordt al via sudo vanuit .xinitrc gestart,
    # dus main.py blijft in dezelfde omgeving draaien.
    os.execv(
        sys.executable,
        [sys.executable, MAIN]
    )


# Logo 2 seconden tonen
root.after(2000, start_jobo)

root.mainloop()
