'use strict';
// Vista DVR con MOSAICO DEL SERVIDOR: el servidor compone la cuadrícula y manda
// UN solo H.264 → el cliente decodifica 1 stream (casi thin-client). Dinámico
// 4/9/16 + paginado. Doble clic en una celda = maximizar esa cámara (stream
// directo). Autónoma: no toca el mosaico clásico.
(function () {
  const view = document.getElementById('dvrView');
  if (!view) return;
  const stageWrap = view.querySelector('#dvrGrid');
  const pager = view.querySelector('#dvrPager');
  const pageLbl = view.querySelector('#dvrPageLbl');
  const titleEl = view.querySelector('#dvrTitle');

  let CFG = { layout: 4, page: 0, pages: 1, cols: 2, rows: 2, cells: [], total: 0 };
  let GRIDP = null;     // player del mosaico (1 stream)
  let MAXP = null;      // player de la cámara maximizada (directo)
  let busy = false;

  // Reproductor: la clase compartida de whep.js, con la política del DVR. Solo
  // vídeo; el mosaico del servidor tarda unos segundos en montar, así que se
  // reintenta RÁPIDO al inicio para engancharlo apenas esté listo.
  function dvrRetry(n) { return n <= 12 ? 500 : n <= 20 ? 2000 : 8000; }
  function makePlayer(whep, video, stateEl, extra) {
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
        if (st === 'live') setState('');
        else if (st === 'novideo') setState('sin video');
        else if (st === 'offline') setState(n > 20 ? 'sin señal' : 'conectando…');
        else setState('conectando…');
      },
    }, extra || {}));
  }

  function esc(s) { return (s == null ? '' : String(s)).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  // --- pide al servidor el mosaico para (layout, page) y lo reproduce ---
  async function applyMosaic(layout, page) {
    if (busy) return; busy = true;
    try {
      const r = await fetch('/api/mosaic', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ layout, page })
      });
      CFG = await r.json();
    } catch (e) { busy = false; return; }
    renderStage();
    busy = false;
  }

  function renderStage() {
    if (GRIDP) { GRIDP.stop(); GRIDP = null; }
    const cols = CFG.cols || 2, rows = CFG.rows || 2;
    // celdas/etiquetas por porcentaje (el mosaico es 16:9 y el stage también)
    let ovl = '';
    (CFG.cells || []).forEach((c, k) => {
      const x = (k % cols) / cols * 100, y = Math.floor(k / cols) / rows * 100;
      ovl += `<div class="dvr-pos" style="left:${x}%;top:${y}%;width:${100 / cols}%;height:${100 / rows}%" data-k="${k}">
        <div class="dvr-load"><img class="dvr-loadimg" src="/static/mark.svg" alt=""></div>
        <div class="dvr-rec" title="Grabando">●</div>
        <div class="dvr-name">${esc(c.title || c.host || '')}</div></div>`;
    });
    stageWrap.innerHTML = `<div class="dvr-stage loading">
      <video id="dvrMosaicVideo" autoplay playsinline muted></video>
      <div class="dvr-ovl">${ovl}</div></div>`;
    const stage = stageWrap.querySelector('.dvr-stage');
    const vid = stage.querySelector('#dvrMosaicVideo');
    // loader por celda (marca CamWay) mientras el mosaico monta; se oculta al reproducir
    const load = on => stage.classList.toggle('loading', on);
    vid.addEventListener('playing', () => load(false));
    ['waiting', 'stalled', 'emptied'].forEach(ev => vid.addEventListener(ev, () => load(true)));
    GRIDP = makePlayer(CFG.whep, vid, null);
    GRIDP.start();
    // doble clic en una celda → maximizar esa cámara (stream directo)
    stage.querySelector('.dvr-ovl').addEventListener('dblclick', ev => {
      const pos = ev.target.closest('.dvr-pos'); if (!pos) return;
      const c = (CFG.cells || [])[+pos.dataset.k]; if (c && c.whep) maximize(c);
    });
    pageLbl.textContent = `${(CFG.page || 0) + 1}/${CFG.pages || 1}`;
    pager.style.display = (CFG.pages || 1) > 1 ? '' : 'none';
    const n = CFG.total || 0;
    const site = document.body.dataset.site || '';
    titleEl.textContent = (site ? site + ' · ' : '') + n + (n === 1 ? ' cámara' : ' cámaras');
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

  async function openDvr() {
    view.hidden = false;
    document.body.classList.add('dvr-open');
    syncWall();
    await applyMosaic(CFG.layout || 4, 0);
  }
  function closeDvr() {
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
  view.querySelector('#dvrPrev').addEventListener('click', () => { const p = ((CFG.page || 0) - 1 + (CFG.pages || 1)) % (CFG.pages || 1); applyMosaic(CFG.layout, p); });
  view.querySelector('#dvrNext').addEventListener('click', () => { const p = ((CFG.page || 0) + 1) % (CFG.pages || 1); applyMosaic(CFG.layout, p); });
  view.querySelector('#dvrLayout').addEventListener('change', e => { applyMosaic(parseInt(e.target.value, 10) || 4, 0); });
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
    else if (e.key === 'ArrowRight') { const p = ((CFG.page || 0) + 1) % (CFG.pages || 1); applyMosaic(CFG.layout, p); }
    else if (e.key === 'ArrowLeft') { const p = ((CFG.page || 0) - 1 + (CFG.pages || 1)) % (CFG.pages || 1); applyMosaic(CFG.layout, p); }
  });
})();
