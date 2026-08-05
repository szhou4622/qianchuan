import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  Field,
  Input,
  Select,
  Spinner,
  Switch,
  Textarea,
  Tooltip,
} from "@fluentui/react-components";
import {
  AppsListDetailRegular,
  ArrowClockwiseRegular,
  BeakerRegular,
  BookDatabaseRegular,
  BotRegular,
  CheckmarkCircleRegular,
  ClipboardTaskListLtrRegular,
  DataTrendingRegular,
  DeleteRegular,
  DoctorRegular,
  HeartPulseRegular,
  KeyRegular,
  LockClosedRegular,
  OpenRegular,
  PlayRegular,
  ShieldCheckmarkRegular,
  SignOutRegular,
} from "@fluentui/react-icons";
import {
  api,
  command,
  hasAdminSession,
  setAdminSession,
  subscribeEvents,
  waitJob,
} from "./api";
import type { Account, Health, Job, Plan } from "./types";

type PageKey =
  | "health"
  | "accounts"
  | "monitor"
  | "strategies"
  | "candidates"
  | "feishu"
  | "operations"
  | "dashboard"
  | "recovery";
type Notice = {
  tone: "info" | "success" | "danger" | "warning";
  message: string;
};

const nav: Array<{ key: PageKey; label: string; icon: any }> = [
  { key: "health", label: "运行健康与首次配置", icon: HeartPulseRegular },
  { key: "accounts", label: "千川账户与计划", icon: AppsListDetailRegular },
  { key: "monitor", label: "监控详情", icon: DataTrendingRegular },
  { key: "strategies", label: "策略模拟", icon: BeakerRegular },
  {
    key: "candidates",
    label: "候选与任务中心",
    icon: ClipboardTaskListLtrRegular,
  },
  { key: "feishu", label: "飞书绑定", icon: BotRegular },
  { key: "operations", label: "操作流水与日报", icon: BookDatabaseRegular },
  { key: "dashboard", label: "数据大屏", icon: DataTrendingRegular },
  { key: "recovery", label: "迁移、诊断与恢复", icon: DoctorRegular },
];

const planSystemName: Record<string, string> = {
  global: "全域",
  chengfang: "乘方",
  unknown: "待确认",
};
const sceneName: Record<string, string> = {
  product: "推商品",
  live: "推直播",
  unknown: "待确认",
};

function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [authenticated, setAuthenticated] = useState(hasAdminSession());
  const [page, setPage] = useState<PageKey>("health");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [jobs, setJobs] = useState<Record<string, Job>>({});
  const [refreshKey, setRefreshKey] = useState(0);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api<Health>("/api/v1/health"));
    } catch (error) {
      setNotice({ tone: "danger", message: errorMessage(error) });
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
  }, [refreshHealth, refreshKey]);
  useEffect(() => {
    if (!authenticated) return;
    const controller = new AbortController();
    void subscribeEvents((event) => {
      if (event.job_uid) {
        void api<Job>(`/api/v1/jobs/${event.job_uid}`)
          .then((job) => {
            setJobs((previous) => ({ ...previous, [job.job_uid]: job }));
            if (
              [
                "succeeded",
                "failed",
                "cancelled",
                "blocked_user_action",
              ].includes(job.status)
            )
              setRefreshKey((value) => value + 1);
          })
          .catch(() => undefined);
      }
    }, controller.signal).catch(() => undefined);
    return () => controller.abort();
  }, [authenticated]);

  const run = useCallback(
    async (
      path: string,
      body: Record<string, unknown>,
      successMessage: string,
    ) => {
      setNotice({ tone: "info", message: "后台任务已提交" });
      try {
        const result = await command(path, body);
        if (result.status === "succeeded") {
          setNotice({ tone: "success", message: successMessage });
          setRefreshKey((value) => value + 1);
          return result;
        }
        const job = await waitJob(result.job_uid, (current) =>
          setJobs((previous) => ({ ...previous, [current.job_uid]: current })),
        );
        if (job.status !== "succeeded")
          throw new Error(job.error_message || "后台任务失败");
        setNotice({ tone: "success", message: successMessage });
        setRefreshKey((value) => value + 1);
        return job;
      } catch (error) {
        setNotice({ tone: "danger", message: errorMessage(error) });
        throw error;
      }
    },
    [],
  );

  if (!health)
    return (
      <div className="boot">
        <Spinner label="正在启动 V1A 只读服务…" />
      </div>
    );
  if (health.admin_required)
    return (
      <AdminGate
        mode="create"
        onAuthenticated={() => {
          setAuthenticated(true);
          void refreshHealth();
        }}
      />
    );
  if (!authenticated)
    return (
      <AdminGate
        mode="login"
        onAuthenticated={() => {
          setAuthenticated(true);
          void refreshHealth();
        }}
      />
    );

  const ActiveIcon =
    nav.find((item) => item.key === page)?.icon ?? HeartPulseRegular;
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <ShieldCheckmarkRegular fontSize={26} />
          <div>
            <strong>千川生产工具</strong>
            <small>Production V1A</small>
          </div>
        </div>
        <div className="read-only-seal">
          <LockClosedRegular /> 可信只读 · 真实写入关闭
        </div>
        <nav aria-label="主导航">
          {nav.map((item, index) => {
            const Icon = item.icon;
            return (
              <button
                key={item.key}
                className={page === item.key ? "nav-item active" : "nav-item"}
                onClick={() => setPage(item.key)}
              >
                <span>{index + 1}</span>
                <Icon />
                <b>{item.label}</b>
              </button>
            );
          })}
        </nav>
        <button
          className="nav-item logout"
          onClick={() => {
            void command("/api/v1/admin/logout", {})
              .catch(() => undefined)
              .finally(() => {
                setAdminSession("");
                setAuthenticated(false);
              });
          }}
        >
          <span>·</span>
          <SignOutRegular />
          <b>锁定本机管理台</b>
        </button>
      </aside>
      <main className="workspace">
        <header className="topbar">
          <div>
            <div className="eyebrow">
              <ActiveIcon /> 生产主流程
            </div>
            <h1>{nav.find((item) => item.key === page)?.label}</h1>
          </div>
          <div className="version-block">
            <span className="status-dot healthy" />
            {health.product_version}
            <small>Schema {health.schema_version}</small>
          </div>
        </header>
        {notice && (
          <div className={`notice ${notice.tone}`} role="status">
            <span>{notice.message}</span>
            <button aria-label="关闭提示" onClick={() => setNotice(null)}>
              ×
            </button>
          </div>
        )}
        <section className="page-content">
          {page === "health" && (
            <HealthPage health={health} setPage={setPage} />
          )}
          {page === "accounts" && (
            <AccountsPage refreshKey={refreshKey} run={run} />
          )}
          {page === "monitor" && (
            <MonitorPage refreshKey={refreshKey} run={run} />
          )}
          {page === "strategies" && (
            <StrategyPage refreshKey={refreshKey} run={run} />
          )}
          {page === "candidates" && (
            <>
              <CandidatePage refreshKey={refreshKey} run={run} />
              <AdjustmentCandidatePanel refreshKey={refreshKey} />
            </>
          )}
          {page === "feishu" && (
            <FeishuPage refreshKey={refreshKey} run={run} />
          )}
          {page === "operations" && <OperationsPage refreshKey={refreshKey} />}
          {page === "dashboard" && <DashboardPage refreshKey={refreshKey} />}
          {page === "recovery" && (
            <RecoveryPage refreshKey={refreshKey} run={run} health={health} />
          )}
        </section>
      </main>
      <JobRail jobs={Object.values(jobs)} />
    </div>
  );
}

