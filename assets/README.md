# Diagrams & screenshots

`topology.svg` is the network diagram referenced from the top-level README -
abstracted by construction (documentation address ranges, generic role names).

Drop **redacted** screenshots here and link them from the README. The text
sanitisation gate that protects the rest of this repo **cannot scan image
pixels**, so each shot must be cropped/blurred by hand before it is committed.
Redact: real hostnames, internal/public IPs, the real domain, and any token.

Captured (embedded in the top-level README):

- `doh-verify.png` - a `kdig` query over DoH showing the TLS 1.3 session and an
  HTTP/2 POST to `/dns-query`, proving the wildcard cert is valid (not self-signed).
- `dns-failover-alert.png` - the DNS-failover firing as a Wazuh/Telegram alert
  (ACTIVATED then CLEARED). Stands in for the Netwatch-red shot and also proves the
  SIEM treats a router DNS change as a hijack indicator. Core hostname redacted.

Not captured (deliberately skipped):

- `split-horizon.png` - would be mostly redaction boxes (hostname + LAN IP + domain
  all on screen); weak payoff for the exposure.
- `vpn-only-ingress.png` - needs an external vantage point and exposes the WAN IP.

Redaction reminder: the text sanitisation gate **cannot scan image pixels**, so any
future shot must have real hostnames, internal/public IPs, the domain, and tokens
cropped or blurred by hand before committing.
