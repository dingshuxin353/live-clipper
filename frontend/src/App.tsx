import { useEffect, useState } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { Onboarding } from "./Onboarding";
import { ResourcesPage, ReviewCompatibilityPage, SettingsPage } from "./CompatibilityPages";
import { NewProjectDialog } from "./ProjectDialogs";
import { ObjectNotFound, ProjectPage, RunPage } from "./ProjectDetail";
import { projectApi } from "./project-api";
import { ProjectsPage, StudioPage } from "./StudioProjects";
import { usePolling } from "./workbench-shared";

function NavLink({ to, active, children }: { to: string; active: boolean; children: React.ReactNode }) { return <Link className={active ? "active" : ""} to={to}>{children}</Link>; }

function AppShell() {
  const location = useLocation(); const navigate = useNavigate(); const [notice, setNotice] = useState(""); const [params] = useSearchParams();
  const studioState = usePolling((signal) => projectApi.studio(signal), 15000, "studio-navigation"); const pendingCount = studioState.data?.pending_review_count ?? 0;
  useEffect(() => { document.body.classList.toggle("in-app-shell", Boolean(window.liveClipperShell)); }, []);
  const openCreate = () => { const next = new URLSearchParams(location.search); next.set("dialog", "new-project"); navigate({ pathname: location.pathname, search: next.toString() }); };
  return <div className="workbench-shell"><header className="top-navigation"><Link className="brand" to="/studio"><img src="/static/venus-mark.png" alt="" /><span><strong>Venus</strong><small>直播内容工作台</small></span></Link><nav aria-label="主导航"><NavLink to="/studio" active={location.pathname === "/studio"}>工作室</NavLink><NavLink to="/projects" active={location.pathname.startsWith("/projects")}>项目</NavLink><NavLink to="/review" active={location.pathname === "/review"}>待审{pendingCount > 0 && <span className="nav-badge" aria-label={`${pendingCount} 条待审`}>{pendingCount}</span>}</NavLink><NavLink to="/resources" active={location.pathname === "/resources"}>资源</NavLink><NavLink to="/settings" active={location.pathname === "/settings"}>设置</NavLink></nav><button className="button primary" onClick={openCreate}>＋ 新建项目</button></header>
    <main className="main-content"><Routes><Route path="/" element={<Navigate to="/studio" replace />} /><Route path="/studio" element={<StudioPage notify={setNotice} state={studioState} />} /><Route path="/projects" element={<ProjectsPage />} /><Route path="/projects/:projectId" element={<ProjectPage notify={setNotice} />} /><Route path="/projects/:projectId/runs/:runId" element={<RunPage />} /><Route path="/review" element={<ReviewCompatibilityPage pendingCount={pendingCount} />} /><Route path="/resources" element={<ResourcesPage />} /><Route path="/settings" element={<SettingsPage notify={setNotice} />} /><Route path="/not-found/object" element={<ObjectNotFound type="对象" />} /><Route path="*" element={<NotFound />} /></Routes></main>
    {params.get("dialog") === "new-project" && <NewProjectDialog notify={setNotice} />}{notice && <div className="toast" role="status"><span>{notice}</span><button aria-label="关闭通知" onClick={() => setNotice("")}>×</button></div>}<Onboarding notify={setNotice} />
  </div>;
}

function NotFound() { return <section className="page"><div className="empty-state"><strong>找不到这个页面</strong><p>请从工作室或项目入口继续。</p><Link className="button primary" to="/studio">返回工作室</Link></div></section>; }
export function App() { return <BrowserRouter><AppShell /></BrowserRouter>; }
