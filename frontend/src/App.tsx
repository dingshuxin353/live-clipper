import { useEffect, useState } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { Onboarding } from "./Onboarding";
import { NewProjectDialog } from "./ProjectDialogs";
import { ObjectNotFound, ProjectPage, RunPage } from "./ProjectDetail";
import { ProjectsPage, StudioPage } from "./StudioProjects";
import { PageHeading } from "./workbench-shared";

function NavLink({ to, active, children }: { to: string; active: boolean; children: React.ReactNode }) { return <Link className={active ? "active" : ""} to={to}>{children}</Link>; }

function AppShell() {
  const location = useLocation(); const navigate = useNavigate(); const [notice, setNotice] = useState(""); const [params] = useSearchParams();
  useEffect(() => { document.body.classList.toggle("in-app-shell", Boolean(window.liveClipperShell)); }, []);
  const openCreate = () => { const next = new URLSearchParams(location.search); next.set("dialog", "new-project"); navigate({ pathname: location.pathname, search: next.toString() }); };
  return <div className="workbench-shell"><header className="top-navigation"><Link className="brand" to="/studio"><img src="/static/venus-mark.png" alt="" /><span><strong>Venus</strong><small>直播内容工作台</small></span></Link><nav aria-label="主导航"><NavLink to="/studio" active={location.pathname === "/studio"}>工作室</NavLink><NavLink to="/projects" active={location.pathname.startsWith("/projects")}>项目</NavLink><NavLink to="/review" active={location.pathname === "/review"}>待审</NavLink><NavLink to="/resources" active={location.pathname === "/resources"}>资源</NavLink><NavLink to="/settings" active={location.pathname === "/settings"}>设置</NavLink></nav><button className="button primary" onClick={openCreate}>＋ 新建项目</button></header>
    <main className="main-content"><Routes><Route path="/" element={<Navigate to="/studio" replace />} /><Route path="/studio" element={<StudioPage notify={setNotice} />} /><Route path="/projects" element={<ProjectsPage />} /><Route path="/projects/:projectId" element={<ProjectPage notify={setNotice} />} /><Route path="/projects/:projectId/runs/:runId" element={<RunPage />} /><Route path="/review" element={<CompatibilityPage kind="review" />} /><Route path="/resources" element={<CompatibilityPage kind="resources" />} /><Route path="/settings" element={<CompatibilityPage kind="settings" />} /><Route path="/not-found/object" element={<ObjectNotFound type="对象" />} /><Route path="*" element={<NotFound />} /></Routes></main>
    {params.get("dialog") === "new-project" && <NewProjectDialog notify={setNotice} />}{notice && <div className="toast" role="status"><span>{notice}</span><button aria-label="关闭通知" onClick={() => setNotice("")}>×</button></div>}<Onboarding notify={setNotice} />
  </div>;
}

function CompatibilityPage({ kind }: { kind: "review" | "resources" | "settings" }) {
  const copy = { review: ["待审", "待审处理界面将在后续 Spec 接入。当前真实待审数量和对应项目可从工作室查看。"], resources: ["资源", "资源管理界面将在后续 Spec 接入。新建项目中的资源选项直接来自当前后端资源档案。"], settings: ["设置", "全局设置仍沿用现有 Venus 配置能力；本轮只迁移项目级工作台，不伪造尚未接入的全局操作。"] }[kind];
  return <section className="page"><PageHeading eyebrow="兼容入口" title={copy[0]} description={copy[1]} /><div className="empty-state"><strong>入口已保留</strong><p>{copy[1]}</p><Link className="button" to="/studio">返回工作室</Link></div></section>;
}
function NotFound() { return <section className="page"><div className="empty-state"><strong>找不到这个页面</strong><p>请从工作室或项目入口继续。</p><Link className="button primary" to="/studio">返回工作室</Link></div></section>; }
export function App() { return <BrowserRouter><AppShell /></BrowserRouter>; }
