import { useEffect, useState } from "react";
import { BellOff } from "lucide-react";
import type {
  TimedReminder,
  TimedReminderSnapshot
} from "../../api/contracts/timedReminders";
import { MAX_TIMED_REMINDER_CONTENT_LENGTH } from "../../api/contracts/timedReminders";
import type { TimedReminderUpdate } from "../../api/services/timedReminders";
import { EmptyState } from "../../components/layout/EmptyState";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Input } from "../../components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from "../../components/ui/table";
import {
  formatTimedReminderCountdown,
  formatTimedReminderState,
  formatTimedReminderTime,
  toDateTimeLocal
} from "./timedReminderPresentation";

type TimedReminderListProps = {
  snapshot: TimedReminderSnapshot;
  requestingIds: ReadonlySet<string>;
  now: Date;
  onUpdate(reminderId: string, update: TimedReminderUpdate): Promise<boolean>;
  onCancel(reminderId: string): void;
  onDismiss(runtimeId: string, reminderIds: readonly string[]): void;
};

/**
 * 展示当前运行期的活跃提醒和最近终态记录。
 *
 * Args:
 *   props: Reminder 权威快照、页面共享时钟、请求占位和管理动作。
 *
 * Returns:
 *   支持倒计时、行内修改和取消的两段提醒表格。
 */
export function TimedReminderList(props: TimedReminderListProps): JSX.Element {
  if (props.snapshot.active.length === 0 && props.snapshot.recent.length === 0) {
    return <EmptyState>本次运行还没有定时提醒。</EmptyState>;
  }
  const scheduled = props.snapshot.active.filter(reminder => reminder.state === "scheduled");
  const ringing = props.snapshot.active.filter(reminder => reminder.state === "ringing");
  const ringingIds = ringing.map(reminder => reminder.reminder_id);
  const dismissing = ringingIds.some(reminderId => props.requestingIds.has(reminderId));
  return (
    <div className="space-y-5">
      <ReminderTable
        reminders={ringing}
        emptyCopy="当前没有正在提醒的事项。"
        title="正在提醒"
        headerAction={ringing.length > 0 ? (
          <Button
            disabled={dismissing}
            onClick={() => props.onDismiss(props.snapshot.runtime_id, ringingIds)}
            size="sm"
          >
            <BellOff aria-hidden="true" className="mr-2 size-4" />
            {dismissing ? "处理中" : "全部知道了"}
          </Button>
        ) : null}
        {...props}
      />
      <ReminderTable
        reminders={scheduled}
        emptyCopy="当前没有等待中的提醒。"
        title="即将提醒"
        {...props}
      />
      <ReminderTable
        reminders={props.snapshot.recent}
        emptyCopy="本次运行还没有终态提醒。"
        title="最近提醒"
        {...props}
      />
    </div>
  );
}

type ReminderTableProps = TimedReminderListProps & {
  reminders: TimedReminder[];
  emptyCopy: string;
  title: string;
  headerAction?: JSX.Element | null;
};

