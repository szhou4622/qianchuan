export type Health = {
  product_version: string;
  schema_version: number;
  runtime_dir: string;
  database: { ok: boolean; integrity: string };
  admin_required: boolean;
  auth_mode?: string;
  authentication?: Record<string, unknown>;
  real_platform_writes: { registered: boolean; network_guard: string; database_guard: string; mode: string };
  collection_capacity?: {
    active_plan_material_relations: number;
    target_interval_seconds: number;
    state: string;
    disk_free_bytes: number;
    disk_state: string;
  };
  setup_progress: Array<{ key: string; label: string; status: string }>;
  browser?: {
    chrome_state: string;
    chrome_path: string;
    qianchuan_login_status: string;
    cookie_updated_at?: string;
    last_verified_at?: string;
    blocked_reason?: string;
  };
  feishu?: {
    credential: string;
    transport: string;
    events: string;
    binding: string;
    sending: string;
  };
  job_queue?: { queued: number; running: number; blocked: number };
  latest_collection_at?: string;
};

export type Account = {
  account_uid: string;
  aavid: string;
  account_name: string;
  enabled: number;
  daily_report_enabled: number;
  catalog_status: string;
  catalog_error_message?: string;
  catalog_completed_at?: string;
  plan_count: number;
  monitored_plan_count: number;
  feishu_route_id?: string;
};

export type Plan = {
  target_uid: string;
  aavid: string;
  ad_id: string;
  plan_name: string;
  plan_system: "global" | "chengfang" | "unknown";
  promotion_scene: "product" | "live" | "unknown";
  platform_status: string;
  verification_state: string;
  monitor_enabled: number;
  monitor_eligible: number;
  ineligible_reason?: string;
  last_successful_collection_at?: string;
};

export type Job = {
  job_uid: string;
  job_type: string;
  status: string;
  progress_current: number;
  progress_total: number;
  progress_message?: string;
  error_message?: string;
};
