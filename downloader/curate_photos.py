#!/usr/bin/env python3
"""Curate an account's downloads down to photos of people.

Accounts opt in with ``"curate_photos": true`` in config/accounts.json. For
each of them this pass looks at every image sitting directly in
``images/<account>/`` and decides:

  * keep  - at least one face matches the account owner, or the image has
            two or more faces (her with company / a couple of girls)
  * skip  - anything else: screenshots, memes, text cards, unrelated media.
            These are moved to ``images/<account>/_skipped/`` so the account
            folder itself is exactly "photos where she is in it".

Identity is learned from the archive itself: the largest face cluster across
her media is taken to be her, and its centroid is cached in
``metadata/curation/<account>.json`` together with a per-file verdict so
later runs only classify what is new.

Face detection/recognition uses OpenCV's YuNet + SFace ONNX models, fetched
from the opencv_zoo repository on first use (they are not committed).
"""

from __future__ import annotations

import json
import math
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

MODELS = {
    "yunet": (
        "face_detection_yunet_2023mar.onnx",
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ),
    "sface": (
        "face_recognition_sface_2021dec.onnx",
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_recognition_sface/face_recognition_sface_2021dec.onnx",
    ),
}

# YuNet confidence for a usable face box.
DETECT_SCORE = 0.6
# Cosine similarity at or above this counts as the same person (SFace's
# published verification threshold is 0.363; keep a margin for recompression).
IDENTITY_SIM = 0.40
# Greedy clustering threshold when learning her identity from scratch.
CLUSTER_SIM = 0.45
# Refuse to learn an identity from a handful of faces.
MIN_IDENTITY_FACES = 10


def log(msg: str) -> None:
    print(msg, flush=True)


def load_accounts() -> list[dict[str, Any]]:
    entries = json.loads(CONFIG.read_text(encoding="utf-8"))["accounts"]
    return [a for a in entries if a.get("curate_photos")]


