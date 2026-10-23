const statusLabels = {
  pending: "等待执行",
  running: "执行中",
  succeeded: "已完成",
  failed: "执行失败",
};

const stepLabels = {
  validate_asset: "校验动画素材",
  extract_frames: "抽取关键帧",
  ai_style_transfer: "AI 风格迁移（Mock）",
  compose_preview: "合成预览结果",
};

let tasks = [];
let selectedTaskId = null;
let refreshing = false;
let toastTimer = null;

const detail = document.querySelector("#detail");
const taskList = document.querySelector("#taskList");
const taskCount = document.querySelector("#taskCount");
const healthText = document.querySelector("#healthText");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function statusBadge(status) {
  return element("span", `status status-${status}`, statusLabels[status] || status);
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  }).format(date);
}

function showToast(message, isError = false) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3200);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败：${response.status}`);
  return payload;
}

function renderTaskList() {
  taskList.replaceChildren();
  taskCount.textContent = `${tasks.length} 个任务`;
  if (!tasks.length) {
    taskList.append(element("div", "empty-state", "还没有任务，先启动一个 Mock 管线。"));
    return;
  }

  for (const task of tasks) {
    const button = element("button", "task-item");
    button.type = "button";
    button.classList.toggle("active", task.id === selectedTaskId);
    button.addEventListener("click", () => selectTask(task.id));

    const top = element("div", "task-item-top");
    top.append(element("span", "task-item-title", task.title), statusBadge(task.status));

    const meta = element("div", "task-item-meta");
    const progressText = `${Math.round((task.progress || 0) * 100)}% · ${task.completed_steps || 0}/${task.total_steps || 0} 步`;
    meta.append(element("span", "", task.id), element("span", "", progressText));

    const progress = element("div", "progress-track");
    const fill = element("span");
    fill.style.width = `${Math.round((task.progress || 0) * 100)}%`;
    progress.append(fill);
    button.append(top, meta, progress);
    taskList.append(button);
  }
}

async function selectTask(taskId) {
  selectedTaskId = taskId;
  renderTaskList();
  await loadDetail();
}

async function loadTasks() {
  const payload = await request("/api/tasks");
  tasks = payload.tasks || [];
  if (!selectedTaskId && tasks.length) selectedTaskId = tasks[0].id;
  if (selectedTaskId && !tasks.some((task) => task.id === selectedTaskId)) {
    selectedTaskId = tasks.length ? tasks[0].id : null;
  }
  renderTaskList();
}

function metric(label, value) {
  const box = element("div", "metric");
  box.append(element("span", "", label), element("strong", "", value));
  return box;
}

function buildDetailShell(task) {
  const content = element("div", "detail-content");
  const head = element("div", "detail-head");
  const titleBlock = element("div");
  titleBlock.append(
    element("p", "eyebrow", "TASK DETAIL"),
    element("h2", "", task.title),
    element("div", "task-id", task.id),
  );

  const actions = element("div", "detail-actions");
  actions.append(statusBadge(task.status));
  if (task.status === "failed") {
    const retry = element("button", "ghost-button", "重试失败步骤");
    retry.type = "button";
    retry.addEventListener("click", () => retryTask(task.id));
    actions.append(retry);
  }
  const refresh = element("button", "ghost-button", "刷新");
  refresh.type = "button";
  refresh.addEventListener("click", loadDetail);
  actions.append(refresh);
  head.append(titleBlock, actions);
  content.append(head);

  const overview = element("div", "overview");
  overview.append(
    metric("任务进度", `${Math.round((task.progress || 0) * 100)}%`),
    metric("已开始步骤", `${task.steps.filter((step) => step.attempts > 0).length}/${task.steps.length}`),
    metric("结果复用", `${task.steps.filter((step) => step.reused).length} 步`),
  );
  content.append(overview);

  const progress = element("div", "overview-progress");
  const progressFill = element("span");
  progressFill.style.width = `${Math.round((task.progress || 0) * 100)}%`;
  progress.append(progressFill);
  content.append(progress);

  const stepSectionTitle = element("div", "section-title");
  stepSectionTitle.append(
    element("h3", "", "步骤状态与依赖"),
    element("span", "", "严格按序号顺序执行"),
  );
  const steps = element("div", "steps");
  for (const step of task.steps) {
    const row = element("div", `step is-${step.status}`);
    row.append(element("div", "step-number", String(step.seq).padStart(2, "0")));
    const main = element("div", "step-main");
    main.append(
      element("div", "step-name", stepLabels[step.name] || step.name),
      element("div", "step-code", step.name),
    );

    const meta = element("div", "step-meta");
    meta.append(element("span", "", `尝试 ${step.attempts}/${step.max_attempts}`));
    if (step.reused) meta.append(element("span", "reuse-badge", "已复用缓存"));
    if (step.next_retry_at) meta.append(element("span", "", `重试于 ${formatTime(step.next_retry_at)}`));
    if (step.finished_at) meta.append(element("span", "", `完成 ${formatTime(step.finished_at)}`));
    main.append(meta);
    if (step.last_error && step.status !== "succeeded") {
      main.append(element("div", "step-detail", step.last_error));
    }
    row.append(main, statusBadge(step.status));
    steps.append(row);
  }
  content.append(stepSectionTitle, steps);

  const historyTitle = element("div", "section-title");
  historyTitle.append(
    element("h3", "", "执行历史"),
    element("span", "", `${task.history.length} 条持久化事件`),
  );
  const history = element("div", "history-list");
  for (const item of [...task.history].reverse()) {
    const row = element("div", "history-item");
    row.dataset.event = item.event;
    const dot = element("span", "history-dot");
    const message = element("span", "history-message", item.message || item.event);
    const time = element("time", "history-time", formatTime(item.created_at));
    row.append(dot, message, time);
    history.append(row);
  }
  content.append(historyTitle, history);

  if (task.error) content.append(element("div", "task-error", task.error));
  return content;
}


async function loadDetail() {
  if (!selectedTaskId) {
    detail.replaceChildren();
    const empty = element("div", "detail-empty");
    const orbit = element("div", "orbit");
    orbit.append(element("span"), element("span"), element("span"));
    empty.append(
      orbit,
      element("h2", "", "选择一个任务"),
      element("p", "", "查看任务总状态、步骤依赖、重试历史和持久化结果。"),
    );
    detail.append(empty);
    return;
  }

  try {
    const task = await request(`/api/tasks/${selectedTaskId}`);
    detail.replaceChildren(buildDetailShell(task));
  } catch (error) {
    showToast(error.message, true);
  }
}

async function retryTask(taskId) {
  try {
    await request(`/api/tasks/${taskId}/retry`, { method: "POST", body: "{}" });
    showToast("已重新排队，成功步骤会直接复用缓存");
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const [taskPayload, health] = await Promise.all([
      request("/api/tasks"),
      request("/api/health"),
    ]);
    tasks = taskPayload.tasks || [];
    if (!selectedTaskId && tasks.length) selectedTaskId = tasks[0].id;
    if (selectedTaskId && !tasks.some((task) => task.id === selectedTaskId)) {
      selectedTaskId = tasks.length ? tasks[0].id : null;
    }
    taskCount.textContent = `${tasks.length} 个任务`;
    renderTaskList();
    healthText.textContent = `服务运行中 · ${health.stats.tasks} 任务 / ${health.stats.cached_results} 缓存`;
    await loadDetail();
  } catch (error) {
    healthText.textContent = "服务连接异常";
    showToast(error.message, true);
  } finally {
    refreshing = false;
  }
}

document.querySelector("#createForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.querySelector("#createButton");
  button.disabled = true;
  const failStep = document.querySelector("#failStep").value;
  const payload = {
    title: document.querySelector("#title").value,
    asset_name: document.querySelector("#assetName").value,
    step_delay_ms: Number(document.querySelector("#stepDelay").value),
    fail_step: failStep || null,
    fail_times: failStep ? Number(document.querySelector("#failTimes").value) : 0,
  };

  try {
    const created = await request("/api/tasks", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    selectedTaskId = created.id;
    showToast("任务已进入执行队列");
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#failurePreset").addEventListener("click", () => {
  document.querySelector("#failStep").value = "ai_style_transfer";
  document.querySelector("#failTimes").value = "1";
  document.querySelector("#stepDelay").value = "800";
  showToast("已填入：AI 风格迁移失败 1 次后自动重试");
});

refresh();
setInterval(refresh, 900);
