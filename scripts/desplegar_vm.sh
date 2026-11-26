#!/usr/bin/env bash
# Instala o actualiza E.D.I.T.H. en una máquina virtual Ubuntu 22.04 / 24.04 con Docker.
#
#   bash desplegar_vm.sh                 # primera vez y cada vez que quieras actualizar
#
# Variables opcionales:
#   EDITH_REPO=https://github.com/usuario/repo.git   repositorio a clonar
#   EDITH_RAMA=main                                  rama a usar
#   EDITH_DIR=~/edith                                carpeta de instalación
#
# Se puede correr varias veces sin problema: lo que ya está hecho se salta.
set -euo pipefail

REPO="${EDITH_REPO:-https://github.com/CamiloMutis/E.D.I.T.H..git}"
RAMA="${EDITH_RAMA:-main}"
DIR="${EDITH_DIR:-$HOME/edith}"

info() { printf '\n==> %s\n' "$*"; }
falla() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -ne 0 ]] || falla "No lo corras como root; usa tu usuario normal (el script pide sudo cuando lo necesita)."
command -v apt-get >/dev/null || falla "Este script es para Ubuntu/Debian (no encontré apt-get)."

# 1. Paquetes básicos
if ! command -v git >/dev/null || ! command -v curl >/dev/null; then
    info "Instalando git y curl"
    sudo apt-get update -y
    sudo apt-get install -y git curl ca-certificates
fi

# 2. Docker (script oficial de Docker; incluye el plugin "docker compose")
if ! command -v docker >/dev/null; then
    info "Instalando Docker"
    curl -fsSL https://get.docker.com -o /tmp/instalar-docker.sh
    sudo sh /tmp/instalar-docker.sh
    rm -f /tmp/instalar-docker.sh
fi
sudo systemctl enable --now docker >/dev/null 2>&1 || true

# Permitir usar docker sin sudo (vale desde el próximo inicio de sesión).
if ! id -nG "$USER" | grep -qw docker; then
    sudo usermod -aG docker "$USER"
    info "Agregué tu usuario al grupo docker. En esta sesión uso sudo; desde la próxima no hará falta."
fi
if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi
"${DOCKER[@]}" compose version >/dev/null 2>&1 || falla "Falta el plugin 'docker compose'. Instálalo con: sudo apt-get install -y docker-compose-plugin"

# 3. Memoria de intercambio (swap): las VM gratuitas de 1 GB se quedan cortas al
#    instalar scikit-learn o al reentrenar los modelos.
memoria_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [[ "$memoria_mb" -lt 2000 ]] && ! swapon --show | grep -q .; then
    info "La VM tiene ${memoria_mb} MB de RAM; creando 2 GB de swap en /swapfile"
    sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# 4. Código
if [[ -d "$DIR/.git" ]]; then
    info "Actualizando el código en $DIR"
    git -C "$DIR" fetch --quiet origin "$RAMA"
    git -C "$DIR" checkout --quiet "$RAMA"
    git -C "$DIR" pull --ff-only --quiet origin "$RAMA"
else
    info "Descargando el código en $DIR"
    git clone --branch "$RAMA" "$REPO" "$DIR"
fi
cd "$DIR"

# 5. Secretos
if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    falla "Creé $DIR/.env a partir de .env.example. Ábrelo (nano $DIR/.env), llena TELEGRAM_BOT_TOKEN, GROQ_API_KEY y demás, y vuelve a correr este script."
fi
chmod 600 .env
grep -q '^TELEGRAM_BOT_TOKEN=.\+' .env || falla "Falta TELEGRAM_BOT_TOKEN en $DIR/.env"
grep -q '^EDITH_CLAVE_MAESTRA=.\+' .env \
    || echo "Aviso: no definiste EDITH_CLAVE_MAESTRA; la app la generará y la guardará en el volumen /datos. Respáldala."

# 6. Arrancar
info "Construyendo la imagen y arrancando el bot (la primera vez tarda unos minutos)"
"${DOCKER[@]}" compose up -d --build
"${DOCKER[@]}" image prune -f >/dev/null || true

info "Listo. Comandos útiles (desde $DIR):"
echo "  docker compose logs -f        # ver los registros"
echo "  docker compose ps             # estado del contenedor"
echo "  docker compose restart        # reiniciar"
echo "  bash scripts/desplegar_vm.sh  # actualizar a la última versión"