def model_paths(models_dir: Path) -> tuple[Path, Path]:
    models_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for key, (filename, url) in MODELS.items():
        path = models_dir / filename
        if not path.is_file() or path.stat().st_size == 0:
            log(f"downloading {filename}")
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 (archive curator)"}
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    path.write_bytes(resp.read())
            except Exception as exc:  # noqa: BLE001 - e.g. TLS interception
                # Fall back to curl, which uses the OS trust store and works
                # on both the runners and TLS-inspected developer machines.
                log(f"urllib failed ({exc}); retrying with curl")
                subprocess.run(
                    ["curl", "-fsSL", "--retry", "3", "-o", str(path), url],
                    check=True,
                )
        paths.append(path)
    return paths[0], paths[1]


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "centroid": None, "files": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"version": 1, "centroid": None, "files": {}}
    state.setdefault("version", 1)
    state.setdefault("centroid", None)
    state.setdefault("files", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    total = [0.0] * dim
    for vec in vectors:
        for i, value in enumerate(vec):
            total[i] += value
    n = float(len(vectors))
    return [value / n for value in total]


def learn_identity(
    embeddings: list[tuple[str, int, list[float]]]
) -> list[float] | None:
    """Return the centroid of the identity that appears in most images."""
    if len(embeddings) < MIN_IDENTITY_FACES:
        return None
    clusters: list[dict[str, Any]] = []
    for name, _index, vec in embeddings:
        best = None
        best_sim = 0.0
        for cluster in clusters:
            sim = cosine(vec, cluster["centroid"])
            if sim >= CLUSTER_SIM and sim > best_sim:
                best, best_sim = cluster, sim
        if best is None:
            clusters.append({"vectors": [vec], "centroid": vec, "images": {name}})
        else:
            best["vectors"].append(vec)
            best["centroid"] = mean_vector(best["vectors"])
            best["images"].add(name)
    # Weight by distinct images, not raw face count: a single group photo
    # must not outvote the owner who appears in hundreds of solo shots.
    dominant = max(clusters, key=lambda c: len(c["images"]))
    log(f"learned identity from {len(dominant['images'])} images "
        f"({len(dominant['vectors'])} faces, {len(clusters)} clusters)")
    return dominant["centroid"]


def classify_folder(
    account: str, models: tuple[Path, Path], state: dict[str, Any]
) -> dict[str, Any]:
    import cv2  # imported here so --help style runs stay cheap
    import numpy as np

    yunet_path, sface_path = models
    detector = cv2.FaceDetectorYN.create(str(yunet_path), "", (320, 320),
                                         DETECT_SCORE, 0.3, 5000)
    recognizer = cv2.FaceRecognizerSF.create(str(sface_path), "")

    folder = IMAGES / account
    if not folder.is_dir():
        log(f"{account}: no images folder, nothing to curate")
        return state

    candidates = sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    known: dict[str, Any] = state["files"]
    # Forget files that are no longer in the account folder.
    present = {p.name for p in candidates}
    for name in list(known):
        if name not in present:
            known.pop(name, None)

    fresh = [p for p in candidates if p.name not in known]
    log(f"{account}: {len(candidates)} images, {len(fresh)} needing a look")

    embeddings: list[tuple[str, int, list[float]]] = []
    face_counts: dict[str, int] = {}
    failures: list[str] = []
    for path in fresh:
        data = np.fromfile(str(path), dtype=np.uint8)  # unicode-safe read
        img = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
        if img is None:
            failures.append(path.name)
            continue
        height, width = img.shape[:2]
        detector.setInputSize((width, height))
        _retval, faces = detector.detect(img)
        count = 0 if faces is None else len(faces)
        face_counts[path.name] = count
        if not count:
            continue
        for index, face in enumerate(faces):
            try:
                aligned = recognizer.alignCrop(img, face)
                feature = recognizer.feature(aligned).flatten().tolist()
            except Exception:  # noqa: BLE001 - a bad crop is just skipped
                continue
            embeddings.append((path.name, index, feature))

    centroid = state.get("centroid")
    if centroid is None and embeddings:
        centroid = learn_identity(embeddings)
        state["centroid"] = centroid
    if centroid is None:
        log(f"{account}: not enough faces yet to learn her identity; "
            f"leaving this run's images in place")
        for name, count in face_counts.items():
            if count >= 2:
                known[name] = {"keep": True, "faces": count, "reason": "group"}
        return state

    sims: dict[str, float] = {}
    for name, _index, vec in embeddings:
        sim = cosine(vec, centroid)
        if sim > sims.get(name, -1.0):
            sims[name] = sim

    skipped_dir = folder / SKIP_DIRNAME
    kept = moved = 0
    for path in fresh:
        if path.name in failures:
            continue
        count = face_counts.get(path.name, 0)
        sim = sims.get(path.name, 0.0)
        if count >= 2:
            verdict, reason = True, "group"
        elif sim >= IDENTITY_SIM:
            verdict, reason = True, f"her (sim={sim:.2f})"
        else:
            verdict, reason = False, f"no match (faces={count}, sim={sim:.2f})"
        known[path.name] = {"keep": verdict, "faces": count, "reason": reason}
        if verdict:
            kept += 1
        else:
            skipped_dir.mkdir(exist_ok=True)
            dest = skipped_dir / path.name
            if dest.exists():
                dest.unlink()
            shutil.move(str(path), str(dest))
            moved += 1

    log(f"{account}: kept {kept}, moved {moved} to {SKIP_DIRNAME}/")
    return state


def main() -> int:
    accounts = load_accounts()
    if not accounts:
        log("No account opted into curate_photos; nothing to do.")
        return 0

    extra = ""
    if os.environ.get("CURATE_ACCOUNT"):
        extra = os.environ["CURATE_ACCOUNT"].strip().lower()
        accounts = [a for a in accounts if a["username"].lower() == extra]
        if not accounts:
            log(f"CURATE_ACCOUNT {extra!r} is not a curated account; nothing to do.")
            return 0

    with tempfile.TemporaryDirectory(prefix="curate-models-") as tmp:
        models = model_paths(Path(tmp))
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
