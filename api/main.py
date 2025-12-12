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
TTS_MODEL_NAME = os.getenv("TTS_MODEL", "gemini-2.5-flash-preview-tts")
TTS_VOICE_NAME = os.getenv("TTS_VOICE", "Kore")  # Japanese-friendly voice
TTS_ENABLED = os.getenv("TTS_ENABLED", "true").lower() == "true"

# Lazy initialization of clients
_speech_client = None
_genai_client = None
_tts_client = None


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


def get_tts_client():
    """Get TTS client (uses Google AI API, not Vertex AI for TTS preview)."""
    global _tts_client
    if _tts_client is None:
        from google import genai
        # TTS preview models require Google AI API (not Vertex AI)
        api_key = os.getenv("GOOGLE_AI_API_KEY", "")
        if api_key:
            _tts_client = genai.Client(api_key=api_key)
        else:
            # Fall back to Vertex AI client
            _tts_client = get_genai_client()
    return _tts_client


async def generate_tts_audio(text: str) -> bytes:
    """Generate TTS audio from text using Gemini 2.5 TTS."""
    from google.genai import types as genai_types
    
    if not text.strip():
        return b""
    
    try:
        client = get_tts_client()
        
        response = client.models.generate_content(
            model=TTS_MODEL_NAME,
            contents=text,
            config=genai_types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=genai_types.SpeechConfig(
                    voice_config=genai_types.VoiceConfig(
                        prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                            voice_name=TTS_VOICE_NAME,
                        )
                    )
                ),
            ),
        )
        
        # Extract audio data from response
        if (response.candidates and 
            response.candidates[0].content and 
            response.candidates[0].content.parts):
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data:
                    return part.inline_data.data
        
        return b""
    except Exception as e:
        logger.error(f"TTS generation error: {str(e)}", exc_info=True)
        return b""


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


def extract_first_sentence(text: str) -> str:
    """Extract the first meaningful sentence from advice text for TTS."""
    import re
    
    # Clean up the text
    text = text.strip()
    
    # Skip headers and bullet points, find the first actual sentence
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        # Skip empty lines, headers (starting with #), and bullet points
        if not line or line.startswith('#') or line.startswith('-') or line.startswith('*'):
            continue
        # Skip lines that are just labels like "次に言う1文:"
        if line.endswith(':') or line.endswith('：'):
            continue
        
        # Find the first sentence (ending with 。, !, ?, or .)
        match = re.search(r'^(.+?[。！？!?.])', line)
        if match:
            return match.group(1)
        
        # If no sentence ending found, return the whole line if it's reasonable length
        if len(line) > 10 and len(line) < 200:
            return line
    
    # Fallback: return first 100 chars
    return text[:100] if len(text) > 100 else text


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
    """Run Gemini advice generation loop with TTS."""
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
            
            # Collect full advice text for TTS
            full_advice_text = ""
            
            for chunk in stream:
                if not state.running:
                    break
                delta = chunk.text or ""
                if delta:
                    full_advice_text += delta
                    await ws.send_json({"type": "advice.delta", "text": delta})
            
            await ws.send_json({"type": "advice.final", "text": ""})
            logger.info("Advice generation completed")
            
            # Generate TTS audio if enabled
            if TTS_ENABLED and full_advice_text.strip():
                logger.info(f"Generating TTS for advice ({len(full_advice_text)} chars)")
                
                # Extract the first sentence (the key advice) for TTS
                # This keeps the audio short and focused
                first_sentence = extract_first_sentence(full_advice_text)
                
                if first_sentence:
                    await ws.send_json({"type": "tts.start"})
                    
                    # Run TTS generation in thread pool to avoid blocking
                    audio_data = await asyncio.get_event_loop().run_in_executor(
                        None, 
                        lambda: asyncio.run(generate_tts_audio(first_sentence))
                    )
                    
                    if audio_data:
                        # Send audio as binary WebSocket message
                        await ws.send_bytes(audio_data)
                        await ws.send_json({
                            "type": "tts.complete",
                            "format": "pcm",
                            "sample_rate": 24000,
                            "channels": 1,
                            "sample_width": 2
                        })
                        logger.info(f"TTS audio sent: {len(audio_data)} bytes")
                    else:
                        await ws.send_json({"type": "tts.error", "message": "Failed to generate audio"})
                        
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
