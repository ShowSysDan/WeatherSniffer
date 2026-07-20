#!/usr/bin/env bash
# =============================================================================
# install.sh - WeatherSniffer Service Installer
# =============================================================================
# Usage:
#   sudo ./install.sh            # Full install: setup + install + start
#   sudo ./install.sh setup      # Create virtualenv and install dependencies
#   sudo ./install.sh install    # Create systemd service only (run setup first)
#   sudo ./install.sh start      # Start the service
#   sudo ./install.sh stop       # Stop the service
#   sudo ./install.sh status     # Show service status and recent logs
#   sudo ./install.sh uninstall  # Remove the service (data stays in Postgres)
#
# Environment variable overrides (export before running 'install'):
#   WS_WEB_PORT   HTTP port for the web UI  (default: 7170)
#   WS_HOST       Bind address              (default: 0.0.0.0)
#   WS_USER       Service user account      (default: weathersniffer)
#   WS_VENV       Virtual environment dir   (default: <project>/.venv)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (resolved at script load time)
# ---------------------------------------------------------------------------
SERVICE_NAME="weathersniffer"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="${INSTALL_DIR}/requirements.txt"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Overridable via environment
WEB_PORT="${WS_WEB_PORT:-7170}"
HOST="${WS_HOST:-0.0.0.0}"
SERVICE_USER="${WS_USER:-weathersniffer}"
VENV_DIR="${WS_VENV:-${INSTALL_DIR}/.venv}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { echo "[INFO]  $*"; }
success() { echo "[OK]    $*"; }
warn()    { echo "[WARN]  $*" >&2; }
die()     { echo "[ERROR] $*" >&2; exit 1; }

require_root() {
    [[ "$(id -u)" -eq 0 ]] || die "This command must be run as root. Try: sudo $0 $*"
}

# ---------------------------------------------------------------------------
# setup: create virtual environment and install Python dependencies
# ---------------------------------------------------------------------------
cmd_setup() {
    require_root

    echo "========================================"
    echo "  WeatherSniffer - Dependency Setup"
    echo "========================================"

    info "Checking Python version..."
    local python_bin
    python_bin="$(command -v python3 2>/dev/null)" \
        || die "python3 not found. Install it with: apt-get install python3"

    local version
    version="$("${python_bin}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    local major="${version%%.*}"
    local minor="${version##*.}"

    if [[ "${major}" -lt 3 ]] || { [[ "${major}" -eq 3 ]] && [[ "${minor}" -lt 11 ]]; }; then
        die "Python 3.11+ is required, found ${version}. Please upgrade Python."
    fi
    success "Python ${version} at ${python_bin}"

    "${python_bin}" -m venv --help >/dev/null 2>&1 \
        || die "Python venv module not found. Install with: apt-get install python3-venv"

    info "Creating virtual environment at ${VENV_DIR}..."
    "${python_bin}" -m venv "${VENV_DIR}"
    success "Virtual environment created."

    info "Installing dependencies from ${REQUIREMENTS}..."
    "${VENV_DIR}/bin/pip" install -r "${REQUIREMENTS}" --quiet \
        || die "Dependency installation failed. Check the output above (psycopg2 needs libpq-dev build-essential)."
    success "Dependencies installed into ${VENV_DIR}."

    echo ""
    success "Setup complete. Run 'sudo $0 install' to create the service."
}

# ---------------------------------------------------------------------------
# install: create systemd service
# ---------------------------------------------------------------------------
cmd_install() {
    require_root

    echo "========================================"
    echo "  WeatherSniffer - Service Installation"
    echo "========================================"

    [[ -x "${VENV_DIR}/bin/python3" ]] \
        || die "Virtual environment not found at ${VENV_DIR}. Run 'sudo $0 setup' first."

    info "Verifying installed packages in ${VENV_DIR}..."
    "${VENV_DIR}/bin/python3" -c "import flask, sqlalchemy, psycopg2, requests, apscheduler, gunicorn, flask_limiter" \
        2>/dev/null \
        || die "Required packages are missing. Run 'sudo $0 setup' first."
    success "All packages present."

    [[ -f "${INSTALL_DIR}/.env" ]] \
        || warn "No .env found — copy .env.example to .env, fill it in, and chmod 600 .env before starting."

    # Create dedicated system user (no login, no home directory)
    if ! id "${SERVICE_USER}" &>/dev/null; then
        info "Creating system user '${SERVICE_USER}'..."
        useradd --system \
                --no-create-home \
                --shell /usr/sbin/nologin \
                --comment "WeatherSniffer service account" \
                "${SERVICE_USER}"
        success "User '${SERVICE_USER}' created."
    else
        info "User '${SERVICE_USER}' already exists."
    fi

    # Grant the service user read access to the application files and venv.
    # (Files remain owned by the deploying user so git pull keeps working.)
    chmod o+rX "${INSTALL_DIR}"
    find "${INSTALL_DIR}" \
        -not -path "${INSTALL_DIR}/.git" \
        -not -path "${INSTALL_DIR}/.git/*" \
        -not -name ".env" \
        -exec chmod o+r {} \; 2>/dev/null || true

    # .env holds secrets: readable by the service user only, not world.
    if [[ -f "${INSTALL_DIR}/.env" ]]; then
        chown "${SERVICE_USER}" "${INSTALL_DIR}/.env"
        chmod 600 "${INSTALL_DIR}/.env"
    fi

    find "${VENV_DIR}" -type d -exec chmod o+rx {} \; 2>/dev/null || true
    find "${VENV_DIR}" -type f -name "python*" -exec chmod o+rx {} \; 2>/dev/null || true

    # Ensure every parent directory is traversable so the service user
    # can reach INSTALL_DIR (e.g. /home/user needs o+x when installed there).
    local parent
    parent="$(dirname "${INSTALL_DIR}")"
    while [[ "${parent}" != "/" ]]; do
        chmod o+x "${parent}" 2>/dev/null || true
        parent="$(dirname "${parent}")"
    done

    info "Writing ${SERVICE_FILE}..."
    cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=WeatherSniffer - Perry Weather poller, rules engine & actions
Documentation=https://github.com/ShowSysDan/WeatherSniffer
After=network.target postgresql.service
Wants=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}

