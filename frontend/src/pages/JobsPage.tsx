import { useEffect, useState } from "react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import type { Job, Site } from "../types";

const JOB_STATUS: Record<string, string> = {
  running: "执行中", completed: "完成", suspended: "挂起", failed: "失败",
};

export default function JobsPage() {
  const { role } = useAuth();
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [start, setStart] = useState("2026-10-01T00:00");
  const [end, setEnd] = useState("2026-11-01T00:00");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = async () => {
    const { data: s } = await api.get<Site[]>("/api/sites");
    setSites(s);
    const { data } = await api.get<Job[]>("/api/jobs", {
      params: siteId === "" ? {} : { site_id: siteId },
    });
    setJobs(data);
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [siteId]);

  const run = async (rerunOf: number | null) => {
    if (siteId === "") { setErr("请先选择站点"); return; }
    setErr(""); setMsg("");
    try {
      const { data } = await api.post<Job>("/api/jobs", {
        site_id: siteId,
        cycle_start: start + ":00",
        cycle_end: end + ":00",
        rerun_of_job_id: rerunOf,
      });
      setMsg(`作业 #${data.id} ${JOB_STATUS[data.status]}：${data.message ?? ""}`);
      load();
    } catch (e) { setErr(errText(e)); }
  };

  return (
    <div>
      <h2 className="page-title">核算作业（冻结读数/电价/订单，重跑沿用相同输入）</h2>
      {role === "finance" && (
        <div className="panel">
          <div className="row">
            <div>
              <label>站点</label>
              <select value={siteId} onChange={(e) => setSiteId(e.target.value === "" ? "" : Number(e.target.value))}>
                <option value="">请选择</option>
                {sites.map((s) => <option key={s.id} value={s.id}>{s.code} {s.name}（{s.timezone}）</option>)}
              </select>
            </div>
            <div><label>周期开始（站点本地时间）</label><input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></div>
            <div><label>周期结束（站点本地时间）</label><input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
            <button onClick={() => run(null)}>发起核算</button>
          </div>
          <div className="muted" style={{ marginTop: 8 }}>
            执行期间到达的更正会被标记 blocked_adopted 留到下一轮；已被采用的数据不会被覆盖。
          </div>
        </div>
      )}
      {msg && <div className="ok-msg">{msg}</div>}
      {err && <div className="error">{err}</div>}
      <div className="panel">
        <table>
          <thead><tr>
            <th>作业#</th><th>站点</th><th>周期（本地）</th><th>状态</th>
            <th>阈值</th><th>重跑源</th><th>信息</th>{role === "finance" && <th></th>}
          </tr></thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>{j.id}</td>
                <td>{sites.find((s) => s.id === j.site_id)?.code ?? j.site_id}</td>
                <td className="mono">{j.cycle_start_local.slice(0, 10)} ~ {j.cycle_end_local.slice(0, 10)}</td>
                <td><span className={`badge ${j.status}`}>{JOB_STATUS[j.status]}</span></td>
                <td className="num">{(Number(j.threshold_pct) * 100).toFixed(2)}%</td>
                <td>{j.rerun_of_job_id ? `#${j.rerun_of_job_id}` : "—"}</td>
                <td>{j.message}</td>
                {role === "finance" && (
                  <td><button className="secondary" onClick={() => run(j.id)}>按相同输入重跑</button></td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
