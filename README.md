# AURVEK

**A production-grade SaaS platform for AI-powered products: multi-provider chat, prompt marketplace, real-time voice, video generation, and white-label infrastructure.**

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<p align="center">
  <img src="docs/screenshots/chat-default-theme.png" alt="AURVEK - AI Chat Interface" width="800">
</p>

<table>
<tr>
<td align="center"><img src="docs/screenshots/chat-neko-glass-theme.png" width="270"><br><sub>Neko Glass</sub></td>
<td align="center"><img src="docs/screenshots/chat-frutiger-aero-theme.png" width="270"><br><sub>Frutiger Aero</sub></td>
<td align="center"><img src="docs/screenshots/chat-writer-theme.png" width="270"><br><sub>Writer</sub></td>
</tr>
</table>

<p align="center">
  <sub>13 built-in themes, all WCAG AA compliant — <a href="docs/screenshots/GALLERY.md#13-ui-themes">see all themes</a> · <a href="docs/screenshots/GALLERY.md">full screenshot gallery</a></sub>
</p>

---

## What is AURVEK?

AURVEK is an open-source AI platform that combines **multi-provider chat**, a **prompt marketplace**, **real-time voice calls**, **WhatsApp integration**, **AI video/image generation**, and a **complete white-label infrastructure** into a single deployable application.

Think of it as **Poe + Shopify for AI prompts + white-label** -- all in one codebase.

### Four Business Modes in One Platform

| Mode | Description |
|------|-------------|
| **Poe Mode** | Public AI aggregator -- users browse and chat with community prompts |
| **Enterprise Mode** | A manager pays for their team's usage with budget controls and auto-refill |
| **Curator Mode** | Full white-label deployment with custom branding, domains, and commission splits |
| **Freelancer Mode** | Creators sell individual AI prompts as paid products to captive users |

Each mode has its own billing logic, user relationships, and access controls -- all running on the same instance.

> **Note**: These modes are architectural configurations, not user-facing labels. The platform adapts automatically based on how each creator configures their prompts and billing.

<p align="center">
  <img src="docs/screenshots/explore-marketplace.png" alt="AURVEK Prompt Marketplace" width="700">
  <br><em>Prompt Marketplace -- browse AI personalities with custom avatars and descriptions</em>
</p>

---

## Core Features

### AI Providers & Models

AURVEK integrates 6 providers through a unified streaming interface with native tool calling:

| Provider | Models | Capabilities |
|----------|--------|-------------|
| **OpenAI** | GPT-4o, GPT-5, O1 | Chat, images (DALL-E 3, gpt-image), TTS, web search |
| **Anthropic** | Claude 3.5/4 Sonnet, Haiku | Chat, thinking tokens, prompt caching, web search |
| **Google** | Gemini 2.5 | Chat, images, video (VEO-3), web search |
| **xAI** | Grok-2, Grok-4 | Chat, web search |
| **OpenRouter** | 300+ models | Multi-provider gateway |
| **ElevenLabs** | ConvAI, TTS, Scribe | Real-time voice, speech synthesis & recognition |

**Native Tool Calling** across all providers: 8+ built-in tools including web search, image generation, video generation, maps, consciousness analysis, and self-protection mechanisms.

---

### Multi-AI Comparison

Send the same message to **2-4 AI models simultaneously** and compare their responses side by side:

- **Parallel execution engine**: All models run concurrently via `asyncio` with SSE multiplexing -- no waiting for one to finish before the next starts
- **Carousel UI**: Swipe or keyboard-navigate between responses, each labeled with the model name
- **Independent billing**: Each model's token usage is tracked and billed separately
- **Per-message model tracking**: Every message records which LLM generated it (`llm_id` in database)
- **Context-aware**: Respects prompt-level LLM restrictions (allowed models, forced model)

---

### Web Search & Citations

AI models can search the web in real time and cite their sources:

