#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
host_only_ip="$(
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["host_only_ip"])' \
    "$project_root/lab-config.json"
)"
cert_dir="${XDG_CONFIG_HOME:-$HOME/.config}/enterprise-sre-automation/tls"
ca_key="$cert_dir/lab-ca.key"
ca_cert="$cert_dir/lab-ca.crt"
api_key="$cert_dir/api.key"
api_csr="$cert_dir/api.csr"
api_cert="$cert_dir/api.crt"
extensions="$cert_dir/api-extensions.cnf"

if [[ -e "$ca_key" || -e "$ca_cert" || -e "$api_key" || -e "$api_cert" ]]; then
  printf 'Refusing to overwrite existing TLS material in %s\n' "$cert_dir" >&2
  exit 1
fi

command -v openssl >/dev/null || {
  printf 'openssl is required; install it with: sudo apt install openssl\n' >&2
  exit 1
}

install -d -m 700 "$cert_dir"

openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -noenc \
  -keyout "$ca_key" \
  -out "$ca_cert" \
  -subj "/CN=Enterprise SRE Lab Development CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc \
  -keyout "$api_key" \
  -out "$api_csr" \
  -subj "/CN=Enterprise SRE Lab Automation API"

printf '%s\n' \
  'basicConstraints=critical,CA:FALSE' \
  'keyUsage=critical,digitalSignature' \
  'extendedKeyUsage=serverAuth' \
  "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:${host_only_ip}" \
  > "$extensions"

openssl x509 -req -sha256 -days 397 \
  -in "$api_csr" \
  -CA "$ca_cert" \
  -CAkey "$ca_key" \
  -CAcreateserial \
  -out "$api_cert" \
  -extfile "$extensions"

rm -f "$api_csr" "$extensions" "$cert_dir/lab-ca.srl"
chmod 600 "$ca_key" "$api_key"
chmod 644 "$ca_cert" "$api_cert"

printf 'Created development CA and API TLS certificate in %s\n' "$cert_dir"
printf 'Keep lab-ca.key private; deploy only lab-ca.crt to monitoring01.\n'
