#!/bin/sh
# Start the headless display stack on :1 if not running (Xvfb, openbox, xcompmgr, x11vnc on localhost:5901).
# xcompmgr is required: without a compositor Wine popups/dialogs are drawn black.
export DISPLAY=:1
pgrep -x Xvfb >/dev/null || { setsid nohup Xvfb :1 -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 </dev/null & sleep 2; }
pgrep -x openbox >/dev/null || setsid nohup openbox >/tmp/openbox.log 2>&1 </dev/null &
pgrep -x xcompmgr >/dev/null || setsid nohup xcompmgr -n >/tmp/xcompmgr.log 2>&1 </dev/null &
pgrep -x x11vnc >/dev/null || setsid nohup x11vnc -display :1 -localhost -forever -shared -nopw -rfbport 5901 >/tmp/x11vnc.log 2>&1 </dev/null &
sleep 1
