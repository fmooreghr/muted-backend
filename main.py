from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import demucs.separate
import tempfile, os, base64

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://muted.cl", "http://localhost:8080"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/process")
async def process_audio(file: UploadFile = File(...)):
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, file.filename)
        with open(input_path, "wb") as f:
            f.write(await file.read())

        demucs.separate.main(["--mp3", "-o", tmpdir, input_path])

        out_dir = None
        for root, dirs, files in os.walk(tmpdir):
            if "vocals.mp3" in files:
                out_dir = root
                break

        stems = {}
        for stem in ["vocals", "bass", "drums", "other"]:
            stem_path = os.path.join(out_dir, f"{stem}.mp3")
            with open(stem_path, "rb") as f:
                stems[stem] = base64.b64encode(f.read()).decode("utf-8")

        return JSONResponse(stems)
