import { useCallback, useEffect, useRef, useState } from "react";

import type { SkillLibraryResponse } from "@/api/contracts/skills";
import type { ConsoleSettings } from "@/api/contracts/settings";
import { fetchSkillLibrary, updateSkillEnabled } from "@/api/services/skills";
import { formatSavedRefreshWarning } from "@/lib/mutationFeedback";

/**
 * 描述技能库列表请求的三个互斥页面状态。
 *
 * Values:
 *   loading: 首次读取或权威刷新正在进行。
 *   ready: 当前快照稳定，可以提交开关修改。
 *   failed: 首次读取失败，尚无可展示快照。
 */
type SkillLibraryRequestState = "loading" | "ready" | "failed";

/**
 * 描述技能库页面可以读取的状态和可以触发的动作。
 *
 * Args:
 *   snapshot: 最近一次成功读取的权威列表；首次成功前为空。
 *   requestState: 当前列表正在读取、可操作或首次读取失败。
 *   loadErrorMessage: 列表读取失败原因。
 *   updateErrorMessage: 最近一次开关提交失败原因。
 *   updateWarningMessage: 开关已保存但共享设置快照刷新失败时的提醒。
 *   updatingSkillId: 当前唯一正在提交的 Skill ID。
 *   retry: 清理现有错误并重新读取完整列表。
 *   setSkillEnabled: 基于当前 revision 串行提交一个开关修改。
 */
export type SkillLibraryController = {
  snapshot: SkillLibraryResponse | null;
  requestState: SkillLibraryRequestState;
  loadErrorMessage: string | null;
  updateErrorMessage: string | null;
  updateWarningMessage: string | null;
  updatingSkillId: string | null;
  retry(): void;
  setSkillEnabled(skillId: string, enabled: boolean): Promise<void>;
};

/**
 * 管理技能库列表读取、并发版本和开关提交。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *
 * Returns:
 *   页面渲染需要的技能库快照、请求状态和修改动作。
 */
