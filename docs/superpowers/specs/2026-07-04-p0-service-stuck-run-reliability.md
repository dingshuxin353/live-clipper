# P0 Spec：修复常驻服务「任务永久卡在处理中」的可靠性 Bug

- 版本：P0（可靠性）
- 日期：2026-07-04
- 目标执行者：Codex（请严格逐字照做，不要自行发挥或推理未写明的改动）
- 预计改动文件：`src/live_clipper/service.py`、`src/live_clipper/config.py`、`src/live_clipper/web.py`、`src/live_clipper/web_static/app.js`、`src/live_clipper/web_static/styles.css`、`tests/test_service.py`（新增测试）
- **禁止改动**的文件见第 9 节「红线」。

---

## 0. 给 Codex 的阅读须知（先读这段）

1. 本文件里凡是给出「新代码」的地方，**请整段复制粘贴**，不要凭记忆重写、不要「顺手优化」。
2. 凡是标注「当前代码（用于定位）」的，是让你在文件里找到对应位置的锚点，**不要把锚点本身也粘贴进去**。
3. 每个任务结束都有「自检」，做完一个任务就按自检核对一次再做下一个。
4. 全部做完后，必须运行第 8 节的验证命令，并把输出贴到最终交付说明里。
5. 不确定的地方，**保持本文件写明的做法**，不要引入本文件没提到的第三方库、配置项、API 端点或文件。
6. 遵守仓库 `AGENTS.md` 的写入边界：不要改 `input/`、`output/`、`work/` 下的用户数据文件（除第 10 节明确的恢复步骤外）。

---

## 1. 背景与问题现象

`live-clipper` 是本地直播录播自动切片工具。常驻服务（`live-clipper service`）会周期性扫描录播源、为新录播启动流水线子进程（提取音频 → ASR → 生成候选），流水线跑完后，服务应把该任务（run）的状态从 `processing` 推进到 `needs_review`（待审阅），Web 控制台随之解锁「AI 审阅 / 渲染」等操作。

**现象**：CLI 脚本能跑通，但 Web UI 上任务永远停在「处理中」，导致后续所有操作按钮都不出现，用户感觉「UI 很多功能跑不通」。

**实测证据**（2026-07-04 复现）：

- `work/service/runs.json` 里 run `2026-06-27-21-00-16__c3820cf1` 的 `phase` 从 2026-07-01 起一直是 `processing`，`pid` 为 `46639`。
- `ps -p 46639` 显示该进程状态为 `<defunct>`（僵尸进程）。
- 该任务的流水线其实**早已成功**：`output/default/2026-06-27-21-00-16__c3820cf1/` 下 `codex_brief.json`、`merged_candidates.json`（78 条候选）、`selected_clips.template.json` 全部已生成。
- 任务日志尾部为 `[流水线] 阶段完成: 已生成候选包, 下一步审阅 codex_brief.json 并写入 selected_clips.json`，说明流水线正常收尾。

---

## 2. 根因分析（务必理解，再动手）

1. `src/live_clipper/service.py` 的 `_start_pipeline_process()`（约 502–528 行）用 `subprocess.Popen(..., start_new_session=True)` 启动流水线子进程，**只返回 `process.pid`，丢弃了 `Popen` 句柄，之后从不 `wait()`/`poll()`**。
2. 子进程执行完毕后，因为父进程（常驻服务）从未回收它，它变成**僵尸进程（defunct）**——进程已死，但进程表项还在。
3. `pid_is_running()`（约 30–37 行）用 `os.kill(pid, 0)` 判活。**对僵尸进程，`os.kill(pid, 0)` 不会抛 `ProcessLookupError`，因此返回 `True`（误判为「还在运行」）。**
4. `reconcile_run()`（约 543 行）第一步就是：若 `pid_is_running(pid)` 为真则 `return False`，不做任何状态推进。于是 run 永远停在 `processing`。

