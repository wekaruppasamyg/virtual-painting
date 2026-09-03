import React, { useEffect, useRef, useState } from "react";
import { FilesetResolver, HandLandmarker } from "@mediapipe/tasks-vision";

const BACKEND_HTTP = (import.meta.env.VITE_BACKEND_URL || "https://virtual-painting-qmt7.onrender.com")
  .replace(/\/$/, "");
const COLORS = [
  { name: "Red", hex: "#EF4444" },
  { name: "Orange", hex: "#F97316" },
  { name: "Yellow", hex: "#EAB308" },
  { name: "Green", hex: "#22C55E" },
  { name: "Blue", hex: "#3B82F6" },
  { name: "Purple", hex: "#A855F7" },
  { name: "Black", hex: "#111111" },
  { name: "White", hex: "#FFFFFF" },
];

function hexToRgb(hex) {
  const clean = hex.replace("#", "");
  const bigint = parseInt(clean, 16);
  return {
    r: (bigint >> 16) & 255,
    g: (bigint >> 8) & 255,
    b: bigint & 255,
  };
}

export default function App() {
  const cameraVideoRef = useRef(null);
  const outputCanvasRef = useRef(null);
  const paintingCanvasRef = useRef(null);
  const settingsRef = useRef({ color: COLORS[0].hex, brushSize: 8, tool: "draw" });

  const [connected, setConnected] = useState(false);
  const [activeColor, setActiveColor] = useState(COLORS[0].hex);
  const [brushSize, setBrushSize] = useState(8);
  const [tool, setTool] = useState("draw"); // "draw" | "eraser"
  const [saveMsg, setSaveMsg] = useState("");

  settingsRef.current = { color: activeColor, brushSize, tool };

  // --- Local camera and hand tracking: no Render round-trip ---
  useEffect(() => {
    let mediaStream;
    let animationId;
    let landmarker;
    let lastVideoTime = -1;
    let previousPoint = null;

    const start = async () => {
      try {
        mediaStream = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 30, max: 30 } },
          audio: false,
        });
        const video = cameraVideoRef.current;
        const output = outputCanvasRef.current;
        const painting = paintingCanvasRef.current;
        video.srcObject = mediaStream;
        await video.play();
        const vision = await FilesetResolver.forVisionTasks(
          "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.22/wasm"
        );
        landmarker = await HandLandmarker.createFromOptions(vision, {
          baseOptions: {
            modelAssetPath: "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
            delegate: "GPU",
          },
          runningMode: "VIDEO",
          numHands: 1,
          minHandDetectionConfidence: 0.6,
          minHandPresenceConfidence: 0.6,
          minTrackingConfidence: 0.6,
        });
        output.width = painting.width = 640;
        output.height = painting.height = 480;
        setConnected(true);

        const drawFrame = () => {
          if (video.readyState >= 2 && video.currentTime !== lastVideoTime) {
            lastVideoTime = video.currentTime;
            const outputContext = output.getContext("2d");
            const paintingContext = painting.getContext("2d");
            outputContext.save();
            outputContext.scale(-1, 1);
            outputContext.drawImage(video, -output.width, 0, output.width, output.height);
            outputContext.restore();
            const result = landmarker.detectForVideo(video, performance.now());
            const hand = result.landmarks?.[0];
            const settings = settingsRef.current;
            if (hand) {
              const index = hand[8];
              const point = { x: (1 - index.x) * output.width, y: index.y * output.height };
              const indexUp = hand[8].y < hand[6].y;
              const middleUp = hand[12].y < hand[10].y;
              const drawing = indexUp && !middleUp;
              if (drawing) {
                if (previousPoint) {
                  paintingContext.strokeStyle = settings.tool === "eraser" ? "rgba(0,0,0,1)" : settings.color;
                  paintingContext.lineWidth = settings.tool === "eraser" ? 40 : settings.brushSize;
                  paintingContext.globalCompositeOperation = settings.tool === "eraser" ? "destination-out" : "source-over";
                  paintingContext.lineCap = "round";
                  paintingContext.beginPath();
                  paintingContext.moveTo(previousPoint.x, previousPoint.y);
                  paintingContext.lineTo(point.x, point.y);
                  paintingContext.stroke();
                }
                previousPoint = point;
              } else {
                previousPoint = null;
              }
              outputContext.fillStyle = settings.tool === "eraser" ? "white" : settings.color;
              outputContext.beginPath();
              outputContext.arc(point.x, point.y, 7, 0, Math.PI * 2);
              outputContext.fill();
            } else {
              previousPoint = null;
            }
            outputContext.globalCompositeOperation = "source-over";
            outputContext.drawImage(painting, 0, 0);
            outputContext.fillStyle = "white";
            outputContext.font = "bold 22px sans-serif";
            outputContext.fillText(`${hand ? "READY" : "NO HAND"}  |  tool: ${settings.tool}`, 12, 30);
          }
          animationId = requestAnimationFrame(drawFrame);
        };
        drawFrame();
      } catch (error) {
        setConnected(false);
      }
    };
    start();

    return () => {
      cancelAnimationFrame(animationId);
      if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
      if (landmarker) landmarker.close();
    };
  }, []);

  // --- API helpers ---
  const api = async (path, body) => {
    await fetch(`${BACKEND_HTTP}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  };

  const handleColorSelect = (hex) => {
    setActiveColor(hex);
    setTool("draw");
    const { r, g, b } = hexToRgb(hex);
    api("/api/color", { r, g, b });
    api("/api/tool", { tool: "draw" });
  };

  const handleBrushSize = (value) => {
    setBrushSize(value);
    api("/api/brush_size", { size: Number(value) });
  };

  const handleToolToggle = (nextTool) => {
    setTool(nextTool);
    api("/api/tool", { tool: nextTool });
  };

  const handleClear = () => {
    const canvas = paintingCanvasRef.current;
    if (canvas) canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  };

  const handleSave = async () => {
    const canvas = outputCanvasRef.current;
    if (!canvas) return;
    const link = document.createElement("a");
    link.download = `virtual-painting-${Date.now()}.png`;
    link.href = canvas.toDataURL("image/png");
    link.click();
    setSaveMsg("Painting downloaded");
    setTimeout(() => setSaveMsg(""), 3000);
  };

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <h1 style={styles.title}>Virtual Painting</h1>
        <span style={{ ...styles.status, color: connected ? "#22c55e" : "#ef4444" }}>
          {connected ? "● Connected" : "● Disconnected"}
        </span>
      </header>

      <div style={styles.main}>
        <div style={styles.videoWrap}> 
          <video ref={cameraVideoRef} muted playsInline style={{ display: "none" }} />
          <canvas ref={paintingCanvasRef} style={{ display: "none" }} />
          <canvas ref={outputCanvasRef} aria-label="Live painting camera" style={styles.video} />
          {!connected && (
            <div style={styles.overlay}>
              Starting local camera and hand tracking...
            </div>
          )}
        </div>

        <aside style={styles.sidebar}>
          <section style={styles.section}>
            <h3 style={styles.sectionTitle}>Color</h3>
            <div style={styles.swatchGrid}>
              {COLORS.map((c) => (
                <button
                  key={c.hex}
                  onClick={() => handleColorSelect(c.hex)}
                  title={c.name}
                  style={{
                    ...styles.swatch,
                    backgroundColor: c.hex,
                    outline:
                      activeColor === c.hex && tool === "draw"
                        ? "3px solid #3B82F6"
                        : "1px solid #d1d5db",
                  }}
                />
              ))}
            </div>
          </section>

          <section style={styles.section}>
            <h3 style={styles.sectionTitle}>Tool</h3>
            <div style={styles.toolRow}>
              <button
                onClick={() => handleToolToggle("draw")}
                style={{
                  ...styles.toolBtn,
                  ...(tool === "draw" ? styles.toolBtnActive : {}),
                }}
              >
                🖌️ Brush
              </button>
              <button
                onClick={() => handleToolToggle("eraser")}
                style={{
                  ...styles.toolBtn,
                  ...(tool === "eraser" ? styles.toolBtnActive : {}),
                }}
              >
                🧹 Eraser
              </button>
            </div>
          </section>

          <section style={styles.section}>
            <h3 style={styles.sectionTitle}>Brush Size: {brushSize}px</h3>
            <input
              type="range"
              min="1"
              max="60"
              value={brushSize}
              onChange={(e) => handleBrushSize(e.target.value)}
              style={styles.slider}
            />
          </section>

          <section style={styles.section}>
            <button onClick={handleClear} style={styles.actionBtn}>
              🗑️ Clear Canvas
            </button>
            <button onClick={handleSave} style={{ ...styles.actionBtn, ...styles.saveBtn }}>
              💾 Save Painting
            </button>
            {saveMsg && <p style={styles.saveMsg}>{saveMsg}</p>}
          </section>

          <section style={styles.section}>
            <h3 style={styles.sectionTitle}>How to draw</h3>
            <ul style={styles.helpList}>
              <li>Raise only your ☝️ index finger to draw.</li>
              <li>Raise index + middle ✌️ to move without drawing.</li>
              <li>Switch to eraser above to remove strokes.</li>
            </ul>
          </section>
        </aside>
      </div>
    </div>
  );
}

const styles = {
  page: {
    minHeight: "100vh",
    background: "#0f172a",
    color: "#e2e8f0",
    fontFamily: "system-ui, sans-serif",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "16px 24px",
    borderBottom: "1px solid #1e293b",
  },
  title: { margin: 0, fontSize: "20px", fontWeight: 700 },
  status: { fontSize: "13px", fontWeight: 600 },
  main: {
    display: "flex",
    gap: "20px",
    padding: "20px",
    flexWrap: "wrap",
  },
  videoWrap: {
    position: "relative",
    flex: "1 1 640px",
    background: "#000",
    borderRadius: "12px",
    overflow: "hidden",
    minHeight: "480px",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },
  video: { width: "100%", height: "auto", display: "block" },
  overlay: {
    position: "absolute",
    color: "#94a3b8",
    fontSize: "14px",
    textAlign: "center",
    padding: "12px",
  },
  sidebar: {
    flex: "0 0 260px",
    background: "#1e293b",
    borderRadius: "12px",
    padding: "16px",
    display: "flex",
    flexDirection: "column",
    gap: "18px",
  },
  section: {},
  sectionTitle: {
    margin: "0 0 8px 0",
    fontSize: "13px",
    textTransform: "uppercase",
    letterSpacing: "0.05em",
    color: "#94a3b8",
  },
  swatchGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(4, 1fr)",
    gap: "8px",
  },
  swatch: {
    width: "40px",
    height: "40px",
    borderRadius: "8px",
    border: "none",
    cursor: "pointer",
  },
  toolRow: { display: "flex", gap: "8px" },
  toolBtn: {
    flex: 1,
    padding: "10px",
    borderRadius: "8px",
    border: "1px solid #334155",
    background: "#0f172a",
    color: "#e2e8f0",
    cursor: "pointer",
    fontSize: "13px",
  },
  toolBtnActive: {
    background: "#3B82F6",
    borderColor: "#3B82F6",
    color: "#fff",
  },
  slider: { width: "100%" },
  actionBtn: {
    width: "100%",
    padding: "10px",
    marginBottom: "8px",
    borderRadius: "8px",
    border: "1px solid #334155",
    background: "#0f172a",
    color: "#e2e8f0",
    cursor: "pointer",
    fontSize: "14px",
  },
  saveBtn: { background: "#16a34a", borderColor: "#16a34a", color: "#fff" },
  saveMsg: { fontSize: "12px", color: "#94a3b8", marginTop: "4px" },
  helpList: { fontSize: "12px", color: "#94a3b8", paddingLeft: "18px", lineHeight: 1.6 },
};