function AdminGate({
  mode,
  onAuthenticated,
}: {
  mode: "create" | "login";
  onAuthenticated: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [recovery, setRecovery] = useState("");
  const [recoveryMode, setRecoveryMode] = useState(false);
  const [recoveryCode, setRecoveryCode] = useState("");
  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const result = recoveryMode
        ? await command("/api/v1/admin/recover", {
            username,
            recovery_code: recoveryCode,
            new_password: password,
          })
        : await command(`/api/v1/admin/${mode}`, { username, password });
      const value = result.result as any;
      if (value.session_token) setAdminSession(value.session_token);
      if (value.recovery_code || value.replacement_recovery_code) {
        setRecovery(value.recovery_code || value.replacement_recovery_code);
      } else {
        onAuthenticated();
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  if (recovery)
    return (
      <div className="auth-screen">
        <div className="auth-card wide">
          <ShieldCheckmarkRegular fontSize={38} />
          <h1>{recoveryMode ? "密码已恢复" : "本机管理员已创建"}</h1>
          <p>
            这是唯一一次展示新的离线恢复码。请抄写到安全位置，工具只保存其哈希。
          </p>
          <div className="recovery-code">{recovery}</div>
          <Button
            appearance="primary"
            onClick={() => {
              if (recoveryMode) {
                setRecovery("");
                setRecoveryMode(false);
                setPassword("");
                setRecoveryCode("");
              } else onAuthenticated();
            }}
          >
            {recoveryMode ? "返回登录" : "我已安全保存，进入工具"}
          </Button>
        </div>
      </div>
    );
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <LockClosedRegular fontSize={34} />
        <p className="kicker">PRODUCTION V1A</p>
        <h1>
          {recoveryMode
            ? "使用离线恢复码"
            : mode === "create"
              ? "创建本机管理员"
              : "登录本机管理台"}
        </h1>
        <p>
          {recoveryMode
            ? "验证恢复码后设置新密码；旧恢复码立即失效。"
            : mode === "create"
              ? "账号只保存在这台电脑，与旧远程账号完全隔离。"
              : "登录后可继续管理本机只读采集与模拟任务。"}
        </p>
        <Field label="管理员账号">
          <Input
            value={username}
            onChange={(_, data) => setUsername(data.value)}
          />
        </Field>
        {recoveryMode && (
          <Field label="离线恢复码">
            <Input
              value={recoveryCode}
              onChange={(_, data) => setRecoveryCode(data.value)}
            />
          </Field>
        )}
        <Field
          label={recoveryMode ? "新密码" : "密码"}
          hint={
            mode === "create" || recoveryMode
              ? "至少10位，包含大小写字母和数字"
              : undefined
          }
        >
          <Input
            type="password"
            value={password}
            onChange={(_, data) => setPassword(data.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void submit();
            }}
          />
        </Field>
        {error && <div className="inline-error">{error}</div>}
        <Button
          appearance="primary"
          disabled={busy}
          onClick={() => void submit()}
        >
          {busy
            ? "处理中…"
            : recoveryMode
              ? "验证并重置密码"
              : mode === "create"
                ? "创建并进入"
                : "登录"}
        </Button>
        {mode === "login" && (
          <Button
            appearance="subtle"
            onClick={() => {
              setRecoveryMode((value) => !value);
              setError("");
            }}
          >
            {recoveryMode ? "返回账号密码登录" : "忘记密码，使用离线恢复码"}
          </Button>
        )}
      </div>
    </div>
  );
}

function HealthPage({
  health,
  setPage,
}: {
  health: Health;
  setPage: (page: PageKey) => void;
}) {
  return (
    <>
      <div className="safety-banner">
        <ShieldCheckmarkRegular />
        <div>
          <strong>V1A 网络写入熔断已启用</strong>
          <span>
            没有注册真实追投、暂停、预算或时长写接口；命中策略只产生冻结候选和模拟审计。
          </span>
        </div>
      </div>
      <div className="metric-grid">
        <Metric
          label="数据库"
          value={health.database.ok ? "健康" : "异常"}
          detail={health.database.integrity}
          tone={health.database.ok ? "good" : "bad"}
        />
        <Metric
          label="千川写能力"
          value="0 个"
          detail="服务层与适配器层双重关闭"
          tone="good"
        />
        <Metric label="运行目录" value="已隔离" detail={health.runtime_dir} />
        <Metric
          label="后台事件流"
          value="持续连接"
          detail="UI重载不影响后台任务"
        />
      </div>
      <Panel
        title="首次配置进度"
        description="按顺序完成后，系统才会对用户明确选择的计划运行只读采集。"
      >
        <div className="setup-grid">
          {health.setup_progress.map((step, index) => (
            <button
              key={step.key}
              className={`setup-step ${step.status}`}
              onClick={() =>
                setPage(
                  (
                    {
                      qianchuan: "accounts",
                      feishu: "feishu",
                      monitor: "accounts",
                      strategy: "strategies",
                      local_admin: "health",
                    } as any
                  )[step.key],
                )
              }
            >
              <span>{index + 1}</span>
              <div>
                <strong>{step.label}</strong>
                <small>
                  {step.status === "complete"
                    ? "已完成"
                    : step.status === "required"
                      ? "需要配置"
                      : "等待前置步骤"}
                </small>
              </div>
              {step.status === "complete" && <CheckmarkCircleRegular />}
            </button>
          ))}
        </div>
      </Panel>
    </>
  );
}

function AccountsPage({
  refreshKey,
  run,
}: {
  refreshKey: number;
  run: RunCommand;
}) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [selected, setSelected] = useState<Account | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api<Account[]>("/api/v1/accounts");
      setAccounts(list);
      setSelected(
        (current) =>
          list.find((item) => item.aavid === current?.aavid) ?? list[0] ?? null,
      );
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load, refreshKey]);
  useEffect(() => {
    if (!selected) {
      setPlans([]);
      return;
    }
    void api<Plan[]>(
      `/api/v1/plans?aavid=${encodeURIComponent(selected.aavid)}`,
    ).then((list) => {
      setPlans(list);
      setChecked(
        new Set(
          list
            .filter((item) => item.monitor_enabled)
            .map((item) => item.target_uid),
        ),
      );
    });
  }, [selected, refreshKey]);
  const filtered = plans.filter(
    (plan) =>
      (!search ||
        `${plan.plan_name}${plan.ad_id}`
          .toLowerCase()
          .includes(search.toLowerCase())) &&
      (status === "all" || plan.platform_status === status),
  );
  const groups = useMemo(
    () =>
      [
        "global:product",
        "global:live",
        "chengfang:product",
        "chengfang:live",
        "unknown:unknown",
      ]
        .map((key) => {
          const [system, scene] = key.split(":");
          return {
            key,
            label: `${planSystemName[system]} · ${sceneName[scene]}`,
            plans: filtered.filter(
              (plan) =>
                plan.plan_system === system && plan.promotion_scene === scene,
            ),
          };
        })
        .filter((group) => group.plans.length),
    [filtered],
  );
  const save = async () => {
    if (!selected) return;
    await run(
      "/api/v1/accounts/monitor-setup",
      {
        aavid: selected.aavid,
        enabled: true,
        daily_report_enabled: Boolean(selected.daily_report_enabled),
        feishu_route_id: selected.feishu_route_id || null,
        target_uids: [...checked],
      },
      "账户路由与监控计划已原子保存",
    );
  };
  return (
    <div className="accounts-layout">
      <Panel
        className="account-directory"
        title="主动添加账户"
        description="工具不会扫描全部授权账户；每次由你在可见 Chrome 中选择一个账户。"
        actions={
          <Button
            appearance="primary"
            icon={<OpenRegular />}
            onClick={() =>
              void run(
                "/api/v1/accounts/add",
                {},
                "账户已添加并完成首轮目录刷新",
              )
            }
          >
            选择并添加账户
          </Button>
        }
      >
        {loading ? (
          <Spinner />
        ) : accounts.length === 0 ? (
          <Empty
            title="还没有账户"
            description="点击“选择并添加账户”，在 Chrome 中进入该账户任意计划详情。"
          />
        ) : (
          <div className="account-list">
            {accounts.map((account) => (
              <button
                key={account.aavid}
                onClick={() => setSelected(account)}
                className={
                  selected?.aavid === account.aavid
                    ? "account-row active"
                    : "account-row"
                }
              >
                <div>
                  <strong>{account.account_name}</strong>
                  <small>账户ID {account.aavid}</small>
                </div>
                <span>{account.plan_count} 计划</span>
                <i className={`catalog ${account.catalog_status}`}>
                  {catalogLabel(account.catalog_status)}
                </i>
              </button>
            ))}
          </div>
        )}
      </Panel>
      <Panel
        className="plan-directory"
        title={selected ? selected.account_name : "计划目录"}
        description={
          selected
            ? `账户ID ${selected.aavid} · 只有身份、状态和证据完整的计划可勾选`
            : "请先选择账户"
        }
        actions={
          selected && (
            <>
              <Tooltip
                content="只刷新当前已添加账户，不扫描其他授权账户"
                relationship="label"
              >
                <Button
                  icon={<ArrowClockwiseRegular />}
                  onClick={() =>
                    void run(
                      "/api/v1/accounts/refresh-catalog",
                      { aavid: selected.aavid },
                      "四类计划目录刷新完成",
                    )
                  }
                >
                  刷新该账户计划
                </Button>
              </Tooltip>
              <Button
                icon={<DeleteRegular />}
                appearance="subtle"
                onClick={() =>
                  confirm("删除后将停止该账户所有监控，确定继续？") &&
                  void run(
                    "/api/v1/accounts/delete",
                    { aavid: selected.aavid },
                    "账户已从V1A移除",
                  )
                }
              >
                移除账户
              </Button>
            </>
          )
        }
      >
        <div className="toolbar">
          <Input
            placeholder="搜索计划名称或ID"
            value={search}
            onChange={(_, data) => setSearch(data.value)}
          />
          <Select value={status} onChange={(_, data) => setStatus(data.value)}>
            <option value="all">全部状态</option>
            <option value="active">投放中</option>
            <option value="paused">已暂停</option>
            <option value="ended">已结束</option>
          </Select>
          <span className="selection-count">已选 {checked.size}/10</span>
        </div>
        <div className="scroll-catalog">
          {!selected ? (
            <Empty
              title="未选择账户"
              description="从左侧选择账户查看四类计划。"
            />
          ) : plans.length === 0 ? (
            <Empty
              title="目录暂无计划"
              description="运行刷新后仍为空时，请查看目录状态和诊断信息；系统不会用异常空结果清空历史目录。"
            />
          ) : (
            groups.map((group) => (
              <div className="plan-group" key={group.key}>
                <h3>
                  {group.label}
                  <span>{group.plans.length}</span>
                </h3>
                {group.plans.map((plan) => (
                  <label
                    className={`plan-row ${!plan.monitor_eligible ? "disabled" : ""}`}
                    key={plan.target_uid}
                  >
                    <Checkbox
                      checked={checked.has(plan.target_uid)}
                      disabled={
                        !plan.monitor_eligible ||
                        (!checked.has(plan.target_uid) && checked.size >= 10)
                      }
                      onChange={(_, data) =>
                        setChecked((previous) => {
                          const next = new Set(previous);
                          data.checked
                            ? next.add(plan.target_uid)
                            : next.delete(plan.target_uid);
                          return next;
                        })
                      }
                    />
                    <div>
                      <strong>{plan.plan_name}</strong>
                      <small>
                        计划ID {plan.ad_id} · {plan.platform_status} ·{" "}
                        {plan.verification_state}
                      </small>
                      {!plan.monitor_eligible && (
                        <em>{plan.ineligible_reason || "证据未确认"}</em>
                      )}
                    </div>
                    <span>{plan.monitor_enabled ? "监控中" : "未监控"}</span>
                  </label>
                ))}
              </div>
            ))
          )}
        </div>
        {selected && (
          <div className="sticky-actions">
            <Switch
              checked={Boolean(selected.enabled)}
              label="启用该账户"
              onChange={(_, data) =>
                setSelected({ ...selected, enabled: Number(data.checked) })
              }
            />
            <Switch
              checked={Boolean(selected.daily_report_enabled)}
              label="昨日平台操作日报"
              onChange={(_, data) =>
                setSelected({
                  ...selected,
                  daily_report_enabled: Number(data.checked),
                })
              }
            />
            <Button appearance="primary" onClick={() => void save()}>
              保存账户与监控计划
            </Button>
          </div>
        )}
      </Panel>
    </div>
  );
}

