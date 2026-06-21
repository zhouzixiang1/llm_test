/* ── 状态标签映射 ─────────────────────────────────────────────────────── */
const STATUS_LABELS = {
  pending: "等待中",
  generating_prompts: "生成提示词",
  generating_image: "生成图片",
  generating_video_prompt: "生成视频提示词",
  generating_video: "生成视频",
  completed: "已完成",
};

const STEP_LABELS = {
  pending: "等待中",
  generating_prompts: "生成提示词",
  generating_image: "生成图片",
  generating_video_prompt: "生成视频提示词",
  generating_video: "生成视频",
  failed: "失败",
};

let currentPage = 1;
const PAGE_SIZE = 15;

/* ── DOM 引用 ────────────────────────────────────────────────────────── */
const itemList = document.getElementById("itemList");
const logList = document.getElementById("logList");
const logCount = document.getElementById("logCount");
const statusBar = document.getElementById("statusBar");
const statusText = document.getElementById("statusText");
const themeInput = document.getElementById("themeInput");
const searchInput = document.getElementById("searchInput");
const statusFilter = document.getElementById("statusFilter");
const sseDot = document.getElementById("sseDot");
const lastErrorEl = document.getElementById("lastError");
const imageSizeInput = document.getElementById("imageSizeInput");
const videoWidthInput = document.getElementById("videoWidthInput");
const videoHeightInput = document.getElementById("videoHeightInput");
const videoNumFramesInput = document.getElementById("videoNumFramesInput");
const videoFrameRateInput = document.getElementById("videoFrameRateInput");
const batchLimitInput = document.getElementById("batchLimitInput");
const scheduleStartInput = document.getElementById("scheduleStartInput");
const scheduleEndInput = document.getElementById("scheduleEndInput");
const stylePreset = document.getElementById("stylePreset");
const variationMode = document.getElementById("variationMode");
const viewToggleBtn = document.getElementById("viewToggleBtn");
const themeToggleBtn = document.getElementById("themeToggleBtn");
const retentionDaysInput = document.getElementById("retentionDaysInput");
const cleanupBtn = document.getElementById("cleanupBtn");

