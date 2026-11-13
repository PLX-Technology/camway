/* CamWay — interfaz de inventario y mosaico */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// Ritmo del mosaico y modo ligero (declarados arriba: se usan en loadWall y en
// el botón, que corren antes de la mitad del archivo).
let snapMs = 700;       // BASE: ms entre fotogramas según el fps del servidor
let lightMode = false;  // forzar el mínimo (override manual del gobernador)
try { lightMode = localStorage.getItem("camway.light") === "1"; } catch (_) {}
const LIGHT_W = 320;    // ancho del fotograma en modo ligero

// Gobernador de calidad adaptativa del muro: mide si el navegador/red van al
// día (saltos por petición aún en vuelo + latencia de carga) y sube/baja solo
// el nivel = tamaño de fotograma + fps. Con histéresis para no oscilar.
// lightMode manual = forzar el nivel mínimo. Testeable ya en la oficina; para
// los taladros el borde avisará además su uplink disponible (fase 2).
let autoQ = true;
try { autoQ = localStorage.getItem("camway.autoq") !== "0"; } catch (_) {}
const Q_LEVELS = [
  { w: 0,   mul: 1.0, name: "alta" },    // pleno (~480px, fps pleno)
  { w: 320, mul: 1.0, name: "media" },   // fotograma chico
  { w: 320, mul: 1.8, name: "media-" },  // chico + menos fps
  { w: 220, mul: 2.6, name: "baja" },    // mínimo
];
let qLevel = 0, qGood = 0, qBad = 0;
let qAtt = 0, qSkip = 0, qLatSum = 0, qLatN = 0;   // métricas de la ventana actual
let qTimer = null;

function qEffWidth() { return lightMode ? LIGHT_W : Q_LEVELS[qLevel].w; }
function qEffMs() {
  return Math.max(120, Math.round(snapMs * (lightMode ? 1.0 : Q_LEVELS[qLevel].mul)));
}
let lastJpegLat = 0;      // latencia media del último ciclo (proxy de banda)

// Auto-vivo por actividad (opción B): el mosaico es fotogramas, pero una cámara
// con DETECCIÓN reciente sube a vídeo WebRTC si el presupuesto de recursos lo
// permite (AIMD: sube de a una si va sano, baja a la mitad al primer problema).
// Default APAGADO (opt-in) porque el WebRTC no se puede probar fuera de Chrome.
let autoLive = false;
try { autoLive = localStorage.getItem("camway.autolive") === "1"; } catch (_) {}
let liveBudget = 0;               // cuántas tejas vivas permite el presupuesto
let liveCooldown = 0;             // ciclos de espera tras una degradación
let autoLiveSet = new Set();      // stream_ids promovidos automáticamente
const LIVE_CAP = 6;               // tope duro de vivos a la vez (evita el lag)
const ACTIVITY_MS = 8000;         // teja "activa" si tuvo detección en este tiempo

// Modo del mosaico: "frames" (fotogramas, ligero) o "live" (vídeo WebRTC fluido
// en TODAS las tejas, para pocas cámaras). El usuario lo elige (se recuerda);
// si no eligió, se usa el default del servidor o "auto" (según nº de cámaras).
const LIVE_MOSAIC_MAX = 6;        // en "auto", hasta esto → vídeo en vivo
let mosaicChoice = null;          // elección explícita del usuario ("frames"|"live")
try { mosaicChoice = localStorage.getItem("camway.mosaicmode") || null; } catch (_) {}
let liveMosaic = false;           // modo efectivo resuelto en loadWall
const _lastVQ = new Map();        // stream_id -> {d,t} de droppedVideoFrames

// Modo "solo una zona": al abrir /?zone=<id> (botón ⧉ de una zona) la ventana
// muestra el mosaico de SOLO esa zona, para dejarlo en una pantalla dedicada
// (p.ej. el puesto del guardia). null = vista normal.
const ZONE_ONLY = new URLSearchParams(location.search).get("zone");

const state = {
  devices: new Map(),   // id -> equipo
  sse: null,
  players: new Map(),   // stream_id -> WhepPlayer
  tiles: new Map(),     // stream_id -> controlador de teja (video + IA)
  detSse: null,         // SSE de detecciones del servidor
  serverDetection: false, // el detector server-side está activo
  fitMode: false,       // modo "ajustar a pantalla" (video wall)
  logo: "",             // logo (data URL) para el encabezado del video wall
};

// --------------------------------------------------------------- utilidades

// Escapa texto para interpolarlo en plantillas de innerHTML (contenido y
// atributos entre comillas). TODO dato que venga de la API —nombres, banners
// RTSP, errores, rutas, usuarios— pasa por aquí: los banners los escribe
// cualquier equipo de la LAN. Para texto suelto, mejor textContent.
function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Llamada JSON a la API. Corta a los API_TIMEOUT_MS (o options.timeout; 0 =
// sin límite) para que una petición colgada no deje la interfaz esperando.
const API_TIMEOUT_MS = 30000;
async function api(path, options = {}) {
  const { timeout = API_TIMEOUT_MS, ...opts } = options;
  const ctrl = timeout > 0 && !opts.signal ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeout) : null;
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...(ctrl ? { signal: ctrl.signal } : {}),
      ...opts,
    });
  } catch (e) {
    if (e.name === "AbortError" && ctrl && ctrl.signal.aborted) {
      throw new Error("El servidor no respondió a tiempo");
    }
    throw e;
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (res.status === 401 && !path.endsWith("/api/me")) {
    showAuth("login");                 // sesión expirada: pedir login de nuevo
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("err", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

function dims(s) {
  return s.width && s.height ? `${s.width}x${s.height}` : "resolución desconocida";
}

// ----------------------------------------------------- talkback (hablar) ---
// Captura el micrófono, lo codifica a G.711 A-law (8 kHz) en un AudioWorklet y
// lo manda por WebSocket al servidor, que lo empuja al altavoz de la cámara por
// el backchannel ONVIF. Mantener pulsado = hablar (half-duplex). Una sola vez.
// OJO: getUserMedia exige contexto seguro → funciona por https (el túnel
// camway.pacific-logging.com) o en localhost, NO por http LAN plano.
let talk = { active: false };

async function startTalk(streamId, btn) {
  if (talk.active) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("El micrófono necesita https (abre CamWay por su dirección segura)", true);
    return;
  }
  talk = { active: true, btn };
  if (btn) btn.classList.add("talking");
  try {
    const ctx = new AudioContext({ sampleRate: 8000 });
    talk.ctx = ctx;
    await ctx.audioWorklet.addModule("/static/talk-worklet.js?v=1");
    const mic = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    if (!talk.active) { mic.getTracks().forEach((t) => t.stop()); return; }  // soltó antes
    talk.mic = mic;
    const src = ctx.createMediaStreamSource(mic);
    const node = new AudioWorkletNode(ctx, "talk-processor");
    talk.node = node;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/talk/${streamId}`);
    ws.binaryType = "arraybuffer";
    talk.ws = ws;
    node.port.onmessage = (e) => { if (ws.readyState === 1) ws.send(e.data); };
    ws.onmessage = (e) => {
      try { const m = JSON.parse(e.data); if (m.error) { toast("Hablar: " + m.error, true); stopTalk(); } }
      catch { /* no-json */ }
    };
    ws.onclose = () => { if (talk.active) stopTalk(); };
    ws.onerror = () => toast("No se pudo abrir el canal de voz", true);
    src.connect(node);
    const sink = ctx.createGain(); sink.gain.value = 0;   // el nodo necesita destino
    node.connect(sink).connect(ctx.destination);
    if (ctx.state === "suspended") ctx.resume();
  } catch (err) {
    toast("Micrófono: " + (err && err.message || err), true);
    stopTalk();
  }
}

function stopTalk() {
  const t = talk;
  talk = { active: false };
  if (t.btn) t.btn.classList.remove("talking");
  try { t.node && t.node.disconnect(); } catch { /* */ }
  try { t.mic && t.mic.getTracks().forEach((x) => x.stop()); } catch { /* */ }
  try { t.ws && t.ws.close(); } catch { /* */ }
  try { t.ctx && t.ctx.close(); } catch { /* */ }
}

// --------------------------------------------------------------- navegación

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".view").forEach((v) => v.classList.remove("active"));
    tab.classList.add("active");
    $(`#view-${tab.dataset.view}`).classList.add("active");
    if (tab.dataset.view === "mosaico") enterWallView();
    if (tab.dataset.view === "grabaciones") loadRecDevices();
    if (tab.dataset.view === "ajustes") {
      if (isAdmin()) { loadUsers(); fillKiosk(); loadEncoding(); loadHealth(); }
      loadAlertPanel();
    }
    // Fuera de la pestaña Mosaico el muro se detiene (sin fotogramas ni WebRTC).
    if (tab.dataset.view !== "mosaico") syncWall();
  });
});

// --------------------------------------------------------------- salud

async function pollHealth() {
  try {
    const h = await api("/api/health");
    $("#dotApi").className = "dot on";
    $("#dotMtx").className = "dot " + (h.mediamtx ? "on" : "off");
    $("#tagDevices").textContent =
      h.devices === 1 ? "1 equipo" : `${h.devices} equipos`;
  } catch (_) {
    $("#dotApi").className = "dot off";
    $("#dotMtx").className = "dot off";
  }
}

// --------------------------------------------------------------- escaneo

function setScanning(on) {
  $("#btnScan").disabled = on;
  $("#btnStop").disabled = !on;
  $("#progressBox").hidden = !on;
}

