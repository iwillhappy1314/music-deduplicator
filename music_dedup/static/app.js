(() => {
  const tokenInput = document.querySelector('#token');
  const connectionState = document.querySelector('#connection-state');
  const statusBadge = document.querySelector('#status-badge');
  const statusDetail = document.querySelector('#status-detail');
  const reportTime = document.querySelector('#report-time');
  const logView = document.querySelector('#log');
  const summaryGrid = document.querySelector('#summary-grid');
  const metadataRefresh = document.querySelector('#metadata-refresh');
  const metadataState = document.querySelector('#metadata-state');
  const metadataList = document.querySelector('#metadata-list');
  const buttons = [...document.querySelectorAll('[data-action]')];
  const statusLabels = { idle: '空闲', running: '运行中', success: '已完成', error: '失败' };
  const actionLabels = { dedup: '执行去重', lyrics: '获取外挂歌词', artwork: '获取专辑封面', all: '全部执行' };
  const metadataLabels = { title: 'Title', artist: 'Artist', album: 'Album' };

  tokenInput.value = window.localStorage.getItem('music-deduplicator-token') || '';
  tokenInput.addEventListener('change', () => window.localStorage.setItem('music-deduplicator-token', tokenInput.value));
  document.querySelector('#token-form').addEventListener('submit', (event) => event.preventDefault());

  function authHeaders() {
    const token = tokenInput.value.trim();
    return token ? { 'X-Auth-Token': token } : {};
  }

  function setMetadataState(message, state = '') {
    metadataState.textContent = message;
    metadataState.className = `metadata-state${state ? ` metadata-state--${state}` : ''}`;
  }

  function setConnection(online) {
    connectionState.classList.toggle('connection-state--online', online);
    connectionState.classList.toggle('connection-state--offline', !online);
    connectionState.lastElementChild.textContent = online ? '已连接' : '连接失败';
  }

  function formatNumber(value) {
    return typeof value === 'number' ? value.toLocaleString('zh-CN') : '—';
  }

  function updateSummary(data) {
    const summary = data.report_summary?.summary || {};
    const enrichment = data.report_summary?.enrichment || {};
    const lyric = enrichment.lyrics || {};
    const artwork = enrichment.artwork || {};
    const created = (lyric.created || 0) + (artwork.created || 0);
    const values = [summary.audio_files_read, summary.duplicate_groups, summary.moved_files, created];
    [...summaryGrid.querySelectorAll('strong')].forEach((node, index) => { node.textContent = formatNumber(values[index]); });
    reportTime.textContent = data.report_summary?.generated_at_utc ? `报告时间 ${data.report_summary.generated_at_utc}` : '等待报告';
  }

  function updateStatus(data) {
    const status = data.status || 'idle';
    statusBadge.textContent = statusLabels[status] || status;
    statusBadge.className = `status-badge status-badge--${status}`;
    statusDetail.textContent = data.action ? `${actionLabels[data.action] || data.action}${status === 'running' ? '正在处理音乐库…' : ''}` : '尚未运行任务';
    logView.textContent = data.log || '等待任务输出…';
    buttons.forEach((button) => { button.disabled = status === 'running'; });
    metadataRefresh.disabled = status === 'running';
    updateSummary(data);
  }

  async function refresh() {
    try {
      const response = await fetch('/api/status', { headers: authHeaders() });
      if (!response.ok) throw new Error('status');
      updateStatus(await response.json());
      setConnection(true);
    } catch (error) {
      setConnection(false);
    }
  }

  async function runAction(action) {
    buttons.forEach((button) => { button.disabled = true; });
    try {
      const response = await fetch(`/api/run/${action}`, { method: 'POST', headers: authHeaders() });
      const payload = await response.json();
      if (!response.ok) window.alert(payload.error || '任务未启动');
    } catch (error) {
      window.alert('无法连接到控制台服务');
    }
    await refresh();
  }

  function createMetadataField(field, value, editable) {
    const label = document.createElement('label');
    label.className = 'metadata-field';
    const labelText = document.createElement('span');
    labelText.textContent = metadataLabels[field];
    const input = document.createElement('input');
    input.name = field;
    input.type = 'text';
    input.value = value || '';
    input.maxLength = 1000;
    input.disabled = !editable;
    label.append(labelText, input);
    return label;
  }

  function updateMetadataStateAfterRemove() {
    const remaining = metadataList.querySelectorAll('.metadata-row').length;
    if (remaining === 0) {
      const empty = document.createElement('p');
      empty.className = 'metadata-empty';
      empty.textContent = '没有发现空的 Title、Artist 或 Album。';
      metadataList.replaceChildren(empty);
      setMetadataState('已修复全部问题', 'success');
      return;
    }
    setMetadataState(`还剩 ${remaining.toLocaleString('zh-CN')} 首歌曲需要处理`);
  }

  function createMetadataRow(issue) {
    const form = document.createElement('form');
    form.className = 'metadata-row';
    form.dataset.sizeBytes = String(issue.size_bytes ?? '');
    form.dataset.modifiedNs = String(issue.modified_ns ?? '');

    const file = document.createElement('div');
    file.className = 'metadata-row__file';
    const fileName = document.createElement('strong');
    const pathParts = String(issue.relative_path || '').split('/');
    fileName.textContent = pathParts[pathParts.length - 1] || '未命名音频';
    const filePath = document.createElement('small');
    filePath.textContent = issue.relative_path || issue.path || '';
    const missing = document.createElement('span');
    missing.className = 'metadata-row__missing';
    missing.textContent = `缺少：${(issue.missing_fields || []).map((field) => metadataLabels[field] || field).join('、')}`;
    file.append(fileName, filePath, missing);

    const fields = ['title', 'artist', 'album'].map((field) => (
      createMetadataField(field, issue[field], issue.editable !== false)
    ));
    const actions = document.createElement('div');
    actions.className = 'metadata-row__actions';
    const save = document.createElement('button');
    save.className = 'metadata-save';
    save.type = 'submit';
    save.textContent = issue.editable === false ? '无法编辑' : '保存';
    save.disabled = issue.editable === false;
    const result = document.createElement('span');
    result.className = 'metadata-row__result';
    result.textContent = issue.error || '';
    actions.append(save, result);
    form.append(file, ...fields, actions);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      save.disabled = true;
      result.className = 'metadata-row__result';
      result.textContent = '保存中…';
      const payload = {
        relative_path: issue.relative_path,
        title: form.elements.title.value.trim(),
        artist: form.elements.artist.value.trim(),
        album: form.elements.album.value.trim(),
        size_bytes: Number(form.dataset.sizeBytes),
        modified_ns: form.dataset.modifiedNs,
      };
      try {
        const response = await fetch('/api/metadata', {
          method: 'PUT',
          headers: { ...authHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const responsePayload = await response.json();
        if (!response.ok) throw new Error(responsePayload.error || '保存失败');
        result.className = 'metadata-row__result metadata-row__result--success';
        result.textContent = '已保存';
        if (responsePayload.issue === null) {
          form.remove();
          updateMetadataStateAfterRemove();
          return;
        }
        issue.size_bytes = responsePayload.issue.size_bytes;
        issue.modified_ns = responsePayload.issue.modified_ns;
        form.dataset.sizeBytes = String(issue.size_bytes);
        form.dataset.modifiedNs = String(issue.modified_ns);
        issue.missing_fields = responsePayload.issue.missing_fields;
        missing.textContent = `缺少：${issue.missing_fields.map((field) => metadataLabels[field] || field).join('、')}`;
        save.disabled = false;
      } catch (error) {
        result.className = 'metadata-row__result metadata-row__result--error';
        result.textContent = error.message || '保存失败';
        save.disabled = false;
      }
    });
    return form;
  }

  async function loadMetadataIssues() {
    metadataRefresh.disabled = true;
    setMetadataState('正在扫描音乐库…');
    try {
      const response = await fetch('/api/metadata/issues', { headers: authHeaders() });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || '扫描失败');
      metadataList.replaceChildren();
      if (!payload.issues.length) {
        const empty = document.createElement('p');
        empty.className = 'metadata-empty';
        empty.textContent = '没有发现空的 Title、Artist 或 Album。';
        metadataList.append(empty);
      } else {
        payload.issues.forEach((issue) => metadataList.append(createMetadataRow(issue)));
      }
      setMetadataState(`发现 ${Number(payload.count).toLocaleString('zh-CN')} 首歌曲需要处理`, payload.count ? '' : 'success');
    } catch (error) {
      setMetadataState(error.message || '扫描失败', 'error');
    } finally {
      metadataRefresh.disabled = false;
    }
  }

  buttons.forEach((button) => button.addEventListener('click', () => runAction(button.dataset.action)));
  document.querySelector('#refresh').addEventListener('click', refresh);
  metadataRefresh.addEventListener('click', loadMetadataIssues);
  refresh();
  loadMetadataIssues();
  window.setInterval(refresh, 2000);
})();
