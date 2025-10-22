import os
import io
import time
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional

import numpy as np
import requests
import av
import pydicom
from pydicom.dataset import FileDataset
from pydicom.uid import generate_uid, ExplicitVRLittleEndian

# --- ORTHANC --
import requests as http  # cliente HTTP para hablar con Orthanc

# --- OBS WebSocket (API moderna) ---
from obsws_python import ReqClient as ObsClient

from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from flask import Flask, render_template

# --------- Config entorno ----------
ORTHANC_URL   = os.getenv("ORTHANC_URL",  "http://3.222.98.144:8042")
ORTHANC_USER  = os.getenv("ORTHANC_USER", "admin")
ORTHANC_PASS  = os.getenv("ORTHANC_PASS", "Cambia_Esta_Clave_MuyFuerte_1!")
OHIF_BASE_URL = os.getenv("OHIF_BASE_URL", "http://3.222.98.144:8042/ohif")

UPLOADS_DIR = "uploads"; os.makedirs(UPLOADS_DIR, exist_ok=True)
OUT_DIR     = "out";     os.makedirs(OUT_DIR, exist_ok=True)

def resource_path(*paths):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    base = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    return os.path.join(base, *paths)


app = Flask(__name__, template_folder=resource_path("templates"))

# --------- Util Orthanc ----------
def _url(path: str) -> str:
    return ORTHANC_URL.rstrip("/") + "/" + path.lstrip("/")

def orthanc_get(path: str, accept_json: bool = True) -> Any:
    headers = {"Accept": "application/json"} if accept_json else {}
    r = requests.get(_url(path), auth=(ORTHANC_USER, ORTHANC_PASS), headers=headers, timeout=30)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json() if accept_json else r.content

def orthanc_post(path: str, files: Dict[str, tuple] = None,
                 data: bytes = None, headers: Dict[str, str] = None) -> Any:
    if files:
        r = requests.post(_url(path), auth=(ORTHANC_USER, ORTHANC_PASS), files=files, timeout=600)
    else:
        r = requests.post(_url(path), auth=(ORTHANC_USER, ORTHANC_PASS),
                          headers=headers or {}, data=data, timeout=600)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    try:
        return r.json()
    except Exception:
        return r.text



# --------- Conversión MP4 -> DICOM multiframe ----------
def _decode_video_to_gray8(
    mp4_path: str,
    target_fps: Optional[float] = None,
    max_frames: Optional[int] = None,
) -> np.ndarray:
    if not os.path.exists(mp4_path):
        raise HTTPException(404, f"No existe el archivo: {mp4_path}")

    container = av.open(mp4_path)
    stream = next((s for s in container.streams if s.type == "video"), None)
    if stream is None:
        raise HTTPException(400, "No se encontró un stream de video en el MP4")

    src_fps = float(stream.average_rate) if stream.average_rate else None
    frames = []
    frame_index = 0
    next_keep = 0.0
    step = 1.0

    if src_fps and target_fps and target_fps > 0:
        step = max(1.0, src_fps / target_fps)

    for packet in container.demux(stream):
        for frame in packet.decode():
            take = False
            if src_fps and target_fps and target_fps > 0:
                if frame_index + 1 >= next_keep:
                    take = True
                    next_keep += step
            else:
                take = True
            frame_index += 1

            if take:
                img = frame.to_ndarray(format="gray")  # (H, W), uint8
                frames.append(img)
                if max_frames and len(frames) >= max_frames:
                    break
        if max_frames and len(frames) >= max_frames:
            break

    container.close()

    if not frames:
        raise HTTPException(400, "No se pudieron decodificar frames del video")

    h = min(f.shape[0] for f in frames)
    w = min(f.shape[1] for f in frames)
    frames = [f[:h, :w] for f in frames]
    arr = np.stack(frames, axis=0).astype(np.uint8)  # [N, H, W]
    return arr


