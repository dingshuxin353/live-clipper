# P1 Spec：AI 审阅闭环 —— 校验容错 · 交互反馈 · 失败可见 · NAS 韧性

- 版本：P1
- 日期：2026-07-06
- 前置：P0（`2026-07-04-p0-service-stuck-run-reliability.md`）已合入；修复 A（codex `--skip-git-repo-check` + `stdin=DEVNULL`）已由人工合入 `review_automation.py::_default_local_runner`。
- 目标执行者：Codex（严格逐字照做，禁止自行发挥）
- 预计改动文件：`src/live_clipper/models.py`、`src/live_clipper/build_codex_brief.py`、`src/live_clipper/review_automation.py`、`src/live_clipper/web.py`、`src/live_clipper/web_static/app.js`、`src/live_clipper/web_static/styles.css`、`src/live_clipper/service.py`、以及 `tests/` 下若干测试。
- **禁止改动**见第 10 节「红线」。

---

## 0. 给 Codex 的阅读须知（先读）

1. 「新代码」块请**整段复制粘贴**，不要凭记忆重写、不要顺手优化。
2. 「当前代码（定位锚点）」只用来找位置，**不要把锚点也粘贴进去**。
3. 每个任务末尾有「自检」，做完一个核对一次再做下一个。
4. 全部完成后运行第 8 节验证命令，输出贴到交付说明。
5. 不引入本文件未提到的库、配置项、API 端点、文件。
6. 遵守 `AGENTS.md` 写入边界：不改 `input/`、`output/`、`work/` 下用户数据。

---

## 1. 背景

本会话已定位并修复了「点 AI 审阅没反应」的根因（codex 卡 stdin + 拒绝非 git 目录，修复 A 已合入）。修复后 codex 能跑通，但真机验证暴露了**下一批问题**，P1 一次解决，让「待审阅 → AI 审阅 → 出片」这条链路真正闭环、且失败可见：

- **C（校验容错）**：codex 返回 `remove_ranges: [{"start":4911.18,"end":4921.42}]`（对象数组），而 `SelectedClip.remove_ranges` 要求 `list[tuple[float,float]]`（`[[start,end]]`），校验抛错，选片不落盘。LLM 输出天生有波动，必须容错。
- **B（交互反馈）**：前端点「立即 AI 审阅」后无在途反馈（按钮不置灰、无「审阅中」提示）。一次要跑约 60 秒的正常审阅，与卡死在界面上无法区分。
- **D（失败可见）**：AI 审阅失败信息只写进 `work/service/review_automation_events.jsonl` 和 summary 文件，任务卡片上看不到，用户无从得知为何没出片。
- **E（NAS 韧性）**：`run_service_once` 先 reconcile（未落盘）再扫描 NAS，NAS 未挂载时 `scan_recording_source` 抛 `FileNotFoundError`，导致 reconcile 结果丢失。用户 NAS 路径经常未挂载。

---

## 2. 目标与非目标

### 目标
1. C：`SelectedClip.remove_ranges` 容忍 `{"start":s,"end":e}` 与 `[s,e]` 两种写法。
2. C：审阅提示词写明 `remove_ranges` 的正确格式（双保险）。
3. B：AI 审阅按钮点击后置灰 + 显示「AI 审阅中…」，结束按成败给真实反馈，删掉误导文案。
4. D：后端把「本 run 最近一次 AI 审阅失败」的错误暴露到 run detail；前端在待审阅卡片上红字显示。
5. E：`run_service_once` 在扫描失败时不丢 reconcile 结果，把扫描失败降级为可观测事件。
6. 补齐测试。

### 非目标
- 不把 AI 审阅/渲染改成异步任务模型（那是 P2）。
- 不加鉴权、不统一全局错误 schema、不做日志轮转（P2）。
- 不新增 Web API 端点。
- 不改 codex/claude 的调用方式（A 已修复，别动）。

---

## 3. 任务分解（按顺序执行）

### Task 1 —— C：`remove_ranges` 容错校验器

**文件**：`src/live_clipper/models.py`

