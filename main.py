from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import demucs.separate
import tempfile, os, base64, json, sys
from io import StringIO

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://muted.cl", "http://localhost:8080"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/process")
async def process_audio(file: UploadFile = File(...)):
    file_bytes = await file.read()
    filename = file.filename

    def generate():
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            with open(input_path, "wb") as f:
                f.write(file_bytes)

            # Capture demucs stderr for progress
            import threading

            progress_value = {"pct": 0}

            original_stderr = sys.stderr
            r, w = os.pipe()
            sys.stderr = os.fdopen(w, 'w')

            def run_demucs():
                demucs.separate.main(["--mp3", "-o", tmpdir, "--overlap", "0.1", "--mp3-bitrate", "128", input_path])

            def read_progress():
                with os.fdopen(r, 'r') as pipe:
                    for line in pipe:
                        if '%' in line:
                            try:
                                pct = float(line.strip().split('%')[0].split()[-1])
                                progress_value["pct"] = pct
                            except:
                                pass

            t_demucs = threading.Thread(target=run_demucs)
            t_progress = threading.Thread(target=read_progress)
            t_progress.start()
            t_demucs.start()

            while t_demucs.is_alive():
                yield f"data: {json.dumps({'type': 'progress', 'value': progress_value['pct']})}\n\n"
                import time; time.sleep(1)

            t_demucs.join()
            sys.stderr = original_stderr

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

            yield f"data: {json.dumps({'type': 'done', 'stems': stems})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")