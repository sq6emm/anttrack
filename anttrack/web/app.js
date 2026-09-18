(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // -- optional API key (only asked for if the server actually requires one) --
  function getApiKey() {
    try { return localStorage.getItem("anttrack_api_key") || ""; } catch { return ""; }
  }
  function setApiKey(key) {
    try { localStorage.setItem("anttrack_api_key", key); } catch { /* ignore */ }
  }

  async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers, { "Content-Type": "application/json" });
    const key = getApiKey();
    if (key) headers["X-API-Key"] = key;
    const res = await fetch(path, Object.assign({}, options, { headers }));
    if (res.status === 401) {
      const key = window.prompt("This AntTrack server requires an API key:");
      if (key) { setApiKey(key); return api(path, options); }
      throw new Error("unauthorized");
    }
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) throw new Error((data && data.detail) || res.statusText);
    return data;
  }

  // -- theme toggle (per-viewer convenience only) --
  const themeToggle = $("theme-toggle");
  function applyStoredTheme() {
    let theme = "";
    try { theme = localStorage.getItem("anttrack_theme") || ""; } catch { /* ignore */ }
    if (theme) document.documentElement.setAttribute("data-theme", theme);
  }
  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme")
      || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("anttrack_theme", next); } catch { /* ignore */ }
  });
  applyStoredTheme();

  // -- tabs --
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $("tab-" + btn.dataset.tab).classList.add("active");
    });
  });

  // -- catalog (for the datalist autocomplete) --
  async function loadCatalog() {
    try {
      const data = await api("/api/catalog");
      const list = $("target-list");
      list.innerHTML = "";
      const names = [...data.bodies, ...data.satellites];
      const frag = document.createDocumentFragment();
      for (const name of names) {
        const opt = document.createElement("option");
        opt.value = name;
        frag.appendChild(opt);
      }
      list.appendChild(frag);
    } catch (err) {
      console.error("catalog load failed", err);
    }
  }
  loadCatalog();

  // -- actions --
  $("track-btn").addEventListener("click", async () => {
    const name = $("target-input").value.trim();
    if (!name) return;
    try { await api("/api/track", { method: "POST", body: JSON.stringify({ name }) }); }
    catch (err) { alert("Could not start tracking: " + err.message); }
  });

  $("loc-btn").addEventListener("click", async () => {
    const locator = $("loc-input").value.trim();
    if (!locator) return;
    try { await api("/api/track/loc", { method: "POST", body: JSON.stringify({ locator }) }); }
    catch (err) { alert("Could not point at locator: " + err.message); }
  });

  $("raw-btn").addEventListener("click", async () => {
    const az = parseFloat($("raw-az").value);
    const el = parseFloat($("raw-el").value);
    if (Number.isNaN(az) || Number.isNaN(el)) { alert("Enter both azimuth and elevation."); return; }
    try { await api("/api/track/raw", { method: "POST", body: JSON.stringify({ az, el }) }); }
    catch (err) { alert("Could not set position: " + err.message); }
  });

  $("stop-btn").addEventListener("click", async () => {
    try { await api("/api/stop", { method: "POST" }); }
    catch (err) { alert("Could not stop: " + err.message); }
  });

  // -- compass (SVG polar plot: radius = 90 - elevation, angle = azimuth) --
  const NS = "http://www.w3.org/2000/svg";
  const compass = $("compass");
  const CENTER = 120, MAX_R = 100;

  function polarPoint(azDeg, elDeg) {
    const r = MAX_R * (1 - Math.max(0, Math.min(90, elDeg)) / 90);
    const theta = (azDeg - 90) * Math.PI / 180; // 0deg az = up
    return [CENTER + r * Math.cos(theta), CENTER + r * Math.sin(theta)];
  }

  function buildCompassChrome() {
    compass.innerHTML = "";
    const gridColor = getComputedStyle(document.documentElement).getPropertyValue("--gridline").trim();
    const mutedColor = getComputedStyle(document.documentElement).getPropertyValue("--text-muted").trim();
    for (const elRing of [0, 30, 60]) {
      const r = MAX_R * (1 - elRing / 90);
      const circle = document.createElementNS(NS, "circle");
      circle.setAttribute("cx", CENTER); circle.setAttribute("cy", CENTER); circle.setAttribute("r", r);
      circle.setAttribute("fill", "none"); circle.setAttribute("stroke", gridColor); circle.setAttribute("stroke-width", "1");
      compass.appendChild(circle);
    }
    const dirs = [["N", 0], ["E", 90], ["S", 180], ["W", 270]];
    for (const [label, az] of dirs) {
      const [x, y] = polarPoint(az, -8);
      const text = document.createElementNS(NS, "text");
      text.setAttribute("x", x); text.setAttribute("y", y + 4);
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("font-size", "11"); text.setAttribute("fill", mutedColor);
      text.textContent = label;
      compass.appendChild(text);
    }
  }
  buildCompassChrome();

  // -- elevation gauge (SVG quarter-circle protractor: 0deg = horizon, 90deg = zenith) --
  const elevationGauge = $("elevation-gauge");
  const EL_CX = 20, EL_CY = 115, EL_R = 85;

  function elevationPoint(elDeg, radius = EL_R) {
    const e = Math.max(0, Math.min(90, elDeg)) * Math.PI / 180;
    return [EL_CX + radius * Math.cos(e), EL_CY - radius * Math.sin(e)];
  }

  function buildElevationChrome() {
    elevationGauge.innerHTML = "";
    const gridColor = getComputedStyle(document.documentElement).getPropertyValue("--gridline").trim();
    const baselineColor = getComputedStyle(document.documentElement).getPropertyValue("--baseline").trim();
    const mutedColor = getComputedStyle(document.documentElement).getPropertyValue("--text-muted").trim();

    const arcPoints = [];
    for (let e = 0; e <= 90; e += 3) arcPoints.push(elevationPoint(e).join(","));
    const arc = document.createElementNS(NS, "polyline");
    arc.setAttribute("points", arcPoints.join(" "));
    arc.setAttribute("fill", "none");
    arc.setAttribute("stroke", gridColor);
    arc.setAttribute("stroke-width", "1");
    elevationGauge.appendChild(arc);

    for (const [x1, y1, x2, y2] of [
      [EL_CX, EL_CY, EL_CX + EL_R + 14, EL_CY],       // horizon baseline
      [EL_CX, EL_CY, EL_CX, EL_CY - EL_R - 14],       // zenith guide
    ]) {
      const line = document.createElementNS(NS, "line");
      line.setAttribute("x1", x1); line.setAttribute("y1", y1);
      line.setAttribute("x2", x2); line.setAttribute("y2", y2);
      line.setAttribute("stroke", baselineColor);
      line.setAttribute("stroke-width", "1.5");
      elevationGauge.appendChild(line);
    }

    for (const e of [0, 30, 60, 90]) {
      const [x, y] = elevationPoint(e);
      const [xOut, yOut] = elevationPoint(e, EL_R + 8);
      const tick = document.createElementNS(NS, "line");
      tick.setAttribute("x1", x); tick.setAttribute("y1", y);
      tick.setAttribute("x2", xOut); tick.setAttribute("y2", yOut);
      tick.setAttribute("stroke", mutedColor);
      tick.setAttribute("stroke-width", "1.5");
      elevationGauge.appendChild(tick);

      const [xLabel, yLabel] = elevationPoint(e, EL_R + 22);
      const text = document.createElementNS(NS, "text");
      text.setAttribute("x", xLabel); text.setAttribute("y", yLabel + 4);
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("font-size", "11"); text.setAttribute("fill", mutedColor);
      text.textContent = e + "°";
      elevationGauge.appendChild(text);
    }
  }
  buildElevationChrome();

  function placeMarker(svgEl, id, point, colorVar, filled) {
    let el = document.getElementById(id);
    if (!point) { if (el) el.remove(); return; }
    const [x, y] = point;
    const color = getComputedStyle(document.documentElement).getPropertyValue(colorVar).trim();
    if (!el) {
      el = document.createElementNS(NS, "circle");
      el.id = id;
      el.setAttribute("r", 6);
      svgEl.appendChild(el);
    }
    el.setAttribute("cx", x); el.setAttribute("cy", y);
    el.setAttribute("fill", filled ? color : "none");
    el.setAttribute("stroke", color);
    el.setAttribute("stroke-width", "2");
  }

  function drawMarker(azDeg, elDeg, colorVar, id, filled) {
    const point = (azDeg == null || elDeg == null) ? null : polarPoint(azDeg, elDeg);
    placeMarker(compass, id, point, colorVar, filled);
  }

  function drawElevationMarker(elDeg, colorVar, id, filled) {
    const point = (elDeg == null) ? null : elevationPoint(elDeg);
    placeMarker(elevationGauge, id, point, colorVar, filled);
  }

  // -- status rendering --
  function fmtAzEl(pos) {
    if (!pos) return "—";
    return `${pos.az.toFixed(1)}° / ${pos.el.toFixed(1)}°`;
  }

  function renderStatus(s) {
    $("s-mode").textContent = s.mode;
    $("s-request").textContent = s.request || "—";
    $("s-commanded").textContent = fmtAzEl(s.commanded);
    $("s-actual").textContent = fmtAzEl(s.actual);
    $("s-error").textContent = (s.az_error_deg != null && s.el_error_deg != null)
      ? `az ${s.az_error_deg.toFixed(2)}° / el ${s.el_error_deg.toFixed(2)}°` : "—";
    $("s-horizon").textContent = s.below_horizon == null ? "—" : (s.below_horizon ? "Yes" : "No");
    $("s-locator").textContent = (s.heading_deg != null)
      ? `${s.heading_deg}° / ${s.distance_km} km` : "—";
    $("s-updated").textContent = s.last_update_utc || "—";

    const pill = $("conn-pill");
    pill.classList.remove("good", "critical");
    if (s.connected) { pill.classList.add("good"); $("conn-label").textContent = "Connected"; }
    else { pill.classList.add("critical"); $("conn-label").textContent = "Disconnected"; }

    const banner = $("banner");
    banner.classList.remove("show", "warning", "critical");
    if (s.error) {
      banner.textContent = "Error: " + s.error;
      banner.classList.add("show", "critical");
    } else if (s.warning) {
      banner.textContent = s.warning;
      banner.classList.add("show", "warning");
    }

    drawMarker(s.commanded && s.commanded.az, s.commanded && s.commanded.el,
      "--series-commanded", "marker-commanded", false);
    drawMarker(s.actual && s.actual.az, s.actual && s.actual.el,
      "--series-actual", "marker-actual", true);

    drawElevationMarker(s.commanded && s.commanded.el,
      "--series-commanded", "el-marker-commanded", false);
    drawElevationMarker(s.actual && s.actual.el,
      "--series-actual", "el-marker-actual", true);
  }

  // -- log panel --
  let lastSeq = 0;
  const logPanel = $("log-panel");
  async function pollLog() {
    try {
      const data = await api(`/api/log?since=${lastSeq}`);
      if (data.lines.length) {
        const atBottom = logPanel.scrollTop + logPanel.clientHeight >= logPanel.scrollHeight - 4;
        for (const { seq, line } of data.lines) {
          logPanel.textContent += line + "\n";
          lastSeq = Math.max(lastSeq, seq);
        }
        if (atBottom) logPanel.scrollTop = logPanel.scrollHeight;
      }
    } catch (err) {
      console.error("log poll failed", err);
    }
  }
  setInterval(pollLog, 2000);
  pollLog();

  // -- camera (optional RTSP-relayed fMP4 preview, native <video> playback) --
  function withKey(path) {
    const key = getApiKey();
    return key ? `${path}${path.includes("?") ? "&" : "?"}key=${encodeURIComponent(key)}` : path;
  }

  const cameraPanel = $("camera-panel");
  const cameraVideo = $("camera-video");
  const cameraOverlay = $("camera-overlay");
  let cameraStarted = false;
  let cameraRetryTimer = null;

  function setCameraOverlay(text) {
    if (text) { cameraOverlay.textContent = text; cameraOverlay.classList.add("show"); }
    else { cameraOverlay.classList.remove("show"); }
  }

  function startCameraStream() {
    clearTimeout(cameraRetryTimer);
    setCameraOverlay("Connecting…");
    cameraVideo.src = withKey(`/api/camera/stream.mp4?t=${Date.now()}`);
    cameraVideo.load();
    cameraVideo.play().catch(() => { /* autoplay can reject before data arrives; ignore */ });
  }

  function retryCameraStream() {
    setCameraOverlay("Camera unavailable – retrying…");
    cameraRetryTimer = setTimeout(startCameraStream, 5000);
  }

  cameraVideo.addEventListener("playing", () => setCameraOverlay(""));
  cameraVideo.addEventListener("error", retryCameraStream);
  cameraVideo.addEventListener("ended", retryCameraStream);

  async function pollCameraStatus() {
    try {
      const status = await api("/api/camera/status");
      if (!status.enabled) { cameraPanel.hidden = true; return; }
      cameraPanel.hidden = false;
      if (!cameraStarted) { cameraStarted = true; startCameraStream(); }
    } catch (err) {
      console.error("camera status poll failed", err);
    }
  }
  setInterval(pollCameraStatus, 5000);
  pollCameraStatus();

  // -- live status via WebSocket, falling back to polling --
  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const key = getApiKey();
    const qs = key ? `?key=${encodeURIComponent(key)}` : "";
    const ws = new WebSocket(`${proto}//${location.host}/ws/status${qs}`);
    ws.onmessage = (ev) => {
      try { renderStatus(JSON.parse(ev.data)); } catch { /* ignore */ }
    };
    ws.onclose = () => setTimeout(connectWs, 2000);
    ws.onerror = () => ws.close();
  }
  connectWs();
})();
