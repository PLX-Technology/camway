'use strict';
// Vista DVR con DOS modos:
//
//  - directo  (por defecto): reproduce cada cámara por separado (WebRTC directo,
//    sin recomprimir). Mismo retardo que a pantalla completa (~0,2-0,4 s). El
//    visor decodifica N streams (4-9 es trivial para un PC o una TV).
//  - mosaico: el servidor compone UNA imagen y el cliente decodifica 1 stream
//    (casi thin-client), a cambio de ~1 s más de retardo porque hay que
//    decodificar, componer y recodificar.
//
// Rejilla Auto/1/2×2/3×3/4×4 + paginado. Doble clic (o Intro) en una celda =
// maximizar esa cámara (stream directo). Autónoma: no toca el muro clásico.
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
  const modeBtns = [...view.querySelectorAll('#dvrMode [data-mode]')];

  let CFG = { layout: 4, page: 0, pages: 1, cols: 2, rows: 2, cells: [], total: 0 };
  let GRIDP = null;     // player del mosaico del servidor (modo mosaico)
  let CELLP = [];       // un player por celda (modo directo)
  let MAXP = null;      // player de la cámara maximizada (directo)
  let busy = false;

  // Rejilla elegida: "auto" (según el nº de cámaras) o 1/4/9/16 (celdas por página).
  let pref = 'auto';
  try { const p = localStorage.getItem('camway.dvr.layout'); if (['auto', '1', '4', '9', '16'].includes(p)) pref = p; } catch (e) {}

  // Modo: directo por defecto (es el que va en tiempo real).
  let mode = 'direct';
  try {
    const m = localStorage.getItem('camway.dvr.mode');
    if (m === 'direct' || m === 'mosaic') mode = m;
  } catch (e) {}

  function autoLayout(total) { return total <= 1 ? 1 : total <= 4 ? 4 : total <= 9 ? 9 : 16; }
  function prefLayout() {
    if (pref !== 'auto') return Number(pref);
    const known = CFG.total || (typeof state !== 'undefined' && state.tiles ? state.tiles.size : 0);
    return known ? autoLayout(known) : 4;
  }
  function paintSeg() {
    segBtns.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.layout === pref)));
    modeBtns.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
  }
  function gridOf(layout) {
    const n = layout === 1 ? 1 : layout === 4 ? 2 : layout === 9 ? 3 : 4;
    return { cols: n, rows: n };
  }

  // Reproductor: la clase compartida de whep.js, con la política del DVR. Solo
  // vídeo; en mosaico el servidor tarda unos segundos en montar, así que se
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

  // --- parar / arrancar lo que esté en pantalla ---
  function stopGrid() {
    if (GRIDP) { try { GRIDP.stop(); } catch (e) {} GRIDP = null; }
    CELLP.forEach(p => { try { p.stop(); } catch (e) {} });
    CELLP = [];
  }
  function pauseGrid() {
    if (GRIDP) { try { GRIDP.stop(); } catch (e) {} }
    CELLP.forEach(p => { try { p.stop(); } catch (e) {} });
  }
  function resumeGrid() {
    // Reconectar desde cero = volver al borde en vivo (sin el retraso acumulado
    // mientras estuvo oculto por la maximización).
    if (mode === 'mosaic') { if (GRIDP) GRIDP.start(); return; }
    CELLP.forEach(p => { try { p.start(); } catch (e) {} });
  }

  // Mensaje en lugar de la rejilla (error del servidor, permisos…). role=alert.
  function stageMessage(text) {
    stopGrid();
    stageWrap.innerHTML = `<div class="dvr-stage"><div class="dvr-state" role="alert">${esc(text)}</div></div>`;
  }

  // ======================================================================
  //  Modo DIRECTO: una cámara por celda, sin recomprimir
  // ======================================================================
  async function applyDirect(layout, page) {
    let d;
    try {
      const r = await fetch('/api/wall', { cache: 'no-store' });
      if (!r.ok) { stageMessage('El servidor respondió ' + r.status + '.'); return; }
      d = await r.json();
    } catch (e) { stageMessage('No se pudo contactar con el servidor.'); return; }

    const tiles = (d.tiles || []).filter(t => t.whep);
    // Auto: con el total real se pide la rejilla que toca.
    if (pref === 'auto' && autoLayout(tiles.length) !== layout) {
      layout = autoLayout(tiles.length);
    }
    const { cols, rows } = gridOf(layout);
    const per = cols * rows;
    const pages = Math.max(1, Math.ceil(tiles.length / per) || 1);
    page = Math.max(0, Math.min(page, pages - 1));
    CFG = {
      layout, page, pages, cols, rows, total: tiles.length,
      cells: tiles.slice(page * per, page * per + per),
      whep: null,
    };
    renderDirect();
    renderBar();
  }

  function renderDirect() {
    stopGrid();
    const cols = CFG.cols || 2, rows = CFG.rows || 2;
    let ovl = '';
    for (let k = 0; k < cols * rows; k++) {
      const c = (CFG.cells || [])[k];
      const x = (k % cols) / cols * 100, y = Math.floor(k / cols) / rows * 100;
      const box = `left:${x}%;top:${y}%;width:${100 / cols}%;height:${100 / rows}%`;
      if (!c) { ovl += `<div class="dvr-pos dvr-empty" style="${box}"><span>Libre</span></div>`; continue; }
      const nm = esc(c.title || c.host || '');
      ovl += `<div class="dvr-pos" style="${box}" data-k="${k}" tabindex="0" role="button" aria-label="Ampliar ${nm}">
        <video autoplay playsinline muted></video>
        <div class="dvr-load" aria-hidden="true">${typeof icon === 'function' ? icon('refresh', 22) : ''}</div>
        <div class="dvr-state" style="display:none"></div>
        <div class="dvr-name"><span class="cw-dot"></span>${nm}</div></div>`;
    }
    stageWrap.innerHTML = `<div class="dvr-stage direct"><div class="dvr-ovl">${ovl}</div></div>`;
    const stage = stageWrap.querySelector('.dvr-stage');
    // Un reproductor por celda. El spinner de cada celda se apaga cuando ESA
    // cámara empieza a reproducir (si una tarda, no bloquea a las demás).
    stage.querySelectorAll('.dvr-pos[data-k]').forEach(pos => {
      const c = (CFG.cells || [])[+pos.dataset.k];
      if (!c || !c.whep) return;
      const vid = pos.querySelector('video');
      const st = pos.querySelector('.dvr-state');
      pos.classList.add('loading');
      const p = makePlayer(c.whep, vid, st, {
        onLive: () => pos.classList.remove('loading'),
      });
      CELLP.push(p);
      p.start();
    });
    bindCells(stage);
  }

  // ======================================================================
  //  Modo MOSAICO: el servidor compone una sola imagen
  // ======================================================================
  async function applyMosaic(layout, page, again) {
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
      return await applyMosaic(autoLayout(CFG.total), 0, true);
    }
    renderMosaic();
    renderBar();
  }

  // --- el mosaico del servidor es UNO para todos (un solo ffmpeg): lo arma quien
  // llamó último a POST /api/mosaic, con SUS cámaras. Si otra persona lo armó con
  // cámaras que yo no tengo, /whep/mosaico responde 403 (o 404 «mosaico no armado»).
  // No se reintenta en bucle: se rearma UNA vez el mío y, si vuelve el 403 (dos
  // personas alternando), se espera 5 s, 10 s, 20 s… y se explica en pantalla.
  const REARM_BASE_MS = 5000, REARM_MAX_MS = 30000;
  let rearmTries = 0, rearmTimer = null;

  function resetRearm() { rearmTries = 0; clearTimeout(rearmTimer); rearmTimer = null; }

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

  function renderMosaic() {
    stopGrid();
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
    const load = on => stage.classList.toggle('loading', on);
    vid.addEventListener('playing', () => load(false));
    ['waiting', 'stalled', 'emptied'].forEach(ev => vid.addEventListener(ev, () => load(true)));
    GRIDP = makePlayer(CFG.whep, vid, null, { onHttpError: mosaicDenied, onLive: resetRearm });
    GRIDP.start();
    bindCells(stage);
  }

  // Barra: página, contador, título, botones.
  function renderBar() {
    const pages = CFG.pages || 1, n = CFG.total || 0;
    pageLbl.textContent = `Página ${(CFG.page || 0) + 1} de ${pages}`;
    pager.style.display = pages > 1 ? '' : 'none';
    countEl.textContent = n + (n === 1 ? ' cámara' : ' cámaras');
    titleEl.textContent = document.body.dataset.site || 'CamWay';
    paintSeg();
  }

  // Doble clic / Intro en una celda → maximizar esa cámara (stream directo).
  function bindCells(stage) {
    const ovlEl = stage.querySelector('.dvr-ovl');
    if (!ovlEl) return;
    const open = pos => { const c = pos && (CFG.cells || [])[+pos.dataset.k]; if (c && c.whep) maximize(c); };
    ovlEl.addEventListener('dblclick', ev => open(ev.target.closest('.dvr-pos[data-k]')));
    ovlEl.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') { const pos = ev.target.closest('.dvr-pos[data-k]'); if (pos) { ev.preventDefault(); open(pos); } }
    });
  }

  // Punto de entrada: elige el modo.
  async function apply(layout, page) {
    if (busy) return; busy = true;
    try {
      resetRearm();
      if (mode === 'direct') await applyDirect(layout, page);
      else await applyMosaic(layout, page);
    } finally { busy = false; }
  }

  function maximize(c) {
    view.classList.add('maxed');
    view.querySelector('#dvrMaxName').textContent = c.title || '';
    // Pausa la rejilla mientras se ve una cámara ampliada: sus <video> quedan
    // OCULTOS y, si siguen recibiendo, el jitter buffer de WebRTC acumula
    // retraso → al volver se vería imagen vieja. Además evita el doble stream.
    pauseGrid();
    MAXP = makePlayer(c.whep_hi || c.whep, view.querySelector('#dvrMaxVideo'), view.querySelector('#dvrMaxState'));
    MAXP.start();
  }
  function unmaximize() {
    if (MAXP) { MAXP.stop(); MAXP = null; }
    try { view.querySelector('#dvrMaxVideo').srcObject = null; } catch (e) {}
    view.classList.remove('maxed');
    resumeGrid();     // reconecta desde cero → borde en vivo
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
    await apply(prefLayout(), 0);
  }
  function closeDvr() {
    clearInterval(clockTimer); clockTimer = null;
    resetRearm();
    unmaximize();
    stopGrid();
    // En modo mosaico NO se detiene el mosaico del servidor al cerrar: se deja
    // CALIENTE para que reabrir el DVR sea instantáneo. Se reconfigura solo al
    // cambiar layout/página.
    view.hidden = true;
    document.body.classList.remove('dvr-open');
    try { if (document.fullscreenElement) document.exitFullscreen(); } catch (e) {}
    syncWall();
  }

  async function autoStart() { if (await hasCams()) openDvr(); }
  window.CamWayDVR = { open: openDvr, close: closeDvr, autoStart };

  document.getElementById('btnDvr')?.addEventListener('click', openDvr);
  view.querySelector('#dvrClose').addEventListener('click', closeDvr);
  view.querySelector('#dvrPrev').addEventListener('click', () => { const p = ((CFG.page || 0) - 1 + (CFG.pages || 1)) % (CFG.pages || 1); apply(CFG.layout, p); });
  view.querySelector('#dvrNext').addEventListener('click', () => { const p = ((CFG.page || 0) + 1) % (CFG.pages || 1); apply(CFG.layout, p); });
  segBtns.forEach(b => b.addEventListener('click', () => {
    pref = b.dataset.layout;
    try { localStorage.setItem('camway.dvr.layout', pref); } catch (e) {}
    paintSeg();
    apply(prefLayout(), 0);
  }));
  // Cambio de modo (Tiempo real / Mosaico): se recuerda en el navegador.
  modeBtns.forEach(b => b.addEventListener('click', () => {
    mode = b.dataset.mode === 'mosaic' ? 'mosaic' : 'direct';
    try { localStorage.setItem('camway.dvr.mode', mode); } catch (e) {}
    paintSeg();
    apply(CFG.layout || prefLayout(), CFG.page || 0);
  }));
  view.querySelector('#dvrUnmax').addEventListener('click', unmaximize);
  view.querySelector('#dvrFull').addEventListener('click', () => {
    try { if (document.fullscreenElement) document.exitFullscreen(); else view.requestFullscreen(); } catch (e) {}
  });
  view.querySelector('#dvrMax').addEventListener('dblclick', unmaximize);
  document.addEventListener('keydown', e => {
    if (view.hidden) return;
    if (e.key === 'Escape') { view.classList.contains('maxed') ? unmaximize() : closeDvr(); }
    else if (e.key === 'ArrowRight') { const p = ((CFG.page || 0) + 1) % (CFG.pages || 1); apply(CFG.layout, p); }
    else if (e.key === 'ArrowLeft') { const p = ((CFG.page || 0) - 1 + (CFG.pages || 1)) % (CFG.pages || 1); apply(CFG.layout, p); }
  });
})();
