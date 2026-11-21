/* ==========================================================================
   Clip Studio — front-end logic
   Vanilla JS, IIFE, no globals. Guards every DOM access so it also loads
   safely on /help where most elements do not exist.
   ========================================================================== */
(function () {
  "use strict";

  var THEME_KEY = "clipper-theme";
  var POLL_MS = 1800;

  /* ---------- Tiny DOM helpers ---------- */
  function $(id) { return document.getElementById(id); }
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function clampInt(el, fallback, min, max) {
    var n = parseInt(el && el.value, 10);
    if (isNaN(n)) return fallback;
    return Math.max(min, Math.min(max, n));
  }

  function formatSeconds(seconds) {
    var s = Number(seconds);
    if (!isFinite(s) || isNaN(s)) return "00:00";
    s = Math.max(0, Math.floor(s));
    var m = Math.floor(s / 60);
    var sec = s % 60;
    return m + ":" + (sec < 10 ? "0" : "") + sec;
  }

  /* ---------- Toasts ---------- */
  var ICONS = {
    success: '<svg class="toast__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>',
    error: '<svg class="toast__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>',
    info: '<svg class="toast__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>'
  };

  function toastStack() {
    var stack = $("toast-stack");
    if (stack) return stack;
    stack = document.createElement("div");
    stack.id = "toast-stack";
    stack.className = "toast-stack";
    stack.setAttribute("role", "region");
    stack.setAttribute("aria-label", "Notifications");
    document.body.appendChild(stack);
    return stack;
  }

  function dismissToast(el) {
    if (!el || el.classList.contains("is-leaving")) return;
    el.classList.add("is-leaving");
    var remove = function () { if (el.parentNode) el.parentNode.removeChild(el); };
    el.addEventListener("animationend", remove, { once: true });
    setTimeout(remove, 600);
  }

  function toast(msg, type) {
    type = type || "info";
    var stack = toastStack();
    var el = document.createElement("div");
    el.className = "toast toast--" + type;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    el.innerHTML = (ICONS[type] || ICONS.info) + '<span class="toast__msg"></span>';
    qs(".toast__msg", el).textContent = msg;
    stack.appendChild(el);
    setTimeout(function () { dismissToast(el); }, 4200);
  }

  /* ---------- Theme ---------- */
  function preferredTheme() {
    try {
      var saved = localStorage.getItem(THEME_KEY);
      if (saved === "light" || saved === "dark") return saved;
    } catch (e) { /* storage blocked */ }
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) return "light";
    return "dark";
  }

  function setTheme(theme, persist) {
    document.documentElement.setAttribute("data-theme", theme);
    try { if (persist) localStorage.setItem(THEME_KEY, theme); } catch (e) { /* ignore */ }
    var btn = $("theme-toggle");
    if (btn) {
      var label = theme === "light" ? "Switch to dark theme" : "Switch to light theme";
      btn.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
      btn.setAttribute("aria-label", label);
      btn.setAttribute("title", label);
    }
  }

  function initTheme() {
    var current = document.documentElement.getAttribute("data-theme") || preferredTheme();
    setTheme(current, false);
    var btn = $("theme-toggle");
    if (btn) {
      btn.addEventListener("click", function () {
        var now = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
        setTheme(now, true);
      });
    }
  }

  /* ---------- YouTube URL validation ---------- */
  function normalizeUrl(url) {
    url = (url || "").trim();
    if (!/^https?:\/\//i.test(url)) return "https://" + url;
    return url;
  }

  function isValidYouTubeUrl(url) {
    if (!url || !url.trim()) return false;
    try {
      var u = new URL(normalizeUrl(url));
      var host = u.hostname.toLowerCase().replace(/^www\./, "");
      var okHost =
        host === "youtube.com" || host === "m.youtube.com" ||
        host === "music.youtube.com" || host === "youtu.be" ||
        host === "youtube-nocookie.com";
      if (!okHost) return false;

      var idRe = /^[A-Za-z0-9_-]{11}$/;

      if (host === "youtu.be") {
        var sid = u.pathname.split("/").filter(Boolean)[0] || "";
        return idRe.test(sid);
      }
      if (u.pathname === "/watch") {
        var v = u.searchParams.get("v");
        return !!v && idRe.test(v);
      }
      if (u.pathname.indexOf("/shorts/") === 0 ||
          u.pathname.indexOf("/embed/") === 0 ||
          u.pathname.indexOf("/live/") === 0) {
        var mid = u.pathname.split("/")[2] || "";
        return idRe.test(mid);
      }
      return false;
    } catch (e) {
      return false;
    }
  }

  /* ---------- State ---------- */
var currentJobId = null;
  var pollTimer = null;
  var currentDownloadUrl = "";
  var currentJobFilename = "";
  var uploadPollTimer = null;
  var stageFloor = 0;
  var youtubeConnected = false;

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  /* ---------- Pipeline ---------- */
  var STAGE_COUNT = 6; // Queued, Parsing, Downloading, Heatmap, Rendering, Complete

  function stageFromStatus(status) {
    var s = (status || "").toLowerCase();
    if (s === "complete") return STAGE_COUNT; // all done
    if (s.indexOf("queued") !== -1) return 0;
    if (s.indexOf("pars") !== -1 || s.indexOf("url") !== -1) return 1;
    if (s.indexOf("download") !== -1 || s.indexOf("duration") !== -1) return 2;
    if (s.indexOf("heatmap") !== -1 || s.indexOf("audio") !== -1) return 3;
    if (s.indexOf("render") !== -1 || s.indexOf("encod") !== -1) return 4;
    return null;
  }

  function stageFromProgress(p) {
    if (p >= 90) return 5;
    if (p >= 55) return 4;
    if (p >= 30) return 3;
    if (p >= 15) return 2;
    if (p >= 5) return 1;
    return 0;
  }

  function setPipeline(stage, isError) {
    var steps = qsa(".pipeline__step");
    for (var i = 0; i < steps.length; i++) {
      var el = steps[i];
      var idx = parseInt(el.getAttribute("data-stage"), 10);
      el.classList.remove("is-done", "is-active", "is-error");
      if (isError) {
        if (idx === stage) el.classList.add("is-error");
        else if (idx < stage) el.classList.add("is-done");
      } else if (stage >= steps.length) {
        el.classList.add("is-done");
      } else if (idx < stage) {
        el.classList.add("is-done");
      } else if (idx === stage) {
        el.classList.add("is-active");
      }
    }
  }

  /* ---------- Progress ---------- */
  function setProgress(pct, state) {
    pct = Math.max(0, Math.min(100, Math.round(pct || 0)));
    var wrap = qs(".progress");
    if (wrap) {
      wrap.style.setProperty("--progress", pct + "%");
      wrap.classList.toggle("is-active", state === "active");
    }
    var fill = $("progress-fill");
    if (fill) {
      fill.classList.toggle("is-complete", state === "complete");
      fill.classList.toggle("is-error", state === "error");
    }
    var bar = qs(".progress-bar");
    if (bar) bar.setAttribute("aria-valuenow", String(pct));
  }

  function isErrorStatus(status) {
    if (status === "Error") return true;
    return /error|fail/i.test(status || "");
  }

  function isCompleteStatus(status) {
    return status === "Complete";
  }

  /* ---------- Render ---------- */
  function setStatusText(text) {
    var el = $("status-text");
    if (el) el.textContent = text;
  }

  function setStatusDot(kind) { // running | success | error
    var dot = $("status-dot");
    if (dot) dot.className = "status-dot is-" + kind;
  }

  function showStatusPanel() {
    var panel = $("status-panel");
    if (panel) panel.hidden = false;
  }

  function hideStatusPanel() {
    var panel = $("status-panel");
    if (panel) panel.hidden = true;
  }

  function setStatusPct(pct) {
    var el = qs(".status-pct");
    if (el) el.textContent = Math.max(0, Math.min(100, Math.round(pct || 0))) + "%";
  }

function showResult(result, jobId) {
    if (!result) return;
    currentDownloadUrl = "/download/" + jobId + "/" + result.filename;
    currentJobFilename = result.filename || "";

    var area = $("result-area");
    if (area) area.hidden = false;
    var errArea = $("error-area");
    if (errArea) errArea.hidden = true;

    var video = $("preview-video");
    var source = video ? qs("source", video) : null;
    if (source) {
      source.setAttribute("src", currentDownloadUrl);
      if (video) {
        try { video.load(); } catch (e) { /* ignore */ }
      }
    }

    var dl = $("download-link");
    if (dl) {
      dl.setAttribute("href", currentDownloadUrl);
      dl.setAttribute("download", result.filename || "");
    }
    var openLink = $("open-link");
    if (openLink) openLink.setAttribute("href", currentDownloadUrl);

    var dur = $("clip-duration");
    if (dur) dur.textContent = formatSeconds(result.clip_duration);

    var title = $("result-title");
    if (title) title.textContent = result.video_title || "Generated clip";

var fname = $("result-filename");
    if (fname) fname.textContent = result.filename || "";

    resetUploadUI(result);
  }

  function showError(msg) {
    var errArea = $("error-area");
    if (errArea) {
      errArea.hidden = false;
      var msgEl = qs(".error-card__msg", errArea);
      if (msgEl) msgEl.textContent = msg;
      else errArea.textContent = msg;
    }
    var area = $("result-area");
    if (area) area.hidden = true;
  }

  function renderStatus(data, jobId) {
    var status = data.status || "";
    var message = data.message || "";
    var progress = (typeof data.progress === "number") ? data.progress : null;
    var complete = isCompleteStatus(status);
    var errored = isErrorStatus(status);

    showStatusPanel();
    setStatusText(message || status || "Processing\u2026");
    setStatusDot(errored ? "error" : complete ? "success" : "running");

    if (complete) {
      setProgress(100, "complete");
      setStatusPct(100);
    } else if (progress !== null) {
      setProgress(progress, errored ? "error" : "active");
      setStatusPct(progress);
    }

    var stage = stageFromStatus(status);
    if (stage === null) {
      stage = complete ? STAGE_COUNT : stageFromProgress(progress !== null ? progress : 0);
    }
    if (stage < stageFloor) stage = stageFloor;
    stageFloor = stage;
    setPipeline(stage, errored);

    if (complete) {
      if (data.result) showResult(data.result, jobId);
      stopPolling();
      loadJobs();
    } else if (errored) {
      showError(message || "The job failed.");
      stopPolling();
      loadJobs();
    }
  }

function resetView() {
    stopPolling();
    stopUploadPolling();
    currentJobId = null;
    currentDownloadUrl = "";
    currentJobFilename = "";
    stageFloor = 0;
    hideStatusPanel();
    var area = $("result-area");
    if (area) area.hidden = true;
    var uploadPanel = $("upload-panel");
    if (uploadPanel) uploadPanel.hidden = true;
    var errArea = $("error-area");
    if (errArea) errArea.hidden = true;
    setProgress(0, "idle");
    setStatusPct(0);
    setPipeline(-1, false);
    var video = $("preview-video");
    if (video) {
      var source = qs("source", video);
      if (source) source.removeAttribute("src");
      try { video.load(); } catch (e) { /* ignore */ }
    }
  }

  /* ---------- Polling ---------- */
  function pollStatus(jobId) {
    var url = "/status/" + encodeURIComponent(jobId);
    return fetch(url, { cache: "no-store" })
      .then(function (res) {
        if (res.status === 404) {
          return res.json().then(
            function (body) { return { notFound: true, body: body }; },
            function () { return { notFound: true, body: null }; }
          );
        }
        return res.json().then(
          function (body) { return { notFound: false, body: body }; },
          function () { return { notFound: false, body: null }; }
        );
      })
      .then(function (r) {
        if (r.notFound) {
          var m = (r.body && r.body.message) || "Job not found or expired.";
          showError(m);
          toast(m, "error");
          stopPolling();
          return;
        }
        if (!r.body) {
          toast("Invalid response from the server.", "error");
          stopPolling();
          return;
        }
        renderStatus(r.body, jobId);
      })
      .catch(function () {
        toast("Lost connection to the server.", "error");
        showError("Lost connection to the server. Please try again.");
        stopPolling();
      });
  }

  function startPolling(jobId) {
    stopPolling();
    resetView();
    currentJobId = jobId;
    showStatusPanel();
    setStatusText("Queued\u2026");
    setStatusDot("running");
    setPipeline(0, false);
    pollStatus(jobId);
    pollTimer = setInterval(function () { pollStatus(jobId); }, POLL_MS);
  }

  /* ---------- URL field UI ---------- */
  function markUrlInvalid(msg) {
    var input = $("youtube_url");
    if (input) input.classList.add("is-invalid");
    var err = $("url-error");
    if (err) {
      err.hidden = false;
      var txt = qs(".field__error-text", err);
      if (txt) txt.textContent = msg;
    }
  }

  function clearUrlError() {
    var input = $("youtube_url");
    if (input) input.classList.remove("is-invalid");
    var err = $("url-error");
    if (err) err.hidden = true;
  }

  function initUrlField() {
    var input = $("youtube_url");
    if (input) {
      input.addEventListener("input", function () {
        var val = input.value.trim();
        if (!val) { clearUrlError(); input.classList.remove("is-valid"); return; }
        if (isValidYouTubeUrl(val)) {
          clearUrlError();
          input.classList.add("is-valid");
          input.classList.remove("is-invalid");
        } else {
          input.classList.remove("is-valid");
        }
      });
    }

    var pasteBtn = $("paste-btn");
    if (pasteBtn) {
      pasteBtn.addEventListener("click", function () {
        if (!navigator.clipboard || !navigator.clipboard.readText) {
          toast("Clipboard access is not available in this browser.", "error");
          return;
        }
        navigator.clipboard.readText().then(function (text) {
          if (input && text) {
            input.value = text.trim();
            input.dispatchEvent(new Event("input", { bubbles: true }));
            toast("Pasted from clipboard", "success");
          }
        }).catch(function () {
          toast("Could not read the clipboard.", "error");
        });
      });
    }
  }

  /* ---------- Sliders ---------- */
  function bindRange(input, readout) {
    if (!input) return;
    var update = function () {
      var v = input.value;
      var min = parseFloat(input.min) || 0;
      var max = parseFloat(input.max) || 100;
      var pct = max > min ? ((v - min) / (max - min)) * 100 : 0;
      input.style.setProperty("--fill", pct + "%");
      if (readout) readout.textContent = v;
    };
    input.addEventListener("input", update);
    update();
  }

  function initRanges() {
    bindRange($("clip_duration"), $("duration-readout"));
    bindRange($("context_before"), $("context-readout"));
  }

  /* ---------- Clipboard copy ---------- */
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        resolve();
      } catch (e) {
        reject(e);
      }
    });
  }

  function initCopy() {
    var btn = $("copy-link");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (!currentDownloadUrl) { toast("There is no clip to copy yet.", "info"); return; }
      var abs = window.location.origin + currentDownloadUrl;
      copyText(abs)
        .then(function () { toast("Link copied to clipboard", "success"); })
        .catch(function () { toast("Could not copy the link.", "error"); });
    });
  }

  /* ---------- Form ---------- */
  function initForm() {
    var form = $("generate-form");
    if (!form) return;

    form.addEventListener("submit", function (e) {
      e.preventDefault();

      var urlInput = $("youtube_url");
      var url = urlInput ? urlInput.value.trim() : "";

      if (!isValidYouTubeUrl(url)) {
        markUrlInvalid("Enter a valid YouTube URL, e.g. https://youtu.be/VIDEO_ID");
        toast("Please enter a valid YouTube URL.", "error");
        if (urlInput) urlInput.focus();
        return;
      }
      clearUrlError();

      var duration = clampInt($("clip_duration"), 45, 10, 300);
      var context = clampInt($("context_before"), 10, 0, 60);

      var btn = $("generate-btn");
      if (btn) { btn.disabled = true; btn.classList.add("is-loading"); }

      resetView();

      fetch("/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          youtube_url: url,
          clip_duration: duration,
          context_before: context
        })
      })
        .then(function (res) {
          return res.json().then(
            function (body) { return { ok: res.ok, body: body }; },
            function () { return { ok: res.ok, body: null }; }
          );
        })
        .then(function (r) {
          if (btn) { btn.disabled = false; btn.classList.remove("is-loading"); }

          if (!r.ok || !r.body || r.body.error) {
            var msg = (r.body && r.body.error) || "Could not start the job.";
            showError(msg);
            toast(msg, "error");
            setPipeline(0, true);
            setStatusDot("error");
            showStatusPanel();
            setStatusText(msg);
            return;
          }
          if (!r.body.job_id) {
            toast("Unexpected server response.", "error");
            return;
          }
          startPolling(r.body.job_id);
        })
        .catch(function () {
          if (btn) { btn.disabled = false; btn.classList.remove("is-loading"); }
          var msg = "Network error \u2014 could not reach the server.";
          showError(msg);
          toast(msg, "error");
        });
    });

    var clearBtn = $("clear-btn");
    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        resetView();
        clearUrlError();
        var urlInput = $("youtube_url");
        if (urlInput) {
          urlInput.value = "";
          urlInput.classList.remove("is-valid");
          urlInput.focus();
        }
      });
    }
  }

  /* ---------- Jobs list ---------- */
  function jobKind(status) {
    if (status === "Complete") return "complete";
    if (isErrorStatus(status)) return "error";
    return "running";
  }

  var EMPTY_JOBS_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>';

  function emptyJobsState(list) {
    list.innerHTML = "";
    var wrap = document.createElement("div");
    wrap.className = "no-jobs";
    wrap.innerHTML = EMPTY_JOBS_ICON + "<div>No jobs yet. Your generated clips will appear here.</div>";
    list.appendChild(wrap);
  }

  function selectJob(jobId) {
    if (!jobId) return;
    startPolling(jobId);
  }

  function renderJobs(list, jobs) {
    list.innerHTML = "";
    var ids = Object.keys(jobs || {});
    if (!ids.length) { emptyJobsState(list); return; }

    ids.forEach(function (jobId) {
      var meta = jobs[jobId] || {};
      var kind = jobKind(meta.status);

      var row = document.createElement("button");
      row.type = "button";
      row.className = "job";
      row.setAttribute("data-jobid", jobId);
      row.setAttribute("aria-label", "View job " + jobId.slice(0, 8) + " \u2014 " + (meta.status || "unknown"));

      var dot = document.createElement("span");
      dot.className = "job__dot job__dot--" + kind;
      dot.setAttribute("aria-hidden", "true");

      var main = document.createElement("span");
      main.className = "job__main";

      var idEl = document.createElement("span");
      idEl.className = "job__id";
      idEl.textContent = "job " + jobId.slice(0, 8);

      var msgEl = document.createElement("span");
      msgEl.className = "job__msg";
      msgEl.textContent = meta.message || meta.status || "";

      main.appendChild(idEl);
      main.appendChild(msgEl);

      var badge = document.createElement("span");
      badge.className = "job__badge job__badge--" + kind;
      badge.textContent = meta.status || "\u2014";

      row.appendChild(dot);
      row.appendChild(main);
      row.appendChild(badge);
      row.addEventListener("click", function () { selectJob(jobId); });

      list.appendChild(row);
    });
  }

  function loadJobs() {
    var list = $("jobs-list");
    if (!list) return;
    fetch("/jobs", { cache: "no-store" })
      .then(function (res) { return res.json().catch(function () { return null; }); })
      .then(function (data) {
        if (!data || !data.jobs) { emptyJobsState(list); return; }
        renderJobs(list, data.jobs);
      })
      .catch(function () { /* stay silent on background load */ });
  }

  /* ---------- YouTube connection ---------- */
  function setLockedLink(docMode) {
    var locked = $("yt-upload-locked");
    if (!locked) return;
    var link = qs("a", locked);
    if (!link) return;
    if (docMode) {
      link.setAttribute("href", "/help#youtube-setup");
      link.textContent = "Set up YouTube";
    } else {
      link.setAttribute("href", "/youtube/connect");
      link.textContent = "Connect YouTube";
    }
  }

  function renderYouTubeConnection(data) {
    data = data || {};
    youtubeConnected = !!data.connected;

    var nav = $("yt-nav");
    var connectBtn = $("yt-connect");
    var connectLabel = $("yt-connect-label");
    var connectedBox = $("yt-connected");
    var channelEl = $("yt-channel");
    var fields = $("yt-upload-fields");
    var locked = $("yt-upload-locked");

    if (nav) nav.hidden = false;

    if (!data.configured) {
      if (connectBtn) { connectBtn.hidden = false; connectBtn.setAttribute("href", "/help#youtube-setup"); }
      if (connectLabel) connectLabel.textContent = "Set up YouTube";
      if (connectedBox) connectedBox.hidden = true;
      if (fields) fields.hidden = true;
      if (locked) locked.hidden = false;
      setLockedLink(true);
      return;
    }

    if (data.connected) {
      if (connectBtn) connectBtn.hidden = true;
      if (connectedBox) connectedBox.hidden = false;
      if (channelEl) {
        channelEl.textContent =
          (data.channel && (data.channel.title || data.channel.custom_url)) || "Connected";
      }
      if (fields) fields.hidden = false;
      if (locked) locked.hidden = true;
    } else {
      if (connectBtn) { connectBtn.hidden = false; connectBtn.setAttribute("href", "/youtube/connect"); }
      if (connectLabel) connectLabel.textContent = "Connect YouTube";
      if (connectedBox) connectedBox.hidden = true;
      if (fields) fields.hidden = true;
      if (locked) locked.hidden = false;
      setLockedLink(false);
    }
  }

  function refreshYouTubeStatus() {
    return fetch("/youtube/status", { cache: "no-store" })
      .then(function (res) { return res.json().catch(function () { return null; }); })
      .then(function (data) { renderYouTubeConnection(data); return data; })
      .catch(function () { /* stay silent on background load */ });
  }

  function initYouTube() {
    refreshYouTubeStatus();
    var disconnectBtn = $("yt-disconnect");
    if (disconnectBtn) {
      disconnectBtn.addEventListener("click", function () {
        disconnectBtn.disabled = true;
        fetch("/youtube/disconnect", { method: "POST" })
          .then(function (res) { return res.json().catch(function () { return null; }); })
          .then(function () {
            toast("Disconnected from YouTube", "success");
            return refreshYouTubeStatus();
          })
          .catch(function () { toast("Could not disconnect.", "error"); })
          .then(function () { disconnectBtn.disabled = false; });
      });
    }
  }

  function consumeYouTubeRedirect() {
    var params;
    try { params = new URLSearchParams(window.location.search); } catch (e) { return; }
    var connected = params.get("youtube");
    var err = params.get("youtube_error");
    if (!connected && !err) return;

    try {
      params.delete("youtube");
      params.delete("youtube_error");
      var rest = params.toString();
      window.history.replaceState({}, "", window.location.pathname + (rest ? "?" + rest : ""));
    } catch (e) { /* ignore */ }

    if (connected === "connected") {
      toast("YouTube account connected", "success");
      refreshYouTubeStatus();
      return;
    }
    if (err) {
      var messages = {
        access_denied: "YouTube connection was cancelled.",
        not_configured: "Add your Google client_secret.json first (see Docs).",
        not_installed: "Google API libraries are missing. Run: pip install -r requirements.txt",
        invalid_state: "The sign-in expired or was invalid. Please try again.",
        token_exchange_failed: "Could not complete the Google sign-in. Please try again.",
        flow_init_failed: "Could not start the sign-in. Check your client_secret.json.",
        bad_client_secret: "client_secret.json could not be read. Check the file."
      };
      toast(messages[err] || "Could not connect to YouTube.", "error");
    }
  }

  /* ---------- YouTube upload ---------- */
  function setUploadStatus(text, isError) {
    var el = $("yt-upload-status");
    if (!el) return;
    el.hidden = !text;
    el.textContent = text || "";
    el.classList.toggle("is-error", !!isError);
  }

  function setUploadButtonBusy(busy, keepDisabled) {
    var btn = $("yt-upload-btn");
    if (!btn) return;
    btn.classList.toggle("is-loading", !!busy);
    if (busy) btn.disabled = true;
    else if (!keepDisabled) btn.disabled = false;
  }

  function uploadErrorMessage(code) {
    var map = {
      not_connected: "Connect YouTube first.",
      not_configured: "Set up YouTube first (see Docs).",
      file_missing: "The clip file could not be found.",
      invalid_file: "Only clips generated by this app can be uploaded.",
      confirm_required: "Please confirm you have the rights to upload.",
      title_required: "A title is required.",
      title_too_long: "Title must be 100 characters or fewer.",
      description_too_long: "Description must be 5000 characters or fewer.",
      upload_failed: "Upload failed. Check your connection and try again."
    };
    return map[code] || "Upload failed. Please try again.";
  }

  function resetUploadUI(result) {
    result = result || {};
    setUploadButtonBusy(false);
    setUploadStatus("", false);
    var resultEl = $("yt-upload-result");
    if (resultEl) resultEl.hidden = true;
    var link = $("yt-video-link");
    if (link) link.setAttribute("href", "#");
    var conf = $("yt-confirm");
    if (conf) conf.checked = false;
    var privacy = $("yt-privacy");
    if (privacy) privacy.value = "private";
    var title = $("yt-title");
    if (title) title.value = result.video_title || "";
    var desc = $("yt-description");
    if (desc) desc.value = result.video_title ? (result.video_title + "\n\n#shorts") : "#shorts";
    var panel = $("upload-panel");
    if (panel) panel.hidden = false;
  }

  function stopUploadPolling() {
    if (uploadPollTimer) { clearInterval(uploadPollTimer); uploadPollTimer = null; }
  }

  function startUploadPolling(jobId) {
    stopUploadPolling();
    var url = "/youtube/upload/status/" + encodeURIComponent(jobId);

    var tick = function () {
      fetch(url, { cache: "no-store" })
        .then(function (res) { return res.json().catch(function () { return null; }); })
        .then(function (data) {
          if (!data || !data.status || data.status === "None") {
            stopUploadPolling();
            setUploadStatus("Upload status unavailable. Check your channel.", true);
            setUploadButtonBusy(false);
            return;
          }
          var status = data.status;
          var pct = typeof data.progress === "number" ? data.progress : 0;

          if (status === "Complete") {
            stopUploadPolling();
            setUploadStatus("Uploaded successfully.", false);
            var res = $("yt-upload-result");
            if (res) res.hidden = false;
            var link = $("yt-video-link");
            if (link) { link.setAttribute("href", data.url || "#"); }
            setUploadButtonBusy(false, true); // keep disabled to prevent duplicates
            toast("Clip uploaded to YouTube", "success");
          } else if (status === "Error") {
            stopUploadPolling();
            setUploadStatus(data.error ? uploadErrorMessage(data.error) : "Upload failed.", true);
            setUploadButtonBusy(false);
            toast("Upload failed", "error");
          } else {
            setUploadStatus("Uploading\u2026 " + pct + "%", false);
          }
        })
        .catch(function () { /* transient poll error; keep trying */ });
    };

    tick();
    uploadPollTimer = setInterval(tick, 1500);
  }

  function initUpload() {
    var btn = $("yt-upload-btn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var confirmBox = $("yt-confirm");
      if (!confirmBox || !confirmBox.checked) {
        toast("Please confirm you have the rights to upload this clip.", "error");
        if (confirmBox) confirmBox.focus();
        return;
      }
      var title = ($("yt-title") || {}).value || "";
      title = title.trim();
      if (!title) { toast("A title is required.", "error"); return; }
      var desc = ($("yt-description") || {}).value || "";
      var privacy = ($("yt-privacy") || {}).value || "private";
      var jobId = currentJobId;
      var filename = currentJobFilename;
      if (!jobId || !filename) { toast("No clip selected yet.", "info"); return; }

      setUploadButtonBusy(true);
      setUploadStatus("Starting upload\u2026", false);

      fetch("/youtube/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: jobId,
          filename: filename,
          title: title,
          description: desc,
          privacy: privacy,
          confirm: true
        })
      })
        .then(function (res) {
          return res.json().then(
            function (body) { return { ok: res.ok, body: body }; },
            function () { return { ok: res.ok, body: null }; }
          );
        })
        .then(function (r) {
          if (!r.ok || !r.body || r.body.error) {
            var code = (r.body && r.body.error) || "upload_failed";
            setUploadStatus(uploadErrorMessage(code), true);
            setUploadButtonBusy(false);
            toast(uploadErrorMessage(code), "error");
            return;
          }
          if (r.body.duplicate) {
            setUploadStatus("This clip is already uploaded or uploading.", false);
            var resEl = $("yt-upload-result");
            if (resEl) resEl.hidden = false;
            var link = $("yt-video-link");
            if (link) link.setAttribute("href", r.body.url || "#");
            setUploadButtonBusy(false, true);
            return;
          }
          setUploadStatus("Uploading\u2026 0%", false);
          startUploadPolling(jobId);
        })
        .catch(function () {
          setUploadStatus("Network error \u2014 could not reach the server.", true);
          setUploadButtonBusy(false);
          toast("Network error \u2014 could not reach the server.", "error");
        });
    });
  }

  /* ---------- Boot ---------- */
function init() {
    initTheme();
    initUrlField();
    initRanges();
    initCopy();
    initForm();
    initYouTube();
    initUpload();
    consumeYouTubeRedirect();
    loadJobs();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
