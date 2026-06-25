/* PackCache Test Harness — Alpine.js frontend */

const APPROACH_COLORS = {
  naive:      { bg: '#3b82f6', border: '#1d4ed8' },
  blobless:   { bg: '#f97316', border: '#c2410c' },
  agentcache: { bg: '#22c55e', border: '#15803d' },
};

const FMT = {
  bytes(b) {
    b = Number(b) || 0;
    const units = ['B','KiB','MiB','GiB'];
    let u = 0;
    while (b >= 1024 && u < units.length - 1) { b /= 1024; u++; }
    return `${b.toFixed(1)} ${units[u]}`;
  },
  seconds(s) {
    s = Number(s) || 0;
    return `${s.toFixed(3)}s`;
  },
  ms(ms) {
    ms = Number(ms) || 0;
    if (ms >= 1000) return `${(ms/1000).toFixed(2)}s`;
    return `${ms.toFixed(0)}ms`;
  },
  number(n) {
    return (Number(n) || 0).toLocaleString();
  },
};

function app() {
  return {
    // ── state ────────────────────────────────────────────────────────────
    view: 'dashboard',
    status: { git_daemon: false, git_daemon_port: 9418, proxy: false, proxy_port: 9419,
               agentcache_service: false, agentcache_port: 8765, repos: [],
               docker_available: false },
    dbStats: {},
    recentRuns: [],
    historyRuns: [],
    currentRun: null,
    logLines: [],
    savings: null,
    formError: '',

    // Preset example scenarios — shown in the New Test form
    presets: [
      {
        label: 'CPython — asyncio + ast (realistic agent task)',
        repo_name: 'cpython.git',
        branch: 'main',
        paths: 'Lib/asyncio/tasks.py\nLib/asyncio/base_events.py\nLib/ast.py',
      },
      {
        label: 'CPython — single C file (compiler internals)',
        repo_name: 'cpython.git',
        branch: 'main',
        paths: 'Python/ceval.c',
      },
      {
        label: 'CPython — pathlib (pure-Python refactor task)',
        repo_name: 'cpython.git',
        branch: 'main',
        paths: 'Lib/pathlib/_local.py\nLib/pathlib/_abc.py',
      },
      {
        label: 'CPython — broad sweep (10 files, stress test)',
        repo_name: 'cpython.git',
        branch: 'main',
        paths: 'Lib/asyncio/tasks.py\nLib/asyncio/base_events.py\nLib/ast.py\nLib/pathlib/_local.py\nLib/typing.py\nLib/dataclasses.py\nPython/ceval.c\nInclude/cpython/object.h\nObjects/typeobject.c\nModules/_io/bufferedio.c',
      },
    ],

    form: {
      repo_name: 'cpython.git',
      branch: 'main',
      target_paths_text: 'Lib/asyncio/tasks.py\nLib/asyncio/base_events.py\nLib/ast.py',
      approaches: ['naive', 'blobless', 'agentcache'],
      num_runs: 3,
      use_docker: true,
      latency_ms: 0,
    },

    _sse: null,
    _chartTime: null,
    _chartBytes: null,
    _chartPhase: null,
    _chartCpu: null,
    _chartNet: null,
    _statusTimer: null,

    // ── init ──────────────────────────────────────────────────────────────
    async init() {
      await this.refreshStatus();
      await this.loadHistory();
      this._statusTimer = setInterval(() => this.refreshStatus(), 5000);
    },

    // ── status & data ─────────────────────────────────────────────────────
    async refreshStatus() {
      try {
        const [st, runs] = await Promise.all([
          fetch('/api/status').then(r => r.json()),
          fetch('/api/runs').then(r => r.json()),
        ]);
        this.status = st;
        this.recentRuns = (runs.runs || []).slice(0, 8);
        this.historyRuns = runs.runs || [];
        // Update docker toggle label if status changed
        this.form.use_docker = st.docker_available;
      } catch (e) { /* network down — ignore */ }
    },

    async loadHistory() {
      try {
        const d = await fetch('/api/runs').then(r => r.json());
        this.historyRuns = d.runs || [];
        this.recentRuns = this.historyRuns.slice(0, 8);
      } catch(e) {}
    },

    // ── presets ───────────────────────────────────────────────────────────
    applyPreset(p) {
      this.form.repo_name = p.repo_name;
      this.form.branch    = p.branch;
      this.form.target_paths_text = p.paths;
    },

    // ── new test ──────────────────────────────────────────────────────────
    async startRun() {
      this.formError = '';
      const paths = this.form.target_paths_text.split('\n').map(s => s.trim()).filter(Boolean);
      if (!paths.length) { this.formError = 'Enter at least one target path.'; return; }
      if (!this.form.repo_name)    { this.formError = 'Select a repository.'; return; }
      if (!this.form.approaches.length) { this.formError = 'Select at least one approach.'; return; }

      const body = {
        repo_name: this.form.repo_name,
        branch: this.form.branch || 'master',
        target_paths: paths,
        approaches: [...this.form.approaches],
        num_runs: Number(this.form.num_runs) || 3,
        use_docker: this.form.use_docker,
        latency_ms: Number(this.form.latency_ms) || 0,
      };

      let data;
      try {
        const r = await fetch('/api/runs', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify(body),
        });
        data = await r.json();
        if (!r.ok) { this.formError = data.detail || 'Failed to start run.'; return; }
      } catch(e) { this.formError = String(e); return; }

      await this.openRun(data.run_id);
    },

    // ── run detail ────────────────────────────────────────────────────────
    async openRun(runId) {
      this.logLines = [];
      this.savings = null;
      this._destroyCharts();

      const run = await fetch(`/api/runs/${runId}`).then(r => r.json());
      this.currentRun = run;
      this.view = 'run';

      if (run.status === 'running') {
        this._subscribeSSE(runId);
      } else if (run.status === 'complete') {
        this._computeSavings(run.results);
        await this.$nextTick();
        this._renderCharts(run.results);
      }
    },

    _subscribeSSE(runId) {
      if (this._sse) { this._sse.close(); this._sse = null; }

      const es = new EventSource(`/api/runs/${runId}/stream`);
      this._sse = es;

      es.onmessage = (e) => {
        let event;
        try { event = JSON.parse(e.data); } catch { return; }

        switch (event.type) {
          case 'log':
            this.logLines.push(event.msg);
            this.$nextTick(() => {
              const c = document.getElementById('log-container');
              if (c) c.scrollTop = c.scrollHeight;
            });
            break;

          case 'approach_start':
            this.logLines.push(`\n── ${event.approach} (${event.total_runs} run(s)) ──`);
            break;

          case 'approach_done':
            if (this.currentRun) {
              if (!this.currentRun.results) this.currentRun.results = [];
              // Update or add result
              const idx = this.currentRun.results.findIndex(r => r.approach === event.approach);
              if (idx >= 0) this.currentRun.results[idx] = event.result;
              else this.currentRun.results.push(event.result);
            }
            break;

          case 'run_complete':
            if (this.currentRun) {
              this.currentRun.status = 'complete';
              this.currentRun.results = event.results;
              this._computeSavings(event.results);
              this.$nextTick(() => this._renderCharts(event.results));
            }
            this.loadHistory();
            break;

          case 'error':
            this.logLines.push(`ERROR: ${event.msg}`);
            if (this.currentRun) this.currentRun.status = 'error';
            this.loadHistory();
            break;

          case 'stream_end':
          case 'ping':
            break;
        }
      };

      es.onerror = () => {
        if (this._sse === es) { es.close(); this._sse = null; }
        this.loadHistory();
      };
    },

    // ── comparison helpers ─────────────────────────────────────────────────
    comparisonRows() {
      if (!this.currentRun?.results?.length) return [];
      const results = this.currentRun.results;

      const makeRow = (label, getValue, format, lowerIsBetter = true) => {
        const vals = results.map(r => getValue(r));
        const extremeIdx = lowerIsBetter
          ? vals.indexOf(Math.min(...vals.filter(v => v > 0)))
          : vals.indexOf(Math.max(...vals));
        const best = results[extremeIdx]?.approach;
        const maxVal = Math.max(...vals, 1);

        return {
          label,
          best,
          fmt: (r) => format(getValue(r)),
          pct: (r) => Math.round((getValue(r) / maxVal) * 100),
        };
      };

      const rows = [
        makeRow('Wall time',        r => r.elapsed_s,            FMT.seconds),
        makeRow('Bytes ↑ (sent)',    r => r.bytes_proxy_in,       FMT.bytes),
        makeRow('Bytes ↓ (recv)',    r => r.bytes_proxy_out,      FMT.bytes),
        makeRow('Total bytes',       r => r.bytes_proxy_total,    FMT.bytes),
        makeRow('Objects received',  r => r.objects_received,     FMT.number),
        makeRow('Disk usage',        r => r.disk_bytes,           FMT.bytes),
        makeRow('Files on disk',     r => r.file_count,           FMT.number),
      ];

      // Add agent-task rows if any result has agent_task data
      const hasAgent = results.some(r => r.agent_task);
      if (hasAgent) {
        rows.push(
          makeRow('Agent ready',      r => r.agent_task?.total_agent_ready_ms ?? 0, FMT.ms),
          makeRow('Net roundtrips',   r => r.agent_task?.network_roundtrips ?? 0,   FMT.number),
          makeRow('Symbol lookup',    r => r.agent_task?.symbol_lookup_ms ?? 0,     FMT.ms),
        );
      }

      return rows;
    },

    _computeSavings(results) {
      if (!results || results.length < 2) return;
      const naive = results.find(r => r.approach === 'naive');
      const ac    = results.find(r => r.approach === 'agentcache');
      if (!naive || !ac) { this.savings = null; return; }
      this.savings = {
        time_pct: naive.elapsed_s > 0
          ? Math.round((1 - ac.elapsed_s / naive.elapsed_s) * 100) : 0,
        bytes_pct: naive.bytes_proxy_total > 0
          ? Math.round((1 - ac.bytes_proxy_total / naive.bytes_proxy_total) * 100) : 0,
        time_abs: FMT.seconds(naive.elapsed_s - ac.elapsed_s),
        bytes_abs: FMT.bytes(naive.bytes_proxy_total - ac.bytes_proxy_total),
        agent_ready_naive: naive.agent_task?.total_agent_ready_ms,
        agent_ready_ac: ac.agent_task?.total_agent_ready_ms,
      };
    },

    savingsSummary() {
      if (!this.savings) return '';
      return `${this.savings.time_pct}% faster (${this.savings.time_abs} saved) · 
              ${this.savings.bytes_pct}% less bandwidth (${this.savings.bytes_abs} saved) 
              vs naive`;
    },

    phaseResult() {
      return this.currentRun?.results?.find(r => r.approach === 'agentcache' && r.phases);
    },

    fmtS(v) { return FMT.seconds(v ?? 0); },

    // ── "time to productive" cards ─────────────────────────────────────────
    agentCards() {
      if (!this.currentRun?.results?.length) return [];
      return this.currentRun.results
        .filter(r => r.agent_task)
        .map(r => ({
          approach: r.approach,
          total_ms: r.agent_task.total_agent_ready_ms,
          symbol_ms: r.agent_task.symbol_lookup_ms,
          file_ms: r.agent_task.file_read_ms,
          roundtrips: r.agent_task.network_roundtrips,
          grep_cpu: r.agent_task.grep_cpu_pct,
          used_docker: r.used_docker,
        }));
    },

    // ── charts ──────────────────────────────────────────────────────────────
    _destroyCharts() {
      if (this._chartTime)  { this._chartTime.destroy();  this._chartTime  = null; }
      if (this._chartBytes) { this._chartBytes.destroy(); this._chartBytes = null; }
      if (this._chartPhase) { this._chartPhase.destroy(); this._chartPhase = null; }
      if (this._chartCpu)   { this._chartCpu.destroy();   this._chartCpu   = null; }
      if (this._chartNet)   { this._chartNet.destroy();   this._chartNet   = null; }
    },

    _renderCharts(results) {
      if (!results?.length) return;

      const labels    = results.map(r => r.approach);
      const colors    = results.map(r => (APPROACH_COLORS[r.approach] || APPROACH_COLORS.naive).bg);
      const borders   = results.map(r => (APPROACH_COLORS[r.approach] || APPROACH_COLORS.naive).border);

      const chartOpts = {
        responsive: true,
        plugins: { legend: { display: false }, tooltip: { mode: 'index' } },
        scales: {
          x: { ticks: { color: '#9ca3af' }, grid: { color: '#374151' } },
          y: { ticks: { color: '#9ca3af' }, grid: { color: '#374151' }, beginAtZero: true },
        },
      };

      // Time chart
      const ctxTime = document.getElementById('chartTime');
      if (ctxTime) {
        this._chartTime = new Chart(ctxTime, {
          type: 'bar',
          data: {
            labels,
            datasets: [{
              label: 'seconds',
              data: results.map(r => r.elapsed_s),
              backgroundColor: colors,
              borderColor: borders,
              borderWidth: 1,
              borderRadius: 4,
            }],
          },
          options: { ...chartOpts, plugins: { ...chartOpts.plugins, tooltip: {
            callbacks: { label: ctx => `${ctx.parsed.y.toFixed(3)}s` }
          }}},
        });
      }

      // Bytes chart — show proxy_out (bytes received from server = pack data)
      const ctxBytes = document.getElementById('chartBytes');
      if (ctxBytes) {
        this._chartBytes = new Chart(ctxBytes, {
          type: 'bar',
          data: {
            labels,
            datasets: [
              {
                label: 'recv (↓)',
                data: results.map(r => r.bytes_proxy_out),
                backgroundColor: colors.map(c => c + 'cc'),
                borderColor: borders,
                borderWidth: 1,
                borderRadius: 4,
              },
              {
                label: 'sent (↑)',
                data: results.map(r => r.bytes_proxy_in),
                backgroundColor: colors.map(c => c + '66'),
                borderColor: borders,
                borderWidth: 1,
                borderRadius: 4,
              },
            ],
          },
          options: { ...chartOpts, plugins: { ...chartOpts.plugins, legend: { display: true,
            labels: { color: '#9ca3af', boxWidth: 12 } },
            tooltip: {
              callbacks: {
                label: ctx => `${ctx.dataset.label}: ${FMT.bytes(ctx.parsed.y)}`
              }
            }
          }},
        });
      }

      // Phase timeline chart (stacked horizontal bar: clone + symbol_lookup + file_read)
      this._renderPhaseChart(results);

      // CPU timeseries
      this._renderCpuChart(results);

      // Network rate timeseries
      this._renderNetChart(results);
    },

    _renderPhaseChart(results) {
      const ctxPhase = document.getElementById('chartPhase');
      if (!ctxPhase) return;
      const hasAgent = results.some(r => r.agent_task);
      if (!hasAgent) return;

      const labels = results.map(r => r.approach);
      const cloneData   = results.map(r => r.clone_ms || (r.elapsed_s * 1000) || 0);
      const symbolData  = results.map(r => r.agent_task?.symbol_lookup_ms || 0);
      const fileData    = results.map(r => r.agent_task?.file_read_ms || 0);

      this._chartPhase = new Chart(ctxPhase, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            {
              label: 'Clone (ms)',
              data: cloneData,
              backgroundColor: labels.map(a => (APPROACH_COLORS[a] || APPROACH_COLORS.naive).bg + 'cc'),
              borderWidth: 0,
              borderRadius: 4,
            },
            {
              label: 'Symbol lookup (ms)',
              data: symbolData,
              backgroundColor: '#f59e0b99',
              borderWidth: 0,
            },
            {
              label: 'File read (ms)',
              data: fileData,
              backgroundColor: '#14b8a699',
              borderWidth: 0,
            },
          ],
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          plugins: {
            legend: { display: true, labels: { color: '#9ca3af', boxWidth: 12 } },
            tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.parsed.x.toFixed(0)}ms` } },
          },
          scales: {
            x: { stacked: true, ticks: { color: '#9ca3af' }, grid: { color: '#374151' }, beginAtZero: true,
                 title: { display: true, text: 'milliseconds', color: '#6b7280' } },
            y: { stacked: true, ticks: { color: '#9ca3af' }, grid: { color: '#374151' } },
          },
        },
      });
    },

    _renderCpuChart(results) {
      const ctxCpu = document.getElementById('chartCpu');
      if (!ctxCpu) return;
      const hasTs = results.some(r => r.timeseries?.length > 0);
      if (!hasTs) return;

      const datasets = results.map(r => {
        const c = (APPROACH_COLORS[r.approach] || APPROACH_COLORS.naive);
        return {
          label: r.approach,
          data: (r.timeseries || []).map(p => ({ x: p.t_ms / 1000, y: p.cpu_pct })),
          borderColor: c.border,
          backgroundColor: c.bg + '33',
          pointRadius: 0,
          tension: 0.2,
          fill: false,
          parsing: false,
        };
      }).filter(ds => ds.data.length > 0);

      if (!datasets.length) return;

      this._chartCpu = new Chart(ctxCpu, {
        type: 'line',
        data: { datasets },
        options: {
          responsive: true,
          animation: false,
          plugins: {
            legend: { display: true, labels: { color: '#9ca3af', boxWidth: 12 } },
            tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}%` } },
          },
          scales: {
            x: { type: 'linear', ticks: { color: '#9ca3af' }, grid: { color: '#374151' },
                 title: { display: true, text: 'time (s)', color: '#6b7280' } },
            y: { ticks: { color: '#9ca3af' }, grid: { color: '#374151' }, beginAtZero: true,
                 title: { display: true, text: 'CPU % (100=1 core)', color: '#6b7280' } },
          },
        },
      });
    },

    _renderNetChart(results) {
      const ctxNet = document.getElementById('chartNet');
      if (!ctxNet) return;
      const hasTs = results.some(r => r.timeseries?.length > 0);
      if (!hasTs) return;

      const INTERVAL_S = 0.2;  // matches proxy sample interval

      const datasets = results.map(r => {
        const c = (APPROACH_COLORS[r.approach] || APPROACH_COLORS.naive);
        return {
          label: r.approach,
          data: (r.timeseries || []).map(p => ({
            x: p.t_ms / 1000,
            y: (p.bytes_out + p.bytes_in) / INTERVAL_S,
          })),
          borderColor: c.border,
          backgroundColor: c.bg + '44',
          pointRadius: 0,
          tension: 0.2,
          fill: true,
          parsing: false,
        };
      }).filter(ds => ds.data.length > 0);

      if (!datasets.length) return;

      this._chartNet = new Chart(ctxNet, {
        type: 'line',
        data: { datasets },
        options: {
          responsive: true,
          animation: false,
          plugins: {
            legend: { display: true, labels: { color: '#9ca3af', boxWidth: 12 } },
            tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${FMT.bytes(ctx.parsed.y)}/s` } },
          },
          scales: {
            x: { type: 'linear', ticks: { color: '#9ca3af' }, grid: { color: '#374151' },
                 title: { display: true, text: 'time (s)', color: '#6b7280' } },
            y: {
              ticks: {
                color: '#9ca3af',
                callback: v => FMT.bytes(v) + '/s',
              },
              grid: { color: '#374151' }, beginAtZero: true,
              title: { display: true, text: 'network rate', color: '#6b7280' },
            },
          },
        },
      });
    },

    // ── styling helpers ────────────────────────────────────────────────────
    statusColor(s) {
      switch (s) {
        case 'complete': return 'bg-green-900 text-green-300';
        case 'running':  return 'bg-blue-900 text-blue-300 animate-pulse';
        case 'error':    return 'bg-red-900 text-red-300';
        default:         return 'bg-gray-700 text-gray-300';
      }
    },

    approachChip(a) {
      const map = { naive:'chip-naive', blobless:'chip-blobless', agentcache:'chip-agentcache' };
      return map[a] || '';
    },

    approachHeaderColor(a) {
      const map = { naive:'text-blue-400', blobless:'text-orange-400', agentcache:'text-green-400' };
      return map[a] || 'text-gray-300';
    },
  };
}
