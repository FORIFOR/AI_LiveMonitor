"use client";

import { useEffect, useRef, useState, useCallback } from "react";

type Msg =
  | { type: "stt.partial"; text: string }
  | { type: "stt.final"; text: string }
  | { type: "advice.delta"; text: string }
  | { type: "advice.final"; text: string }
  | { type: "error"; message: string }
  | { type: "started" }
  | { type: "auth.ok" };

export default function Page() {
  const [connected, setConnected] = useState(false);
  const [running, setRunning] = useState(false);
  const [partial, setPartial] = useState("");
  const [finalLines, setFinalLines] = useState<string[]>([]);
  const [advice, setAdvice] = useState("");
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);

  const wsUrl = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/ws";

  const connect = useCallback(async () => {
    setError(null);
    
    const ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";

    ws.onopen = async () => {
      setConnected(true);
      ws.send(JSON.stringify({ type: "start", lang: "ja-JP", sampleRate: 16000 }));
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as Msg;
        if (msg.type === "stt.partial") setPartial(msg.text);
        if (msg.type === "stt.final") {
          setPartial("");
          setFinalLines((p) => [...p, msg.text]);
        }
        if (msg.type === "advice.delta") setAdvice((p) => p + msg.text);
        if (msg.type === "advice.final") {
          setAdvice((p) => p + "\n---\n");
        }
        if (msg.type === "error") {
          console.error(msg.message);
          setError(msg.message);
        }
        if (msg.type === "started") {
          console.log("Session started");
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
    setAdvice("");
    setError(null);
  };

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
              {finalLines.map((line, i) => (
                <div key={i} className="mb-2 text-gray-800 dark:text-gray-200">
                  {line}
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
            Powered by Google Cloud Speech-to-Text and Vertex AI Gemini
          </p>
        </footer>
      </div>
    </main>
  );
}
