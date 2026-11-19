// 모아플랜 화면 (Vue 3). 데이터는 전부 백엔드 API에서 받음
const API = location.port === '8000' || location.port === '' ? '' : 'http://localhost:8000';

const STEP = { analyze: '요청 분석', plan: '작업 계획', execute: 'Tool 호출', judge: '제약 판단',
               replan: '재계획', enrich: '보조 정보', respond: '응답 작성' };
const TOOL = { search_savings_products: '상품 후보 조회', get_product_detail: '상품 상세', get_economic_indicators: '물가·기준금리',
               find_bank_branches: '영업점 검색', rag_search: '안내 문서 검색', get_schedule: '일정 조회',
               register_plan_schedule: '일정 등록' };
const SLOT_FMT = { '목표 금액': 'won', '월 납입 상한': 'won', '지금 가진 목돈': 'won' };

Vue.createApp({
  data: () => ({
    tab: 'chat',
    tabs: [{ id: 'chat', label: '대화' }, { id: 'plans', label: '계획' }, { id: 'logs', label: '실행 기록' }],
    status: { keys: [], fake_mode: false },
    pw: false, input: '', busy: false, sessionId: null,
    messages: [], liveEvents: [], showAll: false, showLedger: false,
    plans: [], calendar: {}, editing: null, draft: {}, planNotice: '', confirming: null,
    sessions: [], logSession: null, logRows: [], anomalies: [],
    samples: ['1년 안에 1000만원 모으고 싶어. 월 90만원까지 넣을 수 있어. 강남 근처',
              '6개월 뒤 이직해. 목돈 500만원 있고 매달 50만원 가능, 1000만원 만들고 싶어',
              '예금자보호 한도가 얼마야?', '요즘 금리랑 물가 어때?'],
  }),
  computed: {
    liveTools() { return this.liveEvents.filter(e => e.event === 'tool.call').length; },
    liveReplans() { return this.liveEvents.filter(e => e.event === 'replan').length; },
    missingKeys() { return (this.status.keys || []).filter(k => !k.configured).map(k => k.label); },
    visibleEvents() {
      return this.showAll ? this.liveEvents
        : this.liveEvents.filter(e => !['step.enter', 'session.start'].includes(e.event));
    },
    liveStep() {
      const last = [...this.liveEvents].reverse().find(e => e.event === 'step.enter' || e.event === 'tool.call');
      if (!last) return '요청을 읽는 중';
      return last.event === 'tool.call' ? `${TOOL[last.payload.tool] || last.payload.tool} 호출 중` : `${STEP[last.payload.step] || last.payload.step} 중`;
    },
  },
  methods: {
    won(v) { return v === null || v === undefined ? '—' : Number(v).toLocaleString('ko-KR') + '원'; },
    fmtSlot(k, v) {
      if (v === null || v === undefined || (Array.isArray(v) && !v.length)) return '비어 있음';
      if (Array.isArray(v)) return v.join(', ');
      if (SLOT_FMT[k]) return this.won(v);
      if (v === true) return '예'; if (v === false) return '아니오';
      if (v === 'saving') return '적금'; if (v === 'deposit') return '예금'; if (v === 'any') return '상관없음';
      return v;
    },
    firstLine(t) { return (t || '').split('\n')[0]; },
    bodyLines(m) { return (m.view.body_text || '').split('\n').filter(Boolean); },
    eventLabel(e) {
      const p = e.payload;
      switch (e.event) {
        case 'session.start': return '요청 시작';
        case 'session.end': return '응답 완료';
        case 'step.enter': return `${STEP[p.step] || p.step} 시작`;
        case 'step.exit': return `${STEP[p.step] || p.step}`;
        case 'decision': return p.by === 'rule' ? '규칙 보정' : 'LLM 판단';
        case 'plan': return `계획 v${p.version}`;
        case 'tool.call': return `→ ${TOOL[p.tool] || p.tool}`;
        case 'tool.result': return `← ${TOOL[p.tool] || p.tool}`;
        case 'tool.cache_hit': return `↺ ${TOOL[p.tool] || p.tool}`;
        case 'judge': return '판정';
        case 'replan': return '재계획';
        case 'llm.call': return `LLM 호출`;
        case 'worker.decision': return 'Worker 판단';
        case 'logging.verify': return '로깅 Agent 검증';
        default: return e.event;
      }
    },
    eventDetail(e) {
      const p = e.payload;
      switch (e.event) {
        case 'session.start': return p.user;
        case 'session.end': return `LLM ${p.llm_calls}회 · Tool ${p.tool_calls}회 · 캐시 ${p.cache_hits}회`;
        case 'step.exit': return p.output ? this.exitText(p.step, p.output) : '';
        case 'decision': return `${p.value} — ${p.reason}`;
        case 'plan': return `${p.tasks.length}개 작업 · ${p.reason}`;
        case 'tool.call': return this.argText(p.args) + (p.retry_of ? ' (고쳐서 다시 부름)' : '');
        case 'tool.result': return p.summary;
        case 'tool.cache_hit': return '이미 받은 결과 재사용';
        case 'judge': return `통과 ${p.passed} · 탈락 ${p.failed} · 보류 ${p.unjudged}` + (p.best ? ` · 1순위 ${p.best}` : '');
        case 'replan': return `${p.reason} → ${({ include_savings_bank: '저축은행 추가', add_deposit: '예금 조합 추가', more_candidates: '다음 순위 상품 조회', rebuild_verified: '검증된 작업으로 다시 구성' })[p.strategy] || p.strategy} (${p.n}/${p.cap})`;
        case 'llm.call': return `${p.purpose} · ${p.model}`;
        case 'worker.decision': return `${p.action}: ${p.reason}` + (Object.keys(p.changed_args || {}).length ? ` → 바꾼 인자 ${JSON.stringify(p.changed_args)}` : '') + (Object.keys(p.blocked_args || {}).length ? ` · 고정 인자 변경 차단 ${JSON.stringify(p.blocked_args)}` : '');
        case 'logging.verify': return p.verdict === 'anomaly' ? p.findings.map(this.findingLabel).join(', ') : '이상 없음';
        default: return '';
      }
    },
    exitText(step, o) {
      if (step === 'analyze') return `의도 ${o.intent}` + (o.changed && o.changed.length ? ` · 바뀐 조건 ${o.changed.join(', ')}` : '') + (o.missing && o.missing.length ? ` · 비어 있음 ${o.missing.join(', ')}` : '');
      if (step === 'judge' && o.candidates !== undefined) return `조합 ${o.candidates}개 중 통과 ${o.passed}`;
      if (step === 'execute') return `성공 ${o.ok_results}건` + (o.fatal ? ' · 키 오류' : '');
      return '';
    },
    argText(a) {
      if (!a) return '';
      const parts = [];
      if (a.product_type) parts.push(a.product_type === 'saving' ? '적금' : '예금');
      if (a.term_months) parts.push(a.term_months + '개월');
      if (a.fin_group) parts.push(a.fin_group === 'bank' ? '은행' : '저축은행');
      if (a.fin_prdt_cd) parts.push(a.fin_prdt_cd);
      if (a.bank_name) parts.push(a.bank_name + ' @ ' + a.region);
      if (a.query) parts.push(a.query);
      if (a.plan_id) parts.push('계획 #' + a.plan_id);
      return parts.join(' · ');
    },
    verdictLabel(v) { return ({ normal: '이상 없음', anomaly: '이상 징후', undecidable: '판정 불가' })[v] || v; },
    findingLabel(f) {
      if (f.type === 'repeat_call') return `같은 입력으로 ${TOOL[f.tool] || f.tool} ${f.count}번 호출`;
      if (f.type === 'excessive_replan') return `재계획 ${f.count}회 (상한 ${f.cap})`;
      if (f.type === 'fail_streak') return `${TOOL[f.tool] || f.tool} 연달아 실패`;
      if (f.type === 'unplanned_call') return `계획에 없던 ${TOOL[f.tool] || f.tool} 호출`;
      return f.type;
    },
    async api(path, opts = {}) {
      const r = await fetch(API + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
      if (!r.ok && r.status !== 204) throw new Error((await r.json().catch(() => ({}))).detail || '요청이 실패했습니다.');
      return r.status === 204 ? null : r.json();
    },
    async send(text) {
      const message = (text || this.input).trim();
      if (!message || this.busy) return;
      this.input = ''; this.busy = true; this.liveEvents = [];
      this.messages.push({ role: 'user', text: message });
      this.scroll();
      try {
        const res = await fetch(API + '/api/chat/stream', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message, session_id: this.sessionId, mode: this.pw ? 'pw' : 'single' }) });
        const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
        while (true) {
          const { value, done } = await reader.read(); if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
            const kind = (chunk.match(/^event: (.*)$/m) || [])[1];
            const data = JSON.parse((chunk.match(/^data: (.*)$/m) || [])[1] || '{}');
            if (kind === 'event') this.liveEvents.push(data);
            if (kind === 'result') {
              this.sessionId = data.session_id;
              this.liveEvents = data.events.length ? data.events : this.liveEvents;
              this.messages.push({ role: 'assistant', text: data.answer, view: data.view || {}, stats: data.stats || {}, mode: data.mode, open: false });
              if (data.view && ['saved', 'calendar'].includes(data.view.type)) this.loadPlans();
            }
          }
        }
      } catch (e) {
        this.messages.push({ role: 'assistant', text: '서버에 연결하지 못했습니다. 백엔드(uvicorn)가 켜져 있는지 확인하세요.',
                             view: { type: 'error' }, stats: {}, mode: '' });
      } finally { this.busy = false; this.scroll(); }
    },
    scroll() { this.$nextTick(() => { const t = this.$refs.thread; if (t) t.scrollTop = t.scrollHeight; }); },
    moreLabel(m) {                 // 접힌 내용 목록을 버튼에 표시
      const v = m.view || {}, parts = [];
      const extra = (v.recommendations || []).length - 1;
      if (extra > 0) parts.push(`다른 추천 ${extra}개`);
      if (v.conditions) parts.push('조건');
      if ((v.excluded || []).length) parts.push('제외 사유');
      if (v.branches && Object.keys(v.branches).length) parts.push('영업점');
      if ((v.conditional || []).length) parts.push('자격 상품');
      return parts.join(' · ');
    },
    goHome() {                     // 첫 화면으로 (대화, 실행 기록, 세션 초기화. 저장한 계획은 유지)
      if (this.busy) return;
      this.tab = 'chat'; this.messages = []; this.liveEvents = []; this.sessionId = null; this.input = '';
      this.$nextTick(() => document.getElementById('q')?.focus());
    },
    openTab(id) { this.tab = id; if (id === 'plans') this.loadPlans(); if (id === 'logs') this.loadSessions(); },
    async loadStatus() { try { this.status = await this.api('/api/status'); } catch (e) { /* 서버 꺼짐 */ } },
    async loadPlans() {
      try { this.plans = await this.api('/api/plans'); this.calendar = await this.api('/api/calendar'); } catch (e) { this.planNotice = e.message; }
    },
    edit(p) { this.editing = p.plan_id; this.draft = { start_date: p.start_date, term_months: p.term_months, payment_day: p.payment_day, monthly_amount: p.monthly_amount }; },
    async savePlan(p) {
      try {
        const r = await this.api(`/api/plans/${p.plan_id}`, { method: 'PUT', body: JSON.stringify(this.draft) });
        this.planNotice = `${p.plan_id}번 계획을 고쳤습니다.` + (r.calendar_synced ? ' 캘린더 일정도 새 값으로 바꿨습니다.' : '') + (r.overlap_message ? ' ' + r.overlap_message : '');
        this.editing = null; this.loadPlans();
      } catch (e) { this.planNotice = e.message; }
    },
    async removePlan(p) {
      this.confirming = null;      // confirm() 창이 안 뜨는 브라우저가 있어서 화면 버튼으로 확인받음
      await this.api(`/api/plans/${p.plan_id}`, { method: 'DELETE' });
      this.planNotice = `${p.plan_id}번 계획을 지웠습니다.`; this.loadPlans();
    },
    async loadSessions() {
      try { this.sessions = await this.api('/api/logs'); } catch (e) { this.sessions = []; }
      if (!this.logSession && this.sessions.length) this.openLog(this.sessions[0].session_id);   // 최근 대화 자동으로 열기
    },
    shortTime(t) {                 // 오늘이면 '01:47', 아니면 '11-20 01:47'
      if (!t) return '';
      const today = new Date().toISOString().slice(0, 10);
      return t.slice(0, 10) === today ? t.slice(11, 16) : t.slice(5, 16);
    },
    async openLog(id) {
      this.logSession = id;
      this.logRows = await this.api(`/api/logs/${id}`);
      this.anomalies = await this.api(`/api/logs/${id}/anomalies`);
    },
    async deleteLog(id) {
      this.confirming = null;
      await this.api(`/api/logs/${id}`, { method: 'DELETE' });
      this.logSession = null; this.loadSessions();
    },
  },
  mounted() { this.loadStatus(); this.loadPlans(); },
}).mount('#app');
