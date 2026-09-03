import React, { useEffect, useRef, useState } from "react";

const BACKEND_HTTP = (import.meta.env.VITE_BACKEND_URL || "https://virtual-painting-qmt7.onrender.com")
  .replace(/\/$/, "");
const BACKEND_WS = `${BACKEND_HTTP.replace(/^http/, "ws")}/ws/camera`;

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
  const imgRef = useRef(null);
  const cameraVideoRef = useRef(null);
  const cameraCanvasRef = useRef(null);
  const wsRef = useRef(null);

  const [connected, setConnected] = useState(false);
  const [activeColor, setActiveColor] = useState(COLORS[0].hex);
  const [brushSize, setBrushSize] = useState(8);
  const [tool, setTool] = useState("draw"); // "draw" | "eraser"
  const [saveMsg, setSaveMsg] = useState("");

  // --- WebSocket video stream ---
  useEffect(() => {
    const ws = new WebSocket(BACKEND_WS);
    let mediaStream;
    let sendTimer;
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = async () => {
      try {
        mediaStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        const video = cameraVideoRef.current;
        video.srcObject = mediaStream;
        await video.play();
        setConnected(true);
        sendTimer = window.setInterval(() => {
          if (ws.readyState !== WebSocket.OPEN || video.readyState < 2) return;
          const canvas = cameraCanvasRef.current;
          canvas.width = video.videoWidth || 640;
          canvas.height = video.videoHeight || 480;
          canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
          canvas.toBlob((blob) => {
            if (blob && ws.readyState === WebSocket.OPEN) ws.send(blob);
          }, "image/jpeg", 0.7);
        }, 100);
      } catch (error) {
        setConnected(false);
      }
    };
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);

    ws.onmessage = (event) => {
      const blob = new Blob([event.data], { type: "image/jpeg" });
      const url = URL.createObjectURL(blob);
      if (imgRef.current) {
        const prevUrl = imgRef.current.dataset.blobUrl;
        imgRef.current.src = url;
        imgRef.current.dataset.blobUrl = url;
        if (prevUrl) URL.revokeObjectURL(prevUrl);
      }
    };

    return () => {
      window.clearInterval(sendTimer);
      if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
      ws.close();
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
    api("/api/clear", {});
  };

  const handleSave = async () => {
    setSaveMsg("Saving...");
    try {
      const res = await fetch(`${BACKEND_HTTP}/api/save`, { method: "POST" });
      const data = await res.json();
      if (data.ok) {
        const link = document.createElement("a");
        link.href = `${BACKEND_HTTP}/api/download/${data.filename}`;
        link.download = data.filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setSaveMsg(`Saved: ${data.filename}`);
      } else {
        setSaveMsg(`Error: ${data.error || "unknown"}`);
      }
    } catch (e) {
      setSaveMsg("Error saving painting");
    }
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
          <canvas ref={cameraCanvasRef} style={{ display: "none" }} />
          <img ref={imgRef} alt="Live painting stream" style={styles.video} />
          {!connected && (
            <div style={styles.overlay}>
              Waiting for backend at <code>{BACKEND_WS}</code>...
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
