<div align="center">

# 🗣️ Bhasha-Agent

### AI-Powered Multilingual Voice Assistant for Indian Government Welfare Schemes

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Next.js](https://img.shields.io/badge/Next.js-16-000000?style=for-the-badge&logo=nextdotjs&logoColor=white)](https://nextjs.org)
[![WebRTC](https://img.shields.io/badge/WebRTC-FastRTC-333333?style=for-the-badge&logo=webrtc&logoColor=white)](https://fastrtc.org)

*Bridging the language barrier between citizens and welfare — one voice at a time.*

</div>

---

## 📖 Overview

**Bhasha-Agent** is a real-time, multilingual voice AI assistant that helps Indian citizens discover and understand government welfare schemes they are eligible for. Users simply **speak** in their preferred language (Hindi, English, or regional languages), and the agent responds conversationally — searching a curated knowledge base of **1430+ schemes** across **6 states** and **Central Government** programs.

### ✨ Key Highlights

- 🎙️ **Real-Time Voice Conversation** — Sub-2s latency via WebRTC (FastRTC), not WebSocket audio hacks
- 🌐 **Multilingual** — Speaks Hindi, English, and understands regional Indian languages
- 🧠 **Tool-Calling LLM** — Groq-powered Llama-3.3-70B with structured function calling for scheme search, eligibility checks, and detail retrieval
- 🎨 **Rich UI Cards** — Scheme details pop up as interactive cards pushed over WebSocket in real-time
- ⚡ **Sentence-by-Sentence Streaming** — TTS begins on the first sentence while the LLM generates the rest, drastically cutting response time
- 🔇 **Barge-In Support** — Interrupt the agent mid-sentence and it instantly stops and listens

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────┐
│                FRONTEND (Next.js)                   │
│                                                     │
│   ┌────────────┐          ┌──────────────────┐      │
│   │ Voice Orb  │          │  Scheme Card UI  │      │
│   └─────┬──────┘          └────────┬─────────┘      │
│         │ WebRTC Audio             │ WebSocket JSON  │
├─────────┼──────────────────────────┼────────────────┤
│         ▼                          ▼                 │
│              BACKEND (FastAPI + FastRTC)              │
│                                                      │
│   ┌──────────────────────────────────────────────┐   │
│   │           Voice Pipeline (pipeline.py)       │   │
│   │                                              │   │
│   │  1. STT (Deepgram)  →  User's speech to text│   │
│   │  2. LLM (Groq)      →  Reasoning + Tools    │   │
│   │  3. TTS (Cartesia)   →  Response to speech   │   │
│   └──────────────┬───────────────────────────────┘   │
│                  │                                    │
│   ┌──────────────▼───────────────────────────────┐   │
│   │  Tool Registry → Knowledge Base (In-Memory)  │   │
│   │  172 schemes │ 6 states │ 10 categories      │   │
│   └──────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────┘
```

---

## 🛠️ Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | Next.js 16, React 19, TypeScript | Voice UI with animated orb & scheme cards |
| **Backend** | FastAPI, Uvicorn | REST + WebSocket + WebRTC server |
| **Voice Transport** | FastRTC (WebRTC) | Ultra-low latency bidirectional audio |
| **Speech-to-Text** | Deepgram Nova-3 | Real-time multilingual transcription |
| **LLM** | Groq (Llama-3.3-70B) | Conversational AI with tool calling |
| **Text-to-Speech** | Cartesia Sonic-2 | Natural Hindi/English voice synthesis |
| **Knowledge Base** | In-Memory JSON | 172 curated government schemes |

---

## 📋 Prerequisites

Before you begin, make sure you have the following installed:

- **Python 3.10+** — [Download](https://www.python.org/downloads/)
- **Node.js 18+** — [Download](https://nodejs.org/)
- **Git** — [Download](https://git-scm.com/)

You'll also need free API keys from:

| Service | Free Tier | Sign Up |
|---------|-----------|---------|
| **Groq** | 30 requests/min | [console.groq.com](https://console.groq.com/keys) |
| **Deepgram** | 200 min/month | [console.deepgram.com](https://console.deepgram.com) |
| **Cartesia** | Startup free tier | [play.cartesia.ai](https://play.cartesia.ai/keys) |

---

## 📁 Project Structure

```
damco/
├── backend/                    # Python backend server
│   ├── main.py                 # FastAPI app + WebRTC mount
│   ├── config.py               # Pydantic settings (loads .env)
│   ├── .env.example            # ← Copy to .env, add your API keys
│   ├── requirements.txt        # Python dependencies
│   ├── knowledge/              # Knowledge base loader & indexer
│   │   ├── loader.py           # JSON loader
│   │   └── index.py            # In-memory search index
│   ├── voice/                  # Voice pipeline modules
│   │   ├── pipeline.py         # Main voice handler (STT→LLM→TTS)
│   │   ├── llm.py              # Groq LLM with tool calling
│   │   ├── stt.py              # Deepgram speech-to-text
│   │   ├── tts.py              # Cartesia text-to-speech
│   │   └── prompts.py          # System prompts & few-shot examples
│   ├── tools/                  # LLM tool implementations
│   │   ├── registry.py         # Tool registration & dispatch
│   │   ├── search.py           # search_schemes tool
│   │   ├── eligibility.py      # check_eligibility tool
│   │   ├── details.py          # get_scheme_details tool
│   │   ├── card.py             # show_scheme_card tool
│   │   └── end_call.py         # end_call tool
│   └── models/                 # Pydantic data models
│       └── schemas.py
├── frontend/                   # Next.js frontend
│   ├── app/
│   │   ├── page.tsx            # Main voice assistant page
│   │   ├── layout.tsx          # Root layout
│   │   ├── globals.css         # Global styles
│   │   └── components/
│   │       ├── VoiceOrb.tsx    # Animated voice orb component
│   │       └── SchemeCard.tsx  # Scheme detail card component
│   └── package.json
├── scraped_data/               # Knowledge base data
│   ├── knowledge_base_translated.json   # Main KB (172 schemes, multilingual)
│   └── knowledge_base_raw.json          # Raw KB (English only)
└── README.md
```

---

## 🚀 Getting Started

### 1. Clone the Repository

```bash
git clone https://github.com/wrathog12/Damco-AI-Project.git
cd Damco-AI-Project
```

### 2. Backend Setup

```bash
# Navigate to the backend directory
cd backend

# Create a virtual environment
python -m venv venv

# Activate the virtual environment
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure API Keys

```bash
# Copy the example env file
cp .env.example .env

# Open .env and paste your API keys
# (Groq, Deepgram, Cartesia — see Prerequisites above)
```

Your `backend/.env` should look like this:

```env
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEEPGRAM_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
CARTESIA_API_KEY=sk_car_xxxxxxxxxxxxxxxxxxxxxxxx
```

### 4. Frontend Setup

```bash
# From the project root, navigate to frontend
cd frontend

# Install Node.js dependencies
npm install
```

### 5. Run the Application

You need **two terminals** — one for the backend, one for the frontend:

**Terminal 1 — Backend:**

```bash
cd backend
python main.py
```

> The backend server starts at `http://localhost:8000`
> - Health check: `http://localhost:8000/health`
> - Gradio test UI: `http://localhost:8000/gradio`

**Terminal 2 — Frontend:**

```bash
cd frontend
npm run dev
```

> The frontend starts at `http://localhost:3000`

### 6. Start Talking!

1. Open **http://localhost:3000** in your browser
2. Click the **Voice Orb** to start the conversation
3. Speak in **Hindi or English** — e.g., *"Bihar mein students ke liye koi scheme hai?"*
4. The agent will respond with voice + interactive scheme cards

---

## 🎯 Features & Capabilities

### Voice Interaction
- **Natural conversation** — Ask about schemes, follow up, ask for details
- **Barge-in** — Interrupt the agent anytime; it stops and listens immediately
- **Auto-timeout** — Call ends gracefully after 2 minutes of silence

### Scheme Discovery
- **Search by state** — *"Show me schemes in Maharashtra"*
- **Search by category** — *"Education scholarships for SC/ST students"*
- **Eligibility check** — *"Am I eligible for Kanyashree? I'm 17 and studying in class 11"*
- **Scheme details** — *"Tell me more about PM Vishwakarma Yojana"*

### Supported States
Bihar · Madhya Pradesh · Maharashtra · Uttar Pradesh · West Bengal · Central Government

### Supported Categories
Agriculture · Business · Education · Health · Housing · Insurance · Skills · Social Welfare · Sports & Culture · Transport · Utilities · Women & Child

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check + KB statistics |
| `POST` | `/chat` | Text-based chat (for testing without voice) |
| `WS` | `/ws/cards` | WebSocket for real-time scheme card events |
| `POST` | `/rtc/webrtc/offer` | WebRTC signaling (FastRTC auto-mounted) |
| `GET` | `/gradio/` | Gradio test UI for quick voice testing |

---

## 🧪 Testing Without Voice

You can test the backend using the text chat endpoint:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Show me education schemes in Bihar", "language": "en"}'
```

Or visit `http://localhost:8000/gradio` for a quick voice test without the full frontend.

---

## 📄 License

This project was built as part of the **Damco AI Project**.

---

<div align="center">

**Built with ❤️ for India's citizens**

*Making government welfare schemes accessible through the power of voice AI*

</div>