$("#btnScan").addEventListener("click", async () => {
  const cidr = $("#cidr").value.trim();
  if (!cidr) return toast("Indica el segmento a escanear", true);

  try {
    const r = await api("/api/scan", {
      method: "POST", timeout: 0,
      body: JSON.stringify({ cidr, channels: Number($("#channels").value) || 8 }),
    });
    setScanning(true);
    $("#progStage").textContent = "iniciando";
    $("#progFill").style.width = "0%";
    toast(`Escaneando ${r.hosts} direcciones en ${r.ports.length} puertos`);
    openEvents();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#btnOnvifScan").addEventListener("click", async () => {
  const cidr = $("#cidr").value.trim();
  if (!cidr) return toast("Indica el segmento a escanear", true);
  try {
    const r = await api("/api/onvif-scan", {
      method: "POST", timeout: 0, body: JSON.stringify({ cidr }),
    });
    setScanning(true);
    $("#progStage").textContent = "ONVIF: buscando en :8080/:80";
    $("#progFill").style.width = "0%";
    toast(`Descubriendo por ONVIF en ${r.hosts} direcciones`);
    openEvents();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#btnStop").addEventListener("click", async () => {
  await api("/api/scan/stop", { method: "POST" }).catch(() => {});
  setScanning(false);
});

$("#btnClearAll").addEventListener("click", async () => {
  if (state.devices.size === 0) return;
  if (!(await uiConfirm(`¿Borrar los ${state.devices.size} equipos del inventario? No se puede deshacer; las grabaciones ya guardadas no se tocan.`,
        { okLabel: "Borrar todo", danger: true }))) return;
  try {
    const r = await api("/api/devices", { method: "DELETE" });
    toast(`${r.removed} equipos borrados`);
    await loadDevices();
    await pollHealth();
  } catch (err) {
    toast(err.message, true);
  }
});

function openEvents() {
  if (state.sse) state.sse.close();
  const sse = new EventSource("/api/scan/events");
  state.sse = sse;

  sse.onmessage = (ev) => {
    let e;
    try { e = JSON.parse(ev.data); } catch (_) { return; }   // evento ilegible: se ignora

    if (e.stage === "error") {
      toast(e.message, true);
      setScanning(false);
      sse.close();
      return;
    }
    if (e.stage === "fin" || e.stage === "cancelado") {
      $("#progStage").textContent = e.message || "terminado";
      $("#progFill").style.width = "100%";
      setScanning(false);
      sse.close();
      // Las tarjetas que llegaron por SSE no traen el id de cada stream, que
      // solo existe una vez guardado. Al recargar aparecen los botones de
      // publicar.
      loadDevices().then(pollHealth);
      return;
    }

    const pct = e.total ? Math.round((e.done / e.total) * 100) : 0;
    $("#progFill").style.width = pct + "%";
    $("#progStage").textContent = {
      "barrido": "Buscando puertos RTSP abiertos",
      "huella": "Interrogando equipos",
      "onvif-barrido": "Buscando ONVIF (:8080/:80)",
      "onvif": "Pidiendo la ruta RTSP por ONVIF",
      "onvif-listo": "ONVIF listo",
    }[e.stage] || e.stage;
    $("#progCount").textContent = e.total
      ? `${e.done} / ${e.total}${e.message ? " · " + e.message : ""}`
      : e.message || "";

    if (e.device && e.device.streams.length) {
      upsertDeviceCard(e.device, true);
    }
  };

  // Si se corta la conexión a media exploración, no dejar la UI «escaneando».
  sse.onerror = () => {
    sse.close();
    if (state.sse === sse) state.sse = null;
    setScanning(false);
    toast("Se perdió la conexión con el escaneo; revisa el inventario o vuelve a lanzarlo", true);
  };
}

// --------------------------------------------------------------- equipos

async function loadDevices() {
  const { devices } = await api("/api/devices");
  state.devices.clear();
  $("#deviceList").innerHTML = "";
  devices.forEach((d) => upsertDeviceCard(d, false));
  updateEmpty();
}

function updateEmpty() {
  $("#emptyDevices").hidden = state.devices.size > 0;
}

function upsertDeviceCard(dev, isNew) {
  state.devices.set(dev.id, dev);
  const existing = document.getElementById(`dev-${dev.id}`);
  const card = renderDevice(dev, isNew && !existing);
  if (existing) existing.replaceWith(card);
  else $("#deviceList").prepend(card);
  updateEmpty();
}

function renderDevice(dev, animate) {
  const card = document.createElement("article");
  card.className = "card" + (animate ? " nuevo" : "");
  card.id = `dev-${dev.id}`;

  const badges = [];
  if (dev.vendor && dev.vendor !== "desconocido")
    badges.push(`<span class="badge vendor">${esc(dev.vendor)}</span>`);
  if (dev.kind === "nvr")
    badges.push(`<span class="badge nvr">DVR/NVR · ${dev.streams.length} canales</span>`);
  badges.push(dev.needs_auth
    ? `<span class="badge auth">pide clave</span>`
    : `<span class="badge libre">sin autenticación</span>`);
  if (dev.permissive)
    badges.push(`<span class="badge" title="Sirve el mismo video para cualquier ruta">rutas permisivas</span>`);

  card.innerHTML = `
    <div class="cardhead">
      <div class="cardtitle">
        <span class="host">${esc(dev.host)}:${esc(dev.port)}</span>
        <span class="name" data-name>${esc(dev.name)}</span>
        <button class="ghost small btnpush" data-act="rename">Nombrar</button>
        <button class="ghost small" data-act="osd">OSD</button>
        <button class="ghost small" data-act="forget">Olvidar</button>
      </div>
      <div class="cardsub">${dev.server ? esc(dev.server) : "sin banner"}</div>
      <div class="badges">${badges.join("")}</div>
    </div>
    <ul class="streams">
      ${dev.streams.map((s) => renderStream(dev, s)).join("")}
    </ul>
    ${dev.error ? `<div class="carderr">${esc(dev.error)}</div>` : ""}
    ${(dev.needs_auth && dev.streams.length === 0) ? `
      <div class="authform">
        <div class="authrow">
          <input type="text" data-auth-user placeholder="usuario" spellcheck="false" autocomplete="off" value="admin">
          <input type="password" data-auth-pass placeholder="clave" autocomplete="new-password">
          <button class="primary small" data-auth-probe>Probar</button>
        </div>
        <label class="authsave"><input type="checkbox" data-auth-save checked> recordar esta credencial</label>
      </div>` : ""}
  `;

  card.querySelector('[data-act="rename"]').addEventListener("click", async () => {
    const name = await uiPrompt("Nombre para este equipo", { value: dev.name || "", okLabel: "Guardar" });
    if (name === null) return;
    try {
      await api(`/api/devices/${dev.id}`, {
        method: "PATCH", body: JSON.stringify({ name }),
      });
      dev.name = name;
      card.querySelector("[data-name]").textContent = name;
    } catch (err) { toast("No se pudo renombrar: " + err.message, true); }
  });

  card.querySelector('[data-act="osd"]').addEventListener("click", () => openOsdModal(dev));

  card.querySelector('[data-act="forget"]').addEventListener("click", async () => {
    if (!(await uiConfirm(`¿Quitar ${dev.host} del inventario?`, { okLabel: "Quitar", danger: true }))) return;
    try {
      await api(`/api/devices/${dev.id}`, { method: "DELETE" });
      state.devices.delete(dev.id);
      card.remove();
      updateEmpty();
      pollHealth();
    } catch (err) { toast("No se pudo quitar el equipo: " + err.message, true); }
  });

  card.querySelectorAll("[data-publish]").forEach((btn) => {
    btn.addEventListener("click", () => togglePublish(btn, dev));
  });

  card.querySelectorAll("[data-snap]").forEach((img) => {
    img.addEventListener("click", () => loadSnapshot(img));
    if (img.dataset.auto === "1") loadSnapshot(img);
  });

  const probeBtn = card.querySelector("[data-auth-probe]");
  if (probeBtn) {
    const submit = async () => {
      const username = card.querySelector("[data-auth-user]").value.trim();
      const password = card.querySelector("[data-auth-pass]").value;
      const save = card.querySelector("[data-auth-save]").checked;
      if (!username) return toast("Escribe el usuario de la cámara", true);
      probeBtn.disabled = true;
      probeBtn.textContent = "Probando…";
      try {
        const updated = await api(`/api/devices/${dev.id}/credential`, {
          method: "POST",
          body: JSON.stringify({ username, password, save }),
        });
        if (updated.recovered) {
          toast(`${dev.host}: ${updated.streams.length} stream(s) — credencial correcta`);
          upsertDeviceCard(updated, false);
          pollHealth();
        } else {
          toast("Esa credencial no funcionó en esta cámara", true);
          probeBtn.disabled = false;
          probeBtn.textContent = "Probar";
        }
      } catch (err) {
        toast(err.message, true);
        probeBtn.disabled = false;
        probeBtn.textContent = "Probar";
      }
    };
    probeBtn.addEventListener("click", submit);
    card.querySelector("[data-auth-pass]").addEventListener("keydown", (e) => {
      if (e.key === "Enter") submit();
    });
  }

  return card;
}

function renderStream(dev, s) {
  // El id real solo existe cuando el equipo ya está guardado; durante el
  // escaneo las tarjetas llegan sin él y no se puede publicar todavía.
  const sid = s.id || "";
  const codecClass = s.webrtc_ready ? "h264" : "otro";
  const publishable = Boolean(sid);
  const pub = s.published;

  return `
    <li class="streamrow">
      ${sid
        ? `<img class="thumb" data-snap data-sid="${esc(sid)}" data-auto="0"
                alt="miniatura" title="Pulsa para capturar un fotograma"
                src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E">`
        : `<div class="thumb vacia">sin guardar</div>`}
      <div class="streaminfo">
        <div class="top">
          <span class="path" title="${esc(s.url)}">${esc(s.path)}</span>
          <span class="chip ${codecClass}">${esc(s.codec)}</span>
          ${s.audio_codec ? `<span class="chip">audio</span>` : ""}
        </div>
        <div class="meta">
          ${esc(dims(s))}${s.profile ? " · " + esc(s.profile) : ""} · ${esc(s.label)}
          ${s.webrtc_ready ? "" : " · necesita transcodificar para WebRTC"}
        </div>
      </div>
      ${publishable
        ? `<button class="${pub ? "ghost" : "primary"} small"
                   data-publish data-sid="${esc(sid)}" data-pub="${pub ? 1 : 0}">
             ${pub ? "Quitar" : "Publicar"}
           </button>`
        : ""}
    </li>`;
}

async function togglePublish(btn, dev) {
  const sid = btn.dataset.sid;
  const isPub = btn.dataset.pub === "1";
  btn.disabled = true;
  try {
    if (isPub) {
      await api(`/api/streams/${sid}/publish`, { method: "DELETE" });
      btn.textContent = "Publicar";
      btn.className = "primary small";
      btn.dataset.pub = "0";
      toast("Stream retirado del mosaico");
    } else {
      const r = await api(`/api/streams/${sid}/publish`, {
        method: "POST", body: JSON.stringify({}),
      });
      btn.textContent = "Quitar";
      btn.className = "ghost small";
      btn.dataset.pub = "1";
      toast(r.transcode
        ? "Publicado con transcodificación (consume CPU)"
        : "Publicado; H.264 pasa tal cual");
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

function loadSnapshot(img) {
  img.src = `/api/streams/${img.dataset.sid}/snapshot?width=240&t=${Date.now()}`;
  img.onerror = () => {
    img.replaceWith(Object.assign(document.createElement("div"), {
      className: "thumb vacia", textContent: "sin imagen",
    }));
  };
}

// --------------------------------------------------------------- equipo suelto

$("#btnProbe").addEventListener("click", async () => {
  const host = $("#oneHost").value.trim();
  if (!host) return toast("Indica una dirección", true);
  const btn = $("#btnProbe");
  btn.disabled = true;
  btn.textContent = "Interrogando…";
  try {
    const dev = await api("/api/probe", {
      method: "POST", timeout: 0,
      body: JSON.stringify({
        host, port: Number($("#onePort").value) || 554,
        username: $("#oneUser").value.trim(),
        password: $("#onePass").value,
        save: true,        // recuerda la credencial que funcionó para futuros escaneos
      }),
    });
    if (!dev.streams.length) {
      toast(dev.error || "No entrega ningún stream", true);
    } else {
      await loadDevices();
      toast(`${dev.vendor}: ${dev.streams.length} stream(s)`);
      pollHealth();
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Interrogar";
  }
});

$("#btnOnvif").addEventListener("click", async () => {
  const btn = $("#btnOnvif");
  btn.disabled = true;
  btn.textContent = "Buscando…";
  try {
    const r = await api("/api/onvif", { timeout: 0 });
    $("#onvifHint").textContent = r.devices.length
      ? r.devices.map((d) => `${d.address} ${d.name || d.hardware || ""}`).join(" · ")
      : "Nadie respondió al multicast. " + r.nota + ". Usa el barrido del segmento.";
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Descubrir por ONVIF";
  }
});

// --------------------------------------------------------------- sonido de alarma

let audioCtx = null;

// El navegador exige un gesto del usuario para poder sonar: se desbloquea con
// el primer clic/tecla y se deja listo para las alarmas.
function unlockAudio() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch (_) {}
}
document.addEventListener("click", unlockAudio);
document.addEventListener("keydown", unlockAudio);

// Duración total de la sirena de alarma, en segundos (ajustable).
const ALARM_DURATION = 3.5;

// Sirena de dos tonos alternos, generada al vuelo, repetida hasta cubrir
// ALARM_DURATION.
function playAlarm() {
  if (!audioCtx || audioCtx.state !== "running") return;
  const t0 = audioCtx.currentTime;
  const cycle = 0.3;                 // duración de cada pitido + silencio
  const beeps = Math.ceil(ALARM_DURATION / cycle);
  for (let i = 0; i < beeps; i++) {
    const dt = i * cycle;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "square";
    osc.frequency.value = i % 2 ? 1100 : 880;
    gain.gain.setValueAtTime(0.0001, t0 + dt);
    gain.gain.exponentialRampToValueAtTime(0.3, t0 + dt + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.22);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(t0 + dt);
    osc.stop(t0 + dt + 0.24);
  }
}

// --------------------------------------------------------------- mosaico WHEP

// El reproductor WebRTC es la clase compartida de whep.js (la usan también dvr.js,
// live.js y tv.js). Aquí solo se fijan las opciones del mosaico y los textos.
const WALL_WHEP_OPTS = {
  watchdog: { everyMs: 2000, stallMs: 6000, minStep: 0.05 },   // ~6 s congelado => reconecta
  retryDelay: WhepPlayer.backoff(10000),                       // 1 s, 2 s, 4 s … techo 10 s
};

function whepStateText(state, n) {
  switch (state) {
    case "connecting": return "conectando";
    case "reconnecting": return `reconectando (${n})`;
    case "live": return "en vivo";
    case "novideo": return "sin vídeo";
    default: return "sin conexión · reintentando…";
  }
}

// ---- ciclo de vida del muro ----
// El muro corre (fotogramas, WebRTC, gobernadores) SOLO si se ve: pestaña
// Mosaico activa, DVR cerrado y sin «Pausar» manual. syncWall() deriva ese
// estado y arranca o detiene; se llama desde la navegación, el DVR
// (window.CamWayWall.sync) y los botones, así no hay carreras entre ellos.
let wallGen = 0;              // generación de loadWall: descarta respuestas viejas
let wallRunning = false;      // hay refresco / vídeo corriendo ahora
let wallUserPaused = false;   // «Pausado» activado a mano (botón En vivo / Pausado)
let wallSig = null;           // firma de las tejas del último loadWall

// ¿El muro está tapado (otra pestaña o DVR abierto)? Nada debe pedir imagen.
function wallOffscreen() {
  return document.body.classList.contains("dvr-open") ||
    !$("#view-mosaico")?.classList.contains("active");
}

// Botón único «En vivo / Pausado» (aria-pressed = el muro está en vivo).
function paintWallToggle() {
  const b = $("#btnWallToggle");
  if (!b) return;
  b.setAttribute("aria-pressed", String(!wallUserPaused));
  b.classList.toggle("is-paused", wallUserPaused);
  b.innerHTML = wallUserPaused
    ? `${icon("pause", 14)}<span>Pausado</span>`
    : '<span class="cw-dot cw-dot--live"></span><span>En vivo</span>';
}

function setWallPaused(p) {
  wallUserPaused = p;
  paintWallToggle();
  paintAllTiles();                 // la insignia pasa a «Pausado» / vuelve
}

// ---- pie de estado del Mosaico ----
// «N cámaras · en vivo · conectando · pide clave · sin señal (· en pausa)» contados
// desde el estado de cada teja (paintTileStatus), y a la derecha la calidad y la
// detección actuales. Se recalcula con un pequeño retardo: pintar 16 tejas de
// golpe no hace 16 recuentos.
const WF_TEXT = {
  live: (n) => `${n} en vivo`, connecting: (n) => `${n} conectando`,
  auth: (n) => `${n} ${n === 1 ? "pide clave" : "piden clave"}`, offline: (n) => `${n} sin señal`,
  paused: (n) => `${n} en pausa`,
};
let wfTimer = null;
function updateWallFooter() {
  if (wfTimer) return;
  wfTimer = setTimeout(() => { wfTimer = null; renderWallFooter(); }, 60);
}

function renderWallFooter() {
  const foot = $("#wallFoot");
  if (!foot) return;
  const counts = { live: 0, connecting: 0, auth: 0, offline: 0, paused: 0 };
  let ia = 0;
  state.tiles.forEach((c) => {
    if (c.status in counts) counts[c.status]++;
    if (c.config && c.config.enabled) ia++;
  });
  const n = state.tiles.size;
  $("#wfTotal").textContent = `${n} ${n === 1 ? "cámara" : "cámaras"}`;
  Object.keys(counts).forEach((k) => {
    const el = foot.querySelector(`[data-wf="${k}"]`);
    if (!el) return;
    el.hidden = counts[k] === 0;
    if (counts[k]) el.querySelector(".cw-wf__txt").textContent = WF_TEXT[k](counts[k]);
  });
  const calidad = liveMosaic ? "vídeo en vivo"
    : lightMode ? "mínima (modo ligero)"
    : autoQ ? `automática: ${Q_LEVELS[qLevel].name}` : "fija";
  const deteccion = state.serverDetection ? "servidor" : ia ? "navegador" : "sin IA activa";
  $("#wfInfo").textContent = `Calidad ${calidad} · Detección: ${deteccion}`;
}

function startWallFeeds() {
  if (liveMosaic) startLiveMosaic();
  else startSnapRefresh();
}

// ¿Esta teja necesita su vídeo en vivo para la IA del navegador? (con el
// detector del servidor activo, no: él procesa y el navegador solo recibe SSE.)
function keepLiveForAi(c) {
  return !state.serverDetection && !!(c.config && c.config.enabled);
}

// Detiene refresco, gobernadores y WebRTC SIN destruir las tejas. Con el muro
// solo TAPADO (otra pestaña / DVR) las tejas en vivo con IA del navegador se
// conservan: la IA sigue detectando (decisión del usuario). Con «Pausar» manual
// se corta todo.
function suspendWall() {
  wallRunning = false;
  stopSnapRefresh();
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  if (qTimer) { clearInterval(qTimer); qTimer = null; }
  const conservarIa = wallOffscreen() && !wallUserPaused;
  state.tiles.forEach((c) => { if (!(conservarIa && keepLiveForAi(c))) dropLive(c); });
}

function resumeWall() {
  wallRunning = true;
  startWallFeeds();
  const big = enlargedCtrl();       // una ampliada (p. ej. por alarma) vuelve a vídeo
  if (big) goLive(big);
}

function syncWall() {
  const want = state.tiles.size > 0 && !wallUserPaused && !wallOffscreen();
  if (want && !wallRunning) resumeWall();
  else if (!want && wallRunning) suspendWall();
}

// Al volver a la pestaña Mosaico: si las cámaras no cambiaron, solo se reanuda
// (las tejas siguen montadas); si cambiaron o nunca cargó bien, se reconstruye.
async function enterWallView() {
  setWallPaused(false);
  if (wallSig === null) return loadWall();
  try {
    const d = await api("/api/wall");
    if (JSON.stringify(d.tiles) !== wallSig) return loadWall();
  } catch (_) { /* sin red: se reanuda con lo que hay */ }
  syncWall();
}
window.CamWayWall = { sync: syncWall };   // lo usa dvr.js al abrir/cerrar

// Tras un cambio hecho en sitio (PTZ) se actualiza la firma sin reconstruir.
async function refreshWallSig() {
  try { wallSig = JSON.stringify((await api("/api/wall")).tiles); } catch (_) { wallSig = null; }
}

// Mensaje dentro del muro (cargando / error) en lugar de «No hay streams
// publicados», que solo es verdad cuando la API contestó con 0 cámaras.
function showWallMessage(html) {
  const wall = $("#wall");
  wall.classList.remove("fit");
  wall.innerHTML = `<p class="empty" role="status">${html}</p>`;
  return wall.firstElementChild;
}

function showWallError(err) {
  const p = showWallMessage(`No se pudo cargar el muro: ${esc(err && err.message || err)} `
    + `<button class="ghost" data-wall-retry>Reintentar</button>`);
  p.setAttribute("role", "alert");
  p.querySelector("[data-wall-retry]").addEventListener("click", () => loadWall());
}

async function loadWall() {
  const gen = ++wallGen;
  const hadWall = state.tiles.size > 0;
  $("#emptyWall").hidden = true;    // mientras carga no se afirma «no hay cámaras»
  if (!hadWall) showWallMessage("Cargando cámaras…");
  let data;
  try {
    data = await api("/api/wall");
  } catch (err) {
    if (gen !== wallGen) return;
    // Con un muro ya montado se conserva y se avisa; sin muro, mensaje con reintento.
    if (hadWall) toast("No se pudo recargar el muro: " + err.message, true);
    else showWallError(err);
    return;
  }
  if (gen !== wallGen) return;      // otra carga más reciente tomó el relevo
  setWallPaused(false);             // recargar reanuda, como siempre

  // Traer la disposición del usuario desde el servidor la primera vez (persiste
  // entre equipos). Luego saveLayout mantiene la caché al día.
  if (serverLayout === null) await fetchLayout();
  if (gen !== wallGen) return;

  // El navegador refresca los fotogramas al ritmo que produce el servidor.
  // Ritmo de poll = el del servidor. En modo ligero NO se topa: la máquina
  // corre a su máximo real gracias al "no encolar" (salta fotogramas si no
  // llega, sin atrasarse). El modo ligero solo pide fotogramas más pequeños.
  if (data.fps > 0) snapMs = Math.max(120, Math.round(1000 / data.fps));

  $("#wallHint").textContent = data.mediamtx
    ? "Arrastra las cámaras para reordenarlas o moverlas entre zonas."
    : "MediaMTX no responde: el mosaico no podrá conectar.";
  $("#emptyWall").hidden = data.tiles.length > 0;

  wallSig = JSON.stringify(data.tiles);
  stopWall();
  const wall = $("#wall");
  wall.innerHTML = "";

  // 1) Construir todas las tejas (con su player) en un mapa, sin colocarlas.
  const tileEls = new Map();      // streamId -> elemento de la teja
  data.tiles.forEach((tile) => {
    tileEls.set(tile.stream_id, makeTile(tile).el);
  });

  // 2) Colocarlas. En "solo zona" (ventana dedicada) solo las de esa zona; en
  //    "ajustar a pantalla" una rejilla única; si no, según las zonas guardadas.
  if (ZONE_ONLY) {
    renderZoneKiosk(tileEls);
  } else if (data.tiles.length) {
    if (state.fitMode) {
      fitRender(currentTileEls());   // respeta zonas "solo su ventana"
    } else {
      const layout = reconcileLayout(loadLayout(), [...tileEls.keys()]);
      saveLayout(layout);
      renderZones(layout, tileEls);
      applyColumns();
    }
  }

  // 3) Detección: si el detector server-side está activo, el navegador no
  //    corre el modelo; recibe las detecciones por SSE.
  try { state.serverDetection = (await api("/api/health")).detector; } catch (_) {}
  await applyServerConfigs();
  if (gen !== wallGen) return;
  updateWallFooter();
  openDetectionStream();
  startAlarmSweeper();

  // 4) Resolver el modo del mosaico. "frames": fotogramas (ligero, muchas
  //    cámaras). "live": vídeo WebRTC en TODAS (fluido, pocas cámaras). La
  //    elección del usuario manda; si no, el default del servidor; si es
  //    "auto", según el nº de cámaras.
  const def = (mosaicChoice || data.mosaic_default || "auto");
  liveMosaic = def === "live" ||
    (def === "auto" && data.tiles.length > 0 && data.tiles.length <= LIVE_MOSAIC_MAX);
  applyMosaicModeBtn();
  syncWall();     // arranca solo si el muro se ve (no bajo otra pestaña ni el DVR)
}

// Mosaico en vivo: todas las tejas a vídeo WebRTC (substream ligero); al
// maximizar una sube a 720p. Se mantiene un refresco de fotograma SOLO como
// póster mientras el vídeo conecta o si una cámara cae (fallback suave).
function startLiveMosaic() {
  stopSnapRefresh();
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }   // sin gobernador de auto-vivo
  snapTick();                                   // póster inicial
  state.tiles.forEach((c) => {
    if (!c.el.classList.contains("maximized")) goLive(c);
  });
  snapTimer = setInterval(snapTick, Math.max(700, qEffMs()));
}

// ---- teja del mosaico: se arma en piezas con nombre (HTML, reproductor, audio,
// hablar, estado, ampliar, arrastre, botones de admin, PTZ, menú). makeTile las une.

// Estados visibles de una teja (insignia + panel central). Los textos son fijos:
// nada de la API entra en ellos.
const TILE_STATUS = {
  live: "En vivo", connecting: "Conectando", auth: "Pide clave",
  offline: "Sin señal", paused: "Pausado",
};
const SVG_MINUS = '<svg class="cw-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M5 12h14"/></svg>';

// HTML interno de una teja. Todo dato de la API va por esc() (los nombres y los
// banners los escribe cualquier equipo de la LAN).
function tileHtml(tile) {
  const admin = isAdmin();
  const name = esc(tile.title);
  const sub = esc([tile.host, tile.vendor].filter(Boolean).join(" · "));
  return `
    <img class="snap" alt="">
    <video playsinline muted></video>
    <canvas class="detoverlay"></canvas>
    <div class="cw-tilestate" hidden></div>
    <div class="cw-tile__top">
      <div class="cw-tile__id">
        <button type="button" class="cw-tile__name" data-select title="Seleccionar ${name}">${name}</button>
        <div class="cw-tile__sub">${sub}</div>
      </div>
      <span class="cw-tag cw-tag--zone" title="Zona de detección activa" aria-label="Zona de detección activa">${icon("layers", 13)}</span>
      <span class="cw-tag cw-tag--sched" title="IA con horario" aria-label="IA con horario">${icon("calendar", 13)}</span>
      <span class="cw-badge cw-badge--connecting" data-badge>${TILE_STATUS.connecting}</span>
    </div>
    <div class="aialert">${icon("sparkles", 13)}<span data-alert-txt>Detección</span></div>
    <div class="cw-ptz" hidden>
      <button class="cw-ptz__btn up" data-ptz="up" title="Arriba" aria-label="Mover cámara arriba">${icon("chevronDown", 18)}</button>
      <button class="cw-ptz__btn left" data-ptz="left" title="Izquierda" aria-label="Mover cámara a la izquierda">${icon("chevronLeft", 18)}</button>
      <button class="cw-ptz__btn right" data-ptz="right" title="Derecha" aria-label="Mover cámara a la derecha">${icon("chevronRight", 18)}</button>
      <button class="cw-ptz__btn down" data-ptz="down" title="Abajo" aria-label="Mover cámara abajo">${icon("chevronDown", 18)}</button>
      ${tile.zoom ? `<button class="cw-ptz__btn zoomin" data-ptz="zoomin" title="Acercar (zoom óptico)" aria-label="Acercar (zoom óptico)">${icon("plus", 16)}</button>
      <button class="cw-ptz__btn zoomout" data-ptz="zoomout" title="Alejar (zoom óptico)" aria-label="Alejar (zoom óptico)">${SVG_MINUS}</button>` : ""}
    </div>
    <div class="cw-ctl">
      <span class="cw-tile__codec">${esc(tile.codec)}</span>
      <button class="cw-ib cw-ib--glass" type="button" data-enlarge aria-label="Ampliar ${name}" title="Ampliar (doble clic o Intro)"><span class="ico-max">${icon("maximize", 16)}</span><span class="ico-min">${icon("minimize", 16)}</span></button>
      <button class="cw-ib cw-ib--glass" type="button" data-ptzopen aria-pressed="false" aria-label="Mover cámara (PTZ) de ${name}" title="Mover cámara (PTZ)" hidden>${icon("ptz", 16)}</button>
      ${tile.audio ? `<button class="cw-ib cw-ib--glass" type="button" data-mute aria-pressed="false" aria-label="Oír el audio de ${name}" title="Oír audio">${icon("volumeOff", 16)}</button>` : ""}
      ${tile.talk ? `<button class="cw-ib cw-ib--glass" type="button" data-talk aria-label="Hablar por ${name} (mantén pulsado)" title="Hablar (mantén pulsado)">${icon("mic", 16)}</button>` : ""}
      ${admin ? `<button class="cw-ib cw-ib--glass rec${tile.rec ? " recon" : ""}" type="button" data-rec aria-label="Grabar en este equipo ${name}" title="Grabación local">${icon("record", 16)}</button>` : ""}
      <button class="cw-ib cw-ib--glass ai" type="button" data-ai aria-label="Detección por IA de ${name}" title="Detección por IA">${icon("sparkles", 14)}<span>IA</span></button>
      <div class="cw-menuwrap">
        <button class="cw-ib cw-ib--glass" type="button" data-more aria-haspopup="menu" aria-expanded="false" aria-label="Más acciones de ${name}" title="Más acciones">${icon("more", 16)}</button>
        <div class="cw-menu cw-menu--up" role="menu" aria-label="Más acciones de ${name}" hidden>
          <button class="cw-menu__item" type="button" role="menuitem" data-play>${icon("play", 16)}<span class="cw-menu__lbl">Ver en vivo</span></button>
          ${admin ? `<button class="cw-menu__item" type="button" role="menuitem" data-optimize>${icon("zap", 16)}<span class="cw-menu__lbl">Optimizar tiempo real</span></button>
          <button class="cw-menu__item" type="button" role="menuitem" data-ptzcfg>${icon("ptz", 16)}<span class="cw-menu__lbl">Activar PTZ (ONVIF)</span></button>` : ""}
        </div>
      </div>
    </div>`;
}

// Elemento de la teja: enfocable con Tab, Intro la amplía/restaura (igual que el
// doble clic) y Escape restaura una ampliada dentro de la página.
function createTileElement(tile) {
  const el = document.createElement("div");
  el.className = "tile cw-tile";
  el.dataset.streamId = tile.stream_id;
  el.draggable = true;
  el.tabIndex = 0;
  el.setAttribute("role", "group");
  el.setAttribute("aria-label", `Cámara ${tile.title}`);
  el.innerHTML = tileHtml(tile);
  return el;
}

// Reproductor WebRTC de la teja: flujo ligero por defecto, alto al ampliar.
function createTilePlayer(tile, video) {
  const player = new WhepPlayer(tile.whep, video, WALL_WHEP_OPTS);
  player.urlLo = tile.whep;                      // substream ligero (por defecto)
  player.urlHi = tile.whep_hi || tile.whep;      // 3K para pantalla completa
  return player;
}

// Audio: el <video> arranca muteado (política de autoplay). Al pulsar —gesto de
// usuario— se desmutea y suena el audio de la cámara (solo las que publican
// G.711/PCMA; las AAC no traen este botón). Solo una cámara con sonido a la vez
// para no mezclar varias.
function wireTileAudio(el, tile, video, player) {
  const muteBtn = el.querySelector("[data-mute]");
  if (!muteBtn) return;
  muteBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const on = video.muted;               // se va a encender
    if (on) {
      state.players.forEach((p, sid) => {  // silenciar las demás
        if (sid !== tile.stream_id && p.video) { p.video.muted = true; }
      });
      document.querySelectorAll("[data-mute]").forEach((b) => {
        b.innerHTML = icon("volumeOff", 16); b.setAttribute("aria-pressed", "false");
      });
      if (!player.active) { const c = state.tiles.get(tile.stream_id); if (c) goLive(c); }
    }
    video.muted = !on;
    video.volume = 1;
    muteBtn.innerHTML = icon(on ? "volume" : "volumeOff", 16);
    muteBtn.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

// Push-to-talk: mantener pulsado el micrófono habla por el altavoz de la cámara.
function wireTileTalk(el, tile) {
  const talkBtn = el.querySelector("[data-talk]");
  if (!talkBtn) return;
  const press = (ev) => {
    ev.stopPropagation(); ev.preventDefault();
    startTalk(tile.stream_id, talkBtn);
  };
  const release = (ev) => { if (ev) ev.stopPropagation(); stopTalk(); };
  talkBtn.addEventListener("pointerdown", press);
  talkBtn.addEventListener("pointerup", release);
  talkBtn.addEventListener("pointerleave", release);
  talkBtn.addEventListener("pointercancel", release);
  talkBtn.addEventListener("click", (ev) => ev.stopPropagation());
}

// ---- estado de la teja: insignia + panel central ----
// Deriva el estado de lo que se sabe: pide clave (si la API lo marca), pausado,
// y conectando / en vivo / sin señal según el vídeo WebRTC o los fotogramas.
function tileStatus(c) {
  if (c.authError) return "auth";
  if (wallUserPaused) return "paused";
  if (c.live) {
    if (c.videoReady || c.conn === "live") return "live";
    return c.conn === "offline" ? "offline" : "connecting";
  }
  if ((c.snapFails || 0) >= SNAP_FAILS_OFFLINE) return "offline";
  return c.gotFrame ? "live" : "connecting";
}

function tileStatePanel(st, c) {
  if (st === "connecting") return `${icon("refresh", 28)}<span>Conectando…</span>`;
  if (st === "offline") return `${icon("noSignal", 28)}<span>Sin señal · reintentando</span>`;
  if (st === "auth") {
    return `${icon("lock", 28)}<span>La cámara pide usuario y clave</span>` +
      (isAdmin() ? '<button type="button" class="cw-btn cw-btn--sm" data-enter-pass>Ingresar clave</button>' : "");
  }
  return "";
}

function paintTileStatus(c) {
  const st = tileStatus(c);
  if (c.status === st) return;
  c.status = st;
  const el = c.el;
  el.dataset.status = st;
  el.classList.toggle("snapoff", st === "offline");
  const badge = el.querySelector("[data-badge]");
  if (badge) {
    badge.className = `cw-badge cw-badge--${st}`;
    badge.textContent = TILE_STATUS[st];
  }
  const panel = el.querySelector(".cw-tilestate");
  if (panel) {
    const html = tileStatePanel(st, c);
    panel.innerHTML = html;
    panel.hidden = !html;
    panel.dataset.status = st;
  }
  updateWallFooter();
}

function paintAllTiles() { state.tiles.forEach(paintTileStatus); }

// Texto y nombre accesible del elemento Ver/Parar del menú de la teja.
function refreshPlayBtn(el, title, player) {
  const it = el.querySelector("[data-play]");
  if (!it) return;
  it.querySelector(".cw-menu__lbl").textContent = player.active ? "Parar el vídeo" : "Ver en vivo";
  it.firstElementChild.outerHTML = icon(player.active ? "pause" : "play", 16);
}

// Estado de conexión sobre la teja y elemento Ver/Parar.
function wireTileState(el, tile, player) {
  player.onState = (st) => {
    const c = state.tiles.get(tile.stream_id);
    if (!c) return;
    c.conn = st;
    paintTileStatus(c);
  };
  state.players.set(tile.stream_id, player);

  el.querySelector("[data-play]").addEventListener("click", () => {
    const c = state.tiles.get(tile.stream_id);
    if (!c) return;
    if (c.live) goSnap(c); else goLive(c);
  });
  // «Ingresar clave» (cámara que pide usuario y clave): lleva a la pestaña Cámaras.
  el.querySelector(".cw-tilestate").addEventListener("click", (e) => {
    if (e.target.closest("[data-enter-pass]")) $('.tab[data-view="inventario"]')?.click();
  });
}

// Ampliar/restaurar (botón, doble clic, Intro o Escape) y selección de la teja.
function wireTileEnlarge(el, tile) {
  const toggleEnlarge = () => {
    const c = state.tiles.get(tile.stream_id);
    if (!c) return;
    if (c.el.classList.contains("maximized")) {
      // Ya ampliada (por alarma o manual): restaurar (y reconocer si era alarma).
      resetZoom(c);
      if (c.alarmed) dismissAlarm(c);
      else { c.el.classList.remove("maximized"); c.autoMaxed = false; c.player.setQuality(false); goSnap(c); }
    } else if (state.fitMode) {
      maximizeTile(c);         // spotlight in-page, sin salir del fullscreen del SO
    } else {
      toggleFullscreen(el);    // vista normal: pantalla completa del SO
    }
  };
  el.querySelector("[data-enlarge]").addEventListener("click", (e) => { e.stopPropagation(); toggleEnlarge(); });
  el.addEventListener("dblclick", toggleEnlarge);
  el.addEventListener("keydown", (e) => {
    if (e.target !== el) return;               // Intro sobre un botón hace su propia acción
    if (e.key === "Enter") { e.preventDefault(); toggleEnlarge(); }
    else if (e.key === "Escape" && el.classList.contains("maximized")) { e.preventDefault(); toggleEnlarge(); }
  });
  // Seleccionar: clic en la teja o en su nombre (borde azul; la del modo «1»).
  el.addEventListener("click", (e) => {
    if (e.target.closest("button:not([data-select]), a, .cw-menu, .cw-ptz")) return;
    selectTile(tile.stream_id);
  });
}

// Arrastre de la teja (mueve el nodo, el video no se corta). En vista
// ampliada/pantalla completa, arrastrar es para DESPLAZAR el zoom, no reordenar.
function wireTileDrag(el, tile) {
  el.addEventListener("dragstart", (e) => {
    if (el.classList.contains("maximized") || document.fullscreenElement === el) {
      e.preventDefault();
      return;
    }
    el.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", tile.stream_id);
  });
  el.addEventListener("dragend", () => {
    el.classList.remove("dragging");
    $$(".zonebox.dragover").forEach((z) => z.classList.remove("dragover"));
  });
}

// Controlador de la teja: lo que el resto del panel (IA, zoom, alarmas, refresco)
// lee y escribe.
function createTileCtrl(tile, el, video, player) {
  return {
    streamId: tile.stream_id,
    title: tile.title,
    el, video, player,
    snap: el.querySelector("img.snap"),
    aiBtn: el.querySelector("[data-ai]"),
    alertEl: el.querySelector(".aialert"),
    frameUrl: tile.frame || `/api/wall/${tile.stream_id}/frame`,
    // Dimensiones de referencia para el overlay cuando el vídeo no corre
    // (las cajas del detector llegan normalizadas 0..1).
    vw: tile.width || 16, vh: tile.height || 9,
    overlay: el.querySelector(".detoverlay"),
    config: loadAiConfig(tile.stream_id),
    live: false,          // false = mosaico por fotograma; true = vídeo WebRTC
    videoReady: false,    // el vídeo ya tiene su primer frame (oculta el fotograma)
    conn: "connecting",   // último estado del reproductor WebRTC
    gotFrame: false,      // ya llegó algún fotograma (modo fotogramas)
    authError: !!tile.auth_error,   // la API marca «pide usuario y clave» (si lo informa)
    status: null,         // estado visible actual (ver tileStatus)
    alarmed: false,
    lastHit: 0,
    autoMaxed: false,
    tracked: [],          // objetos ya vistos (para no re-disparar por lo mismo)
    lastDetections: [],   // detecciones normalizadas del último análisis
    zoom: { s: 1, x: 0, y: 0 },   // zoom digital en la vista ampliada
  };
}

// Botones solo para admin: grabación local y optimizar tiempo real.
function wireTileAdminButtons(el, tile) {
  const recBtn = el.querySelector("[data-rec]");
  if (recBtn) recBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openRecModal(tile.device_id, tile.title, recBtn);
  });

  const optBtn = el.querySelector("[data-optimize]");
  if (optBtn) optBtn.addEventListener("click", async () => {
    optBtn.classList.add("busy");
    try {
      const r = await api(`/api/streams/${tile.stream_id}/optimize`, { method: "POST", timeout: 0 });
      const n = (r.configs || []).filter((c) => c.ok).length;
      toast(`Optimizada para tiempo real (keyframe 1s en ${n} flujo(s)). Reconecta la cámara.`);
    } catch (e) {
      toast("No se pudo optimizar: " + e.message, true);
    } finally {
      optBtn.classList.remove("busy");
    }
  });
}

// PTZ. Admin: «Activar/Desactivar PTZ» en el menú «Más» de la teja (se aplica en
// sitio, sin rehacer el muro). El botón PTZ de los controles y las flechas solo
// existen si la cámara tiene PTZ activado; las flechas se abren con ese botón.
function wireTilePtz(el, tile) {
  const pad = el.querySelector(".cw-ptz");
  const openBtn = el.querySelector("[data-ptzopen]");
  const cfgItem = el.querySelector("[data-ptzcfg]");
  const paintPtz = () => {
    openBtn.hidden = !tile.ptz;
    if (!tile.ptz) { openBtn.setAttribute("aria-pressed", "false"); el.classList.remove("ptz-open"); }
    pad.hidden = !(tile.ptz && el.classList.contains("ptz-open"));
    if (cfgItem) {
      cfgItem.querySelector(".cw-menu__lbl").textContent = tile.ptz ? "Desactivar PTZ (ONVIF)" : "Activar PTZ (ONVIF)";
      cfgItem.classList.toggle("ptzon", !!tile.ptz);
    }
  };
  openBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = !el.classList.contains("ptz-open");
    el.classList.toggle("ptz-open", open);
    openBtn.setAttribute("aria-pressed", String(open));
    paintPtz();
  });
  if (cfgItem) cfgItem.addEventListener("click", async () => {
    const enabling = !tile.ptz;
    if (!enabling && !(await uiConfirm(`¿Desactivar el PTZ de "${tile.title}"?`, { okLabel: "Desactivar", danger: true }))) return;
    cfgItem.classList.add("busy");
    try {
      await api(`/api/devices/${tile.device_id}/ptz-config`, {
        method: "PUT", body: JSON.stringify({ enabled: enabling }) });
      tile.ptz = enabling;
      paintPtz();
      toast(enabling ? "PTZ activado." : "PTZ desactivado.");
      refreshWallSig();      // la firma del muro refleja el cambio: no hay recarga al volver
    } catch (e) {
      toast("PTZ: " + e.message, true);
    } finally {
      cfgItem.classList.remove("busy");
    }
  });

  paintPtz();
  pad.querySelectorAll("[data-ptz]").forEach((b) => {
    // Movimiento POR PASOS: cada clic = mover+detener en la MISMA petición del
    // servidor. El stop siempre ocurre → es imposible que la cámara quede
    // girando sin control (estas genéricas ignoran el Timeout ONVIF, así que
    // NO usamos 'mantener pulsado'). Para mover más, se pulsa varias veces.
    const dir = b.dataset.ptz;
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (b.classList.contains("busy")) return;
      b.classList.add("busy");
      try { await api(`/api/streams/${tile.stream_id}/ptz?dir=${dir}`); }
      catch (err) { toast("PTZ: " + err.message, true); }
      finally { setTimeout(() => b.classList.remove("busy"), 120); }
    });
  });
  el.addEventListener("dblclick", (e) => {   // no maximizar al usar el pad
    if (e.target.closest(".cw-ptz")) e.stopPropagation();
  }, true);
}

// Crea una teja (video + player + IA + arrastre) y devuelve {el, ctrl}.
function makeTile(tile) {
  const el = createTileElement(tile);
  const video = el.querySelector("video");
  const player = createTilePlayer(tile, video);

  wireTileAudio(el, tile, video, player);
  wireTileTalk(el, tile);
  wireTileState(el, tile, player);
  wireTileEnlarge(el, tile);
  wireTileDrag(el, tile);

  const ctrl = createTileCtrl(tile, el, video, player);
  refreshAiButton(ctrl);
  refreshZoneTag(ctrl);
  refreshSchedTag(ctrl);
  ctrl.aiBtn.addEventListener("click", () => openAiModal(ctrl));

  wireTileAdminButtons(el, tile);
  wireTilePtz(el, tile);
  wireMenu(el.querySelector("[data-more]"), el.querySelector(".cw-menu"));

  setupZoom(ctrl);
  observeTileVisibility(ctrl);
  state.tiles.set(tile.stream_id, ctrl);
  if (ctrl.config.enabled) aiEngine.register(ctrl);
  paintTileStatus(ctrl);
  return { el, ctrl };
}

// ---------------- zonas de disposición del mosaico ----------------

const LAYOUT_KEY = "camway.layout";

// La disposición se guarda EN EL SERVIDOR por usuario (persiste entre
// navegadores y equipos). localStorage queda solo como respaldo offline.
let serverLayout = null;          // caché en memoria de la disposición del usuario
let _layoutSaveTimer = null;

function loadLayoutLocal() {
  try {
    const l = JSON.parse(localStorage.getItem(LAYOUT_KEY));
    if (l && Array.isArray(l.zones)) return l;
  } catch (_) {}
  return { zones: [{ id: "default", name: "Sin zona", cams: [] }] };
}

// Trae la disposición del servidor a la caché (y la respalda en localStorage).
async function fetchLayout() {
  try {
    const l = await api("/api/layout");
    if (l && Array.isArray(l.zones)) {
      serverLayout = l;
      try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(l)); } catch (_) {}
      return;
    }
  } catch (_) {}
  serverLayout = loadLayoutLocal();   // sin servidor: respaldo local
}

function loadLayout() {
  return (serverLayout && Array.isArray(serverLayout.zones)) ? serverLayout : loadLayoutLocal();
}

function saveLayout(l) {
  serverLayout = l;
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(l)); } catch (_) {}
  clearTimeout(_layoutSaveTimer);     // guardado al servidor con pequeño retardo
  _layoutSaveTimer = setTimeout(() => {
    api("/api/layout", { method: "PUT", body: JSON.stringify(l) }).catch(() => {});
  }, 500);
}

// Cada cámara aparece exactamente una vez; las nuevas caen en la zona por defecto.
function reconcileLayout(layout, ids) {
  if (!layout.zones.some((z) => z.id === "default")) {
    layout.zones.unshift({ id: "default", name: "Sin zona", cams: [] });
  }
  const seen = new Set();
  layout.zones.forEach((z) => {
    z.cams = (z.cams || []).filter((id) => ids.includes(id) && !seen.has(id) && seen.add(id));
  });
  const def = layout.zones.find((z) => z.id === "default");
  ids.forEach((id) => { if (!seen.has(id)) { def.cams.push(id); seen.add(id); } });
  return layout;
}

function renderZones(layout, tileEls) {
  const wall = $("#wall");
  wall.innerHTML = "";
  layout.zones.forEach((zone) => {
    const box = makeZoneBox(zone);
    const grid = box.querySelector(".zonegrid");
    zone.cams.forEach((id) => {
      const el = tileEls.get(id);
      if (el) grid.appendChild(el);
    });
    updateZoneCount(box);
    wall.appendChild(box);
  });
}

function makeZoneBox(zone) {
  const box = document.createElement("section");
  box.className = "zonebox";
  box.dataset.zoneId = zone.id;
  const isDefault = zone.id === "default";
  box.innerHTML = `
    <div class="zonehead">
      <input class="zname" spellcheck="false" aria-label="Nombre de la zona">
      <span class="zcount"></span>
      <span class="zspacer"></span>
      ${isDefault ? "" : '<button class="ghost small" data-zfit title="Cuando está activo, esta zona NO aparece en «Ajustar a pantalla» del monitor principal; solo en su propia ventana.">▦ Solo su ventana</button>'}
      <button class="ghost small" data-zwin title="Abrir esta zona en una ventana nueva (pantalla dedicada)">⧉ Ventana</button>
      ${isDefault ? "" : '<button class="ghost small" data-zdel>Eliminar</button>'}
    </div>
    <div class="zonegrid"></div>`;

  const nameInput = box.querySelector(".zname");
  nameInput.value = zone.name;
  nameInput.addEventListener("change", rebuildLayoutFromDom);

  const delBtn = box.querySelector("[data-zdel]");
  if (delBtn) delBtn.addEventListener("click", () => deleteZone(zone.id));

  const winBtn = box.querySelector("[data-zwin]");
  if (winBtn) winBtn.addEventListener("click", () =>
    window.open(`${location.pathname}?zone=${encodeURIComponent(zone.id)}`, "_blank", "noopener"));

  // "Solo su ventana": excluye esta zona del «Ajustar a pantalla» del monitor
  // principal (se sigue viendo en la vista de zonas y en su ventana dedicada).
  box.dataset.fitExclude = zone.fitExclude ? "1" : "";
  const fitBtn = box.querySelector("[data-zfit]");
  if (fitBtn) {
    const paint = () => {
      const on = box.dataset.fitExclude === "1";
      fitBtn.classList.toggle("primary", on);
      fitBtn.textContent = (on ? "✓ " : "▦ ") + "Solo su ventana";
    };
    paint();
    fitBtn.addEventListener("click", () => {
      box.dataset.fitExclude = box.dataset.fitExclude === "1" ? "" : "1";
      paint();
      rebuildLayoutFromDom();
      if (state.fitMode) fitRender(currentTileEls());
    });
  }

  wireZoneDrop(box);
  return box;
}

function wireZoneDrop(box) {
  const grid = box.querySelector(".zonegrid");
  grid.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    box.classList.add("dragover");
    const dragging = $(".tile.dragging");
    if (!dragging) return;
    const t = dropTarget(grid, e.clientX, e.clientY);
    if (!t) grid.appendChild(dragging);
    else if (t.before) grid.insertBefore(dragging, t.ref);
    else grid.insertBefore(dragging, t.ref.nextSibling);
  });
  grid.addEventListener("dragleave", (e) => {
    if (!grid.contains(e.relatedTarget)) box.classList.remove("dragover");
  });
  grid.addEventListener("drop", (e) => {
    e.preventDefault();
    box.classList.remove("dragover");
    rebuildLayoutFromDom();
  });
}

// Teja de referencia para insertar: la más cercana al puntero.
function dropTarget(grid, x, y) {
  const tiles = [...grid.querySelectorAll(".tile:not(.dragging)")];
  if (!tiles.length) return null;
  let ref = null, best = Infinity, before = true;
  for (const t of tiles) {
    const r = t.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const d = Math.hypot(x - cx, y - cy);
    if (d < best) {
      best = d;
      ref = t;
      before = y < cy - 4 || (Math.abs(y - cy) <= r.height / 2 && x < cx);
    }
  }
  return { ref, before };
}

function rebuildLayoutFromDom() {
  const boxes = [...$("#wall").querySelectorAll(".zonebox")];
  const zones = boxes.map((box) => ({
    id: box.dataset.zoneId,
    name: box.querySelector(".zname").value.trim() || "Zona",
    cams: [...box.querySelectorAll(".tile")].map((t) => t.dataset.streamId),
    fitExclude: box.dataset.fitExclude === "1",
  }));
  if (!zones.some((z) => z.id === "default")) {
    zones.unshift({ id: "default", name: "Sin zona", cams: [] });
  }
  saveLayout({ zones });
  boxes.forEach(updateZoneCount);
}

function updateZoneCount(box) {
  const n = box.querySelectorAll(".tile").length;
  box.querySelector(".zcount").textContent = n === 1 ? "1 cámara" : `${n} cámaras`;
}

async function addZone() {
  const name = await uiPrompt("Nombre de la zona", { value: "Nueva zona", okLabel: "Crear" });
  if (name === null) return;
  const box = makeZoneBox({ id: "z" + Date.now().toString(36), name: name || "Zona", cams: [] });
  $("#wall").appendChild(box);
  updateZoneCount(box);
  applyColumns();
  rebuildLayoutFromDom();
}

function deleteZone(id) {
  if (id === "default") return;
  const box = $(`.zonebox[data-zone-id="${id}"]`);
  const defBox = $('.zonebox[data-zone-id="default"]');
  if (box && defBox) {
    const defGrid = defBox.querySelector(".zonegrid");
    box.querySelectorAll(".tile").forEach((t) => defGrid.appendChild(t));
    box.remove();
    updateZoneCount(defBox);
  }
  rebuildLayoutFromDom();
}

// ---- rejilla del mosaico: Auto · 1 · 2×2 · 3×3 ----
// «Auto» = columnas según las cámaras de cada zona (1→1, ≤4→2, ≤9→3, si no 4);
// «1» = solo la cámara seleccionada. Se recuerda en este navegador.
let wallCols = "auto";
try {
  const c = localStorage.getItem("camway.cols");
  if (["auto", "1", "2", "3"].includes(c)) wallCols = c;
} catch (_) {}
let selectedStream = null;        // teja seleccionada (borde azul); la que muestra el modo «1»

function autoCols(n) { return n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4; }

function paintGridSeg() {
  $$("#gridSeg [data-cols]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.cols === wallCols));
  });
}

function applyColumns() {
  const solo = wallCols === "1";
  if (solo && !(selectedStream && state.tiles.has(selectedStream))) {
    selectedStream = $("#wall .tile")?.dataset.streamId || null;   // por defecto, la primera
  }
  state.tiles.forEach((c, sid) => {
    c.el.hidden = solo && sid !== selectedStream;
    c.el.classList.toggle("selected", sid === selectedStream);
  });
  $$("#wall .zonegrid").forEach((g) => {
    const n = [...g.children].filter((t) => t.classList.contains("tile") && !t.hidden).length;
    g.style.setProperty("--cw-cols", solo ? 1 : wallCols === "auto" ? autoCols(n) : Number(wallCols));
    const box = g.closest(".zonebox");
    if (box) box.hidden = solo && n === 0;
  });
  paintGridSeg();
}

function selectTile(streamId) {
  selectedStream = streamId;
  state.tiles.forEach((c, sid) => c.el.classList.toggle("selected", sid === streamId));
  if (wallCols === "1") applyColumns();
}

$$("#gridSeg [data-cols]").forEach((b) => {
  b.addEventListener("click", () => {
    wallCols = b.dataset.cols;
    try { localStorage.setItem("camway.cols", wallCols); } catch (_) {}
    applyColumns();
  });
});

// ---------------- modo "ajustar a pantalla" (video wall) ----------------

function currentTileEls() {
  // Respeta el orden que el usuario dejó con drag-drop (disposición guardada),
  // para que «ajustar a pantalla» conserve ese orden. Las zonas marcadas "solo
  // su ventana" (fitExclude) se EXCLUYEN de aquí: se ven en su ventana aparte.
  const m = new Map();
  const layout = loadLayout();
  const excluida = new Set();
  layout.zones.forEach((z) => { if (z.fitExclude) (z.cams || []).forEach((id) => excluida.add(id)); });
  layout.zones.forEach((z) => {
    if (z.fitExclude) return;
    (z.cams || []).forEach((id) => {
      const c = state.tiles.get(id);
      if (c && !m.has(id)) m.set(id, c.el);
    });
  });
  // Cámaras nuevas sin zona (y no excluidas) también entran al ajuste.
  state.tiles.forEach((c, sid) => { if (!m.has(sid) && !excluida.has(sid)) m.set(sid, c.el); });
  return m;
}

// Una sola rejilla con TODAS las cámaras, dimensionada para que quepan en el
// viewport sin scroll, maximizando el tamaño de cada teja (16:9).
function fitRender(tileEls) {
  const wall = $("#wall");
  wall.classList.add("fit");
  wall.innerHTML = "";
  const els = [...tileEls.values()];
  els.forEach((el) => { el.hidden = false; });   // «ajustar» muestra todas (aunque el modo «1» oculte)
  const n = els.length || 1;

  const rect = wall.getBoundingClientRect();
  const vw = window.innerWidth || document.documentElement.clientWidth || 1280;
  const vh = window.innerHeight || document.documentElement.clientHeight || 800;
  const W = rect.width || vw;
  const H = Math.max(240, vh - rect.top - 6);
  wall.style.height = H + "px";

  // El logo va en la barra del mosaico (visible también en pantalla completa),
  // así que aquí no se repite ningún encabezado.
  const grid = document.createElement("div");
  grid.className = "fitgrid";
  wall.appendChild(grid);
  let bestCols = 1, bestArea = 0;
  for (let cols = 1; cols <= n; cols++) {
    const rows = Math.ceil(n / cols);
    const tw = W / cols, th = H / rows;
    const w = Math.min(tw, th * 16 / 9);   // encajar 16:9
    const area = w * (w * 9 / 16);
    if (area > bestArea) { bestArea = area; bestCols = cols; }
  }
  const rows = Math.ceil(n / bestCols);
  grid.style.gridTemplateColumns = `repeat(${bestCols}, 1fr)`;
  grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;
  els.forEach((el) => grid.appendChild(el));
}

// Mosaico de SOLO una zona (ventana dedicada /?zone=<id>): llena la pantalla con
// las cámaras de esa zona y oculta los controles. Para una pantalla del guardia.
function renderZoneKiosk(tileEls) {
  const layout = reconcileLayout(loadLayout(), [...tileEls.keys()]);
  const zone = layout.zones.find((z) => z.id === ZONE_ONLY);
  const ids = zone ? (zone.cams || []) : [];
  const m = new Map();
  ids.forEach((id) => { const el = tileEls.get(id); if (el) m.set(id, el); });
  const name = zone ? zone.name : "Zona";
  document.title = name + " · CamWay";
  const tEl = $("#zoneKioskName"); if (tEl) tEl.textContent = name;
  $("#emptyWall").hidden = true;
  const wall = $("#wall");
  if (m.size === 0) {
    wall.classList.remove("fit");
    wall.innerHTML = '<p class="empty">Esta zona no tiene cámaras asignadas. Agrégalas desde la vista principal.</p>';
    return;
  }
  fitRender(m);
}

function exitFit() {
  const wall = $("#wall");
  wall.classList.remove("fit");
  wall.style.height = "";
  wall.style.gridTemplateColumns = "";
  wall.style.gridTemplateRows = "";
}

function setFitMode(on) {
  state.fitMode = on;
  const fb = $("#btnFit");
  fb.setAttribute("aria-pressed", String(on));
  fb.setAttribute("aria-label", on ? "Salir de pantalla completa" : "Pantalla completa");
  fb.title = on ? "Salir de pantalla completa" : "Pantalla completa: ajusta todas las cámaras al monitor";
  fb.innerHTML = icon(on ? "minimize" : "maximize", 16);
  $("#view-mosaico").classList.toggle("is-fit", on);
  if (on) {
    fitRender(currentTileEls());
  } else {
    exitFit();
    const tileEls = currentTileEls();
    const layout = reconcileLayout(loadLayout(), [...tileEls.keys()]);
    renderZones(layout, tileEls);
    applyColumns();
  }
}

$("#btnFit").addEventListener("click", () => {
  const turningOn = !state.fitMode;
  if (turningOn) {
    // Pantalla completa real del navegador para llenar todo el monitor.
    const el = document.getElementById("view-mosaico");
    if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
  } else if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  }
  setFitMode(turningOn);
});

// Al reajustar tamaño o entrar/salir de pantalla completa, recomponer la rejilla.
// Al cerrar/recargar la página, cerrar las sesiones WHEP (evita zombis).
window.addEventListener("pagehide", () => { state.players.forEach((p) => p.stop()); });

window.addEventListener("resize", () => {
  if (ZONE_ONLY) {
    const m = new Map([...state.tiles].map(([sid, c]) => [sid, c.el]));
    renderZoneKiosk(m);
  } else if (state.fitMode) {
    fitRender(currentTileEls());
  }
});
let fsCtrl = null;   // teja que está en pantalla completa del SO (vista normal)

document.addEventListener("fullscreenchange", () => {
  // Si el usuario salió de pantalla completa (Esc), salir también del modo ajuste.
  if (!document.fullscreenElement && state.fitMode) setFitMode(false);
  else if (state.fitMode) setTimeout(() => fitRender(currentTileEls()), 100);
  // Vista normal: la teja a pantalla completa muestra vídeo; al salir, fotograma.
  if (!document.fullscreenElement && fsCtrl) {
    resetZoom(fsCtrl);
    fsCtrl.player.setQuality(false);               // vuelve al substream ligero
    if (!fsCtrl.autoMaxed && !fsCtrl.alarmed) goSnap(fsCtrl);
    fsCtrl = null;
  }
});

// ---------------- pantalla completa ----------------

function toggleFullscreen(el) {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else if (el.requestFullscreen) {
    const c = state.tiles.get(el.dataset.streamId);
    if (c) { fsCtrl = c; c.player.setQuality(true); goLive(c); }   // pantalla completa → 3K en vivo
    el.requestFullscreen().catch(() => {});
  }
}

function stopWall() {
  wallRunning = false;
  updateWallFooter();
  if (tileObserver) tileObserver.disconnect();   // las tejas se van a rehacer
  stopSnapRefresh();
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  if (qTimer) { clearInterval(qTimer); qTimer = null; }
  state.players.forEach((p) => p.stop());
  state.players.clear();
  state.tiles.forEach((c) => aiEngine.unregister(c));
  state.tiles.clear();
}

$("#btnNewZone").addEventListener("click", addZone);
$("#btnWallReload").addEventListener("click", () => loadWall());
// Elemento del menú «Más»: casilla (aria-checked) con un valor opcional a la derecha.
function paintMenuToggle(b, checked, val) {
  b.setAttribute("aria-checked", String(!!checked));
  const v = b.querySelector(".cw-menu__val");
  if (v) v.textContent = val || "";
}

function applyLightBtn() {
  const b = $("#btnLight");
  if (!b) return;
  b.title = "Forzar calidad mínima en este equipo (desactiva el ajuste automático)";
  paintMenuToggle(b, lightMode,
    lightMode ? "mínima" : autoQ ? `auto: ${Q_LEVELS[qLevel].name}` : "");
  updateWallFooter();
}
$("#btnLight").addEventListener("click", () => {
  lightMode = !lightMode;
  try { localStorage.setItem("camway.light", lightMode ? "1" : "0"); } catch (_) {}
  applyLightBtn();
  // El ritmo y el ancho del fotograma se leen en cada petición: basta reiniciar
  // el temporizador, sin tocar las tejas.
  if (wallRunning) startWallFeeds();
  toast(lightMode ? "Modo ligero activado (este equipo)" : "Modo ligero desactivado");
});
applyLightBtn();

function applyAutoLiveBtn() {
  const b = $("#btnAutoLive");
  if (!b) return;
  paintMenuToggle(b, autoLive, "");
}
$("#btnAutoLive")?.addEventListener("click", () => {
  autoLive = !autoLive;
  try { localStorage.setItem("camway.autolive", autoLive ? "1" : "0"); } catch (_) {}
  applyAutoLiveBtn();
  if (!autoLive) { liveBudget = 0; reconcileAutoLive(); }   // apagar: todo a fotograma
  toast(autoLive ? "Auto-vivo: las cámaras con actividad suben a vídeo si hay recursos"
                 : "Auto-vivo desactivado");
});
applyAutoLiveBtn();

// Selector de modo del mosaico: fotogramas ↔ vídeo en vivo. Auto-vivo solo
// aplica en modo fotogramas (en vivo ya están todas).
function applyMosaicModeBtn() {
  const b = $("#btnMosaicMode");
  if (!b) return;
  paintMenuToggle(b, liveMosaic, liveMosaic ? "" : "fotogramas");
  updateWallFooter();
  const al = $("#btnAutoLive");
  if (al) al.hidden = liveMosaic;     // en vivo ya están todas: auto-vivo no aplica
}
$("#btnMosaicMode")?.addEventListener("click", () => {
  mosaicChoice = liveMosaic ? "frames" : "live";   // alterna la elección
  try { localStorage.setItem("camway.mosaicmode", mosaicChoice); } catch (_) {}
  toast(mosaicChoice === "live"
    ? "Mosaico en vídeo en vivo (fluido; ideal para pocas cámaras)"
    : "Mosaico en fotogramas (ligero; ideal para muchas cámaras)");
  // Cambio en sitio: las tejas se conservan; solo se sube/baja el vídeo.
  liveMosaic = mosaicChoice === "live";
  applyMosaicModeBtn();
  if (wallRunning) {
    if (!liveMosaic) {
      state.tiles.forEach((c) => { if (!_zoomActive(c.el)) dropLive(c); });
      autoLiveSet = new Set();
    }
    startWallFeeds();
    const big = enlargedCtrl();
    if (big) goLive(big);
  }
});

// ---- comandos de kiosco para el equipo cliente (Ajustes) ----
function fillKiosk() {
  const url = location.origin;   // p.ej. http://172.16.10.20:8120
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set("#kioskUrl", url);
  set("#kioskChrome",
    `"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --kiosk --start-fullscreen "${url}"`);
  set("#kioskEdge",
    `"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" --kiosk "${url}" --edge-kiosk-type=fullscreen --no-first-run`);
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch (_) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      const ok = document.execCommand("copy"); ta.remove(); return ok;
    } catch (_) { return false; }
  }
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  const el = $(b.dataset.copy);
  if (!el) return;
  copyText(el.textContent).then((ok) => {
    if (ok) { b.textContent = "Copiado ✓"; setTimeout(() => (b.textContent = "Copiar"), 1200); }
    else toast("Selecciona el texto y cópialo manualmente", true);
  });
});

