"use strict";

const state = {
  runs: [],
  activeRun: null,
  manifest: null,
  report: null,
  events: [],
  frames: [],
  activeFrame: 0,
  selectedEvent: null,
  detailValue: null,
  playing: false,
  animationFrame: null,
  loadToken: 0,
};

const elements = Object.fromEntries(
  [
    "runCount", "refreshButton", "runFilter", "runList", "runEmpty",
    "runPath", "runTitle", "runScene", "runModeBadge", "runStatusBadge",
    "loadError", "legacyNotice", "cameraVideo", "cameraEmpty", "cameraState",
    "topdownVideo", "topdownEmpty", "topdownState", "playButton", "frameSlider",
    "frameReadout", "timeReadout", "simStep", "subtaskStep", "actionStep",
    "progressLabel", "runProgress", "componentFilter", "eventCount", "timeline",
    "timelineEmpty", "detailComponent", "detailTitle", "detailMeta", "detailJson",
    "copyDetail",
  ].map((id) => [id, document.getElementById(id)]),
);

function apiUrl(runId, resource) {
  return `/api/runs/${encodeURIComponent(runId)}/${resource}`;
}

function artifactUrl(runId, path) {
  const encoded = String(path)
    .split("/")
    .filter(Boolean)
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `/api/runs/${encodeURIComponent(runId)}/artifacts/${encoded}`;
}

