#!/usr/bin/env bash
#
# Nexora Books — put it on an Ubuntu or Debian server.
#
#     sudo bash deploy/install-on-server.sh books.tavonetworks.tech
#
# Run this from inside the unpacked Nexora Books folder. It makes an account
# for the software to run under, installs what it needs into a private virtual
# environment, sets it to start on boot, and puts nginx in front of it.
#
# It does NOT get the certificate — certbot does, and the last lines tell you
# the one command to run. Everything here can be run twice safely.

set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
    echo "Which subdomain is this for?"
    echo "    sudo bash deploy/install-on-server.sh books.tavonetworks.tech"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo."
    exit 1
fi

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/nexorabooks
DATA=/var/lib/nexorabooks

echo
echo "  Nexora Books → $DOMAIN"
echo "  ============================================================"
echo

# --- 1. Nothing runs as root -----------------------------------------------
if ! id nexora >/dev/null 2>&1; then
    echo "  [1/7] Making the 'nexora' account..."
    adduser --system --group --home /nonexistent --no-create-home nexora
else
    echo "  [1/7] The 'nexora' account is already there."
fi

echo "  [2/7] Installing Python, nginx and certbot..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx certbot python3-certbot-nginx

# --- 3. The application ------------------------------------------------------
echo "  [3/7] Copying the application to $APP..."
mkdir -p "$APP"
# The seller's private key issues licences in your name. It has no business on
# a machine that faces the internet, so it is never copied even if it is
# sitting in the folder this is being run from.
rsync -a --delete \
      --exclude 'seller/' \
      --exclude 'build_env/' \
      --exclude 'dist/' \
      --exclude 'build/' \
      --exclude '__pycache__/' \
      --exclude '.pytest_cache/' \
      "$SOURCE"/ "$APP"/
rm -f "$APP/issue_licence.py" "$APP/make_licence_keys.py"

echo "  [4/7] Installing what it needs, in its own environment..."
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"

# --- 5. The books ------------------------------------------------------------
echo "  [5/7] Preparing the data folder at $DATA..."
mkdir -p "$DATA"
chown -R nexora:nexora "$DATA"
chmod 700 "$DATA"
chown -R root:root "$APP"
chmod -R a+rX "$APP"

# --- 6. Start on boot --------------------------------------------------------
echo "  [6/7] Setting it to start on boot..."
install -m 644 "$APP/deploy/nexorabooks.service" /etc/systemd/system/nexorabooks.service
systemctl daemon-reload
systemctl enable --now nexorabooks
sleep 2
systemctl is-active --quiet nexorabooks || {
    echo
    echo "  It did not start. What went wrong is in:"
    echo "      journalctl -u nexorabooks -n 40 --no-pager"
    exit 1
}

# --- 7. nginx ----------------------------------------------------------------
echo "  [7/7] Putting nginx in front of it..."
ZONE='limit_req_zone $binary_remote_addr zone=nexora_login:10m rate=12r/m;'
if ! grep -q "zone=nexora_login" /etc/nginx/nginx.conf; then
    # Goes inside http { }, which is where nginx insists this directive lives.
    sed -i "0,/^http {/s|^http {|http {\n    $ZONE|" /etc/nginx/nginx.conf
fi

sed "s/books\.example\.com/$DOMAIN/g" \
    "$APP/deploy/nginx-nexorabooks.conf" > /etc/nginx/sites-available/nexorabooks
ln -sf /etc/nginx/sites-available/nexorabooks /etc/nginx/sites-enabled/nexorabooks
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo
echo "  ============================================================"
echo "   Nexora Books is running and nginx is in front of it."
echo
echo "   ONE THING LEFT — get the certificate:"
echo
echo "       sudo certbot --nginx -d $DOMAIN"
echo
echo "   Point $DOMAIN at this server's IP address first, or certbot"
echo "   cannot prove the name is yours and will refuse."
echo
echo "   Then open  https://$DOMAIN  and sign in as  admin / admin123"
echo "   and CHANGE THAT PASSWORD BEFORE YOU DO ANYTHING ELSE."
echo
echo "   Read deploy/README.txt. There are four things on it that matter"
echo "   more than the install did, and the licence is one of them."
echo "  ============================================================"
echo
