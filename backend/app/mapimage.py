"""Floor-plan image preparation (perspective rectification) and assisted feature detection."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def rectify(src: Path, dst: Path, corners: list[list[float]] | None, rotate: int, out_width: int) -> tuple[int, int]:
    """Warp the quadrilateral `corners` (TL, TR, BR, BL in source pixels) to an upright rectangle.

    Without corners, the photo is used as-is (only rotated). Returns (width, height).
    """
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("cannot read source image")
    if corners:
        q = np.array(corners, dtype=np.float32)
        if q.shape != (4, 2):
            raise ValueError("corners must be four [x, y] points: TL, TR, BR, BL")
        h0, w0 = img.shape[:2]
        if (q[:, 0] < 0).any() or (q[:, 1] < 0).any() or (q[:, 0] > w0).any() or (q[:, 1] > h0).any():
            raise ValueError("corners must lie inside the source image")
        top = np.linalg.norm(q[1] - q[0])
        bottom = np.linalg.norm(q[2] - q[3])
        left = np.linalg.norm(q[3] - q[0])
        right = np.linalg.norm(q[2] - q[1])
        w, h = (top + bottom) / 2, (left + right) / 2
        if w < 50 or h < 50:
            raise ValueError("selected area is too small")
        out_w = int(out_width)
        out_h = int(round(out_w * h / w))
        target = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32)
        m = cv2.getPerspectiveTransform(q, target)
        img = cv2.warpPerspective(img, m, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    rot = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rotate]
    if rot is not None:
        img = cv2.rotate(img, rot)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dst), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return img.shape[1], img.shape[0]


def detect_exit_signs(path: Path) -> list[dict]:
    """Propose locations of the dark-green 'EXIT' boxes printed on evacuation plans.

    Colour + shape heuristic only; every result is a draft that an administrator must confirm.
    """
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return []
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (45, 60, 10), (95, 255, 115))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    img_area = w * h
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        area = bw * bh
        if not (img_area * 0.0004 <= area <= img_area * 0.006):
            continue
        aspect = bw / max(bh, 1)
        fill = cv2.contourArea(c) / max(area, 1)
        if fill < 0.7 or not (0.4 <= aspect <= 2.2):
            continue
        # Square-ish boxes are the large "EXIT" labels at stair doors; elongated ones are
        # the running-man direction signs along corridors.
        label = "EXIT label" if 1.0 <= aspect <= 1.45 and area >= img_area * 0.0006 else "Exit direction sign"
        out.append({"x": (x + bw / 2) / w, "y": (y + bh / 2) / h, "score": round(fill, 2), "label": label,
                    "bbox": [x / w, y / h, bw / w, bh / h]})
    return sorted(out, key=lambda d: (d["label"] != "EXIT label", -d["score"]))
