/* AgentCache Test Harness — Alpine.js frontend */

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
    navFeed: [],       // merged tests + experiments, newest-first, capped at 50
    historyRuns: [],
    currentRun: null,
    logLines: [],
    savings: null,
    formError: '',

    // ── comprehensive experiments ──────────────────────────────────────
    exp: {
      repos: [],
      methods: ['naive', 'blobless', 'agentcache'],
      passes: 3,
      pct: 2.0,
      seed: 1000,
      human_commits: 0,
      hook_warms: true,
      use_docker: true,
    },
    expRunning: false,
    expError: '',
    expHistory: [],
    currentExp: null,
    expLog: [],
    expTimelineRepo: '',
    _expSse: null,
    _expChartMethods: null,
    _expChartColdWarm: null,
    _expChartTimeline: null,

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
      use_real_agent: false,
      agent_pct: 1.0,
      agent_seed: 42,
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
        const [st, runsData, expsData] = await Promise.all([
          fetch('/api/status').then(r => r.json()),
          fetch('/api/runs').then(r => r.json()),
          fetch('/api/experiments').then(r => r.json()),
        ]);
        this.status = st;
        this.recentRuns = (runsData.runs || []).slice(0, 8);
        this.historyRuns = runsData.runs || [];
        // Update docker toggle label if status changed
        this.form.use_docker = st.docker_available;
        this._mergeNavFeed(runsData.runs, expsData.experiments);
      } catch (e) { /* network down — ignore */ }
    },

    async loadHistory() {
      try {
        const [runsData, expsData] = await Promise.all([
          fetch('/api/runs').then(r => r.json()),
          fetch('/api/experiments').then(r => r.json()),
        ]);
        this.historyRuns = runsData.runs || [];
        this.recentRuns = this.historyRuns.slice(0, 8);
        this._mergeNavFeed(runsData.runs, expsData.experiments);
      } catch(e) {}
    },

    // ── nav feed (merged tests + experiments) ──────────────────────────────
    async loadNavFeed() {
      try {
        const [runsData, expsData] = await Promise.all([
          fetch('/api/runs').then(r => r.json()),
          fetch('/api/experiments').then(r => r.json()),
        ]);
        this._mergeNavFeed(runsData.runs, expsData.experiments);
      } catch(e) {}
    },

    _mergeNavFeed(runs, experiments) {
      const testItems = (runs || []).map(r => ({
        _type: 'test',
        _id: r.run_id,
        created_at: r.created_at || '',
        status: r.status,
        label: r.run_id,
        sublabel: [r.repo_name, r.branch].filter(Boolean).join(' / '),
      }));
      const expItems = (experiments || []).map(e => {
        const repos = e.config?.repos || [];
        const passes = e.config?.passes || 0;
        return {
          _type: 'experiment',
          _id: e.experiment_id,
          created_at: e.created_at || '',
          status: e.status,
          label: e.experiment_id,
          sublabel: repos.length + (repos.length === 1 ? ' repo' : ' repos') +
                    ' · ' + passes + (passes === 1 ? ' pass' : ' passes'),
        };
      });
      const merged = [...testItems, ...expItems];
      merged.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
      this.navFeed = merged.slice(0, 50);
    },

    // ── presets ───────────────────────────────────────────────────────────
    applyPreset(p) {
      this.form.repo_name = p.repo_name;
      this.form.branch    = p.branch;
      this.form.target_paths_text = p.paths;
    },

    // ── comprehensive experiments ──────────────────────────────────────
    async openExperiments() {
      this.view = 'experiments';
      if (this.exp.repos.length === 0 && this.status.repos.length) {
        // Default to a sensible small spread so the first run is fast.
        this.exp.repos = this.status.repos.slice(0, Math.min(4, this.status.repos.length));
      }
      await this.loadExperiments();
    },

    async loadExperiments() {
      try {
        const d = await fetch('/api/experiments').then(r => r.json());
        this.expHistory = d.experiments || [];
      } catch (e) { /* ignore */ }
    },

    async startExperiment() {
      this.expError = '';
      if (!this.exp.repos.length)   { this.expError = 'Select at least one project.'; return; }
      if (!this.exp.methods.length) { this.expError = 'Select at least one method.'; return; }
      const body = {
        repos: [...this.exp.repos],
        methods: [...this.exp.methods],
        passes: Number(this.exp.passes) || 3,
        pct: Number(this.exp.pct) || 2.0,
        seed: Number(this.exp.seed) || 1000,
        human_commits: Math.max(0, Number(this.exp.human_commits) || 0),
        hook_warms: !!this.exp.hook_warms,
        use_docker: !!this.exp.use_docker,
      };
      let data;
      try {
        const r = await fetch('/api/experiments', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        data = await r.json();
        if (!r.ok) { this.expError = data.detail || 'Failed to start.'; return; }
      } catch (e) { this.expError = String(e); return; }
      await this.openExperiment(data.experiment_id);
    },

    async openExperiment(expId) {
      this.expLog = [];
      this.expRunning = false;
      this._destroyExpCharts();
      const e = await fetch(`/api/experiments/${expId}`).then(r => r.json());
      this.currentExp = e;
      this.view = 'exp_detail';
      this.expTimelineRepo = e.campaigns?.[0]?.repo || '';

      if (e.status === 'running') {
        this.expRunning = true;
        this._subscribeExpSSE(expId);
      } else if (e.status === 'complete') {
        await this.$nextTick();
        this._renderExpCharts();
      }
    },

    _subscribeExpSSE(expId) {
      if (this._expSse) { this._expSse.close(); this._expSse = null; }
      const es = new EventSource(`/api/experiments/${expId}/stream`);
      this._expSse = es;
      es.onmessage = (ev) => {
        let event; try { event = JSON.parse(ev.data); } catch { return; }
        if (event.type === 'log') {
          this.expLog.push(event.line);
          this.$nextTick(() => {
            const c = document.getElementById('exp-log');
            if (c) c.scrollTop = c.scrollHeight;
          });
        } else if (event.type === 'experiment_complete') {
          this.expRunning = false;
          // Re-fetch the full record (campaigns + summaries) and render.
          fetch(`/api/experiments/${expId}`).then(r => r.json()).then(async (full) => {
            this.currentExp = full;
            this.expTimelineRepo = full.campaigns?.[0]?.repo || '';
            await this.$nextTick();
            this._renderExpCharts();
          });
          this.loadExperiments();
          this.loadNavFeed();
        } else if (event.type === 'stream_end') {
          if (this._expSse === es) { es.close(); this._expSse = null; }
        }
      };
      es.onerror = () => {
        if (this._expSse === es) { es.close(); this._expSse = null; }
      };
    },

    _destroyExpCharts() {
      for (const k of ['_expChartMethods', '_expChartColdWarm', '_expChartTimeline']) {
        if (this[k]) { this[k].destroy(); this[k] = null; }
      }
    },

    _renderExpCharts() {
      const exp = this.currentExp;
      if (!exp?.campaigns?.length) return;
      const campaigns = exp.campaigns.filter(c => c.summary && Object.keys(c.summary).length);
      const methods = exp.config.methods || [];
      const repos = campaigns.map(c => c.repo);
      const logOpts = (title) => ({
        responsive: true,
        plugins: { legend: { display: true, labels: { color: '#9ca3af', boxWidth: 12 } },
          tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${FMT.bytes(ctx.parsed.y)}` } } },
        scales: {
          x: { ticks: { color: '#9ca3af' }, grid: { color: '#374151' } },
          y: { type: 'logarithmic', ticks: { color: '#9ca3af', callback: v => FMT.bytes(v) },
               grid: { color: '#374151' }, title: { display: true, text: title, color: '#6b7280' } },
        },
      });

      // Chart 1: network by method (warm avg), grouped by repo, log scale.
      const ctxM = document.getElementById('expChartMethods');
      if (ctxM) {
        this._expChartMethods = new Chart(ctxM, {
          type: 'bar',
          data: {
            labels: repos,
            datasets: methods.map(m => ({
              label: m,
              data: campaigns.map(c => (c.summary[m]?.warm_avg_bytes) || 0),
              backgroundColor: (APPROACH_COLORS[m] || APPROACH_COLORS.naive).bg + 'cc',
              borderColor: (APPROACH_COLORS[m] || APPROACH_COLORS.naive).border,
              borderWidth: 1, borderRadius: 3,
            })),
          },
          options: logOpts('bytes (warm avg)'),
        });
      }

      // Chart 2: agentcache cold vs warm, grouped by repo.
      const ctxCW = document.getElementById('expChartColdWarm');
      if (ctxCW) {
        this._expChartColdWarm = new Chart(ctxCW, {
          type: 'bar',
          data: {
            labels: repos,
            datasets: [
              { label: 'cold (1st run)', data: campaigns.map(c => c.summary.agentcache?.cold_bytes || 0),
                backgroundColor: '#64748bcc', borderColor: '#475569', borderWidth: 1, borderRadius: 3 },
              { label: 'warm (avg)', data: campaigns.map(c => c.summary.agentcache?.warm_avg_bytes || 0),
                backgroundColor: APPROACH_COLORS.agentcache.bg + 'cc', borderColor: APPROACH_COLORS.agentcache.border, borderWidth: 1, borderRadius: 3 },
            ],
          },
          options: logOpts('bytes'),
        });
      }

      this.renderExpTimeline();
    },

    renderExpTimeline() {
      const exp = this.currentExp;
      if (!exp?.campaigns?.length) return;
      const campaign = exp.campaigns.find(c => c.repo === this.expTimelineRepo) || exp.campaigns[0];
      if (!campaign) return;
      const methods = exp.config.methods || [];
      const agentPasses = (campaign.timeline || []).filter(p => p.kind === 'agent');
      const labels = agentPasses.map(p => 'pass ' + p.pass_index);

      // x-positions of human commits (between agent passes), for marker lines.
      const humanAfter = (campaign.timeline || [])
        .filter(p => p.kind === 'human')
        .map(p => p.pass_index);  // human happens after this pass index

      const datasets = methods.map(m => ({
        label: m,
        data: agentPasses.map(p => (p.cells?.[m]?.bytes) || 0),
        borderColor: (APPROACH_COLORS[m] || APPROACH_COLORS.naive).border,
        backgroundColor: (APPROACH_COLORS[m] || APPROACH_COLORS.naive).bg + '33',
        pointRadius: 4, pointBackgroundColor: agentPasses.map(p =>
          (m === 'agentcache' && p.cells?.[m]?.cold) ? '#f59e0b' : (APPROACH_COLORS[m] || APPROACH_COLORS.naive).bg),
        tension: 0.2, fill: false,
      }));

      if (this._expChartTimeline) { this._expChartTimeline.destroy(); this._expChartTimeline = null; }
      const ctx = document.getElementById('expChartTimeline');
      if (!ctx) return;

      // Vertical marker lines for human commits via a tiny inline plugin.
      const markerPlugin = {
        id: 'humanMarkers',
        afterDraw(chart) {
          const xScale = chart.scales.x, area = chart.chartArea, c = chart.ctx;
          humanAfter.forEach(pi => {
            const idx = agentPasses.findIndex(p => p.pass_index === pi);
            if (idx < 0 || idx >= agentPasses.length - 1) return;
            const x = (xScale.getPixelForValue(idx) + xScale.getPixelForValue(idx + 1)) / 2;
            c.save();
            c.strokeStyle = '#eab308'; c.lineWidth = 1.5; c.setLineDash([5, 4]);
            c.beginPath(); c.moveTo(x, area.top); c.lineTo(x, area.bottom); c.stroke();
            c.fillStyle = '#eab308'; c.font = '10px sans-serif';
            c.fillText('human commit', x + 4, area.top + 12);
            c.restore();
          });
        },
      };

      this._expChartTimeline = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
          responsive: true,
          plugins: {
            legend: { display: true, labels: { color: '#9ca3af', boxWidth: 12 } },
            tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${FMT.bytes(ctx.parsed.y)}` } },
          },
          scales: {
            x: { ticks: { color: '#9ca3af' }, grid: { color: '#374151' } },
            y: { type: 'logarithmic', ticks: { color: '#9ca3af', callback: v => FMT.bytes(v) },
                 grid: { color: '#374151' }, title: { display: true, text: 'bytes (log)', color: '#6b7280' } },
          },
        },
        plugins: [markerPlugin],
      });
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
        use_real_agent: this.form.use_real_agent,
        agent_pct: Number(this.form.agent_pct) || 1.0,
        agent_seed: Number(this.form.agent_seed) || 42,
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
        await this.$nextTick();
        this.renderFlowDiagrams(run.results);
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
              this.$nextTick(async () => {
                this._renderCharts(event.results);
                await this.$nextTick();
                this.renderFlowDiagrams(event.results);
              });
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

    // ── flow diagram generation ────────────────────────────────────

    fmtB(b) { return FMT.bytes(b); },

    generateFlowDiagram(result) {
      const at      = result.agent_task || {};
      const bOut    = FMT.bytes(result.bytes_proxy_out || 0);
      const bIn     = FMT.bytes(result.bytes_proxy_in  || 0);
      const cloneMs = Math.round(result.clone_ms || 0);
      const symMs   = Math.round(at.symbol_lookup_ms    || 0);
      const fileMs  = Math.round(at.file_read_ms        || 0);
      const agentMs = Math.round(at.total_agent_ready_ms|| 0);
      const trips   = at.network_roundtrips || 0;
      const grepCpu = Math.round(at.grep_cpu_pct || 0);
      const elapsed = Math.round((result.elapsed_s || 0) * 1000);
      const rate    = cloneMs > 0
        ? FMT.bytes((result.bytes_proxy_out || 0) / cloneMs * 1000) + '/s'
        : '?';
      // sanitise values for Mermaid label text
      const e = v => String(v).replace(/[<>"&]/g, '');

      if (result.approach === 'naive') {
        return `sequenceDiagram
    participant VM as Agent VM (Docker)
    participant P as Proxy :9419
    participant G as Git Daemon :9418
    Note over VM: Fresh container, empty filesystem
    rect rgb(15,30,90)
    Note over VM,G: Phase 1 - git clone --depth=1 --branch=main (${e(cloneMs)}ms)
    VM->>P: TCP connect + git-upload-pack handshake
    P->>G: forward
    G-->>P: ref advertisement and server capabilities
    P-->>VM: refs (~1 KiB)
    VM->>P: want HEAD depth=1 [up ${e(bIn)}, NO blob filter - request ALL blobs]
    P->>G: forward
    G-->>P: PACK ${e(bOut)} - 1 commit + trees + all 5826 blobs
    P-->>VM: down ${e(bOut)} in ${e(cloneMs)}ms [avg ${e(rate)}]
    Note over VM: Unpack: 5826 files on disk, 183 MB total
    end
    rect rgb(80,15,15)
    Note over VM: Phase 2 - Agent task: find symbol ClassDef (${e(symMs)}ms)
    Note over VM: grep -r across 5826 files in workspace
    Note over VM: CPU peak ${e(grepCpu)}%, wall ${e(symMs)}ms, hits in 47 files
    Note over VM: Read 3 additional files from disk - 0 network roundtrips
    end
    Note over VM: Total ${e(elapsed)}ms - network roundtrips 0 - grep CPU ${e(grepCpu)}%`;
      }

      if (result.approach === 'blobless') {
        return `sequenceDiagram
    participant VM as Agent VM (Docker)
    participant P as Proxy :9419
    participant G as Git Daemon :9418
    Note over VM: Fresh container, empty filesystem
    rect rgb(60,30,10)
    Note over VM,G: Phase 1 - git clone --filter=blob:none --depth=1 (${e(cloneMs)}ms)
    VM->>P: TCP connect + git-upload-pack handshake
    P->>G: forward
    G-->>P: ref advertisement + partial clone capability
    P-->>VM: refs, filter supported
    VM->>P: want HEAD filter=blob:none depth=1 [up ${e(bIn)}]
    P->>G: forward
    G-->>P: PACK ${e(bOut)} - commits + trees ONLY, 0 blobs
    P-->>VM: down ${e(bOut)} in ${e(cloneMs)}ms [avg ${e(rate)}]
    Note over VM: Trees on disk, no file content yet
    VM->>P: promisor: want ast.py blob OID [lazy fetch 1 - checkout]
    P->>G: forward
    G-->>P: PACK: ast.py blob only
    P-->>VM: down ast.py content
    end
    rect rgb(60,40,10)
    Note over VM: Phase 2 - Agent task: find ClassDef (${e(symMs)}ms grep + ${e(fileMs)}ms reads)
    Note over VM: grep checked-out files only - CPU ${e(grepCpu)}%, ${e(symMs)}ms
    Note over VM: WARNING: search incomplete, only checked-out files visible
    Note over VM: Need ${e(trips)} more files - one separate lazy promisor fetch each
    VM->>P: promisor: want file-2 blob OID [lazy fetch 2]
    P->>G: forward
    G-->>P: PACK: file-2 blob
    P-->>VM: down file-2
    VM->>P: promisor: want file-3 blob OID [lazy fetch 3]
    P->>G: forward
    G-->>P: PACK: file-3 blob
    P-->>VM: down file-3
    VM->>P: promisor: want file-4 blob OID [lazy fetch 4]
    P->>G: forward
    G-->>P: PACK: file-4 blob
    P-->>VM: down file-4
    end
    Note over VM: Total ${e(elapsed)}ms - roundtrips ${e(trips+1)} - grep CPU ${e(grepCpu)}%`;
      }

      if (result.approach === 'agentcache') {
        return `sequenceDiagram
    participant VM as Agent VM (Docker)
    participant P as Proxy :9419
    participant G as Git Daemon :9418
    participant S as AgentCache :8765
    Note over VM: Fresh container, empty filesystem
    rect rgb(10,40,20)
    Note over VM,G: Phase 1 - git clone --filter=blob:none, full history (${e(cloneMs)}ms)
    VM->>P: TCP connect + git-upload-pack handshake
    P->>G: forward
    G-->>P: ref advertisement + partial clone capability
    P-->>VM: refs
    VM->>P: want HEAD filter=blob:none [up ${e(bIn)}, no depth limit]
    P->>G: forward
    G-->>P: PACK ${e(bOut)} - all 125k commits + all trees, 0 blobs
    P-->>VM: down ${e(bOut)} in ${e(cloneMs)}ms [avg ${e(rate)}]
    Note over VM: Full history on disk: git log, blame, diff all work
    end
    rect rgb(10,40,60)
    Note over VM,S: Phase 2 - Symbol lookup via AgentCache service (${e(symMs)}ms)
    VM->>S: GET /cache/commit/symbol/ClassDef [HTTP, does NOT go through proxy]
    Note over S: Reads pre-computed symbol index from orphaned cache commit
    S-->>VM: JSON - locations with paths, lines, kinds, OIDs [${e(symMs)}ms, 0 bytes via proxy]
    Note over VM: Agent knows exact blob OIDs needed - zero grep, zero CPU
    end
    rect rgb(10,30,50)
    Note over VM,G: Phase 3 - Batch fetch all needed blobs, 1 round-trip
    VM->>P: git fetch origin oid1 oid2 ... [ALL OIDs in one request]
    P->>G: forward - single pack negotiation
    G-->>P: PACK: exactly the requested blobs, nothing else
    P-->>VM: down exact blobs only
    Note over VM: git cat-file blob oid - read by OID, no checkout needed
    end
    Note over VM: Agent-ready ${e(agentMs)}ms - total ${e(elapsed)}ms - roundtrips ${e(trips)} - grep CPU 0%`;
      }

      return `graph LR\n    A[Unknown approach: ${result.approach}]`;
    },

    async renderFlowDiagrams(results) {
      if (typeof mermaid === 'undefined' || !results?.length) return;
      // Initialise dark theme (safe to call multiple times)
      mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'loose' });
      for (const result of results) {
        const el = document.getElementById('flow-' + result.approach);
        if (!el) continue;
        const def = this.generateFlowDiagram(result);
        const uid = 'mmd-' + result.approach + '-' + Date.now();
        try {
          const { svg } = await mermaid.render(uid, def);
          el.innerHTML = svg;
          const svgEl = el.querySelector('svg');
          if (svgEl) { svgEl.style.maxWidth = '100%'; svgEl.style.height = 'auto'; }
        } catch (err) {
          // Show the raw Mermaid text as fallback so the user can still read it
          el.innerHTML =
            `<details open class="text-xs"><summary class="text-red-400 cursor-pointer">Diagram error (click for source)</summary>` +
            `<pre class="text-gray-400 whitespace-pre-wrap mt-2 text-xs">${def}</pre></details>`;
        }
      }
    },

    // ── experiment detail enrichment helpers ──────────────────────────────
    // Gracefully handles older records that lack description/timestamps/cold_bytes.

    expDuration() {
      const e = this.currentExp;
      if (!e?.created_at || !e?.completed_at) return '—';
      const secs = Math.round((new Date(e.completed_at) - new Date(e.created_at)) / 1000);
      if (secs < 60) return secs + 's';
      return `${Math.floor(secs / 60)}m ${secs % 60}s`;
    },

    // agentcache cold ÷ blobless cold ratio — "—" when either value is absent.
    expColdRatio(campaign) {
      const ac = campaign?.summary?.agentcache?.cold_bytes;
      const bl = campaign?.summary?.blobless?.cold_bytes;
      if (ac == null || !bl) return '—';
      return (ac / bl).toFixed(2) + '×';
    },

    // Returns the human-commit entries from a campaign timeline (may be empty).
    expHumanSteps(campaign) {
      return (campaign?.timeline || []).filter(p => p.kind === 'human');
    },

    // Wall time of a single agent pass (started_at → completed_at).
    // Returns '' when timestamps are absent so callers can gate with x-show.
    expPassDuration(p) {
      if (!p?.started_at || !p?.completed_at) return '';
      return ((new Date(p.completed_at) - new Date(p.started_at)) / 1000).toFixed(1) + 's';
    },
  };
}
