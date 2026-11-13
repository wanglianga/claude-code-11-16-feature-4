import { createContext, useContext, useState, ReactNode } from "react";
import { api } from "./api";

export type Role = "finance" | "operator" | "property";

interface AuthState {
  token: string;
  role: Role;
  displayName: string;
}

interface AuthCtx extends AuthState {
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState | null>(() => {
    const raw = localStorage.getItem("auth");
    return raw ? (JSON.parse(raw) as AuthState) : null;
  });

  const login = async (username: string, password: string) => {
    const { data } = await api.post("/api/auth/login", { username, password });
    const next: AuthState = {
      token: data.access_token,
      role: data.role,
      displayName: data.display_name,
    };
    localStorage.setItem("token", next.token);
    localStorage.setItem("auth", JSON.stringify(next));
    setState(next);
  };

  const logout = () => {
    localStorage.removeItem("token");
    localStorage.removeItem("auth");
    setState(null);
  };

  return (
    <Ctx.Provider value={{ ...(state as AuthState), login, logout }}>
      {children}
    </Ctx.Provider>
  );
}

export function useAuth(): AuthCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}
