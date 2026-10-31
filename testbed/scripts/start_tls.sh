#!/usr/bin/env bash
# TLS terminator in front of Ollama (plain HTTP :11434 -> HTTPS :11436).
# Regenerates the self-signed cert (365 days) if missing or expiring within 30 days.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f certs/cert.pem ]] || ! openssl x509 -checkend $((30*86400)) -noout -in certs/cert.pem >/dev/null; then
  mkdir -p certs
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout certs/key.pem -out certs/cert.pem \
    -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
  cat certs/cert.pem certs/key.pem > certs/both.pem
  echo "regenerated certs (365 days) - restart any running socat" >&2
fi
exec socat OPENSSL-LISTEN:11436,cert=certs/both.pem,verify=0,fork,reuseaddr TCP:127.0.0.1:11434
