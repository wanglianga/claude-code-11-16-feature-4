import { useEffect, useState } from "react";
import { api } from "../api";
import type { Site } from "../types";

type Tab = "readings" | "orders" | "public" | "replacements";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export default function EntitiesPage() {
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [tab, setTab] = useState<Tab>("readings");
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [rows, setRows] = useState<any[]>([]);

  useEffect(() => { api.get<Site[]>("/api/sites").then(({ data }) => {
    setSites(data);
    if (data.length) setSiteId(data[0].id);
  }); }, []);

  useEffect(() => {
    if (siteId === "") return;
    const url = {
      readings: `/api/entities/readings?site_id=${siteId}`,
      orders: `/api/entities/orders?site_id=${siteId}`,
      public: `/api/entities/public-loads?site_id=${siteId}`,
      replacements: `/api/entities/replacements?site_id=${siteId}`,
    }[tab];
    api.get(url).then(({ data }) => setRows(data));
  }, [siteId, tab]);

  return (
    <div>
      <h2 className="page-title">读数与订单查询（账单下钻的原始依据）</h2>
      <div className="filter-bar">
        <label>站点</label>
        <select value={siteId} onChange={(e) => setSiteId(Number(e.target.value))}>
          {sites.map((s) => <option key={s.id} value={s.id}>{s.code} {s.name}</option>)}
        </select>
      </div>
      <div className="tabs">
        <button className={tab === "readings" ? "active" : ""} onClick={() => setTab("readings")}>电表读数</button>
        <button className={tab === "orders" ? "active" : ""} onClick={() => setTab("orders")}>充电订单</button>
        <button className={tab === "public" ? "active" : ""} onClick={() => setTab("public")}>公共负荷</button>
        <button className={tab === "replacements" ? "active" : ""} onClick={() => setTab("replacements")}>设备更换</button>
      </div>
      <div className="panel">
        {tab === "readings" && (
          <table>
            <thead><tr><th>表号</th><th>类型</th><th>设备</th><th>本地时间</th><th className="num">读数 kWh</th><th>状态</th><th>采用作业</th><th>修订</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.meter_no}</td><td>{r.meter_kind}</td><td>{r.device_id ?? "—"}</td>
                <td className="mono">{r.reading_ts_local}</td><td className="num">{r.reading_kwh}</td>
                <td><span className="badge draft">{r.status}</span>{r.is_correction && <span className="badge estimated"> 补报</span>}</td>
                <td>{r.adopted_by_job_id ? `#${r.adopted_by_job_id}` : "—"}</td>
                <td className="mono muted">rev#{r.revision_id}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
        {tab === "orders" && (
          <table>
            <thead><tr><th>订单号</th><th>设备</th><th>本地开始</th><th>本地结束</th><th className="num">起读数</th><th className="num">止读数</th><th className="num">上报电量</th><th>状态</th><th>冲突</th><th>采用作业</th></tr></thead>
            <tbody>{rows.map((o) => (
              <tr key={o.id}>
                <td className="mono">{o.order_no}</td><td>{o.device_id}</td>
                <td className="mono">{o.start_ts_local}</td><td className="mono">{o.end_ts_local}</td>
                <td className="num">{o.start_reading_kwh ?? "—"}</td><td className="num">{o.end_reading_kwh ?? "—"}</td>
                <td className="num">{o.reported_energy_kwh}</td>
                <td><span className="badge draft">{o.status}</span></td>
                <td>{o.conflict_id ? `#${o.conflict_id}` : "—"}</td>
                <td>{o.adopted_by_job_id ? `#${o.adopted_by_job_id}` : "—"}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
        {tab === "public" && (
          <table>
            <thead><tr><th>负荷名称</th><th>本地开始</th><th>本地结束</th><th className="num">电量 kWh</th><th>是否确认</th><th>状态</th><th>采用作业</th></tr></thead>
            <tbody>{rows.map((p) => (
              <tr key={p.id}>
                <td>{p.load_name}</td>
                <td className="mono">{p.start_ts_local}</td><td className="mono">{p.end_ts_local}</td>
                <td className="num">{p.energy_kwh}</td>
                <td>{p.confirmed ? <span className="badge confirmed">已确认</span> : <span className="badge pending">未确认</span>}</td>
                <td><span className="badge draft">{p.status}</span></td>
                <td>{p.adopted_by_job_id ? `#${p.adopted_by_job_id}` : "—"}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
        {tab === "replacements" && (
          <table>
            <thead><tr><th>表号</th><th>生效时间(UTC)</th><th className="num">旧表尾读数</th><th className="num">新表起读数</th><th>依据</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.meter_no}</td><td className="mono">{r.effective_ts}</td>
                <td className="num">{r.old_final_reading ?? "—"}</td>
                <td className="num">{r.new_initial_reading ?? "—"}</td>
                <td className="muted">{r.evidence_note}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </div>
  );
}