function MonitorPage({
  refreshKey,
  run,
}: {
  refreshKey: number;
  run: RunCommand;
}) {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [collections, setCollections] = useState<any[]>([]);
  const [tasks, setTasks] = useState<any[]>([]);
  const [target, setTarget] = useState("");
  useEffect(() => {
    void api<Plan[]>("/api/v1/plans").then((rows) => {
      const monitored = rows.filter((row) => row.monitor_enabled);
      setPlans(monitored);
      setTarget((value) => value || monitored[0]?.target_uid || "");
    });
  }, [refreshKey]);
  useEffect(() => {
    if (!target) return;
    const query = `target_uid=${encodeURIComponent(target)}`;
    void Promise.all([
      api<any[]>(`/api/v1/collections?${query}`),
      api<any[]>(`/api/v1/control-tasks?${query}`),
    ]).then(([runs, controls]) => {
      setCollections(runs);
      setTasks(controls);
    });
  }, [target, refreshKey]);
  const plan = plans.find((row) => row.target_uid === target);
  return (
    <>
      <Panel
        title="只读监控目标"
        description="素材、商品关系与 Scene 1/2/3 调控任务均来自可信批次；写能力保持关闭。"
        actions={
          target && (
            <Button
              appearance="primary"
              icon={<PlayRegular />}
              onClick={() =>
                void run(
                  "/api/v1/collections/run",
                  { target_uid: target },
                  "只读采集、规则模拟和候选冻结完成",
                )
              }
            >
              立即采集
            </Button>
          )
        }
      >
        <Select value={target} onChange={(_, data) => setTarget(data.value)}>
          <option value="">请选择监控计划</option>
          {plans.map((row) => (
            <option key={row.target_uid} value={row.target_uid}>
              {row.plan_name} · {planSystemName[row.plan_system]} ·{" "}
              {sceneName[row.promotion_scene]}
            </option>
          ))}
        </Select>
        {plan && (
          <div className="evidence-strip">
            <span>
              <b>账户</b>
              {plan.aavid}
            </span>
            <span>
              <b>计划</b>
              {plan.ad_id}
            </span>
            <span>
              <b>类型</b>
              {planSystemName[plan.plan_system]} ·{" "}
              {sceneName[plan.promotion_scene]}
            </span>
            <span>
              <b>最近成功采集</b>
              {plan.last_successful_collection_at || "尚无"}
            </span>
          </div>
        )}
      </Panel>
      <div className="two-columns">
        <Panel title="采集批次">
          <DataTable
            rows={collections.slice(0, 50)}
            columns={[
              "object_type",
              "status",
              "row_count",
              "page_count",
              "started_at",
              "error_code",
            ]}
            empty="尚无采集批次"
          />
        </Panel>
        <Panel title="平台调控任务证据">
          <DataTable
            rows={tasks.slice(0, 50)}
            columns={[
              "scene",
              "task_name",
              "control_task_id",
              "platform_status",
              "updated_at",
            ]}
            empty="尚无调控任务"
          />
        </Panel>
      </div>
    </>
  );
}