`field_validator` 已在第 5 行导入，无需新增 import。

**当前代码（定位锚点，第 94–102 行）**：
```python
class SelectedClip(BaseModel):
    clip_id: str
    source_start: float
    source_end: float
    title: str
    remove_ranges: list[tuple[float, float]] = Field(default_factory=list)
    subtitle_highlights: list[str] = Field(default_factory=list)
    format: str = "horizontal_highlight"
    priority: int = 1

    @field_validator("clip_id")
    @classmethod
    def validate_clip_id(cls, value: str) -> str:
        return validate_safe_id(value, "clip_id")
```

**替换为**（整段复制）：
```python
class SelectedClip(BaseModel):
    clip_id: str
    source_start: float
    source_end: float
    title: str
    remove_ranges: list[tuple[float, float]] = Field(default_factory=list)
    subtitle_highlights: list[str] = Field(default_factory=list)
    format: str = "horizontal_highlight"
    priority: int = 1

    @field_validator("remove_ranges", mode="before")
    @classmethod
    def normalize_remove_ranges(cls, value: object) -> object:
        """容忍 LLM 常见的对象写法 {"start": s, "end": e}，归一化为 [s, e]。

        同时兼容已有的 [s, e] / (s, e) 写法。无法识别的元素原样返回，交由
        pydantic 抛出清晰的类型错误。
        """
        if not isinstance(value, list):
            return value
        normalized: list[object] = []
        for item in value:
            if isinstance(item, dict):
                start = item.get("start", item.get("from"))
                end = item.get("end", item.get("to"))
                if start is not None and end is not None:
                    normalized.append([start, end])
                else:
                    normalized.append(item)
            else:
                normalized.append(item)
        return normalized

    @field_validator("clip_id")
    @classmethod
    def validate_clip_id(cls, value: str) -> str:
        return validate_safe_id(value, "clip_id")
```

**自检 1**：
- `SelectedClip` 里新增了 `normalize_remove_ranges`，且带 `mode="before"`。
- `remove_ranges` 字段类型仍是 `list[tuple[float, float]]`（不要改类型）。
- `validate_clip_id`、`validate_time_range` 等原有校验器保持不变。

---

### Task 2 —— C：提示词写明 `remove_ranges` 格式（双保险）

**2a. 文件**：`src/live_clipper/build_codex_brief.py`

**当前代码（定位锚点，约第 83 行那条指令）**：
```python
        "- Use `remove_ranges` only when an otherwise strong clip has a small removable section.\n"
```

**替换为**：
```python
        "- Use `remove_ranges` only when an otherwise strong clip has a small removable section.\n"
        "- `remove_ranges` MUST be an array of [start, end] number pairs in seconds, e.g. [[12.5, 18.0]]. "
        "Never use objects like {\"start\": ..., \"end\": ...}.\n"
```

**2b. 文件**：`src/live_clipper/review_automation.py`

**当前代码（定位锚点，`_review_prompt` 函数，约第 409–413 行）**：
```python
def _review_prompt(payload: dict[str, Any]) -> str:
    return (
        "你是 live-clipper 的 AI 审阅助手。请只根据下面的审阅包生成 selected_clips.json 需要的 JSON 数组。\n"
        "禁止删除、移动、清理任何文件；禁止 approve/reject confirmation；不要写文件，只返回 JSON 数组。\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
```

**替换为**：
```python
def _review_prompt(payload: dict[str, Any]) -> str:
    return (
        "你是 live-clipper 的 AI 审阅助手。请只根据下面的审阅包生成 selected_clips.json 需要的 JSON 数组。\n"
        "禁止删除、移动、清理任何文件；禁止 approve/reject confirmation；不要写文件，只返回 JSON 数组。\n"
        "字段 remove_ranges 必须是 [开始秒, 结束秒] 的数组，例如 [[12.5, 18.0]]；不要用 {\"start\":...} 这样的对象。\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
```

**自检 2**：两处提示词都加了 `remove_ranges` 格式说明，其余文字未动。

---

### Task 3 —— D（后端）：把 AI 审阅失败信息暴露到 run detail

