class PcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = [];
    this._bufferLen = 0;
    this._readIndex = 0;
    this._readOffset = 0;
    this.targetSampleRate = 16000;
  }

  downsample(float32, inRate, outRate) {
    if (inRate === outRate) return float32;
    const ratio = inRate / outRate;
    const outLen = Math.floor(float32.length / ratio);
    const out = new Float32Array(outLen);
    let o = 0;
    for (let i = 0; i < outLen; i++) {
      const idx = Math.floor(o);
      out[i] = float32[idx] ?? 0;
      o += ratio;
    }
    return out;
  }

  floatTo16BitPCM(f32) {
    const i16 = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
      let s = Math.max(-1, Math.min(1, f32[i]));
      i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return i16;
  }

  compactBuffer() {
    if (this._readIndex > 10) {
      this._buffer = this._buffer.slice(this._readIndex);
      this._readIndex = 0;
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;

    const channel = input[0];
    const down = this.downsample(channel, sampleRate, this.targetSampleRate);
    const pcm16 = this.floatTo16BitPCM(down);

    this._buffer.push(pcm16);
    this._bufferLen += pcm16.length;

    const chunkSize = 320; // 20ms at 16kHz
    while (this._bufferLen >= chunkSize) {
      const out = new Int16Array(chunkSize);
      let filled = 0;
      
      while (filled < chunkSize) {
        const head = this._buffer[this._readIndex];
        const available = head.length - this._readOffset;
        const take = Math.min(available, chunkSize - filled);
        
        out.set(head.subarray(this._readOffset, this._readOffset + take), filled);
        filled += take;
        this._readOffset += take;

        if (this._readOffset >= head.length) {
          this._readIndex++;
          this._readOffset = 0;
        }
      }
      
      this._bufferLen -= chunkSize;
      this.port.postMessage(out.buffer, [out.buffer]);
    }

    this.compactBuffer();
    return true;
  }
}

registerProcessor("pcm-processor", PcmProcessor);
