# AURVEK Nginx Configuration Examples

This directory contains example Nginx configurations for running AURVEK with a reverse proxy setup that provides:

- **Authenticated file serving** (images, videos, PDFs, MP3s)
- **Rate limiting** by content type
- **Auth caching** to reduce backend load
- **CDN-ready static file serving**
- **Custom domain support** for landing pages (e.g., `my-chatbot.com`)

## Architecture Overview

```
                                    +------------------+
                                    |   Cloudflare     |
                                    |  (SSL + Proxy)   |
                                    +--------+---------+
                                             |
                                             v
+------------------+              +----------+----------+
|     Browser      +------------->|       Nginx        |
+------------------+              |   Reverse Proxy    |
                                  +----+----+----+-----+
                                       |    |    |
              +------------------------+    |    +------------------------+
              |                             |                             |
              v                             v                             v
     +--------+--------+         +----------+----------+         +--------+--------+
     |  Static Files   |         |  Auth Endpoints     |         | Custom Domains  |
     |  (CSS, JS, img) |         |  FastAPI :7789      |         | (catch-all _)   |
     +--------+--------+         +----------+----------+         +-----------------+
              |                             |                             |
              v                             v                             v
     +--------+--------+         +----------+----------+         +--------+--------+
     |  data/static/   |         | /auth_image         |         | CustomDomain    |
     |  data/users/    |         | /auth_file          |         | Middleware      |
     +-----------------+         | /get_user_directory |         +-----------------+
                                 +---------------------+
```

## File Descriptions

### `aurvek-main.conf`
Main configuration (default_server) with:
- **Explicit FastAPI routes** (no fallback - protects from scanner attacks)
- JWT token extraction from URL parameters
- Authenticated routes for user files (images, videos, PDFs, MP3s)
- Auth request endpoints proxying to FastAPI
- Rate limiting zones
- Security headers

### `aurvek-custom-domains.conf`
Catch-all server block for custom domain landing pages:
- Handles any domain NOT matched by `aurvek-main.conf`
- Routes all requests to FastAPI's CustomDomainMiddleware
- Middleware validates domain ownership and serves landing page or 404
- SSL handled by Cloudflare Proxy (user configures CNAME)

### `snippets/fastapi-proxy.conf`
Reusable proxy configuration included by route locations. Avoids code duplication.

### `aurvek-cdn.conf`
Static content CDN configuration for serving CSS, JS, and public assets with long cache times.

### `rate_limiting.conf`
Rate limiting zone definitions:
- `general`: 10 req/s for general requests
- `static`: 50 req/s for static assets
- `auth`: 5 req/s for authentication endpoints

## Installation

### 1. Copy configurations

```bash
# Linux/Mac
sudo cp nginx/*.conf /etc/nginx/conf.d/
sudo mkdir -p /etc/nginx/snippets
sudo cp nginx/snippets/*.conf /etc/nginx/snippets/

# Windows (Laragon)
# IMPORTANT: Do NOT use 00-default.conf — Laragon auto-regenerates that file
# from its template on reload/restart, overwriting any custom config.
# Use 01-aurvek.conf (or any name other than 00-default.conf).
copy nginx\aurvek-main.conf C:\laragon\etc\nginx\sites-enabled\01-aurvek.conf
copy nginx\snippets\*.conf C:\laragon\etc\nginx\snippets\
```

### 2. Update placeholders

Replace these placeholders with your actual values:

| Placeholder | Description | Example |
|------------|-------------|---------|
| `{{AURVEK_ROOT}}` | AURVEK installation directory | `/var/www/aurvek` or `D:/AcertingAI/AURVEK` |
| `{{FASTAPI_PORT}}` | FastAPI port | `7789` |
| `{{SSL_CERT}}` | Path to SSL certificate | `/etc/letsencrypt/live/domain/fullchain.pem` |
| `{{SSL_KEY}}` | Path to SSL private key | `/etc/letsencrypt/live/domain/privkey.pem` |
| `{{SNIPPETS_PATH}}` | Path to nginx snippets | `/etc/nginx/snippets` or `C:/laragon/etc/nginx/snippets` |

### 3. SSL Certificates

For the main domain:
```bash
certbot --nginx -d yourdomain.com
```

For custom domains: SSL is handled by Cloudflare Proxy. Users configure a CNAME pointing to your server with Cloudflare's orange cloud enabled.

### 4. Test and reload

```bash
nginx -t
nginx -s reload
```

## Custom Domain Flow

1. User configures custom domain in AURVEK (e.g., `my-chatbot.com`)
2. User sets up CNAME in their DNS pointing to your server
3. User enables Cloudflare Proxy (orange cloud) for SSL
4. User clicks "Verify" - AURVEK checks DNS configuration
5. User pays activation fee (or admin activates for free)
6. Requests to `my-chatbot.com` hit the catch-all server block
7. FastAPI's CustomDomainMiddleware validates and serves landing page

## Environment Variables

Set these in your `.env` file:

```bash
# Primary application domain (skip DB lookup for this domain)
PRIMARY_APP_DOMAIN=yourdomain.com

# Custom domain pricing
CUSTOM_DOMAIN_PRICE=25.00

# Auth configuration
AUTH_IMAGE_ALLOWED_IPS=127.0.0.1
AUTH_IMAGE_ALLOWED_PREFIXES=192.168.1.

# Optional: Cloudflare CDN
CLOUDFLARE_FOR_IMAGES=true
CLOUDFLARE_BASE_URL=https://cdn.yourdomain.com/
CLOUDFLARE_SECRET=your-hmac-secret
```

## Auth Flow

1. User requests: `GET /users/abc/123/hash/files/001/234/img/bot/image.webp?token=JWT`
2. Nginx extracts token via `map $request_uri $extracted_token`
3. Nginx sends internal request to `/auth_image`
4. FastAPI validates JWT token (expiration, username)
5. If valid (200), Nginx serves file with cache headers
6. If invalid (401/403), Nginx returns error

## Rate Limiting Zones

| Zone | Rate | Burst | Purpose |
|------|------|-------|---------|
| `general` | 10 req/s | 20 | General API requests |
| `static` | 50 req/s | 50 | Static files (images, CSS, JS) |
| `auth` | 5 req/s | 15 | Authentication endpoints |

## Security Features

- **Path validation**: Regex patterns prevent directory traversal
- **Scanner blocking**: User-agent based blocking for bots/crawlers
- **Security headers**: X-Frame-Options, X-Content-Type-Options, XSS-Protection
- **Auth caching**: Reduces load on FastAPI for repeated requests
- **Internal locations**: Auth endpoints are internal-only
- **Custom domain validation**: Middleware prevents unauthorized domain access

## Production Recommendations

1. **Use HTTPS everywhere** - Get proper SSL certificates
2. **Enable gzip** - Compress text-based responses
3. **Tune worker_processes** - Match CPU cores
4. **Set up monitoring** - Log analysis and error tracking
5. **Configure fail2ban** - Block repeated auth failures
6. **Use Cloudflare** - For DDoS protection and SSL on custom domains
