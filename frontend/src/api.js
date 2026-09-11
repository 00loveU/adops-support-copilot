export async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  const text = await response.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    const error = new Error(
      response.ok ? "服务响应格式异常，请稍后重试。" : `服务暂时不可用（HTTP ${response.status}）。`,
    );
    error.status = response.status;
    throw error;
  }
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("auth-expired"));
    const error = new Error(payload.error?.message || "请求失败，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return payload.data;
}
