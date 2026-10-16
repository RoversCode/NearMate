import { useEffect, useState, type ReactNode } from "react";
import {
  BellOff,
  ChevronRight,
  Circle,
  CircleAlert,
  ListChecks,
  Sparkles,
  WalletCards
} from "lucide-react";
import {
  MAX_TIMED_REMINDER_CONTENT_LENGTH,
  type ActivityCompact,
  type TimedReminderCompact
} from "../types/backendEvents";
import type { TimedReminderUpdate } from "../lib/timedReminderClient";
import type { ChatRuntime, ChatVoiceState } from "./useChatRuntime";
import { ChatVoiceOrb } from "./ChatVoiceOrb";
import type { LlmAccountStatusSnapshot, LlmAccountProvider } from "./llmAccountStatus";
import type { CompanionStatusPresentation } from "../companion/companionStatus";

type Workspace = ChatRuntime["workspace"];

/**
 * ChatStatusRail 投影当前角色、语音、模型账户、活动任务、提醒和已启用 Skill。
 * 右栏始终存在；动态区为空时保留简短空态，不改变窗口结构。
 * 余额传输失败和厂商查询失败共用整组重试入口。
 *
 * @param props 状态栏所需的角色、工作区和账户余额投影。
 * @returns 含加载、失败与成功状态的聊天右栏。
 */
export function ChatStatusRail(props: {
  companionName: string;
  voiceState: ChatVoiceState;
  companionStatus?: CompanionStatusPresentation;
  workspace: Workspace;
  llmAccountStatus?: LlmAccountStatusSnapshot;
}) {
  const llmAccountStatus = props.llmAccountStatus ?? props.workspace.llmAccountStatus;
  // 厂商错误会随 HTTP 200 返回，重试入口必须同时检查账户状态和传输错误。
  const hasLlmAccountError = Boolean(props.workspace.llmAccountStatusError)
    || llmAccountStatus?.providers.some(provider => provider.status === "error");
  const now = useCurrentTime(
    props.workspace.activitySnapshot.active.length > 0
      || props.workspace.reminderSnapshot.active.length > 0
  );
  const ringing = props.workspace.reminderSnapshot.active.filter(item => item.state === "ringing");
  const scheduled = props.workspace.reminderSnapshot.active.filter(item => item.state === "scheduled");

  return (
    <aside className="chat-status-rail" aria-label="当前状态">
      <ChatVoiceOrb state={props.voiceState} companionName={props.companionName} companionStatus={props.companionStatus} />
      {props.workspace.runtimeRestartMessage ? (
        <p className="chat-status-notice" role="status">
          <CircleAlert size={14} aria-hidden="true" />
          {props.workspace.runtimeRestartMessage}
        </p>
      ) : null}
      {props.workspace.errors.activity || props.workspace.errors.reminder ? (
        <p className="chat-status-error" role="status">
          {props.workspace.errors.activity ?? props.workspace.errors.reminder}
        </p>
      ) : null}
      <StatusGroup
        icon={<WalletCards size={15} aria-hidden="true" />}
        title="模型账户余额"
        count={llmAccountStatus?.providers.length ?? null}
      >
        {props.workspace.llmAccountStatusError ? (
          <div role="status">
            <StatusEmpty message={props.workspace.llmAccountStatusError} />
          </div>
        ) : !llmAccountStatus ? (
          <StatusEmpty message="正在读取余额…" />
        ) : llmAccountStatus.providers.length === 0 ? (
          <StatusEmpty message="暂无已配置的模型账户" />
        ) : (
          <div className="chat-llm-account-list">
            {llmAccountStatus.providers.map(provider => (
              <LlmAccountCard key={provider.provider_id} provider={provider} />
            ))}
          </div>
        )}
        {hasLlmAccountError && props.workspace.refreshLlmAccountStatus ? (
          <div className="chat-status-card-actions">
            <button type="button" onClick={() => void props.workspace.refreshLlmAccountStatus?.()}>
              重新读取余额
            </button>
          </div>
        ) : null}
      </StatusGroup>
      <StatusGroup
        icon={<ListChecks size={15} aria-hidden="true" />}
        title="进行中的任务"
        count={props.workspace.activitySnapshot.active.length}
      >
        {props.workspace.activitySnapshot.active.length === 0 ? (
          <StatusEmpty message="当前没有活动任务" />
        ) : props.workspace.activitySnapshot.active.map(activity => (
          <ActivityCard
            key={activity.activity_id}
            activity={activity}
            now={now}
            requesting={props.workspace.requestingActivityIds.has(activity.activity_id)}
            onCancel={() => void props.workspace.requestCancelActivity(activity.activity_id)}
            onDetail={() => props.workspace.openActivityDetail(activity.activity_id)}
          />
        ))}
      </StatusGroup>
      <StatusGroup
        icon={<BellOff size={15} aria-hidden="true" />}
        title="提醒"
        count={props.workspace.reminderSnapshot.active.length}
      >
        {ringing.length > 0 ? (
          <RingingReminderGroup
            reminders={ringing}
            runtimeId={props.workspace.reminderSnapshot.runtime_id}
            requestingIds={props.workspace.requestingReminderIds}
            onDismiss={ids => void props.workspace.requestDismissReminders(
              props.workspace.reminderSnapshot.runtime_id,
              ids
            )}
          />
        ) : null}
        {scheduled.length === 0 && ringing.length === 0 ? (
          <StatusEmpty message="当前没有待处理提醒" detail="一切都很顺利～" icon={<BellOff size={26} aria-hidden="true" />} />
        ) : null}
        {scheduled.map(reminder => (
          <ReminderCard
            key={reminder.reminder_id}
            reminder={reminder}
            now={now}
            requesting={props.workspace.requestingReminderIds.has(reminder.reminder_id)}
            onUpdate={update => props.workspace.requestUpdateReminder(reminder.reminder_id, update)}
            onCancel={() => void props.workspace.requestCancelReminder(reminder.reminder_id)}
          />
        ))}
      </StatusGroup>
      <StatusGroup
        icon={<Sparkles size={15} aria-hidden="true" />}
        title="已启用 Skill"
        count={props.workspace.enabledSkills.length}
      >
        {props.workspace.enabledSkills.length === 0 ? (
          <StatusEmpty message="暂无已启用 Skill" />
        ) : (
          <ul className="chat-skill-list">
            {props.workspace.enabledSkills.map(skill => (
              <li key={skill.id} data-ready={skill.ready && skill.usable}>
                <span>{skill.name}</span>
                {!skill.usable ? <small>当前不可用</small> : null}
              </li>
            ))}
          </ul>
        )}
      </StatusGroup>
    </aside>
  );
}

