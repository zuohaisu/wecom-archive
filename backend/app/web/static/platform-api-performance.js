/* GH-186: /platform/api-performance — super-admin API performance page.
 * Client-side rendering over the platform-admin read APIs; PC.* helpers
 * from platform-console.js; createElement/textContent only (no innerHTML,
 * matching the other platform pages' XSS discipline). All bucket labels
 * are shown in Asia/Shanghai; empty stats render as "—" (never 0). */
(function () {
  'use strict';
  var PC = window.PC;

  var EMAIL_STATUS_TEXT = {
    unconfigured: '未配置收件邮箱',
    not_attempted: '尚未触发',
    not_triggered: '当日暂无持续异常',
    already_claimed: '当日已处理（重启前已产生当日意图）',
    claim_failed: '当日意图记录失败（未发送）',
    accepted: 'Provider 已接受',
    failed_or_unknown: '失败或结果未知（当天不再重试）'
  };
  var KIND_TEXT = { normal: '普通响应', stream: '流式响应开始' };

  function msText(value, capped) {
    if (value === null || value === undefined) { return '—'; }
    return (capped ? '≥ ' : '') + PC.number(Math.round(value)) + ' ms';
  }
  function countText(value) { return (value === null || value === undefined) ? '—' : PC.number(value); }
  function rateText(value) {
    if (value === null || value === undefined) { return '—'; }
    return (value * 100).toFixed(2) + '%';
  }
  function shanghaiHourLabel(iso) {
    var date = new Date(iso);
    var local = new Date(date.getTime() + (8 * 60 + date.getTimezoneOffset()) * 60000);
    return PC.date(local).slice(11, 16);
  }
  function shanghaiDayLabel(dayText) { return dayText; }

  function showError(error) {
    var box = PC.el('error');
    box.textContent = error && error.message ? error.message : '加载失败';
    box.hidden = false;
  }
  function clearError() { PC.el('error').hidden = true; }

  function renderStatus(data) {
    PC.el('apiperf-enabled').textContent = data.enabled ? '运行中' : '未启用';
    PC.el('apiperf-observations').textContent = '累计观测 ' + PC.number(data.total_observations) + ' 次';
    var flushOk = data.flush.consecutive_flush_errors === 0 && data.flush.last_flush_result !== 'never';
    PC.el('apiperf-flush').textContent = flushOk ? '正常' : (data.flush.last_flush_result === 'never' ? '尚未落库' : '异常');
    PC.el('apiperf-flush-at').textContent = data.flush.last_flush_at
      ? ('上次落库 ' + shanghaiHourLabel(data.flush.last_flush_at))
      : '尚未落库';
    PC.el('apiperf-anomaly-count').textContent = PC.number(data.detection.anomaly_count);
    PC.el('apiperf-detection').textContent = data.detection.enabled
      ? ('窗口 ' + data.detection.window_minutes + ' 分钟 · 阈值 ' + data.detection.p95_threshold_ms + ' ms')
      : '检测未启用';
    PC.el('apiperf-email').textContent = EMAIL_STATUS_TEXT[data.email.status] || data.email.status;
    var coverage = data.coverage;
    var parts = [];
    parts.push('时区 Asia/Shanghai · p95 为直方图估算');
    if (coverage.hourly_rows) {
      parts.push('小时明细覆盖 ' + shanghaiHourLabel(coverage.hourly_earliest) + ' 起（保留 720 小时）');
    } else {
      parts.push('暂无小时明细');
    }
    if (coverage.daily_rows) {
      parts.push('日趋势覆盖 ' + shanghaiDayLabel(coverage.daily_earliest) + ' 起（保留 180 天）');
    } else {
      parts.push('暂无日趋势');
    }
    if (data.dropped_late_observations || data.dropped_pending_overflow) {
      parts.push('存在统计缺口（迟到丢弃 ' + PC.number(data.dropped_late_observations)
        + ' / 缓冲溢出 ' + PC.number(data.dropped_pending_overflow) + '），数据可能不完整');
    }
    PC.el('apiperf-coverage').textContent = parts.join(' · ');
    var notes = PC.el('apiperf-notes');
    PC.clear(notes);
    (data.notes || []).forEach(function (note) {
      var item = document.createElement('li');
      item.textContent = note;
      notes.appendChild(item);
    });
    PC.el('apiperf-refreshed').textContent = '更新于 ' + shanghaiHourLabel(data.generated_at);
  }

  function renderAnomalies(data) {
    var body = PC.el('apiperf-anomaly-rows');
    PC.clear(body);
    if (!data.anomalies.length) {
      PC.emptyRow(body, 7, data.detection_enabled
        ? '当前无持续异常；重启后窗口重新积累，样本不足期间不显示为健康。'
        : '检测未启用。');
      return;
    }
    data.anomalies.forEach(function (anomaly) {
      var row = document.createElement('tr');
      var cells = [
        '[' + anomaly.method + '] ' + anomaly.route,
        KIND_TEXT[anomaly.kind] || anomaly.kind,
        PC.number(anomaly.samples) + '（覆盖 ' + anomaly.minutes_covered + ' 分钟）',
        msText(anomaly.p95_ms, anomaly.p95_capped),
        PC.number(anomaly.threshold_ms) + ' ms',
        '约 ' + anomaly.sustained_minutes + ' 分钟',
        shanghaiHourLabel(anomaly.first_detected_at)
      ];
      cells.forEach(function (text) {
        var cell = document.createElement('td');
        cell.textContent = text;
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
  }

  function endpointRow(endpoint) {
    var row = document.createElement('tr');
    var stats = endpoint.stats;
    var cells = [
      '[' + endpoint.method + '] ' + endpoint.route + (endpoint.registered ? '' : '（已下线，保留历史）'),
      countText(stats.requests),
      rateText(stats.error_rate),
      msText(stats.avg_ms),
      msText(stats.p95_ms, stats.p95_capped),
      (stats.success_min_ms === null ? '—' : PC.number(Math.round(stats.success_min_ms)))
        + ' / '
        + (stats.success_max_ms === null ? '—' : PC.number(Math.round(stats.success_max_ms))),
      countText(stats.cancelled),
      stats.stream_count ? (PC.number(stats.stream_count) + (stats.stream_errors ? ('（错误 ' + PC.number(stats.stream_errors) + '）') : '')) : '—',
      endpoint.has_samples ? (stats.hist_available ? '充足' : '样本不足') : '无样本'
    ];
    cells.forEach(function (text) {
      var cell = document.createElement('td');
      cell.textContent = text;
      row.appendChild(cell);
    });
    return row;
  }

  function renderEndpoints(data) {
    var body = PC.el('apiperf-endpoint-rows');
    PC.clear(body);
    if (!data.endpoints.length) {
      PC.emptyRow(body, 9, '窗口内没有匹配的接口（未调用过的注册路由显示“无样本”）。');
    }
    data.endpoints.forEach(function (endpoint) { body.appendChild(endpointRow(endpoint)); });
    var site = data.site;
    PC.el('apiperf-site-summary').textContent = '全站加权汇总：'
      + PC.number(site.requests) + ' 次请求 · 错误率 ' + rateText(site.error_rate)
      + ' · 平均 ' + msText(site.avg_ms) + ' · 成功 p95 ' + msText(site.p95_ms, site.p95_capped)
      + (site.hist_available ? '' : '（p95 直方图不可用/样本不足）');
    var from = (data.page - 1) * data.page_size + 1;
    var to = Math.min(data.page * data.page_size, data.total);
    PC.el('apiperf-pagination').textContent = data.total
      ? ('第 ' + from + '–' + to + ' 条，共 ' + PC.number(data.total) + ' 个接口')
      : '';
    PC.el('apiperf-window-note').textContent = '最近 ' + data.window_hours + ' 小时（小时聚合；当前小时为已落库部分，最多落后约 15 分钟）';
  }

  function seriesRow(point, labeler) {
    var row = document.createElement('tr');
    var cells = [
      labeler(point.bucket),
      countText(point.requests),
      countText(point.errors),
      msText(point.avg_ms),
      msText(point.p95_ms, point.p95_capped),
      point.stream_count ? PC.number(point.stream_count) : '—'
    ];
    cells.forEach(function (text) {
      var cell = document.createElement('td');
      cell.textContent = text;
      row.appendChild(cell);
    });
    return row;
  }

  function renderSeries(granularity, data, bodyId, emptyLabel) {
    var body = PC.el(bodyId);
    PC.clear(body);
    if (!data.points.length) { PC.emptyRow(body, 6, emptyLabel); return; }
    var labeler = granularity === 'daily' ? shanghaiDayLabel : shanghaiHourLabel;
    data.points.forEach(function (point) { body.appendChild(seriesRow(point, labeler)); });
  }

  function loadStatus() {
    return PC.request('/api/platform/api-performance/status').then(function (data) {
      renderStatus(data);
      return data;
    });
  }
  function loadAnomalies() {
    return PC.request('/api/platform/api-performance/anomalies').then(renderAnomalies);
  }
  function loadEndpoints() {
    var params = new URLSearchParams();
    params.set('window_hours', PC.el('apiperf-window-hours').value || '24');
    params.set('traffic_class', PC.el('apiperf-traffic').value);
    params.set('sort', PC.el('apiperf-sort').value);
    params.set('page_size', '50');
    if (PC.el('apiperf-method').value) { params.set('method', PC.el('apiperf-method').value); }
    if (PC.el('apiperf-q').value) { params.set('q', PC.el('apiperf-q').value); }
    return PC.request('/api/platform/api-performance/endpoints?' + params.toString()).then(renderEndpoints);
  }
  function loadDaily() {
    var params = new URLSearchParams();
    params.set('granularity', 'daily');
    params.set('days', PC.el('apiperf-daily-days').value || '30');
    if (PC.el('apiperf-daily-method').value) { params.set('method', PC.el('apiperf-daily-method').value.trim().toUpperCase()); }
    if (PC.el('apiperf-daily-route').value) { params.set('route', PC.el('apiperf-daily-route').value.trim()); }
    return PC.request('/api/platform/api-performance/series?' + params.toString())
      .then(function (data) { renderSeries('daily', data, 'apiperf-daily-rows', '该范围内暂无日聚合数据。'); })
      .catch(function (error) { PC.emptyRow(PC.el('apiperf-daily-rows'), 6, error.message); });
  }
  function loadHourly() {
    var day = PC.el('apiperf-hourly-date').value;
    var body = PC.el('apiperf-hourly-rows');
    if (!day) { PC.emptyRow(body, 6, '选择日期后查看小时趋势（最近 720 小时内）。'); return Promise.resolve(); }
    var params = new URLSearchParams();
    params.set('granularity', 'hourly');
    params.set('date', day);
    if (PC.el('apiperf-hourly-method').value) { params.set('method', PC.el('apiperf-hourly-method').value.trim().toUpperCase()); }
    if (PC.el('apiperf-hourly-route').value) { params.set('route', PC.el('apiperf-hourly-route').value.trim()); }
    return PC.request('/api/platform/api-performance/series?' + params.toString())
      .then(function (data) { renderSeries('hourly', data, 'apiperf-hourly-rows', '该日暂无小时聚合数据（超出 720 小时窗口会明确拒绝）。'); })
      .catch(function (error) { PC.emptyRow(body, 6, error.message); });
  }

  function refreshAll() {
    clearError();
    return Promise.all([
      loadStatus(), loadAnomalies(), loadEndpoints(), loadDaily(), loadHourly()
    ]).catch(showError);
  }

  function initialiseDates() {
    var now = new Date();
    var local = new Date(now.getTime() + (8 * 60 + now.getTimezoneOffset()) * 60000);
    PC.el('apiperf-hourly-date').value = local.toISOString().slice(0, 10);
  }

  document.addEventListener('DOMContentLoaded', function () {
    initialiseDates();
    PC.el('apiperf-filters').addEventListener('submit', function (event) { event.preventDefault(); loadEndpoints().catch(showError); });
    PC.el('apiperf-daily-form').addEventListener('submit', function (event) { event.preventDefault(); loadDaily().catch(showError); });
    PC.el('apiperf-hourly-form').addEventListener('submit', function (event) { event.preventDefault(); loadHourly().catch(showError); });
    PC.wireRefreshStamp(refreshAll);
    refreshAll();
  });
}());