**3a. 文件**：`src/live_clipper/review_automation.py` —— 新增一个读取 summary 的公开函数。

summary 文件路径由已存在的 `_summary_path(service_dir)` 给出，结构形如：
```json
{"enabled": false, "mode": "local_agent", "provider": "codex_cli",
 "last_run_at": "...", "last_status": "failed", "last_run_id": "<run_id>", "last_error": "..."}
```

在 `get_review_automation_status` 函数（约第 68 行）**之前**，新增：
```python
def read_last_review_summary(service_dir: Path) -> dict[str, Any]:
    """读取最近一次 AI 审阅的结果摘要（不存在时返回空字典）。"""
    path = _summary_path(service_dir)
    return read_json(path) if path.exists() else {}
```

> `read_json`、`Path`、`Any`、`_summary_path` 均已在本文件可用，无需新增 import。

**3b. 文件**：`src/live_clipper/web.py` —— 在 `build_run_detail` 里加 `ai_review` 字段。

`web.py` 顶部已 `from . import ... review_automation ...`（第 15 行），可直接调用。

**当前代码（定位锚点，`build_run_detail` 的 service 分支返回，约第 316–336 行）**：
```python
        run["requires_codex"] = run.get("phase") == "needs_review"
        cleanup = _cleanup_preview(run_dir, paths)
        return {
            "ok": True,
            "run": run,
            "steps": [],
            "files": detail["files"],
            "clips": build_clip_list(run_dir),
            "cleanup": cleanup,
            "state": service_run,
            "events": detail.get("events", []),
            "actions": {
                "can_check": True,
                "can_render": (run_dir / "selected_clips.json").exists() and not detail.get("rendered_clip_count"),
                "can_cleanup_preview": (run_dir / "selected_clips.json").exists(),
                "can_cleanup": bool(detail.get("rendered_clip_count")),
                "can_delete_local_source": bool(service_run.get("local_source_path")) and bool(detail.get("rendered_clip_count")),
                "can_ai_review": run.get("phase") == "needs_review" and not (run_dir / "selected_clips.json").exists(),
            },
            "log": mcp_tools.get_run_log(run_id, lines=log_lines, service_dir=paths.service_dir),
        }
```

**替换为**：
```python
        run["requires_codex"] = run.get("phase") == "needs_review"
        cleanup = _cleanup_preview(run_dir, paths)
        return {
            "ok": True,
            "run": run,
            "steps": [],
            "files": detail["files"],
            "clips": build_clip_list(run_dir),
            "cleanup": cleanup,
            "state": service_run,
            "events": detail.get("events", []),
            "ai_review": _ai_review_for_run(run_id, paths),
            "actions": {
                "can_check": True,
                "can_render": (run_dir / "selected_clips.json").exists() and not detail.get("rendered_clip_count"),
                "can_cleanup_preview": (run_dir / "selected_clips.json").exists(),
                "can_cleanup": bool(detail.get("rendered_clip_count")),
                "can_delete_local_source": bool(service_run.get("local_source_path")) and bool(detail.get("rendered_clip_count")),
                "can_ai_review": run.get("phase") == "needs_review" and not (run_dir / "selected_clips.json").exists(),
            },
            "log": mcp_tools.get_run_log(run_id, lines=log_lines, service_dir=paths.service_dir),
        }
```

（只加了一行 `"ai_review": _ai_review_for_run(run_id, paths),`。）

**同文件**：`build_run_detail` 的**非 service 分支**返回（约第 350–370 行，那个包含 `"steps": _steps_from_status(status),` 的 return 字典）里，同样加一行。找到该 return 字典中的：
```python
        "state": state,
        "actions": {
```
替换为：
```python
        "state": state,
        "ai_review": _ai_review_for_run(run_id, paths),
        "actions": {
```

