"""Pruebas de la geometría del EPP en dos pasos (solo numpy, sin modelos reales).

    python -I detector/test_ppe_two_step.py

El «modelo» de EPP es un doble: encuentra cuadrados blancos (las «cabezas»
sintéticas) pero SOLO si, tras reducir la imagen a 320 px por el lado largo (como
hace el letterbox real), miden al menos MIN_PX píxeles. Eso reproduce el defecto
del paso de imagen completa; NO prueba la precisión del modelo real (eso se mide
con detector/eval_ppe.py dentro del contenedor, con fotogramas reales).
"""
import os
import sys
from types import SimpleNamespace as NS

import numpy as np

if hasattr(sys.stdout, "reconfigure"):      # consola de Windows (cp1252)
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ppe_two_step import detect_heads, dedupe, Head, person_crop  # noqa: E402

MIN_PX = 12          # tamaño mínimo (px a 320) con el que el doble «ve» una cabeza


class FakePPE:
    def __init__(self):
        self.calls = []

    def detect(self, img, conf=0.35, classes=None):
        self.calls.append(img.shape[:2])
        h, w = img.shape[:2]
        scale = 320 / max(h, w)
        mask = img[..., 0] > 200
        out = []
        while mask.any():
            r, c = np.argwhere(mask)[0]
            c2 = c
            while c2 < w and mask[r, c2]:
                c2 += 1
            r2 = r
            while r2 < h and mask[r2, c]:
                r2 += 1
            mask[r:r2, c:c2] = False
            if min(r2 - r, c2 - c) * scale >= MIN_PX:
                out.append(NS(cls="head", score=0.8, x=float(c), y=float(r),
                              w=float(c2 - c), h=float(r2 - r)))
        return out


def frame(w=1920, h=1080):
    return np.zeros((h, w, 3), np.uint8)


def paint(img, x, y, w, h):
    img[y:y + h, x:x + w] = 255


def person(x, y, w, h, score=0.9):
    return NS(cls="person", score=score, x=float(x), y=float(y), w=float(w), h=float(h))


ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("  OK  " if cond else "  FALLA ") + name + (f"  [{extra}]" if extra else ""))


# 1) Persona pequeña en cámara amplia: el paso de imagen completa NO la ve, el de dos pasos SÍ.
img = frame()
paint(img, 919, 506, 22, 22)                       # cabeza de 22 px en 1920 de ancho
ppe = FakePPE()
full = ppe.detect(img)
check("imagen completa NO detecta la cabeza de 22 px (defecto original)", len(full) == 0)
ppe = FakePPE()
heads = detect_heads(img, [person(900, 500, 60, 140)], ppe, 0.35)
check("dos pasos detecta 1 cabeza", len(heads) == 1, f"{len(heads)}")
if heads:
    check("coordenadas en el fotograma (x≈919, y≈506, 22×22)",
          abs(heads[0].x - 919) <= 1 and abs(heads[0].y - 506) <= 1 and heads[0].w == 22 and heads[0].h == 22,
          f"x={heads[0].x} y={heads[0].y} w={heads[0].w} h={heads[0].h}")
check("el recorte es pequeño (no el fotograma entero)", all(max(s) < 300 for s in ppe.calls), f"{ppe.calls}")

# 2) Sin personas: respaldo de imagen completa (cabeza grande de 100 px)
img = frame()
paint(img, 800, 300, 100, 100)
ppe = FakePPE()
heads = detect_heads(img, [], ppe, 0.35)
check("sin personas → respaldo con imagen completa", len(heads) == 1 and ppe.calls == [(1080, 1920)], f"{ppe.calls}")
ppe = FakePPE()
check("sin personas y sin respaldo → nada", detect_heads(img, [], ppe, 0.35, full_frame_fallback=False) == [] and ppe.calls == [])

# 3) Tope de personas: 12 personas → como mucho max_persons recortes
img = frame()
people = [person(100 + 140 * i, 400, 60, 140) for i in range(12)]
ppe = FakePPE()
detect_heads(img, people, ppe, 0.35, max_persons=8)
check("máx. 8 recortes con 12 personas", len(ppe.calls) == 8, f"{len(ppe.calls)}")

# 4) Dos personas solapadas con la misma cabeza visible en ambos recortes → 1 sola
img = frame()
paint(img, 930, 506, 22, 22)
ppe = FakePPE()
heads = detect_heads(img, [person(900, 500, 60, 140), person(915, 500, 60, 140, 0.8)], ppe, 0.35)
check("cabeza duplicada entre recortes vecinos → 1", len(heads) == 1, f"{len(heads)}")

# 5) Cuadrado blanco a la altura de las piernas → filtrado por sanidad (no es cabeza)
img = frame()
paint(img, 915, 570, 30, 30)    # recorte llega hasta y+0.65h=591 → el cuadrado entra, pero cy > p.y+0.45h
ppe = FakePPE()
heads = detect_heads(img, [person(900, 500, 60, 140)], ppe, 0.35)
check("cabeza «en las piernas» descartada", heads == [], f"{heads}")

# 6) Geometría del recorte
c = person_crop((900, 500, 60, 140), 1920, 1080)
check("recorte normal dentro del fotograma", c is not None and 0 <= c[0] < c[2] <= 1920 and 0 <= c[1] < c[3] <= 1080, f"{c}")
c = person_crop((0, 0, 40, 100), 1920, 1080)
check("recorte pegado a la esquina se recorta al fotograma", c is not None and c[0] == 0 and c[1] == 0, f"{c}")
c = person_crop((1900, 1070, 30, 50), 1920, 1080)
check("persona fuera casi del todo: recorte dentro o None", c is None or (c[2] <= 1920 and c[3] <= 1080), f"{c}")
c = person_crop((900, 500, 10, 20), 1920, 1080)
check("persona minúscula: lado mínimo 96 px", c is not None and (c[2] - c[0]) >= 90 and (c[3] - c[1]) >= 90, f"{c}")

# 7) dedupe
a, b = Head("head", .9, 10, 10, 20, 20), Head("head", .7, 12, 11, 20, 20)
check("dedupe conserva la de mayor puntuación", dedupe([b, a]) == [a])

print("\nTODO OK" if ok else "\nHAY FALLOS")
sys.exit(0 if ok else 1)
