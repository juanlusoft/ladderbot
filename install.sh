#!/usr/bin/env bash
# Instalador del Ladderbot v2 (Polygon). Pensado para copiar y pegar sin pensar.
#   sudo bash install.sh
# Deja el bot INSTALADO pero PARADO y sin tocar dinero: después hay que rellenar
# los 5 datos de secrets/ladderbot.env y la estrategia. El tutorial (TUTORIAL.md)
# explica el resto paso a paso.
set -euo pipefail

DEST=${DEST:-/opt/ladderbot}
SYSTEMD_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
NO_SYSTEMD=${NO_SYSTEMD:-0}
SRC="$(cd "$(dirname "$0")" && pwd)"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "hay que ejecutarlo como root:  sudo bash install.sh"

log "Comprobando python3 (se espera 3.12)"
command -v python3 >/dev/null || die "no hay python3 instalado. Instálalo: apt install -y python3 python3-venv"
python3 - <<'PY' || die "se necesita Python 3.12 (las dependencias del fichero requirements.lock son para 3.12)"
import sys
sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)
PY

log "Creando usuario de servicio 'ladderbot'"
if id ladderbot >/dev/null 2>&1; then
  echo "ya existe"
else
  useradd --system --create-home --shell /usr/sbin/nologin ladderbot
fi

log "Copiando ficheros a $DEST"
mkdir -p "$DEST"/{strategies,secrets,state}
install -m 0755 "$SRC/ladderbot.py" "$DEST/ladderbot.py"
for f in telegram_cmd.py precio_chart.py updater.py ver_saldos.py ver_tx.py leer_ingresos.py ajustar_saldo.py; do
  [ -f "$SRC/$f" ] && install -m 0755 "$SRC/$f" "$DEST/$f"
done
install -m 0644 "$SRC/VERSION" "$DEST/VERSION"
install -m 0644 "$SRC/requirements.txt" "$DEST/requirements.txt"
install -m 0644 "$SRC/requirements.lock" "$DEST/requirements.lock"

# Nada de sobrescribir configuración que ya exista (una reinstalación no debe
# borrar el presupuesto ni las claves de nadie).
if [ -f "$DEST/config.json" ]; then
  echo "config.json ya existe: no se toca"
else
  install -m 0644 "$SRC/config.example.json" "$DEST/config.json"
fi
if [ -f "$DEST/strategies/geodnet.json" ]; then
  echo "strategies/geodnet.json ya existe: no se toca"
else
  install -m 0644 "$SRC/strategies/geodnet.example.json" "$DEST/strategies/geodnet.json"
fi
if [ -f "$DEST/secrets/ladderbot.env" ]; then
  echo "secrets/ladderbot.env ya existe: no se toca"
else
  install -m 0600 "$SRC/secrets/ladderbot.env.example" "$DEST/secrets/ladderbot.env"
fi

log "Creando entorno de Python e instalando dependencias (web3 7.16.0)"
if [ ! -x "$DEST/.venv/bin/python" ]; then
  python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install --quiet --upgrade pip
if ! "$DEST/.venv/bin/pip" install --quiet --require-hashes -r "$DEST/requirements.lock"; then
  echo "El fichero de versiones fijas falló; probando la vía simple (requirements.txt)"
  "$DEST/.venv/bin/pip" install --quiet -r "$DEST/requirements.txt"
fi

log "Ajustando permisos"
chown -R ladderbot:ladderbot "$DEST"
chown -R root:root "$DEST"/*.py "$DEST/VERSION" "$DEST/requirements.txt" "$DEST/requirements.lock"
chmod 0700 "$DEST/secrets" "$DEST/state"
chmod 0600 "$DEST/secrets/ladderbot.env"
chmod 0755 "$DEST"/*.py
# el bot reescribe la estrategia (ajustar_saldo.py), así que su carpeta es suya
chown -R ladderbot:ladderbot "$DEST/strategies"

log "Instalando servicios systemd"
if [ "$NO_SYSTEMD" = 1 ]; then
  echo "NO_SYSTEMD=1: se omite systemd"
else
  install -m 0644 "$SRC/systemd/ladderbot.service" "$SYSTEMD_DIR/ladderbot.service"
  install -m 0644 "$SRC/systemd/ladderbot-telegram.service" "$SYSTEMD_DIR/ladderbot-telegram.service"
  install -m 0644 "$SRC/systemd/ladderbot-update.service" "$SYSTEMD_DIR/ladderbot-update.service"
  install -m 0644 "$SRC/systemd/ladderbot-update.path" "$SYSTEMD_DIR/ladderbot-update.path"
  systemctl daemon-reload
  systemctl enable --now ladderbot-update.path
fi

cat <<'FIN'

=== INSTALADO ===

Falta lo importante: rellenar los datos y probar. Haz esto EN ESTE ORDEN:

  1) sudo nano /opt/ladderbot/secrets/ladderbot.env     (RPC, respaldo, wallet y Telegram)
  2) sudo nano /opt/ladderbot/strategies/geodnet.json   (presupuesto y precios de tu escalera)
  3) sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py doctor
  4) sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py reconcile \
       --token GEODNET --cash-available 0 --avg-cost 0
  5) sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py run --dry-run --once

Cuando los cinco pasos salgan bien, enciende el servicio de verdad:

  sudo systemctl enable --now ladderbot ladderbot-telegram

Después, /update instala una OTA cuyo SHA-256 esté verificado. Sin una URL fija:

  /update https://servidor/ladderbot-vX.Y.Z.tar.gz SHA256

El tutorial completo, con qué significa cada cosa y los errores típicos, está en
TUTORIAL.md. Léelo antes de poner dinero real.
FIN
