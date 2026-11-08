#!/usr/bin/env python3
"""Curate an account's downloads down to photos that contain people.

Accounts opt in with ``"curate_photos": true`` in config/accounts.json. For
each of them every image sitting directly in ``images/<account>/`` is kept
when it shows at least one person - one or many, group shots and collages
alike, face or body - and moved to ``images/<account>/_skipped/`` when it
shows none (flowers, objects, scenery, screenshots, text cards, memes).

Detection is a cascade: faces first (OpenCV YuNet), then a person detector
(YOLOv8n) for shots where the face is out of frame, turned away or cropped,
so body-only photos are not thrown out with the scenery. The verdict per file
is cached in ``metadata/curation/<account>.json`` so later runs only look at
new downloads.

Both ONNX models are fetched on first use; they are not committed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "accounts.json"
IMAGES = ROOT / "images"
STATE_DIR = ROOT / "metadata" / "curation"
SKIP_DIRNAME = "_skipped"

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

STATE_VERSION = 3

FACE_MODEL = (
    "face_detection_yunet_2023mar.onnx",
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx",
)
PERSON_MODEL = (
    "yolov8n.onnx",
    "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.onnx",
)

# Face confidence. Deliberately low: a face anywhere in the frame (including a
# small one in a collage) is enough to keep the image.
FACE_SCORE = 0.5
# Person confidence for the body fallback.
PERSON_SCORE = 0.35
PERSON_NMS = 0.45
PERSON_INPUT = 640


def log(msg: str) -> None:
    print(msg, flush=True)


def load_accounts() -> list[dict[str, Any]]:
    entries = json.loads(CONFIG.read_text(encoding="utf-8"))["accounts"]
    return [a for a in entries if a.get("curate_photos")]


def fetch_model(models_dir: Path, name: str, url: str) -> Path:
    models_dir.mkdir(parents=True, exist_ok=True)
    path = models_dir / name
    if path.is_file() and path.stat().st_size:
        return path
    log(f"downloading {name}")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (archive curator)"}
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            path.write_bytes(resp.read())
    except Exception as exc:  # noqa: BLE001 - e.g. TLS interception
        # Fall back to curl, which uses the OS trust store and works on both
        # the runners and TLS-inspected developer machines.
        log(f"urllib failed ({exc}); retrying with curl")
        subprocess.run(
            ["curl", "-fsSL", "--retry", "3", "-o", str(path), url], check=True
        )
    return path


def load_state(path: Path) -> dict[str, Any]:
    blank = {"version": STATE_VERSION, "files": {}}
    if not path.is_file():
        return blank
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return blank
    if state.get("version") != STATE_VERSION:
        # Rules changed: every verdict has to be recomputed.
        return blank
    state.setdefault("files", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def count_persons(img: Any, net: Any, cv2: Any, np: Any) -> int:
    """Detect people with YOLOv8n (letterboxed 640 input, class 0 = person)."""
    height, width = img.shape[:2]
    gain = min(PERSON_INPUT / width, PERSON_INPUT / height)
    new_w, new_h = int(round(width * gain)), int(round(height * gain))
    resized = cv2.resize(img, (new_w, new_h))
    canvas = np.full((PERSON_INPUT, PERSON_INPUT, 3), 114, np.uint8)
    pad_x = (PERSON_INPUT - new_w) // 2
    pad_y = (PERSON_INPUT - new_h) // 2
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized

    blob = cv2.dnn.blobFromImage(
        canvas, 1 / 255.0, (PERSON_INPUT, PERSON_INPUT),
        swapRB=True, crop=False,
    )
    net.setInput(blob)
    out = net.forward()
    preds = out[0].T
    scores = preds[:, 4]  # class 0 = person
    mask = scores > PERSON_SCORE
    if not mask.any():
        return 0
    boxes = []
    for row in preds[mask]:
        cx, cy, bw, bh = row[:4]
        boxes.append([
            int((cx - bw / 2 - pad_x) / gain),
            int((cy - bh / 2 - pad_y) / gain),
            int(bw / gain),
            int(bh / gain),
        ])
    kept = cv2.dnn.NMSBoxes(boxes, scores[mask].tolist(), PERSON_SCORE, PERSON_NMS)
    return len(kept)


def count_people(img: Any, detector: Any, person_net: Any, cv2: Any,
                 np: Any) -> tuple[int, int]:
    """Return (faces, persons) found in an image."""
    height, width = img.shape[:2]
    detector.setInputSize((width, height))
    _retval, faces = detector.detect(img)
    faces = 0 if faces is None else len(faces)
    if faces:
        return faces, 0
    return 0, count_persons(img, person_net, cv2, np)


def classify_folder(
    account: str, models: tuple[Path, Path], state: dict[str, Any]
) -> dict[str, Any]:
    import cv2  # imported here so a config-only run stays cheap
    import numpy as np

    face_model, person_model = models
    detector = cv2.FaceDetectorYN.create(str(face_model), "", (320, 320),
                                         FACE_SCORE, 0.3, 5000)
    person_net = cv2.dnn.readNetFromONNX(str(person_model))

    folder = IMAGES / account
    if not folder.is_dir():
        log(f"{account}: no images folder, nothing to curate")
        return state

    candidates = sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    known: dict[str, Any] = state["files"]
    present = {p.name for p in candidates}
    for name in list(known):
        if name not in present:
            known.pop(name, None)

    fresh = [p for p in candidates if p.name not in known]
    log(f"{account}: {len(candidates)} images, {len(fresh)} needing a look")

    skipped_dir = folder / SKIP_DIRNAME
    kept = moved = unreadable = 0
    for path in fresh:
        data = np.fromfile(str(path), dtype=np.uint8)  # unicode-safe read
        img = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
        if img is None:
            unreadable += 1
            continue
        faces, persons = count_people(img, detector, person_net, cv2, np)
        keep = faces > 0 or persons > 0
        if faces:
            reason = f"faces={faces}"
        elif persons:
            reason = f"persons={persons}"
        else:
            reason = "no people"
        known[path.name] = {
            "keep": keep,
            "faces": faces,
            "persons": persons,
            "reason": reason,
        }
        if keep:
            kept += 1
            continue
        skipped_dir.mkdir(exist_ok=True)
        dest = skipped_dir / path.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(path), str(dest))
        moved += 1

    log(f"{account}: kept {kept} with people, moved {moved} to {SKIP_DIRNAME}/"
        + (f", {unreadable} unreadable" if unreadable else ""))
    return state


def main() -> int:
    accounts = load_accounts()
    if not accounts:
        log("No account opted into curate_photos; nothing to do.")
        return 0

    wanted = os.environ.get("CURATE_ACCOUNT", "").strip().lower()
    if wanted:
        accounts = [a for a in accounts if a["username"].lower() == wanted]
        if not accounts:
            log(f"CURATE_ACCOUNT {wanted!r} is not a curated account; nothing to do.")
            return 0

    with tempfile.TemporaryDirectory(prefix="curate-models-") as tmp:
        models_dir = Path(tmp)
        models = (
            fetch_model(models_dir, *FACE_MODEL),
            fetch_model(models_dir, *PERSON_MODEL),
        )
        for entry in accounts:
            account = entry["username"]
            state_path = STATE_DIR / f"{account}.json"
            state = load_state(state_path)
            try:
                state = classify_folder(account, models, state)
            except Exception as exc:  # noqa: BLE001 - never fail the archive run
                log(f"WARNING: curating {account} failed: {exc}")
                continue
            save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
