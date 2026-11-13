import { useEffect, useState } from "react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import type { Conflict, Site } from "../types";

export default function ConflictsPage() {
  const { role } = useAuth();
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [note, setNote] = useState<Record<number, string>>({});
  const [err, setErr] = useState("");

  const load = async () => {
    const { data: s } = await api.get<Site[]>("/api/sites");
    setSites(s);
    const { data } = await api.get<Conflict[]>("/api/conflicts", {
      params: siteId === "" ? {} : { site_id: siteId },
    });
    setConflicts(data);
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [siteId]);

  const resolve = async (c: Conflict, winnerId: number) => {
    setErr("");
    try {
      await api.post(`/api/conflicts/${c.id}/resolve`, {
        resolved_order_id: winnerId,
        resolution_note: note[c.id] ?? "运营确认",
      });
      load();
    } catch (e) { setErr(errText(e)); }
  };

  return (
    <div>
      <h2 className="page-title">订单时段冲突（两个不同订单声称占用同一设备时段，交运营确认）</h2>
      {err && <div className="error">{err}</div>}
      <div className="filter-bar">
        <label>站点</label>
        <select value={siteId} onChange={(e) => setSiteId(e.target.value === "" ? "" : Number(e.target.value))}>
          <option value="">全部（按权限）</option>
          {sites.map((s) => <option key={s.id} value={s.id}>{s.code}</option>)}
        </select>
      </div>
      <div className="panel">
        <table>
          <thead><tr>
            <th>站点</th><th>设备</th><th>订单 A#</th><th>订单 B#</th><th>状态</th>
            <th>确认结论/备注</th><th>运营处理</th>
          </tr></thead>
          <tbody>
            {conflicts.map((c) => (
              <tr key={c.id}>
                <td>{sites.find((s) => s.id === c.site_id)?.code ?? c.site_id}</td>
                <td className="mono">{c.device_id}</td>
                <td>#{c.order_a_id}</td>
                <td>#{c.order_b_id}</td>
                <td><span className={`badge ${c.status}`}>{c.status}</span>
                  {c.resolution_note && <div className="muted">{c.resolution_note}</div>}</td>
                <td>
                  {c.status === "open" ? (
                    <input placeholder="核对依据/工单号" value={note[c.id] ?? ""}
                      onChange={(e) => setNote({ ...note, [c.id]: e.target.value })} />
                  ) : <>确认订单 #{c.resolved_order_id}</>}
                </td>
                <td>
                  {c.status === "open" && role === "operator" && (
                    <div className="inline">
                      <button className="secondary" onClick={() => resolve(c, c.order_a_id)}>采信 A</button>
                      <button className="secondary" onClick={() => resolve(c, c.order_b_id)}>采信 B</button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
            {conflicts.length === 0 && <tr><td colSpan={7} className="muted">暂无冲突</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
