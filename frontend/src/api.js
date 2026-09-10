export async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("auth-expired"));
    const error = new Error(payload.error?.message || "请求失败，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return payload.data;
}
