import { useEffect, useState } from "react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import type { Bill, Interval, Split, Adjustment, Site, PayableView, BackfillItem } from "../types";

const STATUS_LABEL: Record<string, string> = {
  draft: "待确认", suspended: "挂起", confirmed: "已确认", adjusted: "已更正",
};

export default function BillsPage() {
  const { role } = useAuth();
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [bills, setBills] = useState<Bill[]>([]);
  const [openBill, setOpenBill] = useState<Bill | null>(null);
  const [intervals, setIntervals] = useState<Interval[]>([]);
  const [splits, setSplits] = useState<Split[]>([]);
  const [adjustments, setAdjustments] = useState<Adjustment[]>([]);
  const [backfillItems, setBackfillItems] = useState<BackfillItem[]>([]);
  const [payable, setPayable] = useState<PayableView | null>(null);
  const [precheck, setPrecheck] = useState<Record<string, unknown> | null>(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = async () => {
    const { data: s } = await api.get<Site[]>("/api/sites");
    setSites(s);
    const sid = siteId === "" ? undefined : siteId;
    const { data } = await api.get<Bill[]>("/api/bills", { params: { site_id: sid } });
    setBills(data);
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [siteId]);

  const drill = async (bill: Bill) => {
    setOpenBill(bill);
    setMsg(""); setErr(""); setPrecheck(null);
    const [iv, sp, adj, pre, pv, bf] = await Promise.all([
      api.get<Interval[]>(`/api/jobs/${bill.job_id}/intervals`),
      api.get<Split[]>(`/api/jobs/${bill.job_id}/splits`),
      api.get<Adjustment[]>(`/api/bills/${bill.id}/adjustments`),
      api.get(`/api/bills/${bill.id}/precheck`),
      api.get<PayableView>(`/api/bills/${bill.id}/payable`),
      api.get<BackfillItem[]>(`/api/bills/${bill.id}/backfill-items`),
    ]);
    setIntervals(iv.data); setSplits(sp.data); setAdjustments(adj.data); setPrecheck(pre.data);
    setPayable(pv.data); setBackfillItems(bf.data);
  };

  const confirm = async () => {
    if (!openBill) return;
    setErr(""); setMsg("");
    try {
      const { data } = await api.post<Bill>(`/api/bills/${openBill.id}/confirm`);
      setMsg("账单已确认，原值已冻结");
      setOpenBill(data);
      load();
    } catch (e) { setErr(errText(e)); }
  };

  if (openBill) {
    return (
      <div>
        <button className="secondary" onClick={() => setOpenBill(null)}>← 返回账单列表</button>
        <h2 className="page-title">账单 {openBill.bill_no} <span className={`badge ${openBill.status}`}>{STATUS_LABEL[openBill.status]}</span></h2>
        {msg && <div className="ok-msg">{msg}</div>}
        {err && <div className="error">{err}</div>}
        <div className="panel">
          <h2>确认前依据检查</h2>
          <div className="row">
            <span>新修订数：<b>{String(precheck?.new_revision_count)}</b></span>
            <span>作业状态信息：{String(precheck?.job_message ?? "")}</span>
          </div>
          {Number(precheck?.new_revision_count) > 0 && (
            <div className="error">依据在作业冻结后出现新修订（含被阻塞更正），请先发起下一轮核算，不能直接确认。</div>
          )}
          {role === "finance" && (
            <div style={{ marginTop: 12 }}>
              <button onClick={confirm} disabled={openBill.status !== "draft"}>确认账单</button>
              {openBill.status === "suspended" && <span className="muted" style={{ marginLeft: 10 }}>挂起账单（差额超阈值或计量段挂起）不可确认</span>}
              {openBill.status === "confirmed" && <span className="muted" style={{ marginLeft: 10 }}>已确认，仅可登记差额更正</span>}
            </div>
          )}
        </div>

        <div className="panel">
          <h2>电量与金额构成（不摊平：充电 / 公共 / 未解释分列）</h2>
          <div className="stat-grid">
            <div className="stat"><div className="k">物业总表电量 kWh</div><div className="v">{openBill.master_total_kwh}</div></div>
            <div className="stat"><div className="k">充电实测 kWh</div><div className="v">{openBill.charging_kwh}</div></div>
            <div className="stat"><div className="k">估算电量 kWh</div><div className="v warn">{openBill.estimated_kwh}</div></div>
            <div className="stat"><div className="k">待补订单数</div><div className="v warn">{openBill.pending_order_count}</div></div>
            <div className="stat"><div className="k">经确认公共负荷 kWh</div><div className="v">{openBill.public_kwh}</div></div>
            <div className="stat"><div className="k">未解释差额 kWh</div><div className={`v ${openBill.status === "suspended" ? "danger" : "warn"}`}>{openBill.unexplained_kwh}</div></div>
            <div className="stat"><div className="k">充电金额（元）</div><div className="v">{openBill.charging_amount}</div></div>
            <div className="stat"><div className="k">公共负荷金额（元）</div><div className="v">{openBill.public_amount}</div></div>
          </div>
        </div>

        <div className="panel">
          <h2>逐计量区间下钻（总表读数差值 vs 同范围订单/设备表用电）</h2>
          <table>
            <thead><tr>
              <th>区间（站点本地时间）</th><th className="num">总表起读数</th><th className="num">总表止读数</th>
              <th className="num">总表电量</th><th className="num">设备表电量</th><th className="num">充电</th>
              <th className="num">估算</th><th className="num">公共</th><th className="num">未解释</th><th>依据</th>
            </tr></thead>
            <tbody>
              {intervals.map((iv) => (
                <tr key={iv.id}>
                  <td className="mono">{iv.start_ts_local.slice(5, 16)} ~ {iv.end_ts_local.slice(5, 16)}</td>
                  <td className="num">{iv.master_reading_start ?? "—"}</td>
                  <td className="num">{iv.master_reading_end ?? "—"}</td>
                  <td className="num">{iv.master_energy_kwh ?? "挂起"}</td>
                  <td className="num">{iv.device_meter_kwh}</td>
                  <td className="num">{iv.charging_kwh}</td>
                  <td className="num">{iv.estimated_kwh}</td>
                  <td className="num">{iv.public_kwh}</td>
                  <td className="num">{iv.unexplained_kwh}</td>
                  <td><span className={`badge ${iv.basis_status}`}>{iv.basis_status}</span>
                    {iv.note && <div className="muted">{iv.note}</div>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="panel">
          <h2>订单按电价边界拆分段（估算/待补均显式标注）</h2>
          <table>
            <thead><tr>
              <th>订单</th><th>本地时段</th><th>分时</th><th className="num">单价</th>
              <th className="num">电量 kWh</th><th className="num">金额（元）</th><th>依据</th><th>说明</th>
            </tr></thead>
            <tbody>
              {splits.map((sp) => (
                <tr key={sp.id}>
                  <td>#{sp.order_id}</td>
                  <td className="mono">{sp.start_ts_local.slice(5, 16)}~{sp.end_ts_local.slice(11, 16)}</td>
                  <td>{sp.segment_name}</td>
                  <td className="num">{sp.price_per_kwh}</td>
                  <td className="num">{sp.energy_kwh ?? "待补"}</td>
                  <td className="num">{sp.amount ?? "—"}</td>
                  <td><span className={`badge ${sp.basis_status}`}>{sp.basis_status}</span></td>
                  <td className="muted">{sp.note ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {role === "finance" && <AdjustmentPanel bill={openBill} adjustments={adjustments} onDone={load} onChange={setAdjustments} />}
        {adjustments.length > 0 && role !== "finance" && (
          <div className="panel">
            <h2>差额更正记录（原值保留，不替换）</h2>
            <AdjustmentTable adjustments={adjustments} />
          </div>
        )}

        {payable && (
          <div className="panel">
            <h2>当前应付（旧账金额冻结不覆盖，单列回补调整项）</h2>
            <div className="stat-grid">
              <div className="stat"><div className="k">原充电金额</div><div className="v">{payable.original_charging_amount}</div></div>
              <div className="stat"><div className="k">原公共金额</div><div className="v">{payable.original_public_amount}</div></div>
              <div className="stat"><div className="k">人工差额更正</div><div className="v">{payable.manual_adjustment_delta}</div></div>
              <div className="stat"><div className="k">回补已确认差额</div><div className="v warn">{payable.backfill_confirmed_delta}</div></div>
              <div className="stat"><div className="k">当前应付合计</div><div className="v">{payable.current_payable_amount}</div></div>
              <div className="stat"><div className="k">待复核/退回</div><div className="v">{payable.backfill_pending_count} / {payable.backfill_rejected_count}</div></div>
            </div>
            <div className="muted" style={{ marginTop: 8 }}>{payable.note}</div>
          </div>
        )}

        {backfillItems.length > 0 && (
          <div className="panel">
            <h2>跨月回补调整项（撤原贡献 + 计修正值，逐项可对平）</h2>
            <table>
              <thead><tr>
                <th>变更</th><th>成分/时段</th><th className="num">电量差</th>
                <th className="num">金额差</th><th className="num">舍入差</th>
                <th>状态</th><th>依据说明</th>
              </tr></thead>
              <tbody>
                {backfillItems.map((it) => (
                  <tr key={it.id}>
                    <td className="mono">{it.change_kind}<div className="muted">{it.reason_kind}</div></td>
                    <td className="mono">{it.component}{it.segment_name ? ` / ${it.segment_name}` : ""}</td>
                    <td className="num">{it.energy_delta}</td>
                    <td className="num"><b>{it.amount_delta}</b></td>
                    <td className="num">{it.rounding_delta}</td>
                    <td><span className={`badge ${it.status === "confirmed" ? "confirmed" : "suspended"}`}>{it.status}</span></td>
                    <td className="muted" style={{ maxWidth: 320 }}>
                      {it.suspended_reason ?? it.reason_text}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  }

  return (
    <div>
      <h2 className="page-title">月度账单</h2>
      <div className="filter-bar">
        <label>站点</label>
        <select value={siteId} onChange={(e) => setSiteId(e.target.value === "" ? "" : Number(e.target.value))}>
          <option value="">全部（按权限）</option>
          {sites.map((s) => <option key={s.id} value={s.id}>{s.code} {s.name}</option>)}
        </select>
      </div>
      <div className="panel">
        <table>
          <thead><tr>
            <th>账单号</th><th>站点</th><th className="num">总表电量</th><th className="num">充电</th>
            <th className="num">估算</th><th className="num">公共</th><th className="num">未解释差额</th>
            <th className="num">充电金额</th><th>状态</th><th></th>
          </tr></thead>
          <tbody>
            {bills.map((b) => (
              <tr key={b.id}>
                <td className="mono">{b.bill_no}</td>
                <td>{sites.find((s) => s.id === b.site_id)?.code ?? b.site_id}</td>
                <td className="num">{b.master_total_kwh}</td>
                <td className="num">{b.charging_kwh}</td>
                <td className="num">{b.estimated_kwh}</td>
                <td className="num">{b.public_kwh}</td>
                <td className="num">{b.unexplained_kwh}</td>
                <td className="num">{b.charging_amount}</td>
                <td><span className={`badge ${b.status}`}>{STATUS_LABEL[b.status]}</span></td>
                <td><button className="secondary" onClick={() => drill(b)}>下钻核对</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AdjustmentTable({ adjustments }: { adjustments: Adjustment[] }) {
  return (
    <table>
      <thead><tr><th>成分</th><th className="num">原值</th><th className="num">更正值</th><th className="num">差额 delta</th><th>原因</th><th>登记人</th></tr></thead>
      <tbody>
        {adjustments.map((a) => (
          <tr key={a.id}>
            <td className="mono">{a.component}</td><td className="num">{a.original_value}</td>
            <td className="num">{a.corrected_value}</td><td className="num">{a.delta}</td>
            <td>{a.reason}</td><td>{a.created_by}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function AdjustmentPanel({ bill, adjustments, onDone, onChange }: {
  bill: Bill; adjustments: Adjustment[]; onDone: () => void;
  onChange: (a: Adjustment[]) => void;
}) {
  const [component, setComponent] = useState("unexplained_kwh");
  const [corrected, setCorrected] = useState("");
  const [reason, setReason] = useState("");
  const [err, setErr] = useState("");
  const canAdjust = bill.status === "confirmed" || bill.status === "adjusted";

  const submit = async () => {
    setErr("");
    try {
      const { data } = await api.post<Adjustment>(`/api/bills/${bill.id}/adjustments`, {
        component, corrected_value: corrected, reason,
      });
      onChange([...adjustments, data]);
      setCorrected(""); setReason(""); onDone();
    } catch (e) { setErr(errText(e)); }
  };

  return (
    <div className="panel">
      <h2>差额更正（已确认账单专用，原值不被替换）</h2>
      {!canAdjust && <div className="muted">账单确认后才能登记更正；挂起账单需先解除挂起并重新核算。</div>}
      <div className="row">
        <div>
          <label>更正成分</label>
          <select value={component} onChange={(e) => setComponent(e.target.value)} disabled={!canAdjust}>
            {["unexplained_kwh", "public_kwh", "charging_kwh", "estimated_kwh", "charging_amount", "public_amount"]
              .map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div><label>更正后值</label><input value={corrected} onChange={(e) => setCorrected(e.target.value)} disabled={!canAdjust} /></div>
        <div style={{ flex: 1, minWidth: 220 }}><label>依据/原因</label><input style={{ width: "100%" }} value={reason} onChange={(e) => setReason(e.target.value)} disabled={!canAdjust} /></div>
        <button onClick={submit} disabled={!canAdjust || !corrected || !reason}>登记更正</button>
      </div>
      {err && <div className="error">{err}</div>}
      {adjustments.length > 0 && <div style={{ marginTop: 12 }}><AdjustmentTable adjustments={adjustments} /></div>}
    </div>
  );
}
