// 前端唯一状态源：REST 返回的数据先写入 state，再由 renderXxx() 更新 DOM。
// 不把后端文件内容整体缓存到浏览器，只保存 task id 和选中的相对路径。
const state = {
  taskId: null,
  task: null,
  tree: null,
  timer: null,
  selectedPath: null,
  selectedName: null,
  treeSignature: null,
  chatBusy: false,
  chatLoaded: false,
  announced: new Set(),
  tasks: [],
  draftOpen: false,
  collapsedTasks: new Set(),
  sessionDialog: null,
};

/** 查询页面中的第一个匹配元素。 */
const $ = (selector) => document.querySelector(selector);
/** 创建 DOM 元素，并按需设置类名与纯文本。 */
const create = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const MARKDOWN_TAGS = new Set([
  'A', 'B', 'BLOCKQUOTE', 'BR', 'CODE', 'DEL', 'EM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
  'HR', 'I', 'LI', 'OL', 'P', 'PRE', 'S', 'STRONG', 'TABLE', 'TBODY', 'TD', 'TH', 'THEAD',
  'TR', 'UL',
]);
const MARKDOWN_DROP_TAGS = new Set(['EMBED', 'IFRAME', 'MATH', 'OBJECT', 'SCRIPT', 'STYLE', 'SVG']);

// 模型回复是不可信文本：Marked 只负责解析，下面两步白名单过滤才负责安全。
/** 只允许 Markdown 链接使用 http、https 或 mailto 协议。 */
function safeMarkdownHref(value) {
  const href = String(value || '').trim();
  if (!href) return null;
  try {
    const target = new URL(href, location.href);
    return ['http:', 'https:', 'mailto:'].includes(target.protocol) ? target : null;
  } catch (_error) {
    return null;
  }
}

