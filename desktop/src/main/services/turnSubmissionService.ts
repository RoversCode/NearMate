import type { TurnSubmissionsSnapshot } from "../../shared/turnSubmissions";
import type { EnvironmentSnapshotService } from "./environmentSnapshotService";
import type { TurnSubmissionsClient } from "./turnSubmissionsClient";

type TurnSubmissionServiceOptions = {
  environmentSnapshot: Pick<EnvironmentSnapshotService, "uploadTextSnapshot">;
  turnSubmissionsClient: TurnSubmissionsClient;
  logger?: Pick<Console, "warn">;
};

type ContextPreparationPolicy = "ignore_if_running" | "run_after_current";

export type TurnSubmissionService = {
  submitTurn(input: { submissionId: string; messageText: string }): Promise<TurnSubmissionsSnapshot>;
  editTurn(input: { submissionId: string; messageText: string }): Promise<TurnSubmissionsSnapshot>;
  deleteTurn(submissionId: string): Promise<TurnSubmissionsSnapshot>;
  resumeTurn(submissionId: string): Promise<TurnSubmissionsSnapshot>;
  retryTurn(submissionId: string): Promise<TurnSubmissionsSnapshot>;
};

/**
 * 创建文本 reservation 与现场环境提交编排器。
 *
 * Args:
 *   options: 环境快照端口、Turn HTTP 客户端和诊断日志。
 *
 * Returns:
 *   reservation 成功即返回、环境准备在后台继续的文本提交服务。
 */
export function createTurnSubmissionService(
  options: TurnSubmissionServiceOptions
): TurnSubmissionService {
  const logger = options.logger ?? console;
  const preparationTasks = new Map<string, Promise<void>>();

  /**
   * reservation 已成功后，在后台完成现场环境绑定。
   *
   * Args:
   *   submissionId: 需要准备现场环境的文本提交编号。
   *   policy: 普通幂等提交忽略重复任务；用户重试则排在当前任务收尾之后。
   *
   * Returns:
   *   无；准备任务由服务内部保活并自行上报失败。
   */
  function scheduleContextPreparation(
    submissionId: string,
    policy: ContextPreparationPolicy = "ignore_if_running"
  ): void {
    const currentTask = preparationTasks.get(submissionId);
    if (currentTask && policy === "ignore_if_running") {
      return;
    }

    // 失败快照公开时旧任务还在 finally 之前；明确重试必须接在它后面，不能被当成重复调用。
    const preparation = currentTask
      ? currentTask.then(() => prepareContext(submissionId))
      : prepareContext(submissionId);
    const task = preparation.finally(() => {
      if (preparationTasks.get(submissionId) === task) {
        preparationTasks.delete(submissionId);
      }
    });
    preparationTasks.set(submissionId, task);
  }

  /**
   * 捕获并提交一条 reservation 的现场环境。
   *
   * Args:
   *   submissionId: 需要补齐现场环境的文本提交编号。
   *
   * Returns:
   *   环境提交或失败上报完成后结束的 Promise。
   */
  async function prepareContext(submissionId: string): Promise<void> {
    try {
      const environmentSnapshotId = await options.environmentSnapshot.uploadTextSnapshot();
      await options.turnSubmissionsClient.commitContext({
        submissionId,
        environmentSnapshotId
      });
    } catch (error) {
      logger.warn("[companion-main] turn context preparation failed", {
        submissionId,
        error
      });
      try {
        await options.turnSubmissionsClient.reportPreparationFailed(submissionId);
      } catch (reportError) {
        logger.warn("[companion-main] turn preparation failure report failed", {
          submissionId,
          error: reportError
        });
      }
    }
  }

  return {
    async submitTurn(input) {
      const submissionId = input.submissionId.trim();
      const messageText = input.messageText.trim();
      if (!submissionId || !messageText) {
        throw new Error("turn reservation requires submission id and message text");
      }
      const snapshot = await options.turnSubmissionsClient.reserve({
        submissionId,
        messageText
      });
      scheduleContextPreparation(submissionId);
      return snapshot;
    },
    editTurn(input) {
      return options.turnSubmissionsClient.edit({
        submissionId: input.submissionId,
        messageText: input.messageText.trim()
      });
    },
    deleteTurn(submissionId) {
      return options.turnSubmissionsClient.delete(submissionId);
    },
    resumeTurn(submissionId) {
      return options.turnSubmissionsClient.resume(submissionId);
    },
    async retryTurn(submissionId) {
      const snapshot = await options.turnSubmissionsClient.retry(submissionId);
      scheduleContextPreparation(submissionId, "run_after_current");
      return snapshot;
    }
  };
}
