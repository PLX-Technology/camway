// CamWay — Modo TV (visor limpio, sin admin). Reproduce por WebRTC (TIEMPO REAL)
// donde el navegador lo soporta (Windows Chrome/Edge), y cae a HLS de respaldo
// donde no (Android TV de poca RAM). Navegación con control remoto / teclado.

const $ = (s) => document.querySelector(s);
const grid = $("#grid");
// Escapa texto de la API antes de meterlo en innerHTML (los nombres de cámara
// los puede escribir cualquier equipo de la LAN).
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
const MAX_TILES = 9;
const WEBRTC_OK = typeof RTCPeerConnection !== "undefined";
const HLS_CFG = { maxBufferLength: 6, maxMaxBufferLength: 10, backBufferLength: 0,
  liveSyncDurationCount: 3, enableWorker: true, lowLatencyMode: false };

let tiles = [];               // {title, whep, whep_hi, hls, hls_hi, el, video, ...}
let cols = 1;
let focus = 0;
let maxed = -1;

// ---- reproducción: WebRTC primero, HLS de respaldo ----
function gatherIce(pc) {
  return new Promise((res) => {
    if (pc.iceGatheringState === "complete") return res();
    const d = () => { if (pc.iceGatheringState === "complete") { pc.removeEventListener("icegatheringstatechange", d); res(); } };
    pc.addEventListener("icegatheringstatechange", d);
    setTimeout(res, 2500);
  });
}

function playTile(t, hi) {
  t.useHi = !!hi;
  stopTile(t);
  if (WEBRTC_OK && t.whep) startWebrtc(t);
  else startHls(t);
}

async function startWebrtc(t) {
  const url = t.useHi ? (t.whep_hi || t.whep) : t.whep;
  const pc = new RTCPeerConnection({ iceServers: [] });
  t.pc = pc;
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });
  pc.ontrack = (ev) => { if (t.pc !== pc) return; t.video.srcObject = ev.streams[0]; t.video.play().catch(() => {}); t.retry = 0; };
  pc.onconnectionstatechange = () => {
    if (t.pc !== pc) return;
    if (["failed", "disconnected", "closed"].includes(pc.connectionState)) retryTile(t);
  };
  try {
    await pc.setLocalDescription(await pc.createOffer());
    await gatherIce(pc);
    if (t.pc !== pc) return;
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: pc.localDescription.sdp });
    if (!res.ok) throw new Error("WHEP " + res.status);
    const loc = res.headers.get("Location");
    if (loc) { try { t.resource = new URL(loc, location.href).href; } catch (_) {} }
    const answer = await res.text();
    if (t.pc !== pc) return;
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
  } catch (e) { retryTile(t); }
}

function retryTile(t) {
  if (t.retryTimer) return;
  closePc(t);
  t.retry = (t.retry || 0) + 1;
  if (t.retry > 2 && (t.useHi ? t.hls_hi : t.hls)) { startHls(t); return; }   // 2 fallos → HLS
  t.retryTimer = setTimeout(() => { t.retryTimer = null; startWebrtc(t); }, Math.min(1000 * 2 ** (t.retry - 1), 8000));
}

function startHls(t) {
  const url = t.useHi ? (t.hls_hi || t.hls) : t.hls;
  if (!url) return;
  if (window.Hls && Hls.isSupported()) {
    const h = new Hls(HLS_CFG);
    h.on(Hls.Events.ERROR, (_e, d) => { if (d.fatal) { try { h.startLoad(); } catch (_) {} } });
    h.loadSource(url); h.attachMedia(t.video); t.hlsPlayer = h;
  } else if (t.video.canPlayType("application/vnd.apple.mpegurl")) {
    t.video.src = url;
  }
}

function closePc(t) {
  if (t.pc) { try { t.pc.close(); } catch (_) {} t.pc = null; }
  if (t.resource) { try { fetch(t.resource, { method: "DELETE", keepalive: true }).catch(() => {}); } catch (_) {} t.resource = null; }
}