/** 渲染同一生命周期分区的提醒表格。 */
function ReminderTable({
  reminders,
  emptyCopy,
  title,
  headerAction,
  requestingIds,
  now,
  onUpdate,
  onCancel
}: ReminderTableProps): JSX.Element {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3 space-y-0 pb-3">
        <CardTitle className="text-base">{title}</CardTitle>
        {headerAction}
      </CardHeader>
      <CardContent>
        {reminders.length === 0 ? (
          <p className="text-sm text-muted-foreground">{emptyCopy}</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>提醒内容</TableHead>
                <TableHead>触发时间</TableHead>
                <TableHead>倒计时</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {reminders.map(reminder => (
                <TimedReminderRow
                  key={reminder.reminder_id}
                  reminder={reminder}
                  requesting={requestingIds.has(reminder.reminder_id)}
                  now={now}
                  onUpdate={onUpdate}
                  onCancel={onCancel}
                />
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

type TimedReminderRowProps = {
  reminder: TimedReminder;
  requesting: boolean;
  now: Date;
  onUpdate(reminderId: string, update: TimedReminderUpdate): Promise<boolean>;
  onCancel(reminderId: string): void;
};

/** 渲染一条提醒，并在本行内完成修改。 */
function TimedReminderRow(props: TimedReminderRowProps): JSX.Element {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(props.reminder.content);
  const [triggerAt, setTriggerAt] = useState(() => toDateTimeLocal(props.reminder.trigger_at));
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    if (editing) {
      return;
    }
    setContent(props.reminder.content);
    setTriggerAt(toDateTimeLocal(props.reminder.trigger_at));
  }, [editing, props.reminder.content, props.reminder.trigger_at]);

  /** 校验本行草稿，只提交相对权威快照实际变化的字段。 */
  async function submitUpdate(): Promise<void> {
    const normalizedContent = content.trim();
    if (!normalizedContent) {
      setLocalError("请填写提醒内容。");
      return;
    }
    if (Array.from(normalizedContent).length > MAX_TIMED_REMINDER_CONTENT_LENGTH) {
      setLocalError("提醒内容不能超过五百个字符。");
      return;
    }
    const update: TimedReminderUpdate = {};
    if (normalizedContent !== props.reminder.content) {
      update.content = normalizedContent;
    }
    if (triggerAt !== toDateTimeLocal(props.reminder.trigger_at)) {
      const parsedTriggerAt = new Date(triggerAt);
      if (!Number.isFinite(parsedTriggerAt.getTime())) {
        setLocalError("请填写有效时间。");
        return;
      }
      update.triggerAt = parsedTriggerAt.toISOString();
    }
    if (Object.keys(update).length === 0) {
      setEditing(false);
      return;
    }
    setLocalError(null);
    const updated = await props.onUpdate(props.reminder.reminder_id, update);
    if (updated) {
      setEditing(false);
    }
  }

  if (editing) {
    return (
      <TableRow data-state={props.reminder.state}>
        <TableCell colSpan={5}>
          <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_15rem_auto] lg:items-end">
            <label className="grid gap-1.5 text-xs font-medium text-muted-foreground">
              提醒内容
              <Input
                disabled={props.requesting}
                onChange={event => setContent(event.currentTarget.value)}
                value={content}
              />
            </label>
            <label className="grid gap-1.5 text-xs font-medium text-muted-foreground">
              提醒时间
              <Input
                disabled={props.requesting}
                onChange={event => setTriggerAt(event.currentTarget.value)}
                step={1}
                type="datetime-local"
                value={triggerAt}
              />
            </label>
            <div className="flex gap-2">
              <Button disabled={props.requesting} onClick={() => void submitUpdate()} size="sm">
                {props.requesting ? "保存中" : "保存"}
              </Button>
              <Button
                disabled={props.requesting}
                onClick={() => setEditing(false)}
                size="sm"
                variant="outline"
              >
                放弃
              </Button>
            </div>
          </div>
          {localError ? <p className="mt-2 text-xs text-destructive">{localError}</p> : null}
        </TableCell>
      </TableRow>
    );
  }

  const active = props.reminder.finished_at === null;
  return (
    <TableRow data-state={props.reminder.state}>
      <TableCell className="max-w-xl">
        <span className="block truncate font-medium" title={props.reminder.content}>
          {props.reminder.content}
        </span>
      </TableCell>
      <TableCell className="whitespace-nowrap text-muted-foreground">
        {formatTimedReminderTime(props.reminder.trigger_at)}
      </TableCell>
      <TableCell className="whitespace-nowrap text-muted-foreground">
        {active
          ? props.reminder.state === "ringing"
            ? "到点"
            : formatTimedReminderCountdown(props.reminder.trigger_at, props.now)
          : "—"}
      </TableCell>
      <TableCell>
        <Badge variant={props.reminder.state === "ringing" ? "warning" : "outline"}>
          {formatTimedReminderState(props.reminder.state, props.reminder.finish_reason)}
        </Badge>
      </TableCell>
      <TableCell>
        <div className="flex justify-end gap-2">
          {props.reminder.can_modify ? (
            <Button
              disabled={props.requesting}
              onClick={() => setEditing(true)}
              size="sm"
              variant="outline"
            >
              修改
            </Button>
          ) : null}
          {props.reminder.can_cancel ? (
            <Button
              disabled={props.requesting}
              onClick={() => props.onCancel(props.reminder.reminder_id)}
              size="sm"
              variant="destructive"
            >
              {props.requesting ? "处理中" : "取消"}
            </Button>
          ) : null}
        </div>
      </TableCell>
    </TableRow>
  );
}
