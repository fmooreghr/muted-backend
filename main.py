import logging
import sys
import io

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import demucs.separate
import tempfile, os, base64, json, threading, time

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://muted.cl", "http://localhost:8080"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ThreadSafeStderr(io.TextIOBase):
    """A thread-safe stderr replacement that captures output for progress parsing
    without closing the underlying stream prematurely."""

    def __init__(self, progress_value):
        self._lock = threading.Lock()
        self._progress_value = progress_value
        self._closed_flag = False

    def write(self, s):
        if self._closed_flag:
            return len(s)
        with self._lock:
            if '%' in s:
                try:
                    pct = float(s.split('%')[0].split()[-1])
                    self._progress_value["pct"] = pct
                except Exception:
                    pass
        return len(s)

    def flush(self):
        pass

    def close(self):
        self._closed_flag = True

    @property
    def closed(self):
        return False

    def isatty(self):
        return False

    def writable(self):
        return True


@app.post("/process")
async def process_audio(file: UploadFile = File(...)):
    logger.info(f"[REQUEST] Received '{file.filename}', size: {file.size}")

    file_bytes = await file.read()
    filename = file.filename

    def generate():
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            with open(input_path, "wb") as f:
                f.write(file_bytes)

            progress_value = {"pct": 0}
            demucs_error = {"msg": None}

            original_stderr = sys.stderr
            sys.stderr = ThreadSafeStderr(progress_value)

            def run_demucs():
                try:
                    demucs.separate.main([
                        "--mp3", "-o", tmpdir,
                        "--overlap", "0.1",
                        "--segment", "7",
                        "--mp3-bitrate", "128",
                        input_path
                    ])
                    logger.info("[DEMUCS] Separation finished successfully")
                except Exception as e:
                    demucs_error["msg"] = str(e)
                    logger.error(f"[DEMUCS] Failed: {e}")

            t_demucs = threading.Thread(target=run_demucs, name="demucs-thread")
            t_demucs.start()

            while t_demucs.is_alive():
                yield f"data: {json.dumps({'type': 'progress', 'value': progress_value['pct']})}\n\n"
                time.sleep(1)

            t_demucs.join()
            sys.stderr = original_stderr

            if demucs_error["msg"]:
                logger.error(f"[DEMUCS] Error: {demucs_error['msg']}")
                yield f"data: {json.dumps({'type': 'error', 'message': demucs_error['msg']})}\n\n"
                return

            # Find output stems
            out_dir = None
            for root, dirs, files in os.walk(tmpdir):
                if "vocals.mp3" in files:
                    out_dir = root
                    break

            if out_dir is None:
                logger.error("[OUTPUT] Stem files not found")
                yield f"data: {json.dumps({'type': 'error', 'message': 'Stem files not found after processing'})}\n\n"
                return

            stems = {}
            for stem in ["vocals", "bass", "drums", "other"]:
                stem_path = os.path.join(out_dir, f"{stem}.mp3")
                if not os.path.exists(stem_path):
                    logger.error(f"[OUTPUT] Missing stem: {stem}")
                    yield f"data: {json.dumps({'type': 'error', 'message': f'Missing stem: {stem}'})}\n\n"
                    return
                with open(stem_path, "rb") as f:
                    stems[stem] = base64.b64encode(f.read()).decode("utf-8")

            logger.info("[DONE] Sending stems to client")
            yield f"data: {json.dumps({'type': 'done', 'stems': stems})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")