function stopTile(t) {
  if (t.retryTimer) { clearTimeout(t.retryTimer); t.retryTimer = null; }
  closePc(t);
  if (t.hlsPlayer) { try { t.hlsPlayer.destroy(); } catch (_) {} t.hlsPlayer = null; }
  if (t.video) { try { t.video.srcObject = null; t.video.removeAttribute("src"); t.video.load(); } catch (_) {} }
}
function stop(t) { stopTile(t); }   // alias (compat)

// ---- construir la rejilla ----
function buildGrid(list) {
  grid.innerHTML = ""; tiles = [];
  cols = Math.ceil(Math.sqrt(list.length)) || 1;
  const rows = Math.ceil(list.length / cols);
  grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;
  list.forEach((tile, i) => {
    const el = document.createElement("div");
    el.className = "cell";
    el.innerHTML = `<video muted playsinline autoplay></video>
      <div class="st">Conectando…</div><div class="lbl">${esc(tile.title || "")}</div>`;
    grid.appendChild(el);
    const video = el.querySelector("video");
    video.addEventListener("playing", () => { el.querySelector(".st")?.remove(); });
    const t = { ...tile, el, video, player: null };
    tiles.push(t);
  });
  focus = Math.min(focus, tiles.length - 1); if (focus < 0) focus = 0;
  startGrid();
  setFocus(focus);
}

function startGrid() {            // reproduce el flujo LIGERO de cada tile
  grid.classList.remove("maxed");
  tiles.forEach((t) => { t.el.classList.remove("max"); playTile(t, false); });
}

function setFocus(i) {
  tiles.forEach((t, k) => t.el.classList.toggle("focus", k === i));
  focus = i;
}

// ---- maximizar / restaurar ----
function maximize(i) {
  // Apaga las demás y reproduce solo la elegida en ALTA (720p).
  tiles.forEach((t) => stopTile(t));
  maxed = i;
  grid.classList.add("maxed");
  const t = tiles[i];
  t.el.classList.add("max");
  const st = t.el.querySelector(".st"); if (st) st.textContent = "Conectando…";
  playTile(t, true);
}
function restore() {
  stopTile(tiles[maxed]);
  maxed = -1;
  buildGridFast();              // vuelve a la rejilla ligera
}
function buildGridFast() {       // re-arranca los flujos ligeros sin reconstruir el DOM
  grid.classList.remove("maxed");
  tiles.forEach((t) => { t.el.classList.remove("max");
    const st = t.el.querySelector(".st"); if (st && !st.isConnected) t.el.appendChild(st); });
  tiles.forEach((t) => playTile(t, false));
  setFocus(focus);
}

// ---- panel: menú y grabaciones (igual que el APK) ----
function panelOpen() { return $("#panel").classList.contains("show"); }
function recOpen() { return $("#rec").classList.contains("show"); }

function openPanel(title, items, onBack) {
  // items: [{label, muted?, onSelect?}]
  const p = $("#panel");
  p.innerHTML = `<div class="pbox"><h2>${esc(title)}</h2><div class="plist"></div></div>`;
  const list = p.querySelector(".plist");
  const btns = [];
  items.forEach((it) => {
    if (it.muted) { const d = document.createElement("div"); d.className = "pmuted"; d.textContent = it.label; list.appendChild(d); return; }
    const b = document.createElement("button");
    b.className = "pitem"; b.textContent = it.label; b.tabIndex = -1;
    b.addEventListener("click", () => it.onSelect && it.onSelect());
    list.appendChild(b); btns.push(b);
  });
  p._btns = btns; p._onBack = onBack || closePanel; p._focus = 0;
  p.classList.add("show");
  focusPanelItem(0);
}
function focusPanelItem(i) {
  const p = $("#panel"), btns = p._btns || [];
  if (!btns.length) return;
  i = Math.max(0, Math.min(i, btns.length - 1));
  btns.forEach((b, k) => b.classList.toggle("focus", k === i));
  btns[i].scrollIntoView({ block: "nearest" });
  p._focus = i;
}
function panelMove(d) { focusPanelItem(($("#panel")._focus || 0) + d); }
function panelSelect() { const p = $("#panel"), b = (p._btns || [])[p._focus || 0]; if (b) b.click(); }
function panelBack() { const fn = $("#panel")._onBack; if (fn) fn(); }
function closePanel() { const p = $("#panel"); p.classList.remove("show"); p.innerHTML = ""; p._btns = []; }

