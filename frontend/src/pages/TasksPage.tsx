import { useEffect, useState } from "react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import type { ReviewTask, Site } from "../types";

const KIND_LABEL: Record<string, string> = {
  meter_rollback: "读数回退/换表",
  missing_reading: "缺计量待补",
  order_conflict: "订单时段冲突",
  order_duplicate: "重复上报订单",
  unexplained_diff: "未解释差额",
  public_confirmation: "公共负荷确认",
  estimated_item: "估算项复核",
  order_meter_diff: "读数/上报偏差",
  backfill_suspended: "回补修订链挂起",
};

export default function TasksPage() {
  const { role } = useAuth();
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [tasks, setTasks] = useState<ReviewTask[]>([]);
  const [reply, setReply] = useState<Record<number, string>>({});
  const [err, setErr] = useState("");

  const load = async () => {
    const { data: s } = await api.get<Site[]>("/api/sites");
    setSites(s);
    const { data } = await api.get<ReviewTask[]>("/api/tasks", {
      params: siteId === "" ? {} : { site_id: siteId },
    });
    setTasks(data);
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [siteId]);

  const respond = async (t: ReviewTask) => {
    setErr("");
    try {
      await api.post(`/api/tasks/${t.id}/respond`, { response: reply[t.id] ?? "" });
      setReply({ ...reply, [t.id]: "" });
      load();
    } catch (e) { setErr(errText(e)); }
  };

  return (
    <div>
      <h2 className="page-title">
        核对事项{role === "property" ? "（仅显示分配给本站物业的条目）" : ""}
      </h2>
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
            <th>站点</th><th>类型</th><th>事项</th><th>处理方</th><th>状态</th>
            <th>已回复内容</th><th>回复/处理</th>
          </tr></thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.id}>
                <td>{sites.find((s) => s.id === t.site_id)?.code ?? t.site_id}</td>
                <td>{KIND_LABEL[t.task_kind] ?? t.task_kind}</td>
                <td style={{ maxWidth: 380 }}>
                  <b>{t.title}</b>
                  <div className="muted">{t.detail}</div>
                </td>
                <td><span className="badge draft">{t.assignee}</span></td>
                <td><span className={`badge ${t.status}`}>{t.status}</span></td>
                <td className="muted">{t.response ?? "—"}</td>
                <td>
                  {t.status === "open" ? (
                    <div className="inline">
                      <input placeholder="填写依据/结论"
                        value={reply[t.id] ?? ""}
                        onChange={(e) => setReply({ ...reply, [t.id]: e.target.value })} />
                      <button onClick={() => respond(t)}>提交</button>
                    </div>
                  ) : (
                    <span className="muted">{t.responded_by}</span>
                  )}
                </td>
              </tr>
            ))}
            {tasks.length === 0 && <tr><td colSpan={7} className="muted">暂无核对事项</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
