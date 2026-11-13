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

// Reproductor: la clase compartida de whep.js con la política de la rejilla en
// vivo. Reintentos escalonados: una cámara que NO vuelve (p. ej. Canal 1 caído u
// otro códec) NO debe recrear una conexión WebRTC cada pocos segundos para
// siempre — ese churn de PeerConnections es lo que termina congelando el WebView2
// y tumba TODA la rejilla. Tras varios fallos pasa a reintentos lentos y deja
// fluidas a las cámaras sanas.
function liveRetry(n) { return n <= 4 ? 600 * n : n <= 8 ? 8000 : 20000; }

function setCellState(stateEl, txt) {
  if (!stateEl) return;
  if (!txt) { stateEl.classList.add('hidden'); return; }
  stateEl.classList.remove('hidden');
  stateEl.innerHTML = `<div class="spin"></div><div>${esc(txt)}</div>`;
}

function makePlayer(tile, video, stateEl, hi) {
  const url = (hi && tile.whep_hi) ? tile.whep_hi : tile.whep;
  const p = new WhepPlayer(url, video, {
    // 'disconnected' es TRANSITORIO (ICE se recupera solo en segundos): NO
    // reconectar por eso o se produce un churn de conexión/desconexión.
    // Solo 'failed' es definitivo; un congelado real lo caza el watchdog.
    reconnectOn: ['failed'],
    iceWaitMs: 1200,
    armOn: 'answer',
    liveOn: 'playing',
    retryDelay: liveRetry,
    // Congelado: dale tiempo a que ICE se recupere; a los 12 s intenta reanudar
    // el <video> (muchas veces solo se pausó) y recién a los 20 s reconecta.
    watchdog: { everyMs: 1000, replayMs: 12000, stallMs: 20000, minStep: 0.02 },
    onState: (st, n) => {
      if (st === 'live') setCellState(stateEl, '');
      else if (st === 'novideo') setCellState(stateEl, 'sin video');
      else if (st === 'offline') setCellState(stateEl, n <= 4 ? 'reconectando…' : 'sin señal · reintentando');
      else setCellState(stateEl, 'conectando…');
    },
  });
  p.tile = tile;
  p.t = null;          // temporizador del arranque escalonado
  return p;
}

function setStatus(txt) { $('#st').textContent = txt; }

async function build() {
  let data;
  try { data = await (await fetch('/api/wall', { cache: 'no-store' })).json(); }
  catch (e) { setStatus('sin servidor'); setTimeout(build, 3000); return; }
  const tiles = (data.tiles || []).filter(t => t.whep);   // solo las que tienen video WebRTC
  // limpiar lo anterior
  PLAYERS.forEach(p => { clearTimeout(p.t); p.stop(); }); PLAYERS = [];
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
    const p = makePlayer(t, cell.querySelector('video'), cell.querySelector('.state'), false);
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
  MAXP = makePlayer(p.tile, $('#max-v'), null, true);
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

function esc(s) { return (s == null ? '' : String(s)).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

build();