function showMenu() {
  if (maxed >= 0) restore();
  stopRec();
  openPanel("Menú", [
    { label: "▶  En vivo", onSelect: closePanel },
    { label: "⏺  Grabaciones", onSelect: showRecCameras },
  ], closePanel);
}
function showRecCameras() {
  if (!tiles.length) { flashHint("No hay cámaras"); return; }
  const items = tiles.map((t) => ({ label: t.title || "cámara", onSelect: () => showRecClips(t) }));
  openPanel("Grabaciones — elige cámara", items, showMenu);
}
async function showRecClips(t) {
  openPanel("Grabaciones — " + (t.title || ""), [{ label: "Cargando…", muted: true }], showRecCameras);
  let recs = [];
  try {
    const r = await fetch("/api/recordings/" + t.device_id).then((x) => x.json());
    recs = (r.recordings || []).slice(0, 200);
  } catch (_) { return openPanel("Grabaciones — " + (t.title || ""), [{ label: "No se pudo listar", muted: true }], showRecCameras); }
  if (!recs.length) return openPanel("Grabaciones — " + (t.title || ""), [{ label: "(sin grabaciones)", muted: true }], showRecCameras);
  const items = recs.map((rec) => ({ label: fmtRec(rec), onSelect: () => playRecording(t, rec) }));
  openPanel("Grabaciones — " + (t.title || ""), items, showRecCameras);
}
function fmtRec(r) {
  const d = new Date((r.mtime || 0) * 1000), z = (n) => String(n).padStart(2, "0");
  const when = `${z(d.getDate())}/${z(d.getMonth() + 1)} ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
  const tag = r.kind === "event" ? "  🔔 evento" : "";
  return `${when}${tag}   ·  ${Math.round((r.size || 0) / 1048576)} MB`;
}
function playRecording(t, r) {
  closePanel();
  tiles.forEach((x) => stopTile(x));          // libera los flujos en vivo
  const rec = $("#rec"), v = rec.querySelector("video");
  const url = `/api/recordings/${t.device_id}/file?stream=${encodeURIComponent(r.stream)}`
    + `&name=${encodeURIComponent(r.name)}&kind=${encodeURIComponent(r.kind || "cont")}`;
  rec.querySelector(".rlbl").textContent = (t.title || "") + " — " + fmtRec(r);
  v.src = url; rec.classList.add("show"); v.play().catch(() => {});
}
function stopRec() {
  const rec = $("#rec"); if (!rec.classList.contains("show")) return;
  const v = rec.querySelector("video");
  try { v.pause(); v.removeAttribute("src"); v.load(); } catch (_) {}
  rec.classList.remove("show");
  if (maxed < 0) tiles.forEach((x) => playTile(x, false));   // reanuda el mosaico
}

// ---- control remoto ----
function move(dc, dr) {
  let r = Math.floor(focus / cols), c = focus % cols;
  c += dc; r += dr;
  const rows = Math.ceil(tiles.length / cols);
  if (c < 0) c = 0; if (c >= cols) c = cols - 1;
  if (r < 0) r = 0; if (r >= rows) r = rows - 1;
  let idx = r * cols + c;
  if (idx >= tiles.length) idx = tiles.length - 1;
  setFocus(idx);
}
let hintTimer = null;
function flashHint(msg) {
  const h = $("#hint"); h.textContent = msg; h.classList.add("show");
  clearTimeout(hintTimer); hintTimer = setTimeout(() => h.classList.remove("show"), 2500);
}
const BACK = (k) => k === 8 || k === 27 || k === 461 || k === 10009;
document.addEventListener("keydown", (e) => {
  if ($("#login").classList.contains("show")) return;    // el login maneja su teclado
  const k = e.keyCode;
  // Reproduciendo una grabación: Atrás vuelve al mosaico.
  if (recOpen()) {
    if (BACK(k)) { e.preventDefault(); stopRec(); }
    else if (k === 13) { e.preventDefault(); const v = $("#rec video"); if (v.paused) v.play(); else v.pause(); }
    return;
  }
  // Panel (menú / grabaciones) abierto.
  if (panelOpen()) {
    if (k === 38) { e.preventDefault(); panelMove(-1); }
    else if (k === 40) { e.preventDefault(); panelMove(1); }
    else if (k === 13 || k === 39) { e.preventDefault(); panelSelect(); }
    else if (BACK(k) || k === 37) { e.preventDefault(); panelBack(); }
    return;
  }
  // Cámara maximizada.
  if (maxed >= 0) {
    if (k === 13 || BACK(k)) { e.preventDefault(); restore(); }
    return;
  }
  // Mosaico.
  if (k === 37) { e.preventDefault(); move(-1, 0); }
  else if (k === 39) { e.preventDefault(); move(1, 0); }
  else if (k === 38) { e.preventDefault(); move(0, -1); }
  else if (k === 40) { e.preventDefault(); move(0, 1); }
  else if (k === 13 && tiles.length) { e.preventDefault(); maximize(focus); }
  else if (BACK(k)) { e.preventDefault(); showMenu(); }     // Atrás en el mosaico → menú
});

// ---- carga + sesión ----
async function loadWall() {
  let data;
  try { data = await fetch("/api/wall").then((r) => r.json()); }
  catch (_) { return flashHint("No se pudo conectar con CamWay"); }
  let list = (data.tiles || []);
  if (list.length > MAX_TILES) { list = list.slice(0, MAX_TILES); flashHint(`Mostrando ${MAX_TILES} de ${data.tiles.length} cámaras`); }
  if (!list.length) { $("#empty").style.display = "flex"; return; }
  $("#empty").style.display = "none";
  buildGrid(list);
  flashHint("◀ ▲ ▼ ▶ mover · OK maximizar · Atrás menú/grabaciones");
}

async function boot() {
  let me;
  try { me = await fetch("/api/me").then((r) => r.json()); } catch (_) { me = {}; }
  if (me.authenticated) { loadWall(); }
  else { showLogin(); }
}

function showLogin() {
  $("#login").classList.add("show");
  setTimeout(() => $("#u").focus(), 100);
}
async function doLogin() {
  const u = $("#u").value.trim(), p = $("#p").value;
  if (!u || !p) return;
  $("#err").textContent = "Entrando…";
  try {
    const r = await fetch("/api/login", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p }) });
    if (!r.ok) { $("#err").textContent = "Usuario o contraseña incorrectos"; return; }
    $("#login").classList.remove("show");
    loadWall();
  } catch (_) { $("#err").textContent = "Error de conexión"; }
}
$("#menuBtn").addEventListener("click", () => {
  if (recOpen()) stopRec(); else if (panelOpen()) panelBack(); else if (maxed >= 0) restore(); else showMenu();
});
$("#go").addEventListener("click", doLogin);
$("#p").addEventListener("keydown", (e) => { if (e.keyCode === 13) doLogin(); });
$("#u").addEventListener("keydown", (e) => { if (e.keyCode === 13) $("#p").focus(); });

// Enganches para el cliente Android TV (el botón Atrás del control lo gestiona
// la app nativa y consulta estos métodos).
window.tvIsMaxed = () => maxed >= 0;
window.tvRestore = () => { if (maxed >= 0) restore(); };

boot();
