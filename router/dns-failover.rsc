# RouterOS config for self-hosted-DNS failover. The failover deliberately lives
# on the EDGE ROUTER, not the core - it has to run on something other than the
# box that fails. Netwatch probes the core's DoH listener; two scripts repoint
# LAN DNS on down/up. Import-style export, addresses abstracted.

# --- Normal state: LAN DNS = core resolver, upstream over the core's DoH -------
/ip dns
set allow-remote-requests=yes servers=192.0.2.10 \
    use-doh-server=https://dns.example.com:8053/dns-query verify-doh-cert=yes

# Internal names the router also answers (handy if the core is mid-reboot).
/ip dns static
add address=192.0.2.1  name=router.lan              type=A
add address=192.0.2.10 name=gitlab.example.com      type=A
add address=192.0.2.10 name=wazuh.example.com       type=A
add address=192.0.2.10 name=traefik.example.com     type=A
add address=192.0.2.10 name=dns.example.com         type=A

# --- The probe: bare TCP connection to the DoH port, every 30s -----------------
# TCP-only, so it needs no DNS itself to run and adds no new traffic pattern
# (it is the same port live DoH already uses).
/tool netwatch
add name=p0-dns-health type=tcp-conn host=192.0.2.10 port=8053 \
    interval=30s timeout=2s \
    comment="DNS failover: probe core DoH listener" \
    down-script="/system script run p0-dns-down" \
    up-script="/system script run p0-dns-up"

# --- DOWN: do no harm. Repoint LAN DNS at public resolvers (Quad9 + Cloudflare).
# Degraded mode loses local names and the core's filtering, but the LAN KEEPS
# WORKING INTERNET. A DNS-server change on a router is a textbook hijack
# indicator, so log a loud marker - the SIEM alerts on it.
/system script
add name=p0-dns-down dont-require-permissions=yes \
    source="/ip dns set use-doh-server=\"\" servers=9.9.9.9,1.1.1.1; \
            /ip dns cache flush; \
            :log warning \"DNS-FAILOVER-DOWN core:8053 unreachable; LAN DNS -> public 9.9.9.9,1.1.1.1\""

# --- UP (auto-revert): hand DNS back to the core, behind a short debounce so a
# flapping reboot cannot yank it back and forth.
add name=p0-dns-up dont-require-permissions=yes \
    source=":delay 10s; \
            /ip dns set servers=192.0.2.10 use-doh-server=\"https://dns.example.com:8053/dns-query\"; \
            /ip dns cache flush; \
            :log warning \"DNS-FAILOVER-UP core:8053 healthy; LAN DNS -> core DoH restored\""