/** 递归清理模型生成的 Markdown DOM，仅保留标签和属性白名单。 */
function sanitizeMarkdownTree(parent) {
  [...parent.childNodes].forEach((node) => {
    if (node.nodeType === Node.COMMENT_NODE) {
      node.remove();
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;

    const tag = node.tagName;
    if (MARKDOWN_DROP_TAGS.has(tag)) {
      node.remove();
      return;
    }
    if (!MARKDOWN_TAGS.has(tag)) {
      node.replaceWith(document.createTextNode(node.textContent || ''));
      return;
    }

    const href = tag === 'A' ? safeMarkdownHref(node.getAttribute('href')) : null;
    const title = tag === 'A' ? node.getAttribute('title') : null;
    const start = tag === 'OL' ? node.getAttribute('start') : null;
    const align = ['TD', 'TH'].includes(tag) ? node.getAttribute('align') : null;
    const codeClass = tag === 'CODE' ? node.getAttribute('class') : null;
    [...node.attributes].forEach((attribute) => node.removeAttribute(attribute.name));

    if (href) {
      node.setAttribute('href', href.href);
      if (title) node.setAttribute('title', title);
      if (href.protocol === 'http:' || href.protocol === 'https:') {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer nofollow');
      }
    }
    if (tag === 'OL' && /^-?\d+$/.test(start || '')) node.setAttribute('start', start);
    if (['left', 'center', 'right'].includes(String(align).toLowerCase())) node.setAttribute('align', String(align).toLowerCase());
    if (tag === 'CODE' && /^language-[\w-]+$/.test(codeClass || '')) node.setAttribute('class', codeClass);
    sanitizeMarkdownTree(node);
  });
}

/** 将解析后仍残留的 **文本** 标记安全转换为 strong 元素。 */
function promoteLiteralStrong(parent) {
  const walker = document.createTreeWalker(parent, NodeFilter.SHOW_TEXT);
  const candidates = [];
  while (walker.nextNode()) candidates.push(walker.currentNode);
  candidates.forEach((textNode) => {
    if (textNode.parentElement?.closest('code, pre, strong, a')) return;
    const source = textNode.nodeValue || '';
    const marker = /\*\*(?=\S)([^*\n]*?\S)\*\*/g;
    if (!marker.test(source)) return;
    marker.lastIndex = 0;
    const fragment = document.createDocumentFragment();
    let offset = 0;
    let match;
    while ((match = marker.exec(source)) !== null) {
      fragment.append(document.createTextNode(source.slice(offset, match.index)));
      const strong = document.createElement('strong');
      strong.textContent = match[1];
      fragment.append(strong);
      offset = marker.lastIndex;
    }
    fragment.append(document.createTextNode(source.slice(offset)));
    textNode.replaceWith(fragment);
  });
}

/** 将 Agent Markdown 回复解析、清洗后渲染到聊天气泡。 */
function renderMarkdown(bubble, content) {
  // 先在 inert template 中解析和清洗，最后一次性挂入真实页面。
  bubble.classList.add('markdown-body');
  if (!window.marked?.parse) {
    bubble.textContent = content;
    return;
  }
  try {
    const template = document.createElement('template');
    template.innerHTML = window.marked.parse(String(content || ''), {gfm: true, breaks: true});
    sanitizeMarkdownTree(template.content);
    promoteLiteralStrong(template.content);
    template.content.querySelectorAll('table').forEach((table) => {
      const wrapper = create('div', 'markdown-table-wrap');
      table.replaceWith(wrapper);
      wrapper.append(table);
    });
    bubble.append(template.content);
  } catch (_error) {
    bubble.textContent = content;
  }
}

/** 统一调用后端 REST API，并把非成功响应转换为 Error。 */
async function api(path, options = {}) {
  // 所有 REST 请求统一从这里处理错误，调用方只处理业务成功数据或 Error。
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

/** 更新页面左下角的后端连接状态。 */
function setConnection(online) {
  const node = $('#connection');
  node.classList.toggle('online', online);
  node.lastElementChild.textContent = online ? '服务已连接' : '服务不可用';
}

/** 将后端任务状态代码转换为中文显示文本。 */
function statusText(status) {
  return ({idle: '可聊天', ready: '待指令', queued: '排队中', running: '运行中', completed: '已完成', blocked: '等待配置', failed: '执行失败'})[status] || status;
}

/** 将流水线阶段状态代码转换为中文显示文本。 */
function stageStatusText(status) {
  return ({pending: '等待', running: '进行中', completed: '完成', blocked: '阻塞', failed: '失败'})[status] || status;
}

/** 向聊天区追加用户或 Agent 消息，并滚动到最新内容。 */
function appendMessage(role, content, extraClass = '') {
  const article = create('article', `message ${role} ${extraClass}`.trim());
  const bubble = create('div', 'bubble');
  if (role === 'assistant') renderMarkdown(bubble, content);
  else bubble.textContent = content;
  article.append(bubble);
  $('#chat-messages').append(article);
  $('#chat-messages').scrollTop = $('#chat-messages').scrollHeight;
  return article;
}

/** 清空聊天区并恢复欢迎语及指定的历史消息。 */
function resetConversation(messages = [], idle = false) {
  $('#chat-messages').replaceChildren();
  const greeting = idle
    ? '你好，可以直接和我聊天。需要处理接口文档时，再点击聊天框左下角的“＋”附加 PDF。'
    : '我会跟踪四个处理阶段。你可以让我解释当前接口、检查边界覆盖，或分析失败用例。';
  appendMessage('assistant', greeting);
  messages.forEach((message) => appendMessage(message.role === 'user' ? 'user' : 'assistant', message.content || ''));
}

/** 返回尚未附加 PDF 时使用的空四阶段进度模型。 */
function emptyProgress() {
  return {
    headline: '可以直接聊天',
    overall_percent: 0,
    detail: 'PDF 是可选附件；上传后也会等待你的执行指令',
    stages: [
      {label: 'PDF 解析', completed: 0, total: 1, status: 'pending'},
      {label: '接口切分', completed: 0, total: 0, status: 'pending'},
      {label: '用例生成', completed: 0, total: 0, status: 'pending'},
      {label: '自动化测试', completed: 0, total: 0, status: 'pending'},
    ],
  };
}

/** 切换到新的未持久化聊天草稿，并重置当前任务 UI 状态。 */
function resetWorkspace() {
  if (state.timer) clearInterval(state.timer);
  state.taskId = null;
  state.task = null;
  state.tree = null;
  state.timer = null;
  state.selectedPath = null;
  state.selectedName = null;
  state.treeSignature = null;
  state.chatLoaded = true;
  state.announced = new Set();
  state.draftOpen = true;
  history.replaceState(null, '', location.pathname);
  $('#top-task-name').textContent = '新任务';
  const pill = $('#top-task-status');
  pill.textContent = '可聊天';
  pill.className = 'status-pill idle';
  $('#report-link').classList.add('hidden');
  $('#selected-context').classList.add('hidden');
  $('#chat-suggestions').classList.remove('hidden');
  $('#workspace-location').textContent = '文件保存在后端任务目录；浏览器只传任务 ID 和相对路径。';
  $('#workspace-location').removeAttribute('title');
  renderProgress(emptyProgress());
  resetConversation([], true);
  clearAttachment();
  renderTaskSidebar();
}

/** 将新的阶段状态变化追加为一次性聊天通知。 */
function announceTaskChanges(task) {
  if (!state.chatLoaded) return;
  task.stages.forEach((stage) => {
    const key = `${stage.id}:${stage.status}`;
    if (state.announced.has(key) || !['running', 'completed', 'failed', 'blocked'].includes(stage.status)) return;
    state.announced.add(key);
    if (stage.status === 'running') appendMessage('assistant', `正在执行「${stage.label}」。${task.progress.detail}`);
    if (stage.status === 'completed') appendMessage('assistant', `子任务「${stage.label}」已完成。`);
    if (stage.status === 'failed' || stage.status === 'blocked') appendMessage('assistant', `子任务「${stage.label}」${stageStatusText(stage.status)}，请查看任务状态。`);
  });
  if (task.error) {
    const key = `error:${task.error}`;
    if (!state.announced.has(key)) {
      state.announced.add(key);
      appendMessage('assistant', `任务错误：${task.error}`);
    }
  }
}

/** 渲染总进度条及四个子阶段的完成数量。 */
function renderProgress(progress) {
  $('#progress-headline').textContent = progress.headline;
  $('#progress-percent').textContent = `${progress.overall_percent}%`;
  $('#progress-bar').style.width = `${progress.overall_percent}%`;
  $('#progress-detail').textContent = progress.detail;
  const target = $('#stage-list');
  target.replaceChildren();
  progress.stages.forEach((stage) => {
    const item = create('li', stage.status);
    item.append(create('b', '', stage.label));
    item.append(create('small', '', `${stage.completed}/${stage.total} · ${stageStatusText(stage.status)}`));
    target.append(item);
  });
}

/** 用后端任务快照更新顶部状态、进度与侧边栏。 */
function renderTask(task) {
  // task 是 GET /api/tasks/{id} 的完整快照；不要在此自行推导后端状态。
  const previous = state.tasks.find((item) => item.id === task.id);
  const taskListChanged = !previous || previous.status !== task.status || previous.filename !== task.filename;
  const index = state.tasks.findIndex((item) => item.id === task.id);
  if (index >= 0) state.tasks[index] = task;
  else state.tasks.unshift(task);
  state.task = task;
  $('#top-task-name').textContent = task.title || task.filename;
  const pill = $('#top-task-status');
  pill.textContent = statusText(task.status);
  pill.className = `status-pill ${task.status}`;
  renderProgress(task.progress);
  announceTaskChanges(task);
  if (taskListChanged) renderTaskSidebar();
}

/** 返回各类任务文件在树中的紧凑图标文本。 */
function fileIcon(kind) {
  return ({markdown: 'MD', cases: '{}', excel: 'XLS', results: '✓', image: '▧', report: '◎'})[kind] || '·';
}

/** 生成人数、条数或文件大小等文件树辅助信息。 */
function fileDetail(file) {
  if (!file.available) return '等待';
  if (file.kind === 'cases' && Number.isInteger(file.count)) return `${file.count} 条`;
  if (file.kind === 'results' && file.summary) return `${file.summary.total ?? 0} 条`;
  if (file.kind === 'excel') return `${Math.max(1, Math.round((file.size || 0) / 1024))}K`;
  if (file.kind === 'image') return `${Math.max(1, Math.round((file.size || 0) / 1024))}K`;
  return '';
}

/** 将一个可用文件设为下一条 Agent 消息的选中上下文。 */
function selectFile(file, button) {
  if (!file.available) return;
  state.selectedPath = file.path;
  state.selectedName = file.name;
  document.querySelectorAll('.file-button').forEach((node) => node.classList.remove('active'));
  if (button) button.classList.add('active');
  $('#selected-context-name').textContent = file.name;
  $('#selected-context').classList.remove('hidden');
}

/** 请求后端使用本机默认应用打开选中的任务文件。 */
async function openLocalFile(file) {
  if (!file.available || !state.taskId) return;
  try {
    await api(`/api/tasks/${state.taskId}/files/open`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: file.path}),
    });
    appendMessage('assistant', `已使用本机默认程序打开：${file.name}`);
  } catch (error) {
    appendMessage('assistant', `无法打开 ${file.name}：${error.message}`);
  }
}