function LlmAccountCard(props: { provider: LlmAccountProvider }) {
  const { provider } = props;
  const balanceText = provider.status === "available"
    ? `余额 ${provider.balance ?? "—"}${provider.currency ? ` ${provider.currency}` : ""}`
    : provider.message ?? accountStatusLabel(provider.status);
  return (
    <article className="chat-status-card chat-llm-account-card" data-status={provider.status}>
      <div className="chat-status-card-main">
        <span className="chat-status-card-icon" aria-hidden="true"><WalletCards size={20} strokeWidth={1.8} /></span>
        <div className="chat-status-card-copy">
          <strong>{provider.display_name}</strong>
          <span>{provider.active ? "当前对话" : "已配置"}</span>
        </div>
      </div>
      <small className="chat-status-card-time">{balanceText}</small>
    </article>
  );
}

function accountStatusLabel(status: LlmAccountProvider["status"]): string {
  return {
    available: "余额已读取",
    not_configured: "尚未配置 API Key",
    unsupported: "暂不支持余额查询",
    error: "暂时无法读取余额"
  }[status];
}

function StatusGroup(props: {
  icon: ReactNode;
  title: string;
  count: number | null;
  children: ReactNode;
}) {
  return (
    <section className="chat-status-group" data-count={props.count}>
      <div className="chat-status-group-heading">
        <h2>{props.icon}<span>{props.title}</span><span className="chat-status-group-count">{props.count ?? "—"}</span></h2>
        <ChevronRight className="chat-status-group-chevron" size={17} aria-hidden="true" />
      </div>
      <div className="chat-status-group-body">{props.children}</div>
    </section>
  );
}

function StatusEmpty(props: { message: string; detail?: string; icon?: ReactNode }) {
  return (
    <div className="chat-status-empty" data-empty="true">
      {props.icon ? <span className="chat-status-empty-icon">{props.icon}</span> : null}
      <strong>{props.message}</strong>
      {props.detail ? <small>{props.detail}</small> : null}
    </div>
  );
}

function ActivityCard(props: {
  activity: ActivityCompact;
  now: number;
  requesting: boolean;
  onCancel(): void;
  onDetail(): void;
}) {
  const { activity } = props;
  const isActive = activity.finished_at === null;
  const canCancel = isActive && activity.can_cancel && !props.requesting;
  return (
    <article className="chat-status-card chat-activity-card" data-status={activity.status}>
      <div className="chat-status-card-main">
        <span className="chat-status-card-icon" aria-hidden="true"><Circle size={22} strokeWidth={1.8} /></span>
        <div className="chat-status-card-copy">
          <strong title={activity.goal_summary}>{activity.goal_summary || activity.type_label}</strong>
          <span>{[activity.type_label, activity.stage_label].filter(Boolean).join(" · ")}</span>
        </div>
      </div>
      <small className="chat-status-card-time">{isActive ? formatDuration(Math.max(0, props.now - Date.parse(activity.started_at ?? ""))) : statusLabel(activity.status)}</small>
      <div className="chat-status-card-actions">
        <button type="button" onClick={props.onDetail}>详情</button>
        {isActive ? (
          <button type="button" disabled={!canCancel} onClick={props.onCancel}>
            {props.requesting ? "取消中" : "取消"}
          </button>
        ) : null}
      </div>
    </article>
  );
}

