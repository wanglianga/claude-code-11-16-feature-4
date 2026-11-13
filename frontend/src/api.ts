import axios from "axios";

export const api = axios.create({ baseURL: "/" });

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem("token");
      if (!location.pathname.endsWith("/login")) location.href = "/login";
    }
    return Promise.reject(err);
  }
);

export function errText(e: unknown): string {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (e as any)?.response?.data?.detail ?? (e as Error).message ?? "请求失败";
}
