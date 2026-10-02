#!/usr/bin/env bash
# A self-signed certificate for the dashboard's HTTPS address (https://willie.local:8443), made once.
# Browsers only give a page the microphone over HTTPS; the first visit shows a certificate warning to accept.
# The key is readable by the `willie` group (the dashboard's user) and nobody else. Safe to run again: it keeps an
# existing certificate that still has more than 30 days left.
set -euo pipefail
DIR="${WILLIE_TLS_DIR:-$HOME/.config/willie/tls}"
mkdir -p "$DIR"
if [ -f "$DIR/cert.pem" ] && openssl x509 -checkend $((30*24*3600)) -noout -in "$DIR/cert.pem" >/dev/null 2>&1; then
  echo "certificate in $DIR is still good"
else
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout "$DIR/key.pem" -out "$DIR/cert.pem" \
    -subj "/CN=willie.local" \
    -addext "subjectAltName=DNS:willie.local,DNS:willie,DNS:localhost,IP:127.0.0.1" 2>/dev/null
  echo "made a new certificate in $DIR (valid 10 years)"
fi
chmod 750 "$DIR"; chmod 640 "$DIR/key.pem" "$DIR/cert.pem"
chgrp -R willie "$DIR" 2>/dev/null || true
