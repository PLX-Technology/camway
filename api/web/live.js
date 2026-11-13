'use strict';
// Rejilla de video EN VIVO por WebRTC (cliente embebido de taladro).
// Objetivo: pocas cámaras, FLUIDO, baja latencia y bajo consumo:
//  - usa el substream (whep) para la rejilla; el principal (whep_hi) solo al maximizar
//  - decodificación por hardware del WebView2 (Chromium)
//  - reconexión automática + vigilancia de "congelado" (cortes de luz en taladro)
const $ = s => document.querySelector(s);
const GRID = $('#grid');
let PLAYERS = [];
let MAXP = null;              // player maximizado (overlay)

function iceDone(pc, ms = 1200) {
  return new Promise(res => {
    if (pc.iceGatheringState === 'complete') return res();
    const t = setTimeout(res, ms);
    pc.addEventListener('icegatheringstatechange', () => {
      if (pc.iceGatheringState === 'complete') { clearTimeout(t); res(); }
    });
  });
}

class Player {
  constructor(tile, video, stateEl) {
    this.tile = tile; this.video = video; this.stateEl = stateEl;
    this.pc = null; this.loc = null; this.retry = 0; this.dead = false;
    this.hi = false; this.t = null; this.lastTime = 0; this.lastAdvance = Date.now();
  }
  url() { return (this.hi && this.tile.whep_hi) ? this.tile.whep_hi : this.tile.whep; }
  setState(txt) {
    if (!this.stateEl) return;
    if (!txt) { this.stateEl.classList.add('hidden'); return; }
    this.stateEl.classList.remove('hidden');
    this.stateEl.innerHTML = `<div class="spin"></div><div>${txt}</div>`;
  }
  async start() {
    this._teardown();
    if (this.dead) return;
    if (!this.url()) { this.setState('sin video'); return; }
    this.setState('conectando…');
    try {
      const pc = new RTCPeerConnection({ iceServers: [] });
      this.pc = pc;
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });
      pc.ontrack = e => { if (this.video.srcObject !== e.streams[0]) this.video.srcObject = e.streams[0]; };
      pc.onconnectionstatechange = () => {
        // 'disconnected' es TRANSITORIO (ICE se recupera solo en segundos): NO
        // reconectar por eso o se produce un churn de conexión/desconexión.
        // Solo 'failed' es definitivo; un congelado real lo caza el watchdog.
        if (pc === this.pc && pc.connectionState === 'failed') this._reconnect();
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await iceDone(pc);
      const res = await fetch(this.url(), {
        method: 'POST', headers: { 'Content-Type': 'application/sdp' },
        body: pc.localDescription.sdp,
      });
      if (!res.ok) throw new Error('whep ' + res.status);
      const ans = await res.text();
      this.loc = res.headers.get('Location');
      await pc.setRemoteDescription({ type: 'answer', sdp: ans });
      this.retry = 0; this.lastAdvance = Date.now();
      const clear = () => this.setState('');
      this.video.addEventListener('playing', clear, { once: true });
      setTimeout(() => { if (this.pc === pc) clear(); }, 4000);   // respaldo
    } catch (e) { this._reconnect(); }
  }
  _reconnect() {
    if (this.dead) return;
    this.retry += 1;
    // Backoff escalonado. Lo clave: una cámara que NO vuelve (p. ej. Canal 1
    // caído u otro códec) NO debe recrear una conexión WebRTC cada pocos
    // segundos para siempre — ese churn de PeerConnections es lo que termina
    // congelando el WebView2 y tumba TODA la rejilla. Tras varios fallos pasa
    // a reintentos lentos y deja fluidas a las cámaras sanas.
    let delay;
    if (this.retry <= 4) { delay = 600 * this.retry; this.setState('reconectando…'); }   // 0.6–2.4 s: corte transitorio
    else if (this.retry <= 8) { delay = 8000; this.setState('sin señal · reintentando'); }
    else { delay = 20000; this.setState('sin señal · reintentando'); }                     // caída sostenida: no machacar
    clearTimeout(this.t);
    this.t = setTimeout(() => this.start(), delay);
  }
  _teardown() {
    clearTimeout(this.t);
    try { if (this.pc) this.pc.close(); } catch (e) {}
    if (this.loc) { fetch(this.loc, { method: 'DELETE' }).catch(() => {}); this.loc = null; }
    this.pc = null;
    try { this.video.srcObject = null; } catch (e) {}
  }
  stop() { this.dead = true; this._teardown(); }
  // vigilancia de congelado: si el tiempo del video no avanza, reconectar
  watch() {
    if (this.dead || !this.pc) return;
    const t = this.video.currentTime;
    if (t > this.lastTime + 0.02) { this.lastTime = t; this.lastAdvance = Date.now(); return; }
    // Congelado: dale tiempo a que ICE se recupere; a los 12 s intenta reanudar
    // el <video> (muchas veces solo se pausó) y recién a los 20 s reconecta.
    const stalled = Date.now() - this.lastAdvance;
    if (stalled > 12000 && stalled <= 20000) { try { this.video.play().catch(() => {}); } catch (e) {} }
    else if (stalled > 20000) { this.lastAdvance = Date.now(); this._reconnect(); }
  }
}

