"""Compara el EPP de imagen completa (antes) con el de dos pasos (ahora) sobre
imágenes reales. Se corre DENTRO del contenedor del detector (necesita cv2,
onnxruntime y los modelos ya descargados):

    docker cp detector/eval_ppe.py detector/ppe_two_step.py camway-detector:/app/
    docker cp frame1.jpg camway-detector:/tmp/frame1.jpg
    docker exec camway-detector python /app/eval_ppe.py /tmp/frame1.jpg [/tmp/frame2.jpg …]

Para cada imagen imprime las personas que ve COCO, las cabezas (sin casco) del
paso de imagen completa y las del paso en dos pasos, y guarda
`<imagen>_ppe.jpg` con las cajas (azul = persona, rojo = cabeza «antes»
a trazo fino, verde = cabeza «ahora»).
"""
import sys
import time

import cv2

from ppe_two_step import detect_heads
from yolo import Yolo, ppe_model

PPE_CONF = 0.35
PERSON_CONF = 0.3


def main(paths):
    coco, ppe = Yolo(), ppe_model()
    for path in paths:
        img = cv2.imread(path)
        if img is None:
            print(f"{path}: no se pudo leer")
            continue
        h, w = img.shape[:2]
        t = time.time()
        persons = [d for d in coco.detect(img, conf=0.2, classes=["person"]) if d.score >= PERSON_CONF]
        t_p = time.time() - t
        t = time.time()
        old = ppe.detect(img, conf=PPE_CONF, classes=["head"])
        t_old = time.time() - t
        t = time.time()
        new = detect_heads(img, persons, ppe, PPE_CONF, ["head"])
        t_new = time.time() - t
        print(f"{path} ({w}x{h}): personas={len(persons)} ({t_p*1000:.0f} ms) | "
              f"sin casco ANTES={len(old)} ({t_old*1000:.0f} ms) | AHORA={len(new)} ({t_new*1000:.0f} ms)")
        for d in persons:
            print(f"    persona {d.score:.2f} {d.w:.0f}x{d.h:.0f} en ({d.x:.0f},{d.y:.0f})")
        for tag, ds in (("antes", old), ("ahora", new)):
            for d in ds:
                print(f"    cabeza {tag} {d.score:.2f} {d.w:.0f}x{d.h:.0f} en ({d.x:.0f},{d.y:.0f})")
        for d in persons:
            cv2.rectangle(img, (int(d.x), int(d.y)), (int(d.x + d.w), int(d.y + d.h)), (255, 120, 0), 1)
        for d in old:
            cv2.rectangle(img, (int(d.x), int(d.y)), (int(d.x + d.w), int(d.y + d.h)), (0, 0, 255), 1)
        for d in new:
            cv2.rectangle(img, (int(d.x), int(d.y)), (int(d.x + d.w), int(d.y + d.h)), (0, 200, 0), 3)
        out = path.rsplit(".", 1)[0] + "_ppe.jpg"
        cv2.imwrite(out, img)
        print(f"    guardada {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
