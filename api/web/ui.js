/* CamWay — utilidades de interfaz compartidas: diálogos y gestor de modales.
 *
 * Contrato (ver la nota «Panel web — contrato» de la vault):
 *   uiPrompt(mensaje, { value, placeholder, type, okLabel })  -> Promise<string|null>
 *       type: "text" (por defecto) o "password". null = cancelado.
 *   uiConfirm(mensaje, { okLabel, danger })                    -> Promise<boolean>
 *
 * Además, un gestor de modales (contrato §3) que no necesita nada de app.js:
 *   - detecta abrir/cerrar (atributo `hidden`) de cada `.modal` del HTML y de los
 *     `.modal` que otras partes añadan directamente al <body>;
 *   - al abrir mueve el foco adentro y al cerrar lo devuelve a donde estaba;
 *   - Tab queda atrapado en el modal de arriba;
 *   - Escape cierra haciendo CLIC en el botón `[data-modal-close]` del modal de
 *     arriba (así corre la lógica de cierre de cada pista). Para modales sin esa
 *     marca (p. ej. el de grabación por cámara, creado desde app.js) usa
 *     `[data-close]` o, si no, el primer botón de la cabecera.
 */
"use strict";

(function () {
  const FOCUSABLE = [
    "a[href]", "button:not([disabled])", "input:not([disabled]):not([type=hidden])",
    "select:not([disabled])", "textarea:not([disabled])", "summary",
    "[tabindex]:not([tabindex='-1'])",
  ].join(",");

  let seq = 0;
  const uid = (p) => `${p}${++seq}`;

  // Pila de modales abiertos, el último es el de arriba.
  // Cada entrada: { el, returnTo }
  const stack = [];

  // ------------------------------------------------------- pantalla completa
  // Con un elemento en pantalla completa (una teja, el muro o el DVR) nada fuera
  // de él se ve, y los modales/diálogos viven en <body>: el botón «IA» parecía no
  // hacer nada. Al abrirse la PRIMERA ventana se sale de pantalla completa
  // recordando el elemento; al cerrarse la ÚLTIMA se vuelve a entrar en él.
  // Genérico: vale para todo `.modal`, el login y uiPrompt/uiConfirm.
  let fsEl = null;            // elemento que estaba en pantalla completa
  let fsRestoreTimer = null;
  let fsSwallowUntil = 0;     // hasta cuándo se traga el `fullscreenchange` de SALIDA provocado por esta función

  // app.js reacciona a esa salida apagando el modo «ajustar» y devolviendo la teja
  // a fotograma; como la salida es temporal, ese evento no debe llegar a app.js.
  // Captura en window: corre antes que cualquier oyente de document.
  window.addEventListener("fullscreenchange", (e) => {
    if (fsSwallowUntil && performance.now() < fsSwallowUntil && !document.fullscreenElement) {
      fsSwallowUntil = 0;
      e.stopImmediatePropagation();
    }
  }, true);

  function suspendFullscreen(modal) {
    clearTimeout(fsRestoreTimer);
    const cur = document.fullscreenElement;
    if (!cur || typeof document.exitFullscreen !== "function") return;
    // Si la ventana está dentro del elemento a pantalla completa (o lo contiene) ya se ve.
    if (modal && (cur.contains(modal) || modal.contains(cur))) return;
    fsEl = cur;
    fsSwallowUntil = performance.now() + 800;
    document.exitFullscreen().catch(() => { fsSwallowUntil = 0; });
  }

  function scheduleFullscreenRestore() {
    clearTimeout(fsRestoreTimer);
    if (!fsEl) return;
    // Espera corta: hay flujos que cierran una ventana y abren otra un instante después
    // (IA → editor de zona, que primero carga un fotograma); así no se entra y sale de
    // pantalla completa entre ambas. Si la otra tarda más, solo hay un parpadeo.
    fsRestoreTimer = setTimeout(restoreFullscreen, 350);
  }

  function restoreFullscreen() {
    pruneStack();
    if (stack.length || !fsEl) return;
    const el = fsEl;
    fsEl = null;
    if (!el.isConnected || document.fullscreenElement || typeof el.requestFullscreen !== "function") return;
    el.requestFullscreen().catch(() => armFullscreenRetry(el));
  }

  // El navegador exige un gesto reciente para entrar en pantalla completa. Al cerrar
  // con clic o con Guardar lo hay; con Escape no (no cuenta como activación). Entonces
  // se reintenta en la siguiente pulsación o clic, durante 20 s.
  let fsRetryArmed = false;
  function armFullscreenRetry(el) {
    if (fsRetryArmed) return;
    fsRetryArmed = true;
    const off = () => {
      fsRetryArmed = false;
      clearTimeout(timer);
      document.removeEventListener("pointerdown", onGesture, true);
      document.removeEventListener("keydown", onGesture, true);
    };
    const onGesture = (ev) => {
      if (ev.type === "keydown" && ev.key === "Escape") return;
      off();
      if (el.isConnected && !document.fullscreenElement && !stack.length) {
        el.requestFullscreen().catch(() => {});
      }
    };
    const timer = setTimeout(off, 20000);
    document.addEventListener("pointerdown", onGesture, true);
    document.addEventListener("keydown", onGesture, true);
  }

  function isShown(el) {
    return el.isConnected && !el.hidden && el.getAttribute("aria-hidden") !== "true";
  }

  // Defensa: una ventana oculta por otra vía (estilo, clase) que no pasó por `hidden`
  // dejaría una entrada colgada y desactivaría la salida/entrada de pantalla completa.
  function pruneStack() {
    for (let i = stack.length - 1; i >= 0; i--) if (!isShown(stack[i].el)) stack.splice(i, 1);
  }

  function focusables(el) {
    return Array.from(el.querySelectorAll(FOCUSABLE)).filter(
      (n) => !n.hidden && !n.closest("[hidden]") && n.getClientRects().length > 0
    );
  }

  function ensureAria(el) {
    if (!el.hasAttribute("role")) el.setAttribute("role", "dialog");
    if (!el.hasAttribute("aria-modal")) el.setAttribute("aria-modal", "true");
    if (!el.hasAttribute("aria-labelledby") && !el.hasAttribute("aria-label")) {
      const h = el.querySelector("h1, h2, h3");
      if (h) {
        if (!h.id) h.id = uid("uititle");
        el.setAttribute("aria-labelledby", h.id);
      }
    }
    upgradeCloseButtons(el);
  }

  // Marco de modales (rediseño): el botón «Cerrar» de la cabecera pasa a ser un
  // botón de ícono (x) con aria-label. Solo se toca si el texto es exactamente
  // «Cerrar» y no lleva ya un ícono; los ids y los oyentes del botón no cambian.
  // «Cancelar» (editor de zona) no se toca: cancela, no cierra.
  function upgradeCloseButtons(el) {
    if (typeof icon !== "function") return;
    el.querySelectorAll(".modalhead [data-modal-close], .modalhead [data-close]").forEach((btn) => {
      if (btn.querySelector("svg") || btn.textContent.trim() !== "Cerrar") return;
      btn.classList.remove("ghost", "small");
      btn.classList.add("cw-ib");
      btn.setAttribute("aria-label", "Cerrar");
      btn.title = "Cerrar";
      btn.innerHTML = icon("x", 18);
    });
  }

  function closeButton(el) {
    return (
      el.querySelector("[data-modal-close]") ||
      el.querySelector("[data-close]") ||
      el.querySelector(".modalhead button")
    );
  }

  function open(el) {
    if (stack.some((s) => s.el === el)) return;
    ensureAria(el);
    pruneStack();
    if (!stack.length) suspendFullscreen(el);
    else clearTimeout(fsRestoreTimer);
    const prev = document.activeElement;
    stack.push({ el, returnTo: prev instanceof HTMLElement && prev !== document.body ? prev : null });
    // focus() fuerza el cálculo de estilos, no hace falta esperar al pintado
    // (requestAnimationFrame ni siquiera corre con la pestaña en segundo plano).
    if (el.contains(document.activeElement)) return;
    const target = el.querySelector("[data-autofocus]") || focusables(el)[0] || el;
    if (target === el && !el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
  }

  function close(el) {
    const i = stack.findIndex((s) => s.el === el);
    if (i < 0) return;
    const [entry] = stack.splice(i, 1);
    if (!stack.length) scheduleFullscreenRestore();
    // Solo devolver el foco si el modal cerrado era el de arriba y el foco quedó
    // en él (o se perdió en <body>); si no, otro código ya movió el foco a propósito.
    const active = document.activeElement;
    const lost = !active || active === document.body || el.contains(active);
    if (i === stack.length && lost && entry.returnTo && entry.returnTo.isConnected) {
      entry.returnTo.focus({ preventScroll: true });
    }
  }

  function sync(el) {
    if (isShown(el)) open(el); else close(el);
  }

  // Un observador por modal estático: así no se vigila toda la página.
  const hiddenObserver = new MutationObserver((muts) => {
    for (const m of muts) sync(m.target);
  });
  const watched = new WeakSet();

  function watch(el) {
    if (watched.has(el)) return;
    watched.add(el);
    ensureAria(el);
    hiddenObserver.observe(el, { attributes: true, attributeFilter: ["hidden"] });
    sync(el);
  }

  // Modales añadidos o quitados directamente en <body> (los crea app.js al vuelo).
  const bodyObserver = new MutationObserver((muts) => {
    for (const m of muts) {
      m.addedNodes.forEach((n) => {
        if (n.nodeType === 1 && n.classList.contains("modal")) watch(n);
      });
      m.removedNodes.forEach((n) => {
        if (n.nodeType === 1 && stack.some((s) => s.el === n)) close(n);
      });
    }
  });

  document.querySelectorAll(".modal").forEach(watch);
  // La pantalla de login/registro se comporta como un modal (no tiene cierre).
  const auth = document.getElementById("authOverlay");
  if (auth) watch(auth);
  bodyObserver.observe(document.body, { childList: true });

  document.addEventListener(
    "keydown",
    (e) => {
      const top = stack[stack.length - 1];
      if (!top || !isShown(top.el)) return;
      const el = top.el;
      if (e.key === "Escape") {
        const btn = closeButton(el);
        if (btn) {
          e.preventDefault();
          // Que el DVR u otros oyentes de Escape no actúen debajo del modal.
          e.stopImmediatePropagation();
          btn.click();
        }
        return;
      }
      if (e.key === "Tab") {
        const items = focusables(el);
        if (!items.length) { e.preventDefault(); el.focus(); return; }
        const first = items[0], last = items[items.length - 1];
        const active = document.activeElement;
        if (!el.contains(active)) {
          e.preventDefault();
          (e.shiftKey ? last : first).focus();
        } else if (e.shiftKey && (active === first || active === el)) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && active === last) {
          e.preventDefault(); first.focus();
        }
      }
    },
    true
  );

  // Si el foco se escapa del modal de arriba (clic fuera de él, etc.), traerlo de vuelta.
  document.addEventListener("focusin", (e) => {
    const top = stack[stack.length - 1];
    if (!top || !isShown(top.el) || top.el.contains(e.target)) return;
    if (top.el.id === "authOverlay") return; // login: nada debajo que proteger
    const items = focusables(top.el);
    (items[0] || top.el).focus({ preventScroll: true });
  });

  // ---------------------------------------------------------------- cabecera
  // app.js solo cambia la clase de los puntos de estado (dot on/off) y el texto
  // de #userName; aquí se traduce a texto accesible y a la inicial del avatar.
  [["dotApi", "Servidor"], ["dotMtx", "Vídeo"]].forEach(([id, base]) => {
    const dot = document.getElementById(id);
    if (!dot) return;
    const sync = () => {
      const state = dot.classList.contains("on") ? "en línea"
        : dot.classList.contains("off") ? "sin conexión" : "comprobando";
      dot.setAttribute("aria-label", `${base}: ${state}`);
      dot.title = `${base}: ${state}`;
    };
    new MutationObserver(sync).observe(dot, { attributes: true, attributeFilter: ["class"] });
    sync();
  });
  const userName = document.getElementById("userName");
  const avatar = document.querySelector(".usericon");
  if (userName && avatar) {
    const syncUser = () => {
      const name = userName.textContent.trim();
      avatar.textContent = name ? Array.from(name)[0] : "";
      const box = document.getElementById("userBox");
      if (box) box.title = name ? `Sesión de ${name}` : "";
    };
    new MutationObserver(syncUser).observe(userName, { childList: true, characterData: true, subtree: true });
    syncUser();
  }

  // ---------------------------------------------------------------- avisos (toast)
  // app.js solo pone el texto y la clase `err`; un error se anuncia con
  // prioridad (role=alert) y el resto con cortesía (role=status).
  const toastEl = document.getElementById("toast");
  if (toastEl) {
    const syncToast = () => {
      const err = toastEl.classList.contains("err");
      toastEl.setAttribute("role", err ? "alert" : "status");
      toastEl.setAttribute("aria-live", err ? "assertive" : "polite");
    };
    new MutationObserver(syncToast).observe(toastEl, { attributes: true, attributeFilter: ["class"] });
    syncToast();
  }

  // ---------------------------------------------------------------- pestañas
  // app.js cambia la clase `.active`; aquí se refleja en `aria-selected` y se
  // añade el teclado de un tablist (flechas, Inicio, Fin), sin tocar app.js.
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
  if (tabs.length) {
    const syncTabs = () => {
      tabs.forEach((t) => {
        const on = t.classList.contains("active");
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1; // tabulación "itinerante": una sola parada en el tablist
      });
    };
    const tabObserver = new MutationObserver(syncTabs);
    tabs.forEach((t) => tabObserver.observe(t, { attributes: true, attributeFilter: ["class"] }));
    syncTabs();
    const list = tabs[0].parentElement;
    list.addEventListener("keydown", (e) => {
      const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
      if (!keys.includes(e.key)) return;
      const vis = tabs.filter((t) => t.getClientRects().length > 0); // los data-admin pueden estar ocultos
      const i = vis.indexOf(document.activeElement);
      if (i < 0) return;
      let n = i;
      if (e.key === "ArrowRight") n = (i + 1) % vis.length;
      else if (e.key === "ArrowLeft") n = (i - 1 + vis.length) % vis.length;
      else if (e.key === "Home") n = 0;
      else n = vis.length - 1;
      e.preventDefault();
      vis[n].focus(); // activación manual (Enter/Espacio): cambiar de pestaña arranca cargas pesadas (muro, API)
    });
  }

  // ---------------------------------------------------------------- diálogos
  // Una cola: si dos partes piden diálogo a la vez, se muestran de a uno.
  let queue = Promise.resolve();

  function dialog(message, { input, okLabel, danger } = {}) {
    return new Promise((resolve) => {
      queue = queue.then(
        () =>
          new Promise((done) => {
            const root = document.createElement("div");
            root.className = "modal uidialog";
            const msgId = uid("uimsg");
            const fieldId = uid("uifield");
            root.setAttribute("role", input ? "dialog" : "alertdialog");
            root.setAttribute("aria-modal", "true");
            root.setAttribute("aria-labelledby", msgId);

            const card = document.createElement("form");
            card.className = "modalcard uicard";
            card.noValidate = true;

            const top = document.createElement("div");
            top.className = "uitop";
            const msg = document.createElement(input ? "label" : "p");
            msg.className = "uimsg";
            msg.id = msgId;
            msg.textContent = String(message ?? "");
            if (input) msg.htmlFor = fieldId;
            // x de cierre (cancela); Escape y el clic en el fondo hacen lo mismo
            const x = document.createElement("button");
            x.type = "button";
            x.className = "cw-ib";
            x.setAttribute("aria-label", "Cerrar");
            x.title = "Cerrar";
            x.setAttribute("data-modal-close", "");
            x.innerHTML = typeof icon === "function" ? icon("x", 18) : "×";
            top.append(msg, x);
            card.appendChild(top);

            let field = null;
            if (input) {
              field = document.createElement("input");
              field.id = fieldId;
              field.className = "uifield";
              field.type = input.type === "password" ? "password" : "text";
              field.value = input.value ?? "";
              if (input.placeholder) field.placeholder = input.placeholder;
              field.autocomplete = field.type === "password" ? "new-password" : "off";
              field.spellcheck = false;
              field.setAttribute("data-autofocus", "");
              card.appendChild(field);
            }

            const foot = document.createElement("div");
            foot.className = "uibtns";
            const cancel = document.createElement("button");
            cancel.type = "button";
            cancel.className = "cw-btn";
            cancel.textContent = "Cancelar";
            const ok = document.createElement("button");
            ok.type = "submit";
            ok.className = danger ? "cw-btn cw-btn--danger" : "cw-btn cw-btn--primary";
            ok.textContent = okLabel || "Aceptar";
            // Confirmación: el foco inicial va a Aceptar salvo que sea destructiva,
            // donde cae en Cancelar para que un Enter accidental no borre nada.
            if (!input) (danger ? cancel : ok).setAttribute("data-autofocus", "");
            foot.append(cancel, ok);
            card.appendChild(foot);
            root.appendChild(card);

            let settled = false;
            const finish = (value) => {
              if (settled) return;
              settled = true;
              root.remove();
              resolve(value);
              done();
            };
            card.addEventListener("submit", (e) => {
              e.preventDefault();
              finish(input ? field.value : true);
            });
            const cancelAll = () => finish(input ? null : false);
            cancel.addEventListener("click", cancelAll);
            x.addEventListener("click", cancelAll);
            // Clic en el fondo = cancelar (igual que los demás modales).
            root.addEventListener("mousedown", (e) => {
              if (e.target === root) finish(input ? null : false);
            });

            document.body.appendChild(root); // el observador del <body> lo registra y enfoca
            if (field) queueMicrotask(() => field.select()); // después de que el observador enfoque
          })
      );
    });
  }

  window.uiPrompt = function uiPrompt(message, opts = {}) {
    const o = opts || {};
    return dialog(message, {
      input: { value: o.value, placeholder: o.placeholder, type: o.type },
      okLabel: o.okLabel,
    });
  };

  window.uiConfirm = function uiConfirm(message, opts = {}) {
    const o = opts || {};
    return dialog(message, { okLabel: o.okLabel, danger: !!o.danger });
  };
})();