/* ── Toast 通知（B1） ─────────────────────────────────────────────────── */
function showToast(message, type = "info", duration = 3000) {
  const container = document.getElementById("toastContainer");
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("show"));
  setTimeout(() => {
    toast.classList.remove("show");
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

/* ── 确认弹窗（B1 替代 confirm，D18 焦点陷阱） ───────────────────────── */
function showConfirm(message) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    overlay.innerHTML = `
      <div class="confirm-dialog" role="dialog" aria-modal="true">
        <p>${escapeHtml(message)}</p>
        <div class="confirm-buttons">
          <button class="btn-ghost confirm-cancel">取消</button>
          <button class="btn-danger confirm-ok">确定</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const cancelBtn = overlay.querySelector(".confirm-cancel");
    const okBtn = overlay.querySelector(".confirm-ok");
    const dialog = overlay.querySelector(".confirm-dialog");

    // D18: 焦点陷阱
    const focusableElements = [cancelBtn, okBtn];
    okBtn.focus();

    dialog.addEventListener("keydown", (e) => {
      if (e.key === "Tab") {
        e.preventDefault();
        const current = document.activeElement;
        const idx = focusableElements.indexOf(current);
        if (e.shiftKey) {
          focusableElements[(idx - 1 + focusableElements.length) % focusableElements.length].focus();
        } else {
          focusableElements[(idx + 1) % focusableElements.length].focus();
        }
      }
    });

    cancelBtn.onclick = () => { overlay.remove(); resolve(false); };
    okBtn.onclick = () => { overlay.remove(); resolve(true); };
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") { overlay.remove(); resolve(false); } });
  });
}

/* ── 按钮加载状态（B5） ──────────────────────────────────────────────── */
function withButtonLoading(btn, fn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = original + "...";
  return fn().finally(() => {
    btn.disabled = false;
    btn.textContent = original;
  });
}

/* ── 工具函数 ────────────────────────────────────────────────────────── */
function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}
function stepLabel(step) {
  return STEP_LABELS[step] || step;
}
function formatTime(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString("zh-CN"); } catch { return iso; }
}
function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

/* ── 视图切换（D6） ──────────────────────────────────────────────────── */
function applyViewMode(mode) {
  if (mode === "grid") {
    itemList.classList.add("grid-view");
    viewToggleBtn.textContent = "☰";
    viewToggleBtn.title = "列表视图";
  } else {
    itemList.classList.remove("grid-view");
    viewToggleBtn.textContent = "⊞";
    viewToggleBtn.title = "切换视图";
  }
}
viewToggleBtn.addEventListener("click", () => {
  const isGrid = itemList.classList.contains("grid-view");
  const newMode = isGrid ? "list" : "grid";
  applyViewMode(newMode);
  localStorage.setItem("agnes_view_mode", newMode);
});

/* ── 主题切换（D7） ──────────────────────────────────────────────────── */
function applyTheme(theme) {
  if (theme === "light") {
    document.documentElement.setAttribute("data-theme", "light");
    themeToggleBtn.textContent = "☀️";
    themeToggleBtn.title = "切换到深色主题";
  } else {
    document.documentElement.removeAttribute("data-theme");
    themeToggleBtn.textContent = "🌙";
    themeToggleBtn.title = "切换到浅色主题";
  }
}
themeToggleBtn.addEventListener("click", () => {
  const isLight = document.documentElement.getAttribute("data-theme") === "light";
  const newTheme = isLight ? "dark" : "light";
  applyTheme(newTheme);
  localStorage.setItem("agnes_theme", newTheme);
});

/* ── 持续时间计时器（D8） ─────────────────────────────────────────────── */
function formatElapsed(ms) {
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return totalSec + "s";
  const hours = Math.floor(totalSec / 3600);
  const minutes = Math.floor((totalSec % 3600) / 60);
  const secs = totalSec % 60;
  if (hours > 0) return hours + "h " + minutes + "m";
  return minutes + "m " + secs + "s";
}

function updateDurationTimers() {
  const timers = document.querySelectorAll(".duration-timer");
  const now = Date.now();
  timers.forEach((el) => {
    const started = el.dataset.started;
    if (!started) return;
    const startMs = new Date(started).getTime();
    if (isNaN(startMs)) return;
    el.textContent = formatElapsed(now - startMs);
  });
}
setInterval(updateDurationTimers, 5000);

/* ── 状态栏渲染（含 B8 错误展示） ────────────────────────────────────── */
function renderStatus(status) {
  const running = status.running ? "运行中" : "已停止";
  const stopHint = status.stop_after_item ? "（将在当前条目完成后停止）" : "";
  const logs = status.failed_log_count ?? 0;
  statusText.textContent = `${running}${stopHint} · 内容 ${status.total_count} 条 · 失败日志 ${logs} 条`;
  if (status.theme !== undefined) themeInput.value = status.theme;
  logCount.textContent = logs;

  // 同步高级设置字段（仅在用户未聚焦时更新，避免覆盖正在编辑的值）
  if (document.activeElement !== imageSizeInput && status.image_size) imageSizeInput.value = status.image_size;
  if (document.activeElement !== videoWidthInput && status.video_width) videoWidthInput.value = status.video_width;
  if (document.activeElement !== videoHeightInput && status.video_height) videoHeightInput.value = status.video_height;
  if (document.activeElement !== videoNumFramesInput && status.video_num_frames) videoNumFramesInput.value = status.video_num_frames;
  if (document.activeElement !== videoFrameRateInput && status.video_frame_rate) videoFrameRateInput.value = status.video_frame_rate;
  if (document.activeElement !== batchLimitInput && status.batch_limit !== undefined) batchLimitInput.value = status.batch_limit;
  if (document.activeElement !== scheduleStartInput && status.schedule_start !== undefined) scheduleStartInput.value = status.schedule_start;
  if (document.activeElement !== scheduleEndInput && status.schedule_end !== undefined) scheduleEndInput.value = status.schedule_end;
  if (document.activeElement !== stylePreset && status.style_preset !== undefined) stylePreset.value = status.style_preset;
  if (document.activeElement !== variationMode && status.variation_mode !== undefined) variationMode.value = status.variation_mode;
  if (document.activeElement !== retentionDaysInput && status.retention_days !== undefined) retentionDaysInput.value = status.retention_days;

  // B8: 流水线错误展示
  if (status.last_error) {
    lastErrorEl.textContent = `⚠ ${status.last_error}${status.last_error_time ? " (" + formatTime(status.last_error_time) + ")" : ""}`;
    lastErrorEl.style.display = "block";
  } else {
    lastErrorEl.style.display = "none";
  }
}

/* ── 卡片渲染（含 B3 进度条、B7 封面+下载、D8 持续时间） ──────────────── */
function renderCard(item) {
  const imageSrc = item.image_media || "";
  const videoSrc = item.video_media || item.video_url || "";

  // B3: 动画进度条替代文本
  const progress =
    item.status === "generating_video" && item.video_progress != null
      ? `<div class="progress-bar-container">
           <div class="progress-bar-fill" style="width: ${item.video_progress}%"></div>
           <span class="progress-bar-text">${item.video_progress}%</span>
         </div>`
      : "";

  const remixBtn =
    item.status === "completed"
      ? `<button type="button" class="btn-ghost btn-sm btn-remix-item" data-id="${item.id}">Remix</button>`
      : "";

  const deleteBtn =
    item.status === "completed"
      ? `<button type="button" class="btn-ghost btn-sm btn-delete-item" data-id="${item.id}">删除</button>`
      : "";

  // B7: 下载按钮
  const imageDownload = imageSrc
    ? `<a href="${imageSrc}" download="image-${item.seq}.png" class="btn-ghost btn-sm media-download">⬇ 下载图片</a>`
    : "";
  const videoDownload = videoSrc
    ? `<a href="${videoSrc}" download="video-${item.seq}.mp4" class="btn-ghost btn-sm media-download">⬇ 下载视频</a>`
    : "";

  // D8: 活跃项的持续时间显示
  const isActive = item.status !== "completed" && item.status !== "failed";
  const durationHtml = isActive && item.created_at
    ? `<span class="duration-timer" data-started="${item.created_at}">0s</span>`
    : "";

  // B7: 视频封面图（poster）
  return `
    <article class="card" data-id="${item.id}">
      <div class="card-header">
        <h2>#${item.seq} ${escapeHtml(item.title || "生成中...")}</h2>
        <span class="badge badge-${item.status}">${statusLabel(item.status)}</span>
        ${remixBtn}
        ${deleteBtn}
      </div>
      <div class="prompt-block">
        <strong>图片提示词</strong>
        ${escapeHtml(item.image_prompt || "—")}
      </div>
      <div class="prompt-block">
        <strong>视频提示词</strong>
        ${escapeHtml(item.video_prompt || "—")}
      </div>
      ${progress}
      <div class="media-row">
        <div class="media-box">
          ${imageSrc
            ? `<img src="${imageSrc}" alt="${escapeHtml(item.image_prompt || "生成的图片")}" />`
            : `<div class="placeholder">图片生成中...</div>`}
          ${imageDownload}
        </div>
        <div class="media-box">
          ${videoSrc
            ? `<video src="${videoSrc}" poster="${imageSrc}" controls playsinline></video>`
            : `<div class="placeholder">视频生成中...</div>`}
          ${videoDownload}
        </div>
      </div>
      <div class="card-footer">
        <span>${formatTime(item.created_at)}</span>
        ${durationHtml}
      </div>
    </article>`;
}

/* ── 日志行渲染 ──────────────────────────────────────────────────────── */
function renderLogRow(log) {
  const summary = (log.error || "").slice(0, 120);
  return `
    <details class="log-row" data-id="${log.id}">
      <summary>
        <span class="log-seq">#${log.seq}</span>
        <span class="log-time">${formatTime(log.created_at)}</span>
        <span class="log-step">${stepLabel(log.step)}</span>
        <span class="log-summary">${escapeHtml(summary)}${(log.error || "").length > 120 ? "…" : ""}</span>
        <button type="button" class="btn-ghost btn-sm btn-retry-log" data-id="${log.id}">重试</button>
        <button type="button" class="btn-ghost btn-sm btn-delete-log" data-id="${log.id}">删除</button>
      </summary>
      <div class="log-detail">
        ${log.title ? `<p><strong>标题</strong> ${escapeHtml(log.title)}</p>` : ""}
        ${log.theme ? `<p><strong>主题</strong> ${escapeHtml(log.theme)}</p>` : ""}
        ${log.image_prompt ? `<p><strong>图片提示词</strong> ${escapeHtml(log.image_prompt)}</p>` : ""}
        ${log.video_prompt ? `<p><strong>视频提示词</strong> ${escapeHtml(log.video_prompt)}</p>` : ""}
        <pre class="log-error">${escapeHtml(log.error || "")}</pre>
      </div>
    </details>`;
}

/* ── 精细化 DOM 更新（B2 消除闪烁） ───────────────────────────────────── */
function upsertItem(item) {
  const existing = document.querySelector(`#itemList [data-id="${item.id}"]`);
  if (existing) {
    // 标题
    const h2 = existing.querySelector(".card-header h2");
    if (h2) h2.textContent = `#${item.seq} ${item.title || "生成中..."}`;

    // 状态徽章
    const badge = existing.querySelector(".badge");
    if (badge) {
      badge.className = `badge badge-${item.status}`;
      badge.textContent = statusLabel(item.status);
    }

    // 提示词
    const prompts = existing.querySelectorAll(".prompt-block");
    if (prompts[0]) prompts[0].innerHTML = `<strong>图片提示词</strong>${escapeHtml(item.image_prompt || "—")}`;
    if (prompts[1]) prompts[1].innerHTML = `<strong>视频提示词</strong>${escapeHtml(item.video_prompt || "—")}`;

    // 进度条
    const oldProgress = existing.querySelector(".progress-bar-container");
    const needProgress = item.status === "generating_video" && item.video_progress != null;
    if (oldProgress && !needProgress) {
      oldProgress.remove();
    } else if (needProgress) {
      const newHtml = `<div class="progress-bar-container"><div class="progress-bar-fill" style="width: ${item.video_progress}%"></div><span class="progress-bar-text">${item.video_progress}%</span></div>`;
      if (oldProgress) {
        oldProgress.outerHTML = newHtml;
      } else {
        const mediaRow = existing.querySelector(".media-row");
        if (mediaRow) mediaRow.insertAdjacentHTML("beforebegin", newHtml);
      }
    }

    // 图片（仅 src 变化时更新）
    const imageSrc = item.image_media || "";
    const firstMediaBox = existing.querySelector(".media-box:first-child");
    const img = firstMediaBox ? firstMediaBox.querySelector("img") : null;
    if (imageSrc && !img) {
      if (firstMediaBox) firstMediaBox.innerHTML = `<img src="${imageSrc}" alt="${escapeHtml(item.image_prompt || "生成的图片")}" /><a href="${imageSrc}" download="image-${item.seq}.png" class="btn-ghost btn-sm media-download">⬇ 下载图片</a>`;
    } else if (imageSrc && img && !img.src.endsWith(imageSrc.replace(/^\//, ""))) {
      img.src = imageSrc;
    }

    // 视频（仅 src 变化时更新）
    const videoSrc = item.video_media || item.video_url || "";
    const lastMediaBox = existing.querySelector(".media-box:last-child");
    const video = lastMediaBox ? lastMediaBox.querySelector("video") : null;
    if (videoSrc && !video) {
      if (lastMediaBox) lastMediaBox.innerHTML = `<video src="${videoSrc}" poster="${imageSrc}" controls playsinline></video><a href="${videoSrc}" download="video-${item.seq}.mp4" class="btn-ghost btn-sm media-download">⬇ 下载视频</a>`;
    }

    // Remix + 删除按钮（仅完成时显示）
    const existingRemixBtn = existing.querySelector(".btn-remix-item");
    if (item.status === "completed" && !existingRemixBtn) {
      existing.querySelector(".card-header").insertAdjacentHTML("beforeend",
        `<button type="button" class="btn-ghost btn-sm btn-remix-item" data-id="${item.id}">Remix</button>`);
    }
    const existingDeleteBtn = existing.querySelector(".btn-delete-item");
    if (item.status === "completed" && !existingDeleteBtn) {
      existing.querySelector(".card-header").insertAdjacentHTML("beforeend",
        `<button type="button" class="btn-ghost btn-sm btn-delete-item" data-id="${item.id}">删除</button>`);
    }

    // D8: 更新持续时间计时器
    const footer = existing.querySelector(".card-footer");
    if (footer) {
      const isActive = item.status !== "completed" && item.status !== "failed";
      const timerEl = footer.querySelector(".duration-timer");
      if (isActive && item.created_at) {
        if (timerEl) {
          timerEl.dataset.started = item.created_at;
        } else {
          footer.insertAdjacentHTML("beforeend", `<span class="duration-timer" data-started="${item.created_at}">0s</span>`);
        }
      } else if (timerEl) {
        timerEl.remove();
      }
    }

    // 卡片闪烁提示
    existing.classList.add("highlight");
    setTimeout(() => existing.classList.remove("highlight"), 1500);
  } else {
    itemList.insertAdjacentHTML("afterbegin", renderCard(item));
  }
  const empty = itemList.querySelector(".empty");
  if (empty) empty.remove();
}

function removeItemCard(itemId) {
  const el = document.querySelector(`#itemList [data-id="${itemId}"]`);
  if (el) el.remove();
  if (!itemList.querySelector(".card")) {
    itemList.innerHTML = `<div class="empty">暂无记录，流水线启动后将逐条追加</div>`;
  }
}

function upsertLog(log) {
  const existing = document.querySelector(`#logList [data-id="${log.id}"]`);
  const html = renderLogRow(log);
  if (existing) {
    existing.outerHTML = html;
  } else {
    logList.insertAdjacentHTML("afterbegin", html);
  }
  const empty = logList.querySelector(".empty");
  if (empty) empty.remove();
}

function removeLogRow(logId) {
  const el = document.querySelector(`#logList [data-id="${logId}"]`);
  if (el) el.remove();
  if (!logList.querySelector(".log-row")) {
    logList.innerHTML = `<div class="empty">暂无失败日志</div>`;
  }
}

/* ── 数据加载 ────────────────────────────────────────────────────────── */
async function loadItems(page) {
  if (page !== undefined) currentPage = page;
  const params = new URLSearchParams();
  params.set("limit", PAGE_SIZE);
  params.set("offset", (currentPage - 1) * PAGE_SIZE);
  if (searchInput.value) params.set("search", searchInput.value);
  if (statusFilter.value) params.set("status", statusFilter.value);
  const resp = await fetch(`/api/items?${params.toString()}`);
  const data = await resp.json();
  const total = data.total || 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (currentPage > totalPages) currentPage = totalPages;
  if (!data.items.length) {
    itemList.innerHTML = `<div class="empty">暂无记录，流水线启动后将逐条追加</div>`;
  } else {
    itemList.innerHTML = data.items.map(renderCard).join("");
  }
  renderPagination(total, totalPages);
}

function renderPagination(total, totalPages) {
  let pg = document.getElementById("pagination");
  if (!pg) {
    pg = document.createElement("div");
    pg.id = "pagination";
    pg.className = "pagination";
    itemList.parentNode.insertBefore(pg, itemList.nextSibling);
  }
  if (totalPages <= 1) {
    pg.innerHTML = total > 0 ? `<span class="pagination-info">共 ${total} 条</span>` : "";
    return;
  }
  let html = `<span class="pagination-info">共 ${total} 条，第 ${currentPage}/${totalPages} 页</span><div class="pagination-buttons">`;
  html += `<button class="btn-ghost btn-sm" ${currentPage <= 1 ? "disabled" : ""} data-page="${currentPage - 1}">上一页</button>`;
  const maxButtons = 7;
  let startPage = Math.max(1, currentPage - 3);
  let endPage = Math.min(totalPages, startPage + maxButtons - 1);
  if (endPage - startPage < maxButtons - 1) startPage = Math.max(1, endPage - maxButtons + 1);
  if (startPage > 1) html += `<button class="btn-ghost btn-sm" data-page="1">1</button>`;
  if (startPage > 2) html += `<span class="pagination-ellipsis">…</span>`;
  for (let i = startPage; i <= endPage; i++) {
    if (i === currentPage) {
      html += `<button class="btn-primary btn-sm" disabled>${i}</button>`;
    } else {
      html += `<button class="btn-ghost btn-sm" data-page="${i}">${i}</button>`;
    }
  }
  if (endPage < totalPages - 1) html += `<span class="pagination-ellipsis">…</span>`;
  if (endPage < totalPages) html += `<button class="btn-ghost btn-sm" data-page="${totalPages}">${totalPages}</button>`;
  html += `<button class="btn-ghost btn-sm" ${currentPage >= totalPages ? "disabled" : ""} data-page="${currentPage + 1}">下一页</button>`;
  html += `</div>`;
  pg.innerHTML = html;
}

async function loadLogs() {
  const resp = await fetch("/api/logs");
  const data = await resp.json();
  if (!data.logs.length) {
    logList.innerHTML = `<div class="empty">暂无失败日志</div>`;
    return;
  }
  logList.innerHTML = data.logs.map(renderLogRow).join("");
}

async function loadStatus() {
  const resp = await fetch("/api/status");
  const status = await resp.json();
  renderStatus(status);
}

async function loadSettings() {
  const resp = await fetch("/api/status");
  const status = await resp.json();
  if (status.image_size) imageSizeInput.value = status.image_size;
  if (status.video_width) videoWidthInput.value = status.video_width;
  if (status.video_height) videoHeightInput.value = status.video_height;
  if (status.video_num_frames) videoNumFramesInput.value = status.video_num_frames;
  if (status.video_frame_rate) videoFrameRateInput.value = status.video_frame_rate;
  if (status.batch_limit !== undefined) batchLimitInput.value = status.batch_limit;
  if (status.schedule_start) scheduleStartInput.value = status.schedule_start;
  if (status.schedule_end) scheduleEndInput.value = status.schedule_end;
  if (status.style_preset) stylePreset.value = status.style_preset;
  if (status.variation_mode) variationMode.value = status.variation_mode;
  if (status.retention_days !== undefined) retentionDaysInput.value = status.retention_days;
}

/* ── 操作函数（使用 toast/confirm 替代 alert） ────────────────────────── */
async function deleteItem(itemId) {
  if (!await showConfirm("确定删除这条已完成的内容？本地图片和视频将一并删除。")) return;
  const resp = await fetch(`/api/items/${itemId}`, { method: "DELETE" });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    showToast(err.detail || "删除失败", "error");
    return;
  }
  removeItemCard(itemId);
  await loadStatus();
  showToast("已删除", "success");
}

async function remixItem(itemId) {
  const resp = await fetch(`/api/items/${itemId}/remix`, { method: "POST" });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    showToast(err.detail || "Remix 失败", "error");
    return;
  }
  await loadStatus();
  showToast("已创建 Remix 条目", "success");
}

async function deleteLog(logId) {
  if (!await showConfirm("确定删除这条失败日志？")) return;
  const resp = await fetch(`/api/logs/${logId}`, { method: "DELETE" });
  if (!resp.ok) {
    showToast("删除失败", "error");
    return;
  }
  removeLogRow(logId);
  await loadStatus();
  showToast("日志已删除", "success");
}

async function retryLog(logId) {
  const resp = await fetch(`/api/logs/${logId}/retry`, { method: "POST" });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    showToast(err.detail || "重试失败", "error");
    return;
  }
  removeLogRow(logId);
  await loadItems();
  await loadStatus();
  showToast("已重新加入流水线", "success");
}

