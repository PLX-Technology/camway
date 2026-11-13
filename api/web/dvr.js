'use strict';
// Vista DVR con MOSAICO DEL SERVIDOR: el servidor compone la cuadrícula y manda
// UN solo H.264 → el cliente decodifica 1 stream (casi thin-client). Rejilla
// Auto/1/2×2/3×3/4×4 + paginado. Doble clic (o Intro) en una celda = maximizar esa
// cámara (stream directo). Autónoma: no toca el mosaico clásico.
(function () {
  const view = document.getElementById('dvrView');
  if (!view) return;
  const stageWrap = view.querySelector('#dvrGrid');
  const pager = view.querySelector('#dvrPager');
  const pageLbl = view.querySelector('#dvrPageLbl');
  const titleEl = view.querySelector('#dvrTitle');
  const countEl = view.querySelector('#dvrCount');
  const clockEl = view.querySelector('#dvrClock');
  const segBtns = [...view.querySelectorAll('#dvrSeg [data-layout]')];

  let CFG = { layout: 4, page: 0, pages: 1, cols: 2, rows: 2, cells: [], total: 0 };
  let GRIDP = null;     // player del mosaico (1 stream)
  let MAXP = null;      // player de la cámara maximizada (directo)
  let busy = false;

  // Rejilla elegida: "auto" (según el nº de cámaras) o 1/4/9/16 (celdas por página).
  // Auto: 1 cámara → 1×1, ≤4 → 2×2, ≤9 → 3×3, si no 4×4. Se recuerda en el navegador.
  let pref = 'auto';
  try { const p = localStorage.getItem('camway.dvr.layout'); if (['auto', '1', '4', '9', '16'].includes(p)) pref = p; } catch (e) {}
  function autoLayout(total) { return total <= 1 ? 1 : total <= 4 ? 4 : total <= 9 ? 9 : 16; }
  function prefLayout() {
    if (pref !== 'auto') return Number(pref);
    const known = CFG.total || (typeof state !== 'undefined' && state.tiles ? state.tiles.size : 0);
    return known ? autoLayout(known) : 4;       // sin dato aún: 4, y se corrige al saber el total
  }
  function paintSeg() { segBtns.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.layout === pref))); }

  // Reproductor: la clase compartida de whep.js, con la política del DVR. Solo
  // vídeo; el mosaico del servidor tarda unos segundos en montar, así que se
  // reintenta RÁPIDO al inicio para engancharlo apenas esté listo.
  function dvrRetry(n) { return n <= 12 ? 500 : n <= 20 ? 2000 : 8000; }
  function makePlayer(whep, video, stateEl, extra) {
    const onLive = extra && extra.onLive;
    const setState = txt => {
      if (!stateEl) return;
      stateEl.textContent = txt || '';
      stateEl.style.display = txt ? '' : 'none';
    };
    return new WhepPlayer(whep, video, Object.assign({
      audio: false,
      reconnectOn: ['failed'],
      iceWaitMs: 1200,
      armOn: 'answer',
      liveOn: 'playing',
      retryDelay: dvrRetry,
      // se congela: a los 12 s intenta reanudar el <video>; a los 20 s reconecta
      watchdog: { everyMs: 1000, replayMs: 12000, stallMs: 20000, minStep: 0.02 },
      onState: (st, n) => {
        if (st === 'live') { setState(''); if (onLive) onLive(); }
        else if (st === 'novideo') setState('sin video');
        else if (st === 'offline') setState(n > 20 ? 'sin señal' : 'conectando…');
        else setState('conectando…');
      },
    }, extra || {}));
  }

  function esc(s) { return (s == null ? '' : String(s)).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  // Mensaje en lugar del mosaico (error del servidor, permisos…). role=alert para
  // que los lectores de pantalla lo anuncien; reutiliza .dvr-state del CSS.
  function stageMessage(text) {
    if (GRIDP) { GRIDP.stop(); GRIDP = null; }
    stageWrap.innerHTML = `<div class="dvr-stage"><div class="dvr-state" role="alert">${esc(text)}</div></div>`;
  }

  // --- pide al servidor el mosaico para (layout, page) y lo reproduce ---
  async function applyMosaic(layout, page, again) {
    if (busy) return; busy = true;
    try {
      let r;
      try {
        r = await fetch('/api/mosaic', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ layout, page })
        });
      } catch (e) { stageMessage('No se pudo contactar con el servidor.'); return; }
      if (!r.ok) {
        // 403 «no tienes cámaras asignadas», 401 sesión caducada…: se muestra el
        // texto del servidor y NO se abre el reproductor.
        let detail = '';
        try { detail = (await r.json()).detail || ''; } catch (e) {}
        stageMessage(detail || ('El servidor respondió ' + r.status + '.'));
        return;
      }
      try { CFG = await r.json(); }
      catch (e) { stageMessage('Respuesta no válida del servidor.'); return; }
      // Auto: ahora que se sabe cuántas cámaras hay, se pide la rejilla que toca.
      if (pref === 'auto' && !again && autoLayout(CFG.total || 0) !== CFG.layout && CFG.total) {
        busy = false;
        return await applyMosaic(autoLayout(CFG.total), 0, true);
      }
      renderStage();
    } finally { busy = false; }
  }

  // --- el mosaico del servidor es UNO para todos (un solo ffmpeg): lo arma quien
  // llamó último a POST /api/mosaic, con SUS cámaras. Si otra persona lo armó con
  // cámaras que yo no tengo, /whep/mosaico responde 403 (o 404 «mosaico no armado»).
  // No se reintenta en bucle: se rearma UNA vez el mío y, si vuelve el 403 (dos
  // personas alternando), se espera 5 s, 10 s, 20 s… y se explica en pantalla.
  const REARM_BASE_MS = 5000, REARM_MAX_MS = 30000;
  let rearmTries = 0, rearmTimer = null;

  function resetRearm() { rearmTries = 0; clearTimeout(rearmTimer); rearmTimer = null; }

  // Acción del usuario (abrir, página, cuadrícula): empieza de cero.
  function userApplyMosaic(layout, page) { resetRearm(); return applyMosaic(layout, page); }

  function mosaicDenied(status, player, detail) {
    if (player !== GRIDP) return false;            // solo el del mosaico
    const foreign = status === 403;
    const notArmed = status === 404 && /no armado/i.test(detail || '');
    if (!foreign && !notArmed) return false;       // p. ej. el ffmpeg montándose: reintento normal
    if (rearmTries === 0) { rearmTries = 1; applyMosaic(CFG.layout, CFG.page); return true; }
    const wait = Math.min(REARM_BASE_MS * 2 ** (rearmTries - 1), REARM_MAX_MS);
    rearmTries++;
    stageMessage((foreign
      ? 'Otra persona está usando el mosaico con otras cámaras.'
      : 'El mosaico todavía no está armado.') + ' Reintentando en ' + Math.round(wait / 1000) + ' s…');
    clearTimeout(rearmTimer);
    rearmTimer = setTimeout(() => { rearmTimer = null; if (!view.hidden) applyMosaic(CFG.layout, CFG.page); }, wait);
    return true;
  }

  function renderStage() {
    if (GRIDP) { GRIDP.stop(); GRIDP = null; }
    const cols = CFG.cols || 2, rows = CFG.rows || 2;
    // Celdas por porcentaje (el mosaico es 16:9 y el escenario también). Las que no
    // tienen cámara se tapan con «Libre» (borde punteado): así no se ve el logo
    // grande que el servidor dibuja en las celdas vacías.
    let ovl = '';
    for (let k = 0; k < cols * rows; k++) {
      const c = (CFG.cells || [])[k];
      const x = (k % cols) / cols * 100, y = Math.floor(k / cols) / rows * 100;
      const box = `left:${x}%;top:${y}%;width:${100 / cols}%;height:${100 / rows}%`;
      if (!c) { ovl += `<div class="dvr-pos dvr-empty" style="${box}"><span>Libre</span></div>`; continue; }
      const nm = esc(c.title || c.host || '');
      ovl += `<div class="dvr-pos" style="${box}" data-k="${k}" tabindex="0" role="button" aria-label="Ampliar ${nm}">
        <div class="dvr-load" aria-hidden="true">${typeof icon === 'function' ? icon('refresh', 26) : ''}</div>
        <div class="dvr-name"><span class="cw-dot"></span>${nm}</div></div>`;
    }
    stageWrap.innerHTML = `<div class="dvr-stage loading">
      <video id="dvrMosaicVideo" autoplay playsinline muted></video>
      <div class="dvr-ovl">${ovl}</div></div>`;
    const stage = stageWrap.querySelector('.dvr-stage');
    const vid = stage.querySelector('#dvrMosaicVideo');
    // loader por celda (marca CamWay) mientras el mosaico monta; se oculta al reproducir
    const load = on => stage.classList.toggle('loading', on);
    vid.addEventListener('playing', () => load(false));
    ['waiting', 'stalled', 'emptied'].forEach(ev => vid.addEventListener(ev, () => load(true)));
    GRIDP = makePlayer(CFG.whep, vid, null, { onHttpError: mosaicDenied, onLive: resetRearm });
    GRIDP.start();
    // doble clic en una celda → maximizar esa cámara (stream directo)
    const ovlEl = stage.querySelector('.dvr-ovl');
    const open = pos => { const c = pos && (CFG.cells || [])[+pos.dataset.k]; if (c && c.whep) maximize(c); };
    ovlEl.addEventListener('dblclick', ev => open(ev.target.closest('.dvr-pos[data-k]')));
    ovlEl.addEventListener('keydown', ev => {                   // teclado: Intro amplía la celda enfocada
      if (ev.key === 'Enter') { const pos = ev.target.closest('.dvr-pos[data-k]'); if (pos) { ev.preventDefault(); open(pos); } }
    });
    const pages = CFG.pages || 1, n = CFG.total || 0;
    pageLbl.textContent = `Página ${(CFG.page || 0) + 1} de ${pages}`;
    pager.style.display = pages > 1 ? '' : 'none';
    countEl.textContent = n + (n === 1 ? ' cámara' : ' cámaras');
    titleEl.textContent = document.body.dataset.site || 'CamWay';
    paintSeg();
  }

  function maximize(c) {
    view.classList.add('maxed');
    view.querySelector('#dvrMaxName').textContent = c.title || '';
    // Pausa el mosaico mientras se ve una cámara ampliada: su <video> queda
    // OCULTO y, si sigue recibiendo, el jitter buffer de WebRTC acumula retraso
    // → al volver se veía imagen vieja. Además evita el doble stream.
    if (GRIDP) GRIDP.stop();
    MAXP = makePlayer(c.whep, view.querySelector('#dvrMaxVideo'), view.querySelector('#dvrMaxState'));
    MAXP.start();
  }
  function unmaximize() {
    if (MAXP) { MAXP.stop(); MAXP = null; }
    try { view.querySelector('#dvrMaxVideo').srcObject = null; } catch (e) {}
    view.classList.remove('maxed');
    // Reconecta el mosaico desde cero → vuelve al BORDE EN VIVO (sin el retraso
    // acumulado mientras estuvo oculto). El ffmpeg del servidor sigue caliente,
    // así que reenganchar es casi instantáneo.
    if (GRIDP) GRIDP.start();
  }

  // El muro de app.js se detiene solo mientras body.dvr-open esté puesto.
  function syncWall() { try { window.CamWayWall && window.CamWayWall.sync(); } catch (e) {} }

  async function hasCams() {
    try { const d = await (await fetch('/api/wall', { cache: 'no-store' })).json(); return (d.tiles || []).some(t => t.whep); }
    catch (e) { return false; }
  }

  // Reloj de la barra (solo mientras el DVR está abierto).
  let clockTimer = null;
  function tickClock() {
    const d = new Date();
    clockEl.dateTime = d.toISOString();
    clockEl.textContent = d.toLocaleString('es', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' })
      .replace(',', '').replace(',', ' ·');
  }

  async function openDvr() {
    view.hidden = false;
    document.body.classList.add('dvr-open');
    syncWall();
    tickClock(); clearInterval(clockTimer); clockTimer = setInterval(tickClock, 1000);
    paintSeg();
    await userApplyMosaic(prefLayout(), 0);
  }
  function closeDvr() {
    clearInterval(clockTimer); clockTimer = null;
    resetRearm();
    unmaximize();
    if (GRIDP) { GRIDP.stop(); GRIDP = null; }
    // NO se detiene el mosaico del servidor al cerrar: se deja CALIENTE para que
    // reabrir el DVR sea instantáneo (estación de monitoreo). Se reconfigura solo
    // al cambiar layout/página.
    view.hidden = true;
    document.body.classList.remove('dvr-open');
    try { if (document.fullscreenElement) document.exitFullscreen(); } catch (e) {}
    syncWall();
  }

  async function autoStart() { if (await hasCams()) openDvr(); }
  window.CamWayDVR = { open: openDvr, close: closeDvr, autoStart };

  document.getElementById('btnDvr')?.addEventListener('click', openDvr);
  view.querySelector('#dvrClose').addEventListener('click', closeDvr);
  view.querySelector('#dvrPrev').addEventListener('click', () => { const p = ((CFG.page || 0) - 1 + (CFG.pages || 1)) % (CFG.pages || 1); userApplyMosaic(CFG.layout, p); });
  view.querySelector('#dvrNext').addEventListener('click', () => { const p = ((CFG.page || 0) + 1) % (CFG.pages || 1); userApplyMosaic(CFG.layout, p); });
  segBtns.forEach(b => b.addEventListener('click', () => {
    pref = b.dataset.layout;
    try { localStorage.setItem('camway.dvr.layout', pref); } catch (e) {}
    paintSeg();
    userApplyMosaic(prefLayout(), 0);
  }));
  view.querySelector('#dvrUnmax').addEventListener('click', unmaximize);
  view.querySelector('#dvrFull').addEventListener('click', () => {
    try { if (document.fullscreenElement) document.exitFullscreen(); else view.requestFullscreen(); } catch (e) {}
  });
  view.querySelector('#dvrMax').addEventListener('dblclick', unmaximize);
  // (Antes aquí se reconectaba el mosaico al volver la pestaña a primer plano.
  // Se quitó: WebRTC se mantiene en vivo aunque la pestaña esté tapada —no
  // acumula buffer como HLS— así que reconectar solo causaba un parpadeo
  // molesto al tapar/destapar la ventana. El salto a vivo al volver de
  // MAXIMIZAR —la queja original— sí se conserva en unmaximize().)
  document.addEventListener('keydown', e => {
    if (view.hidden) return;
    if (e.key === 'Escape') { view.classList.contains('maxed') ? unmaximize() : closeDvr(); }
    else if (e.key === 'ArrowRight') { const p = ((CFG.page || 0) + 1) % (CFG.pages || 1); userApplyMosaic(CFG.layout, p); }
    else if (e.key === 'ArrowLeft') { const p = ((CFG.page || 0) - 1 + (CFG.pages || 1)) % (CFG.pages || 1); userApplyMosaic(CFG.layout, p); }
  });
})();