WorkingDirectory=${INSTALL_DIR}

# WeatherSniffer runs as a SINGLE process on purpose: the APScheduler poller,
# the rules/actions engine and the retention janitor live in-process with
# in-memory state. Gunicorn must therefore run exactly one worker
# (--workers 1) — more workers would fire every job N times.
Environment=TZ=America/New_York
Environment=WEB_HOST=${HOST}
Environment=WEB_PORT=${WEB_PORT}
ExecStart=${VENV_DIR}/bin/gunicorn wsgi:app \\
    --workers 1 \\
    --threads 4 \\
    --bind ${HOST}:${WEB_PORT} \\
    --timeout 120

Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=weathersniffer

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    success "Service file written."

    info "Reloading systemd daemon..."
    systemctl daemon-reload

    info "Enabling ${SERVICE_NAME} to start on boot..."
    systemctl enable "${SERVICE_NAME}"

    echo ""
    success "Service installed and enabled."
    info "Configuration baked into service:"
    info "  Web UI port     : ${WEB_PORT}"
    info "  Bind address    : ${HOST}"
    info "  Running as user : ${SERVICE_USER}"
    info "  Virtual env     : ${VENV_DIR}"
    info "  Web server      : gunicorn (1 worker, 4 threads)"
    info "  Database/auth   : configured in ${INSTALL_DIR}/.env"
    echo ""
    info "To change these, edit ${SERVICE_FILE} then run: sudo systemctl daemon-reload"
    echo ""
    success "Run 'sudo $0 start' to launch WeatherSniffer."
}

# ---------------------------------------------------------------------------
# start / stop / status
# ---------------------------------------------------------------------------
cmd_start() {
    require_root
    info "Starting ${SERVICE_NAME}..."
    systemctl start "${SERVICE_NAME}"
    sleep 1
    systemctl status "${SERVICE_NAME}" --no-pager -l || true
    echo ""
    success "WeatherSniffer is running."
    info "Web UI:  http://localhost:${WEB_PORT}"
    info "Logs:    journalctl -u ${SERVICE_NAME} -f"
}

cmd_stop() {
    require_root
    info "Stopping ${SERVICE_NAME}..."
    systemctl stop "${SERVICE_NAME}"
    success "Service stopped."
}

cmd_status() {
    systemctl status "${SERVICE_NAME}" --no-pager -l || true
}

# ---------------------------------------------------------------------------
# uninstall: remove service (data stays in Postgres)
# ---------------------------------------------------------------------------
cmd_uninstall() {
    require_root

    echo "========================================"
    echo "  WeatherSniffer - Uninstall Service"
    echo "========================================"
    warn "This removes the systemd service."
    warn "Data in PostgreSQL (schema 'weathersniffer') will NOT be deleted."
    echo ""
    read -rp "Continue? [y/N] " confirm
    [[ "${confirm}" =~ ^[Yy]$ ]] || { info "Uninstall cancelled."; exit 0; }

    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        info "Stopping service..."
        systemctl stop "${SERVICE_NAME}"
    fi

    if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
        info "Disabling service..."
        systemctl disable "${SERVICE_NAME}"
    fi

    if [[ -f "${SERVICE_FILE}" ]]; then
        info "Removing ${SERVICE_FILE}..."
        rm "${SERVICE_FILE}"
    fi

    systemctl daemon-reload

    echo ""
    success "Service removed."
    info "To fully clean up:"
    info "  sudo rm -rf ${VENV_DIR}"
    info "  sudo userdel ${SERVICE_USER}"
    info "  -- and drop the 'weathersniffer' schema in Postgres if desired."
}

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
usage() {
    echo "Usage: $(basename "$0") [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  (none)     Full install: setup + install + start"
    echo "  setup      Create virtualenv and install Python dependencies"
    echo "  install    Create and enable systemd service"
    echo "  start      Start the service"
    echo "  stop       Stop the service"
    echo "  status     Show service status"
    echo "  uninstall  Remove the service (Postgres data preserved)"
    echo ""
    echo "Environment overrides (set before running 'install'):"
    echo "  WS_WEB_PORT   Web UI HTTP port          (default: 7170)"
    echo "  WS_HOST       Bind address              (default: 0.0.0.0)"
    echo "  WS_USER       Service user              (default: weathersniffer)"
    echo "  WS_VENV       Virtual environment dir   (default: <project>/.venv)"
}

main() {
    local command="${1:-all}"
    case "${command}" in
        setup)     cmd_setup ;;
        install)   cmd_install ;;
        start)     cmd_start ;;
        stop)      cmd_stop ;;
        status)    cmd_status ;;
        uninstall) cmd_uninstall ;;
        all)
            cmd_setup
            echo ""
            cmd_install
            echo ""
            cmd_start
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            warn "Unknown command: ${command}"
            echo ""
            usage
            exit 1
            ;;
    esac
}

main "$@"