**核心修复思路**：常驻服务是这些流水线子进程的父进程，应当在判活时先用**非阻塞的 `os.waitpid(pid, os.WNOHANG)` 回收（reap）已退出的子进程**。回收后僵尸消失，`pid_is_running` 即可正确返回「未运行」，`reconcile_run` 随后把 run 推进到 `needs_review`。

此外再加一层**防御性兜底**：即使进程仍疑似存活，只要流水线产物已经生成（`codex_brief.json` 存在）且该 run 停在 `processing` 超过阈值时间，也强制推进，避免任何未来的同类判活失误再次把任务无声卡死。

---

## 3. 目标与非目标

### 目标（本次必须做）
1. 修 `pid_is_running`，使其能正确回收并识别僵尸子进程。
2. `reconcile_run` 增加「卡死兜底」逻辑：产物已就绪 + 超时 → 强制推进，不再盲信 pid。
3. 新增可配置阈值 `service.stuck_after_minutes`（TOML 可配，默认 180 分钟）。
4. Web UI 的任务卡片：对「处理中且超过阈值」的 run 显示一条**警告提示**（非阻塞、仅提示）。
5. 补齐回归测试。

### 非目标（本次绝对不要做）
- 不重构 `subprocess.Popen` 为进程池 / asyncio / 第三方进程管理库。
- 不改动流水线本身、ASR、渲染逻辑。
- 不给 Web 加鉴权、不统一 API 错误格式（那是后续 P2）。
- 不新增任何 Web API 端点。
- **不**把 `stuck_after_minutes` 加进 Web 配置编辑表单（避免触发第 9 节的字段数量约束）。

---

## 4. 任务分解（按顺序执行）

### Task 1 —— 修复 `pid_is_running`（回收僵尸子进程）

**文件**：`src/live_clipper/service.py`

**当前代码（用于定位，约 30–37 行）**：
```python
def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
```

**替换为**（整段复制）：
```python
def pid_is_running(pid: int) -> bool:
    """判断 pid 是否为存活进程。

    常驻服务会以子进程方式启动流水线（见 _start_pipeline_process）。子进程结束后，
    在父进程回收（reap）之前会变成僵尸（defunct）进程；此时 os.kill(pid, 0) 仍会
    报告其「存活」，这正是导致 run 永久卡在 "processing" 的根因。

    因此这里先用非阻塞 waitpid 尝试回收：如果它是本进程的子进程且已退出，waitpid
    会返回它的 pid 并清除僵尸，从而可以正确报告「未运行」。若它不是本进程的子进程
    （例如服务重启后），waitpid 抛 ChildProcessError，退回到 os.kill 信号探测。
    """
    try:
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        # 不是本进程的子进程（如服务已重启），退回信号探测。
        pass
    except OSError:
        # 探测时出现意外错误：视为未运行，避免把 run 永久卡死。
        return False
    else:
        # reaped_pid == pid -> 子进程已退出并被回收；reaped_pid == 0 -> 仍在运行。
        return reaped_pid != pid

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
```

**要点说明（帮助你理解，不用写进代码）**：
- `os.waitpid(pid, os.WNOHANG)` 对「仍在运行的本进程子进程」返回 `(0, 0)` → `reaped_pid != pid` 为 `True`（存活）。
- 对「已退出的本进程子进程（僵尸）」返回 `(pid, status)` → `reaped_pid == pid` → 返回 `False`（未运行），并顺带清除僵尸。
- `ChildProcessError` 是 `OSError` 的子类，**`except ChildProcessError` 必须写在 `except OSError` 之前**，顺序不能反。

**自检 1**：
- 函数里 `except ChildProcessError` 出现在 `except OSError` 之前。
- 保留了末尾的 `os.kill` 探测分支。
- 没有改动 `os` 的 import（`import os` 已存在，`os.waitpid` / `os.WNOHANG` 无需额外导入）。

---

### Task 2 —— 新增配置项 `service.stuck_after_minutes`

**文件**：`src/live_clipper/config.py`

**2a. 给 `ServiceConfig` 数据类加字段。**

