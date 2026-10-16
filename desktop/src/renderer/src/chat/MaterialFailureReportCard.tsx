import { useEffect, useState } from "react";
import type { MaterialFailureReport } from "../../../shared/materialFeedback";

/**
 * 展示单项直达或多项可展开的材料失败详情。
 *
 * @param props.report 当前运行期需要用户知晓的失败报告。
 * @returns 不展示路径和技术原因码的处理结果卡片。
 */
export function MaterialFailureReportCard({ report }: { report: MaterialFailureReport }) {
  const [expanded, setExpanded] = useState(report.items.length === 1);

  useEffect(() => {
    setExpanded(report.items.length === 1);
  }, [report]);

  const title = report.phase === "preflight"
    ? `${report.items.length} 项材料检查失败，已移除`
    : `${report.items.length} 项材料未添加`;
  const showItems = report.items.length === 1 || expanded;

  return (
    <section className="material-failure-report" role="status" aria-label="材料处理结果">
      <div className="material-failure-report-header">
        <strong>{title}</strong>
        {report.items.length > 1 ? (
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded(current => !current)}
          >
            {expanded ? "收起" : "查看原因"}
          </button>
        ) : null}
      </div>
      {showItems ? (
        <ul className="material-failure-report-list">
          {report.items.map((item, index) => (
            <li key={`${item.materialId ?? item.displayName}:${index}`}>
              <span title={item.displayName}>{item.displayName}</span>
              <p>{item.message}</p>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
