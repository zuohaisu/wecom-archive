/* GH-186: /platform/api-performance — super-admin API performance page.
 * Client-side rendering over the platform-admin read APIs; PC.* helpers
 * from platform-console.js; createElement(NSE)/textContent only (no
 * innerHTML, matching the other platform pages' XSS discipline).
 * All bucket labels are Asia/Shanghai wall time, computed from UTC
 * milliseconds + 8h and read back with UTC getters — deliberately NOT via
 * PC.date() or toISOString() on a shifted-then-unshifted Date, which
 * double-applies the browser's own timezone offset (QA finding 11).
 * Empty stats render as "—" (never 0).
 * Haisu redesign: the first screen is one trend chart (SVG, no external
 * chart library) with 日趋势/小时趋势 tabs; 采集状态 moved to the page
 * bottom. Both granularities share the same chart type: bars for request
 * counts (red segment = errors, left axis) and a line for success p95
 * (right axis, ms); per-bucket tooltips carry the full stats including
 * overall/success min-max so the QA extrema distinction survives. */
(function () {
  'use strict';
  var PC = window.PC;

  var EMAIL_STATUS_TEXT = {
    unconfigured: '未配置收件邮箱',
    not_attempted: '尚未触发',
    not_triggered: '当日暂无持续异常',
    already_claimed: '当日已处理（重启前已产生当日意图）',
    claim_failed: '当日意图记录失败（未发送）',
    job_failed: '当日任务执行失败（未发送，额度未占用）',
    pending: '待发送',
    accepted: 'Provider 已接受',
    failed_or_unknown: '失败或结果未知（当天不再重试）'
  };
  var KIND_TEXT = { normal: '普通响应', stream: '流式响应开始' };

  var state = { page: 1, totalPages: 1, chartTab: 'daily' };
  var CHART = { W: 960, H: 300, LEFT: 56, RIGHT: 68, TOP: 16, BOTTOM: 32 };
  var SVG_NS = 'http://www.w3.org/2000/svg';

  function msText(value, capped) {
    if (value === null || value === undefined) { return '—'; }
    return (capped ? '≥ ' : '') + PC.number(Math.round(value)) + ' ms';
  }
  function countText(value) { return (value === null || value === undefined) ? '—' : PC.number(value); }
  function rateText(value) {
    if (value === null || value === undefined) { return '—'; }
    return (value * 100).toFixed(2) + '%';
  }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function shanghaiParts(epochMs) {
    var shifted = new Date(epochMs + 8 * 3600 * 1000);
    return {
      date: shifted.getUTCFullYear() + '-' + pad2(shifted.getUTCMonth() + 1) + '-' + pad2(shifted.getUTCDate()),
      time: pad2(shifted.getUTCHours()) + ':' + pad2(shifted.getUTCMinutes())
    };
  }
  function shanghaiTimeLabel(iso) { return shanghaiParts(new Date(iso).getTime()).time; }
  function shanghaiDateLabel(value) {
    // Daily buckets arrive as "YYYY-MM-DD" strings; instants get converted.
    if (typeof value === 'string' && value.length === 10) { return value; }
    return shanghaiParts(new Date(value).getTime()).date;
  }
  function shanghaiToday() { return shanghaiParts(Date.now()).date; }

  function showError(error) {
    var box = PC.el('error');
    box.textContent = error && error.message ? error.message : '加载失败';
    box.hidden = false;
  }
  function clearError() { PC.el('error').hidden = true; }

  function setIncomplete(warn, message) {
    warn.textContent = message;
    warn.hidden = false;
  }

  function renderStatus(data) {
    PC.el('apiperf-enabled').textContent = data.enabled ? '运行中' : '未启用';
    PC.el('apiperf-observations').textContent = '累计观测 ' + PC.number(data.total_observations) + ' 次';
    var dropped = (data.dropped_late_observations || 0) + (data.dropped_pending_overflow || 0) + (data.dropped_stale_batch_observations || 0);
    var flushOk = data.flush.consecutive_flush_errors === 0 && data.flush.last_flush_result !== 'never';
    PC.el('apiperf-flush').textContent = flushOk ? '正常' : (data.flush.last_flush_result === 'never' ? '尚未落库' : '异常');
    PC.el('apiperf-flush-at').textContent = data.flush.last_flush_at
      ? ('上次落库 ' + shanghaiTimeLabel(data.flush.last_flush_at))
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
      parts.push('小时明细覆盖 ' + shanghaiTimeLabel(coverage.hourly_earliest) + ' 起（保留 720 小时）');
    } else {
      parts.push('暂无小时明细');
    }
    if (coverage.daily_rows) {
      parts.push('日趋势覆盖 ' + shanghaiDateLabel(coverage.daily_earliest) + ' 起（保留 180 天）');
    } else {
      parts.push('暂无日趋势');
    }
    if (dropped) {
      parts.push('存在统计缺口（迟到丢弃 ' + PC.number(data.dropped_late_observations || 0)
        + ' / 缓冲溢出 ' + PC.number(data.dropped_pending_overflow || 0)
        + ' / 过期批次丢弃 ' + PC.number(data.dropped_stale_batch_observations || 0) + '），数据可能不完整');
    }
    PC.el('apiperf-coverage').textContent = parts.join(' · ');
    var notes = PC.el('apiperf-notes');
    PC.clear(notes);
    (data.notes || []).forEach(function (note) {
      var item = document.createElement('li');
      item.textContent = note;
      notes.appendChild(item);
    });
    PC.el('apiperf-refreshed').textContent = '更新于 ' + shanghaiTimeLabel(data.generated_at);
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
        shanghaiTimeLabel(anomaly.first_detected_at)
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
    /* Haisu: 只用红/绿表达延迟——绿色 p95 < 1000ms（无需处理），
       红色 p95 >= 1000ms（延迟严重；阈值与「持续异常接口」判定一致）。 */
    var SLOW_P95_MS = 1000;
    function latencyClass(ms) {
      if (ms === null || ms === undefined) { return ''; }
      return ms >= SLOW_P95_MS ? 'latency-red' : 'latency-green';
    }
    var avgCell = document.createElement('td');
    avgCell.textContent = msText(stats.avg_ms);
    avgCell.className = latencyClass(stats.avg_ms);
    var p95Cell = document.createElement('td');
    p95Cell.textContent = msText(stats.p95_ms, stats.p95_capped);
    p95Cell.className = latencyClass(stats.p95_ms);
    var cells = [
      { text: '[' + endpoint.method + '] ' + endpoint.route + (endpoint.registered ? '' : '（已下线，保留历史）') },
      { text: countText(stats.requests) },
      { text: rateText(stats.error_rate) },
      { node: avgCell },
      { node: p95Cell },
      { text: (stats.success_min_ms === null ? '—' : PC.number(Math.round(stats.success_min_ms)))
        + ' / '
        + (stats.success_max_ms === null ? '—' : PC.number(Math.round(stats.success_max_ms))) },
      { text: countText(stats.cancelled) },
      { text: stats.stream_count ? (PC.number(stats.stream_count) + (stats.stream_errors ? ('（错误 ' + PC.number(stats.stream_errors) + '）') : '')) : '—' },
      { text: endpoint.has_samples ? (stats.hist_available ? '充足' : '样本不足') : '无样本' }
    ];
    cells.forEach(function (cell) {
      if (cell.node) { row.appendChild(cell.node); return; }
      var td = document.createElement('td');
      td.textContent = cell.text;
      row.appendChild(td);
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
    state.totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
    state.page = data.page;
    PC.el('apiperf-page-info').textContent = data.total ? ('第 ' + data.page + ' / ' + state.totalPages + ' 页') : '';
    PC.el('apiperf-page-prev').disabled = data.page <= 1;
    PC.el('apiperf-page-next').disabled = data.page >= state.totalPages;
    PC.el('apiperf-window-note').textContent = '最近 ' + data.window_hours + ' 小时（小时聚合；当前小时为已落库部分，最多落后约 15 分钟）';
    var warn = PC.el('apiperf-list-incomplete');
    if (data.incomplete) {
      setIncomplete(warn, '窗口数据超出单次查询上限，以上为部分聚合结果，可能偏小；请缩小时间窗口或路由过滤。');
    } else {
      warn.hidden = true;
    }
  }

  function extremaText(min, max) {
    return (min === null ? '—' : PC.number(Math.round(min)))
      + ' / '
      + (max === null ? '—' : PC.number(Math.round(max)));
  }

  // ---------------------------------------------------------------------------
  // Trend chart (SVG, no external library). Bars = requests with an error
  // segment (left axis); line = success p95 (right axis). One <title> per
  // bucket carries the full stats so no information from the retired
  // per-bucket tables is lost.
  // ---------------------------------------------------------------------------

  function svgEl(name) { return document.createElementNS(SVG_NS, name); }

  function svgText(x, y, content, anchor, className) {
    var node = svgEl('text');
    node.setAttribute('x', x);
    node.setAttribute('y', y);
    node.setAttribute('text-anchor', anchor);
    node.setAttribute('class', className);
    node.textContent = content;
    return node;
  }

  function niceMax(value) {
    if (!value || value <= 0) { return 1; }
    var base = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
    var steps = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] * base >= value) { return steps[i] * base; }
    }
    return 10 * base;
  }

  function bucketLabel(granularity, point) {
    if (granularity === 'daily') { return shanghaiDateLabel(point.bucket); }
    var parts = shanghaiParts(new Date(point.bucket).getTime());
    return parts.date + ' ' + parts.time;
  }

  function bucketTooltip(granularity, point) {
    var lines = [bucketLabel(granularity, point)];
    lines.push('请求数 ' + countText(point.requests) + '（错误 ' + countText(point.errors) + '）');
    lines.push('平均 ' + msText(point.avg_ms));
    lines.push('成功 p95 ' + msText(point.p95_ms, point.p95_capped));
    lines.push('整体 min/max ' + extremaText(point.min_ms, point.max_ms));
    lines.push('成功 min/max ' + extremaText(point.success_min_ms, point.success_max_ms));
    lines.push('流式 ' + countText(point.stream_count));
    return lines.join('\n');
  }

  function renderSeriesChart(granularity, data) {
    var host = PC.el('apiperf-chart');
    PC.clear(host);
    var note = PC.el('apiperf-trend-note');
    var daily = granularity === 'daily';
    if (!data.points.length) {
      var empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = daily ? '该范围内暂无日聚合数据。' : '该日暂无小时聚合数据（超出 720 小时窗口会明确拒绝）。';
      host.appendChild(empty);
      note.textContent = '';
      return;
    }
    note.textContent = daily
      ? '横轴为 Asia/Shanghai 日期；日趋势最多回看 180 天。'
      : '横轴为 Asia/Shanghai 小时；当前小时为未完成桶（最多落后约 15 分钟），最早保留一天可能是部分覆盖。';

    var points = data.points;
    var maxRequests = niceMax(Math.max.apply(null, points.map(function (p) { return p.requests || 0; })));
    var p95Values = points
      .filter(function (p) { return p.p95_ms !== null && p.p95_ms !== undefined; })
      .map(function (p) { return p.p95_ms; });
    var maxP95 = niceMax(p95Values.length ? Math.max.apply(null, p95Values) : 0);
    var plotW = CHART.W - CHART.LEFT - CHART.RIGHT;
    var plotH = CHART.H - CHART.TOP - CHART.BOTTOM;
    var step = plotW / points.length;
    var bottom = CHART.TOP + plotH;

    var svg = svgEl('svg');
    svg.setAttribute('viewBox', '0 0 ' + CHART.W + ' ' + CHART.H);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', daily ? '接口日趋势图' : '接口小时趋势图');

    function yRequests(value) { return bottom - (value / maxRequests) * plotH; }
    function yP95(value) { return bottom - (value / maxP95) * plotH; }

    for (var tick = 0; tick <= 4; tick++) {
      var reqValue = maxRequests * tick / 4;
      var y = yRequests(reqValue);
      if (tick > 0) {
        var grid = svgEl('line');
        grid.setAttribute('x1', CHART.LEFT);
        grid.setAttribute('x2', CHART.W - CHART.RIGHT);
        grid.setAttribute('y1', y);
        grid.setAttribute('y2', y);
        grid.setAttribute('class', 'chart-grid');
        svg.appendChild(grid);
      }
      svg.appendChild(svgText(CHART.LEFT - 8, y + 4, PC.number(Math.round(reqValue)), 'end', 'chart-tick'));
      var p95Value = maxP95 * tick / 4;
      svg.appendChild(svgText(CHART.W - CHART.RIGHT + 8, y + 4, PC.number(Math.round(p95Value)) + 'ms', 'start', 'chart-tick'));
    }

    var labelEvery = Math.ceil(points.length / 12);
    var linePoints = [];
    points.forEach(function (point, index) {
      var bucketX = CHART.LEFT + index * step;
      var barWidth = Math.max(1, step * 0.68);
      var barX = bucketX + (step - barWidth) / 2;
      var centre = bucketX + step / 2;
      var total = point.requests || 0;
      var errors = Math.min(point.errors || 0, total);
      if (total > 0) {
        var bar = svgEl('rect');
        bar.setAttribute('x', barX);
        bar.setAttribute('y', yRequests(total));
        bar.setAttribute('width', barWidth);
        bar.setAttribute('height', bottom - yRequests(total));
        bar.setAttribute('class', 'chart-bar');
        svg.appendChild(bar);
        if (errors > 0) {
          var errorBar = svgEl('rect');
          errorBar.setAttribute('x', barX);
          errorBar.setAttribute('y', yRequests(errors));
          errorBar.setAttribute('width', barWidth);
          errorBar.setAttribute('height', bottom - yRequests(errors));
          errorBar.setAttribute('class', 'chart-bar-error');
          svg.appendChild(errorBar);
        }
      }
      if (point.p95_ms !== null && point.p95_ms !== undefined) {
        linePoints.push(centre.toFixed(1) + ',' + yP95(point.p95_ms).toFixed(1));
      }
      if (index % labelEvery === 0) {
        var label = bucketLabel(granularity, point);
        svg.appendChild(svgText(centre, CHART.H - 10, daily ? label.slice(5) : label.slice(11) + ':00', 'middle', 'chart-tick'));
      }
      var hover = svgEl('rect');
      hover.setAttribute('x', bucketX);
      hover.setAttribute('y', CHART.TOP);
      hover.setAttribute('width', step);
      hover.setAttribute('height', plotH);
      hover.setAttribute('class', 'chart-hover');
      var title = svgEl('title');
      title.textContent = bucketTooltip(granularity, point);
      hover.appendChild(title);
      svg.appendChild(hover);
    });

    if (linePoints.length) {
      var p95Line = svgEl('polyline');
      p95Line.setAttribute('points', linePoints.join(' '));
      p95Line.setAttribute('class', 'chart-line');
      svg.appendChild(p95Line);
    }
    host.appendChild(svg);
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
  function loadEndpoints(page) {
    var params = new URLSearchParams();
    params.set('window_hours', PC.el('apiperf-window-hours').value || '24');
    params.set('traffic_class', PC.el('apiperf-traffic').value);
    params.set('sort', PC.el('apiperf-sort').value);
    params.set('page_size', '50');
    params.set('page', String(page || state.page || 1));
    if (PC.el('apiperf-method').value) { params.set('method', PC.el('apiperf-method').value); }
    if (PC.el('apiperf-q').value) { params.set('q', PC.el('apiperf-q').value); }
    return PC.request('/api/platform/api-performance/endpoints?' + params.toString()).then(renderEndpoints);
  }
  function loadTrend() {
    var params = new URLSearchParams();
    if (state.chartTab === 'daily') {
      params.set('granularity', 'daily');
      params.set('days', PC.el('apiperf-daily-days').value || '30');
    } else {
      var day = PC.el('apiperf-hourly-date').value;
      if (!day) {
        PC.el('apiperf-trend-note').textContent = '';
        var host = PC.el('apiperf-chart');
        PC.clear(host);
        var hint = document.createElement('p');
        hint.className = 'muted';
        hint.textContent = '选择日期后查看小时趋势（最近 720 小时内；最早一天可能为部分覆盖）。';
        host.appendChild(hint);
        return Promise.resolve();
      }
      params.set('granularity', 'hourly');
      params.set('date', day);
    }
    if (PC.el('apiperf-trend-method').value) { params.set('method', PC.el('apiperf-trend-method').value.trim().toUpperCase()); }
    if (PC.el('apiperf-trend-route').value) { params.set('route', PC.el('apiperf-trend-route').value.trim()); }
    return PC.request('/api/platform/api-performance/series?' + params.toString())
      .then(function (data) {
        renderSeriesChart(state.chartTab, data);
        var warn = PC.el('apiperf-trend-incomplete');
        if (data.incomplete) {
          setIncomplete(warn, '数据超出单次查询上限，以上为部分聚合，可能偏小；请缩小天数范围或按接口过滤。');
        } else {
          warn.hidden = true;
        }
      })
      .catch(function (error) {
        var host = PC.el('apiperf-chart');
        PC.clear(host);
        var failure = document.createElement('p');
        failure.className = 'muted';
        failure.textContent = error.message || '加载失败';
        host.appendChild(failure);
      });
  }

  function setChartTab(tab) {
    state.chartTab = tab;
    var dailyTab = PC.el('apiperf-tab-daily');
    var hourlyTab = PC.el('apiperf-tab-hourly');
    dailyTab.classList.toggle('active', tab === 'daily');
    hourlyTab.classList.toggle('active', tab === 'hourly');
    dailyTab.setAttribute('aria-selected', tab === 'daily' ? 'true' : 'false');
    hourlyTab.setAttribute('aria-selected', tab === 'hourly' ? 'true' : 'false');
    PC.el('apiperf-days-field').hidden = tab !== 'daily';
    PC.el('apiperf-date-field').hidden = tab !== 'hourly';
    loadTrend().catch(showError);
  }

  function refreshAll() {
    clearError();
    return Promise.all([
      loadStatus(), loadAnomalies(), loadEndpoints(1), loadTrend()
    ]).catch(showError);
  }

  function initialiseDates() {
    PC.el('apiperf-hourly-date').value = shanghaiToday();
  }

  document.addEventListener('DOMContentLoaded', function () {
    initialiseDates();
    PC.el('apiperf-tab-daily').addEventListener('click', function () { setChartTab('daily'); });
    PC.el('apiperf-tab-hourly').addEventListener('click', function () { setChartTab('hourly'); });
    PC.el('apiperf-trend-form').addEventListener('submit', function (event) {
      event.preventDefault();
      loadTrend().catch(showError);
    });
    PC.el('apiperf-filters').addEventListener('submit', function (event) {
      event.preventDefault();
      state.page = 1;
      loadEndpoints(1).catch(showError);
    });
    PC.el('apiperf-page-prev').addEventListener('click', function () {
      if (state.page > 1) { state.page -= 1; loadEndpoints(state.page).catch(showError); }
    });
    PC.el('apiperf-page-next').addEventListener('click', function () {
      if (state.page < state.totalPages) { state.page += 1; loadEndpoints(state.page).catch(showError); }
    });
    PC.wireRefreshStamp(refreshAll);
    refreshAll();
  });
}());
