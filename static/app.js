const state = { bases: [], activeConversationId: null, selectedPages: [], aborter: null, lastAssistantId: null };
const $ = (id) => document.getElementById(id);

function runtime() {
  return {
    public_mcp_url: $("publicMcpUrl").value.trim(),
    admin_mcp_url: $("adminMcpUrl").value.trim(),
    llm_base_url: $("llmBaseUrl").value.trim(),
    llm_api_key: $("llmApiKey").value.trim(),
    llm_model: $("llmModel").value.trim(),
    pass_model_to_mcp: $("passModelToMcp").checked,
  };
}

function saveRuntime() { sessionStorage.setItem("validator-runtime", JSON.stringify(runtime())); }
function restoreRuntime() {
  try {
    const value = JSON.parse(sessionStorage.getItem("validator-runtime") || "{}");
    Object.entries({ publicMcpUrl: "public_mcp_url", adminMcpUrl: "admin_mcp_url", llmBaseUrl: "llm_base_url", llmApiKey: "llm_api_key", llmModel: "llm_model" }).forEach(([id, key]) => { if (value[key]) $(id).value = value[key]; });
    if (typeof value.pass_model_to_mcp === "boolean") $("passModelToMcp").checked = value.pass_model_to_mcp;
  } catch (_) { /* ignore malformed local session config */ }
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { detail: text }; }
  if (!response.ok) throw new Error(data.detail || data.message || `请求失败：${response.status}`);
  return data;
}
function post(url, body) { return request(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
function esc(value) { return String(value ?? "").replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]); }
function selectedBase() { return $("manageBase").value; }
function checkedChatBases() { return [...document.querySelectorAll("#chatBases input:checked")].map((input) => input.value); }
function selectedPageReferences() { return [...document.querySelectorAll("#chatWikiChoices input:checked")].map((input) => JSON.parse(input.dataset.page)); }
function notify(target, error) { $(target).textContent = error instanceof Error ? error.message : String(error); }

async function loadBases() {
  const result = await post("/api/manage/bases", { runtime: runtime() });
  state.bases = result.knowledge_bases || [];
  const current = selectedBase();
  const selectedChatBases = new Set(checkedChatBases());
  $("manageBase").innerHTML = state.bases.map((base) => `<option value="${esc(base.name)}">${esc(base.name)}（${base.page_count} 页）</option>`).join("");
  if (state.bases.some((base) => base.name === current)) $("manageBase").value = current;
  $("chatBases").innerHTML = state.bases.map((base) => `<label><input type="checkbox" value="${esc(base.name)}" ${!selectedChatBases.size || selectedChatBases.has(base.name) ? "checked" : ""}>${esc(base.name)}</label>`).join("");
  $("baseStatus").textContent = `已加载 ${state.bases.length} 个知识库。`;
  await Promise.all([loadDocuments(), loadWiki(), loadConversations()]);
}

async function createBase() {
  const name = $("newBaseName").value.trim();
  if (!name) return notify("baseStatus", "请输入知识库名称。");
  const result = await post("/api/manage/bases/create", { runtime: runtime(), name, description: $("newBaseDescription").value.trim() });
  $("baseStatus").textContent = result.knowledge_base ? `已创建：${result.knowledge_base.name}` : "创建完成。";
  $("newBaseName").value = ""; $("newBaseDescription").value = "";
  await loadBases();
}

async function loadDocuments() {
  const base = selectedBase();
  if (!base) return;
  const result = await post("/api/manage/documents", { runtime: runtime(), knowledge_base_name: base });
  const docs = result.documents || [];
  $("documentsBody").innerHTML = docs.map((doc) => `<tr><td>${esc(doc.filename)}</td><td>${esc(doc.parser_name)}</td><td>${esc(doc.status)}</td><td>${esc(doc.compilation_state || "-")}</td><td><button class="secondary view-doc" data-file="${esc(doc.filename)}">查看解析</button><button class="secondary delete-doc" data-file="${esc(doc.filename)}">删除</button></td></tr>`).join("") || '<tr><td colspan="5" class="hint">暂无文档。</td></tr>';
}

async function showDocument(filename) {
  const result = await post("/api/manage/documents/markdown", { runtime: runtime(), knowledge_base_name: selectedBase(), filename, max_chars: 30000 });
  $("compileLog").textContent = `解析内容：${filename}\n\n${result.markdown || "（无可用 Markdown）"}${result.truncated ? "\n\n[已截断]" : ""}`;
}