// Un solo botón: En vivo (corriendo) ⇄ Pausado. Pausar corta fotogramas y WebRTC
// aunque estemos en mosaico en vivo; reanudar los reinicia.
$("#btnWallToggle").addEventListener("click", () => {
  setWallPaused(!wallUserPaused);
  if (!wallUserPaused && wallRunning) startWallFeeds();   // ya corría: reinicia el ritmo
  else syncWall();
});

// ---- menús emergentes (barra «Más» y menú «Más» de cada teja) ----
// Un menú es un botón (aria-haspopup) + un <div role="menu"> con elementos
// role="menuitem*". Se cierra con Escape, al elegir, al pulsar fuera o al abrir otro.
function closeMenus(except) {
  $$(".cw-menu:not([hidden])").forEach((m) => {
    if (m === except) return;
    m.hidden = true;
    const btn = m.previousElementSibling;
    if (btn && btn.hasAttribute("aria-expanded")) btn.setAttribute("aria-expanded", "false");
    m.closest(".tile")?.classList.remove("menu-open");
  });
}

function wireMenu(btn, menu) {
  const items = () => [...menu.querySelectorAll('[role^="menuitem"]:not([hidden])')];
  const close = (focusBtn) => {
    closeMenus();
    if (focusBtn) btn.focus();
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!menu.hidden) return close(true);
    closeMenus(menu);
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    btn.closest(".tile")?.classList.add("menu-open");
    items()[0]?.focus();
  });
  menu.addEventListener("click", () => closeMenus());   // elegir un elemento cierra
  menu.addEventListener("keydown", (e) => {
    const it = items(), i = it.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
    else if (e.key === "ArrowDown") { e.preventDefault(); it[(i + 1) % it.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); it[(i - 1 + it.length) % it.length]?.focus(); }
    else if (e.key === "Home") { e.preventDefault(); it[0]?.focus(); }
    else if (e.key === "End") { e.preventDefault(); it[it.length - 1]?.focus(); }
    else if (e.key === "Tab") closeMenus();
  });
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".cw-menu, [aria-haspopup='menu']")) closeMenus();
});
wireMenu($("#btnWallMore"), $("#wallMenu"));