export function useSkillLibrary(
  baseUrl: string,
  onSettingsSnapshotRefresh?: (candidateSettings?: ConsoleSettings) => Promise<unknown>
): SkillLibraryController {
  const [snapshot, setSnapshot] = useState<SkillLibraryResponse | null>(null);
  const [requestState, setRequestState] = useState<SkillLibraryRequestState>("loading");
  const [loadErrorMessage, setLoadErrorMessage] = useState<string | null>(null);
  const [updateErrorMessage, setUpdateErrorMessage] = useState<string | null>(null);
  const [updateWarningMessage, setUpdateWarningMessage] = useState<string | null>(null);
  const [updatingSkillId, setUpdatingSkillId] = useState<string | null>(null);
  const [retrySequence, setRetrySequence] = useState(0);
  const activeRequestSequence = useRef(0);
  const activeUpdateSequence = useRef(0);
  const activeUpdateSkillId = useRef<string | null>(null);

  useEffect(() => {
    const abortController = new AbortController();
    const requestSequence = activeRequestSequence.current + 1;
    activeRequestSequence.current = requestSequence;
    setRequestState("loading");
    setLoadErrorMessage(null);

    void fetchSkillLibrary(baseUrl, abortController.signal)
      .then(nextSnapshot => {
        if (abortController.signal.aborted || activeRequestSequence.current !== requestSequence) {
          return;
        }
        setSnapshot(nextSnapshot);
        setRequestState("ready");
      })
      .catch((error: unknown) => {
        if (abortController.signal.aborted || activeRequestSequence.current !== requestSequence) {
          return;
        }
        setRequestState("failed");
        setLoadErrorMessage(toErrorMessage(error, "读取技能库失败"));
      });

    return () => {
      abortController.abort();
      activeRequestSequence.current += 1;
    };
  }, [baseUrl, retrySequence]);

  useEffect(() => {
    setUpdatingSkillId(null);
    setUpdateErrorMessage(null);
    setUpdateWarningMessage(null);
    return () => {
      // 切换 Backend 或卸载页面后，旧 PATCH 即使迟到也不能再改写当前页面。
      activeUpdateSequence.current += 1;
      activeUpdateSkillId.current = null;
    };
  }, [baseUrl]);

  /**
   * 清理错误并重新读取完整技能库。
   *
   * Returns:
   *   无返回值；请求结果通过 controller 状态发布。
   */
  const retry = useCallback((): void => {
    setUpdateErrorMessage(null);
    setUpdateWarningMessage(null);
    setRequestState("loading");
    setRetrySequence(sequence => sequence + 1);
  }, []);

  /**
   * 串行修改一个 Skill 的全局启用偏好。
   *
   * Args:
   *   skillId: 要修改的稳定 Skill ID。
   *   enabled: 用户希望保存的新开关值。
   *
   * Returns:
   *   请求和必要的冲突刷新结束后完成；业务失败由页面错误状态展示。
   */
  const setSkillEnabled = useCallback(async (skillId: string, enabled: boolean): Promise<void> => {
    if (
      snapshot === null ||
      requestState !== "ready" ||
      activeUpdateSkillId.current !== null
    ) {
      return;
    }

    const expectedRevision = snapshot.revision;
    const updateSequence = activeUpdateSequence.current + 1;
    activeUpdateSequence.current = updateSequence;
    activeUpdateSkillId.current = skillId;
    setUpdatingSkillId(skillId);
    setUpdateErrorMessage(null);
    setUpdateWarningMessage(null);
    try {
      const response = await updateSkillEnabled(
        baseUrl,
        skillId,
        enabled,
        expectedRevision
      );
      if (activeUpdateSequence.current !== updateSequence) {
        return;
      }
      setSnapshot(currentSnapshot => {
        // 用户可能在 PATCH 期间重试了列表。此时旧响应不能覆盖更新后的权威快照。
        if (currentSnapshot === null || currentSnapshot.revision !== expectedRevision) {
          return currentSnapshot;
        }
        const skills = currentSnapshot.skills.map(skill =>
          skill.id === response.skill.id ? response.skill : skill
        );
        return {
          ...currentSnapshot,
          skills,
          revision: response.revision,
          summary: summarizeSkills(skills)
        };
      });
      // Skill 偏好保存也会推进 Backend 的全局 settings_revision；同步普通设置
      // 快照，避免用户随后切回常用页保存时携带旧版本。
      try {
        const refreshedSettings = await onSettingsSnapshotRefresh?.(response.settings);
        if (activeUpdateSequence.current !== updateSequence) {
          return;
        }
        if (refreshedSettings === null) {
          setUpdateWarningMessage(formatSavedRefreshWarning(null, "设置快照暂时不可用"));
        }
      } catch (refreshError) {
        // PATCH 已经成功；设置快照读取失败不能进入下面的“保存失败并重取列表”
        // 分支，否则用户会看到误导性错误并可能重复提交同一开关。
        if (activeUpdateSequence.current !== updateSequence) {
          return;
        }
        setUpdateWarningMessage(formatSavedRefreshWarning(refreshError, "设置快照暂时不可用"));
      }
    } catch (error) {
      if (activeUpdateSequence.current !== updateSequence) {
        return;
      }
      setUpdateErrorMessage(toErrorMessage(error, "修改 Skill 开关失败"));
      // PATCH 失败时响应可能在后端提交点之后才丢失；即使最终是 409，
      // 其它配置操作也可能已经推进了共享 settings_revision。先尽力读取
      // 普通设置快照，避免用户切回设置页后继续携带旧版本。
      try {
        await onSettingsSnapshotRefresh?.();
      } catch {
        // 原始 Skill 错误更能指导用户下一步；设置快照刷新失败不覆盖它。
      }
      if (activeUpdateSequence.current !== updateSequence) {
        return;
      }
      // 冲突或其它保存失败都可能意味着列表已经过期；保留错误提示，同时
      // 立即重取权威状态，避免旧 revision 继续参与后续提交。
      setRequestState("loading");
      setRetrySequence(sequence => sequence + 1);
    } finally {
      if (activeUpdateSequence.current !== updateSequence) {
        return;
      }
      activeUpdateSkillId.current = null;
      setUpdatingSkillId(null);
    }
  }, [baseUrl, onSettingsSnapshotRefresh, requestState, snapshot]);

  return {
    snapshot,
    requestState,
    loadErrorMessage,
    updateErrorMessage,
    updateWarningMessage,
    updatingSkillId,
    retry,
    setSkillEnabled
  };
}

/**
 * 根据刚收到的完整 Skill 状态重新计算四项汇总。
 *
 * Args:
 *   skills: 已通过契约校验的完整 Skill 列表。
 *
 * Returns:
 *   与当前列表一致的总数、开启数、就绪数和实际可用数。
 */
function summarizeSkills(skills: SkillLibraryResponse["skills"]): SkillLibraryResponse["summary"] {
  return {
    total: skills.length,
    enabled: skills.filter(skill => skill.enabled).length,
    ready: skills.filter(skill => skill.ready).length,
    usable: skills.filter(skill => skill.usable).length
  };
}

/**
 * 把未知异常收窄成可以直接展示的本地错误文字。
 *
 * Args:
 *   error: 请求链路抛出的未知异常。
 *   fallback: 非 Error 值使用的稳定中文说明。
 *
 * Returns:
 *   可以直接交给错误条展示的文字。
 */
function toErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
