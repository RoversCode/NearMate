import type { Dispatch, SetStateAction } from "react";

import type { BootstrapStatus } from "../api/contracts/bootstrap";
import type { ConsoleSettings } from "../api/contracts/settings";
import type { ConsoleData } from "./consoleTypes";

export type ConsoleActionContext = {
  baseUrl: string;
  consoleData: ConsoleData;
  setConsoleData: Dispatch<SetStateAction<ConsoleData>>;
  setBootstrapStatus: Dispatch<SetStateAction<BootstrapStatus | null>>;
  loadConsoleData: (requestBaseUrl: string, shouldApplyResult?: () => boolean) => Promise<void>;
  /**
   * 读取最新普通设置快照，并按 settings_revision 单调写回 Console。
   * 配置领域各自保存成功后必须调用它，避免下一次普通设置提交拿旧版本。
   */
  refreshSettingsSnapshot: (candidateSettings?: ConsoleSettings) => Promise<ConsoleSettings | null>;
};
