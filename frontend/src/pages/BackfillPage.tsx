import { useEffect, useState } from "react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import type {
  BackfillBatch, BackfillSegment, BackfillItem, Site, PayableView,
} from "../types";

const BATCH_STATUS: Record<string, string> = {
  pending: "待执行", running: "执行中", suspended: "挂起",
  completed: "完成", failed: "失败",
};
const SEG_STATUS: Record<string, string> = {
  pending: "待处理", running: "执行中", done: "完成",
  suspended: "挂起", failed: "失败", skipped: "跳过",
};
const ITEM_STATUS: Record<string, string> = {
  pending_confirmation: "待复核", property_reviewed: "物业已复核",
  confirmed: "财务已确认", rejected: "已退回", suspended: "挂起",
};
const CHANGE_KIND: Record<string, string> = {
  late: "迟到补计", correction: "依据纠正", revocation: "撤销",
};

export default function BackfillPage() {
  const { role } = useAuth();
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [batches, setBatches] = useState<BackfillBatch[]>([]);
  const [note, setNote] = useState("");
  const [candidates, setCandidates] = useState<Record<string, unknown>[]>([]);
  const [openBatch, setOpenBatch] = useState<BackfillBatch | null>(null);
  const [segments, setSegments] = useState<BackfillSegment[]>([]);
  const [items, setItems] = useState<BackfillItem[]>([]);
  const [payables, setPayables] = useState<Record<number, PayableView>>({});
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const loadBatches = async () => {
    const { data: s } = await api.get<Site[]>("/api/sites");
    setSites(s);
    const { data } = await api.get<BackfillBatch[]>("/api/backfill/batches", {
      params: siteId === "" ? {} : { site_id: siteId },
    });
    setBatches(data);
  };
  useEffect(() => { loadBatches(); /* eslint-disable-next-line */ }, [siteId]);

  const preview = async () => {
    if (siteId === "") { setErr("请先选择站点"); return; }
    setErr("");
    try {
      const { data } = await api.get("/api/backfill/candidates", { params: { site_id: siteId } });
      setCandidates(data);
    } catch (e) { setErr(errText(e)); }
  };

  const createAndRun = async () => {
    if (siteId === "") { setErr("请先选择站点"); return; }
    setErr(""); setMsg("");
    try {
      const { data: batch } = await api.post<BackfillBatch>("/api/backfill/batches", {
        site_id: siteId, reason_note: note || null,
      });
      const { data: ran } = await api.post<BackfillBatch>(
        `/api/backfill/batches/${batch.id}/run`);
      setMsg(`批次 ${ran.batch_no}：${BATCH_STATUS[ran.status]}（${ran.message ?? ""}）`);
      setCandidates([]);
      loadBatches();
    } catch (e) { setErr(errText(e)); }
  };

  const resume = async (b: BackfillBatch) => {
    setErr(""); setMsg("");
    try {
      const { data } = await api.post<BackfillBatch>(`/api/backfill/batches/${b.id}/run`);
      setMsg(`续算完成：${BATCH_STATUS[data.status]}，已完成分段不重算`);
      loadBatches();
      if (openBatch?.id === b.id) openDetail(data);
    } catch (e) { setErr(errText(e)); }
  };

  const openDetail = async (b: BackfillBatch) => {
    setOpenBatch(b);
    const [sg, it] = await Promise.all([
      api.get<BackfillSegment[]>(`/api/backfill/batches/${b.id}/segments`),
      api.get<BackfillItem[]>(`/api/backfill/batches/${b.id}/items`),
    ]);
    setSegments(sg.data); setItems(it.data);
    const billIds = [...new Set(it.data.map((x) => x.bill_id))];
    const pv: Record<number, PayableView> = {};
    for (const id of billIds) {
      const { data } = await api.get<PayableView>(`/api/bills/${id}/payable`);
      pv[id] = data;
    }
    setPayables(pv);
  };

  if (openBatch) {
    return (
      <div>
        <button className="secondary" onClick={() => setOpenBatch(null)}>← 返回批次列表</button>
        <h2 className="page-title">
          回补批次 {openBatch.batch_no}{" "}
          <span className={`badge ${openBatch.status}`}>{BATCH_STATUS[openBatch.status]}</span>
        </h2>
        {msg && <div className="ok-msg">{msg}</div>}
        {err && <div className="error">{err}</div>}
        <div className="panel">
          <h2>分段进度（失败只重算未完成分段）</h2>
          <table>
            <thead><tr>
              <th>分段#</th><th>受影响区间（本地）</th><th>状态</th><th className="num">尝试次数</th>
              <th>挂起/错误原因</th>
            </tr></thead>
            <tbody>
              {segments.map((s) => (
                <tr key={s.id}>
                  <td>{s.id}</td>
                  <td className="mono">{s.start_ts_local.slice(5, 16)} ~ {s.end_ts_local.slice(5, 16)}</td>
                  <td><span className={`badge ${s.status}`}>{SEG_STATUS[s.status] ?? s.status}</span></td>
                  <td className="num">{s.attempts}</td>
                  <td className="muted">{s.suspended_reason ?? s.last_error ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(openBatch.status === "failed" || openBatch.status === "suspended") && (
            <div style={{ marginTop: 10 }}>
              <button onClick={() => resume(openBatch)}>续算未完成分段</button>
              <span className="muted" style={{ marginLeft: 10 }}>{openBatch.message}</span>
            </div>
          )}
        </div>

        <div className="panel">
          <h2>回补调整项（撤原贡献 + 计修正值，旧账金额不覆盖）</h2>
          <table>
            <thead><tr>
              <th>类型</th><th>成分/时段</th><th className="num">电量前</th><th className="num">电量后</th>
              <th className="num">电量差</th><th className="num">金额前</th><th className="num">金额后</th>
              <th className="num">金额差</th><th className="num">舍入差</th><th>状态</th><th>依据</th>
              <th>复核</th>
            </tr></thead>
            <tbody>
              {items.map((it) => (
                <ItemRow key={it.id} item={it} role={role}
                  onChanged={() => openDetail(openBatch)} />
              ))}
              {items.length === 0 && (
                <tr><td colSpan={12} className="muted">该批次暂无调整项（可能仍有挂起分段）</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="panel">
          <h2>受影响账单的当前应付（旧账 + 人工更正 + 已确认回补项）</h2>
          <table>
            <thead><tr>
              <th>账单</th><th className="num">原充电金额</th><th className="num">原公共金额</th>
              <th className="num">人工更正差</th><th className="num">回补已确认差</th>
              <th className="num">当前应付</th><th>待处理/退回</th>
            </tr></thead>
            <tbody>
              {Object.values(payables).map((p) => (
                <tr key={p.bill_id}>
                  <td className="mono">{p.bill_no}</td>
                  <td className="num">{p.original_charging_amount}</td>
                  <td className="num">{p.original_public_amount}</td>
                  <td className="num">{p.manual_adjustment_delta}</td>
                  <td className="num">{p.backfill_confirmed_delta}</td>
                  <td className="num"><b>{p.current_payable_amount}</b></td>
                  <td className="muted">待复核 {p.backfill_pending_count} / 退回 {p.backfill_rejected_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted" style={{ marginTop: 8 }}>
            {Object.values(payables)[0]?.note}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h2 className="page-title">跨月回补（已确认月份的迟到订单 / 纠正读数 / 撤销重复记录）</h2>
      {role === "finance" && (
        <div className="panel">
          <div className="row">
            <div>
              <label>站点</label>
              <select value={siteId} onChange={(e) => setSiteId(e.target.value === "" ? "" : Number(e.target.value))}>
                <option value="">请选择</option>
                {sites.map((s) => <option key={s.id} value={s.id}>{s.code} {s.name}</option>)}
              </select>
            </div>
            <div style={{ flex: 1, minWidth: 240 }}>
              <label>批次说明</label>
              <input style={{ width: "100%" }} value={note} onChange={(e) => setNote(e.target.value)}
                placeholder="例如：11 月收到 10 月迟到订单与读数纠正" />
            </div>
            <button className="secondary" onClick={preview} disabled={siteId === ""}>预览候选</button>
            <button onClick={createAndRun} disabled={siteId === ""}>创建并执行批次</button>
          </div>
          <div className="muted" style={{ marginTop: 8 }}>
            批次发起即冻结更正数据集合，启动后新到更正进入下一批；同源修订缺中间版本的分段会挂起，不用最新行猜测历史。
          </div>
          {candidates.length > 0 && (
            <table style={{ marginTop: 10 }}>
              <thead><tr><th>来源记录号</th><th>来源类型</th><th>修订版本</th><th>变更类别</th></tr></thead>
              <tbody>
                {candidates.map((c, i) => (
                  <tr key={i}>
                    <td className="mono">{String(c.source_record_no)}</td>
                    <td>{String(c.source_type)}</td>
                    <td className="num">v{String(c.revision_no)}</td>
                    <td><span className="badge draft">{CHANGE_KIND[String(c.change_kind)] ?? String(c.change_kind)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      {msg && <div className="ok-msg">{msg}</div>}
      {err && <div className="error">{err}</div>}
      <div className="panel">
        <table>
          <thead><tr>
            <th>批次号</th><th>站点</th><th>状态</th>
            <th className="num">分段</th><th className="num">完成</th><th className="num">挂起</th><th className="num">失败</th>
            <th>说明</th><th></th>
          </tr></thead>
          <tbody>
            {batches.map((b) => (
              <tr key={b.id}>
                <td className="mono">{b.batch_no}</td>
                <td>{sites.find((s) => s.id === b.site_id)?.code ?? b.site_id}</td>
                <td><span className={`badge ${b.status}`}>{BATCH_STATUS[b.status] ?? b.status}</span></td>
                <td className="num">{b.segment_total}</td>
                <td className="num">{b.segment_done}</td>
                <td className="num">{b.segment_suspended}</td>
                <td className="num">{b.segment_failed}</td>
                <td className="muted" style={{ maxWidth: 280 }}>{b.message}</td>
                <td><button className="secondary" onClick={() => openDetail(b)}>分段/调整项下钻</button></td>
              </tr>
            ))}
            {batches.length === 0 && <tr><td colSpan={9} className="muted">暂无回补批次</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ItemRow({ item, role, onChanged }: {
  item: BackfillItem; role: string; onChanged: () => void;
}) {
  const [showDetail, setShowDetail] = useState(false);
  const [comment, setComment] = useState("");
  const [err, setErr] = useState("");

  const review = async (action: string) => {
    setErr("");
    try {
      await api.post(`/api/backfill/items/${item.id}/reviews`, { action, comment: comment || null });
      setComment("");
      onChanged();
    } catch (e) { setErr(errText(e)); }
  };

  const canProperty = role === "property"
    && (item.status === "pending_confirmation");
  const canFinance = role === "finance" && item.status === "property_reviewed";

  return (
    <tr>
      <td><span className="badge draft">{CHANGE_KIND[item.change_kind] ?? item.change_kind}</span>
        <div className="muted">{item.reason_kind}</div></td>
      <td className="mono">{item.component}{item.segment_name ? ` / ${item.segment_name}` : ""}</td>
      <td className="num">{item.energy_before}</td>
      <td className="num">{item.energy_after}</td>
      <td className="num">{item.energy_delta}</td>
      <td className="num">{item.amount_before}</td>
      <td className="num">{item.amount_after}</td>
      <td className="num"><b>{item.amount_delta}</b></td>
      <td className="num">{item.rounding_delta}</td>
      <td><span className={`badge ${item.status === "confirmed" ? "confirmed" : item.status === "rejected" ? "rejected" : "suspended"}`}>
        {ITEM_STATUS[item.status] ?? item.status}
      </span>
        {item.suspended_reason && <div className="muted">{item.suspended_reason}</div>}
        {item.reject_note && <div className="muted">退回说明：{item.reject_note}</div>}
      </td>
      <td>
        <button className="secondary" onClick={() => setShowDetail(!showDetail)}>
          {showDetail ? "收起依据" : "查看对平依据"}
        </button>
        {showDetail && (
          <pre className="raw" style={{ marginTop: 6, maxWidth: 360 }}>
            {JSON.stringify(JSON.parse(item.detail_json), null, 2)}
          </pre>
        )}
        <div className="muted" style={{ maxWidth: 260 }}>{item.reason_text}</div>
      </td>
      <td>
        {item.status === "suspended" ? (
          <span className="muted">补齐中间版本后续算</span>
        ) : (
          <div className="inline" style={{ flexWrap: "wrap" }}>
            {canProperty && (
              <>
                <input placeholder="复核意见" value={comment}
                  onChange={(e) => setComment(e.target.value)} style={{ width: 130 }} />
                <button onClick={() => review("acknowledge")}>确认无误</button>
                <button className="danger" onClick={() => review("reject")}>否认退回</button>
              </>
            )}
            {canFinance && <button onClick={() => review("confirm")}>财务确认入结算</button>}
            {!canProperty && !canFinance && <span className="muted">—</span>}
          </div>
        )}
        {err && <div className="error">{err}</div>}
      </td>
    </tr>
  );
}