**3c. 同文件**新增辅助函数 `_ai_review_for_run`。放在 `build_run_detail` 定义**之前**：
```python
def _ai_review_for_run(run_id: str, paths: WebPaths) -> dict[str, Any] | None:
    """若最近一次 AI 审阅针对的是本 run，则返回其状态/错误，供前端展示。"""
    summary = review_automation.read_last_review_summary(paths.service_dir)
    if summary.get("last_run_id") != run_id:
        return None
    return {
        "status": summary.get("last_status"),
        "error": summary.get("last_error"),
        "at": summary.get("last_run_at"),
    }
```

**自检 3**：
- `review_automation.py` 有新公开函数 `read_last_review_summary`。
- `web.py` 有新函数 `_ai_review_for_run`，且 `build_run_detail` 两个分支都加了 `"ai_review": _ai_review_for_run(run_id, paths),`。

---

### Task 4 —— B + D（前端）：AI 审阅交互反馈 + 失败展示

**文件**：`src/live_clipper/web_static/app.js`

> 背景：`api()`（第 19 行）在 HTTP 非 2xx 或 `payload.ok === false` 时会 **throw**；`post()`（第 695 行）内部会自动 `refreshAll()`，但 **`refreshAll()` 不会重新加载 run detail**（`state.detail`）。所以这里改为直接用 `api()` 自管流程，并在结束后显式 `loadRunDetail` 刷新详情。

**4a. 改造点击处理器里的 AI 审阅分支。**

**当前代码（定位锚点，第 734–738 行）**：
```python
    if (event.target.id === "aiReviewRunBtn" && state.selectedRunId) {
      const result = await post(`/api/runs/${encodeURIComponent(state.selectedRunId)}/ai-review`);
      el("logOutput").textContent = JSON.stringify(result, null, 2);
      toast("AI 审阅已完成或已返回处理结果");
    }
```

**替换为**（注意这是 JS，不是 Python；整段复制）：
```javascript
    if (event.target.id === "aiReviewRunBtn" && state.selectedRunId) {
      const button = event.target;
      const runId = state.selectedRunId;
      button.disabled = true;
      button.textContent = "AI 审阅中…（约 1 分钟）";
      try {
        const result = await api(`/api/runs/${encodeURIComponent(runId)}/ai-review`, { method: "POST" });
        el("logOutput").textContent = JSON.stringify(result, null, 2);
        toast(`AI 审阅完成，已选 ${result.selected_count ?? "?"} 个片段`);
      } catch (err) {
        el("logOutput").textContent = String(err && err.message ? err.message : err);
        toast(`AI 审阅失败：${err && err.message ? err.message : err}`);
      } finally {
        await refreshAll();
        if (state.selectedRunId === runId) await loadRunDetail(runId);
      }
    }
```

> 说明：成功时 `api` 返回选片结果并 toast 成功；失败时 `api` 抛出、被本地 `catch` 接住并 toast 明确错误（不再冒泡到外层 catch）；`finally` 里先 `refreshAll()` 再 `loadRunDetail()` 重载详情，让卡片显示新阶段或失败原因。按钮文案无需手动还原——`loadRunDetail` 会用最新数据重渲染整张卡片。

**4b. 在待审阅卡片上显示上次 AI 审阅失败原因。**

**当前代码（定位锚点，`renderReviewContent`，第 275–288 行）**：
```javascript
function renderReviewContent(detail) {
  const run = detail.run || {};
  const candidates = run.candidate_count || detail.candidates_count || 0;
  return `
    <div class="clip-card-body">
      <div class="clip-actions">
        <p class="muted" style="flex: 1;">AI 已找到 <strong>${escapeHtml(candidates)}</strong> 个候选片段，审阅后即可渲染成片。</p>
        <button class="secondary-button small" data-copy-text="${escapeHtml(run.run_dir || "")}" type="button">复制审阅包路径</button>
        <button id="aiReviewRunBtn" class="primary-button small" type="button" ${detail.actions?.can_ai_review ? "" : "disabled"}>立即 AI 审阅</button>
        <button id="renderRunBtn" class="secondary-button small" type="button" ${detail.actions?.can_render ? "" : "disabled"}>渲染</button>
      </div>
    </div>
  `;
}
```

