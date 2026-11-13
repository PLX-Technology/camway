// AudioWorklet de talkback: toma el micrófono (a la frecuencia del contexto),
// lo remuestrea a 8 kHz, lo codifica en G.711 A-law y publica tramas de 160
// bytes (20 ms) por el puerto. El servidor las envuelve en RTP y las manda al
// altavoz de la cámara por el backchannel ONVIF.

// Codificador A-law (implementación verificada, alawmulaw). Silencio = 0xD5.
const LOG_TABLE = [
  1, 1, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4,
  5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
  6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
  6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
];

function encodeALaw(sample) {
  sample = sample === -32768 ? -32767 : sample;
  let sign = (~sample >> 8) & 0x80;
  if (!sign) sample = -sample;
  if (sample > 32635) sample = 32635;
  let companded;
  if (sample >= 256) {
    const exponent = LOG_TABLE[(sample >> 8) & 0x7f];
    const mantissa = (sample >> (exponent + 3)) & 0x0f;
    companded = (exponent << 4) | mantissa;
  } else {
    companded = sample >> 4;
  }
  return (companded ^ (sign ^ 0x55)) & 0xff;
}

const FRAME = 160; // 20 ms a 8 kHz

class TalkProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 8000; // p.ej. 48000/8000 = 6
    this.phase = 0;
    this.out = new Uint8Array(FRAME);
    this.idx = 0;
  }

  process(inputs) {
    const input = inputs[0];
    const ch = input && input[0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.phase -= 1;
      if (this.phase <= 0) {
        this.phase += this.ratio;
        let f = ch[i];
        if (f > 1) f = 1; else if (f < -1) f = -1;
        const s = f < 0 ? f * 32768 : f * 32767;
        this.out[this.idx++] = encodeALaw(s | 0);
        if (this.idx >= FRAME) {
          // copia para transferir (el buffer se reutiliza)
          this.port.postMessage(this.out.slice(0).buffer);
          this.idx = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("talk-processor", TalkProcessor);
