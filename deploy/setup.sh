#!/usr/bin/env bash
# =============================================================================
# Polymarket Bot — script de instalación automática para VPS (Ubuntu 22.04+)
# =============================================================================
# Uso:
#   curl -fsSL https://raw.githubusercontent.com/EndikaBT/polymarket-bot/main/deploy/setup.sh | bash
# O tras clonar el repo:
#   bash deploy/setup.sh
# =============================================================================
set -euo pipefail

# ── Colores ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }

# ── Configuración ─────────────────────────────────────────────────────────────
REPO_URL="https://github.com/EndikaBT/polymarket-bot.git"
INSTALL_DIR="/opt/polymarket-bot"
SERVICE_USER="polybot"
SERVICE_NAME="polymarket-bot"
APP_PORT=5000

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║         Polymarket Bot — Instalación automática          ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 1. Comprobaciones previas ─────────────────────────────────────────────────
info "Comprobando entorno…"
[[ $EUID -eq 0 ]] || error "Ejecuta este script como root: sudo bash deploy/setup.sh"
command -v systemctl &>/dev/null || error "Este script requiere systemd (Ubuntu 22.04+)"

OS=$(. /etc/os-release && echo "$ID")
[[ "$OS" == "ubuntu" || "$OS" == "debian" ]] || warn "Probado en Ubuntu/Debian. Otros sistemas pueden necesitar ajustes."

# ── 2. Paquetes del sistema ───────────────────────────────────────────────────
info "Actualizando paquetes del sistema…"
apt-get update -qq
apt-get install -y -qq git curl python3 python3-pip python3-venv python3-dev \
    build-essential libssl-dev libffi-dev screen ufw fail2ban

PY_VERSION=$(python3 --version | awk '{print $2}')
info "Python $PY_VERSION instalado"

# ── 3. Usuario del servicio ───────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creando usuario '$SERVICE_USER'…"
    useradd --system --shell /bin/bash --create-home "$SERVICE_USER"
    success "Usuario '$SERVICE_USER' creado"
else
    info "Usuario '$SERVICE_USER' ya existe"
fi

# ── 4. Clonar / actualizar el repositorio ─────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Actualizando repositorio existente en $INSTALL_DIR…"
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull --ff-only
else
    info "Clonando repositorio en $INSTALL_DIR…"
    git clone "$REPO_URL" "$INSTALL_DIR"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
fi
success "Código actualizado"

# ── 5. Entorno virtual y dependencias Python ──────────────────────────────────
VENV="$INSTALL_DIR/.venv"
if [[ ! -d "$VENV" ]]; then
    info "Creando entorno virtual…"
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV"
fi

info "Instalando dependencias Python…"
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
success "Dependencias instaladas"

# ── 6. Servicio systemd ───────────────────────────────────────────────────────
info "Configurando servicio systemd '$SERVICE_NAME'…"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Polymarket Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV}/bin/python app.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

# Límites de seguridad
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
success "Servicio systemd configurado"

# ── 7. Firewall básico ────────────────────────────────────────────────────────
info "Configurando firewall (ufw)…"
ufw --force reset >/dev/null 2>&1
ufw default deny incoming >/dev/null 2>&1
ufw default allow outgoing >/dev/null 2>&1
ufw allow ssh >/dev/null 2>&1
# El puerto de la app NO se abre públicamente — solo accesible via Tailscale
success "Firewall configurado (SSH abierto, puerto $APP_PORT solo local)"
ufw --force enable >/dev/null 2>&1

# ── 8. Fail2ban (protección SSH) ──────────────────────────────────────────────
info "Activando fail2ban…"
systemctl enable fail2ban --quiet
systemctl start fail2ban
success "Fail2ban activo"

# ── 9. Tailscale (acceso seguro privado) ─────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
info "Instalando Tailscale (VPN privada)…"
curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1
success "Tailscale instalado"

echo ""
warn "ACCIÓN REQUERIDA: conecta el servidor a tu red Tailscale:"
echo ""
echo -e "  ${YELLOW}tailscale up --ssh${NC}"
echo ""
echo "  1. El comando anterior mostrará una URL — ábrela en tu navegador"
echo "  2. Inicia sesión con Google / GitHub / Microsoft"
echo "  3. Aprueba el dispositivo en https://login.tailscale.com/admin"
echo ""
echo "  Instala Tailscale también en tu portátil/móvil:"
echo "  → https://tailscale.com/download"
echo ""
echo "  Una vez conectado, accede al bot en:"
echo -e "  ${GREEN}http://<IP-TAILSCALE-DEL-VPS>:${APP_PORT}${NC}"
echo "  (la IP Tailscale empieza por 100.x.x.x — ver en https://login.tailscale.com/admin)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 10. Iniciar el bot ────────────────────────────────────────────────────────
echo ""
info "Iniciando el bot…"
systemctl start "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    success "Bot iniciado correctamente"
else
    warn "El servicio no arrancó — revisa los logs:"
    echo "  journalctl -u $SERVICE_NAME -n 30"
fi

# ── Resumen final ─────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║                  Instalación completa                    ║"
echo "╠══════════════════════════════════════════════════════════╣"
printf "║  Directorio:  %-42s ║\n" "$INSTALL_DIR"
printf "║  Servicio:    %-42s ║\n" "systemctl {start|stop|restart|status} $SERVICE_NAME"
printf "║  Logs:        %-42s ║\n" "journalctl -u $SERVICE_NAME -f"
printf "║  Actualizar:  %-42s ║\n" "bash $INSTALL_DIR/deploy/update.sh"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Próximos pasos:                                         ║"
echo "║  1. Ejecuta:  tailscale up --ssh                         ║"
echo "║  2. Autoriza el dispositivo en login.tailscale.com/admin ║"
echo "║  3. Instala Tailscale en tu portátil/móvil               ║"
echo "║  4. Accede a http://<IP-Tailscale>:5000                  ║"
echo "║  5. Configura clave privada en Ajustes del bot           ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
