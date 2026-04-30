import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

API_KEY = os.environ["API_KEY"]
TIMEOUT_SECONDS = int(os.environ.get("TIMEOUT_SECONDS", "30"))
MAX_OUTPUT_BYTES = 1_000_000  # 1 MB cap on stdout/stderr

app = FastAPI(title="PyTest Lab Executor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to your Lovable domain once deployed
    allow_methods=["*"],
    allow_headers=["*"],
)


class FileIn(BaseModel):
    path: str
    content: str


class RunRequest(BaseModel):
    files: list[FileIn]
    entry: str = Field(..., description="Relative path of the file to execute")
    pip_packages: list[str] = []


class RunResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


def _safe_join(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise HTTPException(400, f"Unsafe path: {rel}")
    return target


@app.post("/run", response_model=RunResponse)
def run(req: RunRequest, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")
    if not req.files:
        raise HTTPException(400, "No files provided")

    workdir = Path(tempfile.mkdtemp(prefix=f"pytestlab-{uuid.uuid4().hex[:8]}-"))
    try:
        # Write files
        for f in req.files:
            target = _safe_join(workdir, f.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content)

        entry_path = _safe_join(workdir, req.entry)
        if not entry_path.exists():
            raise HTTPException(400, f"Entry file not found: {req.entry}")

        # Optional pip install into a venv
        env = os.environ.copy()
        if req.pip_packages:
            subprocess.run(
                ["pip", "install", "--no-cache-dir", "--target", str(workdir / ".pkgs"), *req.pip_packages],
                check=True, capture_output=True, timeout=120,
            )
            env["PYTHONPATH"] = str(workdir / ".pkgs")

        # Run the script
        start = time.monotonic()
        try:
            proc = subprocess.run(
                ["python", "-u", str(entry_path)],
                cwd=workdir,
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                env=env,
            )
            stdout = proc.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            stderr = proc.stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as e:
            stdout = (e.stdout or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            stderr = (e.stderr or b"").decode("utf-8", errors="replace") + f"\n[Timed out after {TIMEOUT_SECONDS}s]"
            exit_code = 124

        duration_ms = int((time.monotonic() - start) * 1000)
        return RunResponse(stdout=stdout, stderr=stderr, exit_code=exit_code, duration_ms=duration_ms)

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.get("/health")
def health():
    return {"ok": True}