当前代码（用于定位，约 173–178 行）：
```python
@dataclass(frozen=True)
class ServiceConfig:
    enabled: bool = True
    scan_interval_minutes: int = 30
    auto_render_after_selection: bool = True
    cleanup_mode: str = "preview_only"
```

替换为：
```python
@dataclass(frozen=True)
class ServiceConfig:
    enabled: bool = True
    scan_interval_minutes: int = 30
    auto_render_after_selection: bool = True
    cleanup_mode: str = "preview_only"
    stuck_after_minutes: int = 180
```

**2b. 在 `load_settings` 里读取该字段。**

当前代码（用于定位，约 497–502 行）：
```python
        service=ServiceConfig(
            enabled=bool(service_data.get("enabled", True)),
            scan_interval_minutes=int(service_data.get("scan_interval_minutes", 30)),
            auto_render_after_selection=bool(service_data.get("auto_render_after_selection", True)),
            cleanup_mode=str(service_data.get("cleanup_mode", "preview_only")),
        ),
```

替换为：
```python
        service=ServiceConfig(
            enabled=bool(service_data.get("enabled", True)),
            scan_interval_minutes=int(service_data.get("scan_interval_minutes", 30)),
            auto_render_after_selection=bool(service_data.get("auto_render_after_selection", True)),
            cleanup_mode=str(service_data.get("cleanup_mode", "preview_only")),
            stuck_after_minutes=int(service_data.get("stuck_after_minutes", 180)),
        ),
```

**2c.（可选但推荐）在 TOML 模板里补注释。** 如果 `config.py` 里存在内嵌的 `live-clipper.toml` 模板字符串（搜索 `scan_interval_minutes = 30` 所在的模板段，约 46 行附近的 `[service]` 段），在 `cleanup_mode = "preview_only"` 下一行加入：
```toml
# 任务停在「处理中」超过该分钟数且产物已生成时，服务会判定其卡死并强制推进。设为 0 可关闭此兜底。
stuck_after_minutes = 180
```
若找不到该模板段或不确定，**跳过 2c**，只做 2a、2b 即可（不影响功能）。

**自检 2**：
- `ServiceConfig` 有 5 个字段，最后一个是 `stuck_after_minutes: int = 180`。
- `load_settings` 里 `ServiceConfig(...)` 传了 `stuck_after_minutes=...`。
- **没有**去改 `config_editor.py`（见红线）。

---

### Task 3 —— `reconcile_run` 增加卡死兜底 + 新增两个辅助函数

**文件**：`src/live_clipper/service.py`

**3a. 新增两个辅助函数。** 放在 `reconcile_run` 定义**之前**（例如紧接在 `_phase_from_files` 函数之后、`reconcile_run` 之前）。整段复制：

```python
def _parse_timestamp(value: Any) -> datetime | None:
    """把 run 里的时间戳（ISO 字符串或 Unix 秒）解析为带时区的 datetime；失败返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def _run_is_stuck(run: dict[str, Any], settings: Settings) -> bool:
    """run 是否已在当前阶段停留超过 service.stuck_after_minutes 分钟。"""
    threshold_minutes = settings.service.stuck_after_minutes
    if threshold_minutes <= 0:
        return False
    started = _parse_timestamp(run.get("updated_at")) or _parse_timestamp(run.get("created_at"))
    if started is None:
        return False
    return datetime.now(UTC) - started >= timedelta(minutes=threshold_minutes)
```

> 说明：`datetime`、`UTC`、`timedelta`、`Any` 均已在 `service.py` 顶部导入（第 13、15 行），无需新增 import。

**3b. 替换 `reconcile_run` 函数体。**

当前代码（用于定位，约 543–581 行）：
```python
def reconcile_run(run: dict[str, Any], settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> bool:
    old_phase = run.get("phase")
    run_dir = Path(str(run["run_dir"]))
    pid = run.get("pid")
    if isinstance(pid, int) and pid_is_running(pid):
        return False
    if isinstance(pid, int):
        run["pid"] = None

    inferred = _phase_from_files(run_dir)
    if inferred == "rendering" and settings.service.auto_render_after_selection:
        ...
```

