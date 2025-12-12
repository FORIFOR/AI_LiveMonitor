import asyncio
import json
import os
import time
import threading
import logging
from collections import deque
from itertools import islice
from typing import Deque, Optional
from queue import Queue, Empty, Full

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Realtime Assistant API")

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration from environment
GENAI_PROJECT = os.getenv("GCP_PROJECT", "")
GENAI_LOCATION = os.getenv("GCP_LOCATION", "asia-northeast1")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Lazy initialization of clients
_speech_client = None
_genai_client = None


def get_speech_client():
    global _speech_client
    if _speech_client is None:
        from google.cloud import speech
        _speech_client = speech.SpeechClient()
    return _speech_client


def get_genai_client():
    global _genai_client
    if _genai_client is None:
        from google import genai
        _genai_client = genai.Client(
            vertexai=True,
            project=GENAI_PROJECT,
            location=GENAI_LOCATION
        )
    return _genai_client


class SessionState:
    def __init__(self):
        self.audio_q: Queue = Queue(maxsize=500)
        self.stt_result_q: asyncio.Queue = asyncio.Queue()
        self.recent_text: Deque[str] = deque(maxlen=30)
        self.running = True
        self.uid: Optional[str] = None
        self.last_advice_at = 0.0
        self.stt_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None


def stt_request_generator(state: SessionState):
    """Generator for streaming STT requests (audio chunks only)."""
    from google.cloud import speech

    while state.running:
        try:
            chunk = state.audio_q.get(timeout=0.1)
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)
        except Empty:
            continue


def run_stt_sync(state: SessionState, lang: str):
    """Run STT in a separate thread (synchronous gRPC)."""
    from google.cloud import speech
    
    logger.info(f"Starting STT thread with lang={lang}")
    
    def put_result(result: dict):
        """Thread-safe way to put result into asyncio queue."""
        if state._loop and state.running:
            state._loop.call_soon_threadsafe(
                state.stt_result_q.put_nowait, result
            )
    
    try:
        client = get_speech_client()
        logger.info("Speech client initialized")
        
        # Build config for streaming recognition
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code=lang,
            enable_automatic_punctuation=True,
        )
        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True,
            single_utterance=False,
        )
        
        requests = stt_request_generator(state)
        logger.info("Starting streaming_recognize...")
        responses = client.streaming_recognize(
            config=streaming_config,
            requests=requests,
        )
        
        for resp in responses:
            if not state.running:
                break
            for result in resp.results:
                text = result.alternatives[0].transcript if result.alternatives else ""
                if not text:
                    continue
                if result.is_final:
                    logger.info(f"STT final: {text[:50]}...")
                    put_result({"type": "stt.final", "text": text})
                else:
                    put_result({"type": "stt.partial", "text": text})
        logger.info("STT streaming completed")
    except Exception as e:
        logger.error(f"STT error: {str(e)}", exc_info=True)
        put_result({"type": "error", "message": f"STT error: {str(e)}"})


async def process_stt_results(ws: WebSocket, state: SessionState):
    """Process STT results from asyncio queue and send to WebSocket."""
    while state.running:
        try:
            result = await asyncio.wait_for(
                state.stt_result_q.get(), 
                timeout=0.5
            )
            if result["type"] == "stt.final":
                state.recent_text.append(result["text"])
            await ws.send_json(result)
        except asyncio.TimeoutError:
            continue
        except Exception:
            break


def build_advice_prompt(state: SessionState) -> str:
    """Build prompt for Gemini advice generation."""
    recent_list = list(state.recent_text)
    recent = "\n".join(recent_list[-12:] if len(recent_list) > 12 else recent_list)
    return f"""あなたは会議中のリアルタイムアシスタントです。
以下の会話ログに対して、ユーザーが次に言うべき「1文」を最優先で出してください。
次に、根拠(箇条書き2〜3)、次に聞く質問(1〜2)を出してください。
憶測は憶測と明記。会話に無い固有名詞は作らない。

[会話ログ]
{recent}
"""


async def run_advice(ws: WebSocket, state: SessionState):
    """Run Gemini advice generation loop."""
    from google.genai import types as genai_types
    
    logger.info("Advice generation loop started")
    
    while state.running:
        await asyncio.sleep(1.0)

        if len(state.recent_text) < 2:
            continue

        now = time.time()
        if now - state.last_advice_at < 3.0:
            continue
        state.last_advice_at = now

        prompt = build_advice_prompt(state)
        logger.info(f"Generating advice with {len(state.recent_text)} transcript lines")

        try:
            client = get_genai_client()
            stream = client.models.generate_content_stream(
                model=MODEL_NAME,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.4,
                    max_output_tokens=300,
                ),
            )
            for chunk in stream:
                if not state.running:
                    break
                delta = chunk.text or ""
                if delta:
                    await ws.send_json({"type": "advice.delta", "text": delta})
            await ws.send_json({"type": "advice.final", "text": ""})
            logger.info("Advice generation completed")
        except Exception as e:
            logger.error(f"Advice error: {str(e)}", exc_info=True)
            await ws.send_json({"type": "error", "message": f"Advice error: {str(e)}"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    logger.info("WebSocket connection accepted")
    state = SessionState()
    state._loop = asyncio.get_running_loop()
    
    stt_processor_task = None
    advice_task = None
    audio_chunk_count = 0

    try:
        while True:
            msg = await ws.receive()

            # Binary = audio chunk
            if "bytes" in msg and msg["bytes"] is not None:
                audio_chunk_count += 1
                if audio_chunk_count == 1:
                    logger.info(f"First audio chunk received, size={len(msg['bytes'])} bytes")
                elif audio_chunk_count % 100 == 0:
                    logger.info(f"Audio chunks received: {audio_chunk_count}, queue size: {state.audio_q.qsize()}")
                try:
                    state.audio_q.put_nowait(msg["bytes"])
                except Full:
                    pass  # Queue is full, drop the audio chunk
                continue

            # Text (JSON)
            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse JSON: {msg['text'][:100]}")
                    continue
                    
                t = data.get("type")
                logger.info(f"Received message type: {t}")

                if t == "auth":
                    # Firebase auth verification (optional for MVP)
                    # For now, just acknowledge
                    state.uid = data.get("idToken", "anonymous")
                    await ws.send_json({"type": "auth.ok"})

                if t == "start":
                    lang = data.get("lang", "ja-JP")
                    logger.info(f"Starting session with lang={lang}")
                    
                    # Start STT in a separate thread
                    state.stt_thread = threading.Thread(
                        target=run_stt_sync,
                        args=(state, lang),
                        daemon=True
                    )
                    state.stt_thread.start()
                    
                    # Start async tasks for processing results and advice
                    stt_processor_task = asyncio.create_task(
                        process_stt_results(ws, state)
                    )
                    advice_task = asyncio.create_task(run_advice(ws, state))
                    
                    await ws.send_json({"type": "started"})
                    logger.info("Session started, STT thread and advice task running")

                if t == "stop":
                    logger.info("Stop requested")
                    state.running = False
                    state.audio_q.put(None)  # Signal to stop STT
                    break

    except WebSocketDisconnect:
        state.running = False
        state.audio_q.put(None)
    except Exception as e:
        await ws.send_json({"type": "error", "message": str(e)})
    finally:
        state.running = False
        state.audio_q.put(None)
        
        if stt_processor_task:
            stt_processor_task.cancel()
        if advice_task:
            advice_task.cancel()
        
        try:
            await ws.close()
        except (RuntimeError, ConnectionError):
            pass  # Connection already closed


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
