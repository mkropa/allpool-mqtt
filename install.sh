#!/usr/bin/env bash
# Installiert / aktualisiert die AllPool-MQTT-Bridge als systemd-Dienst.
# Aufruf im entpackten Ordner:  sudo ./install.sh
set -euo pipefail

APP=/opt/allpool-mqtt
CONF=/etc/allpool-mqtt
SRC="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Bitte mit sudo ausführen: sudo $0" >&2
  exit 1
fi

echo "==> Pakete installieren"
apt-get update -qq
apt-get install -y -qq python3 python3-venv >/dev/null

echo "==> Benutzer 'allpool' anlegen (Gruppe dialout für die serielle Schnittstelle)"
if ! id allpool >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin --groups dialout allpool
else
  usermod -aG dialout allpool
fi

echo "==> Programm nach $APP kopieren"
install -d -m 755 "$APP" "$APP/tools"
install -m 755 "$SRC/allpool_mqtt.py" "$APP/allpool_mqtt.py"
install -m 755 "$SRC/tools/allpool_simulator.py" "$APP/tools/allpool_simulator.py"

echo "==> Python-Umgebung einrichten"
if [[ ! -x "$APP/venv/bin/python" ]]; then
  python3 -m venv "$APP/venv"
fi
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$SRC/requirements.txt"

echo "==> Konfiguration"
install -d -m 750 -o root -g allpool "$CONF"
if [[ ! -f "$CONF/config.yaml" ]]; then
  install -m 640 -o root -g allpool "$SRC/config.example.yaml" "$CONF/config.yaml"
  NEW_CONFIG=1
else
  echo "    $CONF/config.yaml existiert bereits – bleibt unverändert"
  NEW_CONFIG=0
fi

echo "==> systemd-Dienst"
install -m 644 "$SRC/allpool-mqtt.service" /etc/systemd/system/allpool-mqtt.service
systemctl daemon-reload
systemctl enable allpool-mqtt.service >/dev/null

# Bequemer Aufruf für Tests:  allpool-mqtt --dump
cat > /usr/local/bin/allpool-mqtt <<EOF
#!/bin/sh
exec $APP/venv/bin/python $APP/allpool_mqtt.py --config $CONF/config.yaml "\$@"
EOF
chmod 755 /usr/local/bin/allpool-mqtt

if [[ $NEW_CONFIG -eq 1 ]]; then
  echo
  echo "Fertig installiert. Jetzt noch:"
  echo "  1. sudo nano $CONF/config.yaml   (MQTT-Broker, Benutzer, Passwort, serieller Port)"
  echo "  2. sudo allpool-mqtt --dump      (Test: liest alle Werte einmal direkt vom ALLPOOL)"
  echo "  3. sudo systemctl start allpool-mqtt"
  echo "  4. journalctl -u allpool-mqtt -f (Log ansehen)"
else
  systemctl restart allpool-mqtt.service
  echo
  echo "Aktualisiert und neu gestartet.  Log: journalctl -u allpool-mqtt -f"
fi
