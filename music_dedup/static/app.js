(() => {
  const tokenInput = document.querySelector('#token');
  const connectionState = document.querySelector('#connection-state');
  const statusBadge = document.querySelector('#status-badge');
  const statusDetail = document.querySelector('#status-detail');
  const reportTime = document.querySelector('#report-time');
  const logView = document.querySelector('#log');
  const summaryGrid = document.querySelector('#summary-grid');
  const buttons = [...document.querySelectorAll('[data-action]')];
  const statusLabels = { idle: '空闲', running: '运行中', success: '已完成', error: '失败' };
  const actionLabels = { dedup: '执行去重', lyrics: '获取外挂歌词', artwork: '获取专辑封面', all: '全部执行' };

  tokenInput.value = window.localStorage.getItem('music-deduplicator-token') || '';
  tokenInput.addEventListener('change', () => window.localStorage.setItem('music-deduplicator-token', tokenInput.value));

  function authHeaders() {
    const token = tokenInput.value.trim();
    return token ? { 'X-Auth-Token': token } : {};
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

  buttons.forEach((button) => button.addEventListener('click', () => runAction(button.dataset.action)));
  document.querySelector('#refresh').addEventListener('click', refresh);
  refresh();
  window.setInterval(refresh, 2000);
})();
