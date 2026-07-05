#!/bin/bash
G=/sys/kernel/config/usb_gadget/g1
test -d "$G" || { echo no-gadget; exit 0; }
echo "" > "$G/UDC" 2>/dev/null || true
find "$G"/configs -type l -delete 2>/dev/null || true
find "$G"/functions -type l -delete 2>/dev/null || true
find "$G" -depth -mindepth 1 -type d -exec rmdir {} + 2>/dev/null || true
rmdir "$G" 2>/dev/null || true
echo teardown-done