// --------------------------------------------------------------- IA por cámara

const AI_KEY = "camway.ai.";
const VEHICLE_CLASSES = ["car", "truck", "bus", "motorcycle"];
const PERSON_CLASSES = ["person"];
const FIRE_CLASSES = ["fuego", "humo"];
// EPP: el modelo da "head" (cabeza descubierta) = persona SIN casco. "helmet"
// (con casco) y "vest" (chaleco) no alarman; solo interesa la ausencia.
const EPP_CLASSES = ["head"];
// Clases de condición EN CURSO (re-alarman mientras persistan, como el fuego):
// una persona sin casco quieta debe seguir avisando, no alarmar una sola vez.
const ONGOING_CLASSES = [...FIRE_CLASSES, ...EPP_CLASSES];
// El fuego es una emergencia EN CURSO: a diferencia de un carro estacionado
// (alarma una vez), re-alarma cada tanto mientras siga a la vista.
const FIRE_REALARM_MS = 20000;
const AI_CLEAR_MS = 8000;      // cuánto se mantiene la alarma tras el último acierto

function defaultAiConfig() {
  return {
    enabled: false, target: "vehiculo", action: "full", minScore: 0.4, zone: null,
    sound: true,
    schedule: { enabled: false, days: [], start: "21:00", end: "06:00" },
  };
}

const DAY_NAMES = { 0: "D", 1: "L", 2: "M", 3: "X", 4: "J", 5: "V", 6: "S" };

function toMinutes(hhmm) {
  const [h, m] = (hhmm || "0:0").split(":").map(Number);
  return h * 60 + m;
}

// ¿Está armada la cámara ahora, según su horario? Sin horario = siempre.
// Un rango que cruza medianoche (p.ej. 21:00→06:00) pertenece al día en que
// EMPIEZA: "Viernes 21:00-06:00" cubre la noche del viernes y la madrugada
// del sábado.
function isWithinSchedule(s, now = new Date()) {
  if (!s || !s.enabled || !s.days || s.days.length === 0) return true;
  const day = now.getDay();
  const cur = now.getHours() * 60 + now.getMinutes();
  const start = toMinutes(s.start), end = toMinutes(s.end);

  if (start === end) return s.days.includes(day);       // 24 h en los días marcados
  if (start < end) {
    return s.days.includes(day) && cur >= start && cur < end;
  }
  // Cruza medianoche.
  if (s.days.includes(day) && cur >= start) return true;        // antes de medianoche
  const prev = (day + 6) % 7;
  if (s.days.includes(prev) && cur < end) return true;          // madrugada del día siguiente
  return false;
}

function scheduleSummary(s) {
  if (!s || !s.enabled || !s.days || !s.days.length) return "Siempre (sin horario).";
  const dias = [1, 2, 3, 4, 5, 6, 0].filter((d) => s.days.includes(d))
    .map((d) => DAY_NAMES[d]).join(" ");
  const cruza = toMinutes(s.start) > toMinutes(s.end) ? " (cruza medianoche)" : "";
  return `Armada ${dias} de ${s.start} a ${s.end}${cruza}.`;
}

function refreshSchedTag(ctrl) {
  const s = ctrl.config.schedule;
  ctrl.el.classList.toggle("hassched", !!(s && s.enabled && s.days && s.days.length));
}

// Punto dentro de polígono (coordenadas normalizadas 0..1), por rayo horizontal.
function pointInPolygon(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0], yi = poly[i][1];
    const xj = poly[j][0], yj = poly[j][1];
    const cruza = (yi > y) !== (yj > y) &&
      x < ((xj - xi) * (y - yi)) / (yj - yi) + xi;
    if (cruza) inside = !inside;
  }
  return inside;
}

function refreshZoneTag(ctrl) {
  const has = Array.isArray(ctrl.config.zone) && ctrl.config.zone.length >= 3;
  ctrl.el.classList.toggle("haszone", has);
}

function loadAiConfig(streamId) {
  try {
    const raw = localStorage.getItem(AI_KEY + streamId);
    return raw ? { ...defaultAiConfig(), ...JSON.parse(raw) } : defaultAiConfig();
  } catch (_) {
    return defaultAiConfig();
  }
}

// Guarda la config en el servidor (fuente de verdad del detector server-side)
// y, solo si la aceptó, en este navegador. La promesa RECHAZA si el servidor no
// la guardó: quien la llama decide cómo avisar (nada de «IA activada» falso).
async function saveAiConfig(streamId, cfg) {
  await api(`/api/streams/${encodeURIComponent(streamId)}/ai`, {
    method: "PUT",
    body: JSON.stringify({
      enabled: cfg.enabled, target: cfg.target, min_score: cfg.minScore,
      action: cfg.action, zone: cfg.zone, schedule: cfg.schedule,
    }),
  });
  try { localStorage.setItem(AI_KEY + streamId, JSON.stringify(cfg)); } catch (_) {}
}

function serverCfgToLocal(s, streamId) {
  // El sonido es preferencia de ESTE navegador: se conserva de localStorage.
  const local = loadAiConfig(streamId);
  return {
    enabled: !!s.enabled, target: s.target || "vehiculo",
    action: s.action || "full", minScore: s.min_score ?? 0.4,
    zone: s.zone || null,
    sound: local.sound !== false,
    schedule: s.schedule || defaultAiConfig().schedule,
  };
}

// Trae del servidor la config de cada cámara (fuente de verdad) y la aplica.
async function applyServerConfigs() {
  let configs = {};
  try { configs = (await api("/api/ai-configs")).configs || {}; }
  catch (_) { return; }
  state.tiles.forEach((ctrl) => {
    const s = configs[ctrl.streamId];
    if (!s) return;
    ctrl.config = serverCfgToLocal(s, ctrl.streamId);
    refreshAiButton(ctrl);
    refreshZoneTag(ctrl);
    refreshSchedTag(ctrl);
    if (ctrl.config.enabled && !state.serverDetection) aiEngine.register(ctrl);
  });
}

function targetClasses(cfg) {
  if (cfg.target === "persona") return PERSON_CLASSES;
  if (cfg.target === "ambos") return [...VEHICLE_CLASSES, ...PERSON_CLASSES];
  if (cfg.target === "fuego") return FIRE_CLASSES;
  if (cfg.target === "epp") return EPP_CLASSES;
  return VEHICLE_CLASSES;
}

function classLabel(name) {
  return { car: "Auto", truck: "Camión", bus: "Bus", motorcycle: "Moto",
           person: "Persona", head: "Sin casco", helmet: "Con casco",
           vest: "Chaleco" }[name] || name;
}

function refreshAiButton(ctrl) {
  ctrl.aiBtn.classList.toggle("on", !!ctrl.config.enabled);
}

// Carga un <script> una sola vez; la promesa se comparte entre llamadas.
const scriptLoads = new Map();
function loadScriptOnce(src) {
  if (!scriptLoads.has(src)) {
    scriptLoads.set(src, new Promise((resolve, reject) => {
      const el = document.createElement("script");
      el.src = src;
      el.onload = resolve;
      el.onerror = () => {
        scriptLoads.delete(src);   // permite reintentar
        el.remove();
        reject(new Error("no cargó TensorFlow.js (¿sin conexión con el servidor?)"));
      };
      document.head.appendChild(el);
    }));
  }
  return scriptLoads.get(src);
}

// tf.min.js primero (coco-ssd lo necesita), luego coco-ssd. Si ya están en la
// página (p. ej. un index.html que aún los incluya) no se vuelven a pedir.
let tfLoading = null;
function loadTensorflow() {
  if (typeof cocoSsd !== "undefined") return Promise.resolve();
  if (!tfLoading) {
    tfLoading = (typeof tf !== "undefined" ? Promise.resolve() : loadScriptOnce("/static/vendor/tf.min.js"))
      .then(() => loadScriptOnce("/static/vendor/coco-ssd.min.js"))
      .catch((err) => { tfLoading = null; throw err; });
  }
  return tfLoading;
}

// El motor comparte un solo modelo y recorre las tejas activas en ronda, para
// no ahogar el navegador corriendo la detección de todas a la vez.
const aiEngine = {
  model: null,
  loading: null,
  active: new Set(),
  cursor: 0,
  timer: null,
  busy: false,

  async ensureModel() {
    if (this.model) return this.model;
    if (!this.loading) {
      // TensorFlow.js y coco-ssd (~15 MB) se bajan solo cuando alguien activa la
      // IA del navegador, una única vez (promesa compartida). Si falla, se
      // descarta la promesa para poder reintentar.
      this.loading = loadTensorflow()
        // Modelo alojado local (no baja de Google): 100% offline.
        .then(() => cocoSsd.load({ modelUrl: "/static/vendor/coco-ssd/model.json" }))
        .catch((err) => { this.loading = null; throw err; });
    }
    this.model = await this.loading;
    return this.model;
  },

  register(ctrl) {
    this.active.add(ctrl);
    this.start();
  },

  unregister(ctrl) {
    this.active.delete(ctrl);
    clearAlarm(ctrl);
    clearOverlay(ctrl);
    if (this.active.size === 0) this.stop();
  },

  start() {
    if (this.timer) return;
    this.timer = setInterval(() => this.tick(), 500);
  },

  stop() {
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
  },

  async tick() {
    if (this.busy || this.active.size === 0) return;
    // La expiración de alarmas la hace alarmSweeper (sobre TODAS las tejas,
    // también las del detector server-side).

    // Si el detector server-side está activo, él hace el trabajo: el navegador
    // no corre el modelo, solo reacciona a los eventos que llegan por SSE.
    if (state.serverDetection) return;

    const list = [...this.active].filter(
      (c) => c.player.active && c.video.readyState >= 2 && c.video.videoWidth > 0 &&
             isWithinSchedule(c.config.schedule)
    );
    if (list.length === 0) return;

    this.busy = true;
    const ctrl = list[this.cursor % list.length];
    this.cursor++;
    try {
      const model = await this.ensureModel();
      // Umbral interno bajo: COCO-SSD devuelve más candidatos y filtramos
      // nosotros según la confianza que eligió el usuario.
      const preds = await model.detect(ctrl.video, 20, 0.2);
      processDetections(ctrl, preds);
    } catch (err) {
      // Un fallo puntual de inferencia no debe frenar la ronda.
    } finally {
      this.busy = false;
    }
  },
};

