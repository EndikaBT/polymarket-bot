#!/usr/bin/env bash
# =============================================================================
# Polymarket Bot — script de actualización
# Uso: bash /opt/polymarket-bot/deploy/update.sh
# =============================================================================
set -euo pipefail

INSTALL_DIR="/opt/polymarket-bot"
SERVICE_NAME="polymarket-bot"
VENV="$INSTALL_DIR/.venv"
SERVICE_USER="polybot"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }

[[ $EUID -eq 0 ]] || { echo "Ejecuta como root: sudo bash deploy/update.sh"; exit 1; }

info "Descargando última versión…"
sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull --ff-only

info "Actualizando dependencias Python…"
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

info "Reiniciando servicio…"
systemctl restart "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    success "Bot actualizado y funcionando"
else
    echo "Error al iniciar — logs:"
    journalctl -u "$SERVICE_NAME" -n 20
fi
