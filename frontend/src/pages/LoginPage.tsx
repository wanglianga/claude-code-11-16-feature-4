import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { errText } from "../api";

const DEMO = [
  ["finance", "Finance@123", "平台财务（全部站点、确认账单）"],
  ["operator", "Operator@123", "运营（冲突/换表/重复订单）"],
  ["property_sh", "Property@123", "上海物业（仅 S01 核对事项）"],
  ["property_xj", "Property@123", "乌鲁木齐物业（仅 S02 核对事项）"],
];

export default function LoginPage() {
  const { login } = useAuth();
  const nav = useNavigate();
  const [username, setUsername] = useState("finance");
  const [password, setPassword] = useState("Finance@123");
  const [error, setError] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await login(username, password);
      nav("/bills");
    } catch (e) {
      setError(errText(e));
    }
  };

  return (
    <div className="login-wrap">
      <div className="panel login-card">
        <h2>充电站月度核算平台</h2>
        <form onSubmit={submit}>
          <div className="field">
            <label>用户名</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} style={{ width: "100%" }} />
          </div>
          <div className="field">
            <label>密码</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
              style={{ width: "100%" }} />
          </div>
          {error && <div className="error">{error}</div>}
          <button style={{ width: "100%" }}>登录</button>
        </form>
        <div className="muted" style={{ marginTop: 14 }}>
          {DEMO.map(([u, p, desc]) => (
            <div key={u} style={{ marginBottom: 4 }}>
              <a href="#" onClick={(e) => { e.preventDefault(); setUsername(u); setPassword(p); }}>
                {u}
              </a>
              ／{p} — {desc}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