/** 为一个任务产物创建支持选择和双击打开的文件按钮。 */
function makeFileButton(file) {
  const button = create('button', `file-button ${file.available ? '' : 'unavailable'}`.trim());
  button.type = 'button';
  button.dataset.kind = file.kind;
  button.title = file.available
    ? (file.kind === 'excel' ? '双击在本机 Excel 中打开' : '单击选为 AI 上下文，双击在本机打开')
    : '文件尚未生成';
  button.append(create('span', 'file-icon', fileIcon(file.kind)));
  button.append(create('span', '', file.name));
  button.append(create('small', '', fileDetail(file)));
  if (file.path === state.selectedPath) button.classList.add('active');
  if (file.kind !== 'excel') button.addEventListener('click', () => selectFile(file, button));
  button.addEventListener('dblclick', () => openLocalFile(file));
  return button;
}

/** 将一个可折叠接口文件夹及其文件节点加入侧边栏。 */
function appendFolder(target, name, files, collapsed = false) {
  const folder = create('section', `tree-folder${collapsed ? ' collapsed' : ''}`);
  const button = create('button', 'folder-button');
  button.type = 'button';
  button.append(create('span', 'folder-chevron', '▾'));
  button.append(create('strong', '', name));
  button.append(create('small', '', `${files.filter((file) => file.available).length}/${files.length}`));
  const children = create('div', 'folder-files');
  files.forEach((file) => children.append(makeFileButton(file)));
  button.addEventListener('click', () => folder.classList.toggle('collapsed'));
  folder.append(button, children);
  target.append(folder);
}

