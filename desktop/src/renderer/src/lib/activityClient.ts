/** 向 Backend 提交一条 Activity 的真实取消请求。 */
export async function cancelActivity(
  httpBaseUrl: string,
  activityId: string
): Promise<void> {
  const response = await fetch(
    `${httpBaseUrl.replace(/\/$/, "")}/api/activities/${encodeURIComponent(activityId)}/cancel`,
    { method: "POST" }
  );
  if (response.ok) {
    return;
  }
  if (response.status === 404) {
    throw new Error("任务已不在本次运行中。");
  }
  if (response.status === 409) {
    throw new Error("任务状态已经变化，当前不能取消。");
  }
  if (response.status === 503) {
    throw new Error("后台运行时暂不可用，请稍后重试。");
  }
  throw new Error(`取消任务失败（HTTP ${response.status}）`);
}