async function clearLogs() {
  if (!await showConfirm("确定清空全部失败日志？")) return;
  await fetch("/api/logs", { method: "DELETE" });
  logList.innerHTML = `<div class="empty">暂无失败日志</div>`;
  await loadStatus();
  showToast("日志已清空", "success");
}

async function clearCompleted() {
  if (!await showConfirm("确定删除全部已完成条目？本地媒体文件将一并删除。")) return;
  const resp = await fetch("/api/items/completed", { method: "DELETE" });
  if (!resp.ok) {
    showToast("删除失败", "error");
    return;
  }
  await loadItems();
  await loadStatus();
  showToast("已清空", "success");
}

/* ── SSE 连接（含 B4 连接指示器） ────────────────────────────────────── */
function connectSSE() {
  const es = new EventSource("/api/events");

  es.addEventListener("open", () => {
    sseDot.classList.add("connected");
    sseDot.title = "SSE 已连接";
  });

  es.addEventListener("status_updated", (e) => {
    renderStatus(JSON.parse(e.data));
  });

  es.addEventListener("item_created", (e) => {
    upsertItem(JSON.parse(e.data));
  });

  es.addEventListener("item_updated", (e) => {
    upsertItem(JSON.parse(e.data));
  });

  es.addEventListener("item_removed", (e) => {
    removeItemCard(JSON.parse(e.data).id);
    loadStatus();
  });

  es.addEventListener("log_created", (e) => {
    upsertLog(JSON.parse(e.data));
    loadStatus();
  });

  es.addEventListener("log_deleted", (e) => {
    removeLogRow(JSON.parse(e.data).id);
    loadStatus();
  });

  es.addEventListener("logs_cleared", () => {
    logList.innerHTML = `<div class="empty">暂无失败日志</div>`;
    loadStatus();
  });

  es.addEventListener("items_cleared", () => {
    loadItems(1);
    loadStatus();
  });

  es.onerror = () => {
    sseDot.classList.remove("connected");
    sseDot.title = "SSE 断开，重连中...";
    es.close();
    setTimeout(connectSSE, 3000);
  };
}