- **Dual engine**: Native provider search (OpenAI, Claude, Gemini, xAI) or Perplexity -- selectable per user in settings
- **Native search**: Each provider uses its own search tool (OpenAI `web_search`, Claude `web_search`, Gemini `google_search`, xAI `x_search`) for grounded, up-to-date responses
- **Perplexity second pass**: Search results are fed back to the original AI for a personality-aware response instead of raw Perplexity output
- **Inline citations**: Collapsible "Sources" block with favicons rendered below bot messages, persisted in database
- **Streaming indicator**: "Searching the web..." pulse animation while the search runs

---

### AI Supervision: Dual Watchdog System

A unique two-layer AI behavior monitoring system that ensures prompts stay on-character and conversations stay safe:

**Pre-Watchdog (Synchronous)**
- Intercepts every user message *before* it reaches the AI
- LLM-based evaluation blocks prompt injection, jailbreaks, and off-topic manipulation
- Runs inline -- malicious inputs never reach the AI model

**Post-Watchdog (Asynchronous)**
- Monitors AI responses *after* generation via Dramatiq background tasks
- Detects personality drift, role contradictions, and policy violations
- **4-level hint escalation**: Steering &#8594; Directive &#8594; Override &#8594; Takeover
- **Takeover mode**: After N ignored hints, the watchdog replaces the AI entirely
- **Force-lock**: Permanently locks conversations flagged with "alert" severity
- **Role Coherence Module**: Detects when the AI contradicts its configured personality

Configurable per prompt with presets for interview, coaching, education, and custom modes. Full admin dashboard with event timeline, filters, and audit trail.

<p align="center">
  <img src="docs/screenshots/watchdog-locked-conversation.png" alt="Watchdog - Locked Conversation" width="700">
  <br><em>Watchdog in action: conversation locked after detecting policy violations with hint escalation</em>
</p>

---

### Security

AURVEK implements defense-in-depth security with multiple independent layers:

**Security Middleware** (1,600+ lines)
- 236+ instant-block patterns (scanners, shells, exploit paths, AWS metadata probes, IoT exploits)
- Progressive blocking: accumulated 404s trigger temporary IP bans
- Dual backend: Redis for multi-worker deployments, in-memory fallback for single-process
- **Automatic Cloudflare WAF escalation**: creates firewall rules for persistent attackers
- Opportunistic cleanup of expired Cloudflare rules
- Smart exemptions for user landing pages
- Admin dashboard with stats, events, and blocked IPs

**Security Guard (LLM-Based Content Moderation)**
- Pre-execution content analysis using a dedicated LLM evaluator
- 4 threat categories: system info extraction, prompt injection, malicious code, jailbreak attempts
- Supports Claude, GPT, Gemini, and xAI as evaluators
- Temperature 0.0 for deterministic classification

**Application Security**
- JWT tokens in HTTPOnly cookies with Redis-backed revocation
- bcrypt + pepper password hashing
- Parameterized SQL queries (no string interpolation)
- Path traversal prevention with `Path.is_relative_to()`
- SSRF protection on all external media URLs
- Decompression bomb protection on file uploads
- Multi-strategy rate limiting across 8+ endpoints (per-IP, per-identifier, per-failure)
- BYOK encryption: AES-128 Fernet with PBKDF2 (100K iterations)
- Admin audit log: every admin action logged with who, what, when, and from where

---

### Prompt Marketplace

Every prompt in AURVEK is a product with its own identity:

