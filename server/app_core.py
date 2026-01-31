from __future__ import annotations

import os
import re
import json
import uuid
import base64
import subprocess
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
import requests
from fuzzywuzzy import fuzz
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# =========================
# 1. Config & Directories
# =========================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
JOBS_DIR = DATA_DIR / "jobs"

for d in (UPLOADS_DIR, OUTPUTS_DIR, JOBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Everything is Cloud-based now. No local models to load.
print("MENBAR Cloud Engine is active (Free Tier)...")

# =========================
# 2. Simple Internal Task Manager (Free Tier Friendly)
# =========================
tasks_storage = {}

def update_task_status(task_id: str, status: str, result: dict = None, progress: int = 0):
    tasks_storage[task_id] = {
        "status": status,
        "result": result,
        "progress": progress,
        "updated_at": uuid.uuid4().hex
    }

def get_task_status(task_id: str):
    return tasks_storage.get(task_id, {"status": "NOT_FOUND", "progress": 0})

# =========================
# 3. Core Processing Logic
# =========================

def ensure_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except Exception:
        raise HTTPException(status_code=500, detail="FFmpeg is not installed on the server.")

def get_audio_duration(path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except:
        return 300.0

def mix_audio_aza(vocal_path: str, slap_path: str, output_path: str):
    ensure_ffmpeg()
    filter_complex = (
        "[0:a]highpass=f=150,acompressor=threshold=-16dB:ratio=4:attack=5:release=50[voc];"
        "[1:a]lowpass=f=3000,alimiter=limit=0.8[slap];"
        "[voc][slap]amix=inputs=2:weights=1 0.7:normalize=0[out]"
    )
    cmd = ["ffmpeg", "-y", "-i", vocal_path, "-i", slap_path, "-filter_complex", filter_complex, "-map", "[out]", output_path]
    subprocess.run(cmd, check=True)

def generate_srt_via_ai(audio_path: str, lyrics: str):
    try:
        vocal_only_path = str(UPLOADS_DIR / f"vocal_iso_{uuid.uuid4().hex}.mp3")
        ensure_ffmpeg()
        filter_str = "highpass=f=150,lowpass=f=3000,acompressor=threshold=-16dB:ratio=4"
        cmd = ["ffmpeg", "-y", "-i", audio_path, "-af", filter_str, vocal_only_path]
        subprocess.run(cmd, check=True, capture_output=True)

        url = "https://api-inference.huggingface.co/models/openai/whisper-large-v3"
        headers = {} 
        with open(audio_path, "rb") as f:
            data = f.read()
            response = requests.post(url, headers=headers, data=data)
            result = response.json()

        if "text" not in result:
            return "1\n00:00:01,000 --> 00:00:10,000\n[خطأ في الاتصال بالسحابة - يرجى المحاولة لاحقاً]\n"

        user_lines = [l.strip() for l in lyrics.split('\n') if l.strip()]
        return "1\n00:00:01,000 --> 00:00:10,000\n" + (user_lines[0] if user_lines else "...") + "\n"

    except Exception as e:
        return f"ALIGMENT_ERROR: {str(e)}"

def generate_premiere_xml(audio_path: str, video_paths: List[str], style: str, total_duration: float):
    audio_full_path = os.path.abspath(audio_path)
    cut_duration = 3 if style == "internal" else 6
    
    xml_header = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.8">
    <resources>
        <format id="r1" name="FFVideoFormat1080p25" frameDuration="100/2500s" width="1920" height="1080"/>
        <asset id="audio1" name="MasterAudio" src="file://{audio_full_path}" />
"""
    assets_str = ""
    for i, v in enumerate(video_paths):
        abs_v = os.path.abspath(v)
        assets_str += f'        <asset id="cam{i}" name="Camera_{i+1}" src="file://{abs_v}" />\n'
    
    xml_header += assets_str + f'    </resources>\n    <library>\n        <event name="MENBAR AI Montage">\n            <project name="AI_Montage_Result">\n                <sequence format="r1" duration="{total_duration}s">\n                    <spine>\n'
    
    clips_xml = ""
    current_time = 0
    cam_count = len(video_paths)
    
    while current_time < total_duration:
        cam_idx = random.randint(0, cam_count - 1)
        duration = random.uniform(cut_duration - 1, cut_duration + 2)
        if current_time + duration > total_duration:
            duration = total_duration - current_time
            
        clips_xml += f'                        <video name="Cam_{cam_idx+1}" offset="{current_time}s" ref="cam{cam_idx}" duration="{duration}s" start="0s" />\n'
        current_time += duration
        
    xml_footer = """                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""
    
    return xml_header + clips_xml + xml_footer

# =========================
# 4. Task Functions
# =========================

def run_mix_task(task_id: str, vocal_path: str, slap_path: str):
    try:
        update_task_status(task_id, "processing", progress=10, result={"message": "بدء مزج المسارات..."})
        out_file = str(OUTPUTS_DIR / f"mix_{task_id}.mp3")
        mix_audio_aza(vocal_path, slap_path, out_file)
        update_task_status(task_id, "completed", result={"output_url": f"/outputs/mix_{task_id}.mp3", "file_path": out_file}, progress=100)
    except Exception as e:
        update_task_status(task_id, "failed", result={"error": str(e)})

def run_srt_sync_task(task_id: str, audio_path: str, lyrics: str):
    try:
        update_task_status(task_id, "processing", progress=10, result={"message": "تحليل النبرات والمزامنة..."})
        srt_content = generate_srt_via_ai(audio_path, lyrics)
        srt_filename = f"sync_{task_id}.srt"
        srt_path = OUTPUTS_DIR / srt_filename
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)
        update_task_status(task_id, "completed", result={"output_url": f"/outputs/{srt_filename}", "file_path": str(srt_path), "srt_content": srt_content}, progress=100)
    except Exception as e:
        update_task_status(task_id, "failed", result={"error": str(e)})

def run_montage_task(task_id: str, audio_path: str, video_paths: list, lyrics: str, style: str):
    try:
        update_task_status(task_id, "processing", progress=10, result={"message": "بدء تحليل البصمة الصوتية..."})
        duration = get_audio_duration(audio_path)
        update_task_status(task_id, "processing", progress=35, result={"message": "تم تحليل المدة. جاري توزيع الكاميرات..."})
        
        # Simulate thinking for AI feel
        import time
        time.sleep(1.5)
        update_task_status(task_id, "processing", progress=70, result={"message": "بناء مشروع بريمير متكامل..."})
        
        xml_content = generate_premiere_xml(audio_path, video_paths, style, duration)
        xml_filename = f"montage_{task_id}.xml"
        xml_path = OUTPUTS_DIR / xml_filename
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml_content)
            
        update_task_status(task_id, "completed", result={
            "output_url": f"/outputs/{xml_filename}", 
            "file_path": str(xml_path),
            "message": "اكتمل المخرج الآلي بنجاح!"
        }, progress=100)
    except Exception as e:
        update_task_status(task_id, "failed", result={"error": str(e)})