/* ── 图片灯箱（B6，D18 Escape 关闭） ─────────────────────────────────── */
document.addEventListener("click", (e) => {
  if (e.target.matches(".media-box img")) {
    const overlay = document.createElement("div");
    overlay.className = "lightbox-overlay";
    overlay.innerHTML = `<img src="${e.target.src}" alt="放大查看" />`;
    overlay.addEventListener("click", () => overlay.remove());
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add("active"));
  }
});

/* ── D18: 全局 Escape 键处理 ─────────────────────────────────────────── */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const lightbox = document.querySelector(".lightbox-overlay");
    if (lightbox) lightbox.remove();
  }
});

/* ── 按钮事件（含 B5 加载状态） ──────────────────────────────────────── */
document.getElementById("saveThemeBtn").addEventListener("click", (e) => {
  withButtonLoading(e.currentTarget, async () => {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        theme: themeInput.value,
        image_size: imageSizeInput.value,
        video_width: videoWidthInput.value,
        video_height: videoHeightInput.value,
        video_num_frames: videoNumFramesInput.value,
        video_frame_rate: videoFrameRateInput.value,
        batch_limit: batchLimitInput.value,
        schedule_start: scheduleStartInput.value,
        schedule_end: scheduleEndInput.value,
        style_preset: stylePreset.value,
        retention_days: retentionDaysInput.value,
        variation_mode: variationMode.value,
      }),
    });
    await loadStatus();
    showToast("设置已保存", "success");
  });
});