// Procesa detecciones (vengan del modelo local o del detector server-side):
// pinta las cajas, filtra por clase/umbral/zona, maneja el reconocimiento y
// dispara la alarma. `preds` = [{class, score, bbox:[x,y,w,h en px del frame]}].
// El rastreo (alarma «solo si es nuevo») lo hace siempre el navegador, también
// con las cajas crudas que llegan del detector server-side.
function processDetections(ctrl, preds) {
  if (!ctrl.config.enabled) { clearOverlay(ctrl); return; }   // IA apagada: nada de cajas
  if (!isWithinSchedule(ctrl.config.schedule)) { clearOverlay(ctrl); return; }

  // Actividad = cualquier detección del modelo en esta cámara → candidata a
  // subir a vivo (auto-vivo B). Separado de la alarma (que filtra tipo/zona).
  if (preds && preds.length) ctrl.lastActivity = Date.now();

  const wanted = targetClasses(ctrl.config);
  const zone = ctrl.config.zone;
  const usaZona = Array.isArray(zone) && zone.length >= 3;
  const [vw, vh] = refDims(ctrl);

  drawDetections(ctrl, preds, wanted, zone, usaZona, vw, vh);

  const hits = preds.filter((p) => {
    if (!wanted.includes(p.class) || p.score < ctrl.config.minScore) return false;
    if (!usaZona) return true;
    const [bx, by, bw, bh] = p.bbox;
    return pointInPolygon((bx + bw / 2) / vw, (by + bh) / vh, zone);
  });

  const now = Date.now();

  // --- Rastreo en el navegador (por usuario), por IoU ---
  // Se empareja cada detección con el track existente que MÁS se solapa (IoU).
  // Un objeto estático siempre se solapa mucho consigo mismo aunque YOLO dibuje
  // el recuadro un poco distinto o la cámara tiemble con el viento → NO se
  // cuenta como nuevo. El track se refresca con la PRESENCIA (cualquier %), así
  // no expira mientras el objeto siga ahí. Solo alarma un objeto NUEVO que
  // además supera el umbral.
  ctrl.tracked = (ctrl.tracked || []).filter((t) => now - t.seen < TRACK_TIMEOUT);
  const present = preds
    .filter((p) => {
      if (!wanted.includes(p.class)) return false;
      if (!usaZona) return true;
      const [bx, by, bw, bh] = p.bbox;
      return pointInPolygon((bx + bw / 2) / vw, (by + bh) / vh, zone);
    })
    .map((h) => {
      const [bx, by, bw, bh] = h.bbox;
      return { class: h.class, x: bx / vw, y: by / vh, w: bw / vw, h: bh / vh, score: h.score };
    });
  ctrl.lastDetections = present;
  const nuevos = [];
  present.forEach((d) => {
    const above = d.score >= ctrl.config.minScore;
    let known = null, best = IOU_MATCH;
    ctrl.tracked.forEach((t) => {
      if (t.class !== d.class || t.used) return;
      const ov = iou(t, d);
      if (ov > best) { best = ov; known = t; }
    });
    const isOngoing = ONGOING_CLASSES.includes(d.class);
    if (known) {                       // sigue ahí: actualiza recuadro y marca visto
      known.x = d.x; known.y = d.y; known.w = d.w; known.h = d.h;
      known.seen = now; known.used = true;
      // Alarma la PRIMERA vez que este objeto supera el umbral (p.ej. entró
      // lejos al 20% y ahora está al 62%). Si ya alarmó, no re-alarma — SALVO
      // condiciones EN CURSO (fuego, sin casco), que re-alarman cada
      // FIRE_REALARM_MS mientras sigan presentes.
      const reFire = isOngoing && (now - (known.alarmedAt || 0) > FIRE_REALARM_MS);
      if (above && (!known.alarmed || reFire)) {
        known.alarmed = true; known.alarmedAt = now; nuevos.push(d);
      }
    } else {
      ctrl.tracked.push({ class: d.class, x: d.x, y: d.y, w: d.w, h: d.h,
                          seen: now, used: true, alarmed: above,
                          alarmedAt: above ? now : 0 });
      if (above) nuevos.push(d);       // nuevo Y sobre umbral
    }
  });
  ctrl.tracked.forEach((t) => { t.used = false; });   // liberar para el próximo frame
  if (nuevos.length) onDetection(ctrl, hits);
  // Objeto que sigue ahí (no nuevo): solo refresca la etiqueta; NO revive la
  // alarma, así la cámara ampliada vuelve a fotograma tras AI_CLEAR_MS.
  else if (ctrl.alarmed && hits.length) updateAlertLabel(ctrl, hits);
}

// Intersección sobre unión de dos recuadros normalizados {x,y,w,h}.
function iou(a, b) {
  const x1 = Math.max(a.x, b.x), y1 = Math.max(a.y, b.y);
  const x2 = Math.min(a.x + a.w, b.x + b.w), y2 = Math.min(a.y + a.h, b.y + b.h);
  const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
  const uni = a.w * a.h + b.w * b.h - inter;
  return uni > 0 ? inter / uni : 0;
}

// ---------------- detecciones del servidor (SSE) ----------------

function openDetectionStream() {
  if (state.detSse) state.detSse.close();
  const sse = new EventSource("/api/detections");
  state.detSse = sse;
  sse.onmessage = (ev) => {
    let e;
    try { e = JSON.parse(ev.data); } catch (_) { return; }
    const ctrl = state.tiles.get(e.stream_id);
    if (!ctrl) return;
    const [vw, vh] = refDims(ctrl);
    const preds = (e.boxes || []).map((b) => ({
      class: b.cls, score: b.score,
      bbox: [b.x * vw, b.y * vh, b.w * vw, b.h * vh],
    }));
    // El servidor envía cajas crudas; cada usuario aplica SU config (zona/tipo/
    // umbral/horario) y su propio rastreo para decidir la alarma.
    processDetections(ctrl, preds);
  };
  sse.onerror = () => { /* EventSource reintenta solo */ };
}

function updateAlertLabel(ctrl, hits) {
  const names = [...new Set(hits.map((h) => classLabel(h.class)))].join(", ");
  const best = Math.round(Math.max(...hits.map((h) => h.score)) * 100);
  ctrl.alertEl.querySelector("[data-alert-txt]").textContent = `${names} ${best}%`;
}

function onDetection(ctrl, hits) {
  ctrl.lastHit = Date.now();
  updateAlertLabel(ctrl, hits);

  if (ctrl.alarmed) return;   // ya en alarma; solo se refrescó lastHit
  ctrl.alarmed = true;

  if (ctrl.config.sound) playAlarm();

  const action = ctrl.config.action;
  if (action === "badge") {
    ctrl.el.classList.add("detecting");
  } else {
    ctrl.el.classList.add("alarm");
    // Maximiza salvo que el usuario tenga UN tile en pantalla completa manual.
    // En modo "ajustar a pantalla" sí maximiza (el fullscreen es de todo el muro).
    if (action === "full" && (state.fitMode || !document.fullscreenElement)) {
      maximizeTile(ctrl);
    }
  }
}

function clearAlarm(ctrl) {
  ctrl.alarmed = false;
  ctrl.el.classList.remove("alarm", "detecting");
  if (ctrl.autoMaxed) {
    ctrl.el.classList.remove("maximized");
    ctrl.autoMaxed = false;
    ctrl.player.setQuality(false);
    goSnap(ctrl);
  }
}

// Barrido de expiración de alarmas, sobre TODAS las tejas (funciona igual con
// detección server-side o local). Apaga la alarma tras AI_CLEAR_MS sin acierto
// o si se sale del horario.
let alarmSweeper = null;
function startAlarmSweeper() {
  if (alarmSweeper) return;
  alarmSweeper = setInterval(() => {
    const now = Date.now();
    state.tiles.forEach((c) => {
      if (!c.alarmed) return;
      if (!isWithinSchedule(c.config.schedule) || now - c.lastHit > AI_CLEAR_MS) clearAlarm(c);
    });
  }, 1000);
}

// Dimensiones de referencia para colocar el overlay: las del vídeo si está en
// vivo; si no, las del stream (el mosaico por fotograma no tiene videoWidth).
function refDims(ctrl) {
  return [ctrl.video.videoWidth || ctrl.vw || 16,
          ctrl.video.videoHeight || ctrl.vh || 9];
}

// Pasa una teja a vídeo WebRTC en vivo (al maximizar o con "Ver"). El fotograma
// sigue visible y refrescándose hasta que el vídeo tiene su primer frame, para
// que no haya pantalla negra durante el arranque de WebRTC.
function goLive(ctrl) {
  // Con el muro tapado no se abre vídeo, salvo que la IA del navegador lo necesite.
  if (ctrl.live || (wallOffscreen() && !keepLiveForAi(ctrl))) return;
  ctrl.live = true;
  ctrl.videoReady = false;
  ctrl.el.classList.add("live");
  ctrl.conn = "connecting";
  ctrl.player.start();
  refreshPlayBtn(ctrl.el, ctrl.title, ctrl.player);
  paintTileStatus(ctrl);
  waitVideoFrame(ctrl);
}

// Marca la teja como "vídeo listo" en cuanto se pinta el primer frame real,
// momento en que el CSS oculta el fotograma y deja ver el vídeo.
function waitVideoFrame(ctrl) {
  const v = ctrl.video;
  const done = () => {
    if (!ctrl.live || ctrl.videoReady) return;
    ctrl.videoReady = true;
    ctrl.el.classList.add("videoready");
    paintTileStatus(ctrl);
  };
  // Sondeo del primer frame REAL pintado (fiable en cualquier navegador y no se
  // pierde al cambiar el srcObject, cosa que sí le pasa a requestVideoFrameCallback).
  const t0 = Date.now();
  const poll = () => {
    if (!ctrl.live || ctrl.videoReady) return;
    if (v.videoWidth > 0 && v.readyState >= 2 && v.currentTime > 0) return done();
    if (Date.now() - t0 > 15000) return;   // se rinde tras 15 s (deja el fotograma)
    setTimeout(poll, 150);
  };
  poll();
}

// Devuelve una teja al mosaico por fotograma (corta el WebRTC).
function goSnap(ctrl) {
  // En mosaico EN VIVO no se baja a fotograma: se vuelve a vídeo ligero (lo).
  if (liveMosaic && !ZONE_ONLY) {
    ctrl.player.setQuality(false);
    if (!ctrl.live) goLive(ctrl);
    return;
  }
  if (!ctrl.live) return;
  dropLive(ctrl);
  bumpSnap(ctrl);   // refresca el fotograma de inmediato al volver
}

// Corta el vídeo WebRTC de una teja y la deja en modo fotograma (sin pedir uno).
function dropLive(ctrl) {
  if (!ctrl.live && !ctrl.player.active) return;
  ctrl.live = false;
  ctrl.videoReady = false;
  ctrl.el.classList.remove("live", "videoready");
  ctrl.player.stop();
  refreshPlayBtn(ctrl.el, ctrl.title, ctrl.player);
  paintTileStatus(ctrl);
}

// ---------------- refresco del mosaico por fotogramas ----------------
// Un solo temporizador refresca el <img> de todas las tejas que NO están en
// vivo. El servidor sirve el último JPEG desde memoria (compartido entre todos
// los espectadores), así que esto es baratísimo aunque miren varias personas.
let snapTimer = null;

// Un fotograma que no responde NO puede dejar la teja congelada y sin aviso:
// cada petición tiene su plazo; tras unos fallos seguidos la teja pasa a «sin
// conexión» y se reintenta más despacio hasta que la cámara vuelva.
const SNAP_TIMEOUT_MS = 8000;
const SNAP_FAILS_OFFLINE = 3;
const SNAP_RETRY_OFFLINE_MS = 5000;

// Teja visible en pantalla (con un margen): fuera de ella no se piden fotogramas.
// Un solo IntersectionObserver para todas las tejas; al entrar en pantalla se
// pide un fotograma de inmediato para no verla vieja.
const tileByEl = new WeakMap();
const tileObserver = typeof IntersectionObserver === "function"
  ? new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        const c = tileByEl.get(e.target);
        if (!c) return;
        const was = c.visible;
        c.visible = e.isIntersecting;
        if (c.visible && !was && wallRunning && !c.live) bumpSnap(c);
      });
    }, { rootMargin: "150px" })
  : null;

function observeTileVisibility(ctrl) {
  ctrl.visible = true;           // hasta que el observador diga lo contrario
  if (!tileObserver) return;
  tileByEl.set(ctrl.el, ctrl);
  tileObserver.observe(ctrl.el);
}

function bumpSnap(ctrl) {
  if (ctrl.videoReady) return;   // el vídeo ya tomó el relevo; no refrescar
  if (wallOffscreen()) return;   // muro tapado (otra pestaña / DVR): sin peticiones
  if (ctrl.visible === false && !_zoomActive(ctrl.el)) return;   // fuera de pantalla (scroll)
  const img = ctrl.snap;
  if (!img || !img.isConnected) return;  // teja no colocada (kiosco): no cuenta
  const t0 = performance.now();
  // Teja ya «sin conexión»: reintento espaciado, sin contar para el gobernador.
  if ((ctrl.snapFails || 0) >= SNAP_FAILS_OFFLINE && !ctrl.snapLoading &&
      t0 - (ctrl.snapLastTry || 0) < SNAP_RETRY_OFFLINE_MS) return;
  qAtt++;
  if (ctrl.snapLoading) { qSkip++; return; }  // petición en vuelo: iba atrasado (señal de presión)
  ctrl.snapLoading = true;
  ctrl.snapLastTry = t0;
  const next = new Image();
  let settled = false;
  const settle = (ok) => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    ctrl.snapLoading = false;
    if (ok) {
      img.src = next.src;
      qLatSum += performance.now() - t0; qLatN++;
      ctrl.snapFails = 0; ctrl.gotFrame = true; paintTileStatus(ctrl);
    } else {
      ctrl.snapFails = (ctrl.snapFails || 0) + 1;
      // El servidor da 503 cuando la cámara lleva >30s sin fotograma nuevo
      // (ver STALE_HIDE en wallcache). Tras unos fallos → "sin conexión".
      paintTileStatus(ctrl);        // tras SNAP_FAILS_OFFLINE fallos pasa a «Sin señal»
    }
  };
  // Sin respuesta a tiempo: se cancela la carga y cuenta como fallo.
  const timer = setTimeout(() => { next.onload = next.onerror = null; next.src = ""; settle(false); },
                           SNAP_TIMEOUT_MS);
  next.onload = () => settle(true);
  next.onerror = () => settle(false);
  const ww = qEffWidth();
  const w = ww ? `&w=${ww}` : "";
  next.src = `${ctrl.frameUrl}?t=${Date.now()}${w}`;
}

// Umbrales de LATENCIA de carga del fotograma (ms). Es la señal buena: refleja
// congestión real de red (Starlink saturado) o decode lento del cliente. NO se
// decide por "saltos": con muchas tejas el navegador solo abre ~6 conexiones a
// la vez, así que los saltos salen altos aunque la máquina esté sobrada.
const Q_DOWN_LAT = 700;   // latencia media alta → bajar calidad
const Q_UP_LAT = 400;     // latencia holgada → subir

// Cada ~4s: evalúa la latencia media y ajusta el nivel (con histéresis).
function qGovern() {
  const att = qAtt, sk = qSkip, n = qLatN, lat = n ? qLatSum / n : 0;
  qAtt = qSkip = qLatSum = qLatN = 0;
  if (n >= 4) lastJpegLat = lat;    // proxy de banda para el auto-vivo
  if (!autoQ || lightMode || !snapTimer || att < 8 || n < 4) return;
  const skipRate = sk / att;   // solo para el log (diagnóstico)
  const bad  = lat > Q_DOWN_LAT;
  const good = lat < Q_UP_LAT;
  if (bad) { qBad++; qGood = 0; } else if (good) { qGood++; qBad = 0; } else { qBad = qGood = 0; }
  let changed = false;
  if (qBad >= 3 && qLevel < Q_LEVELS.length - 1) { qLevel++; qBad = 0; changed = true; }
  else if (qGood >= 2 && qLevel > 0) { qLevel--; qGood = 0; changed = true; }
  if (changed) {
    if (snapTimer) {   // reiniciar el intervalo con el nuevo ritmo
      clearInterval(snapTimer);
      snapTimer = setInterval(snapTick, qEffMs());
    }
    applyLightBtn();
    console.log(`[calidad] ${Q_LEVELS[qLevel].name} (nivel ${qLevel}) skip=${skipRate.toFixed(2)} lat=${lat.toFixed(0)}ms`);
  }
}

// --- Auto-vivo por actividad (opción B) ---
let liveTimer = null;

function _activeCandidates(now) {
  const cands = [];
  state.tiles.forEach((c) => {
    if (c.el.classList.contains("maximized") || !c.el.isConnected) return;  // maximizada: la maneja el usuario/alarma
    const active = c.alarmed || (c.lastActivity && now - c.lastActivity < ACTIVITY_MS);
    if (active) cands.push(c);
  });
  // alarma primero, luego actividad más reciente
  cands.sort((a, b) => (b.alarmed === a.alarmed ? (b.lastActivity || 0) - (a.lastActivity || 0) : (b.alarmed ? 1 : -1)));
  return cands;
}

// Cada ~3s: mide salud de decode de los vivos, ajusta el presupuesto (AIMD) y
// promueve/degrada tejas según actividad.
function liveGovern() {
  if (!autoLive) {
    if (autoLiveSet.size) { liveBudget = 0; reconcileAutoLive(); }  // apagado: bajar todo
    return;
  }
  // 1) salud de decode de las tejas auto-vivas
  let dropped = 0, total = 0;
  autoLiveSet.forEach((sid) => {
    const c = state.tiles.get(sid);
    const v = c && c.video;
    if (!v || !v.getVideoPlaybackQuality) return;
    try {
      const q = v.getVideoPlaybackQuality();
      const prev = _lastVQ.get(sid) || { d: q.droppedVideoFrames, t: q.totalVideoFrames };
      dropped += Math.max(0, q.droppedVideoFrames - prev.d);
      total   += Math.max(0, q.totalVideoFrames   - prev.t);
      _lastVQ.set(sid, { d: q.droppedVideoFrames, t: q.totalVideoFrames });
    } catch (_) {}
  });
  const dropRatio = total > 10 ? dropped / total : 0;
  const bwTight = lastJpegLat > Q_DOWN_LAT;

  // 2) AIMD sobre el presupuesto de vivos
  if (dropRatio > 0.04 || bwTight) {            // problema → bajar a la mitad
    if (liveBudget > 0) { liveBudget = Math.floor(liveBudget / 2); console.log(`[auto-vivo] baja a ${liveBudget} (drop=${dropRatio.toFixed(2)} bwTight=${bwTight})`); }
    liveCooldown = 3;
  } else if (liveCooldown > 0) {
    liveCooldown--;
  } else if (_activeCandidates(Date.now()).length > liveBudget && liveBudget < LIVE_CAP) {
    liveBudget++;                               // hay demanda y va sano → subir de a una
    console.log(`[auto-vivo] sube a ${liveBudget}`);
  }
  reconcileAutoLive();
}

function reconcileAutoLive() {
  const now = Date.now();
  const cands = autoLive ? _activeCandidates(now) : [];
  const want = new Set(cands.slice(0, liveBudget).map((c) => c.streamId));
  // promover los elegidos que aún no están en vivo
  want.forEach((sid) => { const c = state.tiles.get(sid); if (c && !c.live) goLive(c); });
  // degradar los que estaban auto-vivos y ya no deben estarlo
  autoLiveSet.forEach((sid) => {
    if (want.has(sid)) return;
    const c = state.tiles.get(sid);
    if (c && c.live && !c.el.classList.contains("maximized") && !c.alarmed) goSnap(c);
    _lastVQ.delete(sid);
  });
  autoLiveSet = want;
}

// Refresco ROTATIVO: cada ciclo empieza por una teja distinta, así ninguna
// queda siempre de última en la cola de ~6 conexiones del navegador (antes las
// últimas del muro se refrescaban menos, sobre todo con el vivo cargando CPU).
let snapCursor = 0;

// ¿Hay una teja ampliada (maximizada en página o a pantalla completa del SO)?
function enlargedCtrl() {
  for (const c of state.tiles.values()) {
    if (c.el.classList.contains("maximized") || document.fullscreenElement === c.el) return c;
  }
  return null;
}

function snapTick() {
  // Con una teja ampliada, NO refrescar las demás: sus peticiones de fotograma
  // saturaban las ~6 conexiones del navegador y la CPU, y le quitaban fluidez
  // al vídeo en vivo de la ampliada (peor cuanto más atrás estuviera en la cola).
  const big = enlargedCtrl();
  if (big) { bumpSnap(big); return; }
  const arr = [...state.tiles.values()];
  const n = arr.length;
  if (!n) return;
  for (let i = 0; i < n; i++) bumpSnap(arr[(snapCursor + i) % n]);
  snapCursor = (snapCursor + 1) % n;
}

function startSnapRefresh() {
  stopSnapRefresh();
  snapTick();                                     // primer fotograma ya
  snapTimer = setInterval(snapTick, qEffMs());
  if (!qTimer) qTimer = setInterval(qGovern, 4000);     // gobernador de calidad JPEG
  if (!liveTimer) liveTimer = setInterval(liveGovern, 3000);  // gobernador de auto-vivo
}

function stopSnapRefresh() {
  if (snapTimer) { clearInterval(snapTimer); snapTimer = null; }
}

// Al volver a la pestaña (el navegador frena los temporizadores en segundo
// plano), reanudar el refresco del mosaico automáticamente — sin F5 ni Reanudar.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  // Solo si el muro debe estar corriendo: con el DVR abierto o en otra pestaña
  // NO se revive (antes arrancaba debajo del DVR).
  if (wallRunning && !wallOffscreen() && !wallUserPaused) startWallFeeds();
  else syncWall();
});

// ---------------- zoom digital (solo en la vista ampliada/pantalla completa) ----------------
// Amplía la imagen por software (recorte+escala): rueda del mouse para acercar,
// arrastrar para desplazar, pellizco en táctil. NO es zoom óptico — agranda los
// píxeles existentes. Reset al restaurar.
const ZOOM_MAX = 6;

function _zoomActive(el) {
  return el.classList.contains("maximized") || document.fullscreenElement === el;
}

function applyZoom(ctrl) {
  const z = ctrl.zoom;
  const t = z.s > 1.001 ? `translate(${z.x}px, ${z.y}px) scale(${z.s})` : "";
  [ctrl.video, ctrl.snap, ctrl.overlay].forEach((el) => {
    if (!el) return;
    el.style.transformOrigin = "0 0";
    el.style.transform = t;
  });
  ctrl.el.classList.toggle("zoomed", z.s > 1.001);
}

