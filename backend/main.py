"""
Virtual Painting Backend - Full Feature Set
--------------------------------------------
FastAPI + OpenCV + MediaPipe Hands.

Features:
- Freehand draw / eraser, plus shape tools (line, rectangle, circle).
- Two gesture modes: "finger_count" (index-only = draw, index+middle = hover)
  and "pinch" (thumb-index pinch distance below a threshold = draw).
- Optional two-hand mode: second hand's pinch distance controls brush size live.
- Undo / redo via canvas snapshots.
- Save / load paintings to/from disk, plus a gallery listing.
- Autosave on a timer.
- Session recording to an MP4 file.
- Camera enumeration + hot-switching.
- Live FPS + current mode/tool burned into the video feed.

Run with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import glob
import math
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import cv2
import mediapipe as mp
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------

app = FastAPI(title="Virtual Painting Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(__file__)
SAVE_DIR = os.path.join(BASE_DIR, "saved_paintings")
RECORD_DIR = os.path.join(BASE_DIR, "recordings")
os.makedirs(SAVE_DIR, exist_ok=True)
os.makedirs(RECORD_DIR, exist_ok=True)

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720

MAX_UNDO = 25
PINCH_THRESHOLD = 0.045  # normalized landmark-distance threshold for "pinch"

# --------------------------------------------------------------------------
# Shared mutable state (single-user local app -> globals guarded by a lock)
# --------------------------------------------------------------------------

class PaintState:
    def __init__(self):
        self.lock = threading.Lock()

        # Drawing config
        self.color = (0, 0, 255)          # BGR - default red
        self.brush_size = 8
        self.eraser_size = 40
        self.tool = "draw"                # draw | eraser | line | rectangle | circle
        self.gesture_mode = "finger_count"  # finger_count | pinch
        self.two_hand_mode = False

        # Canvas + stroke tracking
        self.canvas: Optional[np.ndarray] = None
        self.prev_point: Optional[tuple] = None
        self.shape_start: Optional[tuple] = None
        self.was_drawing = False

        # Undo / redo
        self.undo_stack = deque(maxlen=MAX_UNDO)
        self.redo_stack = deque(maxlen=MAX_UNDO)

        # Streaming
        self.latest_jpeg: Optional[bytes] = None
        self.running = True
        self.fps = 0.0

        # Camera
        self.camera_index = 0
        self.requested_camera_index = 0

        # Autosave
        self.autosave_enabled = False
        self.autosave_interval = 30  # seconds

        # Recording
        self.is_recording = False
        self.record_writer = None
        self.record_filename = None

    # --- setters -----------------------------------------------------
    def set_color(self, r, g, b):
        with self.lock:
            self.color = (b, g, r)

    def set_brush_size(self, size):
        with self.lock:
            self.brush_size = max(1, min(int(size), 100))

    def set_tool(self, tool):
        with self.lock:
            if tool in ("draw", "eraser", "line", "rectangle", "circle"):
                self.tool = tool
                self.shape_start = None

    def set_gesture_mode(self, mode):
        with self.lock:
            if mode in ("finger_count", "pinch"):
                self.gesture_mode = mode

    def set_two_hand_mode(self, enabled):
        with self.lock:
            self.two_hand_mode = bool(enabled)

    def set_camera(self, index):
        with self.lock:
            self.requested_camera_index = int(index)

    def set_autosave(self, enabled, interval=None):
        with self.lock:
            self.autosave_enabled = bool(enabled)
            if interval:
                self.autosave_interval = max(5, int(interval))

    # --- canvas history ------------------------------------------------
    def push_undo_snapshot(self):
        """Call BEFORE a mutation, so undo restores the pre-mutation state."""
        with self.lock:
            if self.canvas is not None:
                self.undo_stack.append(self.canvas.copy())
                self.redo_stack.clear()

    def undo(self):
        with self.lock:
            if not self.undo_stack or self.canvas is None:
                return False
            self.redo_stack.append(self.canvas.copy())
            self.canvas = self.undo_stack.pop()
            return True

    def redo(self):
        with self.lock:
            if not self.redo_stack or self.canvas is None:
                return False
            self.undo_stack.append(self.canvas.copy())
            self.canvas = self.redo_stack.pop()
            return True

    def clear_canvas(self):
        self.push_undo_snapshot()
        with self.lock:
            if self.canvas is not None:
                self.canvas[:] = 0


state = PaintState()

# --------------------------------------------------------------------------
# MediaPipe setup
# --------------------------------------------------------------------------

mp_hands = mp.solutions.hands
mp_draw = mp.solutions.drawing_utils

hands = mp_hands.Hands(
    static_image_mode=False,
    max_num_hands=1,
    model_complexity=0,
    min_detection_confidence=0.7,
    min_tracking_confidence=0.6,
)

TIP_IDS = [4, 8, 12, 16, 20]  # thumb, index, middle, ring, pinky


def fingers_up(landmarks, handedness_label: str) -> list:
    fingers = []
    if handedness_label == "Right":
        fingers.append(landmarks[TIP_IDS[0]].x < landmarks[TIP_IDS[0] - 1].x)
    else:
        fingers.append(landmarks[TIP_IDS[0]].x > landmarks[TIP_IDS[0] - 1].x)
    for tip_id in TIP_IDS[1:]:
        fingers.append(landmarks[tip_id].y < landmarks[tip_id - 2].y)
    return fingers


def pinch_distance(landmarks) -> float:
    """Normalized distance between thumb tip (4) and index tip (8)."""
    a, b = landmarks[4], landmarks[8]
    return math.hypot(a.x - b.x, a.y - b.y)


def list_available_cameras(max_check=5):
    available = []
    for i in range(max_check):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            available.append(i)
        cap.release()
    return available


# --------------------------------------------------------------------------
# Camera + processing thread
# --------------------------------------------------------------------------

def open_camera(index):
    cap = cv2.VideoCapture(index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    return cap


def camera_loop():
    cap = open_camera(state.camera_index)
    if not cap.isOpened():
        print("ERROR: could not open webcam at index", state.camera_index)

    last_autosave = time.time()
    last_frame_time = time.time()

    while state.running:
        # Hot camera switch
        if state.requested_camera_index != state.camera_index:
            cap.release()
            state.camera_index = state.requested_camera_index
            cap = open_camera(state.camera_index)

        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue

        now = time.time()
        dt = now - last_frame_time
        last_frame_time = now
        if dt > 0:
            state.fps = 0.9 * state.fps + 0.1 * (1.0 / dt)  # smoothed

        frame = cv2.flip(frame, 1)
        h, w, _ = frame.shape

        with state.lock:
            if state.canvas is None:
                state.canvas = np.zeros((h, w, 3), dtype=np.uint8)

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = hands.process(rgb)

        cursor_point = None
        is_drawing = False
        mode_label = "NO HAND"

        primary_landmarks = None
        primary_label = None

        if results.multi_hand_landmarks and results.multi_handedness:
            hand_infos = list(zip(results.multi_hand_landmarks, results.multi_handedness))

            if state.two_hand_mode and len(hand_infos) == 2:
                sorted_hands = sorted(hand_infos, key=lambda hi: hi[1].classification[0].label)
                control_hand, draw_hand = sorted_hands[0], sorted_hands[1]
                primary_landmarks = draw_hand[0].landmark
                primary_label = draw_hand[1].classification[0].label

                dist = pinch_distance(control_hand[0].landmark)
                mapped = int(np.interp(dist, [0.02, 0.25], [2, 60]))
                with state.lock:
                    state.brush_size = max(1, min(mapped, 100))

                mp_draw.draw_landmarks(frame, control_hand[0], mp_hands.HAND_CONNECTIONS)
                mp_draw.draw_landmarks(frame, draw_hand[0], mp_hands.HAND_CONNECTIONS)
            else:
                primary_landmarks = hand_infos[0][0].landmark
                primary_label = hand_infos[0][1].classification[0].label
                mp_draw.draw_landmarks(frame, hand_infos[0][0], mp_hands.HAND_CONNECTIONS)

            index_tip = primary_landmarks[8]
            cursor_point = (int(index_tip.x * w), int(index_tip.y * h))

            if state.gesture_mode == "pinch":
                dist = pinch_distance(primary_landmarks)
                is_drawing = dist < PINCH_THRESHOLD
                mode_label = "DRAWING" if is_drawing else "HOVER"
            else:
                up = fingers_up(primary_landmarks, primary_label)
                index_up, middle_up = up[1], up[2]
                if index_up and not middle_up:
                    is_drawing = True
                    mode_label = "DRAWING"
                elif index_up and middle_up:
                    is_drawing = False
                    mode_label = "HOVER"
                else:
                    is_drawing = False
                    mode_label = "IDLE"

        with state.lock:
            stroke_just_ended = state.was_drawing and not is_drawing
            stroke_just_started = is_drawing and not state.was_drawing
            state.was_drawing = is_drawing

            is_shape_tool = state.tool in ("line", "rectangle", "circle")

            if stroke_just_started:
                if state.canvas is not None:
                    state.undo_stack.append(state.canvas.copy())
                    state.redo_stack.clear()
                if is_shape_tool:
                    state.shape_start = cursor_point

            if cursor_point is not None and is_drawing:
                if not is_shape_tool:
                    if state.prev_point is None:
                        state.prev_point = cursor_point
                    draw_color = state.color if state.tool == "draw" else (0, 0, 0)
                    size = state.brush_size if state.tool == "draw" else state.eraser_size
                    cv2.line(
                        state.canvas, state.prev_point, cursor_point,
                        draw_color, size, lineType=cv2.LINE_AA,
                    )
                    state.prev_point = cursor_point
            else:
                state.prev_point = None
                if stroke_just_ended and is_shape_tool and state.shape_start and cursor_point:
                    _draw_shape(state.canvas, state.tool, state.shape_start, cursor_point,
                                state.color, state.brush_size)
                if stroke_just_ended:
                    state.shape_start = None

            canvas = state.canvas
            gray = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
            _, mask = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)
            mask_inv = cv2.bitwise_not(mask)
            bg = cv2.bitwise_and(frame, frame, mask=mask_inv)
            fg = cv2.bitwise_and(canvas, canvas, mask=mask)
            combined = cv2.add(bg, fg)

            if is_shape_tool and is_drawing and state.shape_start and cursor_point:
                _draw_shape(combined, state.tool, state.shape_start, cursor_point,
                            state.color, state.brush_size)

            if cursor_point is not None:
                indicator_color = state.color if state.tool in ("draw", "line", "rectangle", "circle") else (255, 255, 255)
                cv2.circle(combined, cursor_point, 8, indicator_color, 2)

            hud = f"{mode_label}  |  tool: {state.tool}  |  {state.fps:.0f} fps"
            cv2.putText(combined, hud, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (255, 255, 255), 2, cv2.LINE_AA)
            if state.is_recording:
                cv2.circle(combined, (w - 30, 25), 8, (0, 0, 255), -1)
                cv2.putText(combined, "REC", (w - 75, 33), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, (0, 0, 255), 2, cv2.LINE_AA)

            ok2, buf = cv2.imencode(".jpg", combined, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok2:
                state.latest_jpeg = buf.tobytes()

            if state.is_recording and state.record_writer is not None:
                state.record_writer.write(combined)

        if state.autosave_enabled and (time.time() - last_autosave) >= state.autosave_interval:
            _autosave()
            last_autosave = time.time()

    cap.release()


def _draw_shape(target, tool, p1, p2, color, thickness):
    if tool == "line":
        cv2.line(target, p1, p2, color, thickness, lineType=cv2.LINE_AA)
    elif tool == "rectangle":
        cv2.rectangle(target, p1, p2, color, thickness, lineType=cv2.LINE_AA)
    elif tool == "circle":
        radius = int(math.hypot(p2[0] - p1[0], p2[1] - p1[1]))
        cv2.circle(target, p1, radius, color, thickness, lineType=cv2.LINE_AA)


def _autosave():
    with state.lock:
        if state.canvas is None:
            return
        canvas_copy = state.canvas.copy()
    filepath = os.path.join(SAVE_DIR, "autosave.png")
    cv2.imwrite(filepath, canvas_copy)


def process_browser_frame(frame):
    """Process a webcam frame received from a browser client."""
    height, width, _ = frame.shape
    with state.lock:
        if state.canvas is None or state.canvas.shape[:2] != (height, width):
            state.canvas = np.zeros((height, width, 3), dtype=np.uint8)
        canvas = state.canvas

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = hands.process(rgb)
    cursor_point = None
    is_drawing = False
    mode_label = "NO HAND"

    if results.multi_hand_landmarks and results.multi_handedness:
        landmarks = results.multi_hand_landmarks[0]
        handedness = results.multi_handedness[0].classification[0].label
        mp_draw.draw_landmarks(frame, landmarks, mp_hands.HAND_CONNECTIONS)
        index_tip = landmarks.landmark[8]
        cursor_point = (int(index_tip.x * width), int(index_tip.y * height))
        if state.gesture_mode == "pinch":
            is_drawing = pinch_distance(landmarks.landmark) < PINCH_THRESHOLD
            mode_label = "DRAWING" if is_drawing else "HOVER"
        else:
            up = fingers_up(landmarks.landmark, handedness)
            is_drawing = up[1] and not up[2]
            mode_label = "DRAWING" if is_drawing else ("HOVER" if up[1] else "IDLE")

    with state.lock:
        stroke_started = is_drawing and not state.was_drawing
        state.was_drawing = is_drawing
        if stroke_started and state.canvas is not None:
            state.undo_stack.append(state.canvas.copy())
            state.redo_stack.clear()
        if cursor_point is not None and is_drawing and state.canvas is not None:
            if state.prev_point is None:
                state.prev_point = cursor_point
            draw_color = state.color if state.tool == "draw" else (0, 0, 0)
            size = state.brush_size if state.tool == "draw" else state.eraser_size
            cv2.line(state.canvas, state.prev_point, cursor_point, draw_color, size, lineType=cv2.LINE_AA)
            state.prev_point = cursor_point
        else:
            state.prev_point = None

        canvas = state.canvas
        gray = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)
        bg = cv2.bitwise_and(frame, frame, mask=cv2.bitwise_not(mask))
        fg = cv2.bitwise_and(canvas, canvas, mask=mask)
        combined = cv2.add(bg, fg)

        if cursor_point is not None:
            indicator_color = state.color if state.tool == "draw" else (255, 255, 255)
            cv2.circle(combined, cursor_point, 8, indicator_color, 2)

    cv2.putText(combined, f"{mode_label}  |  tool: {state.tool}", (12, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    ok, buffer = cv2.imencode(".jpg", combined, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buffer.tobytes() if ok else None


camera_thread = threading.Thread(target=camera_loop, daemon=True)
camera_thread.start()

# --------------------------------------------------------------------------
# REST models
# --------------------------------------------------------------------------

class ColorPayload(BaseModel):
    r: int
    g: int
    b: int


class BrushSizePayload(BaseModel):
    size: int


class ToolPayload(BaseModel):
    tool: str


class GestureModePayload(BaseModel):
    mode: str


class TwoHandPayload(BaseModel):
    enabled: bool


class CameraPayload(BaseModel):
    index: int


class AutosavePayload(BaseModel):
    enabled: bool
    interval: Optional[int] = None


# --------------------------------------------------------------------------
# REST endpoints - drawing config
# --------------------------------------------------------------------------

@app.post("/api/color")
def set_color(payload: ColorPayload):
    state.set_color(payload.r, payload.g, payload.b)
    return {"ok": True}


@app.post("/api/brush_size")
def set_brush_size(payload: BrushSizePayload):
    state.set_brush_size(payload.size)
    return {"ok": True, "size": state.brush_size}


@app.post("/api/tool")
def set_tool(payload: ToolPayload):
    state.set_tool(payload.tool)
    return {"ok": True, "tool": state.tool}


@app.post("/api/gesture_mode")
def set_gesture_mode(payload: GestureModePayload):
    state.set_gesture_mode(payload.mode)
    return {"ok": True, "mode": state.gesture_mode}


@app.post("/api/two_hand_mode")
def set_two_hand_mode(payload: TwoHandPayload):
    state.set_two_hand_mode(payload.enabled)
    return {"ok": True, "enabled": state.two_hand_mode}


@app.post("/api/clear")
def clear_canvas():
    state.clear_canvas()
    return {"ok": True}


@app.post("/api/undo")
def undo():
    ok = state.undo()
    return {"ok": ok}


@app.post("/api/redo")
def redo():
    ok = state.redo()
    return {"ok": ok}


# --------------------------------------------------------------------------
# REST endpoints - save / load / gallery
# --------------------------------------------------------------------------

@app.post("/api/save")
def save_painting():
    with state.lock:
        if state.canvas is None:
            return JSONResponse({"ok": False, "error": "Canvas not ready yet"}, status_code=400)
        canvas_copy = state.canvas.copy()

    filename = f"painting_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    filepath = os.path.join(SAVE_DIR, filename)
    cv2.imwrite(filepath, canvas_copy)
    return {"ok": True, "filename": filename}


@app.get("/api/gallery")
def gallery():
    files = sorted(glob.glob(os.path.join(SAVE_DIR, "*.png")), key=os.path.getmtime, reverse=True)
    return {"paintings": [os.path.basename(f) for f in files]}


@app.post("/api/load/{filename}")
def load_painting(filename: str):
    filepath = os.path.join(SAVE_DIR, filename)
    if not os.path.exists(filepath):
        return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)

    img = cv2.imread(filepath)
    if img is None:
        return JSONResponse({"ok": False, "error": "Could not read image"}, status_code=400)

    state.push_undo_snapshot()
    with state.lock:
        if state.canvas is not None:
            img_resized = cv2.resize(img, (state.canvas.shape[1], state.canvas.shape[0]))
            state.canvas = img_resized
        else:
            state.canvas = img
    return {"ok": True}


@app.get("/api/download/{filename}")
def download_painting(filename: str):
    filepath = os.path.join(SAVE_DIR, filename)
    if not os.path.exists(filepath):
        return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)
    return FileResponse(filepath, media_type="image/png", filename=filename)


@app.post("/api/autosave")
def set_autosave(payload: AutosavePayload):
    state.set_autosave(payload.enabled, payload.interval)
    return {"ok": True, "enabled": state.autosave_enabled, "interval": state.autosave_interval}


# --------------------------------------------------------------------------
# REST endpoints - camera
# --------------------------------------------------------------------------

@app.get("/api/cameras")
def cameras():
    return {"available": list_available_cameras(), "current": state.camera_index}


@app.post("/api/camera")
def set_camera(payload: CameraPayload):
    state.set_camera(payload.index)
    return {"ok": True, "index": payload.index}


# --------------------------------------------------------------------------
# REST endpoints - recording (export as video)
# --------------------------------------------------------------------------

@app.post("/api/record/start")
def start_recording():
    with state.lock:
        if state.is_recording:
            return {"ok": False, "error": "Already recording"}
        if state.canvas is None:
            return JSONResponse({"ok": False, "error": "Canvas not ready yet"}, status_code=400)
        h, w = state.canvas.shape[:2]
        filename = f"recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        filepath = os.path.join(RECORD_DIR, filename)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        state.record_writer = cv2.VideoWriter(filepath, fourcc, 20.0, (w, h))
        state.record_filename = filename
        state.is_recording = True
    return {"ok": True, "filename": filename}


@app.post("/api/record/stop")
def stop_recording():
    with state.lock:
        if not state.is_recording:
            return {"ok": False, "error": "Not recording"}
        state.is_recording = False
        if state.record_writer is not None:
            state.record_writer.release()
            state.record_writer = None
        filename = state.record_filename
        state.record_filename = None
    return {"ok": True, "filename": filename}


@app.get("/api/recording/download/{filename}")
def download_recording(filename: str):
    filepath = os.path.join(RECORD_DIR, filename)
    if not os.path.exists(filepath):
        return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)
    return FileResponse(filepath, media_type="video/mp4", filename=filename)


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

@app.get("/api/status")
def status():
    with state.lock:
        return {
            "tool": state.tool,
            "brush_size": state.brush_size,
            "color": state.color,
            "gesture_mode": state.gesture_mode,
            "two_hand_mode": state.two_hand_mode,
            "fps": round(state.fps, 1),
            "is_recording": state.is_recording,
            "camera_index": state.camera_index,
            "autosave_enabled": state.autosave_enabled,
            "autosave_interval": state.autosave_interval,
            "can_undo": len(state.undo_stack) > 0,
            "can_redo": len(state.redo_stack) > 0,
        }


# --------------------------------------------------------------------------
# WebSocket video stream
# --------------------------------------------------------------------------

@app.websocket("/ws/stream")
async def stream(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            if state.latest_jpeg is not None:
                await websocket.send_bytes(state.latest_jpeg)
            await asyncio.sleep(0.033)  # ~30 fps
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/camera")
async def browser_camera(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            frame_data = await websocket.receive_bytes()
            frame_array = np.frombuffer(frame_data, dtype=np.uint8)
            frame = cv2.imdecode(frame_array, cv2.IMREAD_COLOR)
            if frame is None:
                continue
            processed = await asyncio.to_thread(process_browser_frame, frame)
            if processed:
                await websocket.send_bytes(processed)
    except WebSocketDisconnect:
        pass


@app.on_event("shutdown")
def on_shutdown():
    state.running = False
    if state.record_writer is not None:
        state.record_writer.release()