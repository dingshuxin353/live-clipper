export type GenericRecord = Record<string, any>;

export interface Run extends GenericRecord {
  run_id: string;
  phase?: string;
  source_name?: string;
  candidate_count?: number;
  clip_count?: number;
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
