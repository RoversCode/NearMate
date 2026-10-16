import type { BackgroundAgentUsageByModel } from "@/api/contracts/backgroundAgentUsage";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatCacheHitRate, formatUsageCount } from "./usageFormat";

/**
 * 按模型展示四类 Token 与缓存命中率。
 *
 * Args:
 *   rows: Backend 返回的按模型汇总行。
 *
 * Returns:
 *   可横向滚动的模型统计表。
 */
export function UsageByModelTable({ rows }: { rows: BackgroundAgentUsageByModel[] }): JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>按模型汇总</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>模型</TableHead>
              <TableHead className="text-right">总输入</TableHead>
              <TableHead className="text-right">非缓存输入</TableHead>
              <TableHead className="text-right">缓存创建</TableHead>
              <TableHead className="text-right">缓存读取</TableHead>
              <TableHead className="text-right">输出</TableHead>
              <TableHead className="text-right">命中率</TableHead>
              <TableHead className="text-right">任务数</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map(row => (
              <TableRow key={row.model}>
                <TableCell className="font-mono text-xs font-medium">{row.model}</TableCell>
                <TableCell className="text-right tabular-nums">{formatUsageCount(row.total_input_tokens)}</TableCell>
                <TableCell className="text-right tabular-nums">{formatUsageCount(row.non_cached_input_tokens)}</TableCell>
                <TableCell className="text-right tabular-nums">{formatUsageCount(row.cache_creation_input_tokens)}</TableCell>
                <TableCell className="text-right tabular-nums">{formatUsageCount(row.cache_read_input_tokens)}</TableCell>
                <TableCell className="text-right tabular-nums">{formatUsageCount(row.output_tokens)}</TableCell>
                <TableCell className="text-right tabular-nums">{formatCacheHitRate(row.cache_hit_rate)}</TableCell>
                <TableCell className="text-right tabular-nums">{formatUsageCount(row.task_count)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
