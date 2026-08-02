#!/bin/sh
# Create a self-signed code-signing identity in the login keychain.
#
# Needed because an unsigned app cannot hold Accessibility for child processes,
# and an ad-hoc signature changes with every build so the grant is lost each
# time. A stable certificate keeps the grant across rebuilds.
set -e
NAME="${1:-onair-dev}"
PW="onair"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/ext.cnf" <<EOF
[req]
distinguished_name=dn
x509_extensions=v3
prompt=no
[dn]
CN=$NAME
[v3]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
EOF

openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout "$TMP/k.pem" -out "$TMP/c.pem" -config "$TMP/ext.cnf" 2>/dev/null
# -legacy: OpenSSL 3 defaults to PKCS#12 algorithms the macOS keychain rejects.
openssl pkcs12 -export -legacy -macalg sha1 -inkey "$TMP/k.pem" -in "$TMP/c.pem" \
  -name "$NAME" -out "$TMP/id.p12" -passout "pass:$PW" 2>/dev/null
security import "$TMP/id.p12" -k "$HOME/Library/Keychains/login.keychain-db" \
  -P "$PW" -A >/dev/null
echo "created signing identity '$NAME'"