整段替换为（复制到 `return changed` 结束为止）：
```python
def reconcile_run(run: dict[str, Any], settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> bool:
    old_phase = run.get("phase")
    run_dir = Path(str(run["run_dir"]))
    pid = run.get("pid")
    inferred = _phase_from_files(run_dir)

    process_running = isinstance(pid, int) and pid_is_running(pid)
    if process_running:
        # 防御性兜底：进程疑似仍存活，但只要流水线产物已生成（inferred 非空）且该 run
        # 停在 processing 已超过阈值，就不再盲信 pid，让卡死/僵尸进程无法再冻结任务。
        if inferred is None or not _run_is_stuck(run, settings):
            return False
        append_event(
            service_dir,
            "stuck_run_recovered",
            run_id=run["run_id"],
            pid=pid,
            inferred_phase=inferred,
        )

    if isinstance(pid, int):
        run["pid"] = None

    if inferred == "rendering" and settings.service.auto_render_after_selection:
        run["phase"] = "rendering"
        append_event(service_dir, "render_started", run_id=run["run_id"], run_dir=str(run_dir))
        render_selected_clips(run_dir / "selected_clips.json")
        cleanup_local_artifacts(
            run_dir,
            input_dir=settings.recording_source_default.input_dir,
            confirm=False,
        )
        run["phase"] = "rendered"
        append_event(service_dir, "render_completed", run_id=run["run_id"], run_dir=str(run_dir))
        append_event(service_dir, "cleanup_preview_created", run_id=run["run_id"], run_dir=str(run_dir))
    elif inferred:
        run["phase"] = inferred
    elif run.get("phase") == "processing":
        run["phase"] = "failed"
        run["last_error"] = "Pipeline stopped before codex_brief.json was created"

    changed = run.get("phase") != old_phase or (isinstance(pid, int) and run.get("pid") is None)
    if changed:
        run["updated_at"] = now_utc()
        append_event(
            service_dir,
            "phase_changed",
            run_id=run["run_id"],
            phase=run["phase"],
            run_dir=str(run_dir),
        )
    return changed
```

**改动要点**（帮助理解，不用写进代码）：
- 原来的早退是「进程存活就 `return False`」。现在改成：进程存活时，**只有**在「产物未生成」或「未超时」的情况下才 `return False`；若「产物已生成且超时」则记录 `stuck_run_recovered` 事件后继续往下推进。
- 其余渲染 / 失败判定逻辑与原来**完全一致**，不要改动。

**自检 3**：
- `reconcile_run` 里 `inferred = _phase_from_files(run_dir)` 在计算 `process_running` **之前**。
- 保留了 `stuck_run_recovered` 事件记录。
- 渲染分支、`elif inferred`、`elif ... == "processing"` 三个分支与原文一字不差。
- `_parse_timestamp` 和 `_run_is_stuck` 定义在 `reconcile_run` 之前。

---

### Task 4 —— 后端在 runs 列表里输出 `stuck` 标记

**文件**：`src/live_clipper/web.py`

目的：让前端不用自己算阈值，直接读后端给的布尔值，保证前后端阈值一致（单一数据源）。

**4a. 新增一个辅助函数。** 放在 `build_runs_index` 定义之前（例如 `_run_summary` 之后）。整段复制：

```python
def _run_looks_stuck(run: dict[str, Any], stuck_after_minutes: int) -> bool:
    """判断某个 run 是否「停在处理中且疑似卡死」，用于前端提示。"""
    if stuck_after_minutes <= 0:
        return False
    if run.get("phase") != "processing":
        return False
    updated_at = run.get("updated_at")
    started = _coerce_timestamp(updated_at) or _coerce_timestamp(run.get("created_at"))
    if started is None:
        return False
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - started >= timedelta(minutes=stuck_after_minutes)


def _coerce_timestamp(value: Any):
    from datetime import UTC, datetime

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None
```

