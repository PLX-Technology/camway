"""Detector server-side de CamWay.

Por cada cámara con IA activada por ALGÚN usuario: abre su RTSP, aplica
motion-gating (un diff barato de fotogramas) y corre YOLO cuando hay
movimiento o en un barrido periódico. Emite las cajas CRUDAS (persona y
vehículo) sin filtrar por zona/tipo/umbral: la configuración es POR USUARIO,
así que cada navegador aplica su zona/tipo/umbral/horario y decide la alarma.

Correr suelto para verificar una cámara:
    python detector.py test "rtsp://172.16.0.102:554/0"
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time

import cv2
import numpy as np
import requests

from ppe_two_step import detect_heads
from yolo import Yolo, fire_model, ppe_model

API = os.environ.get("CAMWAY_API", "http://api:8000")
POLL_TARGETS_S = 2.0   # aplicar activar/desactivar IA casi de inmediato
ANALYZE_FPS = float(os.environ.get("CAMWAY_ANALYZE_FPS", "3"))
MOTION_AREA = float(os.environ.get("CAMWAY_MOTION_AREA", "0.006"))  # 0.6% de la zona
BASE_CONF = float(os.environ.get("CAMWAY_CONF", "0.2"))
# Cada cuánto correr YOLO aunque NO haya movimiento, para atrapar objetos ya
# parados (un camión estacionado no genera movimiento que dispara el gate).
FULL_SCAN_S = float(os.environ.get("CAMWAY_FULL_SCAN_S", "15"))
# Cada cuánto, como mucho, avisar movimiento a la API para grabar (antirrebote;
# el clip ya dura ~25 s y la API también tiene su propio antirrebote).
MOTION_EMIT_S = float(os.environ.get("CAMWAY_MOTION_EMIT_S", "8"))
# Barrido para cámaras de fuego/humo: mucho más frecuente (el fuego puede no
# generar movimiento que dispare el gate) → baja la latencia de la alarma.
FIRE_FULL_SCAN_S = float(os.environ.get("CAMWAY_FIRE_FULL_SCAN_S", "3"))
# El worker también deja en el muro los fotogramas que SÍ pesca (OpenCV es más
# tolerante que MediaMTX). Solo se muestran si el productor normal está caído.
FRAME_FPS = float(os.environ.get("CAMWAY_DET_FRAME_FPS", "2"))
FRAME_WIDTH = int(os.environ.get("CAMWAY_DET_FRAME_WIDTH", "480"))

VEHICLE = {"car", "truck", "bus", "motorcycle"}
PERSON = {"person"}
ALL_CLASSES = VEHICLE | PERSON   # el detector emite todo; el cliente filtra
FIRE_CLASSES = ["fuego", "humo"]
FIRE_CONF = float(os.environ.get("CAMWAY_FIRE_CONF", "0.25"))  # punto recomendado del modelo

# EPP: el modelo da helmet/head/vest; solo emitimos "head" (persona SIN casco),
# que es lo accionable (helmet=con casco, vest=chaleco presente, no alarman).
PPE_EMIT = [c.strip() for c in os.environ.get("CAMWAY_PPE_CLASSES", "head").split(",") if c.strip()]
PPE_CONF = float(os.environ.get("CAMWAY_PPE_CONF", "0.35"))
# EPP en DOS pasos (personas → recorte de cada una → EPP en el recorte). 0 = el
# comportamiento anterior (EPP sobre el fotograma entero a 320 px).
PPE_TWO_STEP = os.environ.get("CAMWAY_PPE_TWO_STEP", "1").strip().lower() not in ("0", "false", "no", "")
PPE_PERSON_CONF = float(os.environ.get("CAMWAY_PPE_PERSON_CONF", "0.3"))   # umbral de persona para recortar
PPE_MAX_PERSONS = int(os.environ.get("CAMWAY_PPE_MAX_PERSONS", "8"))       # recortes por fotograma (CPU)

# AVISOS fuera de pantalla (WhatsApp/correo): cuando hay una clase "de alerta"
# el detector adjunta un JPEG ANOTADO (caja roja) del cuadro exacto. Máx. 1 por
# segundo y solo con detecciones → casi no añade tráfico. La regla (umbral,
# detecciones seguidas, enfriamiento) la decide la API.
ALERT_CLASSES = {"head", "fuego", "humo"}
# Cuadros CANDIDATOS para el aviso: uno cada ALERT_FRAME_S con su NITIDEZ medida
# alrededor de la persona; la API elige el más nítido de la ventana de
# confirmación (una persona que camina sale movida en la mayoría de cuadros).
ALERT_FRAME_S = float(os.environ.get("CAMWAY_ALERT_FRAME_S", "0.5"))
ALERT_FRAME_W = int(os.environ.get("CAMWAY_ALERT_FRAME_W", "1280"))
ALERT_FRAME_Q = int(os.environ.get("CAMWAY_ALERT_FRAME_Q", "82"))
ALERT_TEXT = {"head": "SIN CASCO", "fuego": "FUEGO", "humo": "HUMO"}

# El modelo de fuego (YOLOv8s@768) es pesado y se baja (~40MB): se carga UNA vez
# y solo cuando alguna cámara pide target "fuego".
_fire_lock = threading.Lock()
_fire_model = None
# El modelo de EPP (YOLOv8n@320, ~12MB): igual, perezoso y una sola vez.
_ppe_lock = threading.Lock()
_ppe_model = None


def get_fire():
    global _fire_model
    with _fire_lock:
        if _fire_model is None:
            print("[detector] cargando modelo de fuego/humo…", flush=True)
            _fire_model = fire_model()
            print("[detector] modelo fuego/humo listo", flush=True)
    return _fire_model


def get_ppe():
    global _ppe_model
    with _ppe_lock:
        if _ppe_model is None:
            print("[detector] cargando modelo de EPP (sin casco)…", flush=True)
            _ppe_model = ppe_model()
            print("[detector] modelo EPP listo", flush=True)
    return _ppe_model


def point_in_poly(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


class MotionGate:
    """Diferencia de fotogramas en escala reducida, restringida a la zona."""

    def __init__(self, zone=None, downscale: int = 160):
        self.downscale = downscale
        self.prev = None
        self.zone = zone          # polígono normalizado o None
        self.mask = None          # se construye al conocer el tamaño reducido

    def _ensure_mask(self, gray):
        if self.zone is None or self.mask is not None:
            return
        h, w = gray.shape[:2]
        pts = np.array([[int(px * w), int(py * h)] for px, py in self.zone], np.int32)
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [pts], 255)
        self.mask = m
        self.mask_area = int(np.count_nonzero(m)) or 1

    def moved(self, frame) -> bool:
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (self.downscale, int(self.downscale * h / w)))
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        self._ensure_mask(gray)
        if self.prev is None:
            self.prev = gray
            return True
        diff = cv2.absdiff(self.prev, gray)
        self.prev = gray
        thresh = diff > 25
        if self.mask is not None:
            thresh = thresh & (self.mask > 0)
            return np.count_nonzero(thresh) / self.mask_area >= MOTION_AREA
        return np.count_nonzero(thresh) / diff.size >= MOTION_AREA


class FrameGrabber(threading.Thread):
    """Lee el RTSP a su ritmo y guarda SOLO el último fotograma.

    Así la inferencia (que es lenta, ~1s con el modelo de fuego) siempre analiza
    lo MÁS FRESCO: sin este hilo, durante cada inferencia se acumulan fotogramas
    en el buffer y el detector termina analizando imágenes de hace varios
    segundos → la alarma sale con mucho retraso. Aquí los viejos se descartan."""

    def __init__(self, url: str):
        super().__init__(daemon=True)
        self.url = url
        self._frame = None
        self._lock = threading.Lock()
        self.stop_flag = threading.Event()

    def run(self):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        fails = 0
        while not self.stop_flag.is_set():
            ok, fr = cap.read()
            if not ok or fr is None:
                fails += 1
                if fails > 30:
                    cap.release(); time.sleep(2)
                    cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG); fails = 0
                else:
                    time.sleep(0.05)
                continue
            fails = 0
            with self._lock:
                self._frame = fr
        cap.release()

    def latest(self):
        with self._lock:
            return self._frame


class CameraWorker(threading.Thread):
    def __init__(self, model: Yolo, target: dict):
        super().__init__(daemon=True)
        self.model = model                       # modelo COCO (persona/vehículo)
        self.stream_id = target["stream_id"]
        self.url = target["url"]
        self.stop_flag = threading.Event()
        self.gate = MotionGate()                 # sin zona: la aplica cada usuario
        self.ai = bool(target.get("ai", True))   # correr el modelo (alarma)
        self.record_motion = bool(target.get("record_motion", False))  # movimiento→grabar
        targets = set(target.get("targets") or [])
        self.want_fire = self.ai and "fuego" in targets
        self.want_ppe = self.ai and "epp" in targets
        # con IA: cualquier target que no sea fuego/epp usa COCO (vacío = default).
        # sin IA (solo grabación por movimiento): no se corre ningún modelo.
        self.want_coco = self.ai and (any(t not in ("fuego", "epp") for t in targets) or not targets)
        self._last_motion = 0.0                  # antirrebote de emisión de movimiento
        self._last_alert_frame = 0.0             # antirrebote del JPEG de aviso
        self.fire = None                         # se carga en run() si hace falta
        self.ppe = None                          # idem (EPP / sin casco)

    def run(self):
        interval = 1.0 / ANALYZE_FPS
        frame_interval = 1.0 / FRAME_FPS if FRAME_FPS > 0 else 0
        last = 0.0
        last_full = 0.0
        last_frame = 0.0
        if self.want_fire:
            try:
                self.fire = get_fire()
            except Exception as e:  # noqa: BLE001
                print(f"[worker] {self.stream_id} fuego no disponible: {e}", flush=True)
        if self.want_ppe:
            try:
                self.ppe = get_ppe()
            except Exception as e:  # noqa: BLE001
                print(f"[worker] {self.stream_id} EPP no disponible: {e}", flush=True)
        modos = ("persona/vehículo " if self.want_coco else "") + \
                ("fuego/humo " if self.fire else "") + ("sin-casco" if self.ppe else "")
        print(f"[worker] {self.stream_id} ({modos.strip() or 'persona/vehículo'})", flush=True)

        grab = FrameGrabber(self.url)
        grab.start()
        try:
            self._loop(grab, interval, frame_interval, last, last_full, last_frame)
        finally:
            grab.stop_flag.set()
        print(f"[worker] {self.stream_id} detenido", flush=True)

    def _loop(self, grab, interval, frame_interval, last, last_full, last_frame):
        while not self.stop_flag.is_set():
            frame = grab.latest()
            if frame is None:
                time.sleep(0.05)
                continue

            now = time.time()
            # Alimenta el muro con lo que SÍ llega (fallback ante RTSP caído).
            if frame_interval and now - last_frame >= frame_interval:
                last_frame = now
                self._push_frame(frame)

            if now - last < interval:
                time.sleep(0.01)
                continue
            last = now

            # Fuego/humo: analizar en CADA ciclo (sin gate de movimiento) para
            # que la alarma salga cuanto antes — una llama estable o humo lento
            # casi no genera movimiento. Persona/vehículo: gate de movimiento +
            # barrido periódico (ahorra CPU en objetos ya parados).
            # Movimiento SIEMPRE (barato): dispara la GRABACIÓN por evento,
            # independiente de la IA. Antirrebote para no spamear.
            moved = self.gate.moved(frame)
            if moved and self.record_motion and (now - self._last_motion) >= MOTION_EMIT_S:
                self._last_motion = now
                self._emit_motion()

            if not self.ai:
                continue            # solo grabación por movimiento, sin modelo

            if self.want_fire or self.want_ppe:
                # fuego y EPP: SIEMPRE (una persona quieta sin casco no dispara
                # el gate de movimiento y la alarma saldría con 15 s de retraso).
                do_analyze = True
            else:
                do_full = (now - last_full) >= FULL_SCAN_S
                if do_full:
                    last_full = now
                do_analyze = moved or do_full
            if not do_analyze:
                continue

            h, w = frame.shape[:2]
            boxes = []
            coco = []
            # Personas/vehículos (COCO). Con EPP en dos pasos también hace falta
            # YOLO de personas aunque nadie pida «persona»: de ahí salen los
            # recortes. Solo se EMITEN las cajas de COCO si se pidieron.
            if self.want_coco or (self.ppe is not None and PPE_TWO_STEP):
                coco = self.model.detect(frame, conf=BASE_CONF,
                                         classes=ALL_CLASSES if self.want_coco else PERSON)
            if self.want_coco:
                for d in coco:
                    boxes.append({
                        "cls": d.cls, "score": round(d.score, 3),
                        "x": round(d.x / w, 4), "y": round(d.y / h, 4),
                        "w": round(d.w / w, 4), "h": round(d.h / h, 4),
                    })
            if self.fire is not None:
                for d in self.fire.detect(frame, conf=FIRE_CONF, iou=0.70,
                                          classes=FIRE_CLASSES):
                    boxes.append({
                        "cls": d.cls, "score": round(d.score, 3),
                        "x": round(d.x / w, 4), "y": round(d.y / h, 4),
                        "w": round(d.w / w, 4), "h": round(d.h / h, 4),
                    })
            if self.ppe is not None:
                if PPE_TWO_STEP:
                    persons = [d for d in coco if d.cls == "person" and d.score >= PPE_PERSON_CONF]
                    heads = detect_heads(frame, persons, self.ppe, PPE_CONF, PPE_EMIT,
                                         max_persons=PPE_MAX_PERSONS)
                else:
                    heads = self.ppe.detect(frame, conf=PPE_CONF, classes=PPE_EMIT)
                for d in heads:
                    boxes.append({
                        "cls": d.cls, "score": round(d.score, 3),
                        "x": round(d.x / w, 4), "y": round(d.y / h, 4),
                        "w": round(d.w / w, 4), "h": round(d.h / h, 4),
                    })
            alert = [b for b in boxes if b["cls"] in ALERT_CLASSES]
            jpg, sharp = None, None
            if alert and now - self._last_alert_frame >= ALERT_FRAME_S:
                self._last_alert_frame = now
                jpg, sharp = self._alert_jpeg(frame, alert)
            self._emit(boxes, jpg, sharp)

    @staticmethod
    def _sharpness(frame, box) -> float:
        """Nitidez (varianza del Laplaciano) de la zona de la persona: cabeza y
        cuerpo bajo ella. Medida donde se MUEVE la persona, no en el fondo fijo
        (que siempre sale nítido y no dice nada del desenfoque de movimiento)."""
        h, w = frame.shape[:2]
        x, y, bw, bh = box["x"] * w, box["y"] * h, box["w"] * w, box["h"] * h
        x0, x1 = int(max(0, x - bw * 0.75)), int(min(w, x + bw * 1.75))
        y0, y1 = int(max(0, y - bh * 0.5)), int(min(h, y + bh * 3.5))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return 0.0
        roi = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        roi = cv2.resize(roi, (160, max(8, int(160 * roi.shape[0] / roi.shape[1]))))
        return float(cv2.Laplacian(roi, cv2.CV_64F).var())

    @classmethod
    def _alert_jpeg(cls, frame, alert):
        """(JPEG base64 con las cajas de alerta dibujadas, nitidez de la persona)."""
        try:
            sharp = cls._sharpness(frame, max(alert, key=lambda b: b["score"]))
            h, w = frame.shape[:2]
            img = frame
            if w > ALERT_FRAME_W:
                img = cv2.resize(frame, (ALERT_FRAME_W, int(ALERT_FRAME_W * h / w)))
            else:
                img = frame.copy()
            ih, iw = img.shape[:2]
            th = max(2, iw // 400)
            for b in alert:
                x0, y0 = int(b["x"] * iw), int(b["y"] * ih)
                x1, y1 = int((b["x"] + b["w"]) * iw), int((b["y"] + b["h"]) * ih)
                cv2.rectangle(img, (x0, y0), (x1, y1), (40, 40, 235), th)
                lab = f'{ALERT_TEXT.get(b["cls"], b["cls"])} {int(b["score"] * 100)}%'
                fs = max(0.6, iw / 1600)
                (tw, tht), _ = cv2.getTextSize(lab, cv2.FONT_HERSHEY_SIMPLEX, fs, 2)
                ty = max(tht + 6, y0)
                cv2.rectangle(img, (x0, ty - tht - 8), (x0 + tw + 8, ty), (40, 40, 235), -1)
                cv2.putText(img, lab, (x0 + 4, ty - 5), cv2.FONT_HERSHEY_SIMPLEX, fs,
                            (255, 255, 255), 2, cv2.LINE_AA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, ALERT_FRAME_Q])
            if not ok:
                return None, None
            import base64
            return base64.b64encode(buf.tobytes()).decode(), round(sharp, 2)
        except cv2.error:
            return None, None

    def _emit(self, boxes, frame_b64=None, sharp=None):
        payload = {"stream_id": self.stream_id, "ts": time.time(), "boxes": boxes}
        if frame_b64:
            payload["frame"] = frame_b64
            payload["sharp"] = sharp
        try:
            requests.post(f"{API}/api/detector/events", json=payload, timeout=6)
        except requests.RequestException:
            pass

    def _emit_motion(self):
        """Avisa a la API que hubo MOVIMIENTO → graba un clip de evento. No lleva
        cajas (no es la IA de la alarma)."""
        try:
            requests.post(f"{API}/api/detector/events", json={
                "stream_id": self.stream_id, "ts": time.time(), "motion": True,
            }, timeout=4)
        except requests.RequestException:
            pass

    def _push_frame(self, frame):
        try:
            h, w = frame.shape[:2]
            if w > FRAME_WIDTH:
                frame = cv2.resize(frame, (FRAME_WIDTH, int(FRAME_WIDTH * h / w)))
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not ok:
                return
            requests.post(f"{API}/api/detector/frame/{self.stream_id}",
                          data=buf.tobytes(),
                          headers={"Content-Type": "image/jpeg"}, timeout=4)
        except (requests.RequestException, cv2.error):
            pass


def load_targets():
    try:
        r = requests.get(f"{API}/api/detector/targets", timeout=5)
        r.raise_for_status()
        return {t["stream_id"]: t for t in r.json().get("targets", [])}
    except requests.RequestException:
        return None


def _sig(t: dict) -> str:
    # el worker depende de la URL y de los targets (si cambian, se reinicia)
    return t.get("url", "") + "|" + ",".join(sorted(t.get("targets") or []))


def main():
    print("[detector] cargando modelo YOLOv8n…", flush=True)
    model = Yolo()
    print("[detector] modelo listo", flush=True)

    workers: dict[str, tuple] = {}   # stream_id -> (worker, signature)
    while True:
        targets = load_targets()
        if targets is not None:
            for sid, t in targets.items():
                sig = _sig(t)
                cur = workers.get(sid)
                if cur is None or not cur[0].is_alive() or cur[1] != sig:
                    if cur is not None:
                        cur[0].stop_flag.set()
                    w = CameraWorker(model, t)
                    workers[sid] = (w, sig)
                    w.start()
            for sid in list(workers):
                if sid not in targets:
                    workers[sid][0].stop_flag.set()
                    del workers[sid]
        time.sleep(POLL_TARGETS_S)


def test(url: str):
    model = Yolo()
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    gate = MotionGate()
    print(f"probando {url} …")
    frames = analyzed = 0
    t0 = time.time()
    while time.time() - t0 < 30 and analyzed < 10:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue
        frames += 1
        if not gate.moved(frame):
            continue
        analyzed += 1
        ti = time.time()
        dets = model.detect(frame, conf=0.25)
        ms = (time.time() - ti) * 1000
        resumen = ", ".join(f"{d.cls} {d.score:.2f}" for d in dets) or "(nada)"
        print(f"  frame#{frames} inferencia {ms:.0f}ms -> {resumen}")
    cap.release()
    print(f"leidos {frames} cuadros, analizados {analyzed}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "test":
        test(sys.argv[2])
    else:
        main()
