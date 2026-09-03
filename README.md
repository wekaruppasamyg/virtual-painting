# Virtual Painting (MediaPipe + OpenCV + React)

Hand-tracking virtual painter. Python backend (FastAPI + OpenCV + MediaPipe) runs the
webcam, detects your hand, and draws on a persistent canvas. It streams the combined
video+painting to a React frontend over WebSocket. The frontend is the control panel:
color picker, brush size, eraser, clear, and save-to-file.

## How drawing works

- ☝️ **Only index finger up** → draw mode (brush follows fingertip).
- ✌️ **Index + middle both up** → hover mode (move without drawing).
- Switch to eraser in the sidebar to erase instead of draw.

## Project structure

```
virtual-painting/
├── backend/
│   ├── main.py           # FastAPI app: camera loop, MediaPipe, WebSocket stream, REST API
│   ├── requirements.txt
│   └── saved_paintings/  # created automatically, holds saved PNGs
└── frontend/
    ├── src/
    │   ├── App.jsx        # video display + control panel
    │   ├── main.jsx
    │   └── index.css
    ├── index.html
    ├── package.json
    └── vite.config.js
```

## Backend setup

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The webcam opens as soon as the server starts (device index `0`). If you have multiple
cameras, change `cv2.VideoCapture(0)` in `main.py` to the right index.

## Frontend setup

```bash
cd frontend
npm install
npm run dev
```

Open the printed local URL (typically `http://localhost:5173`). Make sure the backend
is already running on `http://localhost:8000` — the frontend connects to it directly
(see `BACKEND_HTTP` / `BACKEND_WS` constants at the top of `src/App.jsx`).

## REST API summary

| Method | Path                        | Body                          | Purpose                     |
|--------|-----------------------------|--------------------------------|------------------------------|
| POST   | `/api/color`                | `{ "r": 0, "g": 0, "b": 0 }`   | Set brush color              |
| POST   | `/api/brush_size`           | `{ "size": 8 }`                | Set brush thickness          |
| POST   | `/api/tool`                 | `{ "tool": "draw"|"eraser" }` | Switch tool                  |
| POST   | `/api/clear`                | –                               | Clear the canvas             |
| POST   | `/api/save`                 | –                               | Save canvas as PNG on server |
| GET    | `/api/download/{filename}`  | –                               | Download a saved PNG         |
| GET    | `/api/status`               | –                               | Current tool/brush/color     |
| WS     | `/ws/stream`                | –                               | Live JPEG frame stream       |

## Notes / next steps you might want

- **Multiple hands**: `max_num_hands=1` in `main.py` — bump it if you want two-hand support.
- **Persistence across restarts**: canvas currently lives in memory only; it resets when
  the backend restarts. Could add periodic autosave or load-canvas-on-save if needed.
- **Gesture-based color/tool switching**: right now tool/color changes come from the
  React sidebar. You could add an on-screen palette + pinch-to-select if you want it
  fully gesture-driven like the classic "AI Virtual Painter" tutorials.
- **Deployment**: this is built for local development (webcam access, `localhost` URLs).
  Running MediaPipe/OpenCV against a webcam from a deployed backend isn't practical —
  the camera has to be on the machine running `main.py`.
