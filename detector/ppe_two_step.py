"""EPP («sin casco») en DOS pasos.

Antes el modelo de EPP (YOLOv8n a 320 px) corría sobre el fotograma ENTERO: en una
cámara amplia (1920×1080 → 320) una cabeza de 25 px queda en 4 px y no se detecta.

Ahora:
  1. YOLO de personas (COCO) sobre el fotograma → cajas de persona.
  2. Por cada persona (las más grandes primero, máx. `max_persons`): se recorta su
     caja AMPLIADA (sobre todo hacia arriba) y se corre el modelo de EPP sobre el
     recorte; la cabeza vuelve a ocupar buena parte de los 320 px.
  3. Las cabezas se devuelven en coordenadas del FOTOGRAMA, sin duplicados.
  4. Si no hay personas, se hace el paso de imagen completa como respaldo.

Este módulo solo usa numpy (sin cv2 ni onnxruntime) para poder probar la
geometría fuera del contenedor; el modelo es cualquier objeto con
`detect(img, conf=, classes=) -> [obj con cls, score, x, y, w, h]`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Head:
    cls: str
    score: float
    x: float
    y: float
    w: float
    h: float


def person_crop(box, frame_w: int, frame_h: int, up: float = 0.25, down: float = 0.65,
                side: float = 0.25, min_side: int = 96):
    """Recorte (x0, y0, x1, y1), enteros y dentro del fotograma, que rodea la parte
    de ARRIBA de una persona (cabeza y torso).

    `box` = (x, y, w, h) en píxeles. `up`/`side` ensanchan la caja en fracciones de
    su alto/ancho; `down` es la fracción del alto de la persona que se conserva
    desde su borde superior. Lados de menos de `min_side` px se ensanchan alrededor
    del centro (así una persona minúscula no se amplía hasta convertirla en ruido).
    Devuelve None si el recorte queda vacío."""
    x, y, w, h = box
    x0, x1 = x - side * w, x + w + side * w
    y0, y1 = y - up * h, y + down * h
    for lo, hi, limit, setter in ((x0, x1, frame_w, "x"), (y0, y1, frame_h, "y")):
        if hi - lo < min_side:
            c = (lo + hi) / 2
            lo, hi = c - min_side / 2, c + min_side / 2
        if setter == "x":
            x0, x1 = lo, hi
        else:
            y0, y1 = lo, hi
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(frame_w, int(round(x1))), min(frame_h, int(round(y1)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return x0, y0, x1, y1


def _iou(a: Head, b: Head) -> float:
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def dedupe(heads: list, iou: float = 0.5) -> list:
    """NMS simple: de dos cabezas solapadas (recortes vecinos) se queda la de mayor puntuación."""
    kept: list = []
    for h in sorted(heads, key=lambda d: -d.score):
        if all(_iou(h, k) < iou for k in kept):
            kept.append(h)
    return kept


def detect_heads(frame, persons, ppe, conf: float, classes=("head",), max_persons: int = 8,
                 up: float = 0.25, down: float = 0.65, side: float = 0.25,
                 min_side: int = 96, full_frame_fallback: bool = True) -> list:
    """Cabezas (`classes` del modelo EPP) en coordenadas del fotograma.

    `persons`: detecciones de persona del fotograma (objetos con x, y, w, h, score).
    Con personas: modelo EPP sobre el recorte de cada una (las `max_persons` más
    grandes). Sin personas: modelo EPP sobre el fotograma entero (respaldo)."""
    fh, fw = frame.shape[:2]
    if not persons:
        if not full_frame_fallback:
            return []
        return dedupe([Head(d.cls, d.score, d.x, d.y, d.w, d.h)
                       for d in ppe.detect(frame, conf=conf, classes=list(classes))])

    heads: list = []
    for p in sorted(persons, key=lambda d: -(d.w * d.h))[:max(1, max_persons)]:
        crop = person_crop((p.x, p.y, p.w, p.h), fw, fh, up=up, down=down, side=side,
                           min_side=min_side)
        if crop is None:
            continue
        x0, y0, x1, y1 = crop
        for d in ppe.detect(frame[y0:y1, x0:x1], conf=conf, classes=list(classes)):
            hd = Head(d.cls, d.score, d.x + x0, d.y + y0, d.w, d.h)
            # Sanidad: una cabeza está en la parte alta de SU persona y es pequeña
            # respecto a ella; lo demás son falsos positivos del recorte.
            cy = hd.y + hd.h / 2
            if cy > p.y + 0.45 * p.h or hd.h > 0.6 * p.h:
                continue
            heads.append(hd)
    return dedupe(heads)
