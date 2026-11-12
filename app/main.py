"""업로드 → 변환 작업 → 결과 내려받기 API와 화면."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import time
import uuid
import zipfile
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import fields
from pathlib import Path, PurePosixPath

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import pipeline

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CONVERTER_DATA_DIR", ROOT / "data")) / "jobs"
MAX_UPLOAD_BYTES = int(os.environ.get("CONVERTER_MAX_UPLOAD_MB", "4096")) * 1024 * 1024
RETENTION_SECONDS = int(os.environ.get("CONVERTER_RETENTION_HOURS", "24")) * 3600
WORKERS = int(os.environ.get("CONVERTER_WORKERS", "4"))
# 입장 토큰. 비어 있으면 토큰 없이 쓸 수 있다.
ACCESS_TOKEN = os.environ.get("CONVERTER_ACCESS_TOKEN", "").strip()
SESSION_COOKIE = "converter_session"
SESSION_SECONDS = 12 * 3600
_SESSION_VALUE = hashlib.sha256(f"converter-session:{ACCESS_TOKEN}".encode()).hexdigest()
_OPEN_PATHS = {"/api/health", "/api/session"}

_pool: ProcessPoolExecutor | None = None


def _purge_old_jobs() -> None:
    cutoff = time.time() - RETENTION_SECONDS
    for d in DATA_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _pool
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _purge_old_jobs()
    # 서버가 내려가며 끊긴 작업은 실패로 표시한다
    for d in DATA_DIR.iterdir():
        status = pipeline.read_status(d) if d.is_dir() else None
        if status and status.get("state") in ("queued", "running"):
            pipeline.write_status(d, state="error", message="서버 재시작으로 작업이 중단되었습니다")
    _pool = ProcessPoolExecutor(max_workers=WORKERS)
    yield
    _pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="3D 도시모델 변환", lifespan=lifespan)


def _job_dir(job_id: str) -> Path:
    try:
        uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    d = DATA_DIR / job_id
    if not d.is_dir():
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    return d


def _safe_target(base: Path, name: str) -> Path | None:
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".", "/")]
    if not parts or ".." in parts:
        return None
    return base.joinpath(*parts)


def _extract_zip(path: Path, dest: Path, budget: int) -> None:
    with zipfile.ZipFile(path) as zf:
        total = sum(i.file_size for i in zf.infolist())
        if total > budget:
            raise HTTPException(413, "압축을 푼 크기가 업로드 한도를 넘습니다")
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = _safe_target(dest, info.filename)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


_OPTION_TYPES = {f.name: f.type for f in fields(pipeline.Options)}


def _parse_options(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "options가 올바른 JSON이 아닙니다")
    unknown = set(data) - set(_OPTION_TYPES)
    if unknown:
        raise HTTPException(400, f"알 수 없는 옵션: {', '.join(sorted(unknown))}")
    try:
        opts = pipeline.Options(**data)
        if opts.src_epsg in ("", 0):
            opts.src_epsg = None
        opts.validate()
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    return {f.name: getattr(opts, f.name) for f in fields(opts)}


def _authenticated(request: Request) -> bool:
    if not ACCESS_TOKEN:
        return True
    cookie = request.cookies.get(SESSION_COOKIE, "")
    return hmac.compare_digest(cookie, _SESSION_VALUE)


@app.middleware("http")
async def require_token(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in _OPEN_PATHS and not _authenticated(request):
        return JSONResponse({"detail": "토큰이 필요합니다"}, status_code=401)
    return await call_next(request)


@app.get("/api/session")
def session_state(request: Request):
    body = {"required": bool(ACCESS_TOKEN), "authenticated": _authenticated(request)}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@app.post("/api/session")
async def session_open(request: Request, response: Response, token: str = Body(..., embed=True)):
    if ACCESS_TOKEN and not hmac.compare_digest(token.strip().encode(), ACCESS_TOKEN.encode()):
        await asyncio.sleep(1)  # 무차별 대입을 늦춘다
        raise HTTPException(401, "토큰이 올바르지 않습니다")
    if ACCESS_TOKEN:
        response.set_cookie(
            SESSION_COOKIE,
            _SESSION_VALUE,
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
    return {"authenticated": True}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/jobs")
async def create_job(files: list[UploadFile] = File(...), options: str = Form("{}")):
    opts = _parse_options(options)
    _purge_old_jobs()
    job_id = str(uuid.uuid4())
    job_dir = DATA_DIR / job_id
    in_dir = job_dir / "input"
    in_dir.mkdir(parents=True)
    try:
        received = 0
        for f in files:
            target = _safe_target(in_dir, f.filename or "")
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    received += len(chunk)
                    if received > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "업로드 한도를 넘었습니다")
                    out.write(chunk)
            if target.suffix.lower() == ".zip":
                try:
                    _extract_zip(target, in_dir, MAX_UPLOAD_BYTES)
                except zipfile.BadZipFile:
                    raise HTTPException(400, f"{f.filename}: 올바른 zip 파일이 아닙니다")
                target.unlink()
        has_model = any(
            p.suffix.lower() in pipeline.MODEL_SUFFIXES for p in in_dir.rglob("*") if p.is_file()
        )
        if not has_model:
            raise HTTPException(400, ".obj 또는 .3ds 파일이 없습니다")
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    pipeline.write_status(job_dir, id=job_id, state="queued", progress=0.0, message="대기 중", created=time.time())
    assert _pool is not None
    _pool.submit(pipeline.run, str(job_dir), opts)
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    status = pipeline.read_status(_job_dir(job_id))
    if status is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str):
    path = _job_dir(job_id) / "result.zip"
    if not path.exists():
        raise HTTPException(404, "결과가 아직 없습니다")
    return FileResponse(path, media_type="application/zip", filename=f"citygml_{job_id[:8]}.zip")


@app.get("/api/jobs/{job_id}/preview")
def job_preview(job_id: str):
    path = _job_dir(job_id) / "preview.json"
    if not path.exists():
        raise HTTPException(404, "미리보기가 없습니다")
    return FileResponse(path, media_type="application/json")


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
