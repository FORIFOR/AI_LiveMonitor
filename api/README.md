# Realtime Assistant API

A FastAPI backend for real-time meeting assistance with Cloud Speech-to-Text and Vertex AI Gemini.

## Features

- WebSocket endpoint for real-time audio streaming
- Cloud Speech-to-Text integration (gRPC streaming)
- Vertex AI Gemini for AI-powered advice
- Firebase Auth support (optional)

## Setup

1. Install dependencies:
```bash
poetry install
```

2. Configure environment variables in `.env`:
```
GCP_PROJECT=your-gcp-project-id
GCP_LOCATION=asia-northeast1
GEMINI_MODEL=gemini-2.0-flash
```

3. Run the server:
```bash
poetry run uvicorn main:app --host 0.0.0.0 --port 8000
```

## API Endpoints

- `GET /health` - Health check
- `WS /ws` - WebSocket endpoint for real-time audio streaming
