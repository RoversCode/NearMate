from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Any

from companion.background_agent.contracts import AgentMaterial
from companion.realtime.assistant_events import (
    AssistantControlCommand,
    AssistantRequest,
    RuntimeEvent,
)
from companion.realtime.assistant_process_client import AssistantProcessClient


@dataclass(frozen=True, slots=True)
class AssistantProcessReplacement:
    """描述一次助手运行时原子发布结果。

    Args:
        old_runtime: 被替换的真实运行时；首次发布时为空。
        old_epoch: 旧运行时世代；首次发布时为空。
        current_epoch: 新运行时已经生效的世代。
    """

    old_runtime: AssistantProcessClient | None
    old_epoch: int | None
    current_epoch: int


class AssistantProcessEpochError(RuntimeError):
    """表示某一明确运行时世代上的 bridge 操作失败。"""

    def __init__(self, runtime_epoch: int, cause: BaseException) -> None:
        """保存失败世代并保留原始异常链。

        Args:
            runtime_epoch: 发生失败的助手运行时世代。
            cause: 底层运行时抛出的原始异常。
        """

        super().__init__(str(cause))
        self.runtime_epoch = runtime_epoch


class SwappableAssistantProcessClient:
    """稳定的助手运行时代理。

    Args:
        initial_runtime: 初始真实助手运行时；未初始化时可为空。
    """

    def __init__(self, initial_runtime: AssistantProcessClient | None = None) -> None:
        """创建可热替换的助手运行时代理。

        Args:
            initial_runtime: 当前生效的真实助手运行时。

        Returns:
            无。
        """

        self._runtime = initial_runtime
        self._runtime_epoch = 1 if initial_runtime is not None else 0
        self._lock = RLock()

    @property
    def current_epoch(self) -> int:
        """返回当前已发布助手运行时世代。"""

        with self._lock:
            return self._runtime_epoch

    def replace(self, runtime: AssistantProcessClient | None) -> AssistantProcessReplacement:
        """替换当前真实助手运行时，并返回原子发布结果。

        Args:
            runtime: 新的真实助手运行时；None 表示清空当前目标。

        Returns:
            包含旧实例、旧世代和新世代的发布结果。
        """

        with self._lock:
            old_runtime = self._runtime
            old_epoch = self._runtime_epoch if old_runtime is not None else None
            self._runtime_epoch += 1
            self._runtime = runtime
            return AssistantProcessReplacement(
                old_runtime=old_runtime,
                old_epoch=old_epoch,
                current_epoch=self._runtime_epoch,
            )

    def _require_runtime(self) -> AssistantProcessClient:
        """读取当前真实助手运行时。

        Returns:
            当前已发布的真实助手运行时。

        Raises:
            RuntimeError: 运行时尚未初始化。
        """

        with self._lock:
            runtime = self._runtime
        if runtime is None:
            raise RuntimeError("Assistant runtime 尚未初始化，必须先完成 bootstrap")
        return runtime

    def cancel_current_generation(
        self, *, reason: str, generation_id: str | None = None
    ) -> AssistantControlCommand:
        """取消当前助手 generation。

        Args:
            reason: 取消原因或触发来源。
            generation_id: 可选的目标 generation 标识。

        Returns:
            已提交给当前运行时的控制命令。
        """

        return self._require_runtime().cancel_current_generation(reason=reason, generation_id=generation_id)

    def submit_control_command(self, command: AssistantControlCommand) -> AssistantControlCommand:
        """提交既有助手控制命令。

        Args:
            command: 上游已经创建好的助手控制命令。

        Returns:
            已提交给当前运行时的同一条控制命令。
        """

        with self._lock:
            runtime = self._runtime
            runtime_epoch = self._runtime_epoch
            if runtime is None:
                raise RuntimeError("Assistant runtime 尚未初始化，必须先完成 bootstrap")
            try:
                return runtime.submit_control_command(command)
            except Exception as exception:
                raise AssistantProcessEpochError(runtime_epoch, exception) from exception

    def submit_control_command_for_epoch(
        self,
        command: AssistantControlCommand,
        *,
        runtime_epoch: int,
    ) -> AssistantControlCommand:
        """只把控制命令投递给仍然匹配的当前助手运行时世代。

        Args:
            command: 上游已经创建好的助手控制命令。
            runtime_epoch: 主进程准入时冻结的目标世代。

        Returns:
            已提交给目标运行时的同一条命令。

        Raises:
            AssistantProcessEpochError: 目标世代已被热替换或底层投递失败。
        """

        with self._lock:
            runtime = self._runtime
            current_epoch = self._runtime_epoch
            if runtime is None or runtime_epoch != current_epoch:
                raise AssistantProcessEpochError(
                    runtime_epoch,
                    RuntimeError("Assistant runtime epoch 已失效"),
                )
            try:
                return runtime.submit_control_command(command)
            except Exception as exception:
                raise AssistantProcessEpochError(runtime_epoch, exception) from exception

    def warmup_foreground(self, *, reason: str) -> AssistantControlCommand:
        """提交前台模型 warmup 控制命令。

        Args:
            reason: 触发预热的业务原因。

        Returns:
            已提交给当前运行时的控制命令。
        """

        return self._require_runtime().warmup_foreground(reason=reason)

    def answer_background_agent_interaction(
        self,
        *,
        request_id: str,
        outcome: str,
        answers: dict[str, str] | None = None,
        reason: str = "",
        materials: tuple[AgentMaterial, ...] = (),
        user_text: str = "",
        submission_id: str | None = None,
        runtime_epoch: int | None = None,
    ) -> AssistantControlCommand:
        """把后台智能体交互回答投递给指定的当前运行时世代。

        Args:
            request_id: 后台交互请求编号。
            outcome: 允许、拒绝、回答或取消。
            answers: AskUserQuestion 的答案映射。
            reason: 用户填写的拒绝或取消理由。
            materials: 本轮新增、需要扩展到当前工作的正式材料。
            user_text: Foreground 收到的本轮原始用户话语。
            submission_id: 自然语言回答等待权威回执时使用的提交编号。
            runtime_epoch: Registry claim 时冻结的目标世代。

        Returns:
            已进入目标运行时控制队列的命令。

        Raises:
            RuntimeError: 运行时缺失或目标世代已经被替换。
        """

        with self._lock:
            runtime = self._runtime
            current_epoch = self._runtime_epoch
            if runtime is None:
                raise RuntimeError("Assistant runtime 尚未初始化，必须先完成 bootstrap")
            if runtime_epoch is not None and runtime_epoch != current_epoch:
                raise RuntimeError("后台交互请求所属助手运行时已经失效")
            return runtime.answer_background_agent_interaction(
                request_id=request_id,
                outcome=outcome,
                answers=answers,
                reason=reason,
                materials=materials,
                user_text=user_text,
                submission_id=submission_id,
                runtime_epoch=current_epoch,
            )

    def reject_background_agent_interaction_submission(
        self,
        *,
        submission_id: str,
        request_id: str,
        reason: str,
        runtime_epoch: int | None = None,
    ) -> AssistantControlCommand:
        """把 Registry 拒绝事实投递给指定的当前运行时世代。

        Args:
            submission_id: 自然语言回答的提交编号。
            request_id: 对应的工作交互请求编号。
            reason: 稳定拒绝原因。
            runtime_epoch: 产生 submission 请求的运行时世代。

        Returns:
            已进入目标运行时控制队列的命令。

        Raises:
            RuntimeError: 运行时缺失或目标世代已经被替换。
        """

        with self._lock:
            runtime = self._runtime
            current_epoch = self._runtime_epoch
            if runtime is None:
                raise RuntimeError("Assistant runtime 尚未初始化，必须先完成 bootstrap")
            if runtime_epoch is not None and runtime_epoch != current_epoch:
                raise RuntimeError("后台交互提交所属助手运行时已经失效")
            return runtime.reject_background_agent_interaction_submission(
                submission_id=submission_id,
                request_id=request_id,
                reason=reason,
                runtime_epoch=current_epoch,
            )

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """提交既有助手生成请求。

        Args:
            request: 上游已经创建好的助手请求。

        Returns:
            已提交给当前运行时的同一条请求。
        """

        with self._lock:
            runtime = self._runtime
            runtime_epoch = self._runtime_epoch
            if runtime is None:
                raise RuntimeError("Assistant runtime 尚未初始化，必须先完成 bootstrap")
            try:
                return runtime.submit_existing_request(request)
            except Exception as exception:
                raise AssistantProcessEpochError(runtime_epoch, exception) from exception

    def submit_bridge_request(self, request: AssistantRequest) -> int:
        """提交 Bridge 请求并原子返回实际接收它的运行时世代。

        Args:
            request: Bridge 已从输入队列接管的助手请求。

        Returns:
            实际接收该请求的助手运行时世代。

        Raises:
            RuntimeError: 运行时尚未初始化。
            AssistantProcessEpochError: 目标运行时拒绝或无法接收请求。
        """

        # 请求提交和世代读取共用同一临界区，避免热替换把请求身份误记到新世代。
        with self._lock:
            runtime = self._runtime
            runtime_epoch = self._runtime_epoch
            if runtime is None:
                raise RuntimeError("Assistant runtime 尚未初始化，必须先完成 bootstrap")
            try:
                runtime.submit_existing_request(request)
            except Exception as exception:
                raise AssistantProcessEpochError(runtime_epoch, exception) from exception
            return runtime_epoch

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """读取助手运行时已完成事件。

        Args:
            limit: 本次最多返回的事件数量。

        Returns:
            当前运行时回流的事件列表。
        """

        # 读取目标、排水和标记世代必须处于同一临界区，避免热替换把旧事件误标成新世代。
        with self._lock:
            runtime = self._runtime
            runtime_epoch = self._runtime_epoch
            if runtime is None:
                raise RuntimeError("Assistant runtime 尚未初始化，必须先完成 bootstrap")
            try:
                events = runtime.drain_events(limit=limit)
            except Exception as exception:
                raise AssistantProcessEpochError(runtime_epoch, exception) from exception
        return [replace(event, runtime_epoch=runtime_epoch) for event in events]

    def notify_bridge_work_available(self) -> None:
        """通知 bridge 已有可处理工作。

        Returns:
            无。
        """

        runtime, runtime_epoch = self._runtime_snapshot()
        try:
            runtime.notify_bridge_work_available()
        except Exception as exception:
            raise AssistantProcessEpochError(runtime_epoch, exception) from exception

    def clear_bridge_work_available(self) -> None:
        """清除 bridge 工作可用信号。

        Returns:
            无。
        """

        runtime, runtime_epoch = self._runtime_snapshot()
        try:
            runtime.clear_bridge_work_available()
        except Exception as exception:
            raise AssistantProcessEpochError(runtime_epoch, exception) from exception

    def wait_for_bridge_work(self, timeout: float | None = None) -> bool:
        """等待 bridge 工作可用信号。

        Args:
            timeout: 最长等待秒数，None 表示一直等待。

        Returns:
            等待期间观察到工作信号时返回 True，超时返回 False。
        """

        runtime, runtime_epoch = self._runtime_snapshot()
        try:
            return runtime.wait_for_bridge_work(timeout=timeout)
        except Exception as exception:
            raise AssistantProcessEpochError(runtime_epoch, exception) from exception

    def get_streamvox_runtime_state(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """读取助手子进程内 StreamVox 当前运行状态。

        Args:
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            当前 StreamVox 状态字典。
        """

        return self._require_runtime().get_streamvox_runtime_state(timeout=timeout)

    def switch_streamvox_config(
        self,
        *,
        model: str,
        device: str,
        llama_backend: str | None,
        role_name: str,
        streamvox_seed: int | None,
        inference_options: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """切换助手子进程内 StreamVox 模型、设备和当前角色。

        Args:
            model: 目标模型名称。
            device: 目标推理设备。
            llama_backend: 目标 llama.cpp 后端策略；None 表示不显式指定。
            role_name: 目标角色名称。
            streamvox_seed: 目标推理随机种子。
            inference_options: 目标模型族推理参数。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            更新后的 StreamVox 状态字典。
        """

        return self._require_runtime().switch_streamvox_config(
            model=model,
            device=device,
            llama_backend=llama_backend,
            role_name=role_name,
            streamvox_seed=streamvox_seed,
            inference_options=inference_options,
            timeout=timeout,
        )

    def delete_streamvox_role(self, *, role_name: str, timeout: float = 5.0) -> dict[str, Any]:
        """删除当前模型下的指定 StreamVox 角色。

        Args:
            role_name: 待删除的角色名称。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            更新后的 StreamVox 状态字典。
        """

        return self._require_runtime().delete_streamvox_role(role_name=role_name, timeout=timeout)

    def create_streamvox_role(
        self,
        *,
        role_name: str,
        audio_path: str,
        prompt_text: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """创建当前模型下的 StreamVox 自定义角色。

        Args:
            role_name: 新角色名称。
            audio_path: 用于克隆音色的音频文件路径。
            prompt_text: 音频对应的提示文本。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            更新后的 StreamVox 状态字典。
        """

        return self._require_runtime().create_streamvox_role(
            role_name=role_name,
            audio_path=audio_path,
            prompt_text=prompt_text,
            timeout=timeout,
        )

    def preview_streamvox_role(self, *, role_name: str, text: str, timeout: float = 30.0) -> dict[str, Any]:
        """使用当前模型下的已创建角色生成试听音频。

        Args:
            role_name: 试听使用的角色名称。
            text: 试听文本。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            包含试听音频和采样率的载荷。
        """

        return self._require_runtime().preview_streamvox_role(role_name=role_name, text=text, timeout=timeout)

    def get_streamvox_prompt_data(self, *, role_name: str, timeout: float = 5.0) -> Any | None:
        """读取当前模型下指定角色的 StreamVox prompt 数据。

        Args:
            role_name: 待读取的角色名。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            StreamVox prompt 数据；不存在时返回 None。
        """

        return self._require_runtime().get_streamvox_prompt_data(role_name=role_name, timeout=timeout)

    def start(self) -> None:
        """启动当前真实助手运行时。

        Returns:
            无。
        """

        start_runtime = getattr(self._require_runtime(), "start", None)
        if callable(start_runtime):
            start_runtime()

    def shutdown(self) -> tuple[RuntimeEvent, ...]:
        """关闭当前真实助手运行时并清空代理目标。

        Returns:
            已附加原运行时世代、尚未被 Bridge 取走的退出阶段事件。
        """

        replacement = self.replace(None)
        runtime = replacement.old_runtime
        shutdown_runtime = getattr(runtime, "shutdown", None)
        if not callable(shutdown_runtime):
            return ()
        shutdown_events = shutdown_runtime()
        if not isinstance(shutdown_events, (list, tuple)):
            return ()
        return tuple(
            replace(event, runtime_epoch=replacement.old_epoch)
            for event in shutdown_events
            if isinstance(event, RuntimeEvent)
        )

    def _runtime_snapshot(self) -> tuple[AssistantProcessClient, int]:
        """原子读取当前真实运行时及其世代。

        Returns:
            当前真实运行时和对应世代。

        Raises:
            RuntimeError: 运行时尚未初始化。
        """

        with self._lock:
            runtime = self._runtime
            runtime_epoch = self._runtime_epoch
        if runtime is None:
            raise RuntimeError("Assistant runtime 尚未初始化，必须先完成 bootstrap")
        return runtime, runtime_epoch