> 备注：`web.py` 顶部当前未导入 `datetime`，所以上面在函数内做了局部 `from datetime import ...`，这是**有意为之**，请照抄，不要改成顶部导入（减少对文件头的改动面）。

**4b. 在 `build_runs_index` 里给每个 run 打上 `stuck` 标记。**

当前代码（用于定位，约 177–202 行）：
```python
def build_runs_index(paths: WebPaths | None = None) -> dict[str, Any]:
    paths = paths or WebPaths()
    if _service_runs_available(paths):
        runs = mcp_tools.list_runs(service_dir=paths.service_dir)["runs"]
        for run in runs:
            run["source_name"] = Path(str(run.get("source_path") or run.get("run_id"))).name
            run["candidate_count"] = _count_candidates(Path(str(run["run_dir"])) / "codex_brief.json")
            run["selected_count"] = _count_candidates(Path(str(run["run_dir"])) / "selected_clips.json")
            clips_dir = Path(str(run["run_dir"])) / "clips"
            run["clip_count"] = len(sorted(clips_dir.glob("*.mp4"))) if clips_dir.exists() else 0
            run["requires_codex"] = run.get("phase") == "needs_review"
        return {
            "ok": True,
            "runs": runs,
            "requires_codex": any(run["requires_codex"] for run in runs),
        }
    runs = []
    if paths.output_root.exists():
        for run_dir in sorted(paths.output_root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
            if run_dir.is_dir():
                runs.append(_run_summary(run_dir, paths))
    return {
        "ok": True,
        "runs": runs,
        "requires_codex": any(run["requires_codex"] for run in runs),
    }
```

替换为：
```python
def build_runs_index(paths: WebPaths | None = None) -> dict[str, Any]:
    paths = paths or WebPaths()
    stuck_after_minutes = _settings_for_paths(paths).service.stuck_after_minutes
    if _service_runs_available(paths):
        runs = mcp_tools.list_runs(service_dir=paths.service_dir)["runs"]
        for run in runs:
            run["source_name"] = Path(str(run.get("source_path") or run.get("run_id"))).name
            run["candidate_count"] = _count_candidates(Path(str(run["run_dir"])) / "codex_brief.json")
            run["selected_count"] = _count_candidates(Path(str(run["run_dir"])) / "selected_clips.json")
            clips_dir = Path(str(run["run_dir"])) / "clips"
            run["clip_count"] = len(sorted(clips_dir.glob("*.mp4"))) if clips_dir.exists() else 0
            run["requires_codex"] = run.get("phase") == "needs_review"
            run["stuck"] = _run_looks_stuck(run, stuck_after_minutes)
        return {
            "ok": True,
            "runs": runs,
            "requires_codex": any(run["requires_codex"] for run in runs),
        }
    runs = []
    if paths.output_root.exists():
        for run_dir in sorted(paths.output_root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
            if run_dir.is_dir():
                summary = _run_summary(run_dir, paths)
                summary["stuck"] = _run_looks_stuck(summary, stuck_after_minutes)
                runs.append(summary)
    return {
        "ok": True,
        "runs": runs,
        "requires_codex": any(run["requires_codex"] for run in runs),
    }
```

**自检 4**：
- 两个分支里每个 run 都有 `run["stuck"] = ...`。
- `_run_looks_stuck` 和 `_coerce_timestamp` 已定义且在 `build_runs_index` 之前。
- `_settings_for_paths` 是文件里已存在的函数（约 71 行），直接调用即可，不要重写。

---

### Task 5 —— 前端任务卡显示「疑似卡住」警告

**文件**：`src/live_clipper/web_static/app.js`

