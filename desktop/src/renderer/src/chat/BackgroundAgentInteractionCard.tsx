import { useState } from "react";
import { Check, CircleX, LoaderCircle, Send, ShieldAlert } from "lucide-react";
import type { BackgroundAgentInteractionAction } from "./backgroundAgentInteractionClient";
import type { ChatAgentInteraction, ChatAgentQuestion } from "./chatReducer";

type BackgroundAgentInteractionCardProps = {
  interaction: ChatAgentInteraction;
  onAnswer(answer: BackgroundAgentInteractionAction): Promise<void>;
};

/**
 * 渲染后台智能体暂停时的权限审批或资料问题卡片。
 *
 * @param props 当前交互事实和提交回答的异步端口。
 * @returns 待处理表单或已经收口的只读状态卡。
 */
export function BackgroundAgentInteractionCard({
  interaction,
  onAnswer
}: BackgroundAgentInteractionCardProps) {
  const [selectedAnswers, setSelectedAnswers] = useState<Record<string, string[]>>({});
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  async function submit(answer: BackgroundAgentInteractionAction): Promise<void> {
    setIsSubmitting(true);
    setSubmitError(null);
    try {
      await onAnswer(answer);
    } catch {
      setSubmitError("提交失败，请重试。");
    } finally {
      setIsSubmitting(false);
    }
  }

  if (interaction.status === "resolved") {
    return (
      <section
        className="chat-agent-interaction-card"
        data-status="resolved"
        data-background-interaction-id={interaction.requestId}
        tabIndex={-1}
      >
        <div className="chat-agent-interaction-title">
          <Check size={16} aria-hidden="true" />
          <strong>{resolvedInteractionLabel(interaction)}</strong>
        </div>
      </section>
    );
  }

  if (interaction.status === "processing") {
    return (
      <section
        className="chat-agent-interaction-card"
        data-status="processing"
        data-background-interaction-id={interaction.requestId}
        tabIndex={-1}
      >
        <div className="chat-agent-interaction-title">
          <LoaderCircle size={16} aria-hidden="true" />
          <strong>正在处理你的决定</strong>
        </div>
      </section>
    );
  }

  if (interaction.kind === "permission") {
    return (
      <section
        className="chat-agent-interaction-card"
        data-kind="permission"
        data-background-interaction-id={interaction.requestId}
        tabIndex={-1}
      >
        <div className="chat-agent-interaction-title">
          <ShieldAlert size={17} aria-hidden="true" />
          <strong>当前工作需要权限</strong>
        </div>
        <p>{interaction.actionSummary}</p>
        <PermissionDetails
          toolName={interaction.toolName}
          inputData={interaction.inputData}
        />
        <div className="chat-agent-interaction-actions">
          <button
            type="button"
            className="secondary"
            disabled={isSubmitting}
            onClick={() => void submit({
              requestId: interaction.requestId,
              kind: "permission",
              outcome: "deny"
            })}
          >
            <CircleX size={15} aria-hidden="true" />
            取消任务
          </button>
          <button
            type="button"
            disabled={isSubmitting}
            onClick={() => void submit({
              requestId: interaction.requestId,
              kind: "permission",
              outcome: "allow"
            })}
          >
            <Check size={15} aria-hidden="true" />
            授权本次任务
          </button>
        </div>
        {submitError ? <p className="chat-agent-interaction-error">{submitError}</p> : null}
      </section>
    );
  }

  const answers = buildQuestionAnswers(
    interaction.questions,
    selectedAnswers,
    customAnswers
  );
  const canSubmit = interaction.questions.length > 0
    && interaction.questions.every(question => Boolean(answers[question.question]));

  return (
    <section
      className="chat-agent-interaction-card"
      data-kind="question"
      data-background-interaction-id={interaction.requestId}
      tabIndex={-1}
    >
      <div className="chat-agent-interaction-title">
        <strong>当前工作需要补充信息</strong>
      </div>
      <div className="chat-agent-question-list">
        {interaction.questions.map(question => (
          <AgentQuestionField
            key={question.question}
            question={question}
            selected={selectedAnswers[question.question] ?? []}
            customAnswer={customAnswers[question.question] ?? ""}
            disabled={isSubmitting}
            onSelectedChange={selected => {
              setSelectedAnswers(current => ({
                ...current,
                [question.question]: selected
              }));
            }}
            onCustomAnswerChange={answer => {
              setCustomAnswers(current => ({
                ...current,
                [question.question]: answer
              }));
            }}
          />
        ))}
      </div>
      <div className="chat-agent-interaction-actions">
        <button
          type="button"
          className="secondary"
          disabled={isSubmitting}
          onClick={() => void submit({
            requestId: interaction.requestId,
            kind: "question",
            messageText: "取消当前这项工作。"
          })}
        >
          <CircleX size={15} aria-hidden="true" />
          取消任务
        </button>
        <button
          type="button"
          disabled={isSubmitting || !canSubmit}
          onClick={() => void submit({
            requestId: interaction.requestId,
            kind: "question",
            messageText: buildQuestionResponseText(interaction.questions, answers)
          })}
        >
          <Send size={15} aria-hidden="true" />
          提交回答
        </button>
      </div>
      {submitError ? <p className="chat-agent-interaction-error">{submitError}</p> : null}
    </section>
  );
}

