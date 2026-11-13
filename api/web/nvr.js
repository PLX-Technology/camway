'use strict';
// Asistente "Agregar NVR": inicia sesión como admin y da de alta todos los
// canales del NVR de una sola vez (POST /api/devices/add-nvr).
const $ = s => document.querySelector(s);
const msg = (t, cls) => { const m = $('#msg'); m.textContent = t; m.className = 'msg' + (cls ? ' ' + cls : ''); };

async function go() {
  const host = $('#host').value.trim();
  if (!host) { msg('Escribe la IP del NVR.', 'err'); return; }
  $('#go').disabled = true;
  try {
    // 1) sesión admin (add-nvr requiere administrador). Si aún no hay admin
    //    (primer uso), se CREA con lo que se escribió; si ya existe, se entra.
    const admUser = $('#adm-user').value.trim() || 'admin';
    const admPass = $('#adm-pass').value;
    let me = {};
    try { me = await (await fetch('/api/me', { cache: 'no-store' })).json(); } catch (e) {}
    if (me.setup_needed) {
      if ((admPass || '').length < 4) throw new Error('Define una clave de administrador (mínimo 4 caracteres).');
      const sr = await fetch('/api/setup', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: admUser, password: admPass }),
      });
      if (!sr.ok) { const d2 = await sr.json().catch(() => ({})); throw new Error(d2.detail || 'No se pudo crear el administrador.'); }
    } else {
      const lr = await fetch('/api/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: admUser, password: admPass }),
      });
      if (!lr.ok) throw new Error('Usuario o clave de administrador inválidos.');
    }

    // 2) alta del NVR (todos los canales)
    msg('Agregando cámaras…');
    const body = {
      host, port: 554,
      user: $('#cam-user').value.trim(), pwd: $('#cam-pass').value,
      channels: Number($('#channels').value) || 4,
      proto: $('#proto').value, subtype: 1,
    };
    const r = await fetch('/api/devices/add-nvr', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || ('Error ' + r.status));
    msg(`Listo: ${d.published} de ${d.channels} cámara(s) conectadas. Abriendo el video…`, 'ok');
    setTimeout(() => location.href = '/live', 1200);
  } catch (e) {
    msg(e.message || 'No se pudo agregar el NVR.', 'err');
    $('#go').disabled = false;
  }
}
$('#go').onclick = go;
$('#cam-pass').addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
// Prefill del usuario del NVR = admin por conveniencia
$('#cam-user').value = 'admin';
