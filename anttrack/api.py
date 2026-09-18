import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .camera import CameraStream
from .config import API_KEY, load_camera_config, load_config
from .satellites import CatalogError
from .tracker import Tracker, TrackerError

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

STATUS_PUSH_INTERVAL_S = 1.0
CAMERA_FRAGMENT_WAIT_S = 10.0
CAMERA_INIT_WAIT_S = 10.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    qth_cfg, rotator_cfg = load_config()
    app.state.tracker = Tracker(qth_cfg, rotator_cfg)
    app.state.qth_cfg = qth_cfg
    app.state.rotator_cfg = rotator_cfg

    camera_cfg = load_camera_config()
    app.state.camera = CameraStream(camera_cfg["rtsp_url"] if camera_cfg else None)
    app.state.camera.start()

    yield

    app.state.camera.stop()
    app.state.tracker.close()


app = FastAPI(title="anttrack", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_tracker(request: Request) -> Tracker:
    return request.app.state.tracker


def get_camera(request: Request) -> CameraStream:
    return request.app.state.camera


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if API_KEY and request.url.path.startswith("/api/"):
        # <img>/<video> elements (camera snapshot/stream) can't set custom
        # headers, so also accept the key as a query param.
        supplied = request.headers.get("x-api-key") or request.query_params.get("key")
        if supplied != API_KEY:
            return _json_error(401, "missing or invalid X-API-Key")
    return await call_next(request)


class TrackRequest(BaseModel):
    name: str = Field(
        ..., min_length=1,
        description="Satellite or body name, e.g. 'ISS (ZARYA)' or 'MOON'",
    )


class LocRequest(BaseModel):
    locator: str = Field(
        ..., min_length=4, max_length=8,
        description="Maidenhead locator, e.g. 'JO70AA'",
    )


class RawRequest(BaseModel):
    az: float = Field(..., description="Azimuth in degrees")
    el: float = Field(..., description="Elevation in degrees")


@app.exception_handler(TrackerError)
async def tracker_error_handler(request: Request, exc: TrackerError):
    return _json_error(400, str(exc))


@app.exception_handler(CatalogError)
async def catalog_error_handler(request: Request, exc: CatalogError):
    return _json_error(502, str(exc))


def _json_error(status_code, detail):
    return JSONResponse(status_code=status_code, content={"detail": detail})


@app.get("/api/status")
async def api_status(request: Request):
    return get_tracker(request).status()


@app.get("/api/config")
async def api_config(request: Request):
    return {
        "qth": request.app.state.qth_cfg,
        "rotator": {k: v for k, v in request.app.state.rotator_cfg.items()},
    }


@app.get("/api/catalog")
async def api_catalog(request: Request, query: str = None):
    return get_tracker(request).catalog(query)


@app.post("/api/catalog/refresh")
async def api_catalog_refresh(request: Request):
    return get_tracker(request).refresh_catalog()


@app.get("/api/log")
async def api_log(request: Request, since: int = 0, limit: int = 200):
    lines = get_tracker(request).get_log(since_seq=since, limit=limit)
    return {"lines": [{"seq": seq, "line": line} for seq, line in lines]}


@app.post("/api/track")
async def api_track(body: TrackRequest, request: Request):
    tracker = get_tracker(request)
    tracker.start_target(body.name)
    return tracker.status()


@app.post("/api/track/loc")
async def api_track_loc(body: LocRequest, request: Request):
    tracker = get_tracker(request)
    tracker.start_loc(body.locator)
    return tracker.status()


@app.post("/api/track/raw")
async def api_track_raw(body: RawRequest, request: Request):
    tracker = get_tracker(request)
    tracker.start_raw(body.az, body.el)
    return tracker.status()


@app.post("/api/stop")
async def api_stop(request: Request):
    tracker = get_tracker(request)
    tracker.stop()
    return tracker.status()


@app.get("/api/camera/status")
async def api_camera_status(request: Request):
    return get_camera(request).status()


@app.get("/api/camera/snapshot.jpg")
async def api_camera_snapshot(request: Request):
    camera = get_camera(request)
    if not camera.enabled:
        return _json_error(404, "no camera configured")
    frame = await asyncio.to_thread(camera.capture_snapshot)
    if frame is None:
        return _json_error(503, "could not capture a snapshot")
    return Response(content=frame, media_type="image/jpeg")


@app.get("/api/camera/stream.mp4")
async def api_camera_stream(request: Request):
    camera = get_camera(request)
    if not camera.enabled:
        return _json_error(404, "no camera configured")

    init_segment = await asyncio.to_thread(camera.get_init_segment, CAMERA_INIT_WAIT_S)
    if init_segment is None:
        return _json_error(503, "camera stream not ready yet")

    async def fragments():
        yield init_segment
        seq = 0
        while True:
            if await request.is_disconnected():
                break
            fragment, seq = await asyncio.to_thread(
                camera.get_fragment, seq, CAMERA_FRAGMENT_WAIT_S)
            if fragment is None:
                continue  # nothing new within the wait window; recheck disconnect and retry
            yield fragment

    return StreamingResponse(fragments(), media_type="video/mp4")


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    # Browsers can't set custom headers on a WebSocket handshake, so accept
    # the key as a query param here too.
    supplied_key = websocket.headers.get("x-api-key") or websocket.query_params.get("key")
    if API_KEY and supplied_key != API_KEY:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    tracker: Tracker = websocket.app.state.tracker
    try:
        while True:
            await websocket.send_json(tracker.status())
            await asyncio.sleep(STATUS_PUSH_INTERVAL_S)
    except WebSocketDisconnect:
        pass


# Serve the single-page web app. Mounted last so it doesn't shadow /api routes.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
