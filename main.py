import logging
import sys

# Configure logging to stdout so it's visible in terminal/container logs
logging.basicConfig(
    level=logging.DEBUG,
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

@app.post("/process")
async def process_audio(file: UploadFile = File(...)):
    logger.info(f"[REQUEST] Received file: '{file.filename}', content_type: {file.content_type}")

    file_bytes = await file.read()
    filename = file.filename
    logger.info(f"[REQUEST] File read into memory: {len(file_bytes)} bytes")

    def generate():
        logger.info("[GENERATE] Generator started")

        with tempfile.TemporaryDirectory() as tmpdir:
            logger.info(f"[TEMPDIR] Created temp directory: {tmpdir}")

            input_path = os.path.join(tmpdir, filename)
            with open(input_path, "wb") as f:
                f.write(file_bytes)
            logger.info(f"[TEMPDIR] Wrote input file to: {input_path}")

            progress_value = {"pct": 0}
            demucs_error = {"msg": None}

            original_stderr = sys.stderr
            r, w = os.pipe()
            sys.stderr = os.fdopen(w, 'w')

            def run_demucs():
                logger.info(f"[DEMUCS] Starting demucs separation for: {input_path}")
                try:
                    demucs.separate.main([
                        "--mp3", "-o", tmpdir,
                        "--overlap", "0.1",
                        "--mp3-bitrate", "128",
                        input_path
                    ])
                    logger.info("[DEMUCS] demucs.separate.main() finished successfully")
                except Exception as e:
                    demucs_error["msg"] = str(e)
                    logger.error(f"[DEMUCS] Exception during separation: {e}", exc_info=True)
                finally:
                    # Close the write end of the pipe so read_progress can exit
                    try:
                        sys.stderr.close()
                    except Exception:
                        pass

            def read_progress():
                logger.info("[PROGRESS] Progress reader thread started")
                try:
                    with os.fdopen(r, 'r') as pipe:
                        for line in pipe:
                            line = line.strip()
                            if line:
                                logger.debug(f"[DEMUCS STDERR] {line}")
                            if '%' in line:
                                try:
                                    pct = float(line.split('%')[0].split()[-1])
                                    progress_value["pct"] = pct
                                    logger.info(f"[PROGRESS] {pct:.1f}%")
                                except Exception as parse_err:
                                    logger.warning(f"[PROGRESS] Could not parse progress from line: '{line}' ({parse_err})")
                except Exception as e:
                    logger.error(f"[PROGRESS] Error reading progress pipe: {e}", exc_info=True)
                logger.info("[PROGRESS] Progress reader thread finished")

            t_demucs = threading.Thread(target=run_demucs, name="demucs-thread")
            t_progress = threading.Thread(target=read_progress, name="progress-thread")
            t_progress.start()
            t_demucs.start()
            logger.info("[GENERATE] Demucs and progress threads started, entering polling loop")

            while t_demucs.is_alive():
                msg = json.dumps({'type': 'progress', 'value': progress_value['pct']})
                logger.debug(f"[SSE] Sending progress event: {msg}")
                yield f"data: {msg}\n\n"
                time.sleep(1)

            t_demucs.join()
            t_progress.join()
            sys.stderr = original_stderr
            logger.info("[GENERATE] Both threads finished")

            if demucs_error["msg"]:
                logger.error(f"[GENERATE] Demucs failed, aborting. Error: {demucs_error['msg']}")
                yield f"data: {json.dumps({'type': 'error', 'message': demucs_error['msg']})}\n\n"
                return

            # Find the output directory with stems
            logger.info(f"[OUTPUT] Walking tmpdir to find stem files: {tmpdir}")
            out_dir = None
            for root, dirs, files in os.walk(tmpdir):
                logger.debug(f"[OUTPUT] Checking dir: {root}, files: {files}")
                if "vocals.mp3" in files:
                    out_dir = root
                    logger.info(f"[OUTPUT] Found stems in: {out_dir}")
                    break

            if out_dir is None:
                logger.error("[OUTPUT] Could not find output directory with stem files!")
                yield f"data: {json.dumps({'type': 'error', 'message': 'Stem files not found after processing'})}\n\n"
                return

            stems = {}
            for stem in ["vocals", "bass", "drums", "other"]:
                stem_path = os.path.join(out_dir, f"{stem}.mp3")
                if not os.path.exists(stem_path):
                    logger.error(f"[OUTPUT] Missing stem file: {stem_path}")
                    yield f"data: {json.dumps({'type': 'error', 'message': f'Missing stem: {stem}'})}\n\n"
                    return
                size = os.path.getsize(stem_path)
                logger.info(f"[OUTPUT] Reading stem '{stem}': {size} bytes")
                with open(stem_path, "rb") as f:
                    stems[stem] = base64.b64encode(f.read()).decode("utf-8")
                logger.info(f"[OUTPUT] Stem '{stem}' encoded to base64 ({len(stems[stem])} chars)")

            logger.info("[SSE] Sending 'done' event with all stems")
            yield f"data: {json.dumps({'type': 'done', 'stems': stems})}\n\n"
            logger.info("[GENERATE] Generator complete")

    logger.info("[REQUEST] Returning StreamingResponse")
    return StreamingResponse(generate(), media_type="text/event-stream")