当前代码（用于定位，约 210–225 行，`renderRuns` 内 `list.innerHTML = ...` 这段模板）：
```javascript
  list.innerHTML = state.runs.map((run) => {
    const active = run.run_id === state.selectedRunId;
    const expanded = active && state.detail?.ok;
    return `
      <article class="clip-card ${active ? "active" : ""}">
        <button class="clip-card-main" data-run-id="${escapeHtml(run.run_id)}" type="button">
          <span>
            <span class="run-title">${escapeHtml(run.source_name || run.run_id)}</span>
            <span class="run-meta">${escapeHtml(runMeta(run))}</span>
          </span>
          <span class="status-pill ${escapeHtml(canonicalPhase(run.phase))}">${escapeHtml(labelFor(run.phase || "unknown"))}</span>
        </button>
        ${expanded ? renderRunExpandedContent(state.detail) : ""}
      </article>
    `;
  }).join("");
```

替换为：
```javascript
  list.innerHTML = state.runs.map((run) => {
    const active = run.run_id === state.selectedRunId;
    const expanded = active && state.detail?.ok;
    const stuckNotice = run.stuck
      ? `<div class="run-stuck-notice">⚠️ 已处理较长时间仍未完成，可能已卡住。请点击展开后「查看日志」，或重启本机服务后重试。</div>`
      : "";
    return `
      <article class="clip-card ${active ? "active" : ""} ${run.stuck ? "stuck" : ""}">
        <button class="clip-card-main" data-run-id="${escapeHtml(run.run_id)}" type="button">
          <span>
            <span class="run-title">${escapeHtml(run.source_name || run.run_id)}</span>
            <span class="run-meta">${escapeHtml(runMeta(run))}</span>
          </span>
          <span class="status-pill ${escapeHtml(canonicalPhase(run.phase))}">${escapeHtml(labelFor(run.phase || "unknown"))}</span>
        </button>
        ${stuckNotice}
        ${expanded ? renderRunExpandedContent(state.detail) : ""}
      </article>
    `;
  }).join("");
```

**要点**：`run.stuck` 是 Task 4 后端返回的布尔值。`stuckNotice` 是纯文本提示，不带任何新的按钮或 API 调用（保持 P0 范围）。

**自检 5**：
- `renderRuns` 里引用了 `run.stuck`。
- 提示文案里没有 `onclick`、没有 `fetch`、没有新按钮 id。

---

### Task 6 —— 前端警告样式

**文件**：`src/live_clipper/web_static/styles.css`

在文件末尾追加（复制即可）：
```css
.clip-card.stuck {
  border-color: #f0b429;
}

.run-stuck-notice {
  margin: 8px 12px 12px;
  padding: 8px 12px;
  border-radius: 10px;
  background: #fff8e6;
  color: #8a6d0b;
  font-size: 13px;
  line-height: 1.5;
}
```

> 若这两个颜色与现有设计变量体系（如已有 `--warning` 之类）冲突，可改用现有变量；但**不确定就照抄字面色值**，能用即可。

**自检 6**：追加了 `.clip-card.stuck` 和 `.run-stuck-notice` 两条规则，没有删改已有规则。

---

### Task 7 —— 回归测试

**文件**：`tests/test_service.py`（在文件末尾追加下列测试函数）

> 说明：现有测试大量用 `monkeypatch.setattr(service, "pid_is_running", ...)`，不会受 Task 1 影响。下面新增的测试专门覆盖新行为。`write_json`、`Settings`、`ServiceConfig`、`RecordingSourceDefaultConfig` 等已在该测试文件顶部导入，无需再导。