async function deleteDocument(filename) {
  if (!confirm(`确定删除《${filename}》？`)) return;
  const result = await post("/api/manage/documents/delete", { runtime: runtime(), knowledge_base_name: selectedBase(), filenames: [filename] });
  $("compileLog").textContent = JSON.stringify(result, null, 2);
  await Promise.all([loadDocuments(), loadWiki(), loadBases()]);
}

async function uploadDocuments() {
  const files = [...$("uploadFiles").files];
  if (!selectedBase()) return notify("uploadResult", "请先选择知识库。");
  if (!files.length) return notify("uploadResult", "请选择至少一个文件。");
  const form = new FormData();
  form.append("knowledge_base_name", selectedBase());
  form.append("runtime_json", JSON.stringify(runtime()));
  form.append("compile_enabled", "true");
  files.forEach((file) => form.append("files", file));
  $("uploadResult").textContent = "正在上传并调用管理员 MCP...";
  try {
    const result = await request("/api/manage/documents/upload", { method: "POST", body: form });
    $("uploadResult").textContent = JSON.stringify(result, null, 2);
    $("uploadFiles").value = "";
    await Promise.all([loadDocuments(), loadBases()]);
  } catch (error) { notify("uploadResult", error); }
}

async function startCompile(retry = false) {
  const base = selectedBase();
  if (!base) return notify("compileLog", "请先选择知识库。");
  try {
    const result = await post("/api/manage/compile", { runtime: runtime(), knowledge_base_name: base, filenames: [], retry_failed: retry, candidate_guidance: "", max_candidates: null });
    $("compileLog").textContent = JSON.stringify(result, null, 2);
    if (result.task_id) await pollTask(result.task_id);
  } catch (error) { notify("compileLog", error); }
}

