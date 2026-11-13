/* CamWay — reproductor WHEP/WebRTC compartido.
 *
 * Una sola clase para app.js (mosaico), dvr.js, live.js y tv.js. Cada página
 * conserva su comportamiento con opciones del constructor:
 *
 *   new WhepPlayer(url, video, {
 *     onState(estado, intento)  estados: "connecting" | "reconnecting" |
 *                               "live" | "offline" | "novideo". También se puede
 *                               asignar después (player.onState = fn).
 *     audio          true      pide también audio (recvonly)
 *     retryDelay(n)  ms        espera antes del reintento nº n
 *                              (WhepPlayer.backoff(max) = 1 s, 2 s, 4 s … hasta max)
 *     reconnectOn    [...]     estados de la conexión que fuerzan reconectar
 *     iceWaitMs      1500      tope de espera de candidatos ICE (red local)
 *     armOn          "track"   cuándo se da la conexión por buena (retry = 0 y
 *                              arranca el watchdog): al llegar la pista o al
 *                              aplicar la respuesta WHEP ("answer")
 *     liveOn         "track"   cuándo emite "live": al llegar la pista o al primer
 *                              fotograma ("playing", con respaldo de 4 s)
 *     watchdog       null      { everyMs, stallMs, replayMs, minStep }: si el
 *                              tiempo del vídeo no avanza stallMs, reconecta; si
 *                              pasa replayMs intenta play() antes
 *     fallbackAfter  0         tras N reintentos llama a onFallback(player); si
 *                              devuelve true no se sigue reintentando WebRTC (HLS
 *                              de respaldo en tv.js)
 *     onHttpError(status, player, detail)  si devuelve true, el error HTTP del
 *                              POST WHEP lo gestiona quien llama y NO se reintenta
 *                              solo; detail = texto del error del servidor (el
 *                              «detail» del JSON, o el cuerpo recortado)
 *   })
 *
 *   start()          conecta (reinicia el contador de reintentos)
 *   stop()           parada manual: cierra y no reconecta (se puede volver a start())
 *   setQuality(hi)   cambia a urlHi / urlLo si están definidas y reconecta
 *   url              URL WHEP actual (se puede cambiar antes de start())
 */
"use strict";

class WhepPlayer {
  constructor(url, video, opts) {
    this.url = url;
    this.video = video;
    const o = Object.assign({
      onState: null,
      audio: true,
      retryDelay: WhepPlayer.backoff(10000),
      reconnectOn: ["failed", "disconnected", "closed"],
      iceWaitMs: 1500,
      armOn: "track",
      liveOn: "track",
      watchdog: null,
      fallbackAfter: 0,
      onFallback: null,
      onHttpError: null,
    }, opts || {});
    this.opts = o;
    this.onState = o.onState || (() => {});
    this.pc = null;
    this.resource = null;      // URL de la sesión WHEP (para cerrarla en el servidor)
    this.active = false;       // intención del usuario: quiere ver esta cámara
    this.retry = 0;            // reconexiones consecutivas (0 = conexión limpia)
    this.retryTimer = null;
    this.watchdogTimer = null;
    this.lastTime = 0;
    this.lastAdvance = 0;
    this.lastStatus = 0;       // último código HTTP del POST WHEP (0 = sin respuesta)
    this.urlLo = null;         // opcionales: flujo ligero / alto (ver setQuality)
    this.urlHi = null;
  }

  // Espera 1 s, 2 s, 4 s … con techo maxMs.
  static backoff(maxMs) {
    return (n) => Math.min(1000 * 2 ** (n - 1), maxMs);
  }

  async start() {
    this.active = true;
    this.retry = 0;
    await this._connect();
  }

  // Cambia entre el flujo ligero (urlLo) y el alto (urlHi). Si está en vivo, reconecta.
  setQuality(hi) {
    const target = hi ? (this.urlHi || this.url) : (this.urlLo || this.url);
    if (!target || target === this.url) return;
    this.url = target;
    if (this.active) { this.retry = 0; this._connect(); }
  }

  _emit(state) {
    try { this.onState(state, this.retry); } catch (_) { /* un consumidor roto no frena el reproductor */ }
  }