/** 返回会话自定义标题，缺失时回退到上传文件名。 */
function taskTitle(task) {
  return task.title || (task.filename || '未命名任务').replace(/\.pdf$/i, '');
}

/** 渲染一个同级会话根节点及其重命名、删除和文件树操作。 */
function appendTaskRoot(target, task, draft = false) {
  const taskId = draft ? null : task.id;
  const active = draft ? state.taskId === null && state.draftOpen : taskId === state.taskId;
  const collapsed = !draft && state.collapsedTasks.has(taskId);
  const folder = create('section', `task-root-folder${active ? ' active' : ''}${collapsed ? ' collapsed' : ''}`);
  const header = create('div', 'task-root-header');
  const button = create('button', 'task-root-button');
  button.type = 'button';
  button.title = active && !draft ? '点击折叠或展开当前任务' : '切换到此任务';
  button.append(create('span', 'task-chevron', draft ? '•' : '▾'));
  button.append(create('strong', '', draft ? '新任务' : taskTitle(task)));
  button.append(create('small', '', draft ? '可聊天' : statusText(task.status)));

  const actions = create('div', 'task-actions');
  if (!draft) {
    const renameButton = create('button', 'task-action rename-task', '✎');
    renameButton.type = 'button';
    renameButton.title = `重命名“${taskTitle(task)}”`;
    renameButton.setAttribute('aria-label', `重命名 ${taskTitle(task)}`);
    renameButton.addEventListener('click', (event) => {
      event.stopPropagation();
      openSessionDialog('rename', task);
    });
    const deleteButton = create('button', 'task-action delete-task', '×');
    deleteButton.type = 'button';
    deleteButton.title = task.status === 'running' || task.status === 'queued' ? '运行中的任务不能删除' : `删除“${taskTitle(task)}”`;
    deleteButton.setAttribute('aria-label', `删除 ${taskTitle(task)}`);
    deleteButton.disabled = task.status === 'running' || task.status === 'queued';
    deleteButton.addEventListener('click', (event) => {
      event.stopPropagation();
      openSessionDialog('delete', task);
    });
    actions.append(renameButton, deleteButton);
  }

  const children = create('div', 'task-root-children');
  if (draft && active) {
    children.append(create('p', 'task-empty-copy', '可以直接聊天；需要时再通过“＋”附加接口 PDF。'));
  } else if (active && !collapsed) {
    if (!state.tree) {
      children.append(create('p', 'task-empty-copy', '正在读取任务文件…'));
    } else {
      if (!state.tree.folders.length) children.append(create('p', 'task-empty-copy', '接口切分后，文件会出现在这里。'));
      state.tree.folders.forEach((item) => appendFolder(children, item.name, item.files));
      if (state.tree.report?.available) children.append(makeFileButton(state.tree.report));
    }
  }

  button.addEventListener('click', async () => {
    if (draft) {
      if (!active) resetWorkspace();
      return;
    }
    if (active) {
      if (state.collapsedTasks.has(taskId)) state.collapsedTasks.delete(taskId);
      else state.collapsedTasks.add(taskId);
      renderTaskSidebar();
      return;
    }
    state.collapsedTasks.delete(taskId);
    try {
      await openTask(taskId);
    } catch (error) {
      appendMessage('assistant', `任务切换失败：${error.message}`);
    }
  });

  header.append(button, actions);
  folder.append(header, children);
  target.append(folder);
}

