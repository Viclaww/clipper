import asyncio
import base64
import os
import subprocess
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()
CLIPS_DIR = "/tmp/clips"
COOKIES_FILE = "/tmp/yt_cookies.txt"
os.makedirs(CLIPS_DIR, exist_ok=True)


# Write cookies from env variable at startup
@app.on_event("startup")
def write_cookies():
    yt_cookies = os.getenv("YT_COOKIES_B64")
    if yt_cookies:
        with open(COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(yt_cookies))
        print("Cookies file written")
    else:
        print("WARNING: No YT_COOKIES_B64 set — downloads may fail bot check")


# Semaphore: only 1 download at a time to avoid 429s
download_semaphore = asyncio.Semaphore(1)


class DownloadRequest(BaseModel):
    url: str
    start: str = "00:00:00"
    end: str = "00:01:00"
    title: str = ""


class CompileRequest(BaseModel):
    clip_ids: list[str]
    format: str = "best_moments"
    voiceover: str = ""


def build_ytdlp_cmd(url: str, output: str) -> list:
    cmd = [
        "yt-dlp",
        "-f",
        "bv[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
        "--merge-output-format",
        "mp4",
        "--extractor-args",
        "youtube:player_client=android",
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

        # Small delay to be polite to YouTube
        await asyncio.sleep(2)

        try:
            # Download
            result = subprocess.run(
                build_ytdlp_cmd(req.url, raw_path), capture_output=True, text=True
            )
            if result.returncode != 0:
                raise HTTPException(
                    status_code=500, detail=f"yt-dlp error: {result.stderr[-500:]}"
                )

            # Trim
            result = subprocess.run(
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
            return {"clip_id": clip_id, "title": req.title, "path": out_path}

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/compile")
def compile(req: CompileRequest):
    output_id = str(uuid.uuid4())
    output_path = f"{CLIPS_DIR}/{output_id}_final.mp4"
    concat_list = f"{CLIPS_DIR}/{output_id}_concat.txt"
    voiceover_path = f"{CLIPS_DIR}/{output_id}_voice.mp3"

    # Validate all clips exist
    for clip_id in req.clip_ids:
        clip_path = f"{CLIPS_DIR}/{clip_id}.mp4"
        if not os.path.exists(clip_path):
            raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")

    # Write concat list
    with open(concat_list, "w") as f:
        for clip_id in req.clip_ids:
            f.write(f"file '{CLIPS_DIR}/{clip_id}.mp4'\n")

    # Concatenate clips
    merged_path = f"{CLIPS_DIR}/{output_id}_merged.mp4"
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

    # Mix voiceover if provided
    if req.voiceover:
        with open(voiceover_path, "wb") as f:
            f.write(base64.b64decode(req.voiceover))

        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                merged_path,
                "-i",
                voiceover_path,
                "-filter_complex",
                "[0:a]volume=0.15[orig];[1:a]volume=1.0[voice];[orig][voice]amix=inputs=2:duration=longest[aout]",
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

    # Cleanup
    os.remove(concat_list)
    if os.path.exists(merged_path):
        os.remove(merged_path)

    return {"output_id": output_id, "path": output_path}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug")
def debug():
    cookies_env = os.getenv("YT_COOKIES_B64")
    cookies_file_exists = os.path.exists(COOKIES_FILE)
    cookies_file_size = os.path.getsize(COOKIES_FILE) if cookies_file_exists else 0
    return {
        "YT_COOKIES_B64_set": bool(cookies_env),
        "cookies_file_exists": cookies_file_exists,
        "cookies_file_size_bytes": cookies_file_size,
        "deno_available": subprocess.run(
            ["which", "deno"], capture_output=True
        ).returncode
        == 0,
    }
