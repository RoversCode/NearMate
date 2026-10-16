import { describe, expect, it, vi } from "vitest";
import type { TurnSubmissionsSnapshot } from "../../shared/turnSubmissions";
import { createTurnSubmissionService } from "./turnSubmissionService";
import type { TurnSubmissionsClient } from "./turnSubmissionsClient";

const SUBMISSION_ID = "6d77fa4e-177c-49b6-a4e3-42df9b46211d";
const EMPTY_SNAPSHOT: TurnSubmissionsSnapshot = { revision: 1, items: [] };

function createClient(): TurnSubmissionsClient {
  return {
    reserve: vi.fn(async () => EMPTY_SNAPSHOT),
    commitContext: vi.fn(async () => EMPTY_SNAPSHOT),
    edit: vi.fn(async () => EMPTY_SNAPSHOT),
    delete: vi.fn(async () => EMPTY_SNAPSHOT),
    resume: vi.fn(async () => EMPTY_SNAPSHOT),
    retry: vi.fn(async () => EMPTY_SNAPSHOT),
    reportPreparationFailed: vi.fn(async () => EMPTY_SNAPSHOT)
  };
}

describe("turnSubmissionService", () => {
  it("reservation 成功后立即返回，环境准备在后台继续", async () => {
    const environment = createDeferred<string | null>();
    const client = createClient();
    const service = createTurnSubmissionService({
      environmentSnapshot: { uploadTextSnapshot: vi.fn(() => environment.promise) },
      turnSubmissionsClient: client
    });

    await expect(service.submitTurn({
      submissionId: SUBMISSION_ID,
      messageText: "  整理材料  "
    })).resolves.toEqual(EMPTY_SNAPSHOT);

    expect(client.reserve).toHaveBeenCalledWith({
      submissionId: SUBMISSION_ID,
      messageText: "整理材料"
    });
    expect(client.commitContext).not.toHaveBeenCalled();

    environment.resolve("environment-1");
    await vi.waitFor(() => expect(client.commitContext).toHaveBeenCalledWith({
      submissionId: SUBMISSION_ID,
      environmentSnapshotId: "environment-1"
    }));
  });

  it("没有可用截图时提交 null 环境，不把正常降级误报成失败", async () => {
    const client = createClient();
    const service = createTurnSubmissionService({
      environmentSnapshot: { uploadTextSnapshot: vi.fn(async () => null) },
      turnSubmissionsClient: client
    });

    await service.submitTurn({ submissionId: SUBMISSION_ID, messageText: "继续" });

    await vi.waitFor(() => expect(client.commitContext).toHaveBeenCalledWith({
      submissionId: SUBMISSION_ID,
      environmentSnapshotId: null
    }));
    expect(client.reportPreparationFailed).not.toHaveBeenCalled();
  });

  it("环境上下文提交异常时把 Backend 状态改成可重试失败", async () => {
    const client = createClient();
    vi.mocked(client.commitContext).mockRejectedValue(new Error("network"));
    const service = createTurnSubmissionService({
      environmentSnapshot: { uploadTextSnapshot: vi.fn(async () => "environment-1") },
      turnSubmissionsClient: client,
      logger: { warn: vi.fn() }
    });

    await service.submitTurn({ submissionId: SUBMISSION_ID, messageText: "继续" });

    await vi.waitFor(() => expect(client.reportPreparationFailed).toHaveBeenCalledWith(SUBMISSION_ID));
  });

  it("重试只在 Backend 接受后重新启动环境准备", async () => {
    const client = createClient();
    const uploadTextSnapshot = vi.fn(async () => "environment-2");
    const service = createTurnSubmissionService({
      environmentSnapshot: { uploadTextSnapshot },
      turnSubmissionsClient: client
    });

    await service.retryTurn(SUBMISSION_ID);

    expect(client.retry).toHaveBeenCalledWith(SUBMISSION_ID);
    await vi.waitFor(() => expect(uploadTextSnapshot).toHaveBeenCalledOnce());
  });

  it("失败状态刚公开就重试时，会在旧 preparation 收尾后启动新一轮准备", async () => {
    const client = createClient();
    const uploadTextSnapshot = vi.fn()
      .mockRejectedValueOnce(new Error("capture failed"))
      .mockResolvedValueOnce("environment-2");
    let service: ReturnType<typeof createTurnSubmissionService>;
    vi.mocked(client.reportPreparationFailed).mockImplementation(async () => {
      await service.retryTurn(SUBMISSION_ID);
      return EMPTY_SNAPSHOT;
    });
    service = createTurnSubmissionService({
      environmentSnapshot: { uploadTextSnapshot },
      turnSubmissionsClient: client,
      logger: { warn: vi.fn() }
    });

    await service.submitTurn({ submissionId: SUBMISSION_ID, messageText: "继续" });

    await vi.waitFor(() => expect(client.reportPreparationFailed).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(uploadTextSnapshot).toHaveBeenCalledTimes(2));
    expect(client.retry).toHaveBeenCalledWith(SUBMISSION_ID);
    expect(client.commitContext).toHaveBeenCalledWith({
      submissionId: SUBMISSION_ID,
      environmentSnapshotId: "environment-2"
    });
  });

  it("空正文或编号不会创建 reservation", async () => {
    const client = createClient();
    const uploadTextSnapshot = vi.fn();
    const service = createTurnSubmissionService({
      environmentSnapshot: { uploadTextSnapshot },
      turnSubmissionsClient: client
    });

    await expect(service.submitTurn({ submissionId: SUBMISSION_ID, messageText: "  " }))
      .rejects.toThrow("requires submission id and message text");

    expect(client.reserve).not.toHaveBeenCalled();
    expect(uploadTextSnapshot).not.toHaveBeenCalled();
  });
});

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(nextResolve => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}
