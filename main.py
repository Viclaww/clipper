import json
import os
import subprocess
import uuid

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI()
CLIPS_DIR = "/tmp/clips"
os.makedirs(CLIPS_DIR, exist_ok=True)


class DownloadRequest(BaseModel):
    url: str
    start: str = "00:00:00"
    end: str = "00:01:00"
    title: str = ""


class CompileRequest(BaseModel):
    clip_ids: list[str]
    format: str = "best_moments"
    voiceover: str = ""  # base64 audio


@app.post("/download")
def download(req: DownloadRequest):
    clip_id = str(uuid.uuid4())
    raw_path = f"{CLIPS_DIR}/{clip_id}_raw.mp4"
    out_path = f"{CLIPS_DIR}/{clip_id}.mp4"

    # Download
    subprocess.run(
        ["yt-dlp", "-f", "bv[ext=mp4]+ba[ext=m4a]/b[ext=mp4]", "-o", raw_path, req.url],
        check=True,
    )

    # Trim
    subprocess.run(
        [
            "ffmpeg",
            "-i",
            raw_path,
            "-ss",
            req.start,
            "-to",
            req.end,
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            out_path,
        ],
        check=True,
    )

    os.remove(raw_path)

    return {"clip_id": clip_id, "title": req.title, "path": out_path}


@app.post("/compile")
def compile(req: CompileRequest):
    output_id = str(uuid.uuid4())
    output_path = f"{CLIPS_DIR}/{output_id}_final.mp4"
    concat_list = f"{CLIPS_DIR}/{output_id}_concat.txt"
    voiceover_path = f"{CLIPS_DIR}/{output_id}_voice.mp3"

    # Write concat list
    with open(concat_list, "w") as f:
        for clip_id in req.clip_ids:
            f.write(f"file '{CLIPS_DIR}/{clip_id}.mp4'\n")

    # Concatenate clips
    merged_path = f"{CLIPS_DIR}/{output_id}_merged.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list,
            "-c",
            "copy",
            merged_path,
        ],
        check=True,
    )

    # Decode and save voiceover if provided
    if req.voiceover:
        import base64

        with open(voiceover_path, "wb") as f:
            f.write(base64.b64decode(req.voiceover))

        # Mix voiceover over video (lower original audio)
        subprocess.run(
            [
                "ffmpeg",
                "-i",
                merged_path,
                "-i",
                voiceover_path,
                "-filter_complex",
                "[0:a]volume=0.2[orig];[1:a]volume=1.0[voice];[orig][voice]amix=inputs=2:duration=longest[aout]",
                "-map",
                "0:v",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                output_path,
            ],
            check=True,
        )
        os.remove(voiceover_path)
    else:
        os.rename(merged_path, output_path)

    # Cleanup
    os.remove(concat_list)
    if os.path.exists(merged_path):
        os.remove(merged_path)

    return {"output_id": output_id, "path": output_path}


@app.get("/health")
def health():
    return {"status": "ok"}