def mp4_to_multiframe_dicom(
    mp4_path: str,
    out_path: str,
    patient_id: str = "0001",
    patient_name: str = "Anon_001",
    body_part: str = "LUNG",
    target_fps: float = 6.0,
) -> str:
    frames = _decode_video_to_gray8(mp4_path, target_fps=target_fps)
    n, h, w = frames.shape

    file_meta = pydicom.Dataset()
    file_meta.MediaStorageSOPClassUID    = pydicom.uid.SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID          = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID     = generate_uid()

    ds = FileDataset(out_path, {}, file_meta=file_meta, preamble=b"\0"*128)
    now = datetime.utcnow()
    ds.ContentDate = now.strftime("%Y%m%d")
    ds.ContentTime = now.strftime("%H%M%S.%f")[:13]

    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.Modality = "OT"
    ds.SeriesDescription = "LUS Multi-frame (SC)"
    ds.ImageType = ["DERIVED", "SECONDARY"]
    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.BodyPartExamined = body_part

    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = int(h)
    ds.Columns = int(w)
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0

    ds.NumberOfFrames = str(n)
    if target_fps and target_fps > 0:
        ds.FrameTime = 1000.0 / target_fps  # ms
        ds.CineRate  = int(round(target_fps))

    ds.PixelData = frames.tobytes(order="C")

    ds.StudyID = "LUS"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1

    pydicom.dcmwrite(out_path, ds, write_like_original=False)
    return out_path



# =========================
# Configuración OBS
# =========================
OBS_HOST = os.getenv("OBS_HOST", "localhost")
OBS_PORT = int(os.getenv("OBS_PORT", "4455"))
OBS_PASS = os.getenv("OBS_PASSWORD", "dhiago")

def get_client() -> ObsClient:
    return ObsClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASS)

# =========================
# Endpoints OBS
# =========================
@app.route("/health")
def health():
    return {"ok": True}

@app.route("/record/start", methods=["GET"])
def record_start():
    c = get_client()
    c.start_record()
    return {"ok": True, "msg": "Recording started"}

@app.route("/record/stop", methods=["GET"])
def record_stop():
    c = get_client()
    c.stop_record()
    s = c.get_record_status()
    # Algunas versiones exponen snake_case; otras camelCase
    path = getattr(s, "output_path", None) or getattr(s, "outputPath", None)
    active = getattr(s, "output_active", None) or getattr(s, "outputActive", None)
    return {"ok": True, "file": path, "recording": active}



# --------- PACS basicos ----------
@app.get("/pacs/ping")
def pacs_ping():
    try:
        r = http.get(f"{ORTHANC_URL}/system", auth=(ORTHANC_USER, ORTHANC_PASS), timeout=5)
        if r.status_code == 200:
            j = r.json()
            return {"ok": True, "orthanc": j.get("Version", "unknown"), "name": j.get("Name", "Orthanc")}
        raise HTTPException(status_code=502, detail=f"Orthanc respondió {r.status_code}: {r.text[:180]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo contactar a Orthanc: {e}")


@app.post("/workflow/upload-convert-send")
def upload_convert_send(
    file: UploadFile = File(...),
    target_fps: float = Body(6.0),
    patient_id: str = Body("0001"),
    patient_name: str = Body("Anon_001"),
    body_part: str = Body("LUNG"),
):
    print("FILE:", file)
    mp4_name = file.filename or f"video_{int(time.time())}.mp4"
    mp4_path = os.path.join(UPLOADS_DIR, mp4_name)
    with open(mp4_path, "wb") as f:
        f.write(file.file.read())

    base = os.path.splitext(os.path.basename(mp4_path))[0]
    dicom_path = os.path.join(OUT_DIR, f"{base}.dcm")
    mp4_to_multiframe_dicom(
        mp4_path=mp4_path,
        out_path=dicom_path,
        patient_id=patient_id,
        patient_name=patient_name,
        body_part=body_part,
        target_fps=target_fps,
    )

    with open(dicom_path, "rb") as fh:
        files = {"file": (os.path.basename(dicom_path), fh, "application/dicom")}
        res = orthanc_post("/instances", files=files)

    return {"ok": True, "mp4": mp4_path, "dicom": dicom_path, "orthanc": res}


@app.route("/", methods=["GET"])
def index():
  return  render_template('home.html')

if __name__ == "__main__":
    app.run()

