class PcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = [];
    this._bufferLen = 0;
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
        const head = this._buffer[0];
        const take = Math.min(head.length, chunkSize - filled);
        out.set(head.subarray(0, take), filled);
        filled += take;

        if (take === head.length) this._buffer.shift();
        else this._buffer[0] = head.subarray(take);
      }
      this._bufferLen -= chunkSize;
      this.port.postMessage(out.buffer, [out.buffer]);
    }

    return true;
  }
}

registerProcessor("pcm-processor", PcmProcessor);