async function fetchJson(url, optional = false) {
  const response = await fetch(url, {cache: "no-store"});
  if (optional && response.status === 404) {
    return null;
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const problem = await response.json();
      message = problem.message || message;
    } catch (_error) {
      // Keep the HTTP status when the response is not JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

function scalar(value, fallback = null) {
  return value === undefined || value === null || value === "" ? fallback : value;
}

function finiteNumber(value) {
  if (value === undefined || value === null || value === "") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function at(object, path) {
  let current = object;
  for (const key of path) {
    if (!current || typeof current !== "object" || !(key in current)) {
      return null;
    }
    current = current[key];
  }
  return scalar(current);
}

function first(object, paths) {
  for (const path of paths) {
    const value = at(object, path);
    if (value !== null) {
      return value;
    }
  }
  return null;
}

function asArtifactPath(value) {
  if (value && typeof value === "object") {
    value = value.path || value.file || value.src || value.name;
  }
  if (typeof value !== "string") {
    return null;
  }
  const path = value.trim();
  if (!path || path.startsWith("/") || path.includes("\\")) {
    return null;
  }
  const parts = path.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) {
    return null;
  }
  return parts.join("/");
}

function mediaPath(role, manifest, report, run) {
  const cameraPaths = [
    ["media", "camera"], ["media", "camera_video"], ["videos", "camera"],
    ["artifacts", "replay_camera"], ["artifacts", "replay_camera.mp4"],
    ["artifacts", "camera"], ["artifacts", "camera_video"], ["replay_camera"],
    ["replay_camera_path"], ["camera_video"], ["video"],
  ];
  const topdownPaths = [
    ["media", "topdown"], ["media", "top_down"], ["media", "topdown_video"],
    ["videos", "topdown"], ["artifacts", "replay_topdown"],
    ["artifacts", "replay_topdown.mp4"], ["artifacts", "topdown"],
    ["artifacts", "topdown_video"], ["replay_topdown"], ["replay_topdown_path"],
    ["topdown_video"],
  ];
  const paths = role === "camera" ? cameraPaths : topdownPaths;
  const manifestValue = first(manifest || {}, paths);
  const reportValue = role === "camera"
    ? first(report || {}, [["video", "path"], ["video"]])
    : first(report || {}, [["topdown_video", "path"], ["topdown_video"]]);
  return asArtifactPath(manifestValue)
    || asArtifactPath(reportValue)
    || asArtifactPath(run && run.media ? run.media[role] : null);
}

function frameList(manifest) {
  const raw = first(manifest || {}, [
    ["frames"], ["frame_timeline"], ["frame_map"], ["synchronization", "frames"],
    ["timeline", "frames"], ["media", "frames"],
  ]);
  if (!Array.isArray(raw)) {
    return [];
  }
  const cameraFps = Number(first(manifest || {}, [
    ["media", "camera", "fps"], ["videos", "camera", "fps"], ["camera_fps"],
    ["video_fps"], ["fps"],
  ])) || 30;
  const topdownFps = Number(first(manifest || {}, [
    ["media", "topdown", "fps"], ["videos", "topdown", "fps"],
    ["topdown_fps"], ["video_fps"], ["fps"],
  ])) || cameraFps;

  return raw
    .filter((item) => item && typeof item === "object")
    .map((item, position) => {
      const index = finiteNumber(scalar(
        first(item, [["frame_index"], ["index"], ["frame"]]),
        position,
      ));
      const commonTime = finiteNumber(first(item, [
        ["video_time_seconds"], ["time_seconds"], ["video_time"], ["timestamp_seconds"],
      ]));
      const cameraTimeValue = finiteNumber(first(item, [
        ["camera_time_seconds"], ["camera_time"], ["media_time_seconds"],
      ]));
      const topdownTimeValue = finiteNumber(first(item, [
        ["topdown_time_seconds"], ["topdown_time"],
      ]));
      return {
        raw: item,
        index: Number.isFinite(index) ? index : position,
        cameraTime: Number.isFinite(cameraTimeValue)
          ? cameraTimeValue
          : (Number.isFinite(commonTime) ? commonTime : position / cameraFps),
        topdownTime: Number.isFinite(topdownTimeValue)
          ? topdownTimeValue
          : (Number.isFinite(commonTime) ? commonTime : position / topdownFps),
        simStep: first(item, [["sim_step"], ["global_step"], ["step"]]),
        seq: first(item, [["seq"], ["event_seq"], ["timeline_seq"]]),
        actionId: first(item, [["action_id"], ["action", "id"]]),
        actionIndex: first(item, [["action_index"], ["action_number"]]),
        actionLabel: first(item, [["action", "action"], ["action", "text"], ["action"]]),
        subtask: first(item, [["subtask"], ["subtask_index"], ["subtask_id"]]),
      };
    });
}

function formatDate(value) {
  if (!value) {
    return "Unknown date";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function formatTime(seconds) {
  if (!Number.isFinite(Number(seconds))) {
    return "00:00.000";
  }
  const value = Math.max(Number(seconds), 0);
  const minutes = Math.floor(value / 60);
  const remaining = value - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remaining.toFixed(3).padStart(6, "0")}`;
}

function setBadge(element, text, kind = "neutral") {
  element.textContent = text || "--";
  element.className = `badge ${kind}`;
}

function statusKind(status) {
  const value = String(status || "").toLowerCase();
  if (["error", "failed", "failure", "execution_error", "blocked"].some((word) => value.includes(word))) {
    return "error";
  }
  if (["warning", "caution", "partial", "legacy"].some((word) => value.includes(word))) {
    return "warning";
  }
  if (["done", "complete", "success", "passed", "ok"].some((word) => value.includes(word))) {
    return "live";
  }
  return "neutral";
}

function renderRuns() {
  const filter = elements.runFilter.value.trim().toLowerCase();
  const visible = state.runs.filter((run) => [
    run.task, run.scene, run.relative_path, run.name, run.status,
  ].some((value) => String(value || "").toLowerCase().includes(filter)));
  elements.runList.replaceChildren();
  elements.runEmpty.hidden = visible.length > 0;

  for (const run of visible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "run-item";
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(state.activeRun && state.activeRun.id === run.id));
    button.title = run.relative_path;

    const title = document.createElement("span");
    title.className = "run-item-title";
    title.textContent = run.task || run.name;
    const path = document.createElement("span");
    path.className = "run-item-path";
    path.textContent = run.relative_path;
    const meta = document.createElement("span");
    meta.className = "run-item-meta";
    meta.textContent = `${run.legacy ? "Legacy" : "Replay"}  |  ${formatDate(run.started_at || run.modified_at)}`;
    button.append(title, path, meta);
    button.addEventListener("click", () => selectRun(run));
    elements.runList.append(button);
  }
}

async function loadRuns(preferredId = null) {
  elements.refreshButton.disabled = true;
  try {
    const payload = await fetchJson("/api/runs");
    state.runs = Array.isArray(payload.runs) ? payload.runs : [];
    elements.runCount.textContent = `${state.runs.length} ${state.runs.length === 1 ? "run" : "runs"}`;
    renderRuns();
    if (!state.runs.length) {
      resetReplay();
      return;
    }
    const selected = state.runs.find((run) => run.id === preferredId)
      || state.runs.find((run) => state.activeRun && run.id === state.activeRun.id)
      || state.runs[0];
    if (!state.activeRun || state.activeRun.id !== selected.id) {
      await selectRun(selected);
    }
  } catch (error) {
    showError(`Unable to list runs: ${error.message}`);
  } finally {
    elements.refreshButton.disabled = false;
  }
}

function resetReplay() {
  stopPlayback();
  state.activeRun = null;
  state.manifest = null;
  state.report = null;
  state.events = [];
  state.frames = [];
  elements.runPath.textContent = "NO RUN SELECTED";
  elements.runTitle.textContent = "Select a run";
  elements.runScene.textContent = "";
  setBadge(elements.runModeBadge, "Idle");
  setBadge(elements.runStatusBadge, "--");
  configureVideo("camera", null);
  configureVideo("topdown", null);
  renderTimeline();
  updateFrame(0, false);
  showDetails("DETAIL", "Run metadata", {}, {});
}

async function selectRun(run) {
  const token = ++state.loadToken;
  stopPlayback();
  state.activeRun = run;
  state.manifest = null;
  state.report = null;
  state.events = [];
  state.frames = [];
  state.activeFrame = 0;
  state.selectedEvent = null;
  elements.loadError.hidden = true;
  elements.runPath.textContent = run.relative_path;
  elements.runTitle.textContent = run.task || run.name;
  elements.runScene.textContent = [run.scene, formatDate(run.started_at || run.modified_at)].filter(Boolean).join("  |  ");
  setBadge(elements.runModeBadge, run.legacy ? "Legacy" : "Replay", run.legacy ? "warning" : "live");
  setBadge(elements.runStatusBadge, run.status || "Unknown", statusKind(run.status));
  renderRuns();
  window.location.hash = run.id;

  const [manifestResult, eventResult, reportResult] = await Promise.allSettled([
    fetchJson(apiUrl(run.id, "manifest"), true),
    fetchJson(apiUrl(run.id, "events"), true),
    fetchJson(apiUrl(run.id, "report"), true),
  ]);
  if (token !== state.loadToken) {
    return;
  }

  const failures = [manifestResult, eventResult, reportResult]
    .filter((result) => result.status === "rejected")
    .map((result) => result.reason.message);
  state.manifest = manifestResult.status === "fulfilled" ? manifestResult.value : null;
  const eventPayload = eventResult.status === "fulfilled" ? eventResult.value : null;
  state.events = eventPayload && Array.isArray(eventPayload.events) ? eventPayload.events : [];
  state.report = reportResult.status === "fulfilled" ? reportResult.value : null;
  state.frames = frameList(state.manifest);

  if (failures.length) {
    showError(`Some artifacts could not be loaded: ${failures.join("; ")}`);
  }
  const hasSync = state.frames.length > 0;
  elements.legacyNotice.hidden = !run.legacy && hasSync;
  if (!run.legacy && !hasSync) {
    elements.legacyNotice.textContent = "Replay metadata has no synchronized frame map. Videos remain independently playable.";
  } else {
    elements.legacyNotice.textContent = "Legacy artifacts: videos and report are available without frame-to-step synchronization.";
  }
  setBadge(elements.runModeBadge, hasSync ? "Synchronized" : "Legacy", hasSync ? "live" : "warning");

  configureVideo("camera", mediaPath("camera", state.manifest, state.report, run), !hasSync);
  configureVideo("topdown", mediaPath("topdown", state.manifest, state.report, run), !hasSync);
  configureFrameControls();
  configureComponentFilter();
  renderTimeline();
  updateFrame(0, hasSync);
  showDetails(
    "RUN",
    run.task || run.name,
    {
      Path: run.relative_path,
      Scene: run.scene || "--",
      Started: formatDate(run.started_at || run.modified_at),
      Events: state.events.length,
      Frames: state.frames.length,
    },
    {run, manifest: state.manifest, report: state.report},
  );
}

function configureVideo(role, path, legacyControls = false) {
  const video = role === "camera" ? elements.cameraVideo : elements.topdownVideo;
  const empty = role === "camera" ? elements.cameraEmpty : elements.topdownEmpty;
  const label = role === "camera" ? elements.cameraState : elements.topdownState;
  video.pause();
  video.removeAttribute("src");
  video.load();
  video.controls = Boolean(legacyControls);
  video.classList.toggle("available", Boolean(path));
  empty.hidden = Boolean(path);
  label.textContent = path ? path.split("/").pop() : "Unavailable";
  if (path && state.activeRun) {
    video.src = artifactUrl(state.activeRun.id, path);
    video.load();
  }
}

function configureFrameControls() {
  const enabled = state.frames.length > 0;
  elements.frameSlider.disabled = !enabled;
  elements.frameSlider.min = "0";
  elements.frameSlider.max = String(Math.max(state.frames.length - 1, 0));
  elements.frameSlider.value = "0";
  elements.playButton.disabled = !enabled || (!elements.cameraVideo.src && !elements.topdownVideo.src);
}

function configureComponentFilter() {
  const previous = elements.componentFilter.value;
  const components = [...new Set(state.events.map(eventComponent).filter(Boolean))]
    .sort((left, right) => left.localeCompare(right));
  elements.componentFilter.replaceChildren(new Option("All components", ""));
  for (const component of components) {
    elements.componentFilter.add(new Option(component, component));
  }
  elements.componentFilter.value = components.includes(previous) ? previous : "";
}

function eventComponent(event) {
  return String(first(event || {}, [["component"], ["source"], ["module"]]) || "runtime");
}

function eventType(event) {
  return String(first(event || {}, [["event_type"], ["type"], ["name"], ["event"]]) || "event");
}

function eventStatus(event) {
  return String(first(event || {}, [["status"], ["outcome", "status"], ["payload", "status"]]) || "--");
}

function eventSeq(event, index) {
  return scalar(first(event || {}, [["seq"], ["sequence"]]), index + 1);
}

function frameIndexAtOrBeforeSeq(frames, seq) {
  const targetSeq = finiteNumber(seq);
  if (!frames.length || !Number.isFinite(targetSeq)) {
    return 0;
  }
  let selectedIndex = 0;
  let selectedSeq = Number.NEGATIVE_INFINITY;
  frames.forEach((frame, frameIndex) => {
    const frameSeq = finiteNumber(frame.seq);
    if (
      Number.isFinite(frameSeq)
      && frameSeq <= targetSeq
      && (frameSeq > selectedSeq || (frameSeq === selectedSeq && frameIndex > selectedIndex))
    ) {
      selectedIndex = frameIndex;
      selectedSeq = frameSeq;
    }
  });
  return selectedIndex;
}

function renderTimeline() {
  const component = elements.componentFilter.value;
  const filtered = state.events
    .map((event, index) => ({event, index}))
    .filter(({event}) => !component || eventComponent(event) === component);
  elements.timeline.replaceChildren();
  elements.timelineEmpty.hidden = filtered.length > 0;
  elements.eventCount.textContent = `${filtered.length} ${filtered.length === 1 ? "event" : "events"}`;

  for (const {event, index} of filtered) {
    const seq = eventSeq(event, index);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "timeline-row";
    button.dataset.seq = String(seq);
    button.setAttribute("role", "listitem");
    if (state.selectedEvent === event) {
      button.classList.add("selected");
    }
    const status = eventStatus(event);
    const values = [seq, eventComponent(event), eventType(event), status];
    values.forEach((value, column) => {
      const span = document.createElement("span");
      span.textContent = String(value);
      if (column === 0) span.className = "timeline-seq";
      if (column === 1) span.className = "component-label";
      if (column === 3) span.className = `status-label ${String(status).toLowerCase()}`;
      button.append(span);
    });
    button.addEventListener("click", () => selectEvent(event, index));
    elements.timeline.append(button);
  }
  highlightCurrentEvent();
}

function actionLabelFromValue(value) {
  if (typeof value === "string") {
    const text = value.trim();
    return text || null;
  }
  if (!value || typeof value !== "object") {
    return null;
  }
  for (const key of ["raw", "action", "plan", "text"]) {
    if (key in value) {
      const label = actionLabelFromValue(value[key]);
      if (label) {
        return label;
      }
    }
  }
  return null;
}

function eventActionLabel(event) {
  const candidates = [
    ["payload", "action"],
    ["payload", "plan"],
    ["payload", "action_record", "action"],
    ["payload", "details", "action"],
    ["payload", "details", "action_record", "action"],
    ["action"],
  ];
  for (const path of candidates) {
    const label = actionLabelFromValue(at(event || {}, path));
    if (label) {
      return label;
    }
  }
  return null;
}

function actionLabelForFrame(frame) {
  const direct = actionLabelFromValue(frame && frame.actionLabel);
  if (direct) {
    return direct;
  }

  const actionId = scalar(frame && frame.actionId);
  const targetSeq = finiteNumber(frame && frame.seq);
  let resolved = null;
  for (const event of state.events) {
    const eventSeqValue = finiteNumber(eventSeq(event, 0));
    if (Number.isFinite(targetSeq) && Number.isFinite(eventSeqValue) && eventSeqValue > targetSeq) {
      continue;
    }
    const eventActionId = scalar(first(event || {}, [["action_id"], ["payload", "action_id"]]));
    if (actionId && eventActionId && String(eventActionId) !== String(actionId)) {
      continue;
    }
    const label = eventActionLabel(event);
    if (label) {
      resolved = label;
    }
  }
  return resolved || actionId || null;
}

function selectEvent(event, index) {
  state.selectedEvent = event;
  if (state.frames.length) {
    updateFrame(
      frameIndexAtOrBeforeSeq(state.frames, eventSeq(event, index)),
      true,
    );
  }
  renderTimeline();
  const component = eventComponent(event);
  showDetails(
    component,
    eventType(event),
    {
      Seq: eventSeq(event, index),
      Status: eventStatus(event),
      Action: first(event, [["action_id"], ["payload", "action_id"]]) || "--",
      Step: first(event, [["sim_step"], ["global_step"], ["payload", "global_step"]]) || "--",
    },
    event,
  );
}

function updateFrame(index, seekVideos = true) {
  const maxIndex = Math.max(state.frames.length - 1, 0);
  state.activeFrame = Math.max(0, Math.min(Number(index) || 0, maxIndex));
  elements.frameSlider.value = String(state.activeFrame);
  const frame = state.frames[state.activeFrame] || null;
  if (!frame) {
    elements.frameReadout.textContent = "Frame -- / --";
    elements.timeReadout.textContent = "00:00.000";
    elements.simStep.textContent = "--";
    elements.subtaskStep.textContent = "--";
    elements.actionStep.textContent = "--";
    elements.progressLabel.textContent = "0%";
    elements.runProgress.value = 0;
    highlightCurrentEvent();
    return;
  }

  if (seekVideos) {
    seekVideo(elements.cameraVideo, frame.cameraTime);
    seekVideo(elements.topdownVideo, frame.topdownTime);
  }
  elements.frameReadout.textContent = `Frame ${state.activeFrame + 1} / ${state.frames.length}`;
  elements.timeReadout.textContent = formatTime(frame.cameraTime);
  elements.simStep.textContent = scalar(frame.simStep, "--");
  elements.subtaskStep.textContent = scalar(frame.subtask, "--");
  const actionName = actionLabelForFrame(frame);
  const actionParts = [frame.actionIndex, actionName].filter((value) => value !== null && value !== undefined);
  elements.actionStep.textContent = actionParts.length ? actionParts.join("  |  ") : "--";
  const progress = state.frames.length <= 1 ? 100 : Math.round(state.activeFrame * 100 / maxIndex);
  elements.progressLabel.textContent = `${progress}%`;
  elements.runProgress.value = progress;
  elements.runProgress.textContent = `${progress}%`;
  highlightCurrentEvent();
}

function seekVideo(video, seconds) {
  if (!video.src || !Number.isFinite(Number(seconds))) {
    return;
  }
  let target = Math.max(Number(seconds), 0);
  if (Number.isFinite(video.duration) && video.duration > 0) {
    target = Math.min(target, Math.max(video.duration - 0.001, 0));
  }
  if (Math.abs(video.currentTime - target) > 0.025) {
    video.currentTime = target;
  }
}

function highlightCurrentEvent() {
  const frame = state.frames[state.activeFrame];
  const targetSeq = frame ? Number(frame.seq) : NaN;
  let current = null;
  for (const row of elements.timeline.querySelectorAll(".timeline-row")) {
    const rowSeq = Number(row.dataset.seq);
    const isCurrent = Number.isFinite(targetSeq) && rowSeq <= targetSeq;
    if (isCurrent && (!current || rowSeq >= Number(current.dataset.seq))) {
      current = row;
    }
    row.classList.remove("current");
  }
  if (current) {
    current.classList.add("current");
  }
}

async function togglePlayback() {
  if (state.playing) {
    stopPlayback();
    return;
  }
  const frame = state.frames[state.activeFrame];
  if (!frame) return;
  seekVideo(elements.cameraVideo, frame.cameraTime);
  seekVideo(elements.topdownVideo, frame.topdownTime);
  const playable = [elements.cameraVideo, elements.topdownVideo].filter((video) => video.src);
  if (!playable.length) return;
  state.playing = true;
  elements.playButton.firstElementChild.textContent = "\u275a\u275a";
  elements.playButton.title = "Pause synchronized replay";
  await Promise.allSettled(playable.map((video) => video.play()));
  playbackTick();
}

function stopPlayback() {
  state.playing = false;
  for (const video of [elements.cameraVideo, elements.topdownVideo]) {
    video.pause();
  }
  if (state.animationFrame !== null) {
    cancelAnimationFrame(state.animationFrame);
    state.animationFrame = null;
  }
  elements.playButton.firstElementChild.textContent = "\u25b6";
  elements.playButton.title = "Play synchronized replay";
}

function playbackTick() {
  if (!state.playing) return;
  const master = elements.cameraVideo.src ? elements.cameraVideo : elements.topdownVideo;
  if (master.ended) {
    stopPlayback();
    return;
  }
  const useCamera = master === elements.cameraVideo;
  const currentTime = master.currentTime;
  let bestIndex = state.activeFrame;
  for (let index = state.activeFrame; index < state.frames.length; index += 1) {
    const time = useCamera ? state.frames[index].cameraTime : state.frames[index].topdownTime;
    if (time <= currentTime + 0.02) {
      bestIndex = index;
    } else {
      break;
    }
  }
  if (bestIndex !== state.activeFrame) {
    updateFrame(bestIndex, false);
  }
  const frame = state.frames[bestIndex];
  const follower = useCamera ? elements.topdownVideo : elements.cameraVideo;
  const followerTime = useCamera ? frame.topdownTime : frame.cameraTime;
  if (follower.src && Math.abs(follower.currentTime - followerTime) > 0.12) {
    seekVideo(follower, followerTime);
  }
  state.animationFrame = requestAnimationFrame(playbackTick);
}

function showDetails(component, title, metadata, value) {
  state.detailValue = value;
  elements.detailComponent.textContent = String(component || "DETAIL").toUpperCase();
  elements.detailTitle.textContent = title || "Details";
  elements.detailMeta.replaceChildren();
  for (const [key, item] of Object.entries(metadata || {})) {
    const term = document.createElement("dt");
    term.textContent = key;
    const description = document.createElement("dd");
    description.textContent = String(item);
    elements.detailMeta.append(term, description);
  }
  let text;
  try {
    text = JSON.stringify(value, null, 2);
  } catch (_error) {
    text = String(value);
  }
  elements.detailJson.textContent = text || "{}";
  elements.copyDetail.disabled = value === null || value === undefined;
}

function showError(message) {
  elements.loadError.textContent = message;
  elements.loadError.hidden = false;
}

elements.refreshButton.addEventListener("click", () => loadRuns(state.activeRun && state.activeRun.id));
elements.runFilter.addEventListener("input", renderRuns);
elements.componentFilter.addEventListener("change", renderTimeline);
elements.frameSlider.addEventListener("input", (event) => {
  stopPlayback();
  updateFrame(Number(event.target.value), true);
});
elements.playButton.addEventListener("click", togglePlayback);
elements.copyDetail.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(elements.detailJson.textContent);
    elements.copyDetail.title = "Copied";
    setTimeout(() => { elements.copyDetail.title = "Copy JSON"; }, 1200);
  } catch (error) {
    showError(`Copy failed: ${error.message}`);
  }
});
elements.cameraVideo.addEventListener("ended", stopPlayback);
elements.topdownVideo.addEventListener("ended", () => {
  if (!elements.cameraVideo.src) stopPlayback();
});

const preferredRun = window.location.hash.slice(1);
loadRuns(preferredRun || null);
