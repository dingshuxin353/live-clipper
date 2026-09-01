import { useCallback, useEffect, useRef, useState } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { Onboarding } from "./Onboarding";
import { ClipsPage } from "./ClipsPage";
import { ResourcesPage, ReviewCompatibilityPage, SettingsPage } from "./CompatibilityPages";
import { NewProjectDialog } from "./ProjectDialogs";
import { MigrationFlow } from "./features/migration/MigrationFlow";
import { ObjectNotFound, ProjectPage, RunPage } from "./ProjectDetail";
import { projectApi } from "./project-api";
import type { OnboardingSession, OnboardingSnapshot } from "./project-dto";
import { ProjectsPage, StudioPage } from "./StudioProjects";
import { usePolling } from "./workbench-shared";

function NavLink({ to, active, children }: { to: string; active: boolean; children: React.ReactNode }) { return <Link className={active ? "active" : ""} to={to}>{children}</Link>; }

function WorkbenchShell({ onboarding, openOnboarding, resumeTriggerRef }: { onboarding: OnboardingSnapshot | null; openOnboarding(): Promise<void>; resumeTriggerRef: React.RefObject<HTMLButtonElement | null> }) {
  const location = useLocation(); const navigate = useNavigate(); const [notice, setNotice] = useState(""); const [params] = useSearchParams();
  const studioState = usePolling((signal) => projectApi.studio(signal), 15000, "studio-navigation"); const unseenCount = studioState.data?.unseen_result_count ?? 0;
  const paused = onboarding?.session?.state === "paused";
  const resultRoute = /\/projects\/[^/]+\/runs\/[^/]+$/.test(location.pathname) && ["result", "materials"].includes(params.get("view") ?? "");
  useEffect(() => { document.body.classList.toggle("in-app-shell", Boolean(window.liveClipperShell)); }, []);
  useEffect(() => {
    if (!(location.state as { focusProjectHeading?: boolean } | null)?.focusProjectHeading) return;
    const root = document.querySelector(".main-content");
    if (!root) return;
    const focusHeading = () => {
      const heading = root.querySelector<HTMLElement>(".page-heading h1");
      if (!heading) return false;
      heading.tabIndex = -1; heading.focus(); return true;
    };
    if (focusHeading()) return;
    const observer = new MutationObserver(() => { if (focusHeading()) observer.disconnect(); });
    observer.observe(root, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [location.key, location.state]);
  useEffect(() => { const refreshResults = () => void studioState.refresh(); window.addEventListener("venus-results-changed", refreshResults); return () => window.removeEventListener("venus-results-changed", refreshResults); }, [studioState.refresh]);
  const openCreate = () => { if (paused) { void openOnboarding(); return; } const next = new URLSearchParams(location.search); next.set("dialog", "new-project"); navigate({ pathname: location.pathname, search: next.toString() }); };
  return <div className="workbench-shell"><header className="top-navigation"><Link className="brand" to="/studio"><img src="/static/venus-mark.png" alt="" /><span><strong>Venus</strong><small>直播内容工作台</small></span></Link><nav aria-label="主导航"><NavLink to="/studio" active={location.pathname === "/studio"}>工作室</NavLink><NavLink to="/projects" active={location.pathname.startsWith("/projects") && !resultRoute}>项目</NavLink><NavLink to="/clips" active={location.pathname === "/clips" || resultRoute}>成片{unseenCount > 0 && <span className="nav-badge" aria-label={`${unseenCount} 条新成片`}>{unseenCount}</span>}</NavLink><NavLink to="/resources" active={location.pathname === "/resources"}>资源</NavLink><NavLink to="/settings" active={location.pathname === "/settings"}>设置</NavLink></nav><button className="button primary" onClick={openCreate}>{paused ? "继续首次设置" : "＋ 新建项目"}</button></header>
    <main className="main-content"><Routes><Route path="/" element={<Navigate to="/studio" replace />} /><Route path="/studio" element={<StudioPage notify={setNotice} state={studioState} onboarding={paused ? onboarding : null} resumeOnboarding={openOnboarding} resumeTriggerRef={resumeTriggerRef} />} /><Route path="/projects" element={<ProjectsPage />} /><Route path="/projects/:projectId" element={<ProjectPage notify={setNotice} />} /><Route path="/projects/:projectId/runs/:runId" element={<RunPage />} /><Route path="/clips" element={<ClipsPage />} /><Route path="/review" element={<ReviewCompatibilityPage />} /><Route path="/resources" element={<ResourcesPage />} /><Route path="/settings" element={<SettingsPage notify={setNotice} />} /><Route path="/not-found/object" element={<ObjectNotFound type="对象" />} /><Route path="*" element={<NotFound />} /></Routes></main>
    {!paused && params.get("dialog") === "new-project" && <NewProjectDialog notify={setNotice} />}{notice && <div className="toast" role="status"><span>{notice}</span><button aria-label="关闭通知" onClick={() => setNotice("")}>×</button></div>}
  </div>;
}

function StartupGate() {
  const navigate = useNavigate(); const [snapshot, setSnapshot] = useState<OnboardingSnapshot | null>(null);
  const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [onboardingOpen, setOnboardingOpen] = useState(false);
  const loadRevision = useRef(0); const loadController = useRef<AbortController | null>(null); const resumeTriggerRef = useRef<HTMLButtonElement>(null); const restoreResumeFocus = useRef(false);
  const load = useCallback(async () => {
    loadController.current?.abort();
    const revision = ++loadRevision.current; const controller = new AbortController(); loadController.current = controller; setLoading(true); setError("");
    try {
      let next = await projectApi.onboarding(controller.signal);
      assertStartupSnapshot(next);
      if (next.entry.mode === "onboarding" && next.entry.onboarding === "new" && !next.session) {
        const started = await projectApi.onboardingStart(controller.signal);
        next = { ...next, session: started.session, entry: { ...next.entry, onboarding: "resume" } };
      }
      if (revision !== loadRevision.current) return;
      setSnapshot(next); setOnboardingOpen(next.entry.mode === "onboarding" && next.session?.state !== "paused");
      if (next.entry.mode === "onboarding" && next.session?.state === "paused") navigate("/studio", { replace: true });
    } catch (caught) {
      if (controller.signal.aborted || revision !== loadRevision.current) return;
      setError(caught instanceof Error ? caught.message : "暂时无法确认数据状态");
    } finally { if (revision === loadRevision.current) { loadController.current = null; setLoading(false); } }
  }, [navigate]);
  useEffect(() => { void load(); return () => { loadRevision.current += 1; loadController.current?.abort(); loadController.current = null; }; }, [load]);

  const updateSession = useCallback((session: OnboardingSession) => {
    setSnapshot((current) => current ? { ...current, session, entry: { ...current.entry, mode: session.state === "completed" ? "workbench" : "onboarding", onboarding: session.state === "completed" ? null : session.state === "paused" ? "paused" : session.state === "activation_pending" ? "activation_pending" : "resume" } } : current);
  }, []);
  const refreshSnapshot = useCallback(async () => { const next = await projectApi.onboarding(); setSnapshot(next); return next; }, []);
  const pauseComplete = useCallback((session: OnboardingSession) => { restoreResumeFocus.current = true; updateSession(session); setOnboardingOpen(false); navigate("/studio", { replace: true }); }, [navigate, updateSession]);
  const resume = useCallback(async () => { const session = snapshot?.session; if (!session) return; const resumed = await projectApi.onboardingResume(session.revision); updateSession(resumed.session); setOnboardingOpen(true); }, [snapshot?.session, updateSession]);
  useEffect(() => {
    if (onboardingOpen || snapshot?.session?.state !== "paused" || !restoreResumeFocus.current) return;
    restoreResumeFocus.current = false; resumeTriggerRef.current?.focus();
  }, [onboardingOpen, snapshot?.session?.state]);

  const enterMigratedProject = useCallback((projectId: string) => {
    setSnapshot((current) => current ? { ...current, migration: null, entry: { ...current.entry, mode: "workbench", onboarding: null } } : current);
    navigate(`/projects/${projectId}`, { replace: true, state: { focusProjectHeading: true } });
  }, [navigate]);

  if (loading) return <div className="startup-gate" role="status"><img src="/static/venus-mark.png" alt="" /><strong>正在准备 Venus</strong><span>正在确认本机数据状态…</span></div>;
  if (!snapshot || error) return <div className="startup-gate startup-error" role="alert"><strong>暂时无法确认数据状态</strong><span>{error || "请稍后重试。"}</span><button className="button primary" onClick={() => void load()}>重试</button></div>;
  if (snapshot.entry.mode === "migration_required" && !snapshot.migration) return <SafetyBoundary title="暂时无法读取升级状态" description="为保护现有数据，Venus 已停止进入工作台。请重试启动或使用问题编号诊断。" diagnosticId={snapshot.entry.reason_code} />;
  if (snapshot.entry.mode === "migration_required" && snapshot.migration) return <MigrationFlow startup={snapshot.migration} onEnter={enterMigratedProject} />;
  if (snapshot.entry.mode === "workbench" && snapshot.migration?.entry === "completed" && !snapshot.migration.report?.acknowledged_at) return <MigrationFlow startup={snapshot.migration} onEnter={enterMigratedProject} />;
  if (snapshot.entry.mode === "diagnostic_required") return <SafetyBoundary title="数据状态需要检查" description="为保护现有数据，Venus 已停止首次设置。请使用问题编号联系诊断。" diagnosticId={snapshot.entry.reason_code} />;
  return <><WorkbenchShell onboarding={snapshot.session?.state === "paused" ? snapshot : null} openOnboarding={resume} resumeTriggerRef={resumeTriggerRef} />{onboardingOpen && snapshot.session && <Onboarding snapshot={snapshot} onSession={updateSession} onRefresh={refreshSnapshot} onPaused={pauseComplete} onClose={() => setOnboardingOpen(false)} />}</>;
}

function assertStartupSnapshot(snapshot: OnboardingSnapshot): void {
  const modes = new Set(["onboarding", "workbench", "migration_required", "diagnostic_required"]);
  const onboardingStates = new Set(["new", "resume", "paused", "activation_pending"]);
  if (!snapshot || snapshot.ok !== true || !snapshot.entry || !modes.has(snapshot.entry.mode)) throw new Error("首次设置状态无法识别");
  if (snapshot.entry.mode === "onboarding" && !onboardingStates.has(snapshot.entry.onboarding ?? "")) throw new Error("首次设置状态无法识别");
  if (snapshot.entry.mode !== "onboarding" && snapshot.entry.onboarding !== null) throw new Error("首次设置状态无法识别");
  if (snapshot.entry.mode === "migration_required" && !snapshot.migration) throw new Error("升级状态无法识别");
}

function SafetyBoundary({ title, description, diagnosticId }: { title: string; description: string; diagnosticId: string | null }) { return <div className="startup-gate startup-safety"><img src="/static/venus-mark.png" alt="" /><strong>{title}</strong><span>{description}</span>{diagnosticId && <small>问题编号：{diagnosticId}</small>}</div>; }
function NotFound() { return <section className="page"><div className="empty-state"><strong>找不到这个页面</strong><p>请从工作室或项目入口继续。</p><Link className="button primary" to="/studio">返回工作室</Link></div></section>; }
function AppShell() { return <StartupGate />; }
export function App() { return <BrowserRouter><AppShell /></BrowserRouter>; }
