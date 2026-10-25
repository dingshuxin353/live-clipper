import { useEffect, useRef, useState } from "react";

import { ApiError } from "./api";

type Confirmed<Request, Value> = { kind: "success"; request: Request; value: Value }
  | { kind: "rejected"; request: Request; error: unknown };
type State<Request, Value> = { status: "idle" | "preparing" | "submitting" }
  | { status: "pending"; request: Request }
  | { status: "storage-error"; step: "read"; message: string }
  | { status: "storage-error"; step: "save"; message: string; request: Request }
  | { status: "storage-error"; step: "clear"; message: string; outcome: Confirmed<Request, Value> };
export type OperationResult<Request, Value> = (Confirmed<Request, Value> & { cleared: boolean; isCurrent(): boolean })
  | { kind: "unknown" | "cancelled" | "ignored" }
  | { kind: "preparation-error"; error: unknown };
type Context<Request, Value> = { key: string; alive: boolean; record: string | null; state: State<Request, Value> };

export function usePendingOperation<Request, Value>(
  key: string,
  valid: (value: unknown) => value is Request,
  submit: (request: Request) => Promise<Value>,
) {
  const [, render] = useState(0);
  const context = useRef<Context<Request, Value> | null>(null);
  function read(): Context<Request, Value> {
    try {
      const record = sessionStorage.getItem(key);
      if (record === null) return { key, alive: true, record, state: { status: "idle" } };
      const request: unknown = JSON.parse(record);
      if (!valid(request)) throw new Error("invalid request");
      return { key, alive: true, record, state: { status: "pending", request } };
    } catch {
      return { key, alive: true, record: null, state: { status: "storage-error", step: "read", message: "无法读取或识别本地操作记录，请重新读取；仍有问题时需要排查此记录。" } };
    }
  }
  if (context.current?.key !== key) context.current = read();
  const current = context.current;
  const isCurrent = () => context.current === current && current.alive;
  useEffect(() => { current.alive = true; return () => { current.alive = false; }; }, [current]);
  const update = (state: State<Request, Value>) => {
    current.state = state;
    if (isCurrent()) render(value => value + 1);
  };

  function clear(outcome: Confirmed<Request, Value>): OperationResult<Request, Value> {
    let cleared = false;
    try {
      const stored = sessionStorage.getItem(current.key);
      if (stored !== null && stored !== current.record) throw new Error("record changed");
      if (stored !== null) sessionStorage.removeItem(current.key);
      current.record = null;
      cleared = true;
      update({ status: "idle" });
    } catch {
      update({ status: "storage-error", step: "clear", outcome, message: "操作结果已确认，但无法清除本地操作记录。请重试清除后再开始新操作。" });
    }
    return isCurrent() ? { ...outcome, cleared, isCurrent } : { kind: "ignored" };
  }

  async function send(request: Request): Promise<OperationResult<Request, Value>> {
    update({ status: "submitting" });
    let outcome: Confirmed<Request, Value>;
    try {
      outcome = { kind: "success", request, value: await submit(request) };
    } catch (error) {
      if (!(error instanceof ApiError) || error.outcomeUnknown || error.code === "request_id_conflict") {
        update({ status: "pending", request });
        return isCurrent() ? { kind: "unknown" } : { kind: "ignored" };
      }
      outcome = { kind: "rejected", request, error };
    }
    return clear(outcome);
  }

  async function save(request: Request): Promise<OperationResult<Request, Value>> {
    try {
      const record = JSON.stringify(request);
      const stored = sessionStorage.getItem(current.key);
      if (stored !== null && stored !== record) throw new Error("record changed");
      sessionStorage.setItem(current.key, record);
      current.record = record;
    } catch {
      update({ status: "storage-error", step: "save", request, message: "无法保存本地操作记录，本次请求尚未发送。请恢复存储后重试保存。" });
      return { kind: "ignored" };
    }
    return send(request);
  }

  async function start(prepare: () => Request | null | Promise<Request | null>): Promise<OperationResult<Request, Value>> {
    if (!isCurrent() || current.state.status !== "idle") return { kind: "ignored" };
    update({ status: "preparing" });
    let request: Request | null;
    try {
      request = await prepare();
      if (request !== null && !valid(request)) throw new Error("操作参数不符合要求。");
    } catch (error) {
      update({ status: "idle" });
      return isCurrent() ? { kind: "preparation-error", error } : { kind: "ignored" };
    }
    if (!isCurrent()) return { kind: "ignored" };
    if (request === null) { update({ status: "idle" }); return { kind: "cancelled" }; }
    return save(request);
  }

  async function resume(): Promise<OperationResult<Request, Value>> {
    if (!isCurrent() || current.state.status !== "pending") return { kind: "ignored" };
    return send(current.state.request);
  }

  async function retryStorage(): Promise<OperationResult<Request, Value>> {
    if (!isCurrent() || current.state.status !== "storage-error") return { kind: "ignored" };
    const failure = current.state;
    update({ status: "preparing" });
    if (failure.step === "save") return save(failure.request);
    if (failure.step === "clear") return clear(failure.outcome);
    const next = read();
    current.record = next.record;
    update(next.state);
    return { kind: "ignored" };
  }

  const state = current.state;
  return {
    state, start, resume, retryStorage,
    busy: state.status === "preparing" || state.status === "submitting",
    blocked: state.status !== "idle",
    pending: state.status === "pending",
    storageError: state.status === "storage-error" ? state.message : "",
    storageRetryLabel: state.status === "storage-error" ? { read: "重新读取操作记录", save: "重试保存操作", clear: "重试清除操作记录" }[state.step] : "",
  };
}
