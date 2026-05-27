#!/usr/bin/env python3
"""Extract the renewed *.example.com cert from Traefik's acme.json for the DoH
frontend, and restart dnsproxy only if the cert actually changed. Fired by
dnsproxy-cert-update.path. (Idempotent: a path watcher can fire on writes that
do not change the cert, so the hash check avoids needless restarts.)"""
import json, base64, os, sys, hashlib, subprocess

ACME = "/srv/docker/tier0/certs/acme.json"
CERT = "/etc/dnsproxy-cert.pem"
KEY  = "/etc/dnsproxy-key.pem"

data = json.load(open(ACME))
crt_b64 = key_b64 = None
for resolver in data.values():
    for cert in resolver.get("Certificates", []):
        if "example.com" in cert["domain"]["main"]:
            crt_b64 = cert["certificate"]
            key_b64 = cert["key"]
            break
    if crt_b64:
        break

if not crt_b64:
    print("ERROR: example.com cert not found in acme.json", file=sys.stderr)
    sys.exit(1)

crt = base64.b64decode(crt_b64)
key = base64.b64decode(key_b64)

def sha(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).digest()
    except FileNotFoundError:
        return None

if sha(CERT) == hashlib.sha256(crt).digest():
    print("cert unchanged, nothing to do")
    sys.exit(0)

open(CERT, "wb").write(crt); os.chmod(CERT, 0o644)
open(KEY,  "wb").write(key); os.chmod(KEY,  0o600)
print(f"cert updated ({len(crt)} bytes), restarting dnsproxy")
subprocess.run(["systemctl", "restart", "dnsproxy"], check=True)
