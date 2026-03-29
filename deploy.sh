#!/usr/bin/env bash
# =============================================================================
# deploy.sh – Deploy hu-mail-bridge to a Hetzner Debian VPS
#
# Usage:
#   ./deploy.sh <vps-user>@<vps-ip>   [ssh-key-path]
#
# Example:
#   ./deploy.sh root@1.2.3.4
#   ./deploy.sh root@1.2.3.4 ~/.ssh/hetzner_ed25519
#
# The script:
#   1. Installs Docker + Docker Compose plugin on the VPS if missing (Debian-aware)
#   2. Rsyncs the project files (excluding .git, __pycache__, local volumes)
#   3. Builds the Docker image on the VPS
#   4. (Re)starts the container in detached mode
# =============================================================================

set -euo pipefail

# ------------------------------------------------------------------ #
# Argument parsing
# ------------------------------------------------------------------ #
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <user@host> [ssh-key]"
  exit 1
fi

REMOTE="$1"
SSH_KEY="${2:-}"
REMOTE_DIR="/opt/hu-mail-bridge"

# SSH ControlMaster: one TCP connection, one password prompt for the whole script.
CONTROL_SOCK="/tmp/hu-deploy-${$}.sock"
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
SSH_OPTS="$SSH_OPTS -o ControlMaster=auto -o ControlPath=$CONTROL_SOCK -o ControlPersist=60"
if [[ -n "$SSH_KEY" ]]; then
  SSH_OPTS="$SSH_OPTS -i $SSH_KEY"
fi

# Open the master connection once (this is where the password is entered)
ssh_open_master() {
  info "Opening SSH connection to $REMOTE (enter password once) …"
  ssh $SSH_OPTS -fN "$REMOTE"
}

# Close the master socket on exit (success or failure)
ssh_close_master() {
  ssh -O exit -o ControlPath="$CONTROL_SOCK" "$REMOTE" 2>/dev/null || true
}
trap ssh_close_master EXIT

ssh_cmd()  { ssh  $SSH_OPTS "$REMOTE" "$@"; }
rsync_cmd(){ rsync -az --delete -e "ssh $SSH_OPTS" "$@"; }

# ------------------------------------------------------------------ #
# Colour helpers
# ------------------------------------------------------------------ #
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
error() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ------------------------------------------------------------------ #
# Pre-flight checks (local)
# ------------------------------------------------------------------ #
[[ -f ".env" ]]                 || error ".env file not found. Copy .env.example and fill it in."
[[ -f "docker-compose.yml" ]]  || error "docker-compose.yml not found. Run this script from the project root."

info "Target: $REMOTE  →  $REMOTE_DIR"
ssh_open_master

# ------------------------------------------------------------------ #
# 1. Install Docker on the VPS (idempotent)
# ------------------------------------------------------------------ #
info "Checking Docker installation on VPS …"
ssh_cmd bash -s << 'REMOTE_SCRIPT'
set -euo pipefail

install_docker() {
  echo "Installing Docker …"

  # Remove any leftover repo files from a previous failed attempt
  rm -f /etc/apt/sources.list.d/docker.list \
        /etc/apt/keyrings/docker.gpg

  apt-get update -qq
  apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg lsb-release

  # Detect distro: "ubuntu" or "debian"
  DISTRO_ID="$(. /etc/os-release && echo "$ID")"
  DISTRO_CODENAME="$(lsb_release -cs)"

  # Debian Trixie (13) is not yet in Docker's stable channel; bookworm packages work fine
  if [[ "$DISTRO_ID" == "debian" && "$DISTRO_CODENAME" == "trixie" ]]; then
    echo "Debian Trixie detected – using Docker repo for 'bookworm' (fully compatible)"
    DISTRO_CODENAME="bookworm"
  fi

  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${DISTRO_ID}/gpg" \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/${DISTRO_ID} ${DISTRO_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list

  apt-get update -qq
  apt-get install -y docker-ce docker-ce-cli containerd.io \
      docker-buildx-plugin docker-compose-plugin

  systemctl enable --now docker
  echo "Docker installed."
}

if ! command -v docker &>/dev/null; then
  install_docker
else
  echo "Docker already installed: $(docker --version)"
fi

# Ensure compose plugin is present
if ! docker compose version &>/dev/null; then
  apt-get install -y docker-compose-plugin
fi

echo "Docker Compose: $(docker compose version)"
REMOTE_SCRIPT

# ------------------------------------------------------------------ #
# 2. Create remote directory
# ------------------------------------------------------------------ #
info "Creating remote directory $REMOTE_DIR …"
ssh_cmd "mkdir -p $REMOTE_DIR/user_data $REMOTE_DIR/data"

# ------------------------------------------------------------------ #
# 3. Rsync project files (exclude secrets that differ per env)
# ------------------------------------------------------------------ #
info "Syncing project files …"
rsync_cmd \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='user_data/' \
  --exclude='data/' \
  --exclude='.DS_Store' \
  ./ "$REMOTE:$REMOTE_DIR/"

# ------------------------------------------------------------------ #
# 4. Upload .env (kept separate from the main rsync to make it explicit)
# ------------------------------------------------------------------ #
info "Uploading .env …"
rsync_cmd .env "$REMOTE:$REMOTE_DIR/.env"

# ------------------------------------------------------------------ #
# 5. Build image and (re)start container
# ------------------------------------------------------------------ #
info "Building image and starting container on VPS …"
ssh_cmd bash -s << REMOTE_RUN
set -euo pipefail
cd $REMOTE_DIR
docker compose pull --ignore-pull-failures 2>/dev/null || true
docker compose build --pull
docker compose down --remove-orphans 2>/dev/null || true
docker compose up -d
echo ""
echo "Container status:"
docker compose ps
REMOTE_RUN

# ------------------------------------------------------------------ #
# Done
# ------------------------------------------------------------------ #
info "Deployment complete!"
echo ""
echo "  Tail logs:  ssh $REMOTE 'docker compose -f $REMOTE_DIR/docker-compose.yml logs -f'"
echo "  Stop:       ssh $REMOTE 'docker compose -f $REMOTE_DIR/docker-compose.yml down'"
echo ""
echo "  First-time auth for a user (after adding via web UI):"
echo "    ssh -t $REMOTE 'docker compose -f $REMOTE_DIR/docker-compose.yml run --rm mail-bridge --auth user@hacettepe.edu.tr'"
echo ""
echo "  Web UI: http://$REMOTE:8000"
echo "  Admin:  http://$REMOTE:8000/admin"
