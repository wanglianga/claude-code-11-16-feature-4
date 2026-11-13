import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import LoginPage from "./pages/LoginPage";
import BillsPage from "./pages/BillsPage";
import JobsPage from "./pages/JobsPage";
import ImportsPage from "./pages/ImportsPage";
import TasksPage from "./pages/TasksPage";
import ConflictsPage from "./pages/ConflictsPage";
import EntitiesPage from "./pages/EntitiesPage";
import BackfillPage from "./pages/BackfillPage";

const ROLE_LABEL: Record<string, string> = {
  finance: "平台财务",
  operator: "运营",
  property: "物业",
};

function Shell() {
  const { role, displayName, logout } = useAuth();
  const nav = [
    { to: "/bills", label: "账单与下钻", roles: ["finance", "operator", "property"] },
    { to: "/backfill", label: "跨月回补", roles: ["finance", "operator", "property"] },
    { to: "/jobs", label: "核算作业", roles: ["finance", "operator", "property"] },
    { to: "/imports", label: "原件与导入", roles: ["finance", "operator", "property"] },
    { to: "/tasks", label: "核对事项", roles: ["finance", "operator", "property"] },
    { to: "/conflicts", label: "订单冲突", roles: ["finance", "operator"] },
    { to: "/entities", label: "读数/订单查询", roles: ["finance", "operator", "property"] },
  ].filter((n) => n.roles.includes(role));

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>充电站核算平台</h1>
        <div className="sub">总表 · 设备 · 订单 · 分时电价</div>
        <nav>
          {nav.map((n) => (
            <NavLink key={n.to} to={n.to}
              className={({ isActive }) => (isActive ? "active" : "")}>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="userbox">
          {displayName}（{ROLE_LABEL[role]}）
          <br />
          <button className="secondary" onClick={logout}>退出登录</button>
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/bills" element={<BillsPage />} />
          <Route path="/backfill" element={<BackfillPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/imports" element={<ImportsPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/conflicts" element={<ConflictsPage />} />
          <Route path="/entities" element={<EntitiesPage />} />
          <Route path="*" element={<Navigate to="/bills" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const { token } = useAuth();
  if (!token) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }
  return <Shell />;
}
