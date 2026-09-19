<div align="center">

# 🗣️ Bhasha-Agent

### Real-Time Multilingual Voice AI for Indian Government Welfare Schemes

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Next.js](https://img.shields.io/badge/Next.js-16-000000?style=for-the-badge&logo=nextdotjs&logoColor=white)](https://nextjs.org)
[![Pipecat](https://img.shields.io/badge/Pipecat-1.9-FF6B35?style=for-the-badge)](https://github.com/pipecat-ai/pipecat)
[![Gemini](https://img.shields.io/badge/Gemini_3.5-Flash_Lite-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://ai.google.dev)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-336791?style=for-the-badge&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Qdrant](https://img.shields.io/badge/Qdrant-1.12-DC382D?style=for-the-badge)](https://qdrant.tech)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docs.docker.com/compose/)

*Bridging the language barrier between citizens and welfare — one voice at a time.*

</div>

---

## 📖 What is Bhasha-Agent?

**Bhasha-Agent** is a production-grade, real-time voice assistant that helps Indian citizens discover government welfare schemes they are eligible for. Users simply **speak** — in Hindi, English, Hinglish, Bengali, Marathi, or other regional languages — and the agent responds conversationally, searching a curated corpus of **1,786 schemes** across **all Indian states** and Central Government programs.

The system is **anonymous-first**: no login wall, no OTP screen before value is delivered. A first-time caller talks to the agent immediately. Login is what you do when you want *more* — saved profile, conversation memory, and a larger turn allowance.

---

## 🏗️ System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    FRONTEND  (Next.js 16 + React 19)             │
│                                                                  │
│  ┌────────────┐  ┌───────────┐  ┌─────────┐  ┌──────────────┐   │
│  │ Voice Orb  │  │ Text Chat │  │ Profile │  │ Scheme Cards │   │
│  └─────┬──────┘  └─────┬─────┘  └────┬────┘  └──────┬───────┘   │
│        │               │             │               │           │
│        └───────┬───────┘             │      ┌────────┘           │
│          Pipecat React SDK           │      │ RTVI Messages      │
│          (WebRTC Transport)          │      │                    │
├──────────────────────────────────────┼──────┼────────────────────┤
│                                      │      │                    │
│              BACKEND  (FastAPI + Pipecat 1.9)                    │
│                                                                  │
│  POST /api/offer ──► PipelineWorker (one per caller)             │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │  transport.input()                                        │   │
│  │       ↓                                                   │   │
│  │  STT (Deepgram Nova-3, multilingual)                      │   │
│  │       ↓                                                   │   │
│  │  LanguageTagger  →  auto-detect & switch voice            │   │
│  │       ↓                                                   │   │
│  │  User Aggregator  (VAD: SmartTurnAnalyzerV3)              │   │
│  │       ↓                                                   │   │
│  │  QuotaGate  →  per-turn metering & refusal                │   │
│  │       ↓                                                   │   │
│  │  LLM (Gemini 3.5 Flash-Lite)  →  tool calls              │   │
│  │       ↓                                                   │   │
│  │  TTS (Cartesia Sonic-3)  →  sentence-by-sentence          │   │
│  │       ↓                                                   │   │
│  │  transport.output()  →  CallCloser                        │   │
│  └───────────────────────────────────────────────────────────┘   │
│                           │                                      │
│               ┌───────────┴───────────┐                          │
│               ▼                       ▼                          │
│  ┌──────────────────────┐  ┌──────────────────────┐              │
│  │  PostgreSQL 17       │  │  Qdrant 1.12         │              │
│  │  ───────────────     │  │  ──────────────      │              │
│  │  1,786 schemes       │  │  Dense + Sparse      │              │
│  │  Translations (hi,   │  │  Hybrid search       │              │
│  │    bn, mr)           │  │  RRF fusion           │              │
│  │  User profiles       │  │  Hard state filters   │              │
│  │  Conversations       │  │                      │              │
│  │  OTP / Auth          │  │                      │              │
│  └──────────────────────┘  └──────────────────────┘              │
└──────────────────────────────────────────────────────────────────┘
```

> **One session = one pipeline.** Each caller gets an isolated `PipelineWorker` — no shared conversation history, no cross-caller card leaks. This is the core v2 fix over v1.

---

## ⚡ Key Features

### 🎙️ Real-Time Voice Conversation
- **Sub-2s latency** via WebRTC (Pipecat transport), not WebSocket audio hacks
- **Sentence-by-sentence streaming** — TTS begins on the first sentence while the LLM generates the next
- **Barge-in support** — interrupt the agent mid-sentence and it instantly stops and listens
- **Smart turn-taking** — `SmartTurnAnalyzerV3` with configurable endpointing prevents premature inference

### 🌐 Multilingual (7+ Languages)
- Hindi, English, Hinglish, Bengali, Marathi — full voice + card translations
- Tamil, Telugu, Gujarati, Kannada, Malayalam, Punjabi — voice switching supported
- **Auto-detection** per utterance via Deepgram's `multi` mode
- **Script-aware matching** — Hinglish and Devanagari Hindi are the same *language* but different *scripts*

### 🧠 Intelligent Scheme Discovery
- **1,786 schemes** harvested from myScheme.gov.in's API, stored in Postgres
- **Hybrid retrieval** — dense + sparse vectors in Qdrant, fused with Reciprocal Rank Fusion (RRF)
- **Hard state filters** on every prefetch — a Bihar query **never** returns a Maharashtra scheme
- **Deterministic eligibility** — `check_eligibility` is rules evaluation, never vector similarity
- **NULL = unspecified** — missing data never excludes a scheme; no false "ineligible" verdicts

### 🔐 Privacy & Auth (DPDP Act Compliant)
- **Anonymous-first** — no login wall; OTP login only when users want more turns or saved profiles
- **AES-256-GCM encryption at rest** for all personal data (profiles, transcripts, eligibility reasons)
- **Per-turn quota enforcement** — metered on both voice and text paths, same allowance
- **OTP login** with HMAC-hashed codes, rotating refresh tokens, and family-based theft detection
- **DPDP erasure** — `DELETE /api/me` cascades across all tables; three separate deletion endpoints
- **Enforced retention** — transcripts 90 days, interactions 365 days, swept automatically

### 🎨 Rich UI
- **Voice Orb** with animated states (Ready → Listening → Processing → Speaking)
- **Scheme cards** pushed in real-time via RTVI server messages with multilingual translations
- **Text chat mode** alongside voice for accessibility
- **Profile page** — save eligibility facts (state, age, income, caste) for persistent memory
- **Eligible schemes page** — see all schemes you qualify for at a glance
- **OTP login flow** — phone-based, with quota promotion on first login
- **Dark/light theme** with design tokens and Tailwind CSS 4

### 🧪 Comprehensive Test Suite
- `verify_retrieval.py` — retrieval correctness, state-filter regression gate, NULL invariant
- `two_callers.py` — two live WebRTC callers are isolated (the v1 defect is gone)
- `barge_in.py` — talking over the bot interrupts it
- `verify_quota.py` — identity, metering, spoken refusal (HTTP + voice)
- `verify_auth.py` — OTP, promotion, rotation, replay, erasure
- `verify_profiles.py` — consent, encryption at rest, memory, cascade deletion
- `chat_query_set.py` — 8 fixed queries through the tool loop, comparable across changes

---

## 🛠️ Tech Stack

| Layer | Technology | Role |
|-------|-----------|------|
| **Frontend** | Next.js 16, React 19, TypeScript, Tailwind CSS 4 | Voice UI, profile management, scheme browsing |
| **Frontend SDK** | Pipecat React SDK + Small WebRTC Transport | Real-time voice session management |
| **Backend** | FastAPI, Uvicorn, Pipecat 1.9 | REST API, WebRTC signalling, voice pipeline |
| **LLM** | Gemini 3.5 Flash-Lite (`google-genai` SDK) | Conversational AI with native tool calling |
| **Speech-to-Text** | Deepgram Nova-3 (streaming, multilingual) | Real-time transcription |
| **Text-to-Speech** | Cartesia Sonic-3 | Natural multilingual voice synthesis |
| **Database** | PostgreSQL 17 (canonical store) | Schemes, translations, users, auth, profiles |
| **Vector Search** | Qdrant 1.12 (dense + sparse hybrid) | Semantic scheme retrieval with RRF fusion |
| **Embeddings** | Gemini Embedding API | Dense vectors for Qdrant index |
| **Auth** | JWT (access) + Opaque rotating tokens (refresh) | Stateless verification + theft detection |
| **Encryption** | AES-256-GCM with AAD binding | Personal data encryption at rest |
| **ORM** | SQLAlchemy 2.0 (async) + Alembic | Schema management and migrations |
| **Infrastructure** | Docker Compose | Postgres + Qdrant local setup |
| **Ingestion** | myScheme API harvester → Postgres → Qdrant | Offline batch pipeline (1,786 schemes) |

---

## 📁 Project Structure

```
bhasha-agent/
├── backend/
│   ├── main.py                  # FastAPI app, endpoints, Pipecat worker
│   ├── config.py                # Pydantic settings (all env vars)
│   ├── auth/                    # Identity, OTP, JWT, crypto
│   │   ├── identity.py          # current_user — always succeeds
│   │   ├── routes.py            # /api/auth/* endpoints
│   │   ├── tokens.py            # JWT access + opaque refresh
│   │   ├── anon.py              # HMAC-signed anonymous cookies
│   │   ├── otp.py               # OTP provider (stub / msg91)
│   │   ├── crypto.py            # AES-256-GCM encrypt/decrypt
│   │   └── phone.py             # E.164 normalisation
│   ├── voice/                   # Pipecat voice pipeline
│   │   ├── pipeline.py          # run_session — one per caller
│   │   ├── llm.py               # Gemini chat + tool loop
│   │   ├── prompts.py           # System prompt, few-shots, greeting
│   │   ├── quota_gate.py        # Per-turn quota enforcement
│   │   ├── stt.py               # Deepgram config
│   │   ├── tts.py               # Cartesia config
│   │   └── transport.py         # WebRTC transport setup
│   ├── tools/                   # LLM tool implementations
│   │   ├── registry.py          # Single source of truth for tools
│   │   ├── search.py            # search_schemes
│   │   ├── eligibility.py       # check_eligibility
│   │   ├── details.py           # get_scheme_details
│   │   ├── card.py              # show_scheme_card
│   │   └── end_call.py          # end_call
│   ├── services/                # Business logic
│   │   ├── schemes.py           # Hybrid retrieval + label normalisation
│   │   ├── auth.py              # OTP + login logic
│   │   ├── quota.py             # Atomic turn accounting
│   │   ├── profiles.py          # Encrypted profile CRUD + memory
│   │   ├── conversations.py     # Transcript persistence
│   │   ├── eligibility.py       # Rules-based verdict caching
│   │   └── retention.py         # Automated data sweep
│   ├── models/                  # SQLAlchemy ORM
│   │   ├── scheme.py            # Scheme + SchemeTranslation
│   │   ├── identity.py          # User (anon + verified)
│   │   ├── auth.py              # OTP challenges, refresh tokens
│   │   └── profile.py           # Profiles, conversations, messages
│   ├── search/                  # Vector search
│   │   ├── qdrant_index.py      # Hybrid (dense+sparse) retrieval
│   │   └── sparse.py            # BM25 sparse encoder
│   ├── db/                      # Database engine + base
│   ├── migrations/              # Alembic migrations
│   ├── ingestion/               # Offline data pipeline
│   │   ├── harvest.py           # myScheme API → disk
│   │   ├── load.py              # disk → Postgres
│   │   ├── index_schemes.py     # Postgres → Qdrant
│   │   └── normalize.py         # Field cleaning & translation
│   ├── scripts/                 # Verification & ops scripts
│   │   ├── verify_retrieval.py  # P1 gate
│   │   ├── two_callers.py       # P2 gate — caller isolation
│   │   ├── barge_in.py          # P2 gate — interruption
│   │   ├── verify_quota.py      # P3.1 gate — quota
│   │   ├── verify_auth.py       # P3.2 gate — auth
│   │   ├── verify_profiles.py   # P3.3 gate — profiles
│   │   ├── chat_query_set.py    # Regression queries
│   │   ├── check_config.py      # Config + connectivity check
│   │   └── purge_expired.py     # Retention sweep
│   └── embeddings/              # Embedding utilities
│
├── frontend/
│   ├── app/
│   │   ├── page.tsx             # Main voice + text assistant
│   │   ├── login/page.tsx       # OTP login flow
│   │   ├── profile/page.tsx     # Eligibility facts form
│   │   ├── schemes/page.tsx     # Eligible schemes list
│   │   ├── privacy/page.tsx     # Privacy notice & consent
│   │   ├── providers.tsx        # Session + auth + theme providers
│   │   ├── layout.tsx           # Root layout, header, footer
│   │   ├── globals.css          # Design tokens + Tailwind
│   │   └── components/
│   │       ├── VoiceSession.tsx  # Pipecat React SDK session
│   │       ├── VoiceOrb.tsx      # Animated voice indicator
│   │       ├── TextChat.tsx      # Text chat interface
│   │       ├── SchemeCard.tsx    # Multilingual scheme detail card
│   │       ├── QuotaPill.tsx     # Turn quota display
│   │       ├── SiteHeader.tsx    # Navigation header
│   │       ├── SiteFooter.tsx    # Footer
│   │       ├── LanguageSwitcher.tsx
│   │       └── ThemeToggle.tsx
│   └── lib/
│       ├── api.ts               # Typed API client
│       ├── types.ts             # TypeScript interfaces
│       └── config.ts            # Endpoint configuration
│
├── docker-compose.yml           # Postgres 17 + Qdrant 1.12
└── .env.example                 # All configuration variables
```

---

## 🚀 Getting Started

### Prerequisites

- **Python 3.10+**, **Node.js 18+**, **Docker** (for Postgres & Qdrant)
- API keys from:

| Service | Purpose | Sign Up |
|---------|---------|---------|
| **Google AI (Gemini)** | LLM + Embeddings | [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| **Deepgram** | Speech-to-Text | [console.deepgram.com](https://console.deepgram.com) |
| **Cartesia** | Text-to-Speech | [play.cartesia.ai](https://play.cartesia.ai/keys) |

### 1. Clone & Configure and startup 

```bash
git clone https://github.com/wrathog12/Damco-AI-Project.git
cd Damco-AI-Project

# Configure environment variables
cp .env.example .env              # Root: GEMINI_API_KEY, POSTGRES_*, QDRANT_*
cp backend/.env.example backend/.env   # Backend: DEEPGRAM_API_KEY, CARTESIA_API_KEY
```

### 2. Start Infrastructure

```bash
docker compose up -d              # Postgres 17 + Qdrant 1.12
docker compose ps                 # Both should read "healthy"
```

### 3. Backend Setup

```bash
cd backend
pip install -r requirements.txt

# Apply database migrations
alembic upgrade head

# Verify configuration & connectivity
python scripts/check_config.py

# Start the server
python main.py                    # http://localhost:8000
```

### 4. Frontend Setup

```bash
cd frontend
npm install
cp .env.example .env.local        # NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev                       # http://localhost:3000
```

### 5. Quick Smoke Tests

```bash
# Health check (corpus stats, privacy config)
curl http://localhost:8000/health

# Text chat (no voice needed)
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Show me education schemes in Bihar", "language": "en"}'

# Browser voice test (no frontend needed)
open http://localhost:8000/client
```

---

## 🔌 API Endpoints

### Conversational Surface (never refuses — anonymous-first)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Corpus stats, privacy config, profiles enabled |
| `POST` | `/chat` | Text chat with tool calling (optional `conversation_id`) |
| `GET` | `/api/quota` | Turns remaining (establishes anonymous identity) |
| `POST` | `/api/offer` | WebRTC signalling — start a voice session |
| `GET` | `/client` | Pipecat dev UI for voice testing |

### Account Surface (may refuse with 400/401/403/429)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/auth/request-otp` | Send OTP to phone |
| `POST` | `/api/auth/verify-otp` | Verify OTP, get tokens |
| `POST` | `/api/auth/refresh` | Rotate refresh token |
| `POST` | `/api/auth/logout` | Revoke refresh family |
| `GET` | `/api/me` | Who am I (masked phone) |
| `DELETE` | `/api/me` | DPDP erasure — cascades everything |
| `POST` | `/api/me/consent` | Agree to privacy notice |
| `GET/PUT/DELETE` | `/api/me/profile` | Eligibility facts CRUD |
| `DELETE` | `/api/me/history` | Erase transcripts, keep account |
| `GET` | `/api/me/eligible` | Schemes matching saved profile |

---

## 🔄 Data Pipeline

The ingestion pipeline is an **offline batch job**, not part of the request path:

```
myScheme.gov.in API  ──►  harvest_schemes.py  ──►  JSON on disk
                                                        │
                          load_schemes.py  ◄────────────┘
                                │
                          PostgreSQL 17  (1,786 schemes + translations)
                                │
                          build_index.py
                                │
                          Qdrant 1.12  (dense + sparse vectors)
```

- **1,786 schemes** with native Hindi, Bengali, Marathi translations (5,358 translation rows)
- Rate-limited at 5 req/s against the government API
- Incremental: only embeds schemes whose `content_hash` changed
- The corpus is **final** — no more scraping is wanted

---

## 🧪 Verification Scripts

All scripts live in `backend/scripts/` and serve as the project's test gates:

| Script | What it Proves |
|--------|---------------|
| `check_config.py` | Config resolved correctly, Postgres & Qdrant reachable |
| `verify_retrieval.py` | Hybrid search returns correct schemes, state filter holds, NULL invariant |
| `chat_query_set.py` | 8 fixed queries produce correct tool calls and responses |
| `two_callers.py` | Two simultaneous WebRTC callers are fully isolated |
| `barge_in.py` | Interrupting the bot stops playback immediately |
| `verify_quota.py` | Anonymous quota enforced on HTTP and voice paths |
| `verify_auth.py` | OTP flow, quota promotion, token rotation, replay detection |
| `verify_profiles.py` | Consent, AES-256-GCM encryption, memory, cascade deletion |
| `purge_expired.py` | Retention sweep deletes expired data correctly |

---

## 📜 Migration from v1

The v2 rewrite addressed fundamental architectural flaws:

| Area | v1 (Old) | v2 (Current) |
|------|----------|-------------|
| **Voice Framework** | FastRTC + hand-rolled WebRTC | Pipecat 1.9 (managed pipeline per caller) |
| **LLM** | Groq (Llama-3.3-70B / gpt-oss-120b) | Gemini 3.5 Flash-Lite (`google-genai` native SDK) |
| **TTS Model** | Cartesia Sonic-2 | Cartesia Sonic-3 (Sonic-2 is sunsetted) |
| **Knowledge Base** | In-memory JSON (172 schemes) | Postgres + Qdrant hybrid (1,786 schemes) |
| **Session Isolation** | Global `conversation_history` (shared) | Per-caller `PipelineWorker` (isolated) |
| **Card Delivery** | Separate WebSocket (`/ws/cards`) | RTVI server messages (same WebRTC transport) |
| **Auth** | None | Anonymous-first + OTP login + JWT |
| **Personal Data** | Not stored | AES-256-GCM encrypted profiles + transcripts |
| **Quota** | None | Atomic per-turn metering, spoken refusal |
| **Frontend** | Raw `RTCPeerConnection` | Pipecat React SDK |
| **Data Ingestion** | Crawl4AI web scraper | myScheme API harvester (structured, resumable) |

---

<div align="center">

**Built with ❤️ for India's citizens**

*Making government welfare schemes accessible through the power of voice AI*

</div>