document.getElementById("saveAdvancedBtn").addEventListener("click", (e) => {
  withButtonLoading(e.currentTarget, async () => {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        theme: themeInput.value,
        image_size: imageSizeInput.value,
        video_width: videoWidthInput.value,
        video_height: videoHeightInput.value,
        video_num_frames: videoNumFramesInput.value,
        video_frame_rate: videoFrameRateInput.value,
        batch_limit: batchLimitInput.value,
        schedule_start: scheduleStartInput.value,
        schedule_end: scheduleEndInput.value,
        style_preset: stylePreset.value,
        retention_days: retentionDaysInput.value,
        variation_mode: variationMode.value,
      }),
    });
    await loadStatus();
    showToast("高级设置已保存", "success");
  });
});

document.getElementById("stopBtn").addEventListener("click", (e) => {
  withButtonLoading(e.currentTarget, async () => {
    await fetch("/api/stop", { method: "POST" });
    await loadStatus();
  });
});

document.getElementById("startBtn").addEventListener("click", (e) => {
  withButtonLoading(e.currentTarget, async () => {
    await fetch("/api/start", { method: "POST" });
    await loadStatus();
  });
});

document.getElementById("clearLogsBtn").addEventListener("click", clearLogs);
document.getElementById("clearCompletedBtn").addEventListener("click", clearCompleted);

