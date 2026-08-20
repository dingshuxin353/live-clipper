import { api, patch, post } from "./api";
import type { FormOptionsPayload, ProjectConfig, ProjectSummary, Run, RunFilter, ScanEvent, ScanPreviewPayload, SourceFile, StageEvent, StudioPayload, ValidationPayload } from "./project-dto";

export const projectApi = {
  studio: (signal?: AbortSignal) => api<StudioPayload>("/api/studio", {}, signal),
  markStudioSeen: (throughEventId: number) => post<{ ok: true; last_seen_event_id: number }>("/api/studio/seen", { through_event_id: throughEventId }),
  projects: (signal?: AbortSignal) => api<{ ok: true; projects: ProjectSummary[] }>("/api/projects", {}, signal),
  project: (projectId: string, signal?: AbortSignal) => api<{ ok: true; project: ProjectSummary }>(`/api/projects/${projectId}`, {}, signal),
  runs: (projectId: string, filter: RunFilter, cursor?: string | null, signal?: AbortSignal) => { const search = new URLSearchParams({ filter, limit: "50" }); if (cursor) search.set("cursor", cursor); return api<{ ok: true; runs: Run[]; cursor: string | null; has_more: boolean }>(`/api/projects/${projectId}/runs?${search}`, {}, signal); },
  run: (runId: string, signal?: AbortSignal) => api<{ ok: true; run: Run; stage_events: StageEvent[] }>(`/api/runs/${runId}`, {}, signal),
  formOptions: (signal?: AbortSignal) => api<FormOptionsPayload>("/api/project-form-options", {}, signal),
  scanPreview: (sourceDirectory: string, mode: ProjectConfig["source"]["first_scan_mode"], lookbackDays: number | null) => post<ScanPreviewPayload>("/api/projects/scan-preview", { source_directory: sourceDirectory, first_scan_mode: mode, ...(mode === "recent" ? { lookback_days: lookbackDays } : {}) }),
  validate: (project: { name: string; description: string; config: ProjectConfig }, activationState: "active" | "inactive") => post<ValidationPayload>("/api/projects/validate", { project, activation_state: activationState }),
  create: (id: string, project: { name: string; description: string; config: ProjectConfig }, activationState: "active" | "inactive") => post<{ ok: true; project: ProjectSummary; initial_scan: ScanEvent | null }>("/api/projects", { request_id: id, project, activation_state: activationState }),
  update: (projectId: string, id: string, expectedRevision: number, project: { name: string; description: string; config: ProjectConfig }) => patch<{ ok: true; project: ProjectSummary }>(`/api/projects/${projectId}`, { request_id: id, expected_revision: expectedRevision, project }),
  activate: (projectId: string, action: "enable" | "pause" | "resume", id: string) => post<{ ok: true; project: ProjectSummary; initial_scan: ScanEvent | null }>(`/api/projects/${projectId}/${action}`, { request_id: id }),
  scan: (projectId: string, id: string, scope: "new" | "selected", selectedRelativePaths: string[] = []) => post<{ ok: true; scan: ScanEvent; reused?: boolean }>(`/api/projects/${projectId}/scans`, { request_id: id, scope, selected_relative_paths: selectedRelativePaths }),
  latestScan: (projectId: string, signal?: AbortSignal) => api<{ ok: true; scan: ScanEvent | null }>(`/api/projects/${projectId}/scans/latest`, {}, signal),
  sourceFiles: (projectId: string, signal?: AbortSignal) => api<{ ok: true; files: SourceFile[] }>(`/api/projects/${projectId}/source-files`, {}, signal),
};

export function requestId(prefix: string): string { return `${prefix}-${crypto.randomUUID()}`; }
