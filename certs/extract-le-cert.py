#!/usr/bin/env python3
"""Extract the *.example.com cert + chain + key from Traefik's acme.json and
write them where OpenVPN expects them. Fired by openvpn-cert-refresh.path
whenever the reverse proxy rewrites its cert store."""
import json, base64
from pathlib import Path

ACME     = Path("/srv/docker/tier0/certs/acme.json")
OUT_CERT = Path("/etc/openvpn/server/le-server.crt")
OUT_KEY  = Path("/etc/openvpn/server/le-server.key")
OUT_CA   = Path("/etc/openvpn/server/le-chain.crt")

data  = json.loads(ACME.read_text())
certs = data["letsencrypt"]["Certificates"]
entry = next(c for c in certs if c["domain"]["main"] == "example.com")

cert_pem = base64.b64decode(entry["certificate"]).decode()
key_pem  = base64.b64decode(entry["key"]).decode()

# certificate is a fullchain: first PEM block is the server cert, the rest are
# intermediates. OpenVPN wants them in separate files.
blocks      = cert_pem.split("-----END CERTIFICATE-----\n")
server_cert = blocks[0] + "-----END CERTIFICATE-----\n"
chain       = ("-----END CERTIFICATE-----\n".join(blocks[1:])).strip()

OUT_CERT.write_text(server_cert)
OUT_KEY.write_text(key_pem)
OUT_CA.write_text(chain)
OUT_KEY.chmod(0o600)

import subprocess
result = subprocess.run(
    ["openssl", "x509", "-noout", "-subject", "-dates", "-in", str(OUT_CERT)],
    capture_output=True, text=True,
)
print(result.stdout)
print("Extracted OK")
print(f"  cert  -> {OUT_CERT}")
print(f"  key   -> {OUT_KEY}")
print(f"  chain -> {OUT_CA}")
