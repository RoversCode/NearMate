import { useMemo, useState } from "react";
import { Search } from "lucide-react";
import { EmptyState } from "@/components/layout/EmptyState";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { LogRow } from "../../api/contracts/records";

/**
 * 渲染运行日志页。
 *
 * Args:
 *   rows: 本次运行的结构化事件日志。
 *
 * Returns:
 *   可搜索的事件表格；结构化载荷按需展开，避免长 JSON 淹没事件列表。
 */
export function LogRecordsPage({ rows }: { rows: LogRow[] }): JSX.Element {
  const [query, setQuery] = useState("");
  const visibleRows = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    if (!normalizedQuery) return rows;
    return rows.filter(row => `${row.event_type} ${row.recorded_at} ${JSON.stringify(row.payload)}`.toLocaleLowerCase().includes(normalizedQuery));
  }, [query, rows]);

  return (
    <section className="space-y-6">
      <PageHeader title="本次运行日志" description="仅保留当前应用进程中的结构化事件；应用重启后自动清空。" />
      <div className="flex flex-wrap items-center gap-3">
        <label className="relative min-w-0 flex-1">
          <Search aria-hidden="true" className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input aria-label="搜索运行日志" className="pl-9" onChange={event => setQuery(event.target.value)} placeholder="搜索事件、时间或内容" type="search" value={query} />
        </label>
        <span className="text-xs tabular-nums text-muted-foreground">{visibleRows.length} / {rows.length} 条</span>
      </div>
      {rows.length === 0 ? (
        <EmptyState>本次运行暂无日志。</EmptyState>
      ) : visibleRows.length === 0 ? (
        <EmptyState><p>没有匹配的日志。</p><Button className="mt-3" onClick={() => setQuery("")} variant="outline">清空搜索</Button></EmptyState>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>序号</TableHead>
                  <TableHead>时间</TableHead>
                  <TableHead>事件</TableHead>
                  <TableHead>载荷</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleRows.map((row) => (
                  <TableRow key={row.sequence}>
                    <TableCell className="font-mono text-xs text-muted-foreground">{row.sequence}</TableCell>
                    <TableCell className="whitespace-nowrap text-muted-foreground">{row.recorded_at}</TableCell>
                    <TableCell>
                      <Badge variant="secondary">{row.event_type}</Badge>
                    </TableCell>
                    <TableCell className="min-w-32 max-w-xl text-xs">
                      <details>
                        <summary className="cursor-pointer whitespace-nowrap py-1 text-muted-foreground hover:text-foreground">查看载荷</summary>
                        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted/50 p-3 font-mono leading-relaxed">{JSON.stringify(row.payload, null, 2)}</pre>
                      </details>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </section>
  );
}
