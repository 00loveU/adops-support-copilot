import { useEffect, useState } from "react";
import { api } from "./api.js";

const metricLabels = {
  impressions: "曝光量",
  clicks: "点击量",
  conversions: "转化量",
  spend_cents: "广告消耗",
  revenue_cents: "转化收入",
  ctr: "CTR",
  cvr: "CVR",
  cpc_cents: "CPC",
  cpa_cents: "CPA",
  roas: "ROAS",
};

function formatMetric(key, value) {
  if (value == null) return "不可计算";
  if (key === "ctr" || key === "cvr") return `${(value * 100).toFixed(2)}%`;
  if (key.endsWith("_cents")) return `¥${(value / 100).toFixed(2)}`;
  if (key === "roas") return `${Number(value).toFixed(2)}x`;
  return Number(value).toLocaleString("zh-CN");
}

function Login({ onLogin }) {
  const [username, setUsername] = useState("operator1");
  const [password, setPassword] = useState("Operator123!");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      const data = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      onLogin(data.user);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-intro">
        <div className="brand-mark">AO</div>
        <p className="eyebrow">ADOPS INTELLIGENCE</p>
        <h1>把投放数据，变成清晰的下一步。</h1>
        <p>查询关键指标、定位异常信号，并依据内部模拟 SOP 获得可追溯的排查建议。</p>
        <div className="intro-stats">
          <span><strong>7</strong> 类诊断规则</span>
          <span><strong>100%</strong> 来源可追溯</span>
        </div>
      </section>
      <form className="login-card" onSubmit={submit}>
        <p className="eyebrow">WELCOME BACK</p>
        <h2>登录工作台</h2>
        <p className="muted">使用内部账号继续</p>
        <label>用户名<input value={username} onChange={(e) => setUsername(e.target.value)} required /></label>
        <label>密码<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        {error && <p className="error-message" role="alert">{error}</p>}
        <button className="primary-button" disabled={loading}>{loading ? "登录中…" : "登录"}</button>
        <p className="demo-hint">演示账号已预填，也可使用管理员账号 admin1。</p>
      </form>
    </main>
  );
}

function MetricCards({ metrics }) {
  if (!metrics) return null;
  const entries = [
    ...Object.entries(metrics.raw || {}).filter(([key]) => key !== "currency"),
    ...Object.entries(metrics.calculated || {}),
  ];
  return (
    <section>
      <div className="section-heading"><h3>核心指标</h3><span>{entries.length} 项</span></div>
      <div className="metric-grid">
        {entries.map(([key, value]) => (
          <article className="metric-card" key={key}>
            <span>{metricLabels[key] || key}</span>
            <strong>{formatMetric(key, value)}</strong>
          </article>
        ))}
      </div>
      {metrics.unavailable_metrics?.map((item) => <p className="notice" key={item.metric}>{item.reason}</p>)}
    </section>
  );
}

function Diagnosis({ diagnosis }) {
  if (!diagnosis) return null;
  const sections = [
    ["已确认事实", diagnosis.facts],
    ["样本提醒", diagnosis.sample_warnings],
    ["可能原因", diagnosis.possible_causes],
    ["建议动作", diagnosis.suggestions],
  ];
  return (
    <section>
      <div className="section-heading"><h3>诊断结论</h3><span>{diagnosis.anomalies.length} 个异常</span></div>
      {diagnosis.anomalies.length > 0 && (
        <div className="anomaly-list">
          {diagnosis.anomalies.map((item) => (
            <article className="anomaly" key={item.rule_id}>
              <span className={`severity ${item.severity}`}>{item.severity === "high" ? "高" : "中"}</span>
              <div><strong>{item.name}</strong><small>{item.rule_id}</small></div>
            </article>
          ))}
        </div>
      )}
      <div className="diagnosis-grid">
        {sections.filter(([, items]) => items?.length).map(([title, items]) => (
          <article className="detail-card" key={title}><h4>{title}</h4><ul>{items.map((item) => <li key={item}>{item}</li>)}</ul></article>
        ))}
      </div>
    </section>
  );
}

function Sources({ sources }) {
  if (!sources?.length) return null;
  return (
    <section>
      <div className="section-heading"><h3>知识来源</h3><span>可追溯</span></div>
      <div className="source-list">
        {sources.map((source, index) => (
          <article key={`${source.source_code}-${source.section_title}-${index}`}>
            <code>{source.source_code}</code><strong>{source.title}</strong><span>{source.section_title}</span>
          </article>
        ))}
      </div>
    </section>
  );
}

