# Venus dashboard in Node-RED

Local live view of the Victron system on the GX (Pi .119/.123), a fallback for the FUXA view venus.heissa.de that
needs neither heissa nor wagodb. Standard layout, not a 1:1 copy of the FUXA picture.

Page: `https://<gx>:1881/dashboard/venus` (self-signed certificate of Venus; Node-RED editor on the same port).

Data: local Venus MQTT broker (dbus-flashmq mirrors every dbus path: VE.Bus 290, battery 512 = Speicher A / CAN-bus
BMS MUST, 513 = Speicher B / EBox, grid 30 = PCC from dbus-pcc-grid, system 0, CGwacs settings) and
`/data/batmonitor/state.json` every 5 s. History of the last 6 h (one point per minute) lives in the flow context and
starts empty after a Node-RED restart.

Install (once, Dashboard 2.0 into /data so it survives firmware updates):

    L=$(machinectl show venus -p Leader --value)
    sudo nsenter -t $L -a sh -c 'cd /data/home/nodered/.node-red && setpriv --init-groups --reuid nodered --regid nodered \
        env HOME=/data/home/nodered npm install --omit=dev @flowfuse/node-red-dashboard@1.34.0'
    sudo sh install.sh      # generates the flow, merges it into flows.json (vdash_* nodes), restarts Node-RED

Change the flow in `gen_flows.py`, not in the editor: install.sh replaces every `vdash_*` node.

The Venus nginx forwards the WebSocket upgrade only for `/comms`, so the dashboard falls back to socket.io long
polling (works; the browser console shows one failed WebSocket handshake).
