#!/usr/bin/env bash
# Deja a E.D.I.T.H. corriendo en segundo plano como servicio de usuario de systemd (Linux).
#
#   scripts/servicio.sh instalar        # crea el servicio, lo activa al iniciar sesión y lo arranca
#   scripts/servicio.sh instalar --siempre   # además corre aunque no hayas iniciado sesión (linger)
#   scripts/servicio.sh estado | logs | reiniciar | detener | desinstalar
#
# No necesita sudo. Si el bot falla (sin internet, Moodle caído), systemd lo
# vuelve a arrancar a los 30 segundos.
set -euo pipefail

NOMBRE="edith"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$RAIZ/.venv/bin/python"
UNIDAD="$HOME/.config/systemd/user/$NOMBRE.service"

instalar() {
    if [[ ! -x "$PYTHON" ]]; then
        echo "No encuentro $PYTHON. Crea el entorno primero: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
        exit 1
    fi
    if ! grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$RAIZ/.env" 2>/dev/null; then
        echo "Falta TELEGRAM_BOT_TOKEN en $RAIZ/.env" >&2
        exit 1
    fi
    mkdir -p "$(dirname "$UNIDAD")"
    cat > "$UNIDAD" <<EOF
[Unit]
Description=E.D.I.T.H. - agente académico en Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$RAIZ
ExecStart=$PYTHON $RAIZ/main.py
Restart=on-failure
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$NOMBRE.service"
    if [[ "${1:-}" == "--siempre" ]]; then
        loginctl enable-linger "$USER"
        echo "Linger activado: el servicio corre desde que enciende el equipo, aunque no inicies sesión."
    fi
    echo "Servicio instalado y en marcha. Revisa con: scripts/servicio.sh estado"
}

case "${1:-}" in
    instalar)    instalar "${2:-}" ;;
    estado)      systemctl --user status "$NOMBRE.service" --no-pager ;;
    logs)        journalctl --user -u "$NOMBRE.service" -n 100 -f ;;
    reiniciar)   systemctl --user restart "$NOMBRE.service" ;;
    detener)     systemctl --user stop "$NOMBRE.service" ;;
    desinstalar)
        systemctl --user disable --now "$NOMBRE.service" 2>/dev/null || true
        rm -f "$UNIDAD"
        systemctl --user daemon-reload
        echo "Servicio eliminado." ;;
    *)
        sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
        exit 1 ;;
esac
