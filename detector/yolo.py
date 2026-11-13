"""YOLOv8 por ONNX Runtime + OpenCV. Sin PyTorch (que se cae en CPUs sin AVX2).

Carga un modelo .onnx (lo baja si falta), preprocesa con letterbox, infiere y
posprocesa (NMS) devolviendo detecciones en coordenadas del frame original.

La clase `Yolo` es genérica: se instancia con su propio modelo y su mapa de
clases. Así conviven el modelo COCO (persona/vehículo) y el de fuego/humo.
"""
from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass

import cv2
import numpy as np
import onnxruntime as ort

# COCO: solo las clases que nos interesan (id -> nombre corto).
COCO_NAMES = {
    0: "person", 1: "bicycle", 2: "car", 3: "motorcycle",
    5: "bus", 7: "truck",
}

MODEL_URL = os.environ.get(
    "CAMWAY_YOLO_URL",
    "https://huggingface.co/Xenova/yolov8n/resolve/main/onnx/model.onnx",
)
MODEL_PATH = os.environ.get("CAMWAY_YOLO_PATH", "/data/yolov8n.onnx")

# Modelo de fuego/humo (YOLOv8s entrenado con hard-negative mining; clases
# index 0=fuego, 1=humo; entrada 768). Se baja solo si alguna cámara lo activa.
FIRE_URL = os.environ.get(
    "CAMWAY_FIRE_URL",
    "https://github.com/Supakorn289/fire-smoke-detection-r3e6/releases/download/"
    "v1.0.0-r3e6/fire_smoke_r3_e6_final_768_fp32.onnx",
)
FIRE_PATH = os.environ.get("CAMWAY_FIRE_PATH", "/model/fire.onnx")
FIRE_NAMES = {0: "fuego", 1: "humo"}

# Modelo de EPP (YOLOv8n PPE, entrada 320; clases index 0=helmet, 1=head,
# 2=vest). "head" = cabeza descubierta = persona SIN casco → es lo accionable.
# Se baja solo si alguna cámara activa el target "epp".
PPE_URL = os.environ.get(
    "CAMWAY_PPE_URL",
    "https://huggingface.co/fiacecson20/cctv-ai-ppe/resolve/main/best.onnx",
)
PPE_PATH = os.environ.get("CAMWAY_PPE_PATH", "/model/ppe.onnx")
PPE_NAMES = {0: "helmet", 1: "head", 2: "vest"}


@dataclass
class Detection:
    cls: str
    score: float
    # caja en píxeles del frame original
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def bottom_cy(self) -> float:
        return self.y + self.h

    @property
    def center_cy(self) -> float:
        return self.y + self.h / 2


def ensure_model(url: str, path: str) -> str:
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.replace(tmp, path)
    return path


class Yolo:
    def __init__(self, size: int = 640, providers=None,
                 model_url: str = MODEL_URL, model_path: str = MODEL_PATH,
                 names: dict | None = None):
        self.names = names or COCO_NAMES
        self.name_to_id = {v: k for k, v in self.names.items()}
        path = ensure_model(model_url, model_path)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("CAMWAY_YOLO_THREADS", "2"))
        self.sess = ort.InferenceSession(
            path, sess_options=opts, providers=providers or ["CPUExecutionProvider"]
        )
        self.inp = self.sess.get_inputs()[0].name
        self.size = size

    def _class_ids(self, classes):
        """Acepta nombres o ids y devuelve el conjunto de ids de ESTE modelo."""
        if not classes:
            return set(self.names)
        ids = set()
        for c in classes:
            if isinstance(c, str):
                if c in self.name_to_id:
                    ids.add(self.name_to_id[c])
            else:
                ids.add(int(c))
        return ids

    def _letterbox(self, img):
        h, w = img.shape[:2]
        r = min(self.size / h, self.size / w)
        nh, nw = int(round(h * r)), int(round(w * r))
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.size, self.size, 3), 114, dtype=np.uint8)
        top, left = (self.size - nh) // 2, (self.size - nw) // 2
        canvas[top:top + nh, left:left + nw] = resized
        return canvas, r, left, top

    def detect(self, frame, conf: float = 0.25, iou: float = 0.45,
               classes=None) -> list[Detection]:
        canvas, r, dx, dy = self._letterbox(frame)
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[None]  # NCHW

        out = self.sess.run(None, {self.inp: blob})[0]  # [1, 4+nc, N]
        out = np.squeeze(out, 0).T                       # [N, 4+nc]

        boxes_xywh = out[:, :4]
        scores_all = out[:, 4:]
        cls_ids = np.argmax(scores_all, axis=1)
        cls_scores = scores_all[np.arange(scores_all.shape[0]), cls_ids]

        wanted = self._class_ids(classes)
        keep = (cls_scores >= conf) & np.isin(cls_ids, list(wanted))
        if not keep.any():
            return []

        boxes_xywh = boxes_xywh[keep]
        cls_ids = cls_ids[keep]
        cls_scores = cls_scores[keep]

        # xywh (centro, escala self.size) -> xyxy en frame original
        cx, cy, ww, hh = boxes_xywh.T
        x1 = (cx - ww / 2 - dx) / r
        y1 = (cy - hh / 2 - dy) / r
        bw = ww / r
        bh = hh / r

        rects = np.stack([x1, y1, bw, bh], axis=1).tolist()
        idxs = cv2.dnn.NMSBoxes(rects, cls_scores.tolist(), conf, iou)
        if len(idxs) == 0:
            return []
        idxs = np.array(idxs).flatten()

        dets = []
        for i in idxs:
            name = self.names.get(int(cls_ids[i]))
            if not name:
                continue
            dets.append(Detection(
                cls=name, score=float(cls_scores[i]),
                x=float(x1[i]), y=float(y1[i]), w=float(bw[i]), h=float(bh[i]),
            ))
        return dets


def fire_model() -> "Yolo":
    """Instancia el modelo de fuego/humo (768, clases fuego/humo)."""
    return Yolo(size=768, model_url=FIRE_URL, model_path=FIRE_PATH, names=FIRE_NAMES)


def ppe_model() -> "Yolo":
    """Instancia el modelo de EPP (320, clases helmet/head/vest)."""
    return Yolo(size=320, model_url=PPE_URL, model_path=PPE_PATH, names=PPE_NAMES)