function RingingReminderGroup(props: {
  reminders: TimedReminderCompact[];
  runtimeId: string;
  requestingIds: ReadonlySet<string>;
  onDismiss(ids: string[]): void;
}) {
  const ids = props.reminders.map(item => item.reminder_id);
  const requesting = ids.some(id => props.requestingIds.has(id));
  return (
    <div className="chat-ringing-reminders" data-status="ringing">
      {props.reminders.map(reminder => <strong key={reminder.reminder_id}>{reminder.content}</strong>)}
      <button
        type="button"
        disabled={requesting || !props.runtimeId}
        onClick={() => props.onDismiss(ids)}
      >
        <BellOff size={13} aria-hidden="true" /> {requesting ? "处理中" : "知道了"}
      </button>
    </div>
  );
}

function ReminderCard(props: {
  reminder: TimedReminderCompact;
  now: number;
  requesting: boolean;
  onUpdate(update: TimedReminderUpdate): Promise<void>;
  onCancel(): void;
}) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(props.reminder.content);
  const [triggerAt, setTriggerAt] = useState(() => toDateTimeLocal(props.reminder.trigger_at));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!editing) {
      setContent(props.reminder.content);
      setTriggerAt(toDateTimeLocal(props.reminder.trigger_at));
    }
  }, [editing, props.reminder.content, props.reminder.trigger_at]);

  async function save(): Promise<void> {
    const nextContent = content.trim();
    if (!nextContent) {
      setError("请填写提醒内容。");
      return;
    }
    if (Array.from(nextContent).length > MAX_TIMED_REMINDER_CONTENT_LENGTH) {
      setError("提醒内容不能超过五百个字符");
      return;
    }
    const update: TimedReminderUpdate = {};
    if (nextContent !== props.reminder.content) update.content = nextContent;
    if (triggerAt !== toDateTimeLocal(props.reminder.trigger_at)) {
      const nextDate = new Date(triggerAt);
      if (!Number.isFinite(nextDate.getTime())) {
        setError("请填写有效时间。");
        return;
      }
      update.triggerAt = nextDate.toISOString();
    }
    if (Object.keys(update).length === 0) {
      setEditing(false);
      return;
    }
    setError(null);
    try {
      await props.onUpdate(update);
      setEditing(false);
    } catch {
      // 权威错误由右栏统一展示，保留编辑态方便修正。
    }
  }

  if (editing) {
    return (
      <article className="chat-status-card chat-reminder-card" data-status="scheduled">
        <label>内容<input value={content} disabled={props.requesting} onChange={event => setContent(event.currentTarget.value)} /></label>
        <label>时间<input type="datetime-local" step={1} value={triggerAt} disabled={props.requesting} onChange={event => setTriggerAt(event.currentTarget.value)} /></label>
        {error ? <small className="chat-status-inline-error">{error}</small> : null}
        <div className="chat-status-card-actions">
          <button type="button" disabled={props.requesting} onClick={() => void save()}>{props.requesting ? "保存中" : "保存"}</button>
          <button type="button" disabled={props.requesting} onClick={() => setEditing(false)}>放弃</button>
        </div>
      </article>
    );
  }

  return (
    <article className="chat-status-card chat-reminder-card" data-status={props.reminder.state}>
      <div className="chat-status-card-main">
        <span className="chat-status-card-icon" aria-hidden="true"><Circle size={22} strokeWidth={1.8} /></span>
        <div className="chat-status-card-copy">
          <strong title={props.reminder.content}>{props.reminder.content}</strong>
          <span>{formatReminderTime(props.reminder.trigger_at)}</span>
        </div>
      </div>
      <small className="chat-status-card-time">还有 {formatCountdown(Math.max(0, Date.parse(props.reminder.trigger_at) - props.now))}</small>
      <div className="chat-status-card-actions">
        {props.reminder.can_modify ? <button type="button" disabled={props.requesting} onClick={() => setEditing(true)}>修改</button> : null}
        {props.reminder.can_cancel ? <button type="button" disabled={props.requesting} onClick={props.onCancel}>{props.requesting ? "处理中" : "取消"}</button> : null}
      </div>
    </article>
  );
}

function useCurrentTime(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [enabled]);
  return now;
}

function statusLabel(status: ActivityCompact["status"]): string {
  return {
    running: "运行中",
    waiting_user: "等待输入",
    cancelling: "取消中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断"
  }[status];
}

function formatDuration(value: number): string {
  const seconds = Math.floor(value / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatReminderTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false
  }).format(new Date(value));
}

function formatCountdown(value: number): string {
  const seconds = Math.ceil(value / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function toDateTimeLocal(value: string): string {
  const date = new Date(value);
  const part = (number: number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${part(date.getMonth() + 1)}-${part(date.getDate())}T${part(date.getHours())}:${part(date.getMinutes())}:${part(date.getSeconds())}`;
}
