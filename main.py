import asyncio
import base64
import os
import subprocess
import uuid

import cloudinary
import cloudinary.uploader
import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()
CLIPS_DIR = "/tmp/clips"
COOKIES_FILE = "/tmp/yt_cookies.txt"
os.makedirs(CLIPS_DIR, exist_ok=True)


@app.on_event("startup")
def startup():
    yt_cookies = os.getenv("YT_COOKIES_B64")
    if yt_cookies:
        with open(COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(yt_cookies))
        print("Cookies file written")

    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    )
    print("Cloudinary configured")


download_semaphore = asyncio.Semaphore(1)


class DownloadRequest(BaseModel):
    url: str
    start: str = "00:00:00"
    end: str = "00:00:20"  # 20 seconds per clip → ~1:40 total
    title: str = ""


def build_ytdlp_cmd(url: str, output: str) -> list:
    cmd = [
        "yt-dlp",
        "-f",
        "bv[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
        "--merge-output-format",
        "mp4",
        "--extractor-args",
        "youtube:player_client=android,web",
        "-o",
        output,
    ]
    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    cmd.append(url)
    return cmd


@app.post("/download")
async def download(req: DownloadRequest):
    async with download_semaphore:
        clip_id = str(uuid.uuid4())
        raw_path = f"{CLIPS_DIR}/{clip_id}_raw.mp4"
        out_path = f"{CLIPS_DIR}/{clip_id}.mp4"

        await asyncio.sleep(2)

        try:
            result = subprocess.run(
                build_ytdlp_cmd(req.url, raw_path), capture_output=True, text=True
            )
            if result.returncode != 0:
                raise HTTPException(
                    status_code=500, detail=f"yt-dlp error: {result.stderr[-500:]}"
                )

            # Re-encode to consistent 720p/30fps from the start
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    raw_path,
                    "-ss",
                    req.start,
                    "-to",
                    req.end,
                    "-vf",
                    "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "23",
                    "-c:a",
                    "aac",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    "-r",
                    "30",
                    "-y",
                    out_path,
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                raise HTTPException(
                    status_code=500, detail=f"ffmpeg error: {result.stderr[-500:]}"
                )

            os.remove(raw_path)

            upload_result = cloudinary.uploader.upload(
                out_path,
                resource_type="video",
                public_id=f"speed_clips/{clip_id}",
                overwrite=True,
            )

            os.remove(out_path)

            return {
                "clip_id": clip_id,
                "title": req.title,
                "cloudinary_url": upload_result["secure_url"],
            }

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/compile")
async def compile(
    clip_ids: str = Form(...),
    format: str = Form("best_moments"),
    voiceover: UploadFile = File(None),
):
    import json

    clip_id_list = json.loads(clip_ids)
    total = len(clip_id_list)

    output_id = str(uuid.uuid4())
    concat_list = f"{CLIPS_DIR}/{output_id}_concat.txt"
    merged_path = f"{CLIPS_DIR}/{output_id}_merged.mp4"
    voiceover_path = f"{CLIPS_DIR}/{output_id}_voice.mp3"
    output_path = f"{CLIPS_DIR}/{output_id}_final.mp4"

    local_clips = []
    processed_clips = []

    try:
        # Download each clip from Cloudinary
        for i, clip_id in enumerate(clip_id_list):
            local_path = f"{CLIPS_DIR}/{clip_id}.mp4"
            url = f"https://res.cloudinary.com/{os.getenv('CLOUDINARY_CLOUD_NAME')}/video/upload/speed_clips/{clip_id}.mp4"
            response = requests.get(url)
            if response.status_code != 200:
                raise HTTPException(
                    status_code=404, detail=f"Clip {clip_id} not found on Cloudinary"
                )
            with open(local_path, "wb") as f:
                f.write(response.content)
            local_clips.append(local_path)

            # Determine overlay text
            if format == "ranking":
                label = f"Ranking #{total - i}"
            else:
                label = f"Best Moment #{i + 1}"

            # Add text overlay — white text, black box, top center
            processed_path = f"{CLIPS_DIR}/{output_id}_{i}.mp4"
            safe_label = label.replace("'", "\\'").replace(":", "\\:")
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    local_path,
                    "-vf",
                    (
                        f"drawtext=text='{safe_label}'"
                        f":fontsize=64:fontcolor=white"
                        f":x=(w-text_w)/2:y=40"
                        f":box=1:boxcolor=black@0.6:boxborderw=16"
                    ),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "23",
                    "-c:a",
                    "aac",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    "-r",
                    "30",
                    "-y",
                    processed_path,
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                raise HTTPException(
                    status_code=500, detail=f"drawtext error: {result.stderr[-500:]}"
                )

            processed_clips.append(processed_path)

        # Concatenate all processed clips
        with open(concat_list, "w") as f:
            for path in processed_clips:
                f.write(f"file '{path}'\n")

        result = subprocess.run(
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
                "-y",
                merged_path,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500, detail=f"concat error: {result.stderr[-500:]}"
            )

        # Mix voiceover — loud commentary, quiet original audio
        if voiceover:
            contents = await voiceover.read()
            with open(voiceover_path, "wb") as f:
                f.write(contents)

            result = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    merged_path,
                    "-i",
                    voiceover_path,
                    "-filter_complex",
                    "[1:a]volume=2.0[voice];[0:a]volume=0.05[orig];[voice][orig]amix=inputs=2:duration=first:weights=10 1[aout]",
                    "-map",
                    "0:v",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-y",
                    output_path,
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                raise HTTPException(
                    status_code=500, detail=f"merge error: {result.stderr[-500:]}"
                )

            os.remove(voiceover_path)
        else:
            os.rename(merged_path, output_path)

        # Stream final video back to n8n
        def iter_file():
            with open(output_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
            for p in local_clips + processed_clips + [concat_list, output_path]:
                if os.path.exists(p):
                    os.remove(p)
            if os.path.exists(merged_path):
                os.remove(merged_path)

        return StreamingResponse(
            iter_file(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename=speed_{output_id}.mp4"
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug")
def debug():
    cookies_file_exists = os.path.exists(COOKIES_FILE)
    return {
        "YT_COOKIES_B64_set": bool(os.getenv("YT_COOKIES_B64")),
        "cookies_file_exists": cookies_file_exists,
        "cookies_file_size_bytes": os.path.getsize(COOKIES_FILE)
        if cookies_file_exists
        else 0,
        "cloudinary_configured": bool(os.getenv("CLOUDINARY_CLOUD_NAME")),
    }
