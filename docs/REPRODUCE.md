# Reproducing the network layer on a fresh Ubuntu 24.04 LTS

This guide walks a reader through rebuilding the **network layer** of the homelab
on a clean install of **Ubuntu 24.04 LTS (Noble)**, from scratch and by hand. It
is the manual translation of the tested Ansible roles that automate the same work.

**Source roles** (from the private rebuild playbook, `roles/<name>`):
`sysctl`, `dns`, `unbound`, `dnsproxy`, `ufw`, `nginx`, `openvpn`, `cloudflared`,
`geoblock-update`. The role order below follows the playbook's dependency-aware
sequence.

> **Conventions used in this guide.** Addresses use RFC 5737 documentation ranges,
> matching the repo README:
> Management LAN `192.0.2.0/24` (core server = `192.0.2.10`, edge router =
> `192.0.2.1`), VPN clients `198.51.100.0/24`, Docker bridges `203.0.113.0/24`.
> The wildcard domain is `*.example.com`. Ports that are real-but-private are shown
> as placeholders: `<SSH_PORT>`, `<VPN_PORT>`. Substitute your own values.
> Where a step genuinely requires root, the command is shown with `sudo` but you
> should review it before running.

---

## Contents

- [0. Prerequisites](#0-prerequisites)
- [1. Kernel network hardening (`sysctl` role)](#1-kernel-network-hardening-sysctl-role)
- [2. Stub-resolver + DNS-over-TLS (`dns` role)](#2-stub-resolver--dns-over-tls-dns-role)
- [3. Self-hosted recursive resolver (`unbound` role)](#3-self-hosted-recursive-resolver-unbound-role)
- [4. DoH frontend (`dnsproxy` role)](#4-doh-frontend-dnsproxy-role)
- [5. Host firewall (`ufw` role)](#5-host-firewall-ufw-role)
- [6. OpenVPN - the single inbound path (`openvpn` role)](#6-openvpn---the-single-inbound-path-openvpn-role)
- [7. Reverse proxy front-ends for internal services (`nginx` role)](#7-reverse-proxy-front-ends-for-internal-services-nginx-role)
- [8. Outbound tunnel for public services (`cloudflared` role)](#8-outbound-tunnel-for-public-services-cloudflared-role)
- [9. Weekly GeoIP blocklist refresh (`geoblock-update` role)](#9-weekly-geoip-blocklist-refresh-geoblock-update-role)
- [Order of operations (summary)](#order-of-operations-summary)

---

## 0. Prerequisites

- A fresh Ubuntu 24.04 LTS install with a non-root user that has `sudo`.
- The host has a static or reserved LAN address (this guide assumes `192.0.2.10`).
- You can reach the box over SSH (initially on default port 22 is fine).
- A wildcard TLS certificate for `*.example.com` is available from your reverse
  proxy. This repo's network pieces (dnsproxy DoH, OpenVPN) *consume* that cert;
  obtaining and renewing it is the reverse proxy's job (see the repo README,
  "Certificates"). For a from-scratch run you can begin with a self-signed cert and
  swap it in later.

Install the base tooling these roles rely on:

```bash
sudo apt update
sudo apt install -y curl wget jq git openssl
```

> The full lab base role also installs Docker, nginx, OpenVPN, easy-rsa and more in
> one shot. This guide installs each package next to the step that needs it so the
> network layer can be reproduced on its own.

---

## 1. Kernel network hardening (`sysctl` role)

These sysctls enable safe forwarding (needed by OpenVPN and Docker) and turn on
anti-spoofing protections. Write them to a drop-in:

```bash
sudo tee /etc/sysctl.d/99-hardening.conf > /dev/null <<'EOF'
# Strict reverse path filtering
net.ipv4.conf.default.rp_filter=1
net.ipv4.conf.all.rp_filter=1

# Don't send ICMP redirects
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.default.send_redirects=0

# Don't accept ICMP redirects
net.ipv4.conf.all.accept_redirects=0
net.ipv4.conf.default.accept_redirects=0
net.ipv6.conf.all.accept_redirects=0
net.ipv6.conf.default.accept_redirects=0

# Don't accept source-routed packets
net.ipv4.conf.all.accept_source_route=0
net.ipv6.conf.all.accept_source_route=0

# Log martian packets
net.ipv4.conf.all.log_martians=1
net.ipv4.conf.default.log_martians=1

# Enable IP forwarding (for OpenVPN + Docker)
net.ipv4.ip_forward=1

# TCP SYN cookies
net.ipv4.tcp_syncookies=1
EOF

sudo sysctl --system
```

**Verify:**

```bash
sysctl net.ipv4.ip_forward net.ipv4.conf.all.rp_filter net.ipv4.tcp_syncookies
# expect: ip_forward = 1, rp_filter = 1, tcp_syncookies = 1
```

---

## 2. Stub-resolver + DNS-over-TLS (`dns` role)

Ubuntu ships `systemd-resolved` with a stub listener on `127.0.0.53:53`. This step
points the host's *own* resolution at upstream resolvers over DoT. (Step 3 then
stands up Unbound as the real recursive resolver; the two are reconciled by taking
the stub off port 53.)

```bash
sudo mkdir -p /etc/systemd/resolved.conf.d
sudo tee /etc/systemd/resolved.conf.d/dot.conf > /dev/null <<'EOF'
[Resolve]
DNS=<DNS_PRIMARY> <DNS_SECONDARY>
FallbackDNS=<DNS_FALLBACK>
DNSOverTLS=yes
DNSSEC=allow-downgrade
EOF

sudo systemctl enable --now systemd-resolved
sudo systemctl restart systemd-resolved
```

> `<DNS_PRIMARY>` etc. are public DoT resolvers in the form
> `1.1.1.1#cloudflare-dns.com`. Pick your preferred upstreams.

**Verify:**

```bash
resolvectl status | grep -i "DNS over TLS"
# expect: +DNSOverTLS / yes
```

---

## 3. Self-hosted recursive resolver (`unbound` role)

Unbound is the lab's real resolver: recursive, DNSSEC-validating, query-logged to
the SIEM, with split-horizon records for internal names.

```bash
sudo apt install -y unbound
```

Deploy the listener + per-segment access control:

```bash
sudo tee /etc/unbound/unbound.conf.d/local.conf > /dev/null <<'EOF'
server:
    interface: 0.0.0.0
    port: 53
    access-control: 127.0.0.0/8 allow
    access-control: 203.0.113.0/24 allow
    access-control: 192.0.2.0/24 allow
    access-control: 198.51.100.0/24 allow
    qname-minimisation: yes
    hide-identity: yes
    hide-version: yes
EOF
```

> The `203.0.113.0/24` entry stands in for the Docker bridge range so containers
> can resolve through Unbound. The `192.0.2.0/24` and `198.51.100.0/24` entries are
> the LAN and VPN segments.

Deploy split-horizon records and the detection-driven blocklist. Internal names
resolve to the core's address; the `always_nxdomain` zones pin a set of reverse-DNS
(PTR) delegations and the forward name they point at, after a SIEM finding showed
hosts following those referrals out to an unfamiliar nameserver:

```bash
sudo tee /etc/unbound/unbound.conf.d/local-zones.conf > /dev/null <<'EOF'
server:
    # Pin the PTR delegation zones + forward name flagged by a triaged SIEM alert
    # so Unbound never follows the referral. (Real zones redacted.)
    local-zone: "<PTR_ZONE_1>.in-addr.arpa."   always_nxdomain
    local-zone: "<PTR_ZONE_2>.in-addr.arpa."   always_nxdomain
    local-zone: "<PTR_ZONE_3>.in-addr.arpa."   always_nxdomain
    local-zone: "<BLOCKED_NS_DOMAIN>." always_nxdomain

    local-zone: "example.com." static
    local-data: "gitlab.example.com.    A 192.0.2.10"
    local-data: "wazuh.example.com.     A 192.0.2.10"
    local-data: "traefik.example.com.   A 192.0.2.10"
    local-data: "dns.example.com.       A 192.0.2.10"
    local-data: "vpn.example.com.       A 192.0.2.10"
    local-data: "www.example.com.       A 192.0.2.10"
    # ... one local-data line per internal service; the wildcard cert already covers it
EOF
```

Query logging (shipped to the SIEM), local remote-control socket, and the DNSSEC
trust anchor:

```bash
sudo tee /etc/unbound/unbound.conf.d/logging.conf > /dev/null <<'EOF'
server:
    verbosity: 2
    log-queries: yes
    log-replies: yes
EOF

# remote-control.conf and root-auto-trust-anchor-file.conf are the standard
# Unbound control socket + auto-managed root key; ship the packaged defaults or
# the repo's dns/unbound/ copies.
```

`systemd-resolved`'s stub and Unbound both want port 53. Take the stub off the port
(see **Gotchas** in the repo README), mask the resolvconf integration unit (it
calls a systemd-networkd DBus interface that does not exist on this box), then start
Unbound:

```bash
# /etc/systemd/resolved.conf  -> set DNSStubListener=no, then:
sudo systemctl restart systemd-resolved

sudo systemctl mask unbound-resolvconf.service
sudo systemctl enable --now unbound
```

**Verify:**

```bash
# verify with: sudo unbound-checkconf
dig @127.0.0.1 example.com +short        # expect 192.0.2.10 (split-horizon)
dig @127.0.0.1 cloudflare.com +short     # expect a real recursive answer
```

---

## 4. DoH frontend (`dnsproxy` role)

`dnsproxy` (AdGuard) puts a DNS-over-HTTPS endpoint in front of Unbound for clients
that want encrypted resolution. It listens on `:8053` over TLS and forwards plaintext
to Unbound on loopback.

Download and install the binary (pin the version you tested):

```bash
DNSPROXY_VERSION="<DNSPROXY_VERSION>"   # e.g. a tagged release like vX.Y.Z
curl -fsSL -o /tmp/dnsproxy.tar.gz \
  "https://github.com/AdguardTeam/dnsproxy/releases/download/${DNSPROXY_VERSION}/dnsproxy-linux-amd64-${DNSPROXY_VERSION}.tar.gz"
tar -xzf /tmp/dnsproxy.tar.gz -C /tmp
sudo install -m 0755 /tmp/linux-amd64/dnsproxy /usr/local/bin/dnsproxy
```

Provide the TLS cert/key (extracted from the wildcard cert; see the repo README's
cert fan-out). Until that is wired up you can drop a self-signed pair at the paths
below:

```bash
# /etc/dnsproxy-cert.pem   (mode 0644)
# /etc/dnsproxy-key.pem    (mode 0600)
```

Install the service unit:

```bash
sudo tee /etc/systemd/system/dnsproxy.service > /dev/null <<'EOF'
[Unit]
Description=dnsproxy DoH frontend for Unbound
After=unbound.service
Requires=unbound.service

[Service]
ExecStart=/usr/local/bin/dnsproxy \
  --listen=0.0.0.0 \
  --port=0 \
  --https-port=8053 \
  --tls-crt=/etc/dnsproxy-cert.pem \
  --tls-key=/etc/dnsproxy-key.pem \
  --http3=false \
  --upstream=127.0.0.1:53 \
  --timeout=3s \
  --cache=false \
  --verbose
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now dnsproxy
```

> The cert-renewal automation (a `dnsproxy-cert-update.path` watcher + script that
> re-extracts the cert and restarts dnsproxy only when the cert actually changed) is
> covered by the repo's `certs/` pieces and README. It is optional for a first
> stand-up.

**Verify:**

```bash
ss -tlnp | grep 8053          # expect dnsproxy listening on :8053
kdig -d @dns.example.com +https=/dns-query example.com   # expect TLS1.3 + HTTP/2 + answer
```

---

## 5. Host firewall (`ufw` role)

UFW is configured **deny-inbound / allow-outbound** by default, then opened only for
the deliberate paths. Note that SSH is restricted to the LAN at this stage; the VPN
rule is added in step 6 once OpenVPN is up.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing

# SSH - LAN only during rebuild
sudo ufw allow from 192.0.2.0/24 to any port <SSH_PORT> proto tcp comment 'SSH via LAN'

# Public-facing
sudo ufw allow <VPN_PORT>/udp comment 'OpenVPN'
sudo ufw allow 80/tcp  comment 'Traefik HTTP redirect'
sudo ufw allow 443/tcp comment 'Traefik HTTPS'

# DoH ingress from LAN and VPN only
sudo ufw allow from 192.0.2.0/24    to any port 8053 proto tcp comment 'DoH LAN'
sudo ufw allow from 198.51.100.0/24 to any port 8053 proto tcp comment 'DoH VPN'

# Telemetry from the edge router (NetFlow + syslog)
sudo ufw allow from 192.0.2.1 to any port 2055 proto udp comment 'NetFlow from router'
sudo ufw allow from 192.0.2.1 to any port 5514 proto udp comment 'Syslog from router'

# DNS for Docker containers (on the docker0 bridge only)
sudo ufw allow in on docker0 to any port 53 proto tcp comment 'Unbound DNS for Docker'
sudo ufw allow in on docker0 to any port 53 proto udp comment 'Unbound DNS for Docker'

# Block legacy clear-text protocols both ways
sudo ufw deny in  21/tcp
sudo ufw deny out 21/tcp
sudo ufw deny in  23/tcp
sudo ufw deny out 23/tcp

# Wazuh agent egress to the SIEM
sudo ufw allow out 1514/tcp comment 'Wazuh agent communication'
sudo ufw allow out 1515/tcp comment 'Wazuh agent enrollment'
sudo ufw allow out 9200/tcp comment 'Wazuh indexer API'

sudo ufw enable
```

**Verify:**

```bash
# verify with: sudo ufw status verbose
# expect: Default: deny (incoming), allow (outgoing); the rules above listed
```

---

## 6. OpenVPN - the single inbound path (`openvpn` role)

OpenVPN is the *only* port forwarded from the WAN. Install it and its PKI tooling:

```bash
sudo apt install -y openvpn easy-rsa
```

You need a server PKI: `ca.crt`, `server.crt`, `server.key`, `dh.pem`, `ta.key`.
Generate these with easy-rsa (out of scope here; the playbook restores them from an
encrypted backup). Place them in `/etc/openvpn/server/` with mode `0600`.

Deploy the server config:

```bash
sudo tee /etc/openvpn/server/server.conf > /dev/null <<'EOF'
port <VPN_PORT>
proto udp
dev tun
ca ca.crt
cert server.crt
key server.key
dh dh.pem
server 198.51.100.0 255.255.255.0
topology subnet
ifconfig-pool-persist ipp.txt

# Push all traffic through VPN
push "redirect-gateway def1 bypass-dhcp"

# Push DNS
push "dhcp-option DNS 192.0.2.1"
push "dhcp-option DOMAIN home"

keepalive 10 120
tls-auth ta.key 0
cipher AES-256-GCM
auth SHA256
user nobody
group nogroup
persist-key
persist-tun
status openvpn-status.log
verb 3
explicit-exit-notify 1
EOF
sudo chmod 0600 /etc/openvpn/server/server.conf

sudo systemctl enable --now openvpn-server@server
```

Now that the tunnel is up, allow SSH over the VPN subnet too:

```bash
sudo ufw allow from 198.51.100.0/24 to any port <SSH_PORT> proto tcp comment 'SSH via VPN'
```

On the **edge router**, forward exactly one WAN UDP port (`<VPN_PORT>`) to the
core's `198.51.100.0/24`-issuing listener. No other inbound NAT exists.

**Verify:**

```bash
# verify with: sudo systemctl status openvpn-server@server
ip a show tun0        # expect a tun0 interface with a 198.51.100.x address
```

---

## 7. Reverse proxy front-ends for internal services (`nginx` role)

nginx fronts two internal services (the forge and the SIEM dashboard) on internal
`.home` names. (The lab's *public* TLS termination is Traefik; this nginx layer is
internal-only.)

```bash
sudo apt install -y nginx
sudo rm -f /etc/nginx/sites-enabled/default

# Deploy the gitlab and wazuh vhosts (repo: router/ or your own templates),
# then enable them:
sudo ln -sf /etc/nginx/sites-available/gitlab /etc/nginx/sites-enabled/gitlab
sudo ln -sf /etc/nginx/sites-available/wazuh  /etc/nginx/sites-enabled/wazuh

# Self-signed cert for the internal wazuh vhost
sudo mkdir -p /etc/nginx/ssl && sudo chmod 0700 /etc/nginx/ssl
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/wazuh.key \
  -out /etc/nginx/ssl/wazuh.crt \
  -subj "/CN=wazuh.home"

sudo systemctl enable --now nginx
```

**Verify:**

```bash
# verify with: sudo nginx -t
curl -ksI https://wazuh.home/   # from a host that resolves wazuh.home internally
```

---

## 8. Outbound tunnel for public services (`cloudflared` role)

Public services are published through an **outbound-initiated** tunnel: the daemon
dials out, nothing dials in, so there is zero inbound surface. This needs a
pre-created tunnel and its credential JSON (created via `cloudflared tunnel login`
+ `cloudflared tunnel create`; out of scope here, the playbook restores them).

```bash
sudo apt install -y ./cloudflared-linux-amd64.deb   # from cloudflare's release page
sudo mkdir -p /etc/cloudflared
mkdir -p ~/.cloudflared && chmod 0700 ~/.cloudflared
# place <TUNNEL_ID>.json in ~/.cloudflared/ with mode 0600
```

Main tunnel config (`/etc/cloudflared/config.yml`):

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /home/<USER>/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: <PUBLIC_HOSTNAME>
    service: http://localhost:8080
  - service: http_status:404
```

Install and enable the service unit (the repo also ships a second tunnel for a
file-upload service via its own unit):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
```

**Verify:**

```bash
# verify with: sudo systemctl status cloudflared
# expect: active; tunnel registered, "Registered tunnel connection" in journal
```

---

## 9. Weekly GeoIP blocklist refresh (`geoblock-update` role)

A systemd timer pulls per-country IP ranges and pushes them to the **edge router**
as a firewall address-list. This runs on the core but acts on the router over SSH
(key auth required).

```bash
# Install the update script to /usr/local/bin/update-geoblock.sh (mode 0755).
# It fetches per-country aggregated zone files from ipdeny.com, builds a RouterOS
# .rsc import, scp's it to the router, runs /import, then removes the remote file.
# Configurable: ROUTER address/port, ROUTER_USER, SSH_KEY path, COUNTRIES list.

# Install the service + timer units, then enable the timer:
sudo systemctl daemon-reload
sudo systemctl enable --now geoblock-update.timer
```

**Verify:**

```bash
# verify with: sudo systemctl list-timers geoblock-update.timer
# A DNS-server / address-list change on the router is also a SIEM hijack indicator,
# so confirm the run logged a 'geoblock' tag: journalctl -t geoblock
```

---

## Order of operations (summary)

The playbook sequences these so each step's dependencies exist first:

1. `sysctl` - enable forwarding + anti-spoofing before any routing/NAT.
2. `dns` - host resolution via DoT (so apt/curl work even before Unbound).
3. `unbound` - the real recursive resolver (displaces the resolved stub on :53).
4. `dnsproxy` - DoH frontend, depends on Unbound being up.
5. `ufw` - firewall (SSH LAN-only for now).
6. `openvpn` - the single inbound path; adds the SSH-over-VPN UFW rule.
7. `nginx` - internal reverse-proxy vhosts.
8. `cloudflared` - outbound tunnel for public services.
9. `geoblock-update` - weekly router address-list refresh (timer).

A working network layer = Unbound answering on :53, dnsproxy serving DoH over a
valid cert on :8053, UFW deny-inbound with only the intended ports open, and the
VPN listener reachable on its one forwarded WAN port.