/* ── D16: 清理过期内容 ───────────────────────────────────────────────── */
cleanupBtn.addEventListener("click", async () => {
  withButtonLoading(cleanupBtn, async () => {
    try {
      const resp = await fetch("/api/cleanup", { method: "POST" });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        showToast(err.detail || "清理失败", "error");
        return;
      }
      const data = await resp.json();
      const count = data.deleted ?? 0;
      await loadItems();
      await loadStatus();
      showToast(`已清理 ${count} 条过期内容`, "success");
    } catch {
      showToast("网络错误", "error");
    }
  });
});

/* ── 搜索与筛选（C1） ────────────────────────────────────────────────── */
let searchTimer = null;
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadItems(1), 300);
});
statusFilter.addEventListener("change", () => loadItems(1));

/* ── 风格预设自动保存 ────────────────────────────────────────────────── */
stylePreset.addEventListener("change", async () => {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style_preset: stylePreset.value }),
  });
  showToast("风格已切换", "success");
});

variationMode.addEventListener("change", async () => {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ variation_mode: variationMode.value }),
  });
  showToast("风格变化模式已切换", "success");
});

/* ── 委托点击事件 ────────────────────────────────────────────────────── */
document.addEventListener("click", (e) => {
  const btn = e.target.closest("#pagination button[data-page]");
  if (btn && !btn.disabled) {
    const page = parseInt(btn.dataset.page);
    if (page >= 1) loadItems(page);
  }
});

itemList.addEventListener("click", (e) => {
  const remixBtn = e.target.closest(".btn-remix-item");
  if (remixBtn) {
    remixItem(remixBtn.dataset.id);
    return;
  }
  const btn = e.target.closest(".btn-delete-item");
  if (btn) deleteItem(btn.dataset.id);
});

logList.addEventListener("click", (e) => {
  const retryBtn = e.target.closest(".btn-retry-log");
  if (retryBtn) {
    e.preventDefault();
    retryLog(retryBtn.dataset.id);
    return;
  }
  const btn = e.target.closest(".btn-delete-log");
  if (btn) {
    e.preventDefault();
    deleteLog(btn.dataset.id);
  }
});

/* ── 初始化 ──────────────────────────────────────────────────────────── */
(async () => {
  // D6: 恢复视图模式
  const savedView = localStorage.getItem("agnes_view_mode");
  if (savedView) applyViewMode(savedView);

  // D7: 恢复主题
  const savedTheme = localStorage.getItem("agnes_theme");
  if (savedTheme) applyTheme(savedTheme);

  await loadItems();
  await loadLogs();
  await loadStatus();
  await loadSettings();
  loadAnalytics();
  loadTemplates();
  loadWebhooks();
  connectSSE();
})();