async function pollTask(taskId) {
  for (;;) {
    const status = await post("/api/manage/jobs/status", { runtime: runtime(), task_id: taskId });
    const lines = (status.events || []).map((item) => `${item.at || ""}  ${item.label || item.node || ""}`).join("\n");
    $("compileLog").textContent = `任务 ${taskId}\n状态：${status.state}\n\n${lines}\n\n${status.result ? JSON.stringify(status.result, null, 2) : ""}`;
    if (status.state !== "running") {
      await Promise.all([loadDocuments(), loadWiki(), loadBases()]);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

async function loadWiki() {
  const base = selectedBase();
  if (!base) return;
  const type = $("wikiType").value;
  const result = await post("/api/manage/wiki", { runtime: runtime(), knowledge_base_names: [base], page_types: type ? [type] : [], limit: 100, offset: 0 });
  $("wikiList").innerHTML = (result.pages || []).map((page) => `<div class="wiki-item" data-page="${esc(JSON.stringify(page))}"><strong>${esc(page.title)}</strong><small>${esc(page.page_type)} · ${esc(page.summary)}</small></div>`).join("") || '<p class="hint">暂无 Wiki 页面。</p>';
}

async function showWiki(page) {
  const result = await post("/api/manage/wiki/read", { runtime: runtime(), pages: [{ knowledge_base_name: page.knowledge_base_name, title: page.title, page_type: page.page_type }], max_content_chars: 12000, include_tables: true });
  const item = (result.pages || [])[0];
  $("wikiDetail").innerHTML = item ? `<h2>${esc(item.title)}</h2><p class="hint">${esc(item.summary)}</p><pre>${esc(item.content_markdown)}</pre>${(item.tables || []).map((table) => `<h3>${esc(table.caption)}</h3><pre>${esc(table.content_markdown)}</pre>`).join("")}` : `<p class="hint">未找到页面。</p>`;
}

async function loadConversations() {
  const conversations = await request("/api/chat/conversations");
  $("conversationList").innerHTML = conversations.map((item) => `<div class="conversation-item ${item.id === state.activeConversationId ? "active" : ""}" data-conversation="${item.id}">${esc(item.title)}<small>${item.message_count} 条消息</small></div>`).join("") || '<p class="hint">新建对话开始验证。</p>';
  if (!state.activeConversationId && conversations[0]) await openConversation(conversations[0].id);
}

async function newConversation() {
  const item = await post("/api/chat/conversations", { runtime: runtime(), knowledge_base_names: checkedChatBases() });
  state.activeConversationId = item.id;
  await loadConversations();
  renderConversation(item);
}

async function openConversation(id) {
  state.activeConversationId = id;
  const item = await request(`/api/chat/conversations/${id}`);
  renderConversation(item);
  await loadConversations();
}

function renderConversation(conversation) {
  state.lastAssistantId = null;
  $("chatMessages").innerHTML = "";
  (conversation.messages || []).forEach((message) => appendMessage(message));
}

function appendMessage(message) {
  const node = document.createElement("article");
  node.className = `message ${message.role}`;
  node.dataset.messageId = message.id || "";
  const sourceText = (message.source_pages || []).map((page) => page.title).join(" · ");
  node.innerHTML = `<div class="message-content">${esc(message.content || (message.state === "streaming" ? "正在生成…" : ""))}</div>${sourceText ? `<div class="sources">来源：${esc(sourceText)}</div>` : ""}${message.role === "assistant" && message.state === "completed" ? `<button class="secondary regenerate" data-assistant="${esc(message.id)}">重新生成</button>` : ""}${message.error_message ? `<div class="hint">${esc(message.error_message)}</div>` : ""}`;
  $("chatMessages").append(node);
  $("chatMessages").scrollTop = $("chatMessages").scrollHeight;
  if (message.role === "assistant") state.lastAssistantId = message.id;
  return node;
}

function reuseAssistantMessage(messageId, warnings = []) {
  const node = [...document.querySelectorAll(".message.assistant")].find((item) => item.dataset.messageId === messageId);
  if (!node) return appendMessage({ id: messageId, role: "assistant", content: "", state: "streaming" });
  node.innerHTML = `<div class="message-content">${esc(warnings.length ? warnings.join("\n") : "正在生成…")}</div>`;
  state.lastAssistantId = messageId;
  return node;
}

async function loadChatWikiChoices() {
  const bases = checkedChatBases();
  if (!bases.length) return notify("chatWikiChoices", "请至少选择一个知识库。");
  const types = ["concept", "entity"];
  if ($("chatIncludeQuery").checked) types.push("query");
  const result = await post("/api/manage/wiki", { runtime: runtime(), knowledge_base_names: bases, page_types: types, limit: 100, offset: 0 });
  $("chatWikiChoices").innerHTML = (result.pages || []).map((page) => `<label class="wiki-chip"><input type="checkbox" data-page="${esc(JSON.stringify({ knowledge_base_name: page.knowledge_base_name, title: page.title, page_type: page.page_type }))}">${esc(page.title)}</label>`).join("") || '<span class="hint">没有可选 Wiki；发送问题时仍会自动检索。</span>';
}

async function sendQuestion() {
  const question = $("questionInput").value.trim();
  if (!question) return;
  if (!state.activeConversationId) await newConversation();
  const payload = { runtime: runtime(), conversation_id: state.activeConversationId, question, knowledge_base_names: checkedChatBases(), selected_pages: selectedPageReferences(), auto_retrieve: true, include_query_pages: $("chatIncludeQuery").checked, include_tables: $("chatIncludeTables").checked, persist_question: true };
  $("questionInput").value = "";
  await runStream("/api/chat/stream", payload, question);
}

async function regenerate(assistantId) {
  if (!state.activeConversationId) return;
  await runStream("/api/chat/regenerate", { runtime: runtime(), conversation_id: state.activeConversationId, assistant_message_id: assistantId, include_query_pages: $("chatIncludeQuery").checked, include_tables: $("chatIncludeTables").checked, persist_question: true }, null);
}

async function runStream(url, payload, optimisticQuestion) {
  if (state.aborter) return;
  const aborter = new AbortController(); state.aborter = aborter;
  $("sendQuestion").disabled = true; $("stopChat").disabled = false;
  if (optimisticQuestion) appendMessage({ role: "user", content: optimisticQuestion, state: "completed" });
  let assistantNode = null;
  try {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: aborter.signal });
    if (!response.ok || !response.body) throw new Error(await response.text());
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n"); buffer = blocks.pop();
      for (const block of blocks) {
        const type = (block.match(/^event: (.+)$/m) || [])[1] || "message";
        const raw = (block.match(/^data: (.+)$/m) || [])[1]; if (!raw) continue;
        const data = JSON.parse(raw);
        if (type === "meta") {
          state.activeConversationId = data.conversation_id;
          assistantNode = reuseAssistantMessage(data.assistant_message_id, data.warnings || []);
        } else if (type === "delta" && assistantNode) {
          const content = assistantNode.querySelector(".message-content");
          if (content.textContent === "正在生成…" || content.textContent.includes("未找到相关 Wiki")) content.textContent = "";
          content.textContent += data.text;
          $("chatMessages").scrollTop = $("chatMessages").scrollHeight;
        } else if (type === "completed") {
          await openConversation(state.activeConversationId);
        } else if (type === "error") {
          if (assistantNode) assistantNode.querySelector(".message-content").textContent += `\n\n错误：${data.message}`;
          else alert(data.message);
        }
      }
    }
  } catch (error) {
    if (error.name !== "AbortError") alert(`对话请求失败：${error.message}`);
  } finally {
    state.aborter = null; $("sendQuestion").disabled = false; $("stopChat").disabled = true;
    await loadConversations();
  }
}

