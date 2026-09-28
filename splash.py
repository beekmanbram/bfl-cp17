import os
import sys
import tkinter as tk
from PIL import Image, ImageTk

BASE_DIR = "/home/cp17/bfl-cp17"
LOGO = os.path.join(BASE_DIR, "assets", "logo.png")
MAIN = os.path.join(BASE_DIR, "main.py")

# Xorg from start-cp17.sh is on :0. sudo drops DISPLAY unless it is set here.
os.environ.setdefault("DISPLAY", ":0")

root = tk.Tk()

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