/* ── 生成统计 ─────────────────────────────────────────────────────────── */
async function loadAnalytics() {
  const panel = document.getElementById("analyticsPanel");
  try {
    const resp = await fetch("/api/analytics");
    const data = await resp.json();
    panel.innerHTML = `
      <div class="analytics-card">
        <div class="analytics-value">${data.total_completed}</div>
        <div class="analytics-label">已完成</div>
      </div>
      <div class="analytics-card">
        <div class="analytics-value">${data.total_failed}</div>
        <div class="analytics-label">失败数</div>
      </div>
      <div class="analytics-card">
        <div class="analytics-value">${data.success_rate}%</div>
        <div class="analytics-label">成功率</div>
      </div>
      <div class="analytics-card">
        <div class="analytics-value">${data.avg_generation_time > 0 ? formatDuration(data.avg_generation_time) : "—"}</div>
        <div class="analytics-label">平均耗时</div>
      </div>
      <div class="analytics-card">
        <div class="analytics-value">${data.daily_counts ? Object.values(data.daily_counts).reduce((a, b) => a + b, 0) : 0}</div>
        <div class="analytics-label">近7天</div>
      </div>
    `;
  } catch {
    panel.innerHTML = `<div class="analytics-loading">加载失败</div>`;
  }
}

function formatDuration(seconds) {
  if (seconds < 60) return seconds.toFixed(0) + "秒";
  const minutes = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return minutes + "分" + secs + "秒";
}

/* ── 提示词模板 ────────────────────────────────────────────────────────── */
let editingTemplateId = null;

async function loadTemplates() {
  const list = document.getElementById("templateList");
  try {
    const resp = await fetch("/api/templates");
    const data = await resp.json();
    if (!data.templates.length) {
      list.innerHTML = `<div class="empty" style="padding:12px">暂无模板，可创建模板以自定义提示词结构</div>`;
      return;
    }
    list.innerHTML = data.templates.map(tmpl => `
      <div class="template-item" data-id="${tmpl.id}">
        <span class="template-item-name">${escapeHtml(tmpl.name)}</span>
        ${tmpl.is_default ? '<span class="template-item-default">默认</span>' : ""}
        <button type="button" class="btn-ghost btn-sm btn-set-default-tpl" data-id="${tmpl.id}" ${tmpl.is_default ? "disabled" : ""}>设为默认</button>
        <button type="button" class="btn-ghost btn-sm btn-edit-tpl" data-id="${tmpl.id}">编辑</button>
        <button type="button" class="btn-ghost btn-sm btn-delete-tpl" data-id="${tmpl.id}">删除</button>
      </div>
    `).join("");
  } catch {
    list.innerHTML = `<div class="empty" style="padding:12px">加载模板失败</div>`;
  }
}

document.getElementById("tplSaveBtn").addEventListener("click", async (e) => {
  const name = document.getElementById("tplNameInput").value.trim();
  if (!name) { showToast("请输入模板名称", "warning"); return; }
  const body = {
    name,
    image_prompt_template: document.getElementById("tplImagePrompt").value,
    video_prompt_template: document.getElementById("tplVideoPrompt").value,
    style_modifiers: document.getElementById("tplStyleModifiers").value,
    is_default: false,
  };
  try {
    const url = editingTemplateId ? `/api/templates/${editingTemplateId}` : "/api/templates";
    const method = editingTemplateId ? "PUT" : "POST";
    const resp = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast(err.detail || "保存失败", "error");
      return;
    }
    clearTemplateForm();
    await loadTemplates();
    showToast(editingTemplateId ? "模板已更新" : "模板已创建", "success");
  } catch {
    showToast("网络错误", "error");
  }
});

document.getElementById("tplCancelBtn").addEventListener("click", clearTemplateForm);

function clearTemplateForm() {
  editingTemplateId = null;
  document.getElementById("tplNameInput").value = "";
  document.getElementById("tplImagePrompt").value = "";
  document.getElementById("tplVideoPrompt").value = "";
  document.getElementById("tplStyleModifiers").value = "";
  document.getElementById("templateFormTitle").textContent = "新建模板";
  document.getElementById("tplCancelBtn").style.display = "none";
  document.getElementById("tplSaveBtn").textContent = "保存模板";
}

// 委托事件
document.getElementById("templateList").addEventListener("click", async (e) => {
  const editBtn = e.target.closest(".btn-edit-tpl");
  if (editBtn) {
    editingTemplateId = editBtn.dataset.id;
    try {
      const resp = await fetch("/api/templates");
      const data = await resp.json();
      const tmpl = data.templates.find(t => t.id === editingTemplateId);
      if (tmpl) {
        document.getElementById("tplNameInput").value = tmpl.name || "";
        document.getElementById("tplImagePrompt").value = tmpl.image_prompt_template || "";
        document.getElementById("tplVideoPrompt").value = tmpl.video_prompt_template || "";
        document.getElementById("tplStyleModifiers").value = tmpl.style_modifiers || "";
        document.getElementById("templateFormTitle").textContent = "编辑模板";
        document.getElementById("tplCancelBtn").style.display = "";
        document.getElementById("tplSaveBtn").textContent = "更新模板";
      }
    } catch { showToast("加载模板失败", "error"); }
    return;
  }
  const defaultBtn = e.target.closest(".btn-set-default-tpl");
  if (defaultBtn) {
    const id = defaultBtn.dataset.id;
    const resp = await fetch(`/api/templates/${id}/default`, { method: "POST" });
    if (!resp.ok) { showToast("设置默认失败", "error"); return; }
    await loadTemplates();
    showToast("已设为默认模板", "success");
    return;
  }
  const deleteBtn = e.target.closest(".btn-delete-tpl");
  if (deleteBtn) {
    if (!await showConfirm("确定删除此模板？")) return;
    const id = deleteBtn.dataset.id;
    await fetch(`/api/templates/${id}`, { method: "DELETE" });
    if (editingTemplateId === id) clearTemplateForm();
    await loadTemplates();
    showToast("模板已删除", "success");
  }
});

