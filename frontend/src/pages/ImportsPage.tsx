import { useEffect, useState } from "react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import type { ImportFile, ImportRow, Site } from "../types";

const SOURCE_TYPES = [
  ["master_reading", "物业总表读数"],
  ["device_reading", "设备累计读数"],
  ["order", "充电订单"],
  ["tariff", "分时电价"],
  ["public_load", "公共负荷"],
  ["replacement", "设备更换记录"],
];

export default function ImportsPage() {
  const { role } = useAuth();
  const canUpload = role === "finance" || role === "operator";
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [sourceType, setSourceType] = useState(SOURCE_TYPES[0][0]);
  const [file, setFile] = useState<File | null>(null);
  const [files, setFiles] = useState<ImportFile[]>([]);
  const [rows, setRows] = useState<ImportRow[] | null>(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = async () => {
    const { data: s } = await api.get<Site[]>("/api/sites");
    setSites(s);
    const { data } = await api.get<ImportFile[]>("/api/imports/files", {
      params: siteId === "" ? {} : { site_id: siteId },
    });
    setFiles(data);
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [siteId]);

  const upload = async () => {
    if (siteId === "" || !file) { setErr("请选择站点和文件"); return; }
    setErr(""); setMsg("");
    const fd = new FormData();
    fd.append("site_id", String(siteId));
    fd.append("source_type", sourceType);
    fd.append("file", file);
    try {
      const { data } = await api.post("/api/imports", fd);
      setMsg(`导入完成：${JSON.stringify(data.summary)}（文件 #${data.import_file_id}，共 ${data.row_count} 行）`);
      setFile(null);
      load();
    } catch (e) { setErr(errText(e)); }
  };

  const openRows = async (f: ImportFile) => {
    const { data } = await api.get<ImportRow[]>(`/api/imports/files/${f.id}/rows`);
    setRows(data);
  };

  return (
    <div>
      <h2 className="page-title">原件与导入（原始文件留痕，按来源记录号识别重复/修订）</h2>
      {canUpload && (
        <div className="panel">
          <div className="row">
            <div>
              <label>站点</label>
              <select value={siteId} onChange={(e) => setSiteId(e.target.value === "" ? "" : Number(e.target.value))}>
                <option value="">请选择</option>
                {sites.map((s) => <option key={s.id} value={s.id}>{s.code} {s.name}</option>)}
              </select>
            </div>
            <div>
              <label>来源类型</label>
              <select value={sourceType} onChange={(e) => setSourceType(e.target.value)}>
                {SOURCE_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
            <div><label>CSV 文件</label><input type="file" accept=".csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></div>
            <button onClick={upload}>上传导入</button>
          </div>
          <div className="muted" style={{ marginTop: 8 }}>
            同号同内容计 duplicate；同号内容变化且未被采用则生成新修订；已被账单采用的记录，更正进入 blocked_adopted 不覆盖原值。
          </div>
        </div>
      )}
      {msg && <div className="ok-msg">{msg}</div>}
      {err && <div className="error">{err}</div>}
      <div className="panel">
        <div className="filter-bar">
          <label>按站点筛选</label>
          <select value={siteId} onChange={(e) => setSiteId(e.target.value === "" ? "" : Number(e.target.value))}>
            <option value="">全部（按权限）</option>
            {sites.map((s) => <option key={s.id} value={s.id}>{s.code}</option>)}
          </select>
        </div>
        <table>
          <thead><tr><th>#</th><th>站点</th><th>类型</th><th>原始文件名</th><th>SHA256</th><th className="num">行数</th><th>导入人</th><th></th></tr></thead>
          <tbody>
            {files.map((f) => (
              <tr key={f.id}>
                <td>{f.id}</td>
                <td>{sites.find((s) => s.id === f.site_id)?.code ?? f.site_id}</td>
                <td>{f.source_type}</td>
                <td>{f.original_name}</td>
                <td className="mono muted">{f.sha256.slice(0, 16)}…</td>
                <td className="num">{f.row_count}</td>
                <td>{f.imported_by}</td>
                <td><button className="secondary" onClick={() => openRows(f)}>查看原始行</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows && (
        <div className="panel">
          <h2>逐行原件（物理行号 + 原始文本 + 处理结果）</h2>
          <table>
            <thead><tr><th className="num">行号</th><th>来源记录号</th><th>结果</th><th>备注</th><th>原始行</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="num">{r.line_no}</td>
                  <td className="mono">{r.source_record_no ?? "—"}</td>
                  <td><span className={`badge ${r.outcome}`}>{r.outcome}</span></td>
                  <td className="muted">{r.note ?? ""}</td>
                  <td className="mono" style={{ maxWidth: 420, overflowX: "auto" }}>{r.raw_line}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ marginTop: 10 }}><button className="secondary" onClick={() => setRows(null)}>关闭</button></div>
        </div>
      )}
    </div>
  );
}