function setStatus(txt) { $('#st').textContent = txt; }

async function build() {
  let data;
  try { data = await (await fetch('/api/wall', { cache: 'no-store' })).json(); }
  catch (e) { setStatus('sin servidor'); setTimeout(build, 3000); return; }
  const tiles = (data.tiles || []).filter(t => t.whep);   // solo las que tienen video WebRTC
  // limpiar lo anterior
  PLAYERS.forEach(p => p.stop()); PLAYERS = [];
  GRID.innerHTML = '';
  if (!tiles.length) {
    $('#empty').classList.add('on');
    setStatus('0 cámaras');
    return;
  }
  $('#empty').classList.remove('on');
  // columnas: 1→1, 2→2, 3-4→2, 5-6→3, 7-9→3, >9→4
  const n = tiles.length;
  const cols = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  GRID.style.setProperty('--cols', cols);
  tiles.forEach(t => {
    const cell = document.createElement('div');
    cell.className = 'cell';
    cell.innerHTML = `<video autoplay playsinline muted></video>
      <div class="state"><div class="spin"></div><div>conectando…</div></div>
      <div class="name">${esc(t.title || t.host || '')}</div>`;
    GRID.appendChild(cell);
    const p = new Player(t, cell.querySelector('video'), cell.querySelector('.state'));
    cell.onclick = () => maximize(p);
    PLAYERS.push(p);
  });
  // Arranque ESCALONADO: disparar las N negociaciones WebRTC a la vez saturaba
  // el WebView2 al abrir (ventana "no responde", arranque en negro). ~350 ms
  // entre cámaras suaviza el pico sin sacrificar fluidez una vez conectadas.
  PLAYERS.forEach((p, i) => { p.t = setTimeout(() => p.start(), i * 350); });
  setStatus(n + (n === 1 ? ' cámara' : ' cámaras'));
}

// ---- maximizar una cámara (usa el principal whep_hi si existe) ----
function maximize(p) {
  MAXP = new Player(p.tile, $('#max-v'), null);
  MAXP.hi = true;
  $('#max-name').textContent = p.tile.title || '';
  $('#max').classList.add('on');
  MAXP.start();
}
function restore() {
  if (MAXP) { MAXP.stop(); MAXP = null; }
  $('#max').classList.remove('on');
  $('#max-v').srcObject = null;
}
$('#max').onclick = restore;
document.addEventListener('keydown', e => { if (e.key === 'Escape') restore(); });

// agregar cámaras → asistente de NVR; ⚙ → interfaz completa de administración
$('#btn-admin').onclick = () => location.href = '/nvr';
$('#empty-admin').onclick = () => location.href = '/nvr';

// vigilancia global de congelado (1/s) + del maximizado
setInterval(() => { PLAYERS.forEach(p => p.watch()); if (MAXP) MAXP.watch(); }, 1000);

// recargar el catálogo cada tanto por si se agregan/quitan cámaras
setInterval(async () => {
  try {
    const d = await (await fetch('/api/wall', { cache: 'no-store' })).json();
    const now = (d.tiles || []).filter(t => t.whep).map(t => t.stream_id).sort().join('|');
    const cur = PLAYERS.map(p => p.tile.stream_id).sort().join('|');
    // Solo reconstruir si de verdad cambió el CONJUNTO de cámaras; nunca por un
    // muro vacío transitorio (evita que se caiga toda la rejilla cada tanto).
    if (now && now !== cur) build();
  } catch (e) {}
}, 15000);

function esc(s) { return (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

build();