/* ── Webhook 管理（D14） ──────────────────────────────────────────────── */
let editingWebhookId = null;

async function loadWebhooks() {
  const list = document.getElementById("webhookList");
  try {
    const resp = await fetch("/api/webhooks");
    const data = await resp.json();
    if (!data.webhooks || !data.webhooks.length) {
      list.innerHTML = `<div class="empty" style="padding:12px">暂无 Webhook，可创建 Webhook 以接收事件通知</div>`;
      return;
    }
    list.innerHTML = data.webhooks.map(wh => {
      const urlDisplay = wh.url.length > 50 ? wh.url.slice(0, 50) + "..." : wh.url;
      const activeClass = wh.is_active !== false ? "active" : "inactive";
      const activeLabel = wh.is_active !== false ? "启用" : "停用";
      return `
        <div class="webhook-item" data-id="${wh.id}">
          <span class="webhook-item-url" title="${escapeHtml(wh.url)}">${escapeHtml(urlDisplay)}</span>
          <span class="webhook-item-events">${escapeHtml(wh.events || "")}</span>
          <span class="webhook-item-active ${activeClass}">${activeLabel}</span>
          <button type="button" class="btn-ghost btn-sm btn-test-wh" data-id="${wh.id}">测试</button>
          <button type="button" class="btn-ghost btn-sm btn-edit-wh" data-id="${wh.id}">编辑</button>
          <button type="button" class="btn-ghost btn-sm btn-delete-wh" data-id="${wh.id}">删除</button>
        </div>`;
    }).join("");
  } catch {
    list.innerHTML = `<div class="empty" style="padding:12px">加载 Webhook 失败</div>`;
  }
}

document.getElementById("whSaveBtn").addEventListener("click", async () => {
  const url = document.getElementById("whUrlInput").value.trim();
  if (!url) { showToast("请输入 Webhook URL", "warning"); return; }
  const body = {
    url,
    events: document.getElementById("whEventsInput").value,
    secret: document.getElementById("whSecretInput").value,
  };
  try {
    const reqUrl = editingWebhookId ? `/api/webhooks/${editingWebhookId}` : "/api/webhooks";
    const method = editingWebhookId ? "PUT" : "POST";
    const resp = await fetch(reqUrl, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast(err.detail || "保存失败", "error");
      return;
    }
    clearWebhookForm();
    await loadWebhooks();
    showToast(editingWebhookId ? "Webhook 已更新" : "Webhook 已创建", "success");
  } catch {
    showToast("网络错误", "error");
  }
});

document.getElementById("whCancelBtn").addEventListener("click", clearWebhookForm);

function clearWebhookForm() {
  editingWebhookId = null;
  document.getElementById("whUrlInput").value = "";
  document.getElementById("whEventsInput").value = "";
  document.getElementById("whSecretInput").value = "";
  document.getElementById("webhookFormTitle").textContent = "新建 Webhook";
  document.getElementById("whCancelBtn").style.display = "none";
  document.getElementById("whSaveBtn").textContent = "保存";
}

// 委托事件
document.getElementById("webhookList").addEventListener("click", async (e) => {
  const testBtn = e.target.closest(".btn-test-wh");
  if (testBtn) {
    const id = testBtn.dataset.id;
    try {
      const resp = await fetch(`/api/webhooks/${id}/test`, { method: "POST" });
      if (!resp.ok) {
        showToast("测试失败", "error");
        return;
      }
      showToast("测试请求已发送", "success");
    } catch {
      showToast("网络错误", "error");
    }
    return;
  }
  const editBtn = e.target.closest(".btn-edit-wh");
  if (editBtn) {
    editingWebhookId = editBtn.dataset.id;
    try {
      const resp = await fetch("/api/webhooks");
      const data = await resp.json();
      const wh = data.webhooks.find(w => w.id === editingWebhookId);
      if (wh) {
        document.getElementById("whUrlInput").value = wh.url || "";
        document.getElementById("whEventsInput").value = wh.events || "";
        document.getElementById("whSecretInput").value = wh.secret || "";
        document.getElementById("webhookFormTitle").textContent = "编辑 Webhook";
        document.getElementById("whCancelBtn").style.display = "";
        document.getElementById("whSaveBtn").textContent = "更新";
      }
    } catch { showToast("加载 Webhook 失败", "error"); }
    return;
  }
  const deleteBtn = e.target.closest(".btn-delete-wh");
  if (deleteBtn) {
    if (!await showConfirm("确定删除此 Webhook？")) return;
    const id = deleteBtn.dataset.id;
    await fetch(`/api/webhooks/${id}`, { method: "DELETE" });
    if (editingWebhookId === id) clearWebhookForm();
    await loadWebhooks();
    showToast("Webhook 已删除", "success");
  }
});
