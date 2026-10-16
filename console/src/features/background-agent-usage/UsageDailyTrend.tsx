import type { BackgroundAgentUsageByDay } from "@/api/contracts/backgroundAgentUsage";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { usageDateOrdinal } from "./usageDateRange";

const VIEW_WIDTH = 760;
const VIEW_HEIGHT = 230;
const PADDING_LEFT = 42;
const PADDING_RIGHT = 18;
const PADDING_TOP = 18;
const PADDING_BOTTOM = 38;

/**
 * 使用原生 SVG 绘制每日输入与输出趋势。
 *
 * Args:
 *   rows: 按本地日期升序排列的每日统计。
 *
 * Returns:
 *   无第三方图表依赖的双折线趋势卡。
 */
export function UsageDailyTrend({
  rows,
  fromDate,
  toDate
}: {
  rows: BackgroundAgentUsageByDay[];
  fromDate: string;
  toDate: string;
}): JSX.Element {
  const plotWidth = VIEW_WIDTH - PADDING_LEFT - PADDING_RIGHT;
  const plotHeight = VIEW_HEIGHT - PADDING_TOP - PADDING_BOTTOM;
  const fromOrdinal = usageDateOrdinal(fromDate);
  const dayCount = usageDateOrdinal(toDate) - fromOrdinal + 1;
  const maximum = Math.max(
    1,
    ...rows.flatMap(row => [row.total_input_tokens, row.output_tokens])
  );
  const xAt = (row: BackgroundAgentUsageByDay): number =>
    PADDING_LEFT +
    ((usageDateOrdinal(row.date) - fromOrdinal + 0.5) / dayCount) * plotWidth;
  const yAt = (value: number): number =>
    PADDING_TOP + plotHeight - (value / maximum) * plotHeight;
  const segments = consecutiveDateSegments(rows);
  const labelStep = Math.max(1, Math.ceil(rows.length / 6));

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle>每日输入 / 输出趋势</CardTitle>
        <CardDescription>所有用量归入任务终态发生的 Backend 本地日期。</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="mb-2 flex gap-4 text-xs text-muted-foreground">
          <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-primary" />总输入</span>
          <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-warning" />输出</span>
        </div>
        <svg
          aria-label="每日后台工作输入和输出 Token 趋势"
          className="h-auto w-full overflow-visible"
          role="img"
          viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
        >
          {[0, 0.5, 1].map(ratio => {
            const y = PADDING_TOP + plotHeight * ratio;
            const label = Math.round(maximum * (1 - ratio)).toLocaleString("zh-CN");
            return (
              <g key={ratio}>
                <line className="stroke-border" x1={PADDING_LEFT} x2={VIEW_WIDTH - PADDING_RIGHT} y1={y} y2={y} />
                <text className="fill-muted-foreground text-[10px]" textAnchor="end" x={PADDING_LEFT - 7} y={y + 3}>{label}</text>
              </g>
            );
          })}
          {segments.filter(segment => segment.length > 1).flatMap((segment, segmentIndex) => [
            <polyline
              className="fill-none stroke-primary"
              key={`input-${segmentIndex}`}
              points={segment.map(row => `${xAt(row)},${yAt(row.total_input_tokens)}`).join(" ")}
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="3"
            />,
            <polyline
              className="fill-none stroke-warning"
              key={`output-${segmentIndex}`}
              points={segment.map(row => `${xAt(row)},${yAt(row.output_tokens)}`).join(" ")}
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="3"
            />
          ])}
          {rows.map(row => (
            <g key={`points-${row.date}`}>
              <circle className="fill-primary" cx={xAt(row)} cy={yAt(row.total_input_tokens)} r="4" />
              <circle className="fill-warning" cx={xAt(row)} cy={yAt(row.output_tokens)} r="4" />
            </g>
          ))}
          {rows.map((row, index) => {
            const shouldShow = index === 0 || index === rows.length - 1 || index % labelStep === 0;
            return shouldShow ? (
              <text
                className="fill-muted-foreground text-[10px]"
                key={row.date}
                textAnchor="middle"
                x={xAt(row)}
                y={VIEW_HEIGHT - 12}
              >
                {row.date.slice(5)}
              </text>
            ) : null;
          })}
        </svg>
      </CardContent>
    </Card>
  );
}

/** 把连续日期组成折线段，避免跨过缺失日期直接连线。 */
function consecutiveDateSegments(
  rows: BackgroundAgentUsageByDay[]
): BackgroundAgentUsageByDay[][] {
  const segments: BackgroundAgentUsageByDay[][] = [];
  for (const row of rows) {
    const currentSegment = segments.at(-1);
    const previousRow = currentSegment?.at(-1);
    if (
      currentSegment === undefined ||
      previousRow === undefined ||
      usageDateOrdinal(row.date) !== usageDateOrdinal(previousRow.date) + 1
    ) {
      segments.push([row]);
      continue;
    }
    currentSegment.push(row);
  }
  return segments;
}