function clampZoom(ctrl) {
  const z = ctrl.zoom;
  const r = ctrl.el.getBoundingClientRect();
  z.x = Math.min(0, Math.max(r.width * (1 - z.s), z.x));
  z.y = Math.min(0, Math.max(r.height * (1 - z.s), z.y));
}

function resetZoom(ctrl) {
  if (!ctrl.zoom) return;
  ctrl.zoom.s = 1; ctrl.zoom.x = 0; ctrl.zoom.y = 0;
  applyZoom(ctrl);
}

function _zoomAt(ctrl, cx, cy, s1) {
  const z = ctrl.zoom, s0 = z.s;
  s1 = Math.min(ZOOM_MAX, Math.max(1, s1));
  if (s1 === s0) return;
  z.x = cx - (cx - z.x) * (s1 / s0);   // mantiene fijo el punto bajo el cursor
  z.y = cy - (cy - z.y) * (s1 / s0);
  z.s = s1;
  if (s1 <= 1.001) { z.s = 1; z.x = 0; z.y = 0; }
  clampZoom(ctrl);
  applyZoom(ctrl);
}

function setupZoom(ctrl) {
  const el = ctrl.el;
  el.addEventListener("wheel", (e) => {
    if (!_zoomActive(el)) return;
    e.preventDefault();
    const r = el.getBoundingClientRect();
    _zoomAt(ctrl, e.clientX - r.left, e.clientY - r.top,
            ctrl.zoom.s * (e.deltaY < 0 ? 1.2 : 1 / 1.2));
  }, { passive: false });

  const pts = new Map();
  let pan = null, pinch = null;
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  el.addEventListener("pointerdown", (e) => {
    if (!_zoomActive(el)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 2) {
      const p = [...pts.values()];
      pinch = { d: dist(p[0], p[1]), s: ctrl.zoom.s }; pan = null;
    } else if (pts.size === 1 && ctrl.zoom.s > 1.001) {
      pan = { x: e.clientX, y: e.clientY, ox: ctrl.zoom.x, oy: ctrl.zoom.y };
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
    }
  });
  el.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pts.size === 2) {
      const p = [...pts.values()], r = el.getBoundingClientRect();
      _zoomAt(ctrl, (p[0].x + p[1].x) / 2 - r.left, (p[0].y + p[1].y) / 2 - r.top,
              pinch.s * (dist(p[0], p[1]) / pinch.d));
    } else if (pan) {
      ctrl.zoom.x = pan.ox + (e.clientX - pan.x);
      ctrl.zoom.y = pan.oy + (e.clientY - pan.y);
      clampZoom(ctrl); applyZoom(ctrl);
    }
  });
  const up = (e) => {
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (pts.size === 0) pan = null;
  };
  el.addEventListener("pointerup", up);
  el.addEventListener("pointercancel", up);
}

function maximizeTile(ctrl) {
  // Una sola teja maximizada a la vez.
  state.tiles.forEach((c) => {
    if (c !== ctrl && c.autoMaxed) {
      c.el.classList.remove("maximized"); c.autoMaxed = false; resetZoom(c);
      c.player.setQuality(false); goSnap(c);
    }
  });
  ctrl.el.classList.add("maximized");
  ctrl.autoMaxed = true;
  ctrl.player.setQuality(true);   // 3K al ampliar
  goLive(ctrl);        // la teja ampliada sí muestra vídeo en vivo
}

// Dibuja sobre la teja lo que ve el modelo: verde = dispararía (tipo correcto,
// en zona, sobre el umbral); amarillo = detectado pero por debajo del umbral;
// gris = fuera de la zona. Más la zona en azul. Hace visible el porqué.
function drawDetections(ctrl, preds, wanted, zone, usaZona, vw, vh) {
  const cv = ctrl.overlay;
  if (!cv) return;
  const el = ctrl.el;
  const cw = el.clientWidth, ch = el.clientHeight;
  if (!cw || !ch) return;
  if (cv.width !== cw) cv.width = cw;
  if (cv.height !== ch) cv.height = ch;
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cw, ch);

  // El vídeo va con object-fit cover (rejilla) o contain (ampliada): se calcula el
  // rectángulo real según el que tenga ahora.
  const cover = getComputedStyle(ctrl.video).objectFit === "cover";
  const scale = cover ? Math.max(cw / vw, ch / vh) : Math.min(cw / vw, ch / vh);
  const dw = vw * scale, dh = vh * scale;
  const ox = (cw - dw) / 2, oy = (ch - dh) / 2;

  // La zona NO se pinta en la vista en vivo: solo se usa para filtrar. Se
  // dibuja únicamente en el editor de zona (modal).
  ctx.font = "11px sans-serif";
  ctx.textBaseline = "bottom";
  preds.forEach((p) => {
    if (!wanted.includes(p.class)) return;
    const [bx, by, bw, bh] = p.bbox;
    const cx = (bx + bw / 2) / vw, cy = (by + bh) / vh;
    const inzone = !usaZona || pointInPolygon(cx, cy, zone);
    if (!inzone) return;                       // fuera de la zona: no se dibuja
    const x = ox + bx * scale, y = oy + by * scale, w = bw * scale, h = bh * scale;
    const pass = p.score >= ctrl.config.minScore;
    const color = pass ? "#3fb950" : "#d29922";
    ctx.strokeStyle = color;
    ctx.lineWidth = pass ? 2.5 : 1.5;
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = color;
    ctx.fillText(`${classLabel(p.class)} ${Math.round(p.score * 100)}%`, x + 1, Math.max(11, y));
  });
}

function clearOverlay(ctrl) {
  const cv = ctrl.overlay;
  if (cv) cv.getContext("2d").clearRect(0, 0, cv.width, cv.height);
}

// Rastreo por IoU: dos recuadros son "el mismo objeto" si se solapan más que
// esto. Un objeto estático se solapa mucho consigo mismo aunque el recuadro
// varíe o la cámara tiemble; uno genuinamente nuevo aparece sin solaparse.
const IOU_MATCH = 0.3;
const TRACK_TIMEOUT = 60000; // cuánto se recuerda un objeto tras dejar de verse

// Descartar la alarma (doble-clic): vuelve al mosaico. El objeto ya está en la
// lista rastreada, así que no re-dispara mientras siga en el mismo sitio.
function dismissAlarm(ctrl) {
  ctrl.alarmed = false;
  ctrl.el.classList.remove("alarm", "detecting", "maximized");
  ctrl.autoMaxed = false;
  ctrl.player.setQuality(false);
  goSnap(ctrl);
}

// ---------------- modal de configuración ----------------

let aiModalCtrl = null;

function openAiModal(ctrl) {
  aiModalCtrl = ctrl;
  const cfg = ctrl.config;
  $("#aiCamName").textContent = ctrl.title;
  $("#aiEnabled").checked = cfg.enabled;
  $("#aiTarget").value = cfg.target;
  $("#aiAction").value = cfg.action;
  $("#aiScore").value = Math.round(cfg.minScore * 100);
  $("#aiScoreVal").textContent = Math.round(cfg.minScore * 100) + "%";
  $("#aiSound").checked = cfg.sound !== false;
  $("#aiModelState").textContent = aiEngine.model
    ? "Modelo listo."
    : "El modelo se baja al activar (una vez).";
  updateZoneState(ctrl);

  const s = cfg.schedule || defaultAiConfig().schedule;
  $("#aiSchedOn").checked = s.enabled;
  $("#aiStart").value = s.start;
  $("#aiEnd").value = s.end;
  $$("#aiDays button").forEach((b) => {
    b.classList.toggle("on", s.days.includes(Number(b.dataset.d)));
  });
  refreshSchedBody();

  $("#aiModal").hidden = false;
  loadAlertBox(ctrl.streamId);
}

// ---- dispositivo de WhatsApp que envía (solo admin): estado / desvincular / vincular ----
const WA_STATE_TXT = {
  open: ["on", "Vinculado"],
  connecting: ["", "Esperando vinculación…"],
  close: ["", "Sin dispositivo vinculado"],
  missing: ["", "Sin dispositivo vinculado"],
  offline: ["", "Evolution no responde"],
  unconfigured: ["", "WhatsApp no configurado en el servidor"],
};

async function loadWaCard() {
  const card = $("#waCard");
  if (!card) return;
  card.hidden = !isAdmin();
  if (!isAdmin()) return;
  let w;
  try { w = await api("/api/settings/whatsapp"); } catch (_) { return; }
  const [cls, txt] = WA_STATE_TXT[w.state] || ["", w.state];
  $("#waDot").className = "dot " + cls;
  $("#waTitle").textContent = w.state === "open"
    ? `WhatsApp vinculado: +${w.number || "?"}${w.name ? " · " + w.name : ""}`
    : `WhatsApp: ${txt}`;
  $("#waDetail").textContent = w.state === "open"
    ? "Este es el número que ENVÍA las alertas. Para cambiarlo: Desvincular y luego Vincular otro."
    : w.state === "offline" || w.state === "unconfigured"
      ? "Mientras no haya dispositivo, las alertas quedan en cola y salen al vincular."
      : "Sin dispositivo vinculado las alertas quedan en cola (hasta 6 h) y salen al vincular uno.";
  $("#waLink").hidden = w.state === "open" || w.state === "unconfigured" || w.state === "offline";
  $("#waUnlink").hidden = w.state !== "open";
}

let waTimers = [];
function waStopTimers() { waTimers.forEach(clearInterval); waTimers = []; }

function waCloseModal() {
  waStopTimers();
  $("#waModal").hidden = true;
  loadAlertPanel();   // refresca también la tarjeta de WhatsApp
}

async function waRefreshQr() {
  try {
    const r = await api("/api/settings/whatsapp/link", { method: "POST", timeout: 0, body: JSON.stringify({}) });
    if (r.state === "open") { toast("WhatsApp vinculado"); return waCloseModal(); }
    if (r.qr) {
      $("#waQr").src = r.qr; $("#waQr").hidden = false;
      $("#waQrMsg").textContent = "";
    } else {
      $("#waQr").hidden = true;
      $("#waQrMsg").textContent = "Esperando el código… (si tarda, cierra y vuelve a abrir)";
    }
  } catch (e) { $("#waQrMsg").textContent = e.message; }
}

async function waPoll() {
  try {
    const w = await api("/api/settings/whatsapp");
    $("#waState").textContent = w.state === "open" ? "¡Vinculado!" : "Esperando que escanees…";
    if (w.state === "open") {
      toast(`WhatsApp vinculado: +${w.number || ""}`);
      waCloseModal();
    }
  } catch (_) {}
}

$("#waLink").addEventListener("click", async () => {
  $("#waModal").hidden = false;
  $("#waQr").hidden = true; $("#waCode").hidden = true;
  $("#waQrMsg").textContent = "Generando código…"; $("#waState").textContent = "";
  waStopTimers();
  await waRefreshQr();
  waTimers.push(setInterval(waPoll, 3000));
  waTimers.push(setInterval(() => { if ($("#waCode").hidden) waRefreshQr(); }, 20000));
});
$("#waClose").addEventListener("click", waCloseModal);
$("#waModal").addEventListener("click", (e) => { if (e.target.id === "waModal") waCloseModal(); });

$("#waPair").addEventListener("click", async () => {
  const phone = $("#waPhone").value.trim();
  if (!phone) return toast("Escribe el número del celular que enviará las alertas", true);
  const btn = $("#waPair"); btn.disabled = true;
  try {
    const r = await api("/api/settings/whatsapp/link", { method: "POST", timeout: 0, body: JSON.stringify({ phone }) });
    if (r.pairing_code) {
      $("#waCode").textContent = r.pairing_code.replace(/(.{4})(?=.)/g, "$1-");
      $("#waCode").hidden = false;
      $("#waQrMsg").textContent = "En el celular: Dispositivos vinculados → Vincular con el número de teléfono → escribe este código.";
      $("#waQr").hidden = true;
    } else { toast("No se obtuvo código; usa el QR", true); }
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
});

$("#waUnlink").addEventListener("click", async () => {
  if (!(await uiConfirm("¿Desvincular el WhatsApp que envía las alertas?\n\n" +
      "Dejarán de salir avisos hasta que vincules otro dispositivo " +
      "(los pendientes esperan en cola). Los números destinatarios y las reglas se conservan.",
      { okLabel: "Desvincular", danger: true }))) return;
  const btn = $("#waUnlink"); btn.disabled = true;
  try {
    await api("/api/settings/whatsapp/unlink", { method: "POST" });
    toast("Dispositivo desvinculado");
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  loadAlertPanel();   // refresca también la tarjeta de WhatsApp
});

$("#btnSiteSave").addEventListener("click", async () => {
  const name = $("#siteName").value.trim();
  try {
    const r = await api("/api/settings", { method: "POST", body: JSON.stringify({ site_name: name }) });
    applySite(r.site_name || "");
    toast(name ? `Instancia identificada como «${r.site_name}»` : "Nombre de la instancia quitado");
  } catch (e) { toast(e.message, true); }
});

// ---- salud del equipo (Ajustes, solo admin; datos de Netdata) ----
function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

function hsTile(label, value, sub, pct, level) {
  const t = document.createElement("div");
  t.className = "hstile" + (level && level !== "ok" ? " " + level : "");
  const l = document.createElement("div"); l.className = "hsl"; l.textContent = label;
  const v = document.createElement("div"); v.className = "hsv"; v.textContent = value;
  t.append(l, v);
  if (pct != null) {
    const b = document.createElement("div"); b.className = "hsbar";
    const i = document.createElement("i"); i.style.width = Math.min(100, Math.max(0, pct)) + "%";
    b.append(i); t.append(b);
  }
  const s = document.createElement("div"); s.className = "hss"; s.textContent = sub;
  t.append(s);
  return t;
}

async function loadHealth() {
  const panel = $("#panelSalud");
  if (!panel || !isAdmin()) return;
  const sum = $("#hsSummary");
  let h;
  try { h = await api("/api/system/health"); }
  catch (e) { sum.textContent = "No se pudo consultar el estado del equipo: " + e.message; return; }
  $("#hsLink").href = `http://${location.hostname}:19999`;
  if (!h.ok) {
    sum.className = "hint hsstat-warn"; sum.textContent = h.error;
    $("#hsGrid").replaceChildren(); $("#hsTemps").replaceChildren(); $("#hsCont").hidden = true;
    return;
  }
  sum.className = "hint hsstat-" + h.status;
  sum.textContent = h.status === "ok"
    ? `✔ Equipo en buen estado · encendido hace ${fmtUptime(h.uptime_s)}`
    : `⚠ ${h.issues.join(" · ")}`;
  $("#hsGrid").replaceChildren(
    hsTile("CPU", h.cpu.pct.toFixed(0) + " %", `carga ${h.cpu.load.join(" / ")} · ${h.cpu.cores} núcleos`, h.cpu.pct, h.cpu.level),
    hsTile("Memoria", h.ram.pct.toFixed(0) + " %", `${h.ram.used_gb} de ${h.ram.total_gb} GB`, h.ram.pct, h.ram.level),
    hsTile("Disco", h.disk.pct.toFixed(0) + " %", `${h.disk.free_gb} GB libres de ${h.disk.total_gb}`, h.disk.pct, h.disk.level),
    hsTile("Red", `↓ ${h.net.down_mbps} Mb/s`, `↑ ${h.net.up_mbps} Mb/s`, null, "ok"),
  );
  $("#hsTemps").replaceChildren(...h.sensors.map((s) => {
    const c = document.createElement("span");
    c.className = "hschip" + (s.level !== "ok" ? " " + s.level : "");
    c.append(s.name);
    const b = document.createElement("b"); b.textContent = s.c.toFixed(0) + " °C"; c.append(b);
    return c;
  }));
  const tb = $("#hsCont tbody");
  tb.replaceChildren(...h.containers.map((c) => {
    const tr = document.createElement("tr");
    for (const txt of [c.name, c.cpu.toFixed(1) + " %", c.mem_mb + " MB"]) {
      const td = document.createElement("td"); td.textContent = txt; tr.append(td);
    }
    return tr;
  }));
  $("#hsCont").hidden = !h.containers.length;
}

// Sondeo único de Ajustes. Solo trabaja si la vista Ajustes está activa y la
// pestaña del navegador visible (en Mosaico no hay peticiones de Ajustes).
//   · salud del equipo: cada 5 s (solo admin; loadHealth lo comprueba)
//   · WhatsApp + destinatarios + log: cada 15 s, UNA consulta de WhatsApp por
//     ciclo (loadAlertPanel ya llama a loadWaCard) y sin pisar el modal de
//     vinculación, que tiene su propio sondeo.
const SETTINGS_TICK_MS = 5000;
const ALERTS_EVERY_MS = 15000;
let lastAlertsPoll = 0;

function settingsPollActive() {
  const v = $("#view-ajustes");
  return !!(v && v.classList.contains("active")) && !document.hidden;
}

function settingsPollTick(force = false) {
  if (!settingsPollActive()) return;
  loadHealth();
  const now = Date.now();
  if ((force || now - lastAlertsPoll >= ALERTS_EVERY_MS - 500) && $("#waModal").hidden) {
    loadAlertPanel({ poll: true });
  }
}
setInterval(settingsPollTick, SETTINGS_TICK_MS);
// Al volver a la pestaña del navegador, se pone al día enseguida.
document.addEventListener("visibilitychange", () => { if (!document.hidden) settingsPollTick(true); });

// ---- panel de destinatarios (Ajustes) ----
function fmtPhone(p) { return "+" + p; }

// Firma de lo que se pinta en la lista: si no cambió, no se reconstruye.
let alertPanelSig = "";
let alertPanelBusy = false;

// opts.poll = llamada del sondeo periódico: no repinta si nada cambió ni
// mientras el usuario tiene el foco dentro de la lista (lo haría perder).
async function loadAlertPanel(opts = {}) {
  const panel = $("#panelAlertas");
  if (!panel) return;
  panel.hidden = !canAlerts();
  if (!canAlerts()) return;
  if (opts.poll && alertPanelBusy) return;   // la anterior sigue en vuelo
  lastAlertsPoll = Date.now();               // cuenta cualquier carga para el ciclo de 15 s
  alertPanelBusy = true;
  try { await paintAlertPanel(opts); } finally { alertPanelBusy = false; }
}

async function paintAlertPanel(opts) {
  loadWaCard();   // única consulta de WhatsApp por ciclo
  let a;
  try { a = await api("/api/settings/alerts"); } catch (_) { return; }
  const act = a.recipients.filter((r) => r.enabled).length;
  const waWarn = a.wa_state && a.wa_state !== "open" && a.wa_state !== "unconfigured"
    ? "⚠ WhatsApp sin dispositivo vinculado: las alertas esperan en cola. Avisa al administrador. " : "";
  $("#alStatus").textContent = waWarn + (!a.channels.whatsapp && !a.channels.email
    ? "⚠ Todavía no hay destinatarios activos: agrega al menos un número."
    : `✔ ${act} número(s) activo(s)` + (a.channels.group ? " + un grupo fijo del servidor" : "") +
      (a.pending ? ` · ${a.pending} aviso(s) en cola esperando enlace` : ""));
  const list = $("#alList");
  const sig = JSON.stringify([a.recipients, (a.log || []).slice(0, 10), a.labels]);
  if (opts.poll && (sig === alertPanelSig || list.contains(document.activeElement))) return;
  alertPanelSig = sig;
  list.innerHTML = "";
  if (!a.recipients.length) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="hint">Sin números todavía.</span>';
    list.appendChild(li);
  }
  a.recipients.forEach((r) => {
    const li = document.createElement("li");
    const on = document.createElement("input");
    on.type = "checkbox"; on.checked = r.enabled; on.title = "Recibe alertas";
    on.addEventListener("change", async () => {
      try {
        await api(`/api/settings/alerts/recipients/${r.id}`, { method: "PUT", body: JSON.stringify({ enabled: on.checked }) });
        loadAlertPanel();
      } catch (e) { toast(e.message, true); on.checked = !on.checked; }
    });
    const nm = document.createElement("span"); nm.className = "uname"; nm.textContent = r.name;
    const ph = document.createElement("span"); ph.className = "ucount"; ph.textContent = fmtPhone(r.phone);
    const sp = document.createElement("span"); sp.className = "uspacer";
    const test = document.createElement("button"); test.className = "ghost small"; test.textContent = "Probar";
    test.addEventListener("click", async () => {
      test.disabled = true;
      try {
        await api(`/api/settings/alerts/recipients/${r.id}/test`, { method: "POST" });
        toast(`Mensaje de prueba enviado a ${r.name}`);
      } catch (e) { toast(e.message, true); } finally { test.disabled = false; }
    });
    const del = document.createElement("button"); del.className = "ghost small"; del.textContent = "Quitar";
    del.addEventListener("click", async () => {
      if (!(await uiConfirm(`¿Quitar a ${r.name} (${fmtPhone(r.phone)}) de las alertas?`, { okLabel: "Quitar", danger: true }))) return;
      try { await api(`/api/settings/alerts/recipients/${r.id}`, { method: "DELETE" }); loadAlertPanel(); }
      catch (e) { toast(e.message, true); }
    });
    [on, nm, ph, sp, test, del].forEach((el) => li.appendChild(el));
    list.appendChild(li);
  });
  const lg = $("#alLog");
  lg.innerHTML = "";
  (a.log || []).slice(0, 10).forEach((e) => {
    const li = document.createElement("li");
    const tx = document.createElement("span");
    tx.className = "ucount";
    tx.textContent = `${new Date(e.ts * 1000).toLocaleString()} · ${e.camera} · ` +
      `${(a.labels && a.labels[e.cls]) || e.cls}${e.test ? " (prueba)" : ""} — ${e.status}`;
    li.appendChild(tx); lg.appendChild(li);
  });
  if (!a.log || !a.log.length) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="hint">Aún no se ha enviado ningún aviso.</span>';
    lg.appendChild(li);
  }
}