  async _connect() {
    this._teardownPc();
    if (!this.active) return;
    if (!this.url) { this._emit("novideo"); return; }
    this._emit(this.retry ? "reconnecting" : "connecting");
    try {
      const pc = new RTCPeerConnection({ iceServers: [] });
      this.pc = pc;
      pc.addTransceiver("video", { direction: "recvonly" });
      if (this.opts.audio) pc.addTransceiver("audio", { direction: "recvonly" });

      pc.ontrack = (ev) => {
        if (this.pc !== pc) return;
        if (this.video.srcObject !== ev.streams[0]) this.video.srcObject = ev.streams[0];
        this.video.play().catch(() => {});
        if (this.opts.armOn === "track") this._arm();
        this._signalLive(pc);
      };
      pc.onconnectionstatechange = () => {
        if (!this.active || this.pc !== pc) return;
        if (this.opts.reconnectOn.includes(pc.connectionState)) this._scheduleReconnect();
      };

      await pc.setLocalDescription(await pc.createOffer());
      await this.gatherIce(pc);
      if (!this.active || this.pc !== pc) return;

      // MediaMTX admite WHEP sin trickle: se manda la oferta ya completa.
      const res = await fetch(this.url, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: pc.localDescription.sdp,
      });
      this.lastStatus = res.status;
      if (!res.ok) {
        const e = new Error(`WHEP ${res.status}`);
        e.status = res.status;
        e.detail = await this._errorDetail(res);
        throw e;
      }
      // URL de la sesión: hay que hacerle DELETE al cerrar, si no MediaMTX
      // deja la sesión viva (zombi) y se acumulan → CPU y lag.
      const loc = res.headers.get("Location");
      if (loc) { try { this.resource = new URL(loc, window.location.href).href; } catch (_) {} }
      const answer = await res.text();
      if (!this.active || this.pc !== pc) return;
      await pc.setRemoteDescription({ type: "answer", sdp: answer });
      if (this.opts.armOn === "answer") this._arm();
    } catch (err) {
      if (!this.active) return;
      if (err && err.status && this.opts.onHttpError &&
          this.opts.onHttpError(err.status, this, err.detail || "") === true) {
        this._teardownPc();       // lo gestiona quien llama: sin reintento automático
        return;
      }
      this._scheduleReconnect();
    }
  }

  // Texto del error de una respuesta no-OK (JSON {"detail": …} o cuerpo recortado).
  async _errorDetail(res) {
    try {
      const t = await res.text();
      try { return String(JSON.parse(t).detail || ""); } catch (_) { return t.slice(0, 200); }
    } catch (_) { return ""; }
  }

  // La conexión se da por buena: se reinicia el contador y arranca el watchdog.
  _arm() {
    this.retry = 0;
    this._startWatchdog();
  }

  // Emite "live" al llegar la pista o, según liveOn, al primer fotograma.
  _signalLive(pc) {
    if (this.opts.liveOn !== "playing") { this._emit("live"); return; }
    const done = () => { if (this.active && this.pc === pc) this._emit("live"); };
    this.video.addEventListener("playing", done, { once: true });
    setTimeout(done, 4000);     // respaldo: no dejar el estado «conectando» para siempre
  }

  // Vigila que el vídeo avance; si se congela (MediaMTX perdió la cámara pero
  // la conexión sigue "viva"), fuerza la reconexión.
  _startWatchdog() {
    this._stopWatchdog();
    const w = this.opts.watchdog;
    if (!w) return;
    this.lastTime = this.video.currentTime;
    this.lastAdvance = Date.now();
    this.watchdogTimer = setInterval(() => {
      if (!this.active || !this.pc) return;
      const t = this.video.currentTime;
      if (t > this.lastTime + (w.minStep || 0.05)) {
        this.lastTime = t;
        this.lastAdvance = Date.now();
        return;
      }
      const stalled = Date.now() - this.lastAdvance;
      if (w.replayMs && stalled > w.replayMs && stalled < w.stallMs) {
        try { this.video.play().catch(() => {}); } catch (_) {}   // a veces solo se pausó
      } else if (stalled >= w.stallMs) {
        this.lastAdvance = Date.now();
        this._scheduleReconnect();
      }
    }, w.everyMs || 2000);
  }

  _stopWatchdog() {
    if (this.watchdogTimer) { clearInterval(this.watchdogTimer); this.watchdogTimer = null; }
  }

  _scheduleReconnect() {
    if (!this.active || this.retryTimer) return;   // ya hay una en cola
    this._stopWatchdog();
    this._teardownPc();
    this.retry++;
    const o = this.opts;
    if (o.fallbackAfter && this.retry > o.fallbackAfter && o.onFallback && o.onFallback(this) === true) {
      return;                                       // el respaldo (HLS) toma el relevo
    }
    this._emit("offline");
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this._connect();
    }, o.retryDelay(this.retry));
  }

  _teardownPc() {
    if (this.pc) {
      try { this.pc.close(); } catch (_) {}
      this.pc = null;
    }
    if (this.resource) {
      // Cierra la sesión en MediaMTX (si no, queda zombi consumiendo CPU).
      try { fetch(this.resource, { method: "DELETE", keepalive: true }).catch(() => {}); } catch (_) {}
      this.resource = null;
    }
    if (this.video) { try { this.video.srcObject = null; } catch (_) {} }
  }

  gatherIce(pc) {
    return new Promise((resolve) => {
      if (pc.iceGatheringState === "complete") return resolve();
      const done = () => {
        if (pc.iceGatheringState === "complete") {
          pc.removeEventListener("icegatheringstatechange", done);
          resolve();
        }
      };
      pc.addEventListener("icegatheringstatechange", done);
      // Red local: si algún candidato se atasca, no vale la pena esperarlo.
      setTimeout(resolve, this.opts.iceWaitMs);
    });
  }

  stop() {   // parada manual: no reconecta
    this.active = false;
    this.retry = 0;
    if (this.retryTimer) { clearTimeout(this.retryTimer); this.retryTimer = null; }
    this._stopWatchdog();
    this._teardownPc();
  }
}