**替换为**：
```javascript
function renderReviewContent(detail) {
  const run = detail.run || {};
  const candidates = run.candidate_count || detail.candidates_count || 0;
  const aiReview = detail.ai_review;
  const aiError = aiReview && aiReview.status === "failed"
    ? `<div class="notice error" style="margin-bottom: 12px;">上次 AI 审阅失败：${escapeHtml(aiReview.error || "未知错误")}</div>`
    : "";
  return `
    <div class="clip-card-body">
      ${aiError}
      <div class="clip-actions">
        <p class="muted" style="flex: 1;">AI 已找到 <strong>${escapeHtml(candidates)}</strong> 个候选片段，审阅后即可渲染成片。</p>
        <button class="secondary-button small" data-copy-text="${escapeHtml(run.run_dir || "")}" type="button">复制审阅包路径</button>
        <button id="aiReviewRunBtn" class="primary-button small" type="button" ${detail.actions?.can_ai_review ? "" : "disabled"}>立即 AI 审阅</button>
        <button id="renderRunBtn" class="secondary-button small" type="button" ${detail.actions?.can_render ? "" : "disabled"}>渲染</button>
      </div>
    </div>
  `;
}
```

**自检 4**：
- AI 审阅分支用的是 `api(...)` + 本地 try/catch/finally，不再是 `post(...)`。
- 删除了「AI 审阅已完成或已返回处理结果」这句话。
- `renderReviewContent` 会在 `detail.ai_review.status === "failed"` 时渲染红色 notice。
- `.notice.error` 是样式表已有的类（无需新增 CSS；若不存在再看 Task 5 说明）。

---

### Task 5 —— E：`run_service_once` NAS 扫描崩溃韧性

**文件**：`src/live_clipper/service.py`

**当前代码（定位锚点，第 717–745 行）**：
```python
def run_service_once(settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    validate_service_settings(settings)
    ensure_dir(service_dir)
    runs = load_runs(service_dir)
    for run in runs:
        reconcile_run(run, settings, service_dir=service_dir)

    started = []
    known = _known_fingerprints(runs)
    for source_path in scan_recording_source(settings.recording_source_default):
        identity = build_run_identity(
            settings.recording_source_default.source_id,
            source_path,
            output_root=settings.recording_source_default.output_root,
        )
        if identity["fingerprint"] in known:
            continue
        run = _start_run_for_source(source_path, settings=settings, service_dir=service_dir)
        runs.append(run)
        started.append(run)
        known.add(run["fingerprint"])

    save_runs(runs, service_dir)
    return {
        "ok": True,
        "known_runs": len(runs),
        "started_runs": len(started),
        "service_dir": str(service_dir),
    }
```

**替换为**（整段复制）：
```python
def run_service_once(settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    validate_service_settings(settings)
    ensure_dir(service_dir)
    runs = load_runs(service_dir)
    for run in runs:
        reconcile_run(run, settings, service_dir=service_dir)
    # 先把 reconcile 结果落盘：即使随后扫描录播源失败（如 NAS 未挂载），
    # 也不能丢掉状态推进。
    save_runs(runs, service_dir)

    started = []
    scan_error: str | None = None
    known = _known_fingerprints(runs)
    try:
        sources = scan_recording_source(settings.recording_source_default)
    except FileNotFoundError as exc:
        sources = []
        scan_error = str(exc)
        append_event(service_dir, "recording_source_unavailable", source_dir=str(exc))
    for source_path in sources:
        identity = build_run_identity(
            settings.recording_source_default.source_id,
            source_path,
            output_root=settings.recording_source_default.output_root,
        )
        if identity["fingerprint"] in known:
            continue
        run = _start_run_for_source(source_path, settings=settings, service_dir=service_dir)
        runs.append(run)
        started.append(run)
        known.add(run["fingerprint"])

    if started:
        save_runs(runs, service_dir)
    return {
        "ok": True,
        "known_runs": len(runs),
        "started_runs": len(started),
        "scan_error": scan_error,
        "service_dir": str(service_dir),
    }
```