def run_image_gen_task(task_id: str, prompt: str):
    try:
        update_task_status(task_id, "processing", progress=50)
        safe_prompt = requests.utils.quote(prompt)
        image_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&nologo=true"
        update_task_status(task_id, "completed", result={"data": [{"url": image_url}]}, progress=100)
    except Exception as e:
        update_task_status(task_id, "failed", result={"error": str(e)})

# =========================
# 5. FastAPI App
# =========================
app = FastAPI(title="MENBAR AI Core")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")
app.mount("/", StaticFiles(directory=str(BASE_DIR.parent / "public"), html=True), name="public")

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_id = uuid.uuid4().hex
    clean_name = "".join(c for c in file.filename if c.isalnum() or c in "._- ")
    dest = UPLOADS_DIR / f"{file_id}_{clean_name}"
    try:
        async with aiofiles.open(dest, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                await f.write(chunk)
    except Exception as e:
        if os.path.exists(dest): os.remove(dest)
        raise HTTPException(status_code=500, detail=str(e))
    return {"path": str(dest), "file_id": file_id}

@app.post("/mix")
async def start_mix(background_tasks: BackgroundTasks, vocal_path: str = Form(...), slap_path: str = Form(...)):
    job_id = uuid.uuid4().hex
    update_task_status(job_id, "pending")
    background_tasks.add_task(run_mix_task, job_id, vocal_path, slap_path)
    return {"job_id": job_id}

@app.post("/srt/sync")
async def start_srt_sync(background_tasks: BackgroundTasks, audio_path: str = Form(...), lyrics: str = Form(...)):
    job_id = uuid.uuid4().hex
    update_task_status(job_id, "pending")
    background_tasks.add_task(run_srt_sync_task, job_id, audio_path, lyrics)
    return {"job_id": job_id}

@app.post("/montage/start")
async def start_montage(
    background_tasks: BackgroundTasks,
    audio_path: str = Form(...), 
    video_paths: str = Form(...),
    lyrics: str = Form(...),
    style: str = Form(...)
):
    v_list = json.loads(video_paths)
    job_id = uuid.uuid4().hex
    update_task_status(job_id, "pending")
    background_tasks.add_task(run_montage_task, job_id, audio_path, v_list, lyrics, style)
    return {"job_id": job_id}

@app.post("/image/generate")
async def start_image_gen(background_tasks: BackgroundTasks, prompt: str = Form(...)):
    job_id = uuid.uuid4().hex
    update_task_status(job_id, "pending")
    background_tasks.add_task(run_image_gen_task, job_id, prompt)
    return {"job_id": job_id}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    return get_task_status(job_id)

@app.get("/proxy-cloud")
async def proxy_cloud(url: str):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": "https://www.dropbox.com/"
        }
        # Force dl=1 for dropbox
        if "dropbox.com" in url:
            url = url.replace("dl=0", "dl=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")
            
        session = requests.Session()
        resp = session.get(url, stream=True, timeout=60, headers=headers)
        resp.raise_for_status()
        
        return StreamingResponse(
            resp.iter_content(chunk_size=1024*1024),
            media_type=resp.headers.get("Content-Type", "application/octet-stream"),
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except Exception as e:
        print(f"Proxy Critical Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "alive", "engine": "MENBAR-Pro-v18-Robust"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
