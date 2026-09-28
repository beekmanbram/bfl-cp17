#!/bin/bash

export DISPLAY=:0
export HOME=/home/cp17

/usr/bin/Xorg :0 \
    vt1 \
    -nolisten tcp \
    -nocursor \
    -keeptty &

XPID=$!

for i in {1..50}; do
    if DISPLAY=:0 xrandr >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done

OUTPUT=$(DISPLAY=:0 xrandr --query | awk '/ connected/{print $1; exit}')

if [ -n "$OUTPUT" ]; then
    DISPLAY=:0 xrandr --output "$OUTPUT" --rotate left
fi

DISPLAY=:0 xset s off
DISPLAY=:0 xset -dpms
DISPLAY=:0 xset s noblank

DISPLAY=:0 /usr/bin/python3 /home/cp17/bfl-cp17/splash.py

wait "$XPID"
