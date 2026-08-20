export type GenericRecord = Record<string, any>;

export interface Run extends GenericRecord {
  run_id: string;
  phase?: string;
  phase_group?: string;
  source_name?: string;
  candidate_count?: number;
  clip_count?: number;
  queue_position?: number;
}

export interface PhaseCounts {
  all: number;
  queued: number;
  processing: number;
  needs_review: number;
  rendered: number;
  failed: number;
  other: number;
}

export interface RunsPayload {
  ok?: boolean;
  runs?: Run[];
  count?: number;
  total?: number;
  offset?: number;
  limit?: number;
  has_more?: boolean;
  phase?: string | null;
  phase_counts?: Partial<PhaseCounts>;
}

export interface RunListState {
  loaded: boolean;
  count: number;
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  phase: string | null;
  phase_counts: PhaseCounts;
}

export interface Model extends GenericRecord {
  id: string;
  display_name: string;
  tier_label: string;
  state: "installed" | "downloading" | "damaged" | string;
  current?: boolean;
  downloading?: boolean;
  job_id?: string;
}

export interface AppSnapshot {
  service: GenericRecord | null;
  runs: Run[];
  confirmations: GenericRecord[];
  events: GenericRecord[];
  configPayload: GenericRecord | null;
  scheduler: GenericRecord | null;
  reviewAutomation: GenericRecord | null;
  models: Model[];
}

export type TabId = "clips" | "automation" | "confirmations" | "settings";
