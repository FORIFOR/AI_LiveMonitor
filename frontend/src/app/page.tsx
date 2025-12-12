"use client";

import { useEffect, useRef, useState, useCallback } from "react";

type Msg =
  | { type: "stt.partial"; text: string }
  | { type: "stt.final"; text: string }
  | { type: "advice.delta"; text: string }
  | { type: "advice.final"; text: string }
  | { type: "error"; message: string }
  | { type: "started" }
  | { type: "auth.ok" }
  | { type: "tts.start" }
  | { type: "tts.complete"; format: string; sample_rate: number; channels: number; sample_width: number }
  | { type: "tts.error"; message: string };

interface TranscriptLine {
  id: string;
  text: string;
}

let lineIdCounter = 0;
const generateLineId = () => `line-${Date.now()}-${++lineIdCounter}`;

export default function Page() {
  const [connected, setConnected] = useState(false);
  const [running, setRunning] = useState(false);
  const [partial, setPartial] = useState("");
  const [finalLines, setFinalLines] = useState<TranscriptLine[]>([]);
  const [adviceChunks, setAdviceChunks] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [ttsPlaying, setTtsPlaying] = useState(false);
  const [ttsEnabled, setTtsEnabled] = useState(true);

  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const ttsAudioCtxRef = useRef<AudioContext | null>(null);
  const pendingTtsDataRef = useRef<ArrayBuffer | null>(null);

  const wsUrl = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/ws";

  // Play TTS audio from PCM data
  const playTtsAudio = useCallback(async (audioData: ArrayBuffer | null, sampleRate: number) => {
    if (!audioData || !ttsEnabled) {
      setTtsPlaying(false);
      return;
    }

    try {
      // Create or reuse audio context for TTS playback
      if (!ttsAudioCtxRef.current || ttsAudioCtxRef.current.state === "closed") {
        ttsAudioCtxRef.current = new AudioContext({ sampleRate });
      }
      const ctx = ttsAudioCtxRef.current;

      // Resume context if suspended (browser autoplay policy)
      if (ctx.state === "suspended") {
        await ctx.resume();
      }

      // Convert PCM Int16 to Float32 for Web Audio API
      const int16Array = new Int16Array(audioData);
      const float32Array = new Float32Array(int16Array.length);
      for (let i = 0; i < int16Array.length; i++) {
        float32Array[i] = int16Array[i] / 32768.0;
      }

      // Create audio buffer and play
      const audioBuffer = ctx.createBuffer(1, float32Array.length, sampleRate);
      audioBuffer.getChannelData(0).set(float32Array);

      const source = ctx.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(ctx.destination);
      
      source.onended = () => {
        setTtsPlaying(false);
      };
      
      source.start();
      console.log("TTS audio playback started");
    } catch (e) {
      console.error("Failed to play TTS audio:", e);
      setTtsPlaying(false);
    }
  }, [ttsEnabled]);

  const connect = useCallback(async () => {
    setError(null);
    
    const ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";

    ws.onopen = async () => {
      setConnected(true);
      ws.send(JSON.stringify({ type: "start", lang: "ja-JP", sampleRate: 16000 }));
    };

    ws.onmessage = (ev) => {
      // Handle binary data (TTS audio)
      if (ev.data instanceof ArrayBuffer) {
        console.log("Received TTS audio data:", ev.data.byteLength, "bytes");
        pendingTtsDataRef.current = ev.data;
        return;
      }
      
      try {
        const msg = JSON.parse(ev.data) as Msg;
        if (msg.type === "stt.partial") setPartial(msg.text);
        if (msg.type === "stt.final") {
          setPartial("");
          setFinalLines((p) => [...p, { id: generateLineId(), text: msg.text }]);
        }
        if (msg.type === "advice.delta") {
          setAdviceChunks((p) => [...p, msg.text]);
        }
        if (msg.type === "advice.final") {
          setAdviceChunks((p) => [...p, "\n---\n"]);
        }
        if (msg.type === "error") {
          console.error(msg.message);
          setError(msg.message);
        }
        if (msg.type === "started") {
          console.log("Session started");
        }
        if (msg.type === "tts.start") {
          console.log("TTS generation started");
          setTtsPlaying(true);
        }
        if (msg.type === "tts.complete") {
          console.log("TTS complete, playing audio");
          playTtsAudio(pendingTtsDataRef.current, msg.sample_rate);
          pendingTtsDataRef.current = null;
        }
        if (msg.type === "tts.error") {
          console.error("TTS error:", msg.message);
          setTtsPlaying(false);
        }
      } catch (e) {
        console.error("Failed to parse message:", e);
      }
    };

    ws.onerror = (e) => {
      console.error("WebSocket error:", e);
      setError("WebSocket connection error");
    };

    ws.onclose = () => {
      setConnected(false);
      setRunning(false);
    };

    wsRef.current = ws;
  }, [wsUrl]);

  const startMic = async () => {
    try {
      setError(null);
      
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        await connect();
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      mediaStreamRef.current = stream;

      const audioCtx = new AudioContext();
      audioCtxRef.current = audioCtx;

      await audioCtx.audioWorklet.addModule("/pcm-worklet.js");
      const src = audioCtx.createMediaStreamSource(stream);

      const node = new AudioWorkletNode(audioCtx, "pcm-processor");
      workletNodeRef.current = node;

      node.port.onmessage = (ev) => {
        const buf = ev.data as ArrayBuffer;
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(buf);
        }
      };

      src.connect(node);
      node.connect(audioCtx.destination);

      setRunning(true);
    } catch (e) {
      console.error("Failed to start microphone:", e);
      setError(e instanceof Error ? e.message : "Failed to start microphone");
    }
  };

  const stop = async () => {
    try {
      wsRef.current?.send(JSON.stringify({ type: "stop" }));
      wsRef.current?.close();
    } catch (e) {
      console.error("Error stopping:", e);
    }

    workletNodeRef.current?.disconnect();
    await audioCtxRef.current?.close();

    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());

    setRunning(false);
    setConnected(false);
  };

  const clearTranscript = () => {
    setFinalLines([]);
    setPartial("");
    setAdviceChunks([]);
    setError(null);
  };

  const advice = adviceChunks.join("");

  useEffect(() => {
    return () => {
      wsRef.current?.close();
      mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
      audioCtxRef.current?.close();
    };
  }, []);

  return (
    <main className="min-h-screen bg-gradient-to-b from-gray-50 to-gray-100 dark:from-gray-900 dark:to-gray-800 p-6">
      <div className="max-w-4xl mx-auto">
        <header className="mb-8">
          <h1 className="text-3xl font-bold text-gray-900 dark:text-white mb-2">
            Realtime Meeting Assistant
          </h1>
          <p className="text-gray-600 dark:text-gray-400">
            Real-time transcription and AI-powered advice for your meetings
          </p>
        </header>

        <div className="flex flex-wrap gap-4 items-center mb-6">
          <button
            disabled={running}
            onClick={startMic}
            className={`px-6 py-3 rounded-lg font-medium transition-all ${
              running
                ? "bg-gray-300 text-gray-500 cursor-not-allowed"
                : "bg-green-600 hover:bg-green-700 text-white shadow-lg hover:shadow-xl"
            }`}
          >
            Start Recording
          </button>
          <button
            disabled={!running}
            onClick={stop}
            className={`px-6 py-3 rounded-lg font-medium transition-all ${
              !running
                ? "bg-gray-300 text-gray-500 cursor-not-allowed"
                : "bg-red-600 hover:bg-red-700 text-white shadow-lg hover:shadow-xl"
            }`}
          >
            Stop Recording
          </button>
          <button
            onClick={clearTranscript}
            className="px-6 py-3 rounded-lg font-medium bg-gray-200 hover:bg-gray-300 text-gray-700 transition-all"
          >
            Clear
          </button>
          <button
            onClick={() => setTtsEnabled(!ttsEnabled)}
            className={`px-4 py-2 rounded-lg font-medium transition-all flex items-center gap-2 ${
              ttsEnabled
                ? "bg-purple-600 hover:bg-purple-700 text-white"
                : "bg-gray-200 hover:bg-gray-300 text-gray-700"
            }`}
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
            </svg>
            TTS {ttsEnabled ? "ON" : "OFF"}
          </button>
          <div className="flex items-center gap-2 ml-auto">
            <div
              className={`w-3 h-3 rounded-full ${
                connected ? "bg-green-500 animate-pulse" : "bg-gray-400"
              }`}
            />
            <span className="text-sm text-gray-600 dark:text-gray-400">
              {connected ? "Connected" : "Disconnected"}
            </span>
            {running && (
              <span className="ml-2 text-sm text-red-500 animate-pulse">
                Recording...
              </span>
            )}
            {ttsPlaying && (
              <span className="ml-2 text-sm text-purple-500 animate-pulse flex items-center gap-1">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
                </svg>
                Speaking...
              </span>
            )}
          </div>
        </div>

        {error && (
          <div className="mb-6 p-4 bg-red-100 border border-red-300 rounded-lg text-red-700">
            {error}
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <section className="bg-white dark:bg-gray-800 rounded-xl shadow-lg p-6">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4 flex items-center gap-2">
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
              </svg>
              Live Transcript
            </h2>
            <div className="h-80 overflow-y-auto border border-gray-200 dark:border-gray-700 rounded-lg p-4 bg-gray-50 dark:bg-gray-900">
              {finalLines.length === 0 && !partial && (
                <p className="text-gray-400 italic">
                  Start recording to see the transcript...
                </p>
              )}
              {finalLines.map((line) => (
                <div key={line.id} className="mb-2 text-gray-800 dark:text-gray-200">
                  {line.text}
                </div>
              ))}
              {partial && (
                <div className="text-gray-500 dark:text-gray-400 italic">
                  {partial}
                </div>
              )}
            </div>
          </section>

          <section className="bg-white dark:bg-gray-800 rounded-xl shadow-lg p-6">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4 flex items-center gap-2">
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
              </svg>
              AI Advice (Stream)
            </h2>
            <div className="h-80 overflow-y-auto border border-gray-200 dark:border-gray-700 rounded-lg p-4 bg-gray-50 dark:bg-gray-900">
              {!advice && (
                <p className="text-gray-400 italic">
                  AI advice will appear here as you speak...
                </p>
              )}
              <pre className="whitespace-pre-wrap text-gray-800 dark:text-gray-200 font-sans text-sm leading-relaxed">
                {advice}
              </pre>
            </div>
          </section>
        </div>

        <footer className="mt-8 text-center text-sm text-gray-500 dark:text-gray-400">
          <p>
            Powered by Google Cloud Speech-to-Text, Vertex AI Gemini, and Gemini 2.5 TTS
          </p>
        </footer>
      </div>
    </main>
  );
}
