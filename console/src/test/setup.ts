/**
 * 为 jsdom 补齐 Radix Switch 测量隐藏输入时依赖的浏览器接口。
 */
class TestResizeObserver implements ResizeObserver {
  /**
   * 接收标准回调以匹配浏览器构造签名。
   *
   * Args:
   *   callback: 元素尺寸变化时的浏览器回调；当前测试不模拟尺寸变化。
   */
  constructor(callback: ResizeObserverCallback) {
    void callback;
  }

  /**
   * 登记一个观察目标。
   *
   * Args:
   *   target: 需要观察的 DOM 元素。
   *   options: 可选观察盒模型配置。
   */
  observe(target: Element, options?: ResizeObserverOptions): void {
    void target;
    void options;
  }

  /**
   * 取消一个观察目标。
   *
   * Args:
   *   target: 不再需要观察的 DOM 元素。
   */
  unobserve(target: Element): void {
    void target;
  }

  /**
   * 取消当前实例的全部观察目标。
   */
  disconnect(): void {}
}

globalThis.ResizeObserver = TestResizeObserver;