function ResultPanel({ result, traceId, onFeedback }) {
  if (!result) return (
    <div className="empty-state"><span>✦</span><h3>等待你的问题</h3><p>选择计划并发起分析，或者直接询问指标口径与排查规则。</p></div>
  );
  if (result.status === "waiting_clarification") return null;
  return (
    <div className="result-content">
      {result.degraded && <p className="degraded-notice">大模型当前不可用，已返回确定性分析结果。</p>}
      <section className="answer-card"><p className="eyebrow">COPILOT ANSWER</p><p>{result.answer}</p></section>
      <MetricCards metrics={result.metrics} />
      <Diagnosis diagnosis={result.diagnosis} />
      <Sources sources={result.sources} />
      <section className="result-footer">
        <div><span>这次回答有帮助吗？</span><button onClick={() => onFeedback("helpful")}>有帮助</button><button onClick={() => onFeedback("not_helpful")}>没帮助</button></div>
        <code>TRACE · {traceId}</code>
      </section>
    </div>
  );
}

function Workspace({ showToast, conversationId, setConversationId }) {
  const [advertisers, setAdvertisers] = useState([]);
  const [campaigns, setCampaigns] = useState([]);
  const [conversations, setConversations] = useState([]);
  const [history, setHistory] = useState([]);
  const [advertiserId, setAdvertiserId] = useState("");
  const [campaignId, setCampaignId] = useState("");
  const [startDate, setStartDate] = useState("2026-09-08");
  const [endDate, setEndDate] = useState("2026-09-08");
  const [message, setMessage] = useState("诊断 CMP013 在 2026-09-08 为什么异常");
  const [result, setResult] = useState(null);
  const [traceId, setTraceId] = useState("");
  const [clarification, setClarification] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => { api("/api/advertisers?page_size=100").then((data) => setAdvertisers(data.items)); }, []);
  useEffect(() => {
    api("/api/conversations").then((data) => {
      setConversations(data.items);
      if (conversationId == null && data.items.length) setConversationId(data.items[0].id);
    }).catch((error) => showToast(error.message, true));
  }, []);
  useEffect(() => {
    if (!conversationId || conversationId === "new") {
      setHistory([]);
      setResult(null);
      setTraceId("");
      return;
    }
    loadConversation(conversationId);
  }, [conversationId]);
  useEffect(() => {
    setCampaignId("");
    if (!advertiserId) return setCampaigns([]);
    api(`/api/advertisers/${advertiserId}/campaigns?page_size=100`).then((data) => setCampaigns(data.items));
  }, [advertiserId]);

  async function loadConversation(id) {
    try {
      const data = await api(`/api/conversations/${id}`);
      setHistory(data.messages);
      setResult(data.latest_result);
      setTraceId(data.latest_trace_id || "");
    } catch (error) { showToast(error.message, true); }
  }

  async function refreshConversations() {
    const data = await api("/api/conversations");
    setConversations(data.items);
  }

  async function send(event) {
    event.preventDefault();
    setLoading(true);
    try {
      const payload = await api("/api/assistant/messages", {
        method: "POST",
        body: JSON.stringify({
          message,
          context: {
            advertiser_id: advertiserId ? Number(advertiserId) : null,
            campaign_id: campaignId ? Number(campaignId) : null,
            start_date: startDate || null,
            end_date: endDate || null,
          },
          conversation_id: conversationId && conversationId !== "new" ? conversationId : null,
        }),
      });
      setResult(payload);
      setConversationId(payload.conversation_id);
      const response = await fetchLatestTrace(payload.record_id);
      setTraceId(response);
      await refreshConversations();
      await loadConversation(payload.conversation_id);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      setLoading(false);
    }
  }

  async function fetchLatestTrace(recordId) {
    const record = await api(`/api/records/${recordId}`);
    return record.trace_id;
  }

  async function continueTask(event) {
    event.preventDefault();
    setLoading(true);
    try {
      const payload = await api(`/api/assistant/sessions/${result.session_id}/messages`, {
        method: "POST", body: JSON.stringify({ message: clarification }),
      });
      setResult(payload);
      setClarification("");
      setTraceId(await fetchLatestTrace(payload.record_id));
      await refreshConversations();
      await loadConversation(payload.conversation_id);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      setLoading(false);
    }
  }

  async function feedback(rating) {
    try {
      await api(`/api/records/${result.record_id}/feedback`, {
        method: "POST", body: JSON.stringify({ rating }),
      });
      showToast(rating === "helpful" ? "已记录：有帮助" : "已记录：没帮助");
    } catch (error) { showToast(error.message, true); }
  }

  return (
    <div className="workspace-grid">
      <section className="query-panel">
        <p className="eyebrow">NEW ANALYSIS</p><h2>发起投放分析</h2><p className="muted">筛选条件可选，缺少的信息会在对话中向你确认。</p>
        <div className="conversation-toolbar">
          <select
            value={conversationId || "new"}
            onChange={(event) => setConversationId(event.target.value)}
            aria-label="选择历史会话"
          >
            <option value="new">新会话</option>
            {conversations.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title} · {item.message_count} 条消息
              </option>
            ))}
          </select>
          <button type="button" onClick={() => setConversationId("new")}>＋ 新建</button>
        </div>
        {history.length > 0 && (
          <div className="chat-history">
            {history.map((item) => (
              <article className={item.role} key={item.id}>
                <span>{item.role === "user" ? "你" : "AI"}</span>
                <p>{item.content}</p>
              </article>
            ))}
          </div>
        )}
        <form onSubmit={send}>
          <div className="field-row">
            <label>广告主<select value={advertiserId} onChange={(e) => setAdvertiserId(e.target.value)}><option value="">不指定</option>{advertisers.map((item) => <option key={item.id} value={item.id}>{item.advertiser_code} · {item.name}</option>)}</select></label>
            <label>广告计划<select value={campaignId} onChange={(e) => setCampaignId(e.target.value)} disabled={!advertiserId}><option value="">不指定</option>{campaigns.map((item) => <option key={item.id} value={item.id}>{item.campaign_code} · {item.name}</option>)}</select></label>
          </div>
          <div className="field-row"><label>开始日期<input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} /></label><label>结束日期<input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} /></label></div>
          <label>你的问题<textarea rows="5" value={message} onChange={(e) => setMessage(e.target.value)} placeholder="例如：诊断 CMP013 在 2026-09-08 为什么异常" required /></label>
          <div className="examples"><span>试试：</span><button type="button" onClick={() => setMessage("CTR 是什么意思？")}>CTR 口径</button><button type="button" onClick={() => setMessage("广告没有曝光怎么排查？")}>无曝光排查</button></div>
          <button className="primary-button" disabled={loading}>{loading ? "分析中…" : "开始分析 →"}</button>
        </form>
        {result?.status === "waiting_clarification" && (
          <form className="clarification" onSubmit={continueTask}><strong>还需要一个信息</strong><p>{result.clarification_question}</p><div><input value={clarification} onChange={(e) => setClarification(e.target.value)} placeholder="在这里补充" required /><button disabled={loading}>继续</button></div></form>
        )}
      </section>
      <section className="result-panel"><ResultPanel result={result} traceId={traceId} onFeedback={feedback} /></section>
    </div>
  );
}

