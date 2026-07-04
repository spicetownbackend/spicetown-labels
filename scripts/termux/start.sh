#!/data/data/com.termux/files/usr/bin/sh
# Spice Town print bridge — Termux launcher (see docs/BRIDGE.md).
# Copy to ~/bridge/start.sh on the tablet, then EDIT THE TWO LINES MARKED below.
# Also copy to ~/.termux/boot/start-bridge.sh for auto-start on reboot.

termux-wake-lock

export BRIDGE_SERVER=https://spicetown-labels.onrender.com
export BRIDGE_TOKEN=PASTE_YOUR_TOKEN_HERE        # <-- EDIT: STL_BRIDGE_TOKEN from Render's Environment tab
export BRIDGE_PRINTER=192.168.1.50               # <-- EDIT: the QL-810W's IP on the store WiFi

# No printer yet? Comment out the BRIDGE_PRINTER line above and uncomment:
# export BRIDGE_SPOOL_DIR=~/bridge/spool

while true; do
  python ~/bridge/print_bridge.py >> ~/bridge/bridge.log 2>&1
  sleep 5
done
