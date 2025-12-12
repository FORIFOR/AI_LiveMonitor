import asyncio
import json
import os
import time
import threading
from collections import deque
from typing import Deque, Optional
from queue import Queue, Empty

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

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
        self.recent_text: Deque[str] = deque(maxlen=30)
        self.running = True
        self.uid: Optional[str] = None
        self.last_advice_at = 0.0
        self.stt_thread: Optional[threading.Thread] = None


def stt_request_generator(state: SessionState, lang: str):
    """Generator for streaming STT requests."""
    from google.cloud import speech
    
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
    yield speech.StreamingRecognizeRequest(streaming_config=streaming_config)

    while state.running:
        try:
            chunk = state.audio_q.get(timeout=0.1)
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)
        except Empty:
            continue


def run_stt_sync(state: SessionState, lang: str, result_queue: Queue):
    """Run STT in a separate thread (synchronous gRPC)."""
    try:
        client = get_speech_client()
        requests = stt_request_generator(state, lang)
        responses = client.streaming_recognize(requests=requests)
        
        for resp in responses:
            if not state.running:
                break
            for result in resp.results:
                text = result.alternatives[0].transcript if result.alternatives else ""
                if not text:
                    continue
                if result.is_final:
                    result_queue.put({"type": "stt.final", "text": text})
                else:
                    result_queue.put({"type": "stt.partial", "text": text})
    except Exception as e:
        result_queue.put({"type": "error", "message": f"STT error: {str(e)}"})


async def process_stt_results(ws: WebSocket, state: SessionState, result_queue: Queue):
    """Process STT results from the thread and send to WebSocket."""
    while state.running:
        try:
            result = result_queue.get_nowait()
            if result["type"] == "stt.final":
                state.recent_text.append(result["text"])
            await ws.send_json(result)
        except Empty:
            await asyncio.sleep(0.05)
        except Exception:
            break


def build_advice_prompt(state: SessionState) -> str:
    """Build prompt for Gemini advice generation."""
    recent = "\n".join(list(state.recent_text)[-12:])
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
    
    while state.running:
        await asyncio.sleep(1.0)

        if len(state.recent_text) < 2:
            continue

        now = time.time()
        if now - state.last_advice_at < 3.0:
            continue
        state.last_advice_at = now

        prompt = build_advice_prompt(state)

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
        except Exception as e:
            await ws.send_json({"type": "error", "message": f"Advice error: {str(e)}"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    state = SessionState()
    stt_result_queue: Queue = Queue()
    
    stt_processor_task = None
    advice_task = None

    try:
        while True:
            msg = await ws.receive()

            # Binary = audio chunk
            if "bytes" in msg and msg["bytes"] is not None:
                try:
                    state.audio_q.put_nowait(msg["bytes"])
                except:
                    pass
                continue

            # Text (JSON)
            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                    
                t = data.get("type")

                if t == "auth":
                    # Firebase auth verification (optional for MVP)
                    # For now, just acknowledge
                    state.uid = data.get("idToken", "anonymous")
                    await ws.send_json({"type": "auth.ok"})

                if t == "start":
                    lang = data.get("lang", "ja-JP")
                    
                    # Start STT in a separate thread
                    state.stt_thread = threading.Thread(
                        target=run_stt_sync,
                        args=(state, lang, stt_result_queue),
                        daemon=True
                    )
                    state.stt_thread.start()
                    
                    # Start async tasks for processing results and advice
                    stt_processor_task = asyncio.create_task(
                        process_stt_results(ws, state, stt_result_queue)
                    )
                    advice_task = asyncio.create_task(run_advice(ws, state))
                    
                    await ws.send_json({"type": "started"})

                if t == "stop":
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
        except:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
