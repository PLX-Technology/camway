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
  let watchTimer = null;
  let busy = false;

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
    constructor(whep, video, stateEl) {
      this.whep = whep; this.video = video; this.stateEl = stateEl;
      this.pc = null; this.loc = null; this.retry = 0; this.dead = false;
      this.lastTime = 0; this.lastAdvance = Date.now(); this.t = null;
    }
    setState(txt) {
      if (!this.stateEl) return;
      this.stateEl.textContent = txt || '';
      this.stateEl.style.display = txt ? '' : 'none';
    }
    async start() {
      this._teardown();
      if (this.dead || !this.whep) { this.setState('sin video'); return; }
      this.setState('conectando…');
      try {
        const pc = new RTCPeerConnection({ iceServers: [] });
        this.pc = pc;
        pc.addTransceiver('video', { direction: 'recvonly' });
        pc.ontrack = e => { if (this.video.srcObject !== e.streams[0]) this.video.srcObject = e.streams[0]; };
        pc.onconnectionstatechange = () => { if (pc === this.pc && pc.connectionState === 'failed') this._reconnect(); };
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        await iceDone(pc);
        const res = await fetch(this.whep, { method: 'POST', headers: { 'Content-Type': 'application/sdp' }, body: pc.localDescription.sdp });
        if (!res.ok) throw new Error('whep ' + res.status);
        const ans = await res.text();
        this.loc = res.headers.get('Location');
        await pc.setRemoteDescription({ type: 'answer', sdp: ans });
        this.retry = 0; this.lastAdvance = Date.now();
        const clear = () => this.setState('');
        this.video.addEventListener('playing', clear, { once: true });
        setTimeout(() => { if (this.pc === pc) clear(); }, 4500);
      } catch (e) { this._reconnect(); }
    }
    _reconnect() {
      if (this.dead) return;
      this.retry += 1;
      let delay;
      // El mosaico del servidor tarda unos segundos en montar: reintentar RÁPIDO
      // al inicio para engancharlo apenas esté listo (no backoff largo de entrada).
      if (this.retry <= 12) { delay = 500; this.setState('conectando…'); }
      else if (this.retry <= 20) { delay = 2000; this.setState('conectando…'); }
      else { delay = 8000; this.setState('sin señal'); }
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
    watch() {
      if (this.dead || !this.pc) return;
      const t = this.video.currentTime;
      if (t > this.lastTime + 0.02) { this.lastTime = t; this.lastAdvance = Date.now(); return; }
      const stalled = Date.now() - this.lastAdvance;
      if (stalled > 12000 && stalled <= 20000) { try { this.video.play().catch(() => {}); } catch (e) {} }
      else if (stalled > 20000) { this.lastAdvance = Date.now(); this._reconnect(); }
    }
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
    GRIDP = new Player(CFG.whep, vid, null);
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
    if (GRIDP) GRIDP._teardown();
    MAXP = new Player(c.whep, view.querySelector('#dvrMaxVideo'), view.querySelector('#dvrMaxState'));
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

  function pauseWall(p) { try { document.getElementById(p ? 'btnWallStop' : 'btnWallStart')?.click(); } catch (e) {} }

  async function hasCams() {
    try { const d = await (await fetch('/api/wall', { cache: 'no-store' })).json(); return (d.tiles || []).some(t => t.whep); }
    catch (e) { return false; }
  }

  async function openDvr() {
    pauseWall(true);
    view.hidden = false;
    document.body.classList.add('dvr-open');
    await applyMosaic(CFG.layout || 4, 0);
    clearInterval(watchTimer);
    watchTimer = setInterval(() => { if (GRIDP) GRIDP.watch(); if (MAXP) MAXP.watch(); }, 1000);
  }
  function closeDvr() {
    unmaximize();
    if (GRIDP) { GRIDP.stop(); GRIDP = null; }
    clearInterval(watchTimer); watchTimer = null;
    // NO se detiene el mosaico del servidor al cerrar: se deja CALIENTE para que
    // reabrir el DVR sea instantáneo (estación de monitoreo). Se reconfigura solo
    // al cambiar layout/página.
    view.hidden = true;
    document.body.classList.remove('dvr-open');
    try { if (document.fullscreenElement) document.exitFullscreen(); } catch (e) {}
    pauseWall(false);
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