function bindEvents() {
  $("connectionToggle").onclick = () => $("connectionPanel").classList.toggle("hidden");
  ["publicMcpUrl", "adminMcpUrl", "llmBaseUrl", "llmApiKey", "llmModel", "passModelToMcp"].forEach((id) => $(id).addEventListener("change", saveRuntime));
  $("testConnection").onclick = async () => { try { const result = await post("/api/connections/test", { runtime: runtime() }); await loadBases(); $("connectionStatus").textContent = result.message || "两个 MCP 连接正常。"; } catch (error) { notify("connectionStatus", error); } };
  document.querySelectorAll(".tabs button").forEach((button) => button.onclick = () => { document.querySelectorAll(".tabs button").forEach((item) => item.classList.toggle("active", item === button)); $("manageTab").classList.toggle("hidden", button.dataset.tab !== "manage"); $("chatTab").classList.toggle("hidden", button.dataset.tab !== "chat"); });
  $("refreshBases").onclick = () => loadBases().catch((error) => notify("baseStatus", error)); $("createBase").onclick = () => createBase().catch((error) => notify("baseStatus", error)); $("manageBase").onchange = () => Promise.all([loadDocuments(), loadWiki()]);
  $("refreshDocuments").onclick = () => loadDocuments().catch((error) => notify("compileLog", error)); $("uploadButton").onclick = uploadDocuments; $("compileButton").onclick = () => startCompile(false); $("retryButton").onclick = () => startCompile(true); $("refreshWiki").onclick = () => loadWiki().catch((error) => notify("wikiDetail", error));
  $("documentsBody").onclick = (event) => { const button = event.target.closest("button"); if (!button) return; if (button.classList.contains("view-doc")) showDocument(button.dataset.file).catch((error) => notify("compileLog", error)); if (button.classList.contains("delete-doc")) deleteDocument(button.dataset.file).catch((error) => notify("compileLog", error)); };
  $("wikiList").onclick = (event) => { const node = event.target.closest(".wiki-item"); if (node) showWiki(JSON.parse(node.dataset.page)).catch((error) => notify("wikiDetail", error)); };
  $("newConversation").onclick = () => newConversation().catch((error) => alert(error.message)); $("conversationList").onclick = (event) => { const node = event.target.closest(".conversation-item"); if (node) openConversation(node.dataset.conversation).catch((error) => alert(error.message)); };
  $("chatSearchWiki").onclick = () => loadChatWikiChoices().catch((error) => notify("chatWikiChoices", error)); $("sendQuestion").onclick = sendQuestion; $("questionInput").onkeydown = (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendQuestion(); } };
  $("stopChat").onclick = () => { if (state.activeConversationId && state.lastAssistantId) post("/api/chat/stop", { runtime: runtime(), conversation_id: state.activeConversationId, assistant_message_id: state.lastAssistantId, include_query_pages: $("chatIncludeQuery").checked, include_tables: $("chatIncludeTables").checked, persist_question: false }).catch(() => {}); if (state.aborter) state.aborter.abort(); };
  $("chatMessages").onclick = (event) => { const button = event.target.closest(".regenerate"); if (button) regenerate(button.dataset.assistant); };
}

document.addEventListener("DOMContentLoaded", async () => { restoreRuntime(); bindEvents(); try { await loadBases(); } catch (error) { notify("baseStatus", error); $("connectionPanel").classList.remove("hidden"); } });
