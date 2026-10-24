"""Person detection and per-camera tracking for camera-derived observations.

Runs on the backend computer (laptop / Raspberry Pi), never on the ESP32-CAM.

Default detector: OpenCV's built-in HOG pedestrian detector. It needs no model
download and runs on a CPU, but it is weak: it misses seated, partial and
small people and produces false positives. If a YOLO-style ONNX model is placed
at data/models/person.onnx, the ONNX detector is used instead.

Observations are anonymous ("Person observation 014"). Nothing here performs
face recognition or tries to infer who a person is.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

import cv2
import numpy as np

log = logging.getLogger("skymesh.vision")


@dataclass
class Detection:
    x: float
    y: float
    w: float
    h: float
    confidence: float

    @property
    def foot(self) -> tuple[float, float]:
        """Bottom-centre of the box: the point that touches the floor plane."""
        return self.x + self.w / 2, self.y + self.h


class Detector(Protocol):
    name: str

    def detect(self, frame: np.ndarray) -> list[Detection]: ...


class HogDetector:
    name = "opencv-hog-people"

    # 0.75 chosen from the supplied campus walkthrough videos: below it most boxes were door
    # frames and ceiling panels (see test-results/video_analysis.md).
    def __init__(self, min_confidence: float = 0.75, work_width: int = 640):
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.min_confidence = min_confidence
        self.work_width = work_width

    def detect(self, frame: np.ndarray) -> list[Detection]:
        h, w = frame.shape[:2]
        scale = min(1.0, self.work_width / max(w, 1))
        small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
        rects, weights = self.hog.detectMultiScale(small, winStride=(8, 8), padding=(8, 8), scale=1.05)
        dets = []
        for (x, y, rw, rh), wt in zip(rects, np.ravel(weights) if len(weights) else []):
            conf = float(1 / (1 + np.exp(-float(wt))))  # squash SVM margin into 0..1 for display
            if conf >= self.min_confidence:
                dets.append(Detection(x / scale, y / scale, rw / scale, rh / scale, round(conf, 3)))
        return _nms(dets, 0.45)


class OnnxYoloDetector:
    """YOLOv5/v8-style ONNX export (class 0 = person), input 640x640."""
    name = "onnx-yolo"

    def __init__(self, path: Path, min_confidence: float = 0.45):
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self.min_confidence = min_confidence
        self.name = f"onnx-yolo:{path.name}"

    def detect(self, frame: np.ndarray) -> list[Detection]:
        h, w = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(frame, 1 / 255.0, (640, 640), swapRB=True, crop=False)
        self.net.setInput(blob)
        out = self.net.forward()
        out = np.squeeze(out)
        if out.shape[0] < out.shape[1]:  # v8 layout (84, N) -> (N, 84)
            out = out.T
        sx, sy = w / 640, h / 640
        dets = []
        for r in out:
            if r.shape[0] == 85:  # v5: cx,cy,w,h,obj,classes...
                conf = float(r[4] * r[5])
            else:  # v8: cx,cy,w,h,classes...
                conf = float(r[4])
            if conf < self.min_confidence:
                continue
            cx, cy, bw, bh = r[:4]
            dets.append(Detection((cx - bw / 2) * sx, (cy - bh / 2) * sy, bw * sx, bh * sy, round(conf, 3)))
        return _nms(dets, 0.45)


def default_detector(data_dir: Path) -> Detector:
    onnx = data_dir / "models" / "person.onnx"
    if onnx.exists():
        try:
            return OnnxYoloDetector(onnx)
        except cv2.error as e:  # unreadable model: fall back, and say so
            log.warning("could not load %s (%s); using HOG", onnx, e)
    return HogDetector()


def _iou(a: Detection, b: Detection) -> float:
    x1, y1 = max(a.x, b.x), max(a.y, b.y)
    x2, y2 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _nms(dets: list[Detection], thr: float) -> list[Detection]:
    keep: list[Detection] = []
    for d in sorted(dets, key=lambda d: d.confidence, reverse=True):
        if all(_iou(d, k) < thr for k in keep):
            keep.append(d)
    return keep


@dataclass
class Track:
    label: str
    last: Detection
    last_frame: int
    hits: int = 1


@dataclass
class IouTracker:
    """Greedy IoU association within a single camera view. Tracks are not identities."""
    iou_threshold: float = 0.3
    max_missed_frames: int = 15
    next_number: int = 1
    tracks: list[Track] = field(default_factory=list)

    def update(self, dets: list[Detection], frame_index: int) -> list[tuple[Track, Detection]]:
        self.tracks = [t for t in self.tracks if frame_index - t.last_frame <= self.max_missed_frames]
        pairs = sorted(((_iou(t.last, d), ti, di) for ti, t in enumerate(self.tracks) for di, d in enumerate(dets)),
                       reverse=True)
        used_t, used_d, out = set(), set(), []
        for iou, ti, di in pairs:
            if iou < self.iou_threshold or ti in used_t or di in used_d:
                continue
            t = self.tracks[ti]
            t.last, t.last_frame, t.hits = dets[di], frame_index, t.hits + 1
            used_t.add(ti)
            used_d.add(di)
            out.append((t, dets[di]))
        for di, d in enumerate(dets):
            if di not in used_d:
                t = Track(f"Person observation {self.next_number:03d}", d, frame_index)
                self.next_number += 1
                self.tracks.append(t)
                out.append((t, d))
        return out


class FallDetector:
    """Heuristic "person fell and is not moving" detector for one fixed camera.

    Background subtraction finds the largest moving shape. A fall is: the shape was upright (taller than
    wide), turns horizontal within `fall_window_s`, and then stays horizontal and still for `still_s`.
    It cannot tell fainting from someone lying down on purpose, so every alert is "possible fall" and
    needs a human to confirm. A cooldown stops repeated alerts for the same event.
    """

    WORK_W = 160
    MOTION_THRESHOLD = 2.5  # mean grey-level change inside the person's box below which they count as motionless

    def __init__(self, still_s: float = 8.0, fall_window_s: float = 2.0, cooldown_s: float = 120.0,
                 idle_s: float = 0.0):
        """`idle_s` > 0 also alerts when a person in view has not moved at all for that long."""
        self.still_s, self.fall_window_s, self.cooldown_s, self.idle_s = still_s, fall_window_s, cooldown_s, idle_s
        self.bg = cv2.createBackgroundSubtractorMOG2(history=400, varThreshold=28, detectShadows=False)
        self.kernel = np.ones((3, 3), np.uint8)
        self.frames = 0
        self.state = "idle"  # idle | upright | down | alerted
        self.last_upright_t: float | None = None
        self.last_upright_cy = 0.0
        self.down_box: tuple | None = None
        self.still_since = 0.0
        self.last_alert = -1e9
        self.missing = 0
        self.prev_gray: np.ndarray | None = None
        self.motion = 0.0
        self.present = False      # a person-sized shape was in view on the last frame
        self.was_moving = False   # it moved at some point (things that never moved, like furniture, are ignored)
        self.idle_since: float | None = None
        self.idle_alerted = False

    def _blob(self, frame: np.ndarray) -> tuple[tuple, float] | None:
        h, w = frame.shape[:2]
        scale = self.WORK_W / w
        small = cv2.resize(frame, (self.WORK_W, max(1, int(h * scale))))
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small, (5, 5), 0)
        prev, self.prev_gray = self.prev_gray, gray
        # Freeze the background while someone may be lying still, adapt very slowly while someone is in view
        # (so a motionless person stays visible for minutes), and learn normally when the room is empty.
        lr = 0.0 if self.state in ("down", "alerted") else (0.0005 if self.present else -1)
        mask = self.bg.apply(gray, learningRate=lr)
        self.frames += 1
        if self.frames < 15:  # learning the empty scene
            return None
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.dilate(mask, self.kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        c = max(contours, key=cv2.contourArea)
        x, y, bw, bh = cv2.boundingRect(c)
        frac = bw * bh / float(gray.shape[0] * gray.shape[1])
        if not 0.01 <= frac <= 0.6:
            return None
        if prev is not None and prev.shape == gray.shape:
            self.motion = float(cv2.absdiff(prev[y:y + bh, x:x + bw], gray[y:y + bh, x:x + bw]).mean())
        inv = 1 / scale
        return (x * inv, y * inv, bw * inv, bh * inv), frac

    @staticmethod
    def _iou(a, b) -> float:
        ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
        iw, ih = max(0, min(ax2, bx2) - max(a[0], b[0])), max(0, min(ay2, by2) - max(a[1], b[1]))
        inter = iw * ih
        union = a[2] * a[3] + b[2] * b[3] - inter
        return inter / union if union else 0.0

    def update(self, frame: np.ndarray, t: float) -> dict | None:
        found = self._blob(frame)
        if found is None:
            self.missing += 1
            if self.missing > 8:
                self.present = self.was_moving = False
                self.idle_since, self.idle_alerted = None, False
                self.prev_box, self.moving_frames = None, 0
                if self.state in ("upright", "alerted"):
                    self.state = "idle"  # person left the view
            return None
        self.missing = 0
        self.present = True
        box, _ = found
        alert = self._check_idle(box, t)
        if alert:
            return alert
        ratio = box[3] / max(box[2], 1e-6)
        cy = box[1] + box[3] / 2
        if ratio > 1.3:  # upright: standing or walking
            if self.state in ("down", "alerted"):
                self.state = "idle"  # got up again
            self.state = "upright" if self.state != "alerted" else self.state
            self.last_upright_t, self.last_upright_cy = t, cy
            return None
        if ratio < 0.8:  # horizontal
            if self.state == "upright" and self.last_upright_t is not None and t - self.last_upright_t <= self.fall_window_s \
                    and cy >= self.last_upright_cy - 0.05 * frame.shape[0]:
                self.state, self.down_box, self.still_since = "down", box, t
                return None
            if self.state == "down":
                if self._iou(box, self.down_box) < 0.6:  # still moving around on the floor
                    self.down_box, self.still_since = box, t
                elif t - self.still_since >= self.still_s and t - self.last_alert >= self.cooldown_s:
                    self.state, self.last_alert = "alerted", t
                    self.idle_alerted = True  # one alert per event: the idle rule stays quiet for this person
                    return {"kind": "possible_fall", "bbox": [round(v) for v in box], "still_s": round(t - self.still_since, 1),
                            "confidence": 0.5, "reason": "upright person became horizontal and stayed still"}
        return None

    def _check_idle(self, box: tuple, t: float) -> dict | None:
        """Person in view who has not moved at all for `idle_s` seconds (collapsed, unconscious, ...)."""
        prev, self.prev_box = getattr(self, "prev_box", None), box
        if prev is not None:
            shift = abs((box[0] + box[2] / 2) - (prev[0] + prev[2] / 2)) + abs((box[1] + box[3] / 2) - (prev[1] + prev[3] / 2))
            if shift > 0.03 * max(box[2], box[3]):
                # Travelled across the view: a person walking, not furniture that simply appeared.
                self.moving_frames = getattr(self, "moving_frames", 0) + 1
                if self.moving_frames >= 3:
                    self.was_moving = True
        if self.motion >= self.MOTION_THRESHOLD:
            self.idle_since, self.idle_alerted = None, False
            return None
        if self.idle_s <= 0 or not self.was_moving:
            return None
        if self.idle_since is None:
            self.idle_since = t
        if not self.idle_alerted and t - self.idle_since >= self.idle_s and t - self.last_alert >= self.cooldown_s:
            self.idle_alerted, self.last_alert = True, t
            return {"kind": "not_moving", "bbox": [round(v) for v in box], "still_s": round(t - self.idle_since, 1),
                    "confidence": 0.4, "reason": "person in view has not moved"}
        return None


def iter_video_frames(path: Path, sample_fps: float) -> Iterator[tuple[int, float, np.ndarray]]:
    """Yield (frame_index, seconds_from_start, frame) sampled at roughly sample_fps."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"cannot open video {path.name}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(round(fps / sample_fps)))
    idx = 0
    try:
        while True:
            ok = cap.grab()
            if not ok:
                break
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    yield idx, idx / fps, frame
            idx += 1
    finally:
        cap.release()


def decode_jpeg(data: bytes) -> np.ndarray | None:
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR) if arr.size else None