function Records({ showToast }) {
  const [records, setRecords] = useState([]);
  const [selected, setSelected] = useState(null);
  useEffect(() => { api("/api/records?page_size=100").then((data) => setRecords(data.items)).catch((e) => showToast(e.message, true)); }, []);
  async function openRecord(id) {
    try { setSelected(await api(`/api/records/${id}`)); } catch (error) { showToast(error.message, true); }
  }
  return (
    <section className="page-card">
      <div className="page-title"><div><p className="eyebrow">HISTORY</p><h2>我的处理记录</h2></div><span>{records.length} 条记录</span></div>
      <div className="records-layout">
        <div className="record-list">{records.length === 0 ? <p className="muted">暂无处理记录</p> : records.map((record) => <button key={record.id} className={selected?.id === record.id ? "active" : ""} onClick={() => openRecord(record.id)}><div><span className={`status-dot ${record.status}`}></span><strong>{record.original_query}</strong></div><small>{new Date(record.created_at).toLocaleString("zh-CN")} · {record.intent}</small><p>{record.answer_summary || "等待补充参数"}</p></button>)}</div>
        <div className="record-detail">{selected ? <><p className="eyebrow">RECORD #{selected.id}</p><h3>{selected.original_query}</h3><p>{selected.final_answer || "该任务尚未完成。"}</p><dl><div><dt>状态</dt><dd>{selected.status}</dd></div><div><dt>意图</dt><dd>{selected.intent}</dd></div><div><dt>反馈</dt><dd>{selected.feedback || "未反馈"}</dd></div><div><dt>耗时</dt><dd>{selected.total_latency_ms ?? "-"} ms</dd></div></dl><code>{selected.trace_id}</code></> : <div className="empty-state small"><span>↗</span><p>选择一条记录查看详情</p></div>}</div>
      </div>
    </section>
  );
}

function Knowledge({ showToast }) {
  const [sources, setSources] = useState([]);
  const [expanded, setExpanded] = useState(null);
  useEffect(() => { api("/api/admin/knowledge-sources?page_size=100").then((data) => setSources(data.items)).catch((e) => showToast(e.message, true)); }, []);
  return (
    <section className="page-card"><div className="page-title"><div><p className="eyebrow">KNOWLEDGE BASE</p><h2>知识来源</h2></div><span>只读 · {sources.length} 份</span></div><div className="knowledge-list">{sources.map((source) => <article key={source.id}><button onClick={() => setExpanded(expanded === source.id ? null : source.id)}><div><code>{source.source_code}</code><h3>{source.title}</h3><p>{source.category} · V{source.version}</p></div><span>{expanded === source.id ? "−" : "+"}</span></button>{expanded === source.id && <div className="chunks">{source.chunks.map((chunk) => <section key={chunk.id}><h4>{chunk.section_title}</h4><p>{chunk.content}</p></section>)}</div>}</article>)}</div></section>
  );
}

export default function App() {
  const [user, setUser] = useState(undefined);
  const [page, setPage] = useState("workspace");
  const [conversationId, setConversationId] = useState(null);
  const [toast, setToast] = useState(null);
  useEffect(() => { api("/api/auth/me").then((data) => setUser(data.user)).catch(() => setUser(null)); }, []);
  useEffect(() => {
    const expire = () => { setUser(null); setConversationId(null); };
    window.addEventListener("auth-expired", expire);
    return () => window.removeEventListener("auth-expired", expire);
  }, []);
  function showToast(message, isError = false) { setToast({ message, isError }); window.setTimeout(() => setToast(null), 2600); }
  async function logout() { await api("/api/auth/logout", { method: "POST" }); setConversationId(null); setUser(null); }
  if (user === undefined) return <div className="boot-screen">正在进入工作台…</div>;
  if (!user) return <Login onLogin={setUser} />;
  return (
    <div className="app-shell">
      <aside><div className="brand"><div className="brand-mark">AO</div><div><strong>AdOps</strong><span>Support Copilot</span></div></div><nav><button className={page === "workspace" ? "active" : ""} onClick={() => setPage("workspace")}><span>✦</span>智能分析</button><button className={page === "records" ? "active" : ""} onClick={() => setPage("records")}><span>◫</span>处理记录</button>{user.role === "admin" && <><p>管理员</p><button className={page === "knowledge" ? "active" : ""} onClick={() => setPage("knowledge")}><span>◇</span>知识来源</button></>}</nav><div className="user-block"><div>{user.display_name.slice(0, 1)}</div><p><strong>{user.display_name}</strong><span>{user.role === "admin" ? "系统管理员" : "广告运营"}</span></p><button onClick={logout} title="退出登录">↪</button></div></aside>
      <main className="app-main">
        <header><div><p className="eyebrow">OPERATIONS CENTER</p><h1>{page === "workspace" ? "广告诊断工作台" : page === "records" ? "处理记录" : "知识来源"}</h1></div><span className="system-status"><i></i>系统运行正常</span></header>
        <div hidden={page !== "workspace"}>
          <Workspace showToast={showToast} conversationId={conversationId} setConversationId={setConversationId} />
        </div>
        {page === "records" && <Records showToast={showToast} />}
        {page === "knowledge" && <Knowledge showToast={showToast} />}
      </main>
      {toast && <div className={`toast ${toast.isError ? "error" : ""}`} role="status">{toast.message}</div>}
    </div>
  );
}