/**
 * 把资料卡答案变成一条人能直接读懂的正常用户消息。
 *
 * @param questions 当前卡片按原顺序展示的问题。
 * @param answers 问题原文到用户答案的完整映射。
 * @returns 可直接交给普通 submitText 链路的补充回答。
 */
function buildQuestionResponseText(
  questions: ChatAgentQuestion[],
  answers: Record<string, string>
): string {
  const answerLines = questions.map(
    question => `- ${question.question}：${answers[question.question]}`
  );
  return ["我对当前工作的补充回答：", ...answerLines].join("\n");
}

type AgentQuestionFieldProps = {
  question: ChatAgentQuestion;
  selected: string[];
  customAnswer: string;
  disabled: boolean;
  onSelectedChange(selected: string[]): void;
  onCustomAnswerChange(answer: string): void;
};

/**
 * 渲染一个问题的单选、多选和自由输入控件。
 *
 * @param props 问题定义、当前草稿和变更回调。
 * @returns 不负责网络提交的受控问题字段。
 */
function AgentQuestionField({
  question,
  selected,
  customAnswer,
  disabled,
  onSelectedChange,
  onCustomAnswerChange
}: AgentQuestionFieldProps) {
  function toggleOption(label: string): void {
    if (question.multiSelect) {
      onSelectedChange(
        selected.includes(label)
          ? selected.filter(value => value !== label)
          : [...selected, label]
      );
      return;
    }
    onSelectedChange([label]);
  }

  return (
    <fieldset className="chat-agent-question-field">
      <legend>
        {question.header ? <span>{question.header}</span> : null}
        {question.question}
      </legend>
      {question.options.length > 0 ? (
        <div className="chat-agent-question-options">
          {question.options.map(option => (
            <label key={option.label}>
              <input
                type={question.multiSelect ? "checkbox" : "radio"}
                name={`question-${question.question}`}
                checked={selected.includes(option.label)}
                disabled={disabled}
                onChange={() => toggleOption(option.label)}
              />
              <span>
                <strong>{option.label}</strong>
                {option.description ? <small>{option.description}</small> : null}
              </span>
            </label>
          ))}
        </div>
      ) : null}
      <input
        className="chat-agent-question-custom-input"
        type="text"
        value={customAnswer}
        disabled={disabled}
        placeholder={question.options.length > 0 ? "补充说明（可选）" : "请输入回答"}
        aria-label={`${question.question}的回答`}
        onChange={event => onCustomAnswerChange(event.target.value)}
      />
    </fieldset>
  );
}

/**
 * 汇总选项和自由输入，生成 Claude SDK 需要的问题到答案映射。
 *
 * @param questions 当前卡片的问题定义。
 * @param selectedAnswers 每道题选中的标签。
 * @param customAnswers 每道题的自由输入。
 * @returns 去掉空值后的问题正文到答案映射。
 */
function buildQuestionAnswers(
  questions: ChatAgentQuestion[],
  selectedAnswers: Record<string, string[]>,
  customAnswers: Record<string, string>
): Record<string, string> {
  return Object.fromEntries(questions.flatMap(question => {
    const values = [
      ...(selectedAnswers[question.question] ?? []),
      customAnswers[question.question]?.trim() ?? ""
    ].filter(Boolean);
    return values.length > 0 ? [[question.question, values.join(", ")]] : [];
  }));
}

/**
 * 生成交互卡收口后的稳定短文案。
 *
 * @param interaction 已收到 resolved 事件的交互事实。
 * @returns 面向用户的只读状态说明。
 */
function resolvedInteractionLabel(interaction: ChatAgentInteraction): string {
  return {
    allowed: "本次任务已授权",
    denied: "已取消这项任务",
    answered: "已提交补充信息",
    cancelled: "已取消这项请求"
  }[interaction.outcome ?? "cancelled"];
}

/**
 * 折叠展示 Backend 已截取的工具名和输入，避免默认暴露冗长技术细节。
 *
 * @param props 权限请求携带的工具名和 JSON 友好输入。
 * @returns 默认关闭、可由用户主动展开的只读详情。
 */
function PermissionDetails({
  toolName,
  inputData
}: {
  toolName: string;
  inputData: Record<string, unknown>;
}) {
  const preview = JSON.stringify(inputData, null, 2);
  return (
    <details className="chat-agent-permission-details">
      <summary>查看操作详情</summary>
      <p>工具：{toolName || "受控工具"}</p>
      {preview && preview !== "{}"
        ? <pre className="chat-agent-permission-preview">{preview}</pre>
        : null}
    </details>
  );
}
