/* CamWay — utilidades de interfaz compartidas (diálogos).
 *
 * Contrato (ver la nota «Panel web — contrato» de la vault):
 *   uiPrompt(mensaje, { value, placeholder, type, okLabel })  -> Promise<string|null>
 *       type: "text" (por defecto) o "password". null = cancelado.
 *   uiConfirm(mensaje, { okLabel, danger })                    -> Promise<boolean>
 *
 * Esta primera versión (fase 0) solo envuelve window.prompt/confirm para que el
 * resto del panel ya pueda usar la API asíncrona. La pista Interfaz la reemplaza
 * por diálogos propios accesibles, SIN cambiar estas firmas.
 */
"use strict";

function uiPrompt(message, opts = {}) {
  const r = window.prompt(message, opts.value ?? "");
  return Promise.resolve(r === null ? null : r);
}

function uiConfirm(message, opts = {}) {
  return Promise.resolve(window.confirm(message));
}