function StrategyPage({
  refreshKey,
  run,
}: {
  refreshKey: number;
  run: RunCommand;
}) {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [target, setTarget] = useState("");
  const [items, setItems] = useState<any[]>([]);
  const [title, setTitle] = useState("高质量视频候选");
  const [strategyType, setStrategyType] = useState("retarget_create");
  const [level, setLevel] = useState("material");
  const [metric, setMetric] = useState("spend_cent");
  const [operator, setOperator] = useState("gte");
  const [value, setValue] = useState("10000");
  const [maxValue, setMaxValue] = useState("20000");
  const [priority, setPriority] = useState("10");
  const [budgetDelta, setBudgetDelta] = useState("0");
  const [durationDelta, setDurationDelta] = useState("0");
  useEffect(() => {
    void api<Plan[]>("/api/v1/plans").then((rows) => {
      const eligible = rows.filter(
        (row) => row.monitor_enabled && row.monitor_eligible,
      );
      setPlans(eligible);
      setTarget((current) => current || eligible[0]?.target_uid || "");
    });
  }, [refreshKey]);
  useEffect(() => {
    if (target)
      void api<any[]>(
        `/api/v1/strategies?target_uid=${encodeURIComponent(target)}`,
      ).then(setItems);
    else setItems([]);
  }, [target, refreshKey]);
  const save = () =>
    run(
      "/api/v1/strategies/save",
      {
        target_uid: target,
        title,
        priority: Number(priority),
        strategy_type: strategyType,
        trigger_level: level,
        trigger: {
          conditions: [
            operator === "between"
              ? { metric, operator, min: value, max: maxValue }
              : { metric, operator, value },
          ],
        },
        action_params:
          strategyType === "retarget_adjust"
            ? {
                budget_delta_cent: Number(budgetDelta),
                duration_delta_hours: durationDelta,
              }
            : {},
        enabled: true,
        cooldown_minutes: 30,
      },
      "模拟策略已保存",
    );
  return (
    <div className="two-columns strategy-layout">
      <Panel
        title="新建模拟策略"
        description="V1A只评估今日累计；同一策略内所有条件使用 AND。"
      >
        <Field label="监控计划">
          <Select value={target} onChange={(_, data) => setTarget(data.value)}>
            <option value="">请选择</option>
            {plans.map((plan) => (
              <option key={plan.target_uid} value={plan.target_uid}>
                {plan.plan_name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="策略名称">
          <Input value={title} onChange={(_, data) => setTitle(data.value)} />
        </Field>
        <div className="form-grid">
          <Field label="模拟动作">
            <Select
              value={strategyType}
              onChange={(_, data) => setStrategyType(data.value)}
            >
              <option value="retarget_create">新建追投候选</option>
              <option value="retarget_pause">暂停Scene 2追投任务</option>
              <option value="retarget_adjust">调整Scene 2追投任务</option>
            </Select>
          </Field>
          <Field label="触发层级">
            <Select value={level} onChange={(_, data) => setLevel(data.value)}>
              <option value="material">素材级</option>
              <option value="product">商品级</option>
            </Select>
          </Field>
          <Field label="优先级（越小越高）">
            <Input
              type="number"
              min={1}
              value={priority}
              onChange={(_, data) => setPriority(data.value)}
            />
          </Field>
          <Field label="指标">
            <Select
              value={metric}
              onChange={(_, data) => setMetric(data.value)}
            >
              <option value="spend_cent">消耗（分）</option>
              <option value="order_count">成交订单数</option>
              <option value="gmv_cent">成交金额（分）</option>
              <option value="roi_decimal">ROI</option>
            </Select>
          </Field>
          <Field label="比较">
            <Select
              value={operator}
              onChange={(_, data) => setOperator(data.value)}
            >
              <option value="gt">大于</option>
              <option value="gte">大于等于</option>
              <option value="lt">小于</option>
              <option value="lte">小于等于</option>
              <option value="between">区间（含边界）</option>
            </Select>
          </Field>
        </div>
        {operator === "between" ? (
          <div className="form-grid">
            <Field label="区间下限">
              <Input value={value} onChange={(_, data) => setValue(data.value)} />
            </Field>
            <Field label="区间上限">
              <Input value={maxValue} onChange={(_, data) => setMaxValue(data.value)} />
            </Field>
          </div>
        ) : (
          <Field label="阈值">
            <Input value={value} onChange={(_, data) => setValue(data.value)} />
          </Field>
        )}
        {strategyType === "retarget_adjust" && (
          <div className="form-grid">
            <Field label="预算增加（分）">
              <Input
                type="number"
                min={0}
                value={budgetDelta}
                onChange={(_, data) => setBudgetDelta(data.value)}
              />
            </Field>
            <Field label="时长增加（小时）">
              <Input
                type="number"
                min={0}
                step="0.1"
                value={durationDelta}
                onChange={(_, data) => setDurationDelta(data.value)}
              />
            </Field>
          </div>
        )}
        <div className="dry-run-callout">
          <BeakerRegular /> 保存后只会冻结模拟候选，不会调用千川写接口。
        </div>
        <Button
          appearance="primary"
          disabled={!target}
          onClick={() => void save()}
        >
          保存并启用模拟策略
        </Button>
      </Panel>
      <Panel
        title="已配置策略"
        description="同一对象命中多条策略时，只采用优先级数字最小的一条。"
      >
        {items.length ? (
          <div className="stack-list">
            {items.map((item) => (
              <div className="strategy-row" key={item.strategy_id}>
                <div>
                  <strong>{item.title}</strong>
                  <small>
                    优先级 {item.priority} · {item.strategy_type} ·{" "}
                    {item.trigger_level === "product" ? "商品级" : "素材级"} ·
                    版本 {item.version}
                  </small>
                </div>
                <Switch
                  checked={Boolean(item.enabled)}
                  onChange={(_, data) =>
                    void run(
                      "/api/v1/strategies/toggle",
                      { strategy_id: item.strategy_id, enabled: data.checked },
                      data.checked ? "策略已启用" : "策略已停用",
                    )
                  }
                />
              </div>
            ))}
          </div>
        ) : (
          <Empty
            title="暂无策略"
            description="先选择一个已监控计划并创建模拟策略。"
          />
        )}
      </Panel>
    </div>
  );
}

function CandidatePage({
  refreshKey,
  run,
}: {
  refreshKey: number;
  run: RunCommand;
}) {
  const [batches, setBatches] = useState<any[]>([]);
  const [active, setActive] = useState<any | null>(null);
  const [page, setPage] = useState<any | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [groups, setGroups] = useState<
    Array<{ name: string; mode: string; material_ids: string[] }>
  >([]);
  useEffect(() => {
    void api<any[]>("/api/v1/candidates").then((rows) => {
      setBatches(rows);
      setActive(
        (current: any | null) =>
          rows.find(
            (row) => row.candidate_batch_id === current?.candidate_batch_id,
          ) ??
          rows[0] ??
          null,
      );
    });
  }, [refreshKey]);
  useEffect(() => {
    setPageNumber(1);
    setSelected(new Set());
    setGroups([]);
  }, [active?.candidate_batch_id]);
  useEffect(() => {
    if (!active) return;
    void api<any>(
      `/api/v1/candidates/${active.candidate_batch_id}?page=${pageNumber}&page_size=20`,
    ).then(setPage);
  }, [active, pageNumber, refreshKey]);
  const addSelectedGroup = () => {
    if (!selected.size || selected.size > 20) return;
    setGroups((current) => [
      ...current,
      {
        name: `分组${current.length + 1}`,
        mode: "selected_group",
        material_ids: [...selected],
      },
    ]);
  };
  const makeSingleGroups = async () => {
    if (!active || !page) return;
    const all: string[] = [];
    for (let index = 1; index <= page.total_pages; index += 1) {
      const value = await api<any>(
        `/api/v1/candidates/${active.candidate_batch_id}?page=${index}&page_size=20`,
      );
      all.push(...value.items.map((item: any) => item.material_id));
    }
    setGroups([
      { name: "全部逐条分别成组", mode: "single_each", material_ids: all },
    ]);
  };
  const materials = page?.items ?? [];
  return (
    <div className="candidate-layout">
      <Panel
        className="candidate-list-panel"
        title="冻结候选批次"
        description="同一候选内容只保留一张活动卡；素材、指标或策略变化才生成新批次。"
      >
        {batches.length ? (
          <div className="account-list">
            {batches.map((batch) => (
              <button
                key={batch.candidate_batch_id}
                className={
                  active?.candidate_batch_id === batch.candidate_batch_id
                    ? "account-row active"
                    : "account-row"
                }
                onClick={() => setActive(batch)}
              >
                <div>
                  <strong>{batch.material_count} 条视频候选</strong>
                  <small>
                    {batch.created_at} · {batch.status}
                  </small>
                </div>
                <i>{batch.trigger_level === "product" ? "商品级" : "素材级"}</i>
              </button>
            ))}
          </div>
        ) : (
          <Empty
            title="暂无候选"
            description="完成一次完整、新鲜的素材采集并命中模拟策略后生成。"
          />
        )}
      </Panel>
      <Panel
        className="candidate-detail"
        title="素材分组模拟"
        description="单组最多20条；不同分组允许重复选择同一素材。"
        actions={
          active && (
            <Button
              onClick={() =>
                void run(
                  "/api/v1/candidates/send-preview",
                  { candidate_batch_id: active.candidate_batch_id },
                  "飞书V1A模拟预览已加入发送队列",
                )
              }
            >
              发送模拟卡
            </Button>
          )
        }
      >
        {!active ? (
          <Empty title="请选择候选批次" description="" />
        ) : (
          <>
            <div className="material-grid">
              {materials.map((material: any) => (
                <label
                  key={material.material_id}
                  className="material-candidate"
                >
                  <Checkbox
                    checked={selected.has(material.material_id)}
                    onChange={(_, data) =>
                      setSelected((previous) => {
                        const next = new Set(previous);
                        data.checked
                          ? next.add(material.material_id)
                          : next.delete(material.material_id);
                        return next;
                      })
                    }
                  />
                  <div>
                    <strong>
                      {material.material_name || `素材 ${material.material_id}`}
                    </strong>
                    <small>素材ID {material.material_id}</small>
                    <span>
                      订单 {material.metrics?.order_count ?? 0} · ROI{" "}
                      {material.metrics?.roi_decimal ?? "—"} · 成交 ¥
                      {formatCent(material.metrics?.gmv_cent)}
                    </span>
                  </div>
                </label>
              ))}
            </div>
            {page?.total_pages > 1 && (
              <div className="button-row">
                <Button
                  disabled={pageNumber <= 1}
                  onClick={() => setPageNumber((value) => value - 1)}
                >
                  上一页
                </Button>
                <span>
                  第 {pageNumber}/{page.total_pages} 页 · 共 {page.total} 条
                </span>
                <Button
                  disabled={pageNumber >= page.total_pages}
                  onClick={() => setPageNumber((value) => value + 1)}
                >
                  下一页
                </Button>
              </div>
            )}
            <div className="group-actions">
              <Button
                onClick={addSelectedGroup}
                disabled={!selected.size || selected.size > 20}
              >
                所选素材为一组
              </Button>
              <Tooltip
                content={
                  page?.total > 20
                    ? "单组最多20条，请用多选拆成多个分组"
                    : "将全部候选放入一组"
                }
                relationship="label"
              >
                <Button
                  onClick={() =>
                    setGroups([
                      {
                        name: "全部为一组",
                        mode: "all_group",
                        material_ids: materials.map(
                          (item: any) => item.material_id,
                        ),
                      },
                    ])
                  }
                  disabled={!materials.length || page?.total > 20}
                >
                  全部为一组
                </Button>
              </Tooltip>
              <Button
                onClick={() => void makeSingleGroups()}
                disabled={!materials.length}
              >
                全部逐条分别成组
              </Button>
            </div>
            <div className="group-list">
              {groups.map((group, index) => (
                <div key={`${group.name}-${index}`}>
                  <strong>{group.name}</strong>
                  <span>{group.material_ids.length} 条</span>
                  <button
                    aria-label={`删除${group.name}`}
                    onClick={() =>
                      setGroups((current) =>
                        current.filter((_, itemIndex) => itemIndex !== index),
                      )
                    }
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
            <Button
              appearance="primary"
              disabled={!groups.length}
              onClick={() =>
                void run(
                  "/api/v1/candidates/groups",
                  { candidate_batch_id: active.candidate_batch_id, groups },
                  "模拟分组已保存，未执行任何千川操作",
                )
              }
            >
              保存 Dry-run 分组
            </Button>
          </>
        )}
      </Panel>
    </div>
  );
}

function AdjustmentCandidatePanel({ refreshKey }: { refreshKey: number }) {
  const [items, setItems] = useState<any[]>([]);
  useEffect(() => {
    void api<any[]>("/api/v1/adjustment-candidates").then(setItems);
  }, [refreshKey]);
  return (
    <Panel
      title="Scene 2 暂停与调整模拟"
      description="仅展示素材追投任务的冻结候选；V1A不会创建或推进任何真实执行任务。"
    >
      <DataTable
        rows={items}
        columns={[
          "created_at",
          "plan_name",
          "task_name",
          "control_task_id",
          "action_type",
          "budget_before_cent",
          "budget_expected_after_cent",
          "duration_expected_after_hours_decimal",
          "status",
        ]}
        empty="暂无Scene 2暂停或调整候选"
      />
    </Panel>
  );
}

function FeishuPage({
  refreshKey,
  run,
}: {
  refreshKey: number;
  run: RunCommand;
}) {
  const [status, setStatus] = useState<any>({});
  const [appId, setAppId] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [code, setCode] = useState("");
  const load = useCallback(
    () => api<any>("/api/v1/feishu/status").then(setStatus),
    [],
  );
  useEffect(() => {
    void load();
  }, [load, refreshKey]);
  const save = async () => {
    try {
      const queued = await command("/api/v1/feishu/config", {
        app_id: appId,
        app_secret: appSecret,
      });
      const result =
        queued.status === "succeeded" ? queued : await waitJob(queued.job_uid);
      if (result.status !== "succeeded")
        throw new Error(result.error_message || "飞书凭据验证失败");
      const payload = result.result_json
        ? JSON.parse(result.result_json)
        : result.result;
      if (!payload?.valid)
        throw new Error(payload?.error || "飞书凭据验证失败");
      setAppSecret("");
      await load();
    } catch (error) {
      alert(errorMessage(error));
    }
  };
  const issue = async (purpose: string) => {
    const result: any = await run(
      "/api/v1/feishu/binding-code",
      { purpose },
      "一次性绑定码已生成",
    );
    const payload = result.result_json
      ? JSON.parse(result.result_json)
      : result.result;
    setCode(payload?.code || "请在任务结果中查看");
  };
  const states = [
    ["凭据有效", status.credential],
    ["传输连接", status.transport],
    ["事件接收", status.events],
    ["授权绑定", status.binding],
    ["消息发送", status.sending],
  ];
  return (
    <div className="two-columns">
      <Panel
        title="飞书长连接"
        description="无需公网IP、域名或Cloudflare。App Secret经Windows DPAPI加密，仅保存在本机。"
      >
        <div className="connection-grid">
          {states.map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <strong
                className={
                  value === "valid" ||
                  value === "connected" ||
                  value === "receiving" ||
                  value === "bound" ||
                  value === "ready"
                    ? "good-text"
                    : "warning-text"
                }
              >
                {value || "unknown"}
              </strong>
            </div>
          ))}
        </div>
        <Field label="App ID">
          <Input value={appId} onChange={(_, data) => setAppId(data.value)} />
        </Field>
        <Field label="App Secret">
          <Input
            type="password"
            value={appSecret}
            onChange={(_, data) => setAppSecret(data.value)}
          />
        </Field>
        <div className="button-row">
          <Button appearance="primary" onClick={() => void save()}>
            保存并验证凭据
          </Button>
          <Button
            onClick={() =>
              void run("/api/v1/feishu/reconnect", {}, "长连接启动请求已完成")
            }
          >
            连接/重连
          </Button>
        </div>
        <div className="permission-box">
          <strong>应用权限</strong>
          <code>im:message:send_as_bot</code>
          <code>im:message:update</code>
          <code>im:message.p2p_msg:readonly</code>
          <code>im:message.group_at_msg:readonly</code>
          <small>事件订阅：card.action.trigger、im.message.receive_v1</small>
        </div>
      </Panel>
      <Panel
        title="授权人与接收位置"
        description="绑定码10分钟有效、一次性使用。只有个人绑定者可以继续绑定群。"
      >
        <div className="button-row">
          <Button onClick={() => void issue("personal")}>生成个人绑定码</Button>
          <Button onClick={() => void issue("group")}>生成群绑定码</Button>
          <Button
            onClick={() =>
              void run("/api/v1/feishu/test-card", {}, "测试卡已发送")
            }
          >
            发送测试卡
          </Button>
        </div>
        {code && (
          <div className="binding-code">
            <span>请私聊机器人发送</span>
            <strong>绑定 {code}</strong>
          </div>
        )}
        <ol className="guide-list">
          <li>在飞书后台选择“使用长连接接收事件”。</li>
          <li>发布应用，并把自己加入应用可用范围。</li>
          <li>个人绑定：私聊机器人发送“绑定 123456”。</li>
          <li>群绑定：群内 @机器人发送“绑定群 123456”。</li>
        </ol>
      </Panel>
    </div>
  );
}

function OperationsPage({ refreshKey }: { refreshKey: number }) {
  const today = new Date();
  const seven = new Date(Date.now() - 6 * 86400000);
  const iso = (date: Date) => date.toISOString().slice(0, 10);
  const [from, setFrom] = useState(iso(seven));
  const [to, setTo] = useState(iso(today));
  const [source, setSource] = useState("");
  const [rows, setRows] = useState<any[]>([]);
  const [report, setReport] = useState<any>(null);
  const load = useCallback(async () => {
    const query = new URLSearchParams({
      date_from: from,
      date_to: to,
      ...(source ? { source } : {}),
    });
    setRows(await api<any[]>(`/api/v1/operation-events?${query}`));
  }, [from, to, source]);
  useEffect(() => {
    void load();
  }, [load, refreshKey]);
  const exportCsv = () => {
    const columns = [
      "event_time_beijing",
      "account_name",
      "aavid",
      "source_plan_name",
      "action_type",
      "source",
      "result_status",
      "error_message",
    ];
    const escape = (value: unknown) =>
      `"${String(value ?? "").replaceAll('"', '""')}"`;
    const text =
      "\ufeff" +
      [
        columns.join(","),
        ...rows.map((row) =>
          columns.map((column) => escape(row[column])).join(","),
        ),
      ].join("\r\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(
      new Blob([text], { type: "text/csv;charset=utf-8" }),
    );
    link.download = `千川账户操作流水_${from}_${to}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  };
  const preview = async () =>
    setReport(await api<any>(`/api/v1/daily-report?business_date=${to}`));
  return (
    <>
      <Panel
        title="账户操作流水"
        description="只展示平台操作、工具只读审计和明确的模拟审计；普通浏览器轨迹不会进入日报。"
        actions={
          <>
            <Button onClick={() => void load()}>查询</Button>
            <Button onClick={exportCsv}>导出当前结果</Button>
          </>
        }
      >
        <div className="toolbar">
          <Field label="开始日期">
            <Input
              type="date"
              value={from}
              onChange={(_, data) => setFrom(data.value)}
            />
          </Field>
          <Field label="结束日期">
            <Input
              type="date"
              value={to}
              onChange={(_, data) => setTo(data.value)}
            />
          </Field>
          <Field label="来源">
            <Select
              value={source}
              onChange={(_, data) => setSource(data.value)}
            >
              <option value="">全部</option>
              <option value="platform_log">平台操作日志</option>
              <option value="tool_direct">工具审计</option>
              <option value="simulation">V1A模拟</option>
            </Select>
          </Field>
          <Button onClick={() => void preview()}>预览日报</Button>
        </div>
        <DataTable
          rows={rows}
          columns={[
            "event_time_beijing",
            "account_name",
            "source_plan_name",
            "action_type",
            "source",
            "result_status",
          ]}
          empty="当前筛选范围无操作流水"
        />
      </Panel>
      {report && (
        <Panel title="日报双段预览">
          <pre className="json-preview">{JSON.stringify(report, null, 2)}</pre>
        </Panel>
      )}
    </>
  );
}

function DashboardPage({ refreshKey }: { refreshKey: number }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [candidates, setCandidates] = useState<any[]>([]);
  useEffect(() => {
    void Promise.all([
      api<Account[]>("/api/v1/accounts"),
      api<Plan[]>("/api/v1/plans"),
      api<any[]>("/api/v1/candidates"),
    ]).then(([a, p, c]) => {
      setAccounts(a);
      setPlans(p);
      setCandidates(c);
    });
  }, [refreshKey]);
  const complete = accounts.filter(
    (account) => account.catalog_status === "complete",
  ).length;
  return (
    <>
      <div className="metric-grid">
        <Metric
          label="主动添加账户"
          value={`${accounts.length}`}
          detail={`${complete} 个目录完整`}
        />
        <Metric
          label="已登记计划"
          value={`${plans.length}`}
          detail={`${plans.filter((plan) => plan.monitor_enabled).length} 个参与监控`}
        />
        <Metric
          label="可监控计划"
          value={`${plans.filter((plan) => plan.monitor_eligible).length}`}
          detail="四类身份与状态已确认"
        />
        <Metric
          label="模拟候选批次"
          value={`${candidates.length}`}
          detail="不计入真实追投或停投"
        />
      </div>
      <Panel
        title="只读能力概览"
        description="数据大屏是次级观察页面，不承担首次配置入口。"
      >
        <div className="dashboard-bars">
          {["全域·推商品", "全域·推直播", "乘方·推商品", "乘方·推直播"].map(
            (label, index) => {
              const keys = [
                ["global", "product"],
                ["global", "live"],
                ["chengfang", "product"],
                ["chengfang", "live"],
              ][index];
              const count = plans.filter(
                (plan) =>
                  plan.plan_system === keys[0] &&
                  plan.promotion_scene === keys[1],
              ).length;
              return (
                <div key={label}>
                  <span>{label}</span>
                  <div>
                    <i style={{ width: `${Math.min(100, count * 8)}%` }} />
                  </div>
                  <strong>{count}</strong>
                </div>
              );
            },
          )}
        </div>
      </Panel>
    </>
  );
}

function RecoveryPage({
  refreshKey,
  run,
  health,
}: {
  refreshKey: number;
  run: RunCommand;
  health: Health;
}) {
  const [data, setData] = useState<any>({ sources: [], runs: [] });
  const [caps, setCaps] = useState<any[]>([]);
  const [evidence, setEvidence] = useState<any[]>([]);
  const load = useCallback(
    () =>
      Promise.all([
        api<any>("/api/v1/migrations"),
        api<any[]>("/api/v1/capabilities"),
        api<any[]>("/api/v1/adapter-evidence"),
      ]).then(([migrations, capabilities, adapterEvidence]) => {
        setData(migrations);
        setCaps(capabilities);
        setEvidence(adapterEvidence);
      }),
    [],
  );
  useEffect(() => {
    void load();
  }, [load, refreshKey]);
  return (
    <>
      <div className="two-columns">
        <Panel
          title="旧版数据迁移"
          description="只允许人工选择一套 v0.1.46 主数据源；不合并，不修改原库，执行前生成快照和清单。"
          actions={
            <Button
              onClick={() =>
                void run("/api/v1/migrations/scan", {}, "旧版数据源扫描完成")
              }
            >
              扫描数据源
            </Button>
          }
        >
          {data.sources?.length ? (
            <div className="stack-list">
              {data.sources.map((source: any) => (
                <div className="migration-row" key={source.source_uid}>
                  <div>
                    <strong>{source.database_path}</strong>
                    <small>
                      更新 {source.modified_at} · 账户 {source.account_count} ·
                      计划 {source.plan_count} · 流水 {source.operation_count}
                    </small>
                  </div>
                  <Button
                    appearance="primary"
                    onClick={() =>
                      confirm(
                        "确认将此数据副本迁入当前V1A管理员？原库不会修改。",
                      ) &&
                      void run(
                        "/api/v1/migrations/execute",
                        { source_uid: source.source_uid },
                        "迁移完成，已生成报告和回滚说明",
                      )
                    }
                  >
                    选择并迁移
                  </Button>
                </div>
              ))}
            </div>
          ) : (
            <Empty
              title="尚未扫描到数据源"
              description="点击扫描后会展示每套数据的更新时间和规模，系统不会自动合并。"
            />
          )}
        </Panel>
        <Panel title="运行诊断">
          <DataTable
            rows={[
              { item: "SQLite完整性", status: health.database.integrity },
              {
                item: "网络写入守卫",
                status: health.real_platform_writes.network_guard,
              },
              {
                item: "数据库执行守卫",
                status: health.real_platform_writes.database_guard,
              },
              {
                item: "真实写接口注册",
                status: health.real_platform_writes.registered ? "危险" : "0个",
              },
              {
                item: "采集容量",
                status: health.collection_capacity?.state || "unknown",
              },
              {
                item: "磁盘保护",
                status: health.collection_capacity?.disk_state || "unknown",
              },
            ]}
            columns={["item", "status"]}
            empty=""
          />
        </Panel>
      </div>
      <Panel
        title="迁移记录与恢复"
        description="恢复会先保存当前运行库，随后在下一次启动时应用迁移前快照。"
      >
        {data.runs?.length ? (
          <div className="stack-list">
            {data.runs.map((item: any) => (
              <div className="migration-row" key={item.migration_uid}>
                <div>
                  <strong>{item.migration_uid}</strong>
                  <small>
                    {item.status} · {item.started_at} ·{" "}
                    {item.report_path || "暂无报告"}
                  </small>
                </div>
                <Button
                  disabled={item.status !== "succeeded"}
                  onClick={() =>
                    confirm(
                      "确认恢复到此次迁移前的运行库？当前运行库会先生成安全副本，工具需重新启动。",
                    ) &&
                    void run(
                      "/api/v1/migrations/restore",
                      { migration_uid: item.migration_uid },
                      "恢复申请已保存，请重新启动工具",
                    )
                  }
                >
                  一键恢复
                </Button>
              </div>
            ))}
          </div>
        ) : (
          <Empty
            title="暂无迁移记录"
            description="完成一次迁移后，可在这里查看报告或申请恢复。"
          />
        )}
      </Panel>
      <Panel
        title="四类适配器能力矩阵"
        description="V1A所有写能力必须保持 blocked_by_evidence 或 dry_run_ready。"
      >
        <DataTable
          rows={caps}
          columns={[
            "adapter_key",
            "adapter_version",
            "evidence_level",
            "read_catalog",
            "read_video_material",
            "read_control_tasks",
            "create_retarget",
            "pause_task",
            "adjust_task",
          ]}
          empty="能力矩阵未加载"
        />
      </Panel>
      <Panel
        title="只读契约证据"
        description="展示真实采集时观测到的脱敏响应结构指纹；同一能力出现新指纹时需重新核验。"
      >
        <DataTable
          rows={evidence}
          columns={[
            "adapter_name",
            "adapter_version",
            "capability_name",
            "dataset_key",
            "response_schema_hash",
            "evidence_level",
            "last_seen_at",
          ]}
          empty="尚无真实只读采集证据"
        />
      </Panel>
    </>
  );
}

function Panel({
  title,
  description,
  actions,
  className = "",
  children,
}: {
  title: string;
  description?: string;
  actions?: any;
  className?: string;
  children: any;
}) {
  return (
    <section className={`panel ${className}`}>
      <header>
        <div>
          <h2>{title}</h2>
          {description && <p>{description}</p>}
        </div>
        {actions && <div className="panel-actions">{actions}</div>}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}
function Metric({
  label,
  value,
  detail,
  tone = "neutral",
}: {
  label: string;
  value: string;
  detail: string;
  tone?: string;
}) {
  return (
    <div className={`metric ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}
function Empty({ title, description }: { title: string; description: string }) {
  return (
    <div className="empty">
      <BookDatabaseRegular />
      <strong>{title}</strong>
      <span>{description}</span>
    </div>
  );
}
function DataTable({
  rows,
  columns,
  empty,
}: {
  rows: any[];
  columns: string[];
  empty: string;
}) {
  if (!rows.length) return <Empty title={empty} description="" />;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={row.event_uid || row.run_uid || row.control_task_id || index}
            >
              {columns.map((column) => (
                <td key={column}>{formatCell(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function JobRail({ jobs }: { jobs: Job[] }) {
  const active = jobs.filter((job) =>
    ["queued", "running"].includes(job.status),
  );
  if (!active.length) return null;
  return (
    <aside className="job-rail">
      {active.slice(-3).map((job) => (
        <div key={job.job_uid}>
          <Spinner size="tiny" />
          <span>
            <strong>{job.job_type}</strong>
            <small>{job.progress_message || job.status}</small>
          </span>
          {job.progress_total > 0 && (
            <b>
              {job.progress_current}/{job.progress_total}
            </b>
          )}
        </div>
      ))}
    </aside>
  );
}

type RunCommand = (
  path: string,
  body: Record<string, unknown>,
  successMessage: string,
) => Promise<any>;
function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
function formatCell(value: unknown) {
  if (typeof value === "boolean") return value ? "是" : "否";
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
function formatCent(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? (number / 100).toFixed(2) : "0.00";
}
function catalogLabel(status: string) {
  return (
    (
      {
        complete: "目录完整",
        partial: "同步不完整",
        suspicious_empty: "异常空结果",
        never_synced: "待同步",
      } as Record<string, string>
    )[status] || status
  );
}

export default App;
