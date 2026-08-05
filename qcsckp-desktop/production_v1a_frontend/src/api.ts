export type ApiEnvelope<T> = {
  request_id: string;
  success: boolean;
  data: T;
  error: null | { code: string; message: string; details?: unknown; retryable?: boolean };
};

const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
const launchToken = hash.get("token") ?? sessionStorage.getItem("qcsckp_launch_token") ?? "";
if (launchToken) sessionStorage.setItem("qcsckp_launch_token", launchToken);
if (location.hash) history.replaceState(null, "", location.pathname + location.search);

let adminSession = sessionStorage.getItem("qcsckp_admin_session") ?? "";

export function setAdminSession(token: string) {
  adminSession = token;
  if (token) sessionStorage.setItem("qcsckp_admin_session", token);
  else sessionStorage.removeItem("qcsckp_admin_session");
}

export function hasAdminSession() {
  return Boolean(adminSession);
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${launchToken}`);
  if (adminSession) headers.set("X-QCSCKP-Session", adminSession);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  const envelope = (await response.json()) as ApiEnvelope<T>;
  if (!response.ok || !envelope.success) {
    const error = new Error(envelope.error?.message || `HTTP ${response.status}`);
    Object.assign(error, { code: envelope.error?.code, requestId: envelope.request_id });
    throw error;
  }
  return envelope.data;
}

export function command(path: string, body: Record<string, unknown> = {}) {
  return api<{ job_uid: string; status: string; result?: unknown }>(path, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function waitJob(jobUid: string, onProgress?: (job: any) => void) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const job = await api<any>(`/api/v1/jobs/${encodeURIComponent(jobUid)}`);
    onProgress?.(job);
    if (["succeeded", "failed", "cancelled", "blocked_user_action"].includes(job.status)) return job;
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  throw new Error("后台任务等待超时");
}

export async function subscribeEvents(onEvent: (event: any) => void, signal: AbortSignal) {
  if (!adminSession) return;
  const response = await fetch("/api/v1/events", {
    headers: {
      Authorization: `Bearer ${launchToken}`,
      "X-QCSCKP-Session": adminSession,
    },
    signal,
  });
  if (!response.ok || !response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (!signal.aborted) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const line = chunk.split("\n").find((value) => value.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}