/** 打开会话重命名或永久删除确认对话框。 */
function openSessionDialog(mode, task) {
  state.sessionDialog = {mode, task};
  const deleting = mode === 'delete';
  $('#session-modal-title').textContent = deleting ? '删除会话' : '重命名会话';
  $('#session-modal-description').textContent = deleting
    ? `确定删除“${taskTitle(task)}”吗？该任务的聊天记录、接口文档、测试用例、测试结果和图片都会从后端磁盘永久删除。`
    : '新名称会同步保存到该任务后端目录中的 task_meta.json。';
  $('#session-name-field').classList.toggle('hidden', deleting);
  $('#session-name-input').value = taskTitle(task);
  const confirmButton = $('#session-modal-confirm');
  confirmButton.textContent = deleting ? '永久删除' : '保存名称';
  confirmButton.classList.toggle('danger', deleting);
  $('#session-modal').classList.remove('hidden');
  if (!deleting) {
    $('#session-name-input').focus();
    $('#session-name-input').select();
  }
}

/** 关闭会话管理对话框并清理待处理状态。 */
function closeSessionDialog() {
  state.sessionDialog = null;
  $('#session-modal').classList.add('hidden');
}

/** 调用 REST 接口重命名会话并同步前端任务列表。 */
async function renameSession(task, nextTitle) {
  const currentTitle = taskTitle(task);
  if (nextTitle.trim() === currentTitle) return;
  try {
    const updated = await api(`/api/tasks/${task.id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: nextTitle.trim()}),
    });
    const index = state.tasks.findIndex((item) => item.id === task.id);
    if (index >= 0) state.tasks[index] = updated;
    if (state.taskId === task.id) {
      state.task = updated;
      $('#top-task-name').textContent = updated.title;
    }
    renderTaskSidebar();
  } catch (error) {
    appendMessage('assistant', `会话重命名失败：${error.message}`);
  }
}

/** 删除后端会话目录，并选择下一个可用会话或新任务页。 */
async function deleteSession(task) {
  const title = taskTitle(task);
  try {
    const deletingActiveTask = state.taskId === task.id;
    await api(`/api/tasks/${task.id}`, {method: 'DELETE'});
    state.collapsedTasks.delete(task.id);
    await loadTaskList();
    if (!deletingActiveTask) return;
    if (state.draftOpen) {
      resetWorkspace();
    } else if (state.tasks.length) {
      await openTask(state.tasks[0].id);
    } else {
      resetWorkspace();
    }
  } catch (error) {
    appendMessage('assistant', `会话删除失败：${error.message}`);
  }
}

/** 根据当前任务列表和文件树重新绘制整个左侧工作区。 */
function renderTaskSidebar() {
  const target = $('#file-tree');
  target.replaceChildren();
  if (state.draftOpen) appendTaskRoot(target, {}, true);
  state.tasks.forEach((task) => appendTaskRoot(target, task));
  if (!state.draftOpen && !state.tasks.length) {
    target.append(create('p', 'empty-copy', '点击“新任务”，在这里创建一个同级任务窗口。'));
  }
  const reportAvailable = Boolean(state.taskId && state.tree?.report?.available);
  $('#report-link').classList.toggle('hidden', !reportAvailable);
  const workspaceRoot = state.taskId ? state.tree?.workspace_root : null;
  const locationNode = $('#workspace-location');
  locationNode.textContent = workspaceRoot ? `后端任务目录：${workspaceRoot}` : '文件保存在后端任务目录；浏览器只传任务 ID 和相对路径。';
  if (workspaceRoot) locationNode.title = workspaceRoot;
  else locationNode.removeAttribute('title');
}

/** 保存最新文件树快照并触发侧边栏渲染。 */
function renderFileTree(tree) {
  state.tree = tree;
  renderTaskSidebar();
}

/** 从后端读取全部持久化会话并刷新侧边栏。 */
async function loadTaskList() {
  const data = await api('/api/tasks');
  state.tasks = data.tasks || [];
  renderTaskSidebar();
  return state.tasks;
}

/** 读取当前会话文件树，并仅在内容变化时重新渲染。 */
async function loadFiles(force = false) {
  if (!state.taskId) return;
  const tree = await api(`/api/tasks/${state.taskId}/files`);
  const signature = JSON.stringify({
    folders: tree.folders.map((folder) => folder.files.map((file) => [file.path, file.available, file.size])),
    report: tree.report?.available,
  });
  if (force || signature !== state.treeSignature) {
    state.treeSignature = signature;
    renderFileTree(tree);
  }
}

/** 读取当前会话的持久化聊天历史并恢复到页面。 */
async function loadChat() {
  if (!state.taskId) return;
  const data = await api(`/api/tasks/${state.taskId}/chat`);
  resetConversation(data.messages || []);
  state.chatLoaded = true;
}

/** 轮询当前任务状态与文件树，同时防止旧请求覆盖新会话。 */
async function poll() {
  // 保存请求发出时的 id，防止旧任务的慢响应覆盖刚切换的新会话。
  const requestedTaskId = state.taskId;
  if (!requestedTaskId) return;
  try {
    const task = await api(`/api/tasks/${requestedTaskId}`);
    if (state.taskId !== requestedTaskId) return;
    renderTask(task);
    await loadFiles();
    if (['idle', 'ready', 'completed', 'blocked', 'failed'].includes(task.status) && state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
  } catch (error) {
    setConnection(false);
    appendMessage('assistant', `任务状态读取失败：${error.message}`);
  }
}

/** 仅在任务处于活动状态时启动周期性状态轮询。 */
function startPolling() {
  if (state.timer) clearInterval(state.timer);
  if (state.task && !['idle', 'ready', 'completed', 'blocked', 'failed'].includes(state.task.status)) {
    state.timer = setInterval(poll, 1600);
  }
}

/** 切换到指定会话并加载聊天、进度和任务文件。 */
async function openTask(taskId) {
  if (state.timer) clearInterval(state.timer);
  state.taskId = taskId;
  state.task = null;
  state.tree = null;
  state.timer = null;
  state.treeSignature = null;
  state.selectedPath = null;
  state.selectedName = null;
  state.chatLoaded = false;
  state.announced = new Set();
  $('#selected-context').classList.add('hidden');
  history.replaceState(null, '', `?task=${taskId}`);
  renderTaskSidebar();
  await loadChat();
  await poll();
  startPolling();
}

/** 将字节数格式化为 KB 或 MB。 */
function formatBytes(size) {
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

/** 校验并显示聊天框中待上传 PDF 的附件卡片。 */
function showAttachment(file) {
  if (!file) return clearAttachment();
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    $('#chat-pdf').value = '';
    appendMessage('assistant', '附件必须是 PDF 文件。');
    return;
  }
  $('#attachment-name').textContent = file.name;
  $('#attachment-size').textContent = formatBytes(file.size);
  $('#attachment-card').classList.remove('hidden');
  $('#chat-input').placeholder = '说明希望 Agent 如何处理这份 PDF…';
}

/** 清除待上传附件并恢复聊天输入提示。 */
function clearAttachment() {
  const input = $('#chat-pdf');
  if (input) input.value = '';
  const card = $('#attachment-card');
  if (card) card.classList.add('hidden');
  const text = $('#chat-input');
  if (text) text.placeholder = '输入任务要求，或点击＋附加接口 PDF…';
}

/** 创建一个无需 PDF 即可聊天的后端会话。 */
async function createChatSession() {
  // 空 JSON 创建纯聊天会话，PDF 不是创建会话的前置条件。
  return api('/api/tasks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({}),
  });
}

/** 新建带 PDF 的会话，或将 PDF 附加到已有空会话。 */
async function uploadTaskPdf(file, taskId = null) {
  // taskId 存在表示附加到当前空会话；否则创建一个新的带 PDF 会话。
  return api(taskId ? `/api/tasks/${taskId}/pdf` : '/api/tasks', {
    method: 'POST',
    body: file,
    headers: {
      'Content-Type': 'application/pdf',
      'X-Filename': encodeURIComponent(file.name),
      'X-API-Base-URL': $('#attachment-base-url').value.trim(),
      'X-Execute-Tests': String($('#attachment-execute').checked),
    },
  });
}

/** 提交聊天消息，必要时先创建会话或上传 PDF，并刷新任务状态。 */
async function sendChat(event) {
  // 主交互链：确保会话存在 → 可选附加 PDF → POST chat → 刷新进度/文件树。
  event.preventDefault();
  if (state.chatBusy) return;
  const input = $('#chat-input');
  const file = $('#chat-pdf').files[0] || null;
  const typedMessage = input.value.trim();
  if (!typedMessage && !file) return;
  state.chatBusy = true;
  $('#send-chat').disabled = true;
  $('#attach-pdf').disabled = true;
  $('#chat-state').textContent = file ? '正在附加 PDF…' : (!state.taskId ? '正在创建会话…' : 'DeepSeek 正在处理…');
  try {
    if (file) {
      const currentAcceptsPdf = Boolean(state.taskId && state.task && !state.task.has_pdf);
      const task = await uploadTaskPdf(file, currentAcceptsPdf ? state.taskId : null);
      state.draftOpen = false;
      await loadTaskList();
      await openTask(task.id);
      clearAttachment();
    } else if (!state.taskId) {
      const task = await createChatSession();
      state.draftOpen = false;
      await loadTaskList();
      await openTask(task.id);
    }

    const message = typedMessage || '我已上传接口 PDF。请确认收到并等待我的下一步指令。';
    input.value = '';
    appendMessage('user', file ? `📎 ${file.name}\n${message}` : message);
    const thinking = appendMessage('assistant', '正在读取任务状态和工作区文件', 'thinking');
    thinking.querySelector('.bubble').classList.add('thinking-dots');
    $('#chat-state').textContent = 'DeepSeek 正在处理…';
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(poll, 1600);
    try {
      const data = await api(`/api/tasks/${state.taskId}/chat`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message, selected_path: state.selectedPath}),
      });
      thinking.remove();
      appendMessage('assistant', data.message.content);
      $('#chat-suggestions').classList.add('hidden');
    } catch (error) {
      thinking.remove();
      appendMessage('assistant', `消息处理失败：${error.message}`);
    }
  } catch (error) {
    appendMessage('assistant', `PDF 任务创建失败：${error.message}`);
  } finally {
    if (state.taskId) {
      await poll();
      startPolling();
    }
    state.chatBusy = false;
    $('#send-chat').disabled = false;
    $('#attach-pdf').disabled = false;
    $('#chat-state').textContent = 'DeepSeek · 可执行工作区 Agent';
    input.focus();
  }
}

/** 为聊天、附件、会话管理和快捷建议绑定页面事件。 */
function bindActions() {
  $('#chat-form').addEventListener('submit', sendChat);
  $('#report-link').addEventListener('dblclick', () => {
    if (state.tree?.report?.available) openLocalFile(state.tree.report);
  });
  $('#refresh-files').addEventListener('click', () => {
    loadTaskList().then(() => {
      if (state.taskId) return loadFiles(true);
      return null;
    });
  });
  $('#clear-context').addEventListener('click', () => {
    state.selectedPath = null;
    state.selectedName = null;
    $('#selected-context').classList.add('hidden');
    document.querySelectorAll('.file-button').forEach((node) => node.classList.remove('active'));
  });
  $('#new-task-button').addEventListener('click', resetWorkspace);
  $('#attach-pdf').addEventListener('click', () => $('#chat-pdf').click());
  $('#chat-pdf').addEventListener('change', () => showAttachment($('#chat-pdf').files[0]));
  $('#remove-attachment').addEventListener('click', clearAttachment);
  $('#session-modal-cancel').addEventListener('click', closeSessionDialog);
  $('#session-modal').addEventListener('click', (event) => {
    if (event.target === $('#session-modal')) closeSessionDialog();
  });
  $('#session-modal-confirm').addEventListener('click', async () => {
    const pending = state.sessionDialog;
    if (!pending) return;
    const confirmButton = $('#session-modal-confirm');
    confirmButton.disabled = true;
    try {
      if (pending.mode === 'rename') {
        const title = $('#session-name-input').value.trim();
        if (!title) {
          $('#session-modal-description').textContent = '会话名称不能为空。';
          return;
        }
        await renameSession(pending.task, title);
      } else {
        await deleteSession(pending.task);
      }
      closeSessionDialog();
    } finally {
      confirmButton.disabled = false;
    }
  });
  $('#session-name-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') $('#session-modal-confirm').click();
    if (event.key === 'Escape') closeSessionDialog();
  });
  $('#chat-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      $('#chat-form').requestSubmit();
    }
  });
  document.querySelectorAll('#chat-suggestions button').forEach((button) => button.addEventListener('click', () => {
    $('#chat-input').value = button.textContent;
    $('#chat-form').requestSubmit();
  }));
}

/** 检查后端连接并恢复 URL 指定或最近一次持久化会话。 */
async function boot() {
  // 启动时先验证后端，再恢复 URL 指定会话或最近一次持久化会话。
  bindActions();
  renderProgress(emptyProgress());
  try {
    await api('/api/health');
    setConnection(true);
  } catch {
    setConnection(false);
    resetConversation([], true);
    return;
  }

  try {
    await loadTaskList();
  } catch (error) {
    appendMessage('assistant', `历史任务读取失败：${error.message}`);
  }

  const requestedTaskId = new URLSearchParams(location.search).get('task');
  if (/^[a-f0-9]{32}$/.test(requestedTaskId || '')) {
    try {
      await openTask(requestedTaskId);
      return;
    } catch (error) {
      appendMessage('assistant', `指定任务无法打开：${error.message}`);
    }
  }

  if (state.tasks.length) {
    await openTask(state.tasks[0].id);
    return;
  }
  resetWorkspace();
}

boot();
