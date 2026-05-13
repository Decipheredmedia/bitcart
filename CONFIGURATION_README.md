# Bitcart Deployment — Configuration & Operations Guide

> **Managed by `deploy_bitcart.py` v1.0.0**
> Fork: <https://github.com/Decipheredmedia/bitcart>

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [Environment Variables Reference](#2-environment-variables-reference)
3. [Wallet Rotation Procedure](#3-wallet-rotation-procedure)
4. [Backup & Restore Procedure](#4-backup--restore-procedure)
5. [Disaster Recovery Runbook](#5-disaster-recovery-runbook)
6. [Upgrade Procedure](#6-upgrade-procedure)
7. [Troubleshooting Matrix](#7-troubleshooting-matrix)
8. [API Authentication Walkthrough](#8-api-authentication-walkthrough)
9. [How to Add a New Coin](#9-how-to-add-a-new-coin)
10. [How to Rotate Let's Encrypt Certificates Manually](#10-how-to-rotate-lets-encrypt-certificates-manually)
11. [SEO Feature Inventory](#11-seo-feature-inventory)
12. [Security Checklist](#12-security-checklist)
13. [License & Upstream Attribution](#13-license--upstream-attribution)

---

## 1. Architecture

```
                     Internet (HTTPS / 443)
                             │
                  ┌──────────▼──────────┐
                  │      Nginx (TLS)     │
                  │  /etc/nginx/sites-  │
                  │  available/*.conf   │
                  └──────┬──────┬───────┘
                         │      │
          ┌──────────────▼──┐ ┌─▼──────────────┐
          │  Bitcart API     │ │  Admin / Store  │
          │  :8000           │ │  :8001 / :8002  │
          │  (FastAPI)       │ │  (Nuxt.js SPA)  │
          └──────┬───────────┘ └────────────────┘
                 │
    ┌────────────▼────────────┐
    │   Docker Compose Stack   │
    │  ┌─────────┐ ┌────────┐ │
    │  │Postgres │ │ Redis  │ │
    │  │:5432    │ │:6379   │ │
    │  │(127.0.1)│ │(127.0.1│ │
    │  └─────────┘ └────────┘ │
    │  ┌──────────────────────┐│
    │  │ Crypto Daemons (BTC, ││
    │  │ LTC, ETH, XMR, …)   ││
    │  └──────────────────────┘│
    └─────────────────────────┘
          │              │
    ┌─────▼──┐     ┌─────▼────┐
    │  Tor   │     │Lightning │
    │(optional│    │(optional)│
    └────────┘     └──────────┘

SEO Assets (/opt/bitcart-seo/<domain>/)
  sitemap.xml  robots.txt  humans.txt  manifest.webmanifest
  .well-known/security.txt  ads.txt  <indexnow-key>.txt
  jsonld.json

Logs
  /var/log/bitcart-deploy.log   — JSON structured log
  /var/log/nginx/<domain>-*.log — Nginx access/error logs
```

**Data flow for a payment:**

1. Customer visits `https://<store_domain>` → served by Nginx → Nuxt.js SPA (port 8002).
2. SPA calls `https://<domain>/api` → FastAPI backend (port 8000).
3. Backend queries the cryptocurrency daemon for a fresh address from the configured xpub.
4. Customer pays on-chain → daemon detects the payment → backend marks invoice paid.
5. Merchant sees funds arrive in the wallet they supplied — Bitcart **never holds keys**.

---

## 2. Environment Variables Reference

The `.env` file lives at `/opt/bitcart-docker/.env` (mode 0600). It is rendered
from `templates/env.j2` by `deploy_bitcart.py`. **Do not edit by hand** — re-run
`sudo python3 deploy_bitcart.py configure` instead.

| Variable | Example | Purpose |
|---|---|---|
| `BITCART_HOST` | `pay.example.com` | FQDN for the main API service |
| `BITCART_ADMIN_HOST` | `admin.example.com` | FQDN for the admin panel |
| `BITCART_STORE_HOST` | `shop.example.com` | FQDN for the storefront |
| `BITCART_LETSENCRYPT_HOST` | comma-separated domains | Domains to obtain TLS for |
| `BITCART_INSTALL` | `all` | Install profile (`all`, `backend`, `frontend`) |
| `BITCART_CRYPTOS` | `btc,ltc,eth` | Comma-separated list of enabled crypto daemons |
| `BITCART_REVERSEPROXY` | `nginx` | Which reverse-proxy to use (`nginx`, `caddy`, `none`) |
| `BITCART_ADDITIONAL_COMPONENTS` | `tor` | Extra optional components |
| `BITCART_EMAIL` | `admin@example.com` | Let's Encrypt registration email |
| `BITCART_LIGHTNING` | `true` / `false` | Enable Lightning Network |
| `BITCART_HTTPS_REDIRECT` | `true` | Force HTTP→HTTPS redirect |
| `POSTGRES_PASSWORD` | `<random>` | PostgreSQL password (auto-generated) |
| `POSTGRES_DB` | `bitcartcc` | PostgreSQL database name |
| `POSTGRES_USER` | `postgres` | PostgreSQL user |
| `BTC_NETWORK` | `mainnet` | Bitcoin network (`mainnet`, `testnet`, `regtest`) |
| `LTC_NETWORK` | `mainnet` | Litecoin network |
| `ETH_NETWORK` | `mainnet` | Ethereum network |

---

## 3. Wallet Rotation Procedure

> **Goal:** Replace an existing merchant wallet xpub with a new one, without
> losing historical invoice data.

### Steps

1. **Generate a new xpub** in your hardware wallet or key-management tool.
   Never reuse the old xpub.

2. **Update `config.yaml`** with the new xpub for the relevant coin:

   ```yaml
   wallets:
     btc: "xpub<NEW_EXTENDED_PUBLIC_KEY>"
   ```

3. **Run configure** to re-provision the wallet via the API:

   ```bash
   sudo python3 deploy_bitcart.py configure --config config.yaml
   ```

   The script calls `PATCH /api/wallets/{id}` with the new xpub, or creates a
   new wallet record if the old one is removed.

4. **Update the store** to reference the new wallet ID:

   The `configure` subcommand automatically patches the store's wallet list.

5. **Verify** that the new wallet appears in the admin panel at
   `https://admin.example.com` → Wallets.

6. **Archive the old wallet record** (do not delete — it preserves invoice history).

7. **Rotate the backup** (see §4) so the new xpub is included.

### Security Note

Bitcart stores **only the extended public key (xpub)** — never private keys.
Rotating a wallet does not expose funds. Private keys remain in your hardware
wallet or key-management system at all times.

---

## 4. Backup & Restore Procedure

### What is backed up

| Item | Location |
|---|---|
| PostgreSQL database dump | Included in tarball |
| Docker `.env` file | `/opt/bitcart-docker/.env` |
| SEO assets | `/opt/bitcart-seo/` |
| Install directory | `/opt/bitcart/` |

> **Not backed up automatically:** private keys (xpubs are backed up — store
> private keys separately in a hardware wallet or encrypted vault).

### Create a backup

```bash
sudo python3 deploy_bitcart.py backup \
  --config config.yaml \
  --output /opt/bitcart-backups/backup-$(date +%Y%m%d).tar.gz
```

Verify the archive:

```bash
tar -tzf /opt/bitcart-backups/backup-20240101.tar.gz | head -20
```

### Restore from backup

```bash
# 1. Stop the running stack
sudo docker compose -f /opt/bitcart-docker/docker-compose.yml down

# 2. Run restore
sudo python3 deploy_bitcart.py restore \
  --config config.yaml \
  --input /opt/bitcart-backups/backup-20240101.tar.gz

# 3. Restart the stack
cd /opt/bitcart-docker && sudo bash setup.sh
```

### Automated backup schedule

Add to `/etc/cron.d/bitcart-backup`:

```cron
0 3 * * * root python3 /path/to/deploy_bitcart.py backup \
  --config /path/to/config.yaml \
  --output /opt/bitcart-backups/backup-$(date +\%Y\%m\%d).tar.gz \
  && find /opt/bitcart-backups -name "*.tar.gz" -mtime +30 -delete
```

---

## 5. Disaster Recovery Runbook

### Scenario A — Single container crash

```bash
# Identify the crashed container
docker compose -f /opt/bitcart-docker/docker-compose.yml ps

# Restart it
docker compose -f /opt/bitcart-docker/docker-compose.yml restart <service-name>

# Tail logs
docker compose -f /opt/bitcart-docker/docker-compose.yml logs -f <service-name>
```

### Scenario B — Full server loss (rebuild from backup)

1. Provision a new Ubuntu 22.04 VM with the same domain's DNS A-records
   updated to the new IP.
2. Copy your backup tarball and `config.yaml` to the new server.
3. Run a fresh install **without** the `--config` flag first to install system
   dependencies, then restore:

   ```bash
   # Install system deps only
   sudo apt-get update && sudo apt-get install -y python3 git
   sudo python3 deploy_bitcart.py install --dry-run --config config.yaml
   # Full install
   sudo python3 deploy_bitcart.py install --config config.yaml
   # Restore data
   sudo python3 deploy_bitcart.py restore \
     --config config.yaml \
     --input /path/to/backup.tar.gz
   ```

4. Verify:

   ```bash
   sudo python3 deploy_bitcart.py verify --config config.yaml
   ```

### Scenario C — Postgres data corruption

```bash
# Shell into postgres container
docker compose -f /opt/bitcart-docker/docker-compose.yml exec postgres bash

# Inside container: drop and recreate the database
psql -U postgres -c "DROP DATABASE bitcartcc;"
psql -U postgres -c "CREATE DATABASE bitcartcc OWNER postgres;"
exit

# Restore dump from backup tarball
tar -xzf /path/to/backup.tar.gz --to-stdout bitcart-pg-dump.sql \
  | docker compose -f /opt/bitcart-docker/docker-compose.yml \
    exec -T postgres psql -U postgres bitcartcc
```

### Scenario D — TLS certificates expired

See §10 for manual TLS rotation.

---

## 6. Upgrade Procedure

Bitcart follows the `setup.sh` upgrade model from `bitcart-docker`.

```bash
# 1. Run update subcommand (pulls latest tags, re-runs setup.sh)
sudo python3 deploy_bitcart.py update --config config.yaml

# 2. Watch containers restart
docker compose -f /opt/bitcart-docker/docker-compose.yml logs -f

# 3. Re-verify
sudo python3 deploy_bitcart.py verify --config config.yaml
```

**Pinning to a specific version:**

Edit `config.yaml` or use `git checkout <tag>` inside `/opt/bitcart` before
running the update. The `RepoFetcher` class automatically checks for the latest
semver tag and pins to it.

**Rolling back:**

```bash
cd /opt/bitcart
git checkout <previous-tag>
cd /opt/bitcart-docker
git checkout <previous-tag>
bash setup.sh
```

---

## 7. Troubleshooting Matrix

### 1. Containers not starting after `install`

**Symptom:** `docker compose ps` shows containers in `Restarting` state.

**Diagnostic:**

```bash
docker compose -f /opt/bitcart-docker/docker-compose.yml logs --tail=50
```

**Fix:** Check for port conflicts. Ensure ports 8000–8002 are free.

---

### 2. Nginx returns 502 Bad Gateway

**Symptom:** `curl -I https://pay.example.com` returns `502`.

**Diagnostic:**

```bash
curl -v http://127.0.0.1:8000/api/  # Check API directly
sudo nginx -t                        # Test Nginx config
sudo tail -n 50 /var/log/nginx/pay.example.com-error.log
```

**Fix:** Confirm the API container is healthy. Restart if needed:

```bash
docker compose -f /opt/bitcart-docker/docker-compose.yml restart backend
```

---

### 3. Let's Encrypt certificate issuance fails

**Symptom:** `certbot` exits with `Connection refused` or DNS error.

**Diagnostic:**

```bash
# Verify DNS
dig +short pay.example.com
# Verify port 80 is reachable from outside
curl -I http://pay.example.com/.well-known/acme-challenge/test
```

**Fix:** Ensure DNS A-records are propagated and port 80 is open in UFW and
any cloud firewall (security group). Let's Encrypt requires port 80 for
the HTTP-01 challenge even if you redirect to HTTPS.

---

### 4. Wallet not appearing in admin panel

**Symptom:** Wallet created via API but not visible in UI.

**Diagnostic:**

```bash
# Check API response
TOKEN=$(curl -s -X POST https://pay.example.com/api/token \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<pw>","permissions":["full_control"]}' \
  | jq -r .access_token)

curl -s https://pay.example.com/api/wallets \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Fix:** Confirm the wallet currency daemon is listed in `BITCART_CRYPTOS` in
`.env`. Re-run configure.

---

### 5. TLS certificate renewal fails silently

**Symptom:** Certificate approaching expiry; renewal cron not working.

**Diagnostic:**

```bash
sudo certbot renew --dry-run
sudo systemctl status certbot.timer
```

**Fix:** Ensure the certbot systemd timer is enabled:

```bash
sudo systemctl enable --now certbot.timer
```

---

### 6. sitemap.xml returns 404

**Symptom:** `curl https://shop.example.com/sitemap.xml` returns 404.

**Diagnostic:**

```bash
ls -la /opt/bitcart-seo/shop.example.com/
sudo nginx -T | grep sitemap
```

**Fix:** Re-run SEO generation:

```bash
sudo python3 deploy_bitcart.py configure --config config.yaml --seo-only
```

---

### 7. Payments not detected

**Symptom:** Customer paid on-chain but invoice stays "pending".

**Diagnostic:**

```bash
# Check daemon connectivity
docker compose -f /opt/bitcart-docker/docker-compose.yml logs btc
# Verify wallet xpub is correct in API
TOKEN=…
curl -s https://pay.example.com/api/wallets -H "Authorization: Bearer $TOKEN" \
  | jq '.[].xpub'
```

**Fix:** Confirm the xpub is correct and the daemon is fully synced. Sync
progress visible in daemon logs.

---

### 8. Admin panel login fails

**Symptom:** 401 on POST /api/token.

**Diagnostic:**

```bash
curl -v -X POST https://pay.example.com/api/token \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<pw>","permissions":["full_control"]}'
```

**Fix:** Reset admin password via Django management command inside the backend
container:

```bash
docker compose -f /opt/bitcart-docker/docker-compose.yml exec backend \
  python3 -m alembic upgrade head
```

Or re-provision the user via the script.

---

### 9. UFW blocks Docker networking

**Symptom:** Containers cannot reach the internet; DNS lookups fail inside containers.

**Fix:** Docker manages its own iptables rules. Ensure UFW's `DEFAULT_FORWARD_POLICY`
is `ACCEPT`:

```bash
grep FORWARD /etc/default/ufw
# Should read: DEFAULT_FORWARD_POLICY="ACCEPT"
sudo sed -i 's/DEFAULT_FORWARD_POLICY="DROP"/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
sudo ufw reload
```

---

### 10. robots.txt does not reference sitemap

**Symptom:** `curl https://shop.example.com/robots.txt` does not contain `Sitemap:`.

**Fix:** Regenerate:

```bash
sudo python3 deploy_bitcart.py configure --config config.yaml --seo-only
sudo nginx -s reload
```

---

### 11. fail2ban banning legitimate traffic

**Diagnostic:**

```bash
sudo fail2ban-client status bitcart-api
sudo fail2ban-client set bitcart-api unbanip <IP>
```

**Fix:** Whitelist your IP in `/etc/fail2ban/jail.local`:

```ini
[DEFAULT]
ignoreip = 127.0.0.1/8 <your-ip>
```

---

### 12. Postgres out of disk space

**Diagnostic:**

```bash
df -h /var/lib/docker
du -sh /var/lib/docker/volumes/
```

**Fix:** Run VACUUM inside the container:

```bash
docker compose -f /opt/bitcart-docker/docker-compose.yml exec postgres \
  psql -U postgres bitcartcc -c "VACUUM FULL ANALYZE;"
```

---

### 13. Lightning channel not opening

**Symptom:** `POST /api/wallets/{id}/channels/open` returns 400.

**Diagnostic:**

```bash
docker compose -f /opt/bitcart-docker/docker-compose.yml logs lnd
```

**Fix:** Ensure `BITCART_LIGHTNING=true` in `.env` and port 9735/tcp is open in UFW.

---

### 14. Tor hidden service address not generated

**Symptom:** `BITCART_ADDITIONAL_COMPONENTS=tor` set but no `.onion` address visible.

**Diagnostic:**

```bash
docker compose -f /opt/bitcart-docker/docker-compose.yml logs tor
cat /var/lib/tor/bitcart/hostname
```

**Fix:** Ensure `tor_enable: true` in `config.yaml`, re-run configure.

---

### 15. Cron sitemap regeneration not running

**Diagnostic:**

```bash
sudo crontab -l
cat /etc/cron.d/bitcart-sitemap
sudo journalctl -u cron | grep bitcart
```

**Fix:** Confirm `/etc/cron.d/bitcart-sitemap` exists with correct permissions (0644):

```bash
ls -la /etc/cron.d/bitcart-sitemap
sudo systemctl restart cron
```

---

## 8. API Authentication Walkthrough

Bitcart uses JWT Bearer tokens. All authenticated requests must include:

```
Authorization: Bearer <access_token>
```

### Step 1 — Obtain a token

```bash
curl -s -X POST https://pay.example.com/api/token \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin@example.com",
    "password": "YourAdminPassword",
    "permissions": ["full_control"]
  }' | jq .
```

Response:

```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9…",
  "token_type": "bearer"
}
```

### Step 2 — List wallets

```bash
TOKEN="eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9…"

curl -s https://pay.example.com/api/wallets \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### Step 3 — Create an invoice

```bash
curl -s -X POST https://pay.example.com/api/invoices \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "price": 10.00,
    "currency": "USD",
    "store_id": "<store_id>",
    "order_id": "order-001",
    "notification_url": "https://yourapp.example.com/webhook/bitcart"
  }' | jq .
```

### Step 4 — Check invoice status

```bash
curl -s https://pay.example.com/api/invoices/<invoice_id> \
  -H "Authorization: Bearer $TOKEN" | jq .status
```

### Step 5 — Webhook verification

Bitcart signs webhook payloads with HMAC-SHA256 using the notification URL as
the key. Verify in your receiver:

```python
import hmac, hashlib
expected = hmac.new(
    key=b"<your-shared-secret>",
    msg=request.body,
    digestmod=hashlib.sha256,
).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-Signature"])
```

---

## 9. How to Add a New Coin

1. **Verify the coin is supported** by the upstream Bitcart daemon list:
   <https://docs.bitcartcc.com/support/faq#supported-currencies>

2. **Add the coin to `config.yaml`**:

   ```yaml
   wallets:
     newcoin: "xpub<NEW_COIN_EXTENDED_PUBLIC_KEY>"
   cryptos:
     - btc
     - newcoin
   ```

3. **Re-run configure** to update `.env` and provision the wallet:

   ```bash
   sudo python3 deploy_bitcart.py configure --config config.yaml
   ```

4. **Verify** the new daemon is running:

   ```bash
   docker compose -f /opt/bitcart-docker/docker-compose.yml ps | grep newcoin
   ```

5. **Add validation logic** in `deploy_bitcart.py`'s `validate_wallet()` function
   for the new coin's address format if it differs from existing patterns.

---

## 10. How to Rotate Let's Encrypt Certificates Manually

### Force renewal for a single domain

```bash
sudo certbot renew --cert-name pay.example.com --force-renewal
sudo systemctl reload nginx
```

### Revoke and re-issue (after key compromise)

```bash
sudo certbot revoke --cert-path /etc/letsencrypt/live/pay.example.com/cert.pem
sudo certbot delete --cert-name pay.example.com
sudo certbot --nginx -d pay.example.com \
  --email admin@example.com --agree-tos --non-interactive
sudo systemctl reload nginx
```

### Verify expiry

```bash
sudo certbot certificates
# or
echo | openssl s_client -connect pay.example.com:443 -servername pay.example.com 2>/dev/null \
  | openssl x509 -noout -dates
```

### Automate renewal check

Certbot installs a systemd timer automatically. Confirm:

```bash
sudo systemctl list-timers | grep certbot
```

---

## 11. SEO Feature Inventory

All SEO assets are generated to `/opt/bitcart-seo/<domain>/` and served by Nginx.

| Feature | Path / Header | Verification |
|---|---|---|
| XML Sitemap | `/sitemap.xml` | `curl -s https://<domain>/sitemap.xml \| xmllint --noout -` |
| Sitemap Index | `/sitemap_index.xml` | `curl -I https://<domain>/sitemap_index.xml` |
| robots.txt | `/robots.txt` | `curl -s https://<domain>/robots.txt \| grep Sitemap` |
| JSON-LD (Organization) | Inline script tag | Google Rich Results Test |
| JSON-LD (WebSite + SearchAction) | Inline script tag | Google Rich Results Test |
| JSON-LD (Product + Offer) | Per-product page | `curl -s https://<domain>/products/<id> \| grep application/ld+json` |
| JSON-LD (BreadcrumbList) | Per-category page | Google Rich Results Test |
| Open Graph tags | `<meta property="og:*">` | `curl -s https://<domain> \| grep og:` |
| Twitter Card tags | `<meta name="twitter:*">` | `curl -s https://<domain> \| grep twitter:` |
| Canonical links | `<link rel="canonical">` | `curl -s https://<domain> \| grep canonical` |
| humans.txt | `/humans.txt` | `curl -I https://<domain>/humans.txt` |
| security.txt | `/.well-known/security.txt` | `curl https://<domain>/.well-known/security.txt` |
| ads.txt | `/ads.txt` | `curl -I https://<domain>/ads.txt` |
| Web App Manifest | `/manifest.webmanifest` | `curl -I https://<domain>/manifest.webmanifest` |
| IndexNow key | `/<key>.txt` | `curl https://<domain>/<key>.txt` |
| Google verification | `/<token>.html` | `curl -I https://<domain>/<token>.html` |
| Bing verification | `/BingSiteAuth.xml` | `curl -I https://<domain>/BingSiteAuth.xml` |
| Gzip compression | `Content-Encoding: gzip` | `curl -I -H "Accept-Encoding: gzip" https://<domain>/` |
| Cache-Control tuning | `Cache-Control: public, immutable` | `curl -I https://<domain>/favicon.ico` |
| HSTS | `Strict-Transport-Security` | `curl -I https://<domain>/ \| grep Strict` |
| X-Frame-Options | `X-Frame-Options: SAMEORIGIN` | `curl -I https://<domain>/ \| grep X-Frame` |
| Sitemap auto-ping | cron every 6 hours | `cat /etc/cron.d/bitcart-sitemap` |

### Validate sitemap XML

```bash
curl -s https://shop.example.com/sitemap.xml | xmllint --noout -
echo "Exit code: $?"   # 0 = valid XML
```

### Check SSL Labs grade

Visit <https://www.ssllabs.com/ssltest/analyze.html?d=pay.example.com> or use the CLI:

```bash
curl -s "https://api.ssllabs.com/api/v3/analyze?host=pay.example.com&all=done" \
  | jq '.endpoints[0].grade'
```

---

## 12. Security Checklist

- [ ] All domains use HTTPS with valid Let's Encrypt certificates (A+ SSL Labs grade)
- [ ] HTTP Strict Transport Security (HSTS) header present on all domains
- [ ] `X-Frame-Options: SAMEORIGIN` set
- [ ] `X-Content-Type-Options: nosniff` set
- [ ] `Referrer-Policy` set
- [ ] Content Security Policy (CSP) configured
- [ ] UFW allows only ports 22, 80, 443 (and 9735 if Lightning enabled)
- [ ] PostgreSQL bound to 127.0.0.1 only (not exposed publicly)
- [ ] Redis bound to 127.0.0.1 only
- [ ] fail2ban Bitcart-API jail active (`sudo fail2ban-client status bitcart-api`)
- [ ] unattended-upgrades enabled for automatic security patching
- [ ] Admin password meets strength requirements (≥16 chars, mixed)
- [ ] PostgreSQL password is randomly generated and stored in `.env` (mode 0600)
- [ ] No private keys stored on the server (only xpubs)
- [ ] Backups are encrypted and stored off-site
- [ ] Deployment log at `/var/log/bitcart-deploy.log` is reviewed regularly
- [ ] Docker containers run without `--privileged`
- [ ] `security.txt` is present and up to date at `/.well-known/security.txt`
- [ ] SSH key-based auth only (password auth disabled in `/etc/ssh/sshd_config`)
- [ ] Regular `sudo apt-get upgrade` or unattended-upgrades running

---

## 13. License & Upstream Attribution

This deployment automation (`deploy_bitcart.py`) is released under the **MIT License**.

**Upstream projects used by this deployment:**

| Project | Repository | License |
|---|---|---|
| Bitcart (fork) | <https://github.com/Decipheredmedia/bitcart> | MIT |
| Bitcart (upstream) | <https://github.com/bitcart/bitcart> | MIT |
| bitcart-docker | <https://github.com/bitcart/bitcart-docker> | MIT |
| bitcart-admin | <https://github.com/bitcart/bitcart-admin> | MIT |
| bitcart-store | <https://github.com/bitcart/bitcart-store> | MIT |
| Nginx | <https://nginx.org> | BSD-2-Clause |
| Certbot / Let's Encrypt | <https://certbot.eff.org> | Apache 2.0 |
| Docker | <https://www.docker.com> | Apache 2.0 |
| PostgreSQL | <https://www.postgresql.org> | PostgreSQL License |
| Redis | <https://redis.io> | BSD-3-Clause |
| Jinja2 | <https://jinja.palletsprojects.com> | BSD-3-Clause |
| Requests | <https://requests.readthedocs.io> | Apache 2.0 |
| PyYAML | <https://pyyaml.org> | MIT |
| cryptography | <https://cryptography.io> | Apache 2.0 / BSD |

Full license texts are available in each project's repository.

---

*This document was generated automatically by `deploy_bitcart.py` and should be
reviewed after every major upgrade.*