$("#alAdd").addEventListener("click", async () => {
  const name = $("#alName").value.trim();
  const phone = $("#alPhone").value.trim();
  if (!phone) return toast("Escribe el número con código de país", true);
  const btn = $("#alAdd"); btn.disabled = true;
  try {
    const r = await api("/api/settings/alerts/recipients", { method: "POST", body: JSON.stringify({ name, phone }) });
    $("#alName").value = ""; $("#alPhone").value = "";
    toast(r.warning ? r.warning : `Número agregado: ${r.recipient.name}`, !!r.warning);
    loadAlertPanel();
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
});

// ---- avisos por WhatsApp / correo (regla del servidor; admin y seguridad) ----
async function loadAlertBox(streamId) {
  const box = $("#aiAlertBox");
  box.hidden = !canAlerts();
  if (!canAlerts()) return;
  $("#alState").textContent = "";
  try {
    const a = await api("/api/settings/alerts");
    const r = Object.assign({}, a.defaults, a.cameras[streamId] || {});
    $("#alEnabled").checked = !!r.enabled;
    $("#alHead").checked = r.classes.includes("head");
    $("#alFire").checked = r.classes.includes("fuego") || r.classes.includes("humo");
    $("#alHits").value = String(r.min_hits);
    $("#alCool").value = String(r.cooldown);
    const n = (a.recipients || []).filter((x) => x.enabled).length;
    const ch = [];
    if (a.channels.whatsapp) ch.push(`WhatsApp (${n} número(s)${a.channels.group ? " + grupo fijo" : ""})`);
    if (a.channels.email) ch.push("correo");
    $("#alChannels").textContent = ch.length
      ? `Se enviará por: ${ch.join(" y ")}.` +
        (a.pending ? ` ${a.pending} aviso(s) en cola esperando enlace.` : "")
      : "Sin destinatarios: agrega números en Ajustes → Destinatarios de alertas.";
    $("#alTest").disabled = !ch.length;
  } catch (e) {
    $("#alChannels").textContent = "No se pudo leer la configuración de avisos.";
  }
}

async function saveAlertRule(streamId) {
  if (!canAlerts() || $("#aiAlertBox").hidden) return;
  const classes = [];
  if ($("#alHead").checked) classes.push("head");
  if ($("#alFire").checked) classes.push("fuego", "humo");
  await api(`/api/settings/alerts/${encodeURIComponent(streamId)}`, {
    method: "PUT",
    body: JSON.stringify({
      enabled: $("#alEnabled").checked && classes.length > 0,
      classes: classes.length ? classes : ["head"],
      min_hits: Number($("#alHits").value),
      cooldown: Number($("#alCool").value),
    }),
  });
}

$("#alTest").addEventListener("click", async () => {
  if (!aiModalCtrl) return;
  const btn = $("#alTest");
  btn.disabled = true;
  $("#alState").textContent = "Enviando…";
  try {
    await api("/api/settings/alerts/test", {
      method: "POST", body: JSON.stringify({ stream_id: aiModalCtrl.streamId }),
    });
    $("#alState").textContent = "Prueba en cola: debe llegar en unos segundos.";
  } catch (e) {
    $("#alState").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});
["alHead", "alFire", "alHits", "alCool"].forEach((id) => {
  $("#" + id).addEventListener("change", () => { $("#alEnabled").checked = true; });
});

function readDays() {
  return $$("#aiDays button.on").map((b) => Number(b.dataset.d));
}

function refreshSchedBody() {
  const on = $("#aiSchedOn").checked;
  $("#aiSchedBody").classList.toggle("on", on);
  const sched = { enabled: on, days: readDays(),
                  start: $("#aiStart").value, end: $("#aiEnd").value };
  $("#aiSchedHint").textContent = scheduleSummary(sched);
}

function updateZoneState(ctrl) {
  const has = Array.isArray(ctrl.config.zone) && ctrl.config.zone.length >= 3;
  $("#aiZoneState").textContent = has
    ? `Zona definida (${ctrl.config.zone.length} puntos). Solo ahí se detecta.`
    : "Sin zona: se detecta en toda la imagen.";
}

function closeAiModal() {
  $("#aiModal").hidden = true;
  aiModalCtrl = null;
}

$("#aiScore").addEventListener("input", (e) => {
  $("#aiScoreVal").textContent = e.target.value + "%";
});

// Al marcar el sonido, suena una vez de prueba (y desbloquea el audio).
$("#aiSound").addEventListener("change", () => {
  if ($("#aiSound").checked) { unlockAudio(); setTimeout(playAlarm, 60); }
});

// Tocar cualquier ajuste implica que la quieres activar: se marca sola la
// casilla, para no caer en "guardé pero seguía desactivada".
["aiTarget", "aiAction", "aiScore"].forEach((id) => {
  $("#" + id).addEventListener("change", () => { $("#aiEnabled").checked = true; });
});

// Horario: días, franja y el interruptor "solo en un horario".
$("#aiSchedOn").addEventListener("change", () => {
  if ($("#aiSchedOn").checked) $("#aiEnabled").checked = true;
  refreshSchedBody();
});
$("#aiDays").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-d]");
  if (!b) return;
  b.classList.toggle("on");
  $("#aiEnabled").checked = true;
  refreshSchedBody();
});
["aiStart", "aiEnd"].forEach((id) => {
  $("#" + id).addEventListener("change", refreshSchedBody);
});
$("#aiClose").addEventListener("click", closeAiModal);
$("#aiModal").addEventListener("click", (e) => {
  if (e.target.id === "aiModal") closeAiModal();
});

$("#aiSave").addEventListener("click", async () => {
  if (!aiModalCtrl) return;
  const ctrl = aiModalCtrl;
  const btnSave = $("#aiSave");
  if (btnSave.disabled) return;   // ya hay un guardado en curso
  btnSave.disabled = true;
  const next = {
    enabled: $("#aiEnabled").checked,
    target: $("#aiTarget").value,
    action: $("#aiAction").value,
    minScore: Number($("#aiScore").value) / 100,
    sound: $("#aiSound").checked,
    zone: ctrl.config.zone || null,   // la zona se edita aparte; se conserva
    schedule: {
      enabled: $("#aiSchedOn").checked,
      days: readDays(),
      start: $("#aiStart").value,
      end: $("#aiEnd").value,
    },
  };
  try {
    await saveAiConfig(ctrl.streamId, next);
  } catch (err) {
    // El servidor no la guardó: la teja conserva su config anterior, no se
    // anuncia «IA activada» y el modal queda abierto para reintentar.
    toast("No se guardó la configuración de IA: " + err.message, true);
    btnSave.disabled = false;
    return;
  }
  ctrl.config = next;
  refreshAiButton(ctrl);
  refreshZoneTag(ctrl);
  refreshSchedTag(ctrl);
  try {
    await saveAlertRule(ctrl.streamId);
  } catch (err) {
    toast("No se guardaron los avisos: " + err.message, true);
  }

  if (ctrl.config.enabled) {
    if (state.serverDetection) {
      // El detector server-side la tomará (poll cada pocos segundos).
      toast(`IA activada en ${ctrl.title} (procesa el servidor)`);
    } else {
      const btn = $("#aiSave");
      btn.disabled = true;
      btn.textContent = "Preparando modelo…";
      try {
        await aiEngine.ensureModel();
        aiEngine.register(ctrl);
        goLive(ctrl);   // el análisis local necesita el vídeo corriendo
        toast(`IA activada en ${ctrl.title}`);
      } catch (err) {
        toast(err.message, true);
        ctrl.config.enabled = false;
        refreshAiButton(ctrl);
        try { await saveAiConfig(ctrl.streamId, ctrl.config); }
        catch (e2) { toast("No se pudo desactivar la IA en el servidor: " + e2.message, true); }
      } finally {
        btn.disabled = false;
        btn.textContent = "Guardar";
      }
    }
  } else {
    aiEngine.unregister(ctrl);
    toast(`IA desactivada en ${ctrl.title}`);
  }
  btnSave.disabled = false;
  closeAiModal();
});

// ---------------- editor de zona (trazado libre) ----------------

let zoneCtrl = null;
let zoneBg = null;          // snapshot congelado del frame
let zonePoints = [];        // puntos en píxeles del canvas
let zoneDrawing = false;
let zoneRestoreSnap = false; // la teja NO estaba en vivo antes de abrir el editor

// Cierra el editor, vuelve al modal de IA y devuelve la teja a fotograma si el
// editor la había subido a vídeo (si no, quedaba una conexión WHEP abierta).
function closeZoneEditor() {
  $("#zoneEditor").hidden = true;
  $("#aiModal").hidden = false;
  releaseZoneVideo();
  zoneBg = null;                 // libera el fotograma congelado
}

function releaseZoneVideo() {
  const c = zoneCtrl;
  if (zoneRestoreSnap && c && state.tiles.get(c.streamId) === c) goSnap(c);
  zoneRestoreSnap = false;
}

function waitFrame(video, ms) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const check = () => {
      if (video.videoWidth > 0) return resolve(true);
      if (Date.now() - t0 > ms) return resolve(false);
      setTimeout(check, 200);
    };
    check();
  });
}

async function openZoneEditor(ctrl) {
  zoneCtrl = ctrl;
  const canvas = $("#zoneCanvas");
  const video = ctrl.video;
  $("#zoneCamName").textContent = ctrl.title;

  if (!(video.videoWidth > 0)) {
    zoneRestoreSnap = !ctrl.live;
    goLive(ctrl);   // el editor dibuja sobre un fotograma real del vídeo
    const ok = await waitFrame(video, 6000);
    if (!ok) {
      toast("Conecta la cámara (botón Ver) para poder dibujar la zona", true);
      $("#aiModal").hidden = false;
      releaseZoneVideo();
      return;
    }
  }

  const maxW = Math.min(760, video.videoWidth);
  const scale = maxW / video.videoWidth;
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);

  // Fondo congelado para trazar con comodidad.
  zoneBg = document.createElement("canvas");
  zoneBg.width = canvas.width;
  zoneBg.height = canvas.height;
  zoneBg.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  // Ya hay fotograma congelado: no hace falta mantener el vídeo en vivo
  // detrás del editor (se baja ahora, no al guardar).
  releaseZoneVideo();

  zonePoints = (ctrl.config.zone || []).map(([x, y]) => [x * canvas.width, y * canvas.height]);
  drawZone();
  $("#zoneInfo").textContent = zonePoints.length >= 3
    ? `${zonePoints.length} puntos` : "Mantén presionado y traza el área.";
  $("#zoneEditor").hidden = false;
}

function drawZone() {
  const canvas = $("#zoneCanvas");
  const ctx = canvas.getContext("2d");
  if (zoneBg) ctx.drawImage(zoneBg, 0, 0);
  else { ctx.fillStyle = "#000"; ctx.fillRect(0, 0, canvas.width, canvas.height); }
  if (zonePoints.length < 1) return;

  ctx.beginPath();
  ctx.moveTo(zonePoints[0][0], zonePoints[0][1]);
  zonePoints.forEach((p) => ctx.lineTo(p[0], p[1]));
  if (!zoneDrawing && zonePoints.length >= 3) ctx.closePath();
  ctx.fillStyle = "rgba(47,155,255,.25)";
  ctx.strokeStyle = "#2f9bff";
  ctx.lineWidth = 2;
  if (!zoneDrawing && zonePoints.length >= 3) ctx.fill();
  ctx.stroke();
}

function zoneEventPoint(e) {
  const canvas = $("#zoneCanvas");
  const r = canvas.getBoundingClientRect();
  return [
    (e.clientX - r.left) * (canvas.width / r.width),
    (e.clientY - r.top) * (canvas.height / r.height),
  ];
}

function initZoneEditor() {
  const canvas = $("#zoneCanvas");

  canvas.addEventListener("pointerdown", (e) => {
    zoneDrawing = true;
    zonePoints = [zoneEventPoint(e)];
    canvas.setPointerCapture(e.pointerId);
    drawZone();
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!zoneDrawing) return;
    const p = zoneEventPoint(e);
    const last = zonePoints[zonePoints.length - 1];
    if (Math.hypot(p[0] - last[0], p[1] - last[1]) > 5) {
      zonePoints.push(p);
      drawZone();
    }
  });
  const finish = () => {
    if (!zoneDrawing) return;
    zoneDrawing = false;
    drawZone();
    $("#zoneInfo").textContent = zonePoints.length >= 3
      ? `${zonePoints.length} puntos` : "Muy pocos puntos: traza un área mayor.";
  };
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", finish);

  $("#zoneClear").addEventListener("click", () => {
    zonePoints = [];
    drawZone();
    $("#zoneInfo").textContent = "Sin zona: se detecta en toda la imagen.";
  });

  $("#zoneCancel").addEventListener("click", closeZoneEditor);

  $("#zoneSave").addEventListener("click", async () => {
    const canvas = $("#zoneCanvas");
    const previa = zoneCtrl.config.zone;
    if (zonePoints.length >= 3) {
      zoneCtrl.config.zone = zonePoints.map(([x, y]) => [x / canvas.width, y / canvas.height]);
    } else {
      zoneCtrl.config.zone = null;
    }
    try {
      await saveAiConfig(zoneCtrl.streamId, zoneCtrl.config);
    } catch (err) {
      // El servidor no la guardó: se conserva la zona anterior y el editor sigue
      // abierto para reintentar.
      zoneCtrl.config.zone = previa;
      toast("No se guardó la zona: " + err.message, true);
      return;
    }
    refreshZoneTag(zoneCtrl);
    updateZoneState(zoneCtrl);
    closeZoneEditor();
    toast(zoneCtrl.config.zone ? "Zona guardada" : "Zona borrada (toda la imagen)");
  });
}

// Abrir el editor de zona en una ventana aparte (más grande). Guarda por
// usuario en el servidor y avisa por postMessage para refrescar aquí.
$("#zoneNewWin").addEventListener("click", () => {
  if (!zoneCtrl) return;
  const url = `/static/zone.html?stream=${encodeURIComponent(zoneCtrl.streamId)}`
            + `&name=${encodeURIComponent(zoneCtrl.title || "")}`;
  window.open(url, "camway_zone_" + zoneCtrl.streamId, "width=1100,height=760");
});

window.addEventListener("message", async (e) => {
  if (!e.data || e.data.type !== "zone-saved") return;
  const ctrl = state.tiles.get(e.data.streamId);
  if (!ctrl) return;
  try {
    const cfg = await api(`/api/streams/${e.data.streamId}/ai`);
    ctrl.config = serverCfgToLocal(cfg, e.data.streamId);
    refreshZoneTag(ctrl); refreshAiButton(ctrl); refreshSchedTag(ctrl);
    toast("Zona actualizada desde la ventana");
  } catch (_) {}
});

$("#aiZoneBtn").addEventListener("click", () => {
  if (!aiModalCtrl) return;
  $("#aiModal").hidden = true;
  openZoneEditor(aiModalCtrl);
});

initZoneEditor();

// --------------------------------------------------------------- ajustes

// La sección «Credenciales a probar» + «Re-probar las que piden clave» se
// quitó de Ajustes: probar/reprobar credenciales en lote martilleaba las
// cámaras y disparaba el bloqueo anti-fuerza-bruta de las HiLook/Hikvision
// (vetan la IP del servidor). Las cámaras con clave se dan de alta una a una
// (su credencial propia o por URL en «Probar un equipo suelto»), sin barrido.

// --- OSD en la cámara (nombre + fecha/hora QUEMADOS en el video) ---
let osdDev = null;
function openOsdModal(dev) {
  osdDev = dev;
  $("#osdCamName").textContent = dev.name || dev.host;
  $("#osdName").value = dev.name || "";
  $("#osdShowName").checked = true;
  $("#osdShowTime").checked = true;
  $("#osdSetTime").checked = false;
  $("#osdNtp").value = "";
  $("#osdState").textContent = "";
  $("#osdSave").disabled = false;
  $("#osdModal").hidden = false;
}
function closeOsdModal() { $("#osdModal").hidden = true; osdDev = null; }
$("#osdClose")?.addEventListener("click", closeOsdModal);
$("#osdModal")?.addEventListener("click", (e) => { if (e.target.id === "osdModal") closeOsdModal(); });
$("#osdSave")?.addEventListener("click", async () => {
  if (!osdDev) return;
  const btn = $("#osdSave");
  btn.disabled = true;
  $("#osdState").textContent = "Aplicando en la cámara…";
  try {
    const res = await api(`/api/devices/${osdDev.id}/osd`, {
      method: "POST", timeout: 0,
      body: JSON.stringify({
        name: $("#osdName").value.trim(),
        show_name: $("#osdShowName").checked,
        show_time: $("#osdShowTime").checked,
        set_time: $("#osdSetTime").checked || !!$("#osdNtp").value.trim(),
        ntp: $("#osdNtp").value.trim(),
      }),
    });
    const via = (res.osd && res.osd.method) || (res.time && res.time.method) || "ok";
    $("#osdState").textContent = `Aplicado por ${via.toUpperCase()}.` +
      (res.errors && res.errors.length ? " Avisos: " + res.errors.join("; ") : "");
    toast("OSD aplicado en la cámara");
  } catch (err) {
    $("#osdState").textContent = err.message;
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

async function loadSettings() {
  const s = await api("/api/settings");
  $("#cidr").value = s.cidr;
  $("#channels").value = s.channels;
  $("#setPorts").value = s.ports.join(", ");
  $("#setConc").value = s.concurrency;
  $("#setTimeout").value = s.connect_timeout;
  state.logo = s.logo || "";
  showLogoPreview(state.logo);
  if ($("#siteName")) $("#siteName").value = s.site_name || "";
  applySite(s.site_name || "");
}

function showLogoPreview(dataUrl) {
  // Vista previa en Ajustes (el logo de la app va solo en la barra superior).
  for (const id of ["#logoPreview"]) {
    const img = $(id);
    if (!img) continue;
    if (dataUrl) { img.src = dataUrl; img.hidden = false; }
    else { img.hidden = true; img.removeAttribute("src"); }
  }
}

// ---- Calidad de vídeo/grabación (resolución, fps, bitrate) ----
let encState = null;

function encMbps(v) {
  if (!v) return 0;
  const s = String(v).toLowerCase();
  if (s.endsWith("k")) return parseFloat(s) / 1000;
  if (s.endsWith("m")) return parseFloat(s);
  return parseFloat(s) / 1e6;
}

async function loadEncoding() {
  try { encState = await api("/api/settings/encoding"); }
  catch (_) { return; }
  const e = encState.encoding || {};
  const res = `${e.max_width || 1280}x${e.max_height || 720}`;
  const rs = $("#encRes");
  if (rs) {
    if (![...rs.options].some((o) => o.value === res)) {
      const opt = document.createElement("option"); opt.value = res; opt.textContent = res;
      rs.appendChild(opt);
    }
    rs.value = res;
  }
  if ($("#encFps")) $("#encFps").value = (e.max_fps ?? 15);
  if ($("#encBitrate")) $("#encBitrate").value = e.max_bitrate || "";
  encEstimate();
}

function encEstimate() {
  const est = $("#encEstimate");
  if (!est || !encState) return;
  const mbps = encMbps($("#encBitrate").value);
  const cams = Math.max(1, encState.recording_cameras || 1);
  const disk = encState.disk;
  const freeGB = disk ? (disk.free / 1e9) : 0;
  if (!mbps) {
    est.textContent = `Bitrate automático: tamaño variable (no estimable). ${disk ? freeGB.toFixed(0) + " GB libres." : ""}`;
    return;
  }
  const usableGB = Math.max(0, freeGB - Math.min(freeGB * 0.1, 50));   // reserva SO/eventos/cache
  const perDayGB = (mbps + 0.05) * 86400 / 8 / 1000 * cams;            // +audio
  const days = perDayGB ? usableGB / perDayGB : 0;
  est.textContent = `Con ${cams} cámara(s) grabando a ${mbps} Mbps y ${freeGB.toFixed(0)} GB libres: `
    + `~${days.toFixed(0)} días de grabación continua (${perDayGB.toFixed(0)} GB/día). `
    + `Pon la retención de cada cámara ≤ ese número.`;
}

$("#encBitrate")?.addEventListener("change", encEstimate);
$("#encRes")?.addEventListener("change", encEstimate);
$("#btnSaveEnc")?.addEventListener("click", async () => {
  const [w, h] = $("#encRes").value.split("x");
  const body = {
    max_width: Number(w), max_height: Number(h),
    max_fps: Number($("#encFps").value) || 15,
    max_bitrate: $("#encBitrate").value || "",
  };
  $("#encHint").textContent = "Aplicando a todas las cámaras…";
  try {
    await api("/api/settings/encoding", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("#encHint").textContent = "Guardado y aplicado. El vivo de las cámaras transcodificadas se reconecta en unos segundos.";
    toast("Calidad de vídeo actualizada");
    loadEncoding();
  } catch (e) {
    $("#encHint").textContent = "Error: " + e.message;
  }
});

// Reduce la imagen a un tamaño chico (para no guardar un data URL enorme).
function resizeImage(file, maxW, maxH) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const r = Math.min(maxW / img.width, maxH / img.height, 1);
      const w = Math.round(img.width * r), h = Math.round(img.height * r);
      const cv = document.createElement("canvas");
      cv.width = w; cv.height = h;
      cv.getContext("2d").drawImage(img, 0, 0, w, h);
      resolve(cv.toDataURL("image/png"));
      URL.revokeObjectURL(img.src);
    };
    img.onerror = reject;
    img.src = URL.createObjectURL(file);
  });
}

$("#logoFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const dataUrl = await resizeImage(file, 400, 80);
    await api("/api/settings", { method: "POST", body: JSON.stringify({ logo: dataUrl }) });
    state.logo = dataUrl;
    showLogoPreview(dataUrl);
    $("#logoHint").textContent = "Logo guardado. Aparece en el modo pantalla completa.";
    toast("Logo guardado");
  } catch (err) {
    toast("No se pudo procesar la imagen", true);
  }
  e.target.value = "";
});

$("#btnLogoClear").addEventListener("click", async () => {
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ logo: "" }) });
  } catch (err) { return toast("No se pudo quitar el logo: " + err.message, true); }
  state.logo = "";
  showLogoPreview("");
  $("#logoHint").textContent = "Logo quitado.";
});

$("#btnSaveSettings").addEventListener("click", async () => {
  try {
    const ports = $("#setPorts").value.split(",")
      .map((p) => parseInt(p.trim(), 10)).filter(Boolean);
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        ports,
        concurrency: Number($("#setConc").value),
        connect_timeout: Number($("#setTimeout").value),
      }),
    });
    $("#settingsHint").textContent = "Guardado.";
    toast("Ajustes guardados");
  } catch (err) {
    toast(err.message, true);
  }
});