**改动要点**（理解用，不写进代码）：
- reconcile 后立即 `save_runs` 一次 → 扫描崩溃也不丢状态。
- 扫描包 `try/except FileNotFoundError` → 降级为 `recording_source_unavailable` 事件 + 返回 `scan_error`，不再抛出。
- 新增 run 时再 `save_runs` 一次。

**自检 5**：
- reconcile 循环后有一处 `save_runs(runs, service_dir)`。
- 扫描被 `try/except FileNotFoundError` 包裹，except 里 `append_event(..., "recording_source_unavailable", ...)`。
- 返回值新增 `"scan_error"` 键。

---

### Task 6 —— 测试

> 现有测试写法可参考：`tests/test_models.py`、`tests/test_codex_selection.py`、`tests/test_service.py`、`tests/test_web_review_automation.py`。

**6a. 文件**：`tests/test_models.py` 末尾追加：
```python
def test_selected_clip_accepts_object_remove_ranges():
    from live_clipper.models import SelectedClip

    clip = SelectedClip(
        clip_id="w0001-c001",
        source_start=0.0,
        source_end=30.0,
        title="t",
        remove_ranges=[{"start": 4.0, "end": 6.0}],
    )
    assert clip.remove_ranges == [(4.0, 6.0)]


def test_selected_clip_accepts_array_remove_ranges():
    from live_clipper.models import SelectedClip

    clip = SelectedClip(
        clip_id="w0001-c001",
        source_start=0.0,
        source_end=30.0,
        title="t",
        remove_ranges=[[4.0, 6.0]],
    )
    assert clip.remove_ranges == [(4.0, 6.0)]
```

**6b. 文件**：`tests/test_service.py` 末尾追加：
```python
def test_run_service_once_persists_reconcile_when_scan_source_missing(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "recording__abc123"
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    write_json(service_dir / "runs.json", {
        "runs": [
            {
                "run_id": "recording__abc123",
                "run_dir": str(run_dir),
                "phase": "processing",
                "pid": None,
            }
        ]
    })

    def fake_scan(config):
        raise FileNotFoundError("/Volumes/nas/missing")

    monkeypatch.setattr(service, "scan_recording_source", fake_scan)

    report = service.run_service_once(Settings(), service_dir=service_dir)

    assert report["ok"] is True
    assert report["scan_error"] == "/Volumes/nas/missing"
    saved = read_json(service_dir / "runs.json")["runs"]
    assert saved[0]["phase"] == "needs_review"
    events = (service_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "recording_source_unavailable" in events
```

**6c. 文件**：`tests/test_web_review_automation.py` 末尾追加一个测试，验证 `build_run_detail` 会带出 AI 审阅失败信息。请先阅读该文件顶部已有的 `_paths` / `_write_run` 等辅助函数写法并复用它们；测试主体逻辑为：

- 用现有辅助构造一个处于 `needs_review` 的 run；
- 往 `paths.service_dir` 写一个 `review_automation.json`（用 `write_json` + `review_automation._summary_path(paths.service_dir)`），内容 `{"last_run_id": <run_id>, "last_status": "failed", "last_error": "boom", "last_run_at": "..."}`；
- 调 `web.build_run_detail(run_id, paths)`，断言 `detail["ai_review"]["status"] == "failed"` 且 `detail["ai_review"]["error"] == "boom"`；
- 再写一个 `last_run_id` 为**别的** run 的 summary，断言此时 `detail["ai_review"] is None`。

> 若该文件缺少可复用的 run 构造辅助，可参考 `test_post_api_run_ai_review_executes_and_writes_selection` 的写法自行构造最小 run 目录与 `runs.json`。

**自检 6**：新增测试函数名不与现有重复；`pytest` 能收集到。

---

## 4. 数据契约（前后端约定）

- `GET /api/runs/{run_id}` 返回体新增顶层字段 `ai_review`：
  - 当最近一次 AI 审阅的 `last_run_id` 等于本 run 时，为 `{"status": "...", "error": "...", "at": "..."}`；
  - 否则为 `null`。