追加内容：
```python
def test_pid_is_running_reaps_exited_child(monkeypatch):
    def fake_waitpid(pid, flag):
        assert pid == 4321
        assert flag == os.WNOHANG
        return (4321, 0)  # 子进程已退出并被回收

    monkeypatch.setattr(service.os, "waitpid", fake_waitpid)

    assert service.pid_is_running(4321) is False


def test_pid_is_running_reports_live_child(monkeypatch):
    monkeypatch.setattr(service.os, "waitpid", lambda pid, flag: (0, 0))  # 仍在运行

    assert service.pid_is_running(4321) is True


def test_pid_is_running_falls_back_for_non_child(monkeypatch):
    def fake_waitpid(pid, flag):
        raise ChildProcessError

    kills = []

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(service.os, "waitpid", fake_waitpid)
    monkeypatch.setattr(service.os, "kill", fake_kill)

    assert service.pid_is_running(4321) is False
    assert kills == [(4321, 0)]


def test_reconcile_recovers_stuck_run_when_output_ready(tmp_path, monkeypatch):
    run_dir = tmp_path / "output" / "default" / "recording__stuck01"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    # pid 仍被误判为存活（模拟僵尸进程），但产物已生成
    monkeypatch.setattr(service, "pid_is_running", lambda pid: True)
    run = {
        "run_id": "recording__stuck01",
        "source_id": "default",
        "run_dir": str(run_dir),
        "phase": "processing",
        "pid": 4321,
        "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",  # 远超阈值
    }
    settings = Settings(service=ServiceConfig(stuck_after_minutes=180))

    changed = service.reconcile_run(run, settings, service_dir=tmp_path / "service")

    assert changed is True
    assert run["phase"] == "needs_review"
    assert run["pid"] is None
    events = (tmp_path / "service" / "events.jsonl").read_text(encoding="utf-8")
    assert "stuck_run_recovered" in events


def test_reconcile_keeps_processing_when_running_and_not_stuck(tmp_path, monkeypatch):
    run_dir = tmp_path / "output" / "default" / "recording__live01"
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    monkeypatch.setattr(service, "pid_is_running", lambda pid: True)
    run = {
        "run_id": "recording__live01",
        "source_id": "default",
        "run_dir": str(run_dir),
        "phase": "processing",
        "pid": 4321,
        "created_at": service.now_utc(),
        "updated_at": service.now_utc(),  # 刚刚更新，未超时
    }
    settings = Settings(service=ServiceConfig(stuck_after_minutes=180))

    changed = service.reconcile_run(run, settings, service_dir=tmp_path / "service")

    assert changed is False
    assert run["phase"] == "processing"
    assert run["pid"] == 4321


def test_reconcile_disabled_stuck_guard_when_threshold_zero(tmp_path, monkeypatch):
    run_dir = tmp_path / "output" / "default" / "recording__live02"
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    monkeypatch.setattr(service, "pid_is_running", lambda pid: True)
    run = {
        "run_id": "recording__live02",
        "source_id": "default",
        "run_dir": str(run_dir),
        "phase": "processing",
        "pid": 4321,
        "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    settings = Settings(service=ServiceConfig(stuck_after_minutes=0))  # 关闭兜底

    changed = service.reconcile_run(run, settings, service_dir=tmp_path / "service")

    assert changed is False
    assert run["phase"] == "processing"
```

**自检 7**：新增 6 个测试函数，函数名不与现有重复。

---

## 5. 各任务依赖关系

```
Task 1 (pid_is_running)  ─┐
Task 2 (config 字段)      ─┼─> Task 3 (reconcile 兜底) ─> Task 7 (测试)
                          └─> Task 4 (web stuck 标记) ─> Task 5 (前端提示) ─> Task 6 (样式)
```
建议就按 Task 1→7 顺序做。

---

## 6. 数据契约（前后端约定，勿改）

- `GET /api/runs` 返回的每个 run 对象**新增**一个字段：`"stuck": true|false`。
- `stuck` 语义：`phase == "processing"` 且停留时间 ≥ `service.stuck_after_minutes` 分钟。阈值为 0 时永远为 `false`。
- 前端只读该布尔值，不做时间计算。

---

## 7. 验收标准（Definition of Done）