// --------------------------------------------------------------- grabaciones

let recDevices = [];
let recSelected = null;

async function loadRecDevices() {
  let data;
  try { data = await api("/api/recordings/devices"); }
  catch (_) { return; }
  if (!data.available && !data.devices.length) $("#recHint").textContent = "Blue Iris no está configurado.";
  recDevices = data.devices;
  if (!$("#recDate").value) $("#recDate").value = new Date().toISOString().slice(0, 10);
  if (!recDevices.length) {
    $("#recGrid").innerHTML = '<p class="hint">No tienes cámaras con grabaciones.</p>';
    $("#recCamName").textContent = "—";
    return;
  }
  // menú con miniatura por cámara
  $("#recCamMenu").innerHTML = recDevices.map((d, i) => `
    <div class="ts-item" data-i="${i}">
      <img src="${esc(d.thumb)}" alt="" onerror="this.style.visibility='hidden'">
      <span class="n">${esc(d.name)}</span>
    </div>`).join("");
  $$("#recCamMenu .ts-item").forEach((el) =>
    el.addEventListener("click", () => { recSelectDevice(recDevices[+el.dataset.i]); loadClips(); }));
  if (!recSelected || !recDevices.some((d) => d.device_id === recSelected.device_id))
    recSelectDevice(recDevices[0]);
}

function recSelectDevice(d) {
  recSelected = d;
  $("#recCamName").textContent = d.name;
  const th = $("#recCamThumb");
  if (d.thumb) { th.src = d.thumb; th.style.visibility = ""; } else th.style.visibility = "hidden";
  $("#recCamMenu").hidden = true;
  $$("#recCamMenu .ts-item").forEach((el) =>
    el.classList.toggle("sel", recDevices[+el.dataset.i]?.device_id === d.device_id));
}

$("#recCamTrigger").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#recCamMenu").hidden = !$("#recCamMenu").hidden;
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#recCamSel")) $("#recCamMenu").hidden = true;
});

async function loadClips() {
  const dev = recSelected && recSelected.device_id;
  const date = $("#recDate").value;
  const grid = $("#recGrid");
  if (!dev) return;
  $("#recHint").textContent = "Cargando…";
  grid.innerHTML = "";
  // Local si lo dice el server, o si la cámara no es de Blue Iris (sin bi_camera).
  if (recSelected.source === "local" || !recSelected.bi_camera)
    return loadLocalClips(dev, grid);
  let data;
  try { data = await api(`/api/recordings/${dev}/clips?date=${date}`); }
  catch (err) { $("#recHint").textContent = err.message; return; }
  $("#recHint").textContent = `${data.clips.length} grabación(es)`;
  if (!data.clips.length) { grid.innerHTML = '<p class="hint">Sin grabaciones ese día.</p>'; return; }
  data.clips.forEach((c) => grid.appendChild(recCard(dev, c)));
  // Cargar las miniaturas con concurrencia limitada: así el pool de conexiones
  // no se satura y la reproducción de un clip no queda encolada tras ellas.
  thumbQueue = [...grid.querySelectorAll("img[data-src]")];
  pumpThumbs();
}

// Grabaciones LOCALES (las que CamWay graba por su cuenta, no Blue Iris).
async function loadLocalClips(dev, grid) {
  let data;
  try { data = await api(`/api/recordings/${dev}`); }
  catch (err) { $("#recHint").textContent = err.message; return; }
  const recs = data.recordings || [];
  const mode = data.config && data.config.mode;
  $("#recHint").textContent = recs.length
    ? `${recs.length} grabación(es) locales${mode ? " · modo " + mode : ""}`
    : "Grabando localmente; aún no hay archivos (o grabación recién activada).";
  if (!recs.length) { grid.innerHTML = '<p class="hint">Sin grabaciones todavía.</p>'; return; }
  recs.forEach((r) => grid.appendChild(localRecCard(dev, r)));
}

function localRecCard(dev, r) {
  const el = document.createElement("div");
  el.className = "reccard";
  const url = `/api/recordings/${dev}/file?stream=${encodeURIComponent(r.stream)}&name=${encodeURIComponent(r.name)}&kind=${r.kind}`;
  const tag = r.kind === "event" ? '<span class="alert" title="Clip de evento">🔔</span>' : "";
  const label = r.kind === "event" ? "evento" : "continuo";
  el.innerHTML = `
    <div class="recthumb norec">
      <span class="play">▶</span>${tag}
      <span class="dur">${label}</span>
    </div>
    <div class="recmeta"><div class="t">${esc(fmtRecTime(r.mtime))}</div><div class="s">${esc(fmtSize(r.size))}</div></div>`;
  el.addEventListener("click", () =>
    playRecUrl(`${recSelected.name} — ${fmtRecTime(r.mtime)}`, url));
  return el;
}

let thumbQueue = [];
let thumbActive = 0;
const THUMB_CONCURRENCY = 4;
function pumpThumbs() {
  while (thumbActive < THUMB_CONCURRENCY && thumbQueue.length) {
    const img = thumbQueue.shift();
    thumbActive++;
    img.addEventListener("load", onThumbDone, { once: true });
    img.addEventListener("error", onThumbDone, { once: true });
    img.src = img.dataset.src;
  }
}
function onThumbDone() { thumbActive--; pumpThumbs(); }

function fmtClipTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function recCard(dev, c) {
  const el = document.createElement("div");
  el.className = "reccard";
  const dur = (c.size || "").split("(")[0].trim();
  const alert = (c.flags & 2) ? '<span class="alert" title="Con alerta">🔔</span>' : "";
  const thumb = `/api/recordings/${encodeURIComponent(dev)}/thumb?path=${encodeURIComponent(c.path)}`;
  el.innerHTML = `
    <div class="recthumb">
      <img data-src="${esc(thumb)}" alt="" decoding="async" onerror="this.style.display='none'">
      <span class="play">▶</span>${alert}
      <span class="dur">${esc(dur)}</span>
    </div>
    <div class="recmeta"><div class="t">${esc(fmtClipTime(c.date))}</div><div class="s">${esc(c.size)}</div></div>`;
  el.addEventListener("click", () => openRecPlayer(dev, c));
  return el;
}

function openRecPlayer(dev, c) {
  const url = `/api/recordings/${dev}/video.mp4?path=${encodeURIComponent(c.path)}`;
  $("#recPlayTitle").textContent =
    `${recSelected?.name || ""} — ${fmtClipTime(c.date)}`;
  $("#recPlayInfo").textContent = "Preparando vídeo…  " + (c.size || "");
  const v = $("#recVideo");
  v.pause(); v.removeAttribute("src"); v.load();   // limpiar carga previa colgada
  v.onplaying = () => { $("#recPlayInfo").textContent = c.size || ""; };
  v.onerror = () => { $("#recPlayInfo").textContent = "No se pudo reproducir esta grabación."; };
  $("#recDownload").href = url;
  $("#recPlayer").hidden = false;
  v.src = url;
  v.load();
  v.play().catch(() => {});
}

function closeRecPlayer() {
  const v = $("#recVideo");
  v.pause(); v.removeAttribute("src"); v.load();
  $("#recPlayer").hidden = true;
}

$("#recLoad").addEventListener("click", loadClips);
$("#recPlayClose").addEventListener("click", closeRecPlayer);
$("#recPlayer").addEventListener("click", (e) => { if (e.target.id === "recPlayer") closeRecPlayer(); });

// ------------------------------------- grabación local (opt-in por cámara) ---
function playRecUrl(title, url) {
  $("#recPlayTitle").textContent = title;
  $("#recPlayInfo").textContent = "Preparando vídeo…";
  const v = $("#recVideo");
  v.pause(); v.removeAttribute("src"); v.load();
  v.onplaying = () => { $("#recPlayInfo").textContent = ""; };
  v.onerror = () => { $("#recPlayInfo").textContent = "No se pudo reproducir."; };
  $("#recDownload").href = url;
  $("#recPlayer").hidden = false;
  v.src = url; v.load(); v.play().catch(() => {});
}

function fmtRecTime(sec) {
  try { return new Date(sec * 1000).toLocaleString("es-VE"); }
  catch { return new Date(sec * 1000).toLocaleString(); }
}
function fmtSize(b) {
  if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
  if (b >= 1e6) return (b / 1e6).toFixed(0) + " MB";
  return (b / 1e3).toFixed(0) + " KB";
}

async function openRecModal(deviceId, title, recBtn) {
  let data = { recordings: [], config: null };
  try { data = await api(`/api/recordings/${deviceId}`); }
  catch (e) { toast("Grabaciones: " + e.message, true); }
  const cfg = data.config || {};
  const on = !!data.config;

  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = `
    <div class="modalcard">
      <div class="modalhead">
        <h3>Grabación local — ${esc(title)}</h3>
        <button class="ghost small" data-close>Cerrar</button>
      </div>
      <div class="aibody">
        <label class="switch"><input type="checkbox" id="recEnabled" ${on ? "checked" : ""}>
          <span>Grabar esta cámara</span></label>
        <p class="hint">Por defecto NO se graba. Al activarla, la cámara queda
          conectada 24/7 (consume CPU y disco). Borra sola lo más viejo.</p>
        <div class="recfields">
          <label>Modo
            <select id="recMode">
              <option value="hybrid"${cfg.mode !== "continuous" ? " selected" : ""}>Híbrido: continuo + clips de evento</option>
              <option value="continuous"${cfg.mode === "continuous" ? " selected" : ""}>Solo continuo 24/7</option>
            </select>
          </label>
          <label>Retención continuo (días)
            <input type="number" id="recContDays" min="1" max="60" value="${esc(cfg.cont_days || 3)}">
          </label>
          <label id="recEvWrap">Retención clips de evento (días)
            <input type="number" id="recEventDays" min="1" max="90" value="${esc(cfg.event_days || 7)}">
          </label>
        </div>
        <div class="reclist" id="recList2">
          ${data.recordings.length ? "" : '<p class="hint">Aún no hay grabaciones.</p>'}
        </div>
      </div>
      <div class="aifoot">
        <span class="hint" id="recSaveHint"></span>
        <button class="primary" data-save>Guardar</button>
      </div>
    </div>`;
  document.body.appendChild(modal);

  const list = modal.querySelector("#recList2");
  for (const r of data.recordings) {
    const row = document.createElement("div");
    row.className = "recrow";
    const tag = r.kind === "event" ? '<span class="evtag">evento</span>' : "";
    const url = `/api/recordings/${deviceId}/file?stream=${encodeURIComponent(r.stream)}&name=${encodeURIComponent(r.name)}&kind=${r.kind}`;
    row.innerHTML = `<span class="rt">▶ ${esc(fmtRecTime(r.mtime))} ${tag}</span><span class="rs">${esc(fmtSize(r.size))}</span>`;
    row.addEventListener("click", () => playRecUrl(`${title} — ${fmtRecTime(r.mtime)}`, url));
    list.appendChild(row);
  }

  const close = () => modal.remove();
  modal.querySelector("[data-close]").addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });

  const syncMode = () => {
    const hy = modal.querySelector("#recMode").value === "hybrid";
    modal.querySelector("#recEvWrap").style.display = hy ? "" : "none";
  };
  modal.querySelector("#recMode").addEventListener("change", syncMode);
  syncMode();

  modal.querySelector("[data-save]").addEventListener("click", async () => {
    const enabled = modal.querySelector("#recEnabled").checked;
    const body = {
      enabled,
      mode: modal.querySelector("#recMode").value,
      cont_days: Number(modal.querySelector("#recContDays").value) || 3,
      event_days: Number(modal.querySelector("#recEventDays").value) || 7,
    };
    modal.querySelector("#recSaveHint").textContent = "Guardando…";
    try {
      await api(`/api/recordings/${deviceId}/config`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (recBtn) recBtn.classList.toggle("recon", enabled);
      toast(enabled ? "Grabación activada" : "Grabación apagada");
      close();
    } catch (e) {
      modal.querySelector("#recSaveHint").textContent = "";
      toast("No se pudo guardar: " + e.message, true);
    }
  });
}

// --------------------------------------------------------------- autenticación

function showAuth(mode) {
  $("#authOverlay").hidden = false;
  $("#loginForm").hidden = mode !== "login";
  $("#setupForm").hidden = mode !== "setup";
  setTimeout(() => $(mode === "setup" ? "#setUser" : "#logUser")?.focus(), 60);
}

// Nombre del taladro/sitio: encabezado, mosaico, login, pestaña y vista DVR.
function applySite(name) {
  name = (name || "").trim();
  state.site = name;
  document.body.dataset.site = name;                 // lo lee dvr.js
  document.title = name ? `${name} · CamWay` : "CamWay";
  for (const id of ["#siteBadge", "#wallSite", "#authSite"]) {
    const el = $(id);
    if (!el) continue;
    el.textContent = name;
    el.hidden = !name;
  }
  window.CamWayDVR?.refreshTitle?.();
}

async function checkAuth() {
  let me;
  try { me = await api("/api/me"); }
  catch (_) { me = { authenticated: false, setup_needed: false }; }
  applySite(me.site);
  if (me.logo) {                       // logo (si hay) en login + cabecera
    const l = $("#authLogo"); l.src = me.logo; l.hidden = false;
    const t = $("#authTitle"); if (t) t.hidden = true;   // el logo ya dice "CamWay": sin texto redundante
    // cabecera: el logo en vez del cuadro + texto "CamWay"
    const bl = $("#brandLogo"); if (bl) { bl.src = me.logo; bl.hidden = false; }
    const bm = $("#brandMark"); if (bm) bm.hidden = true;
    const bt = $("#brandTitle"); if (bt) bt.hidden = true;
    state.logo = me.logo;
  }
  if (me.setup_needed) { showAuth("setup"); return false; }
  if (!me.authenticated) { showAuth("login"); return false; }
  state.user = me.user;
  return true;
}

function isAdmin() { return state.user && state.user.role === "admin"; }
// Admin y jefe de seguridad gestionan los avisos por WhatsApp.
function canAlerts() {
  const r = state.user && state.user.role;
  return r === "admin" || r === "seguridad";
}

function applyRole() {
  const u = state.user;
  $("#userBox").hidden = false;
  $("#userName").textContent = u.username;
  const admin = u.role === "admin";
  $$("[data-admin]").forEach((el) => { el.style.display = admin ? "" : "none"; });
  if (u.role === "seguridad") {        // jefe de seguridad: Ajustes = solo los avisos
    const t = $('.tab[data-view="ajustes"]'); if (t) t.style.display = "";
    $$("#view-ajustes > section.panel").forEach((p) => { p.hidden = p.id !== "panelAlertas"; });
  }
  if (!admin) {                       // viewer: solo mosaico
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".view").forEach((v) => v.classList.remove("active"));
    $('.tab[data-view="mosaico"]').classList.add("active");
    $("#view-mosaico").classList.add("active");
  }
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#loginErr").hidden = true;
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({
      username: $("#logUser").value.trim(), password: $("#logPass").value }) });
    location.reload();
  } catch (err) { $("#loginErr").textContent = err.message; $("#loginErr").hidden = false; }
});

$("#setupForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#setupErr").hidden = true;
  try {
    await api("/api/setup", { method: "POST", body: JSON.stringify({
      username: $("#setUser").value.trim(), password: $("#setPass").value }) });
    location.reload();
  } catch (err) { $("#setupErr").textContent = err.message; $("#setupErr").hidden = false; }
});

$("#btnLogout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_) {}
  location.reload();
});

// --------------------------------------------------------------- usuarios (admin)

function camLabel(dev) {
  return dev.name || `${dev.host}${dev.vendor && dev.vendor !== "desconocido" ? " · " + dev.vendor : ""}`;
}

// Lista de cámaras para asignar a un usuario. Se vuelve a pedir cada vez que
// se abre el selector (una cámara nueva aparece sin recargar la página); si la
// petición falla se usa la última lista conocida.
async function deviceList() {
  try { state.allDevices = (await api("/api/devices")).devices; }
  catch (_) { state.allDevices = state.allDevices || []; }
  return state.allDevices;
}

async function buildCamChecks(container, selected = []) {
  const sel = new Set(selected);
  const devs = await deviceList();
  container.innerHTML = devs.map((d) => {
    const pub = (d.streams || []).find((s) => s.published);
    const thumb = pub ? `/api/wall/${encodeURIComponent(pub.id)}/frame` : "";   // fotograma cacheado
    return `
    <label class="camcheck">
      <span class="camthumb">${thumb
        ? `<img src="${esc(thumb)}" alt="" onerror="this.remove()">`
        : ""}<span class="noimg">sin vista</span></span>
      <span class="camrow">
        <input type="checkbox" value="${esc(d.id)}" ${sel.has(d.id) ? "checked" : ""}>
        <span class="camname">${esc(camLabel(d))}</span>
      </span>
    </label>`;
  }).join("") || '<span class="hint">Escanea o agrega cámaras primero.</span>';
}

// querySelectorAll dentro de un nodo concreto
function $$in(node, sel) { return Array.from(node.querySelectorAll(sel)); }

$("#btnAddUser").addEventListener("click", async () => {
  const username = $("#uName").value.trim();
  const password = $("#uPass").value;
  const role = $("#uRole").value;
  if (!username || password.length < 4) return toast("Usuario y contraseña (≥4)", true);
  try {
    // Se crea sin cámaras; se asignan luego con el botón «Cámaras» de su fila.
    await api("/api/users", { method: "POST", body: JSON.stringify({ username, password, role, cameras: [] }) });
    $("#uName").value = ""; $("#uPass").value = "";
    toast(role === "viewer"
      ? `Viewer ${username} creado — asígnale cámaras con «Cámaras»`
      : `Usuario ${username} creado`);
    loadUsers();
  } catch (err) { toast(err.message, true); }
});

async function loadUsers() {
  const list = $("#userList");
  if (!list) return;
  let data;
  try { data = await api("/api/users"); } catch (_) { return; }
  list.innerHTML = "";
  data.users.forEach((u) => list.appendChild(userRow(u)));
}

function userRow(u) {
  const li = document.createElement("li");
  const admin = u.role === "admin";
  li.innerHTML = `
    <span class="uname">${esc(u.username)}</span>
    <span class="urole ${admin ? "admin" : ""}">${esc(u.role)}</span>
    <span class="ucount">${admin ? "todas las cámaras" : u.cameras.length + " cámara(s)"}</span>
    <span class="uspacer"></span>
    ${admin ? "" : '<button class="ghost small" data-edit>Cámaras</button>'}
    <button class="ghost small" data-pass>Clave</button>
    <button class="ghost small" data-del>Borrar</button>`;

  const editBtn = li.querySelector("[data-edit]");
  if (editBtn) editBtn.addEventListener("click", async () => {
    // Un solo contenedor (lista + botón Guardar): plegar lo quita entero y
    // desplegar de nuevo no acumula botones.
    const open = li.querySelector(".rowedit");
    if (open) { open.remove(); return; }
    if (editBtn.disabled) return;           // ya se está abriendo
    editBtn.disabled = true;
    const wrap = document.createElement("div");
    wrap.className = "rowedit";
    const box = document.createElement("div");
    box.className = "camchecks";
    const save = document.createElement("button");
    save.className = "primary small rowsave"; save.textContent = "Guardar cámaras";
    wrap.append(box, save);
    try { await buildCamChecks(box, u.cameras); }
    finally { editBtn.disabled = false; }
    li.appendChild(wrap);
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await api(`/api/users/${u.id}`, { method: "PUT",
          body: JSON.stringify({ cameras: $$in(box, "input:checked").map((c) => c.value) }) });
        toast("Cámaras actualizadas"); loadUsers();
      } catch (err) { toast(err.message, true); save.disabled = false; }
    });
  });

  li.querySelector("[data-pass]").addEventListener("click", async () => {
    const pass = await uiPrompt(`Nueva contraseña para ${u.username} (≥4):`, { type: "password", okLabel: "Cambiar" });
    if (!pass) return;
    if (pass.length < 4) return toast("Mínimo 4 caracteres", true);
    try { await api(`/api/users/${u.id}`, { method: "PUT", body: JSON.stringify({ password: pass }) });
      toast("Contraseña cambiada"); } catch (err) { toast(err.message, true); }
  });

  li.querySelector("[data-del]").addEventListener("click", async () => {
    if (!(await uiConfirm(`¿Borrar al usuario ${u.username}?`, { okLabel: "Borrar", danger: true }))) return;
    try { await api(`/api/users/${u.id}`, { method: "DELETE" }); toast("Usuario borrado"); loadUsers(); }
    catch (err) { toast(err.message, true); }
  });
  return li;
}

// --------------------------------------------------------------- arranque

function enterZoneKioskChrome() {
  // Ventana dedicada de una zona: sin barras ni controles, solo el mosaico.
  document.body.classList.add("zoneonly");
  const tb = $(".topbar"); if (tb) tb.style.display = "none";
  const wb = $(".cw-wallbar"); if (wb) wb.style.display = "none";
  const wh = $("#wallHint"); if (wh) wh.style.display = "none";
  const wf = $("#wallFoot"); if (wf) wf.style.display = "none";
  $$(".view").forEach((v) => v.classList.remove("active"));
  const m = $("#view-mosaico"); if (m) m.classList.add("active");
}

(async function init() {
  const vacio = $("#emptyWall");
  if (vacio) vacio.hidden = true;     // no parpadear «sin streams» antes de cargar
  if (!(await checkAuth())) return;   // muestra login/setup y se detiene
  $("#authOverlay").hidden = true;
  if (ZONE_ONLY) enterZoneKioskChrome();
  applyRole();
  await pollHealth();
  setInterval(pollHealth, 8000);
  await loadSettings();
  if (!ZONE_ONLY && isAdmin()) { await loadDevices(); loadUsers(); }
  // Primero se decide si arranca el DVR (autoStart marca body.dvr-open antes de
  // volver); luego se carga el muro, que solo corre si no hay DVR encima.
  if (!ZONE_ONLY) await window.CamWayDVR?.autoStart?.();
  loadWall();                          // el mosaico (el servidor filtra por permiso)
})();