- **Custom Landing Pages**: Full HTML/CSS/JS per prompt with static assets (images, audio, styles)
- **AI Landing Page Wizard**: Generate and iterate on landing pages using Claude Code CLI (4 visual styles, multi-language, brand colors)
- **Custom Domains**: Users can attach their own domain to a prompt (CNAME verification, 4-state lifecycle: unverified &#8594; pending &#8594; verified &#8594; active)
- **Categories**: 10 categories with age-restriction support and per-user access controls
- **Landing Page Analytics**: Anonymized visitor tracking (hashed IPs), referrer analysis, return visitor detection, conversion funnels (visit &#8594; signup)
- **Discount Codes**: Configurable coupons with usage limits, expiration dates, and percentage discounts
- **LLM Restrictions**: Creators control which AI models work with their prompt -- any model, a restricted list, a single forced model, or forced with the model name hidden in the UI

---

### Creator Storefronts

Creators get their own public storefront page within AURVEK:

- **Creator Profiles**: Public bio, avatar, social links, and verified status (`/store/{slug}`)
- **Contextual Branding**: Navbar and theme adapt when viewing a creator's storefront -- creator's logo and brand appear naturally, reverting to AURVEK when navigating away
- **Individual Prompt Purchase**: Buy single prompts via Stripe Checkout or claim free prompts directly from the Explore page
- **Explore Ranking**: Algorithmic scoring based on engagement signals (access count, purchases, chatters, favorites) with configurable weights and admin dashboard. Prompts with landing pages receive a ranking boost
- **Landing Preview**: Fullscreen iframe overlay in `/explore` with keyboard navigation between landings -- browse creator pitches without leaving the page

---

### Monetization & Payouts

**Stripe Connect Integration**
- Creator bank account onboarding via Stripe Express
- Real transfers to creators ($50 minimum payout threshold)
- Per-prompt earnings dashboard with stats
- Webhooks for `account.updated` and `transfer.failed`
- Commission model: 70% creator / 30% platform (configurable)

**Enterprise Billing**
- Manager-pays-for-team model with budget caps
- Auto-refill when balance drops below threshold
- Per-user usage tracking and cost attribution

---

### Multimedia Generation

**Image Generation** -- 5 engines:
- DALL-E 3 and OpenAI gpt-image
- Ideogram V2
- Google Gemini
- POE API (access to multiple image models)
- Up to 16 reference images per request, automatic aspect ratio detection

**Video Generation** -- Google VEO:
- 3 models: VEO 3.1-fast, VEO 3.1, VEO 2.0
- Negative prompts, configurable aspect ratios
- Async generation with polling and exponential backoff

**Text-to-Speech** -- Dual engine:
- ElevenLabs + OpenAI TTS
- Intelligent text chunking for long content
- SHA256-based audio caching
- WebSocket streaming for real-time playback
- Multi-key load balancer with health scoring

**Speech-to-Text** -- Dual engine with auto-fallback:
- Deepgram Nova-2 (primary)
- ElevenLabs Scribe v2 (fallback)
- Automatic engine switching on failure

**Export**:
- **PDF**: ReportLab with full Markdown rendering (tables, code blocks, emojis, nested lists), embedded images, document metadata
- **MP3**: TTS-powered audio export of entire conversations

<p align="center">
  <img src="docs/screenshots/multimedia-image-video-generation.png" alt="AI Image and Video Generation" width="700">
  <br><em>AI-generated images and videos created directly in chat conversation</em>
</p>

---

### Real-Time Voice Calls

Live voice conversations with AI through ElevenLabs ConvAI:

- Auto-cached SDK proxy (resilient to CDN outages)
- Multi-key load balancer with performance scoring
- Watchdog integration (voice calls are supervised too)
- Automatic transcript synchronization to chat history
- Audio download for completed calls
- Admin tooling: per-prompt agent/voice configuration

---

### WhatsApp Integration

AI chat via WhatsApp through Twilio:

- Twilio webhook signature verification
- SSRF prevention on incoming media URLs
- Text and voice mode switching per user
- Automatic voice note transcription (dual-engine with fallback)
- Configurable response format (text or audio)
- Async httpx-based Twilio client (lightweight, no SDK dependency)

---

### White-Label & Branding

Full white-label infrastructure for Curator Mode:

- Custom logo and brand colors
- Complete AURVEK branding removal
- Forced theme for sub-users
- Branded transactional emails
- Custom domain attachment ($25 one-time)
- DNS verification with automated CNAME checking

---

### User Features

- **Alter Egos**: Multiple personas per user with custom avatar, name, and description -- injected into system prompts automatically
- **BYOK (Bring Your Own Keys)**: Users supply their own API keys with 3 modes (own only, system only, prefer own). Keys encrypted with AES-128 Fernet (PBKDF2, 100K iterations) and masked in UI
- **Chat Folders**: Nested folder organization with drag-and-drop, color coding, and custom sort order
- **Message Search**: FTS5 full-text search across all conversations with WhatsApp-style sidebar UI, result highlighting, and per-conversation scoping
- **Unified Settings**: Single `/settings` page combining profile, usage & billing, and API key management
- **13 UI Themes**: All WCAG AA compliant for contrast. Includes Default, Light, Coder (VS Code), Terminal, Writer, Neko Glass, Frutiger Aero, Memphis, E-ink, Katari Shoji, Halloween, Christmas, Valentine's. Template included for creating new themes
- **Usage Dashboard**: Personal usage stats at `/my-usage` with daily aggregation charts

<details>
<summary><strong>Theme Gallery -- click to expand</strong></summary>
<br>
<table>
<tr>
<td align="center"><strong>Default</strong><br><img src="docs/screenshots/chat-default-theme-expanded.png" width="400"></td>
<td align="center"><strong>Terminal</strong><br><img src="docs/screenshots/chat-terminal-theme.png" width="400"></td>
</tr>
<tr>
<td align="center"><strong>Neko Glass</strong><br><img src="docs/screenshots/chat-neko-glass-theme.png" width="400"></td>
<td align="center"><strong>Memphis</strong><br><img src="docs/screenshots/chat-memphis-theme.png" width="400"></td>
</tr>
<tr>
<td align="center"><strong>Writer</strong><br><img src="docs/screenshots/chat-writer-theme.png" width="400"></td>
<td align="center"><strong>Coder (VS Code)</strong><br><img src="docs/screenshots/chat-coder-theme.png" width="400"></td>
</tr>
<tr>
<td align="center"><strong>Light</strong><br><img src="docs/screenshots/chat-light-theme.png" width="400"></td>
<td align="center"><strong>Halloween</strong><br><img src="docs/screenshots/chat-halloween-theme.png" width="400"></td>
</tr>
<tr>
<td align="center"><strong>Christmas</strong><br><img src="docs/screenshots/chat-christmas-theme.png" width="400"></td>
<td align="center"><strong>Valentine's Day</strong><br><img src="docs/screenshots/chat-valentines-theme.png" width="400"></td>
</tr>
</table>

<p align="center"><em>All 13 themes are WCAG AA compliant. New themes can be created from the included template.</em></p>
</details>

---

### Authentication

Multi-modal authentication system:

| Method | Description |
|--------|-------------|
| **Magic Links** | Passwordless email login |
| **Passwords** | bcrypt + pepper hashing |
| **Google OAuth** | One-click Google account login |
| **Combined** | Magic link + password (configurable per user) |

- JWT tokens stored in HTTPOnly cookies
- Redis-backed token revocation
- Per-endpoint rate limiting with 12 strategies across 8+ endpoints
- Admin-controlled password change permissions

---

### Admin Tooling

- **User Management**: Create, edit, delete users with role assignment (admin/manager/user)
- **Prompt Management**: Approve, categorize, and configure marketplace prompts
- **Watchdog Dashboard**: Event timeline, hint history, force-locks, per-conversation audit
- **Security Dashboard**: Blocked IPs, attack patterns, Cloudflare WAF rule management
- **Usage Dashboard**: Platform-wide stats with filters, top users, CSV export
- **Audit Log**: Complete record of every admin action (who, what, when, IP)
- **LLM Configuration**: Per-model pricing, token limits, and feature flags
- **ElevenLabs Agent Config**: Map voices and agents to specific prompts
- **Ranking Dashboard**: Configure explore ranking weights and trigger recalculation at `/admin/ranking`
- **WhatsApp Admin**: Twilio configuration and phone number management at `/admin/whatsapp`

<details>
<summary><strong>Admin Screenshots -- click to expand</strong></summary>
<br>
<table>
<tr>
<td align="center"><strong>Admin Dashboard</strong><br><img src="docs/screenshots/admin-dashboard.png" width="400"></td>
<td align="center"><strong>Platform Usage Analytics</strong><br><img src="docs/screenshots/platform-usage-analytics.png" width="400"></td>
</tr>
<tr>
<td align="center"><strong>LLM Management (344+ models)</strong><br><img src="docs/screenshots/llm-management-openrouter.png" width="400"></td>
<td align="center"><strong>Pricing Configuration</strong><br><img src="docs/screenshots/pricing-configuration.png" width="400"></td>
</tr>
</table>
</details>

---

## Architecture

AURVEK uses a **FastAPI + Flask hybrid** architecture:

```
                    +-------------------------------------+
                    |           Cloudflare CDN            |
                    +-----------------+-------------------+
                                      |
                    +-----------------v-------------------+
                    |        Nginx Reverse Proxy          |
                    |   (auth_request for file access)    |
                    +-----------------+-------------------+
                                      |
          +---------------------------+---------------------------+
          |                           |                           |
+---------v---------+   +-------------v-------------+   +---------v---------+
|    FastAPI         |   |     WebSocket Server      |   |   Background      |
|  (REST + SSE)      |   |  (Real-time chat + TTS)   |   |   (Dramatiq)      |
+---------+---------+   +-------------+-------------+   +---------+---------+
          |                           |                           |
          +---------------------------+---------------------------+
                                      |
                    +-----------------v-------------------+
                    |       SQLite (WAL mode, pooled)     |
                    +-------------------------------------+
```

### Tech Stack

| Layer | Technology |
|-------|------------|
| **Backend** | FastAPI + Flask (Jinja2 templating) |
| **Database** | SQLite with aiosqlite, WAL mode, connection pooling |
| **Real-time** | WebSockets (chat, TTS streaming), Server-Sent Events |
| **Caching** | Redis (primary) with in-memory fallback |
| **Background Jobs** | Dramatiq + Redis (watchdog, video polling, async tasks) |
| **File Storage** | 3-tier SHA1 hash directories, JWT-authenticated image access |
| **CDN** | Cloudflare with Redis/Cloudflare/in-memory token modes |
| **Payments** | Stripe Connect (marketplace payouts, subscriptions) |
| **Messaging** | Twilio (WhatsApp), SendGrid/SMTP (email) |

### Project Structure

```
AURVEK/
├── app.py                  # Main application (FastAPI + Flask routing)
├── auth.py                 # Authentication (JWT, magic links, OAuth, rate limiting)
├── database.py             # Async SQLite with connection pooling
├── ai_calls.py             # Unified AI provider interface + streaming
├── common.py               # Configuration, constants, cost calculations
├── prompts.py              # Marketplace logic & file management
├── clients.py              # Shared API clients (Stripe, Twilio, Deepgram)
├── ranking.py              # Explore ranking algorithm & scheduling
├── message_search.py       # FTS5 full-text message search
├── storefront_service.py   # Creator profiles & storefront logic
├── twilio_async.py         # Async Twilio client (httpx, no SDK)
├── welcome_service.py      # Welcome page resolution & serving
├── security_guard_llm.py   # LLM-based content moderation
├── elevenlabs_service.py   # Voice calls, transcripts, agent management
├── whatsapp.py             # Twilio WhatsApp integration
├── middleware/
│   ├── security.py         # Request filtering, IP blocking, WAF escalation
│   └── custom_domains.py   # Custom domain resolution & caching
├── routes/
│   └── custom_domains.py   # Custom domain API endpoints
├── tools/
│   ├── watchdog.py         # Pre/post watchdog + hint escalation
│   ├── llm_caller.py       # Non-streaming LLM caller for background tasks
│   ├── perplexity.py       # Web search integration
│   ├── generate_images.py  # AI image generation (DALL-E, Ideogram, Gemini)
│   ├── generate_videos.py  # AI video generation (Google VEO)
│   ├── download_pdf.py     # Conversation export to PDF
│   ├── download_mp3.py     # TTS-powered audio export
│   └── tts.py              # Text-to-speech with multi-key load balancer
├── data/
│   ├── static/             # CSS themes, JS modules, media
│   │   ├── css/themes/     # 13 unified themes (WCAG AA)
│   │   ├── css/chat/       # Chat-specific theme variants
│   │   └── js/chat/        # Frontend modules (chat, folders, voice, search, audio)
│   ├── seed/               # Seed data (bot images, landing pages, prompts)
│   └── users/              # Hash-based user directories (prompts, profiles)
├── templates/              # Jinja2 templates (chat, admin, marketplace)
├── tests/                  # Test suite (security, watchdog)
└── nginx/                  # Production Nginx configs
```

---

## Quick Start

### Prerequisites

- Python 3.10+
- Redis (recommended; optional with in-memory fallback)
- Node.js 18+ (only for AI Landing Page Wizard)

### Installation

```bash
# Clone the repository
git clone https://github.com/jordicor/SparkAI.git
cd SparkAI

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Copy environment template and configure
cp .env.example .env
# Edit .env -- at minimum: OPENAI_KEY, PEPPER, APP_SECRET_KEY

# Initialize database
python init_db.py

# Run
python app.py
```

Available at `http://localhost:7789`.

### Environment Variables

```bash
# Core (required)
APP_SECRET_KEY=your-secret-key
PEPPER=your-pepper-for-hashing

# AI Providers (at least one required)
OPENAI_KEY=sk-...
ANTHROPIC_KEY=sk-ant-...
GOOGLE_AI_KEY=AI...
XAI_API_KEY=xai-...
OPENROUTER_API_KEY=sk-or-...

# Voice & Speech (optional)
ELEVENLABS_KEY=...
DEEPGRAM_KEY=...

# WhatsApp (optional)
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...

# Payments (optional)
STRIPE_SECRET_KEY=sk_...
STRIPE_CONNECT_CLIENT_ID=ca_...

# Security (optional, enhances protection)
CLOUDFLARE_API_TOKEN=...
CLOUDFLARE_ZONE_ID=...
```

See `.env.example` for all options.

### Production Deployment

For production with Nginx (authenticated file serving, reverse proxy):

```bash
# See nginx/ directory for configuration examples
# Windows users: Laragon provides Nginx + Redis with minimal setup
```

---

## Configuration

### Redis

When available, Redis powers:
- Rate limiting (multi-strategy, per-endpoint)
- JWT token revocation
- Image access token caching
- Background task queues (Dramatiq)
- Security middleware IP tracking (multi-worker safe)

Without Redis, AURVEK falls back to in-memory stores (single-process only).

### Themes

All 13 themes (including Neko Glass with glassmorphism effects and custom wallpaper support) use standardized CSS variables and are **WCAG AA compliant**. To create a new theme, copy `data/static/css/themes/_template.css` and define your color palette.

### Watchdog

Configure per prompt via the admin panel. Presets available for common use cases (interview, coaching, education). Escalation thresholds, takeover behavior, and force-lock severity are all adjustable.

---

## Background & Philosophy

AURVEK started as **TestMyPrompt** (2024) -- a tool for testing AI prompt security, born from building a [Santa Claus AI chatbot](https://github.com/jordicor/santa-claus-is-calling) that needed to never break character. The need for forensic logging of where prompts failed evolved into a full platform.

The codebase has areas of intentional over-engineering -- exploring patterns, testing architectural limits, and learning by building. This is a deliberate choice: some systems (like the multi-layer security stack or the watchdog escalation chain) are more complex than strictly necessary because building them was part of the learning process.

It's not a tutorial project. It's a production system that I use daily -- that's how bugs, design flaws, and usability issues get caught and fixed. Development is ongoing: prompt discovery for the marketplace, deeper mobile design passes, and other improvements are actively in progress and will be published as they land.

---

## Contributing

Contributions are welcome. Areas where help is most needed:

- **Testing**: Basic test suite exists (security, watchdog) -- needs broader coverage
- **Type Hints**: Partial coverage, needs mypy integration
- **API Documentation**: Beyond auto-generated docs
- **New Themes**: Following the `_template.css` pattern

Please open an issue before submitting large changes.

---

## Credits

**Developed by:** [Jordi Cor](https://github.com/jordicor)

**Early contributor:** My nephew, who helped during the TestMyPrompt era with testing, code reviews, and backend development.

**Built with AI assistance:** Claude, GPT, and Codex -- the "vibe coding" approach where AI writes for speed and humans review for quality.

---

## License

MIT License -- see [LICENSE](LICENSE) for details.

---

## Related

- [Santa Claus AI](https://github.com/jordicor/santa-claus-is-calling) -- The project that started it all