1. `pid_is_running` 对「已退出的子进程」返回 `False`（会回收僵尸），对「仍在运行的子进程」返回 `True`，对「非本进程子进程」退回 `os.kill` 探测。
2. 当流水线产物（`codex_brief.json`）已生成、run 停在 `processing` 超过阈值时，`reconcile_run` 能把它推进到 `needs_review` 并记录 `stuck_run_recovered` 事件。
3. 阈值内、或 `stuck_after_minutes=0` 时，行为与修复前一致（不误伤正常长任务）。
4. `GET /api/runs` 每个 run 带 `stuck` 字段；Web 任务卡对 `stuck=true` 的 run 显示黄色警告条。
5. 新增测试全部通过；**原有测试无一回退**。
6. 未改动第 9 节红线中的任何文件/约束。

---

## 8. 必须运行的验证命令（把输出贴到交付说明）

```bash
# 1. 全量测试（重点确保 test_service.py 与 test_web_v8_redesign.py 全绿）
.venv/bin/python -m pytest -q

# 2. 单独看本次相关测试
.venv/bin/python -m pytest tests/test_service.py -q
.venv/bin/python -m pytest tests/test_web_v8_redesign.py -q

# 3. 静态确认新增字段被正确读取
.venv/bin/python -c "from live_clipper.config import ServiceConfig; print(ServiceConfig().stuck_after_minutes)"
# 期望输出：180
```

若 `.venv/bin/python` 不存在，使用仓库约定的解释器（见 README / AGENTS.md），但**不要**新建虚拟环境。

---

## 9. 红线（绝对不要做）

1. **不要**修改 `src/live_clipper/config_editor.py`。
2. **不要**修改 `tests/test_web_v8_redesign.py` 中 `assert len(fields) == 49` 这一断言，也不要因为加了配置项就去改这个数字——因为本 Spec **没有**把 `stuck_after_minutes` 加入 Web 配置编辑表单。若你发现该断言失败，说明你误改了配置编辑器，请回退。
3. **不要**改 `web.py` 里 `_pid_is_running`（它从 `automation` 导入，服务重启后由服务进程回收僵尸即可，无需在 web 进程重复处理）。
4. **不要**新增任何 Web API 端点、路由、鉴权。
5. **不要**引入新的第三方依赖。
6. **不要**改动 `input/`、`output/`、`work/` 下的任何用户数据文件（第 10 节的恢复步骤除外，且需用户执行）。
7. **不要**把 `os.waitpid` 改成阻塞式（必须带 `os.WNOHANG`）。

---

## 10. 卡住任务的恢复（部署后由用户执行，Codex 无需自动跑）

代码修复合入后，历史上卡住的那条 run 需要让**修复后的服务**跑一次 reconcile 才能恢复：

```bash
# 1. 停止旧服务（它仍持有僵尸子进程）
.venv/bin/live-clipper service stop

# 2. 用修复后的代码跑一次单次协调（会回收僵尸并推进卡住的 run）
.venv/bin/live-clipper service start --once

# 3. 确认那条 run 已从 processing 变为 needs_review
.venv/bin/python -c "from live_clipper.utils import read_json; import json; d=read_json('work/service/runs.json'); print(json.dumps([{'run_id':r['run_id'],'phase':r['phase']} for r in (d['runs'] if isinstance(d,dict) else d)], ensure_ascii=False, indent=2))"

# 4. 确认无误后重新以常驻方式启动
.venv/bin/live-clipper service start
```

预期：`2026-06-27-21-00-16__c3820cf1` 的 `phase` 变为 `needs_review`，Web 控制台该任务出现「AI 审阅 / 渲染」相关操作。

> 说明：`service start --once` 会加载最新代码，因此必须在代码合入后执行；直接手改 `runs.json` 属于下策，除非上述步骤无效才考虑，且需先备份。

---

## 11. 交付说明模板（Codex 完成后请填写）

- [ ] Task 1–7 全部完成，逐条自检通过
- [ ] `pytest -q` 全绿（贴出末尾统计行，例：`XX passed`）
- [ ] `test_web_v8_redesign.py` 仍通过（未触碰 49 字段断言）
- [ ] 未改动第 9 节红线文件
- [ ] 附：本次改动的 `git diff --stat`