- `POST /api/runs/{run_id}/ai-review` 契约不变：成功 `{"ok": true, "selected_count": N, ...}`；失败 HTTP 400 + `{"ok": false, "message": "...", ...}`（`api()` 会据此 throw）。

---

## 5. 任务依赖

```
Task 1 (C 校验器) ─┐
Task 2 (C 提示词) ─┴─> 使 codex 输出能通过校验
Task 3 (D 后端 ai_review) ──> Task 4 (B+D 前端)
Task 5 (E NAS 韧性)  独立
Task 6 (测试) 覆盖 1/3/5
```
建议顺序：1 → 2 → 3 → 4 → 5 → 6。

---

## 6. 验收标准（DoD）

1. `SelectedClip` 同时接受 `[{"start":s,"end":e}]` 与 `[[s,e]]`，都归一化为 `[(s,e)]`。
2. 两处提示词都写明 `remove_ranges` 格式。
3. 前端点「立即 AI 审阅」：按钮立刻置灰显示「AI 审阅中…」；成功 toast「已选 N 个片段」；失败 toast 明确错误；无「已完成或已返回处理结果」这类模糊文案。
4. AI 审阅失败后，待审阅卡片顶部红字显示上次失败原因。
5. `run_service_once` 在扫描源缺失时不抛异常、reconcile 结果已落盘、返回含 `scan_error`、事件含 `recording_source_unavailable`。
6. 新增测试通过；**原有测试无一回退**。
7. 未触碰第 10 节红线。

---

## 7. 人工端到端验证（Codex 无需自动跑，交付后由人执行）

```bash
# 1. 重启 Web 控制台加载新代码（若在跑）
# 2. 打开 http://127.0.0.1:8765 → 切片结果 → 点开待审阅任务 → 立即 AI 审阅
#    预期：按钮变「AI 审阅中…」；约 1 分钟后成功出选片并进入渲染，或红字显示失败原因
```

---

## 8. 必须运行的验证命令（输出贴到交付说明）

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_models.py tests/test_codex_selection.py tests/test_service.py tests/test_web_review_automation.py -q

# 静态确认 C 生效
.venv/bin/python -c "from live_clipper.models import SelectedClip; c=SelectedClip(clip_id='w0001-c001',source_start=0.0,source_end=30.0,title='t',remove_ranges=[{'start':4.0,'end':6.0}]); print(c.remove_ranges)"
# 期望输出：[(4.0, 6.0)]
```

---

## 9. 参考：本会话已实测过的证据（帮助你理解，不用改）

- 修复 A 后 codex 真机跑通（62 秒），产出选片；但校验因 `remove_ranges.0` 期望 tuple、实际收到 `{'start':4911.18,'end':4921.42}` 而失败 —— 即 Task 1 要解决的。
- 失败 summary 已被写入 `work/service/review_automation.json`（`last_status="failed"`、`last_error` 含该 pydantic 报错）—— 即 Task 3 要暴露的。

---

## 10. 红线（绝对不要做）

1. **不要**改 `_default_local_runner` 里 codex/claude 的调用方式（A 已修复）。
2. **不要**改 `SelectedClip.remove_ranges` 的字段类型（保持 `list[tuple[float, float]]`），只加 `mode="before"` 校验器。
3. **不要**改 `tests/test_web_v8_redesign.py` 的 `assert len(fields) == 49`。
4. **不要**新增 Web API 端点、路由、鉴权。
5. **不要**把 AI 审阅/渲染改成异步/后台任务（P2 再做）。
6. **不要**引入新第三方依赖。
7. **不要**改 `input/`、`output/`、`work/` 下用户数据文件。

---

## 11. 交付说明模板（Codex 完成后填写）

- [ ] Task 1–6 完成，逐条自检通过
- [ ] `pytest -q` 全绿（贴末尾统计行）
- [ ] `test_web_v8_redesign.py` 仍通过（未碰 49 断言）
- [ ] 静态验证命令输出 `[(4.0, 6.0)]`
- [ ] 未触碰红线文件/约束
- [ ] 附本次 `git diff --stat`
