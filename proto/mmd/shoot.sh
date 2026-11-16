#!/bin/bash
# Throwaway: screenshot the prototype at the given times.  ./shoot.sh 2 18.5 28
cd "$(dirname "$0")"
CH=~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome
for t in "$@"; do
  "$CH" --headless=new --no-sandbox --hide-scrollbars --enable-unsafe-swiftshader --use-angle=swiftshader \
    --window-size=540,960 --virtual-time-budget=40000 --screenshot="shots/t$t.png" \
    "http://127.0.0.1:8012/?t=$t" 2>&1 | grep -v "^$" | grep -iv "dbus\|gcm\|vaapi\|font" | tail -3
done
ls -la shots | tail -n +2
