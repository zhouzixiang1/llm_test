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

const itemList = document.getElementById("itemList");
const logList = document.getElementById("logList");
const logCount = document.getElementById("logCount");
const statusBar = document.getElementById("statusBar");
const themeInput = document.getElementById("themeInput");

function renderStatus(status) {
  const running = status.running ? "运行中" : "已停止";
  const stopHint = status.stop_after_item ? "（将在当前条目完成后停止）" : "";
  const logs = status.failed_log_count ?? 0;
  statusBar.textContent = `${running}${stopHint} · 内容 ${status.total_count} 条 · 失败日志 ${logs} 条`;
  if (status.theme !== undefined) themeInput.value = status.theme;
  logCount.textContent = logs;
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

function stepLabel(step) {
  return STEP_LABELS[step] || step;
}

function formatTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("zh-CN");
  } catch {
    return iso;
  }
}

function renderCard(item) {
  const imageSrc = item.image_media || "";
  const videoSrc = item.video_media || item.video_url || "";
  const progress =
    item.status === "generating_video" && item.video_progress
      ? `<div class="progress">视频进度: ${item.video_progress}%</div>`
      : "";
  const deleteBtn =
    item.status === "completed"
      ? `<button type="button" class="btn-ghost btn-sm btn-delete-item" data-id="${item.id}">删除</button>`
      : "";

  return `
    <article class="card" data-id="${item.id}">
      <div class="card-header">
        <h2>#${item.seq} ${escapeHtml(item.title || "生成中...")}</h2>
        <span class="badge badge-${item.status}">${statusLabel(item.status)}</span>
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
            ? `<img src="${imageSrc}" alt="generated image" />`
            : `<div class="placeholder">图片生成中...</div>`}
        </div>
        <div class="media-box">
          ${videoSrc
            ? `<video src="${videoSrc}" controls playsinline></video>`
            : `<div class="placeholder">视频生成中...</div>`}
        </div>
      </div>
    </article>
  `;
}

function renderLogRow(log) {
  const summary = (log.error || "").slice(0, 120);
  return `
    <details class="log-row" data-id="${log.id}">
      <summary>
        <span class="log-seq">#${log.seq}</span>
        <span class="log-time">${formatTime(log.created_at)}</span>
        <span class="log-step">${stepLabel(log.step)}</span>
        <span class="log-summary">${escapeHtml(summary)}${(log.error || "").length > 120 ? "…" : ""}</span>
        <button type="button" class="btn-ghost btn-sm btn-delete-log" data-id="${log.id}">删除</button>
      </summary>
      <div class="log-detail">
        ${log.title ? `<p><strong>标题</strong> ${escapeHtml(log.title)}</p>` : ""}
        ${log.theme ? `<p><strong>主题</strong> ${escapeHtml(log.theme)}</p>` : ""}
        ${log.image_prompt ? `<p><strong>图片提示词</strong> ${escapeHtml(log.image_prompt)}</p>` : ""}
        ${log.video_prompt ? `<p><strong>视频提示词</strong> ${escapeHtml(log.video_prompt)}</p>` : ""}
        <pre class="log-error">${escapeHtml(log.error || "")}</pre>
      </div>
    </details>
  `;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function upsertItem(item) {
  const existing = document.querySelector(`#itemList [data-id="${item.id}"]`);
  const html = renderCard(item);
  if (existing) {
    existing.outerHTML = html;
  } else {
    itemList.insertAdjacentHTML("afterbegin", html);
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

async function loadItems() {
  const resp = await fetch("/api/items");
  const data = await resp.json();
  if (!data.items.length) {
    itemList.innerHTML = `<div class="empty">暂无记录，流水线启动后将逐条追加</div>`;
    return;
  }
  itemList.innerHTML = data.items.map(renderCard).join("");
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

async function deleteItem(itemId) {
  if (!confirm("确定删除这条已完成的内容？本地图片和视频将一并删除。")) return;
  const resp = await fetch(`/api/items/${itemId}`, { method: "DELETE" });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    alert(err.detail || "删除失败");
    return;
  }
  removeItemCard(itemId);
  await loadStatus();
}

async function deleteLog(logId) {
  if (!confirm("确定删除这条失败日志？")) return;
  const resp = await fetch(`/api/logs/${logId}`, { method: "DELETE" });
  if (!resp.ok) {
    alert("删除失败");
    return;
  }
  removeLogRow(logId);
  await loadStatus();
}

async function clearLogs() {
  if (!confirm("确定清空全部失败日志？")) return;
  await fetch("/api/logs", { method: "DELETE" });
  logList.innerHTML = `<div class="empty">暂无失败日志</div>`;
  await loadStatus();
}

async function clearCompleted() {
  if (!confirm("确定删除全部已完成条目？本地媒体文件将一并删除。")) return;
  const resp = await fetch("/api/items/completed", { method: "DELETE" });
  if (!resp.ok) {
    alert("删除失败");
    return;
  }
  await loadItems();
  await loadStatus();
}

function connectSSE() {
  const es = new EventSource("/api/events");

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
    loadItems();
    loadStatus();
  });

  es.onerror = () => {
    es.close();
    setTimeout(connectSSE, 3000);
  };
}

document.getElementById("saveThemeBtn").addEventListener("click", async () => {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme: themeInput.value }),
  });
  await loadStatus();
});

document.getElementById("stopBtn").addEventListener("click", async () => {
  await fetch("/api/stop", { method: "POST" });
  await loadStatus();
});

document.getElementById("startBtn").addEventListener("click", async () => {
  await fetch("/api/start", { method: "POST" });
  await loadStatus();
});

document.getElementById("clearLogsBtn").addEventListener("click", clearLogs);
document.getElementById("clearCompletedBtn").addEventListener("click", clearCompleted);

itemList.addEventListener("click", (e) => {
  const btn = e.target.closest(".btn-delete-item");
  if (btn) deleteItem(btn.dataset.id);
});

logList.addEventListener("click", (e) => {
  const btn = e.target.closest(".btn-delete-log");
  if (btn) {
    e.preventDefault();
    deleteLog(btn.dataset.id);
  }
});

(async () => {
  await loadItems();
  await loadLogs();
  await loadStatus();
  connectSSE();
})();
