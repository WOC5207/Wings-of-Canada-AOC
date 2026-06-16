# Deploying the AOC on a Synology NAS with your own domain

The app runs as a Docker container (DSM **Container Manager**), and DSM's
built-in **reverse proxy** puts your domain + HTTPS certificate in front of
it. Total setup time is around 20-30 minutes.

Throughout this guide, replace `aoc.example.com` with the hostname you want,
e.g. `aoc.yourdomain.ca` or `ops.yourdomain.ca`.

---

## 1. Copy the project to the NAS

1. In **Package Center**, install **Container Manager** (DSM 7.2+; on older
   DSM it is called **Docker**).
2. In **File Station**, create a folder such as `docker/wings-aoc`.
3. Upload the project into it. You only need:
   `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `requirements.txt`,
   `app.py`, and the `aoc/`, `templates/`, `static/` folders.
   Do **not** upload `.venv/` (and `data/` only if you want to migrate an
   existing local database - see §7).

## 2. Build and start the container

1. Open **Container Manager → Project → Create**.
2. Project name: `wings-aoc`. Path: the folder from step 1. It will detect
   `docker-compose.yml` automatically.
3. Click through and **Build** - the first build downloads the Python image
   and takes a few minutes.
4. The container starts automatically and restarts on NAS reboot
   (`restart: unless-stopped`).

Quick test from a PC on your LAN: `http://NAS-IP:8080` should show the
sign-in page.

> **Don't register yet.** With the shipped settings (`AOC_SECURE_COOKIES=1`)
> sign-in only works over HTTPS, i.e. through the reverse proxy. If you want
> to fully test over plain `http://NAS-IP:8080` first, set
> `AOC_SECURE_COOKIES=0` in `docker-compose.yml`, rebuild, test, then set it
> back to `1`.
>
> If port 8080 is already used on your NAS, change the **left** side of the
> port mapping in `docker-compose.yml` (e.g. `"8081:8080"`) and use that port
> in §5.

## 3. Point your domain at your NAS

At your DNS provider (registrar, Cloudflare, ...):

- **If your home IP rarely changes:** add an **A record** for
  `aoc.example.com` pointing to your public IP.
- **Recommended - survives IP changes:** set up Synology DDNS first
  (**Control Panel → External Access → DDNS**, e.g.
  `yourname.synology.me`), then add a **CNAME record** for
  `aoc.example.com` pointing to that DDNS hostname.

On your **router**, forward TCP ports **80 and 443** to the NAS's LAN IP.
Port 80 is needed for the Let's Encrypt certificate; 443 serves the site.

> **CGNAT warning:** if your ISP puts you behind carrier-grade NAT (public
> IP starts with `100.x`, or port forwarding simply never works), inbound
> connections can't reach you. In that case run a **Cloudflare Tunnel**
> container instead and skip §3-§5; ask me and I'll set that up.

## 4. Get the HTTPS certificate

1. **Control Panel → Security → Certificate → Add → Get a certificate from
   Let's Encrypt.**
2. Domain name: `aoc.example.com`, your email. (This requires port 80
   forwarded, per §3.)
3. DSM renews it automatically.

## 5. Create the reverse proxy rule

**Control Panel → Login Portal → Advanced → Reverse Proxy → Create:**

| | Setting | Value |
|---|---|---|
| Source | Protocol | HTTPS |
| | Hostname | `aoc.example.com` |
| | Port | 443 |
| | Enable HSTS | ✔ (optional, recommended) |
| Destination | Protocol | HTTP |
| | Hostname | `localhost` |
| | Port | `8080` (or what you mapped in §2) |

Then assign the certificate: **Control Panel → Security → Certificate →
Settings** - make sure `aoc.example.com` is mapped to the Let's Encrypt
certificate from §4.

If the **DSM firewall** is enabled (Control Panel → Security → Firewall),
allow TCP 80/443.

## 6. First sign-in

Open `https://aoc.example.com`. **Register yourself first - the first
account created becomes the Administrator.** Then share the link with your
members; everyone who registers joins as a Standard member and can start
flying right away. Anonymous visitors only see the public status page.

## 7. Operations

- **Backups:** everything (database + session key) is in
  `docker/wings-aoc/data/` on the NAS. Copy that folder, or include it in
  Hyper Backup / Snapshot Replication. To migrate the database you started
  on your PC, copy your local `data/aoc.sqlite3` into that folder while the
  container is stopped.
- **Updating the app:** upload the changed files over the old ones, then
  **Container Manager → Project → wings-aoc → Action → Build** (data is
  untouched - it lives in the mounted `data/` folder).
- **Logs:** Container Manager → Container → wings-of-canada-aoc → Logs.
- **DSM updates / reboots:** the container auto-starts; no action needed.

## Environment variables (set in docker-compose.yml)

| Variable | Default | Meaning |
|---|---|---|
| `AOC_BEHIND_PROXY` | `1` | Trust `X-Forwarded-*` headers from DSM's reverse proxy. Set `0` only when exposing the port directly. |
| `AOC_SECURE_COOKIES` | `1` | Session cookies sent over HTTPS only. Set `0` temporarily for plain-HTTP testing. |
| `AOC_DATA_DIR` | `/app/data` (via volume) | Where the database lives. Normally leave alone and move the volume instead. |
| `PORT` | `8080` | Port the app listens on **inside** the container. |
| `TZ` | `America/Toronto` | Container timezone (log timestamps). |
