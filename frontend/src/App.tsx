import { FormEvent, useEffect, useMemo, useState, type CSSProperties, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react';
import eyLogoUrl from './assets/ey-logo.svg';

type Page = 'home' | 'crawler' | 'obligations' | 'gap' | 'diagnostics';

type Kpi = { label: string; value: string | number };
type LogRow = { stage: string; status: string; message: string; row_count: number };
type PipelineInfo = { pipeline_version: string; run_id?: string; source_file?: string; source_sha256?: string };
type CrawlerRecord = {
  id: string;
  title: string;
  section: string;
  category: string;
  year: string;
  source_link: string;
  filename: string;
  cached: boolean;
  downloaded: boolean;
  source_type?: string;
  status?: string;
  document_type?: string;
  file_size_bytes?: number;
};
type CategoryStatus = {
  category: string;
  expected: number;
  indexed: number;
  pdfs_cached: number;
  files_bundled?: number;
  pdfs_bundled?: number;
  complete: boolean;
};
type Results = {
  kpis: Kpi[];
  tabs: Record<string, any>;
  logs: LogRow[];
  output_files?: Record<string, string>;
  pipeline?: PipelineInfo;
  extraction_pipeline?: { pipeline_version: string; input_mode: string; crawler_enabled: boolean };
  accuracy?: {
    overall_percentage: number;
    rating: string;
    population: string;
    method: string;
    actionable_manual_review_rows?: number;
    all_manual_review_rows?: number;
  };
  gap_quality?: {
    population: number;
    method: string;
    assessment_confidence_percentage: number;
    evidence_grounding_percentage: number;
    recommendation_completeness_percentage: number;
    manual_review_rows: number;
    gap_rows: number;
    disclaimer: string;
  };
};

type ReviewQueueItem = {
  id: string;
  section: string;
  title: string;
  status?: string;
  priority?: string;
  sourcePage?: string;
  reason: string;
  sourceText?: string;
  evidence?: string;
  missingElements?: string;
  recommendation?: string;
};

type ReviewDecision = {
  disposition: 'pending' | 'confirmed' | 'amend' | 'escalate';
  note: string;
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');
const REQUIRED_GAP_PIPELINE = '2026-08-18.2-neutral-recommendations';

function friendlyApiError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  if (message.includes('Failed to fetch')) {
    return 'Cannot reach the backend. Start the FastAPI backend on http://127.0.0.1:8000, then try again.';
  }
  return message || 'The backend returned an error. Check the backend PowerShell window for details.';
}

async function readApiError(response: Response) {
  const fallback = `${response.status} ${response.statusText}`.trim();
  try {
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      const data = await response.json();
      if (typeof data?.detail === 'string') return data.detail;
      if (Array.isArray(data?.detail)) return data.detail.map((item: any) => item?.msg || JSON.stringify(item)).join('; ');
      return JSON.stringify(data);
    }
    const text = await response.text();
    return text || fallback || 'Unknown backend error';
  } catch {
    return fallback || 'Unknown backend error';
  }
}

function titleCase(value: string) {
  return value.split('_').join(' ');
}

const INTERNAL_QUALITY_LABEL = /accuracy|confidence|percentage/i;
const INTERNAL_QUALITY_COLUMN = /accuracy|confidence|(?:^|\s)coverage\s*%|fidelity\s*%|completeness\s*%|quality\s*%|cleanliness\s*%|material elements\s*%|semantic coverage\s*%/i;

function clientSafeLogs(rows: LogRow[]) {
  return rows.filter((row) => !INTERNAL_QUALITY_LABEL.test(`${row.stage} ${row.message}`));
}

function statusTone(value: unknown) {
  const normalised = String(value || '').toLowerCase();
  if (normalised.includes('missing') || normalised.includes('failed') || normalised === 'high') return 'danger';
  if (normalised.includes('partial') || normalised.includes('warning') || normalised === 'medium') return 'warning';
  if (normalised.includes('covered') || normalised.includes('healthy') || normalised.includes('completed') || normalised.includes('ready') || normalised.includes('available')) return 'success';
  return 'neutral';
}

function trackTilt(event: MouseEvent<HTMLElement>) {
  const target = event.currentTarget;
  const rect = target.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const rotateY = ((x / rect.width) - 0.5) * 7;
  const rotateX = (0.5 - (y / rect.height)) * 7;
  target.style.setProperty('--tilt-x', `${rotateX.toFixed(2)}deg`);
  target.style.setProperty('--tilt-y', `${rotateY.toFixed(2)}deg`);
  target.style.setProperty('--glow-x', `${x.toFixed(0)}px`);
  target.style.setProperty('--glow-y', `${y.toFixed(0)}px`);
}

function resetTilt(event: MouseEvent<HTMLElement>) {
  event.currentTarget.style.setProperty('--tilt-x', '0deg');
  event.currentTarget.style.setProperty('--tilt-y', '0deg');
}

function ProgressSteps({ steps, activeIndex }: { steps: string[]; activeIndex: number }) {
  return (
    <div className="progress-steps" style={{ gridTemplateColumns: `repeat(${steps.length}, minmax(0, 1fr))` }}>
      {steps.map((step, index) => (
        <div className={`progress-step ${index <= activeIndex ? 'active' : ''}`} key={step}>
          <span>{index + 1}</span>
          <p>{step}</p>
        </div>
      ))}
    </div>
  );
}

function KpiGrid({ kpis }: { kpis: Kpi[] }) {
  const clientKpis = kpis.filter((kpi) => !INTERNAL_QUALITY_LABEL.test(kpi.label));
  return (
    <div className="kpi-grid">
      {clientKpis.map((kpi) => (
        <div className="kpi-card" key={kpi.label}>
          <span>{kpi.label}</span>
          <strong>{kpi.value}</strong>
        </div>
      ))}
    </div>
  );
}

function DataTable({
  rows,
  maxRows = 25,
  hideInternalQuality = false,
  onRowClick,
}: {
  rows: any[];
  maxRows?: number;
  hideInternalQuality?: boolean;
  onRowClick?: (row: any) => void;
}) {
  const columns = useMemo(() => {
    const first = rows?.[0] || {};
    return Object.keys(first).filter((column) => !hideInternalQuality || !INTERNAL_QUALITY_COLUMN.test(column));
  }, [rows, hideInternalQuality]);

  if (!rows || rows.length === 0) {
    return <div className="empty-state">No rows available yet.</div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{columns.map((col) => <th key={col}>{titleCase(col)}</th>)}</tr>
        </thead>
        <tbody>
          {rows.slice(0, maxRows).map((row, rowIndex) => (
            <tr
              key={rowIndex}
              className={onRowClick ? 'clickable-result-row' : ''}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={onRowClick ? (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onRowClick(row);
                }
              } : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              aria-label={onRowClick ? `Open evidence for section ${String(row.Section || row.section || rowIndex + 1)}` : undefined}
            >
              {columns.map((col) => {
                const value = String(row[col] ?? '');
                const isBadge = /status|priority/i.test(col);
                return <td key={col} title={value}>{isBadge && value ? <span className={`data-badge ${statusTone(value)}`}>{value}</span> : value}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > maxRows && <p className="table-note">Showing first {maxRows} of {rows.length} rows. Download the output for the full register.</p>}
    </div>
  );
}

type ExecutiveFilter = 'covered' | 'partial' | 'missing' | 'review';

function ExecutiveSummary({
  rows,
  active,
  onSelect,
}: {
  rows: any[];
  active: ExecutiveFilter | null;
  onSelect: (filter: ExecutiveFilter) => void;
}) {
  const cards: Array<{ key: ExecutiveFilter; label: string; count: number; tone: string; detail: string }> = [
    {
      key: 'covered',
      label: 'Covered',
      count: rows.filter((row) => row['Coverage Status'] === 'Completely Covered').length,
      tone: 'covered',
      detail: 'No gap found',
    },
    {
      key: 'partial',
      label: 'Partially covered',
      count: rows.filter((row) => row['Coverage Status'] === 'Partially Covered').length,
      tone: 'partial',
      detail: 'Action needed',
    },
    {
      key: 'missing',
      label: 'Missing',
      count: rows.filter((row) => row['Coverage Status'] === 'Completely Missing').length,
      tone: 'missing',
      detail: 'Control needed',
    },
    {
      key: 'review',
      label: 'Needs review',
      count: rows.filter((row) => String(row['Manual Review Required'] || '').toLowerCase() === 'yes').length,
      tone: 'review',
      detail: 'Professional check',
    },
  ];

  return (
    <div className="executive-summary" aria-label="Executive coverage summary">
      {cards.map((card) => (
        <button
          type="button"
          key={card.key}
          className={`executive-summary-card ${card.tone} ${active === card.key ? 'active' : ''}`}
          onClick={() => onSelect(card.key)}
          aria-pressed={active === card.key}
        >
          <span>{card.label}</span>
          <strong>{card.count}</strong>
          <small>{card.detail}</small>
        </button>
      ))}
    </div>
  );
}

function EvidenceDrawer({ row, onClose }: { row: any | null; onClose: () => void }) {
  useEffect(() => {
    if (!row) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', handleEscape);
    };
  }, [row, onClose]);

  if (!row) return null;
  const status = String(row['Coverage Status'] || 'Review');
  const requirement = String(row['Language from Directive'] || row.Obligation || 'No requirement text available.');
  const evidence = String(row['Corresponding Policy Text'] || 'No matching policy evidence found.');
  const missing = String(row['Missing Elements'] || (status === 'Completely Covered' ? 'No missing requirement identified.' : 'Review the requirement against the cited policy evidence.'));
  const recommendation = String(row['Draft Policy Clause'] || row['Policy Gap and Recommendations'] || (status === 'Completely Covered' ? 'No policy amendment recommended.' : 'No draft clause available.'));

  return (
    <div className="evidence-drawer-layer" role="presentation">
      <button className="evidence-drawer-backdrop" type="button" onClick={onClose} aria-label="Close evidence panel" />
      <aside className="evidence-drawer" role="dialog" aria-modal="true" aria-label={`Evidence review for section ${String(row.Section || 'unknown')}`}>
        <div className="evidence-drawer-header">
          <div>
            <span>Section {String(row.Section || '—')}</span>
            <h2>Evidence comparison</h2>
          </div>
          <button type="button" onClick={onClose} aria-label="Close evidence comparison">×</button>
        </div>
        <div className="evidence-status-line">
          <span className={`data-badge ${statusTone(status)}`}>{status}</span>
          {row.Priority && <span className={`data-badge ${statusTone(row.Priority)}`}>{String(row.Priority)} priority</span>}
          {row['Policy Page'] && <span>Policy page {String(row['Policy Page'])}</span>}
        </div>
        <div className="evidence-drawer-body">
          <section className="evidence-block requirement">
            <span>01 · Regulatory requirement</span>
            <p>{requirement}</p>
          </section>
          <section className="evidence-block matched">
            <span>02 · Matching policy evidence</span>
            <p>{evidence}</p>
          </section>
          <section className="evidence-block missing">
            <span>03 · Missing requirement</span>
            <p>{missing}</p>
          </section>
          <section className="evidence-block recommendation">
            <span>04 · Recommended clause</span>
            <p>{recommendation}</p>
          </section>
        </div>
        <div className="evidence-drawer-footer">
          <Icon name="info" size={15} />Review the evidence before accepting the finding.
        </div>
      </aside>
    </div>
  );
}

function ProcessingJourney({ stages }: { stages: string[] }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <div className="processing-journey glass-panel" role="status" aria-live="polite">
      <div className="processing-journey-head">
        <div><span className="journey-live-dot" /><strong>Processing</strong></div>
        <time>{elapsed}s elapsed</time>
      </div>
      <div className="processing-stage-list">
        {stages.map((stage, index) => (
          <div key={stage} className="processing-stage">
            <span>{String(index + 1).padStart(2, '0')}</span>
            <strong>{stage}</strong>
            <i aria-hidden="true" />
          </div>
        ))}
      </div>
      <small>Stages are confirmed when the completed result returns.</small>
    </div>
  );
}

function HomeButton({ setPage }: { setPage: (page: Page) => void }) {
  return <button className="ghost-button compact" onClick={() => setPage('home')}><Icon name="home" size={15} />Home</button>;
}

function PageHeader({ utility, title, description, setPage }: { utility: string; title: string; description: string; setPage: (page: Page) => void }) {
  return (
    <div className="page-header glass-panel">
      <div>
        <div className="page-kicker"><span>{utility}</span><small>Connected workflow</small></div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <HomeButton setPage={setPage} />
    </div>
  );
}

function EmptyGuide({ title, body }: { title: string; body: string }) {
  return (
    <div className="empty-guide">
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
}

function LoadingPanel({ title, detail }: { title: string; detail: string }) {
  return <div className="loading-panel" role="status" aria-live="polite"><span className="loading-spinner" /><div><strong>{title}</strong><p>{detail}</p><span className="loading-track"><i /></span></div></div>;
}

function EYLogo() {
  return (
    <span className="ey-logo">
      <img src={eyLogoUrl} alt="EY" />
    </span>
  );
}

type IconName = 'home' | 'library' | 'obligations' | 'gap' | 'diagnostics' | 'arrow' | 'shield' | 'scan' | 'export' | 'spark' | 'info';

function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, ReactNode> = {
    home: <><path d="M3 10.8 12 3l9 7.8" /><path d="M5.5 9.6V21h13V9.6M9.5 21v-6h5v6" /></>,
    library: <><path d="M4 4.5h5.5A2.5 2.5 0 0 1 12 7v13a2.5 2.5 0 0 0-2.5-2.5H4z" /><path d="M20 4.5h-5.5A2.5 2.5 0 0 0 12 7v13a2.5 2.5 0 0 1 2.5-2.5H20z" /></>,
    obligations: <><path d="M7 3h10l3 3v15H4V3z" /><path d="M14 3v5h6M8 12h8M8 16h6" /></>,
    gap: <><path d="M4 5h6v6H4zM14 13h6v6h-6z" /><path d="M10 8h4a3 3 0 0 1 3 3v2M14 16h-4a3 3 0 0 1-3-3v-2" /></>,
    diagnostics: <><circle cx="12" cy="12" r="8" /><path d="M12 8v4l3 2M12 2v2M12 20v2M2 12h2M20 12h2" /></>,
    arrow: <><path d="M5 12h14M14 7l5 5-5 5" /></>,
    shield: <><path d="M12 3 20 6v5c0 5-3.4 8.5-8 10-4.6-1.5-8-5-8-10V6z" /><path d="m8.5 12 2.2 2.2 4.8-5" /></>,
    scan: <><path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4" /><path d="M7 12h10M8.5 9h7M8.5 15h7" /></>,
    export: <><path d="M12 3v12M7.5 7.5 12 3l4.5 4.5" /><path d="M5 13v7h14v-7" /></>,
    spark: <><path d="m12 3 1.2 4.2L17 9l-3.8 1.8L12 15l-1.2-4.2L7 9l3.8-1.8z" /><path d="m18.5 15 .7 2.3 2.3.7-2.3.7-.7 2.3-.7-2.3-2.3-.7 2.3-.7zM5 13l.7 2.3 2.3.7-2.3.7L5 19l-.7-2.3L2 16l2.3-.7z" /></>,
    info: <><circle cx="12" cy="12" r="9" /><path d="M12 10.5V17M12 7.2h.01" /></>,
  };
  return <svg className="ui-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}


function ResultsPanel({
  title,
  summary,
  children,
}: {
  title: string;
  summary: string;
  children: ReactNode;
}) {
  return (
    <div className="results-window glass-panel">
      <div className="results-header">
        <div>
          <p className="eyebrow">Results workspace</p>
          <h2>{title}</h2>
        </div>
        <p>{summary}</p>
      </div>
      {children}
    </div>
  );
}

function TabScroll({ children }: { children: ReactNode }) {
  return <div className="tab-content-scroll">{children}</div>;
}

function obligationReviewReason(row: any) {
  const reasons: string[] = [];
  if (Number(row['Missing Material Elements'] || 0) > 0) reasons.push('Possible missing material elements');
  if (Number(row['Answer Completeness %'] ?? 100) < 100) reasons.push('Extracted wording may be incomplete');
  if (Number(row['Source Fidelity %'] ?? 100) < 75) reasons.push('Source-page traceability needs confirmation');
  if (Number(row['Source Cleanliness %'] ?? 100) < 85 || Number(row['Text Cleanliness %'] ?? 100) < 85) {
    reasons.push('Possible OCR or page-layout contamination');
  }
  return reasons.length ? reasons.join(' · ') : 'Qualified source verification is required';
}

function ReviewQueue({
  items,
  storageKey,
  emptyMessage,
}: {
  items: ReviewQueueItem[];
  storageKey: string;
  emptyMessage: string;
}) {
  const [filter, setFilter] = useState<'open' | 'completed' | 'all'>('open');
  const [search, setSearch] = useState('');
  const [copiedId, setCopiedId] = useState('');
  const [decisions, setDecisions] = useState<Record<string, ReviewDecision>>(() => {
    if (typeof window === 'undefined') return {};
    try {
      return JSON.parse(window.localStorage.getItem(storageKey) || '{}');
    } catch {
      return {};
    }
  });

  useEffect(() => {
    window.localStorage.setItem(storageKey, JSON.stringify(decisions));
  }, [decisions, storageKey]);

  const decisionFor = (id: string): ReviewDecision => decisions[id] || { disposition: 'pending', note: '' };
  const completedCount = items.filter((item) => decisionFor(item.id).disposition !== 'pending').length;
  const visibleItems = items.filter((item) => {
    const decision = decisionFor(item.id);
    const matchesFilter = filter === 'all'
      || (filter === 'open' && decision.disposition === 'pending')
      || (filter === 'completed' && decision.disposition !== 'pending');
    const haystack = `${item.section} ${item.title} ${item.status || ''} ${item.reason} ${item.missingElements || ''}`.toLowerCase();
    return matchesFilter && haystack.includes(search.trim().toLowerCase());
  });

  const updateDecision = (id: string, patch: Partial<ReviewDecision>) => {
    setDecisions((current) => ({
      ...current,
      [id]: { ...decisionFor(id), ...patch },
    }));
  };

  const copyBrief = async (item: ReviewQueueItem) => {
    const brief = [
      `Section: ${item.section}`,
      `Item: ${item.title}`,
      item.status ? `Current finding: ${item.status}` : '',
      item.reason ? `Why review is required: ${item.reason}` : '',
      item.missingElements ? `Missing elements: ${item.missingElements}` : '',
      item.sourcePage ? `Source page: ${item.sourcePage}` : '',
      item.evidence ? `Evidence: ${item.evidence}` : '',
      item.recommendation ? `Proposed action: ${item.recommendation}` : '',
    ].filter(Boolean).join('\n');
    try {
      await navigator.clipboard.writeText(brief);
      setCopiedId(item.id);
      window.setTimeout(() => setCopiedId(''), 1800);
    } catch {
      setCopiedId('');
    }
  };

  const exportReviewLog = () => {
    const headers = ['Section', 'Item', 'Current Finding', 'Review Disposition', 'Reviewer Note'];
    const values = items.map((item) => {
      const decision = decisionFor(item.id);
      return [item.section, item.title, item.status || '', decision.disposition, decision.note];
    });
    const quote = (value: unknown) => `"${String(value ?? '').replaceAll('"', '""')}"`;
    const csv = [headers, ...values].map((row) => row.map(quote).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'regulamosaic-professional-review-log.csv';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  };

  if (items.length === 0) {
    return <div className="review-queue-empty"><Icon name="shield" size={22} /><div><strong>No queued review items</strong><p>{emptyMessage}</p></div></div>;
  }

  return (
    <div className="review-queue">
      <div className="review-queue-head">
        <div>
          <p className="eyebrow">Qualified review workspace</p>
          <h3>{completedCount} of {items.length} items dispositioned</h3>
          <p>Confirm, amend, or escalate each flagged item. Decisions and notes are saved in this browser.</p>
        </div>
        <button className="secondary-button" onClick={exportReviewLog}><Icon name="export" size={15} />Export review log</button>
      </div>
      <div className="review-progress" aria-label={`${completedCount} of ${items.length} review items completed`}>
        <i style={{ width: `${items.length ? (completedCount / items.length) * 100 : 0}%` }} />
      </div>
      <div className="review-toolbar">
        <div className="review-filter" role="group" aria-label="Filter review queue">
          {(['open', 'completed', 'all'] as const).map((value) => (
            <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>
              {value === 'open' ? 'Open' : value === 'completed' ? 'Completed' : 'All'}
            </button>
          ))}
        </div>
        <label className="review-search"><span>Search queue</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Section, finding, or issue" /></label>
        <span className="review-visible-count">{visibleItems.length} shown</span>
      </div>
      <div className="review-list">
        {visibleItems.map((item) => {
          const decision = decisionFor(item.id);
          return (
            <article className={`review-item ${decision.disposition !== 'pending' ? 'resolved' : ''}`} key={item.id}>
              <div className="review-item-main">
                <div className="review-item-meta">
                  <span className="review-section">Section {item.section}</span>
                  {item.status && <span className={`data-badge ${statusTone(item.status)}`}>{item.status}</span>}
                  {item.priority && <span className={`data-badge ${statusTone(item.priority)}`}>{item.priority} priority</span>}
                  {item.sourcePage && <span>Source page {item.sourcePage}</span>}
                </div>
                <h4>{item.title}</h4>
                <p className="review-reason"><Icon name="shield" size={15} />{item.reason}</p>
                <details>
                  <summary>View evidence and proposed action</summary>
                  <div className="review-context">
                    {item.sourceText && <div><span>Regulatory source</span><p>{item.sourceText}</p></div>}
                    {item.evidence && <div><span>Policy evidence</span><p>{item.evidence}</p></div>}
                    {item.missingElements && <div><span>Missing elements</span><p>{item.missingElements}</p></div>}
                    {item.recommendation && <div><span>Proposed action</span><p>{item.recommendation}</p></div>}
                  </div>
                </details>
              </div>
              <div className="review-decision">
                <label>Review disposition
                  <select value={decision.disposition} onChange={(event) => updateDecision(item.id, { disposition: event.target.value as ReviewDecision['disposition'] })}>
                    <option value="pending">Pending review</option>
                    <option value="confirmed">Confirmed as presented</option>
                    <option value="amend">Amend generated output</option>
                    <option value="escalate">Escalate to specialist</option>
                  </select>
                </label>
                <label>Reviewer note
                  <textarea value={decision.note} onChange={(event) => updateDecision(item.id, { note: event.target.value })} placeholder="Record the decision basis or required amendment" />
                </label>
                <button className="ghost-button compact" onClick={() => void copyBrief(item)}>{copiedId === item.id ? 'Copied' : 'Copy review brief'}</button>
              </div>
            </article>
          );
        })}
        {visibleItems.length === 0 && <div className="empty-state">No review items match this filter.</div>}
      </div>
    </div>
  );
}

function HomePage({ setPage }: { setPage: (page: Page) => void }) {
  const cards = [
    {
      page: 'crawler' as Page,
      label: 'Utility 01',
      icon: 'library' as IconName,
      title: 'FSCA Directive Library',
      subtitle: 'Curated regulatory source library',
      body: 'Find and open the directive you need.',
      bullets: ['Browse by topic', 'Filter by year', 'Open in extraction'],
    },
    {
      page: 'obligations' as Page,
      label: 'Utility 02',
      icon: 'obligations' as IconName,
      title: 'Obligation Extraction',
      subtitle: 'Directive-to-obligation conversion',
      body: 'Turn a directive into a source-linked obligation register.',
      bullets: ['PDF and OCR', 'Obligation register', 'Source-page links'],
    },
    {
      page: 'gap' as Page,
      label: 'Utility 03',
      icon: 'gap' as IconName,
      title: 'Policy Gap Reviewer',
      subtitle: 'Policy alignment assessment',
      body: 'Compare obligations with policy and identify the gaps.',
      bullets: ['Coverage status', 'Policy evidence', 'Recommendations'],
    },
  ];

  const workflowChoices = [
    {
      page: 'crawler' as Page,
      icon: 'library' as IconName,
      shortTitle: 'Directive Library',
      cue: 'I need a source',
      eyebrow: 'Start with verified source material',
      title: 'Browse the complete directive library',
      description: 'Find the directive you need and open it.',
      requirements: ['Choose a topic', 'Filter by year', 'Open the source'],
      output: 'Verified source selected',
      action: 'Open Directive Library',
    },
    {
      page: 'obligations' as Page,
      icon: 'obligations' as IconName,
      shortTitle: 'Extract Obligations',
      cue: 'I have a directive',
      eyebrow: 'Start with a regulatory document',
      title: 'Build a traceable obligation register',
      description: 'Choose a directive and build its obligation register.',
      requirements: ['Choose one PDF', 'Extract obligations', 'Download the register'],
      output: 'Obligation register created',
      action: 'Start Obligation Extraction',
    },
    {
      page: 'gap' as Page,
      icon: 'gap' as IconName,
      shortTitle: 'Review Coverage',
      cue: 'I have policy evidence',
      eyebrow: 'Start with obligations and policy',
      title: 'Assess policy coverage and close gaps',
      description: 'Compare your register with policy and see what is missing.',
      requirements: ['Choose a register', 'Add the policy', 'Review the gaps'],
      output: 'Policy response assessed',
      action: 'Start Gap Review',
    },
  ];
  const [selectedWorkflow, setSelectedWorkflow] = useState(0);
  const selectedChoice = workflowChoices[selectedWorkflow];
  const moveWorkflowSelection = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const lastIndex = workflowChoices.length - 1;
    let nextIndex: number | null = null;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') nextIndex = index === lastIndex ? 0 : index + 1;
    if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') nextIndex = index === 0 ? lastIndex : index - 1;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = lastIndex;
    if (nextIndex === null) return;
    event.preventDefault();
    setSelectedWorkflow(nextIndex);
    requestAnimationFrame(() => document.getElementById(`workflow-choice-${nextIndex}`)?.focus());
  };

  return (
    <section className="hero">
      <div className="hero-layout">
        <div className="hero-copy">
          <div className="hero-brandline">
            <div className="hero-ey-logo" aria-label="EY"><EYLogo /></div>
            <span />
            <div><small>Regulatory intelligence</small><strong>Built for defensible review</strong></div>
          </div>
          <p className="eyebrow"><i /> RegulaMosaic workspace</p>
          <h1>See every obligation.<br /><span>Close every gap.</span></h1>
          <p>
            One connected workspace that turns complex regulatory documents into traceable obligations, evidence-led findings, and review-ready outputs.
          </p>
          <div className="hero-actions">
            <button className="primary-button" onClick={() => setPage('crawler')}>Enter the workspace <Icon name="arrow" size={16} /></button>
            <button className="secondary-button" onClick={() => setPage('obligations')}><Icon name="obligations" size={16} />Upload a directive</button>
          </div>
          <div className="hero-proof" aria-label="Workspace capabilities">
            <span><i><Icon name="shield" size={16} /></i><b>50 verified PDF directives</b></span>
            <span><i><Icon name="scan" size={16} /></i><b>Native + OCR PDFs</b></span>
            <span><i><Icon name="export" size={16} /></i><b>Evidence-linked outputs</b></span>
          </div>
        </div>
        <div className="workflow-card quick-start-card glass-panel" aria-label="Choose a compliance workflow">
          <span className="card-glare" aria-hidden="true" />
          <div className="workflow-card-header">
            <div><span>Quick start</span><strong>What do you want to do?</strong></div>
            <span className="choice-count"><b>{selectedWorkflow + 1}</b> of {workflowChoices.length}</span>
          </div>

          <div className="workflow-selector" role="tablist" aria-label="Choose your starting point">
            {workflowChoices.map((choice, index) => (
              <button
                key={choice.shortTitle}
                id={`workflow-choice-${index}`}
                className={`workflow-choice ${selectedWorkflow === index ? 'active' : ''}`}
                role="tab"
                aria-selected={selectedWorkflow === index}
                aria-controls="workflow-preview"
                tabIndex={selectedWorkflow === index ? 0 : -1}
                onClick={() => setSelectedWorkflow(index)}
                onKeyDown={(event) => moveWorkflowSelection(event, index)}
              >
                <span className="workflow-choice-number">0{index + 1}</span>
                <span className="workflow-choice-icon"><Icon name={choice.icon} size={19} /></span>
                <span className="workflow-choice-copy"><small>{choice.cue}</small><strong>{choice.shortTitle}</strong></span>
                <span className="workflow-choice-state" aria-hidden="true">{selectedWorkflow === index ? 'Selected' : 'Choose'}</span>
              </button>
            ))}
          </div>

          <div
            className="workflow-preview"
            id="workflow-preview"
            role="tabpanel"
            aria-labelledby={`workflow-choice-${selectedWorkflow}`}
            key={selectedChoice.shortTitle}
          >
            <div className="workflow-preview-heading">
              <span><Icon name={selectedChoice.icon} size={22} /></span>
              <div><small>{selectedChoice.eyebrow}</small><h2>{selectedChoice.title}</h2></div>
            </div>
            <p>{selectedChoice.description}</p>
            <div className="workflow-deliverables">
              {selectedChoice.requirements.map((item) => <span key={item}><i aria-hidden="true">✓</i>{item}</span>)}
            </div>
            <div className="workflow-outcome"><span>Outcome</span><strong>{selectedChoice.output}</strong></div>
            <button className="primary-button workflow-launch" onClick={() => setPage(selectedChoice.page)}>
              {selectedChoice.action}<Icon name="arrow" size={16} />
            </button>
          </div>

          <button className="workflow-link quick-diagnostics" onClick={() => setPage('diagnostics')}><span><Icon name="diagnostics" size={15} />Check system readiness</span><span>Diagnostics <Icon name="arrow" size={14} /></span></button>
        </div>
      </div>

      <div className="section-heading reveal-on-scroll">
        <div><span>Connected capabilities</span><h2>One review journey. Three focused utilities.</h2></div>
        <p>Move from source material to a defensible policy response without losing the evidence trail.</p>
      </div>
      <div className="utility-grid">
        {cards.map((card, index) => (
          <div className="utility-card-shell reveal-on-scroll" style={{ '--reveal-delay': `${index * 110}ms` } as CSSProperties} key={card.title}>
            <button className="utility-card interactive-tilt" onClick={() => setPage(card.page)} onMouseMove={trackTilt} onMouseLeave={resetTilt}>
              <span className="card-glare" aria-hidden="true" />
              <div className="utility-card-top"><span>{card.label}</span><b><Icon name={card.icon} size={20} /></b></div>
              <h2>{card.title}</h2>
              <h3>{card.subtitle}</h3>
              <p>{card.body}</p>
              <ul>{card.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}</ul>
              <div className="card-action">Open utility <Icon name="arrow" size={15} /></div>
            </button>
          </div>
        ))}
      </div>
      <div className="review-principle glass-panel reveal-on-scroll">
        <span className="review-icon"><Icon name="shield" size={22} /></span>
        <div><small>Responsible intelligence</small><strong>AI accelerates the review. Qualified professionals own the decision.</strong></div>
        <p>Every output retains source context and explicit review cues to support—not replace—professional judgement.</p>
      </div>
    </section>
  );
}

function CrawlerPage({ setPage }: { setPage: (page: Page) => void }) {
  const [records, setRecords] = useState<CrawlerRecord[]>([]);
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [metadata, setMetadata] = useState<any>(null);
  const [section, setSection] = useState('');
  const [year, setYear] = useState('All');
  const [tab, setTab] = useState('Directives');
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const [notice, setNotice] = useState('');
  const [selectedCategoryStatus, setSelectedCategoryStatus] = useState<CategoryStatus | null>(null);

  useEffect(() => {
    let active = true;
    fetch(`${API_BASE}/api/crawler/metadata`, { cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error(await readApiError(response));
        return response.json();
      })
      .then((data) => {
        if (active) setMetadata(data);
      })
      .catch((error) => {
        if (active) setErrorMessage(friendlyApiError(error));
      });
    return () => { active = false; };
  }, []);

  const chooseTopic = async (topic: string) => {
    setSection(topic);
    setYear('All');
    setLoading(true);
    setErrorMessage('');
    setNotice('');
    try {
      const response = await fetch(`${API_BASE}/api/crawler/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          section: topic,
          year: 'All',
          refresh: false,
          cached_only: true,
        }),
        cache: 'no-store',
      });
      if (!response.ok) throw new Error(await readApiError(response));
      const data = await response.json();
      const status = data.selected_category_status as CategoryStatus | undefined;
      setRecords(data.records || []);
      setLogs(data.logs || []);
      setSelectedCategoryStatus(status || null);
      setNotice(`${data.records?.length || 0} official ${topic} file(s) ready to review.`);
    } catch (error) {
      setRecords([]);
      setSelectedCategoryStatus(null);
      setErrorMessage(`Directive library unavailable: ${friendlyApiError(error)}`);
    } finally {
      setLoading(false);
    }
  };

  const years = ['All', ...Array.from(new Set(records.map((item) => item.year).filter((item) => item && item !== 'Unknown'))).sort().reverse()];
  const filtered = records.filter((item) => year === 'All' || item.year === year);
  const categories: string[] = (metadata?.sections || []).filter((item: string) => item !== 'All');
  const formatBytes = (value = 0) => value >= 1024 * 1024
    ? `${(value / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(value / 1024))} KB`;

  return (
    <section className="page-shell">
      <PageHeader
        utility="Utility 1"
        title="FSCA Directive Library"
        description="Browse official directives by topic and select the regulatory source needed for your review."
        setPage={setPage}
      />
      <div className="topic-picker glass-panel">
        <div className="topic-picker-heading">
          <div>
            <span>Choose a directive topic</span>
            <strong>Select the regulatory collection you want to review.</strong>
          </div>
          <small>Browse by topic, then refine the available files by year.</small>
        </div>
        <div className="topic-choice-grid">
          {categories.map((category) => {
            const status = metadata?.category_status?.[category] as CategoryStatus | undefined;
            const expected = Number(metadata?.expected_category_counts?.[category] || status?.expected || 0);
            return (
              <button
                key={category}
                className={`topic-choice ${section === category ? 'active' : ''}`}
                onClick={() => void chooseTopic(category)}
                disabled={loading}
              >
                <span>{category}</span>
                <strong>{status?.indexed || 0}/{expected}</strong>
                <small>{status?.complete ? 'Ready to review' : 'Temporarily unavailable'}</small>
              </button>
            );
          })}
        </div>
      </div>
      {loading && <LoadingPanel title="Opening directive topic" detail="Preparing the selected regulatory documents for review." />}
      {errorMessage && <div className="error-panel"><strong>Unable to open the directive library</strong><p>{errorMessage}</p></div>}
      {notice && <div className="status-banner info-banner">{notice}</div>}
      {records.length > 0 ? (
        <ResultsPanel title={`${section} files`} summary="Review the available directives and continue with the source document required for your assessment.">
          <div className={`category-completeness ${selectedCategoryStatus?.complete ? 'complete' : 'incomplete'}`}>
            <div>
              <span>{selectedCategoryStatus?.complete ? 'Topic ready' : 'Topic unavailable'}</span>
              <strong>{selectedCategoryStatus?.files_bundled || records.length}/{selectedCategoryStatus?.expected || records.length} official files available</strong>
            </div>
            <small>{selectedCategoryStatus?.pdfs_bundled || records.filter((item) => item.document_type === 'pdf').length} verified PDF files</small>
          </div>
          <KpiGrid kpis={[
            { label: 'Topic files', value: `${records.length}/${selectedCategoryStatus?.expected || records.length}` },
            { label: 'Verified files', value: selectedCategoryStatus?.complete ? records.length : 'Review' },
            { label: 'PDF files', value: records.filter((item) => item.document_type === 'pdf').length },
            { label: 'Local FSCA requests', value: 0 },
          ]} />
          <div className="tabs" role="tablist" aria-label="Directive library result views">
            <button className="active" onClick={() => setTab('Directives')}>Directives</button>
          </div>
          <TabScroll>
            {tab === 'Directives' && <>
              <div className="filter-row inline-filters">
                <label>Topic<select value={section} onChange={(event) => void chooseTopic(event.target.value)}>{categories.map((item) => <option key={item}>{item}</option>)}</select></label>
                <label>Launch year<select value={year} onChange={(event) => setYear(event.target.value)}>{years.map((item) => <option key={item}>{item}</option>)}</select></label>
                <span className="filter-count">{filtered.length} of {records.length} files</span>
              </div>
              <div className="directive-list crawler-directive-list">
                {filtered.map((item) => (
                  <div className="directive-row bundled-row" key={item.id}>
                    <span className={`file-type-badge ${item.document_type === 'doc' ? 'word' : 'pdf'}`}>{(item.document_type || 'file').toUpperCase()}</span>
                    <span>
                      <strong>{item.title}</strong>
                      <small>{item.filename} · {item.year || 'Unknown year'} · {formatBytes(item.file_size_bytes)}</small>
                    </span>
                    <span className="local-status">Available</span>
                  </div>
                ))}
              </div>
            </>}
          </TabScroll>
          <div className="action-row compact-actions">
            <button className="primary-button" onClick={() => setPage('obligations')}>Continue to Obligation Extraction</button>
            <HomeButton setPage={setPage} />
          </div>
        </ResultsPanel>
      ) : <EmptyGuide title="Choose a directive topic" body="Select a category above to review its available regulatory source documents." />}
    </section>
  );
}

function ObligationPage({ setPage }: { setPage: (page: Page) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [availableDirectives, setAvailableDirectives] = useState<any[]>([]);
  const [directiveName, setDirectiveName] = useState('');
  const [results, setResults] = useState<Results | null>(null);
  const [activeStep, setActiveStep] = useState(0);
  const [tab, setTab] = useState('Obligations');
  const [categoryFilter, setCategoryFilter] = useState('All');
  const [departmentFilter, setDepartmentFilter] = useState('All');
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const [sourceInputKey, setSourceInputKey] = useState(0);

  useEffect(() => {
    fetch(`${API_BASE}/api/obligations/available-directives`, { cache: 'no-store' })
      .then((response) => response.json())
      .then((data) => setAvailableDirectives(data.documents || []))
      .catch(() => undefined);
  }, []);

  const chooseLibraryDirective = (value: string) => {
    setDirectiveName(value);
    if (value) {
      setFile(null);
      setSourceInputKey((key) => key + 1);
    }
    setErrorMessage('');
  };

  const chooseUploadedDirective = (selectedFile: File | null) => {
    setFile(selectedFile);
    if (selectedFile) setDirectiveName('');
    setErrorMessage('');
  };

  const clearLibraryDirective = () => {
    setDirectiveName('');
    setErrorMessage('');
  };

  const clearUploadedDirective = () => {
    setFile(null);
    setSourceInputKey((key) => key + 1);
    setErrorMessage('');
  };

  const clearDirectiveSource = () => {
    setDirectiveName('');
    setFile(null);
    setSourceInputKey((key) => key + 1);
    setResults(null);
    setActiveStep(0);
    setErrorMessage('');
  };

  const extract = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setErrorMessage('');
    setActiveStep(1);
    try {
      const form = new FormData();
      if (file) form.append('file', file);
      if (!file && directiveName) form.append('directive_name', directiveName);
      const response = await fetch(`${API_BASE}/api/obligations/extract`, { method: 'POST', body: form });
      if (!response.ok) throw new Error(await readApiError(response));
      setActiveStep(2);
      const data = await response.json();
      setResults(data);
      setActiveStep(3);
    } catch (error) {
      setErrorMessage(`Extraction failed: ${friendlyApiError(error)}`);
      setActiveStep(0);
    } finally {
      setLoading(false);
    }
  };

  const obligations = results?.tabs?.obligations || [];
  const categories = ['All', ...Array.from(new Set(obligations.map((row: any) => row['Obligation Category']).filter(Boolean)))];
  const departments = ['All', ...Array.from(new Set(obligations.map((row: any) => row['Primary Responsible Department']).filter(Boolean)))];
  const filteredObligations = obligations.filter((row: any) => (categoryFilter === 'All' || row['Obligation Category'] === categoryFilter) && (departmentFilter === 'All' || row['Primary Responsible Department'] === departmentFilter));
  const obligationReviewItems = useMemo<ReviewQueueItem[]>(() => {
    const extractedRows = results?.tabs?.obligations || [];
    const bySection = new Map<string, any>();
    extractedRows.forEach((row: any) => {
      const section = String(row.Section || '');
      if (!bySection.has(section)) bySection.set(section, row);
    });
    return (results?.tabs?.accuracy_review || [])
      .filter((row: any) => String(row['Manual Review Required'] || '').toLowerCase() === 'yes')
      .map((row: any, index: number) => {
        const section = String(row.Section || 'Unknown');
        const extracted = bySection.get(section) || {};
        return {
          id: `obligation-${section}-${index}`,
          section,
          title: extracted.Obligation || 'Review the extracted obligation against its source clause',
          status: extracted.Actionable === 'Yes' ? 'Actionable' : 'Informational',
          priority: extracted.Priority,
          sourcePage: String(row['Source Page'] || extracted['Source Page'] || ''),
          reason: obligationReviewReason(row),
          sourceText: extracted['Language from Directive'],
          missingElements: Number(row['Missing Material Elements'] || 0) > 0 ? 'One or more detected material elements may not be preserved in the extracted wording.' : '',
          recommendation: 'Compare the generated obligation with the cited source page, confirm conditions, deadlines, prohibitions and approvals, then amend before downstream policy review if required.',
        };
      });
  }, [results]);
  const obligationReviewKey = `regulamosaic-review-${results?.output_files?.excel || 'obligations'}`;

  return (
    <section className="page-shell">
      <PageHeader
        utility="Utility 2"
        title="Obligation Extraction"
        description="Choose a directive. We’ll build a review-ready obligation register."
        setPage={setPage}
      />
      <ProgressSteps steps={['Choose source', 'Read document', 'Build register', 'Review']} activeIndex={activeStep} />
      <form className="control-panel glass-panel" onSubmit={extract}>
        <div className="input-panel-heading">
          <div><span>Choose source</span><strong>Use one PDF.</strong></div>
          <button className="ghost-button compact clear-all-button" type="button" onClick={clearDirectiveSource} disabled={(!file && !directiveName) || loading}>Clear</button>
        </div>
        <div className="input-guidance" role="note"><Icon name="info" size={17} /><span>Choose Option A or Option B.</span></div>
        <div className="gap-input-grid two-column-input-grid">
          <div className={`input-card ${directiveName ? 'selected' : ''} ${file ? 'disabled' : ''}`} aria-disabled={Boolean(file)}>
            <div className="input-card-title"><span>Option A</span><strong>Library PDF</strong></div>
            <label>{file ? 'Clear Option B to switch' : 'Choose a directive'}
              <select value={directiveName} onChange={(event) => chooseLibraryDirective(event.target.value)} disabled={Boolean(file) || loading}>
                <option value="">Choose a directive</option>
                {availableDirectives.map((item) => <option key={item.name} value={item.name}>{item.category ? `${item.category} — ` : ''}{item.name}</option>)}
              </select>
            </label>
            {directiveName && <div className="selected-file"><span title={directiveName}>{directiveName}</span><button type="button" onClick={clearLibraryDirective} aria-label="Remove library directive">×</button></div>}
          </div>
          <div className={`input-card ${file ? 'selected' : ''} ${directiveName ? 'disabled' : ''}`} aria-disabled={Boolean(directiveName)}>
            <div className="input-card-title"><span>Option B</span><strong>Upload PDF</strong></div>
            <label>{directiveName ? 'Clear Option A to switch' : 'Choose a PDF'}
              <input key={sourceInputKey} type="file" accept="application/pdf,.pdf" disabled={Boolean(directiveName) || loading} onChange={(event) => chooseUploadedDirective(event.target.files?.[0] || null)} />
            </label>
            {file && <div className="selected-file"><span title={file.name}>{file.name}</span><button type="button" onClick={clearUploadedDirective} aria-label="Remove uploaded directive">×</button></div>}
          </div>
        </div>
        <div className="assessment-submit-row">
          <span>{file || directiveName ? 'Ready.' : 'Choose one PDF.'}</span>
          <button className="primary-button" type="submit" disabled={(!file && !directiveName) || loading}>{loading ? 'Working...' : 'Start Extraction'}</button>
        </div>
      </form>
      {errorMessage && <div className="error-panel"><strong>Unable to complete extraction</strong><p>{errorMessage}</p></div>}
      {loading && <ProcessingJourney stages={['Reading document', 'Identifying requirements', 'Building register', 'Validating results', 'Preparing output']} />}

      {results ? (
        <ResultsPanel
          title="Obligation extraction results"
          summary="Review obligations, source text and flagged items."
        >
          <KpiGrid kpis={results.kpis} />
          <div className="status-banner success-banner">
            {results.accuracy?.actionable_manual_review_rows || 0} row(s) need review before the register is used.
          </div>
          <div className="tabs" role="tablist" aria-label="Obligation result views">{['Obligations', 'Review Queue', 'Text Breakdown', 'Statistics'].map((name) => <button key={name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}>{name}{name === 'Review Queue' && <span className="tab-count">{obligationReviewItems.length}</span>}</button>)}</div>
          <TabScroll>
            {tab === 'Obligations' && <><div className="filter-row inline-filters"><label>Category<select value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>{categories.map((x) => <option key={String(x)}>{String(x)}</option>)}</select></label><label>Responsible department<select value={departmentFilter} onChange={(e) => setDepartmentFilter(e.target.value)}>{departments.map((x) => <option key={String(x)}>{String(x)}</option>)}</select></label><span className="filter-count">{filteredObligations.length} rows</span></div><DataTable rows={filteredObligations} hideInternalQuality /></>}
            {tab === 'Review Queue' && <ReviewQueue key={obligationReviewKey} items={obligationReviewItems} storageKey={obligationReviewKey} emptyMessage="No extracted obligations are currently flagged for additional source verification." />}
            {tab === 'Text Breakdown' && <DataTable rows={results.tabs.text_breakdown || []} />}
            {tab === 'Statistics' && <div className="stats-grid"><DataTable rows={results.tabs.statistics?.category || []} /><DataTable rows={results.tabs.statistics?.department || []} /><DataTable rows={results.tabs.statistics?.priority || []} /><DataTable rows={results.tabs.statistics?.actionable || []} /></div>}
          </TabScroll>
          <div className="action-row compact-actions">
            {results.output_files?.excel && <a className="primary-button" href={`${API_BASE}/api/obligations/outputs/${results.output_files.excel}`}>Download Excel</a>}
            {results.output_files?.csv && <a className="secondary-button" href={`${API_BASE}/api/obligations/outputs/${results.output_files.csv}`}>Download CSV</a>}
            <button className="ghost-button" onClick={() => { setResults(null); setActiveStep(0); }}>New Extraction</button>
            <button className="secondary-button" onClick={() => setPage('gap')}>Proceed to Policy Gap Reviewer</button>
            <HomeButton setPage={setPage} />
          </div>
        </ResultsPanel>
      ) : <EmptyGuide title="Choose a directive" body="Select a library PDF or upload one." />}
    </section>
  );
}

function GapPage({ setPage }: { setPage: (page: Page) => void }) {
  const [availableRegisters, setAvailableRegisters] = useState<any[]>([]);
  const [registerName, setRegisterName] = useState('');
  const [register, setRegister] = useState<File | null>(null);
  const [policy, setPolicy] = useState<File | null>(null);
  const [results, setResults] = useState<Results | null>(null);
  const [activeStep, setActiveStep] = useState(0);
  const [tab, setTab] = useState('Gap Assessment');
  const [statusFilter, setStatusFilter] = useState('All');
  const [reviewOnly, setReviewOnly] = useState(false);
  const [evidenceRow, setEvidenceRow] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const [registerInputKey, setRegisterInputKey] = useState(0);
  const [policyInputKey, setPolicyInputKey] = useState(0);

  useEffect(() => {
    fetch(`${API_BASE}/api/gap/available-registers`)
      .then((response) => response.json())
      .then((data) => setAvailableRegisters(data.registers || []))
      .catch(() => undefined);
  }, []);

  const chooseGeneratedRegister = (value: string) => {
    setRegisterName(value);
    if (value) {
      setRegister(null);
      setRegisterInputKey((key) => key + 1);
    }
    setErrorMessage('');
  };

  const chooseUploadedRegister = (file: File | null) => {
    setRegister(file);
    if (file) setRegisterName('');
    setErrorMessage('');
  };

  const clearGeneratedRegister = () => {
    setRegisterName('');
    setErrorMessage('');
  };

  const clearUploadedRegister = () => {
    setRegister(null);
    setRegisterInputKey((key) => key + 1);
    setErrorMessage('');
  };

  const clearPolicy = () => {
    setPolicy(null);
    setPolicyInputKey((key) => key + 1);
    setErrorMessage('');
  };

  const clearAllInputs = () => {
    setRegisterName('');
    setRegister(null);
    setPolicy(null);
    setRegisterInputKey((key) => key + 1);
    setPolicyInputKey((key) => key + 1);
    setResults(null);
    setActiveStep(0);
    setErrorMessage('');
  };

  const runReview = async (event: FormEvent) => {
    event.preventDefault();
    if ((!register && !registerName) || !policy) return;
    setLoading(true);
    setErrorMessage('');
    setActiveStep(1);
    try {
      const healthResponse = await fetch(`${API_BASE}/api/health`, { cache: 'no-store' });
      if (!healthResponse.ok) throw new Error(await readApiError(healthResponse));
      const health = await healthResponse.json();
      const activeVersion = health?.gap_pipeline?.pipeline_version;
      if (activeVersion !== REQUIRED_GAP_PIPELINE) {
        throw new Error(`The frontend reached gap pipeline ${activeVersion || 'unknown'}, but ${REQUIRED_GAP_PIPELINE} is required. Stop the stale backend or correct VITE_API_BASE_URL, then restart both servers.`);
      }
      const form = new FormData();
      if (register) form.append('register', register);
      if (registerName) form.append('register_name', registerName);
      form.append('policy', policy);
      const response = await fetch(`${API_BASE}/api/gap/review`, { method: 'POST', body: form, cache: 'no-store' });
      if (!response.ok) throw new Error(await readApiError(response));
      const data = await response.json();
      if (data?.pipeline?.pipeline_version !== REQUIRED_GAP_PIPELINE || !data?.pipeline?.run_id) {
        throw new Error('The assessment response has missing or stale pipeline provenance, so no workbook will be offered for download.');
      }
      setResults(data);
      setActiveStep(2);
    } catch (error) {
      setErrorMessage(`Gap review failed: ${friendlyApiError(error)}`);
      setActiveStep(0);
    } finally {
      setLoading(false);
    }
  };

  const rows = results?.tabs?.gap_assessment || [];
  const statuses = ['All', ...Array.from(new Set(rows.map((row: any) => row['Coverage Status']).filter(Boolean)))];
  const filteredRows = rows.filter((row: any) => (
    (statusFilter === 'All' || row['Coverage Status'] === statusFilter)
    && (!reviewOnly || String(row['Manual Review Required'] || '').toLowerCase() === 'yes')
  ));
  const activeExecutiveFilter: ExecutiveFilter | null = reviewOnly
    ? 'review'
    : statusFilter === 'Completely Covered'
      ? 'covered'
      : statusFilter === 'Partially Covered'
        ? 'partial'
        : statusFilter === 'Completely Missing'
          ? 'missing'
          : null;
  const applyExecutiveFilter = (filter: ExecutiveFilter) => {
    if (activeExecutiveFilter === filter) {
      setStatusFilter('All');
      setReviewOnly(false);
      return;
    }
    setReviewOnly(filter === 'review');
    setStatusFilter(
      filter === 'covered'
        ? 'Completely Covered'
        : filter === 'partial'
          ? 'Partially Covered'
          : filter === 'missing'
            ? 'Completely Missing'
            : 'All'
    );
    setTab('Gap Assessment');
  };
  const gapReviewItems = useMemo<ReviewQueueItem[]>(() => (
    (results?.tabs?.gap_assessment || [])
      .filter((row: any) => String(row['Manual Review Required'] || '').toLowerCase() === 'yes')
      .map((row: any, index: number) => ({
        id: `gap-${String(row.Section || 'unknown')}-${index}`,
        section: String(row.Section || 'Unknown'),
        title: row.Obligation || 'Review this policy coverage finding',
        status: row['Coverage Status'],
        priority: row.Priority,
        sourcePage: String(row['Policy Page'] || ''),
        reason: row['Review Rationale'] || 'The generated finding requires qualified professional review.',
        sourceText: row['Language from Directive'],
        evidence: row['Corresponding Policy Text'],
        missingElements: row['Missing Elements'],
        recommendation: row['Policy Gap and Recommendations'],
      }))
  ), [results]);
  const gapReviewKey = `regulamosaic-review-${results?.pipeline?.run_id || results?.output_files?.excel || 'gap'}`;

  return (
    <section className="page-shell">
      <PageHeader
        utility="Utility 3"
        title="Policy Gap Reviewer"
        description="Compare an obligation register with an internal policy."
        setPage={setPage}
      />
      <ProgressSteps steps={['Choose files', 'Compare', 'Results']} activeIndex={activeStep} />
      <form className="control-panel glass-panel" onSubmit={runReview}>
        <div className="input-panel-heading">
          <div><span>Choose files</span><strong>One register + one policy.</strong></div>
          <button className="ghost-button compact clear-all-button" type="button" onClick={clearAllInputs} disabled={(!register && !registerName && !policy) || loading}>Clear all</button>
        </div>
        <div className="input-guidance" role="note"><Icon name="info" size={17} /><span>Choose Option A or Option B.</span></div>
        <div className="gap-input-grid">
          <div className={`input-card ${registerName ? 'selected' : ''} ${register ? 'disabled' : ''}`} aria-disabled={Boolean(register)}>
            <div className="input-card-title"><span>Option A</span><strong>Saved register</strong></div>
            <label>{register ? 'Clear Option B to switch' : 'Pick a register'}
              <select value={registerName} onChange={(event) => chooseGeneratedRegister(event.target.value)} disabled={Boolean(register) || loading}>
                <option value="">Choose a register</option>
                {availableRegisters.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
              </select>
            </label>
            {registerName && <div className="selected-file"><span title={registerName}>{registerName}</span><button type="button" onClick={clearGeneratedRegister} aria-label="Remove selected generated register">×</button></div>}
          </div>
          <div className={`input-card ${register ? 'selected' : ''} ${registerName ? 'disabled' : ''}`} aria-disabled={Boolean(registerName)}>
            <div className="input-card-title"><span>Option B</span><strong>Upload register</strong></div>
            <label>{registerName ? 'Clear Option A to switch' : 'Excel or CSV file'}
              <input key={registerInputKey} type="file" accept=".xlsx,.xls,.csv" disabled={Boolean(registerName) || loading} onChange={(event) => chooseUploadedRegister(event.target.files?.[0] || null)} />
            </label>
            {register && <div className="selected-file"><span title={register.name}>{register.name}</span><button type="button" onClick={clearUploadedRegister} aria-label="Remove uploaded obligation register">×</button></div>}
          </div>
          <div className={`input-card policy-card ${policy ? 'selected' : ''}`}>
            <div className="input-card-title"><span>Required</span><strong>Internal policy</strong></div>
            <label>Policy PDF
              <input key={policyInputKey} type="file" accept="application/pdf" disabled={loading} onChange={(event) => setPolicy(event.target.files?.[0] || null)} />
            </label>
            {policy && <div className="selected-file"><span title={policy.name}>{policy.name}</span><button type="button" onClick={clearPolicy} aria-label="Remove internal policy">×</button></div>}
          </div>
        </div>
        <div className="assessment-submit-row">
          <span>{(register || registerName) && policy ? 'Ready.' : 'Add a register and policy.'}</span>
          <button className="primary-button" type="submit" disabled={(!register && !registerName) || !policy || loading}>{loading ? 'Working...' : 'Start Review'}</button>
        </div>
      </form>
      {errorMessage && <div className="error-panel"><strong>Unable to complete gap review</strong><p>{errorMessage}</p></div>}
      {loading && <ProcessingJourney stages={['Reading documents', 'Identifying requirements', 'Matching evidence', 'Validating results', 'Preparing output']} />}
      {results ? (
        <ResultsPanel
          title="Policy gap review results"
          summary="Review coverage, evidence and recommended actions."
        >
          <ExecutiveSummary rows={rows} active={activeExecutiveFilter} onSelect={applyExecutiveFilter} />
          <KpiGrid kpis={results.kpis} />
          {results.gap_quality && <div className="status-banner success-banner">
            {results.gap_quality.population} obligations reviewed · {results.gap_quality.gap_rows} gaps · {results.gap_quality.manual_review_rows} need review.
          </div>}
          <div className="tabs" role="tablist" aria-label="Gap review result views">{['Gap Assessment', 'Review Queue', 'Statistics'].map((name) => <button key={name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}>{name}{name === 'Review Queue' && <span className="tab-count">{gapReviewItems.length}</span>}</button>)}</div>
          <TabScroll>
            {tab === 'Gap Assessment' && <><div className="filter-row inline-filters"><label>Coverage status<select value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setReviewOnly(false); }}>{statuses.map((x) => <option key={String(x)}>{String(x)}</option>)}</select></label><span className="filter-count">{reviewOnly ? 'Needs review · ' : ''}{filteredRows.length} rows · Select a row for evidence</span></div><DataTable rows={filteredRows} hideInternalQuality onRowClick={setEvidenceRow} /></>}
            {tab === 'Review Queue' && <ReviewQueue key={gapReviewKey} items={gapReviewItems} storageKey={gapReviewKey} emptyMessage="No policy findings are currently flagged for additional professional review." />}
            {tab === 'Statistics' && <div className="stats-grid"><DataTable rows={results.tabs.statistics?.status || []} /><DataTable rows={results.tabs.statistics?.category || []} /><DataTable rows={results.tabs.statistics?.department || []} /><DataTable rows={results.tabs.statistics?.priority || []} /></div>}
          </TabScroll>
          <div className="action-row compact-actions">
            {results.output_files?.excel && <a className="primary-button" href={`${API_BASE}/api/gap/outputs/${results.output_files.excel}?run=${encodeURIComponent(results.pipeline?.run_id || '')}`}>Download Excel</a>}
            {results.output_files?.csv && <a className="secondary-button" href={`${API_BASE}/api/gap/outputs/${results.output_files.csv}?run=${encodeURIComponent(results.pipeline?.run_id || '')}`}>Download CSV</a>}
            <button className="ghost-button" onClick={() => { setResults(null); setActiveStep(0); }}>New Gap Assessment</button>
            <button className="secondary-button" onClick={() => setPage('obligations')}>Back to Obligation Extraction</button>
            <HomeButton setPage={setPage} />
          </div>
        </ResultsPanel>
      ) : <EmptyGuide title="Choose your files" body="Add one register and one policy PDF." />}
      <EvidenceDrawer row={evidenceRow} onClose={() => setEvidenceRow(null)} />
    </section>
  );
}

function DiagnosticsPage({ setPage }: { setPage: (page: Page) => void }) {
  const [checks, setChecks] = useState<any[]>([]);
  const [pipeline, setPipeline] = useState<PipelineInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState('');

  const refresh = async () => {
    setLoading(true);
    setErrorMessage('');
    try {
      const response = await fetch(`${API_BASE}/api/diagnostics`, { cache: 'no-store' });
      if (!response.ok) throw new Error(await readApiError(response));
      const data = await response.json();
      setChecks(data.checks || []);
      setPipeline(data.pipeline || null);
    } catch (error) {
      setErrorMessage(friendlyApiError(error));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(); }, []);

  return (
    <section className="page-shell">
      <PageHeader utility="System" title="Diagnostics" description="Confirm crawler traffic guardrails, backend health, pipeline versions, benchmark readiness, storage, and OCR." setPage={setPage} />
      <div className="control-panel glass-panel">
        <button className="primary-button" onClick={refresh} disabled={loading}>{loading ? 'Checking...' : 'Run Diagnostics'}</button>
        {pipeline && <span className="diagnostic-version">Gap pipeline {pipeline.pipeline_version} · {pipeline.source_sha256?.slice(0, 16)}</span>}
      </div>
      {loading && <LoadingPanel title="Running diagnostics" detail="Checking the active backend and local workflow dependencies." />}
      {errorMessage && <div className="error-panel"><strong>Diagnostics unavailable</strong><p>{errorMessage}</p></div>}
      {!loading && checks.length > 0 && <ResultsPanel title="System readiness" summary="Live checks from the backend currently serving this frontend."><div className="diagnostic-grid">{checks.map((check) => <div className="diagnostic-card" key={check.component}><span className={`health-dot ${statusTone(check.status)}`} /><div><small>{check.component}</small><strong>{check.status}</strong><p>{check.detail}</p></div></div>)}</div></ResultsPanel>}
    </section>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>('home');

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }, [page]);

  useEffect(() => {
    const nodes = Array.from(document.querySelectorAll<HTMLElement>('.reveal-on-scroll'));
    if (!nodes.length) return;
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.14 });
    nodes.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [page]);

  return (
    <main>
      <nav className="topbar">
        <button className="brand-mark" onClick={() => setPage('home')} aria-label="Go to home"><EYLogo /></button>
        <span className="brand-divider" />
        <button className="topbar-title" onClick={() => setPage('home')} aria-label="RegulaMosaic home"><strong><span>Regula</span>Mosaic</strong><small>Regulatory Intelligence Workspace</small></button>
        <span className="topbar-live"><i />Review workspace ready</span>
        <div className="topbar-pills">
          <button className={page === 'home' ? 'active' : ''} onClick={() => setPage('home')}><Icon name="home" size={15} />Home</button>
          <button className={page === 'crawler' ? 'active' : ''} onClick={() => setPage('crawler')}><Icon name="library" size={15} />Library</button>
          <button className={page === 'obligations' ? 'active' : ''} onClick={() => setPage('obligations')}><Icon name="obligations" size={15} />Obligations</button>
          <button className={page === 'gap' ? 'active' : ''} onClick={() => setPage('gap')}><Icon name="gap" size={15} />Gap Review</button>
          <button className={page === 'diagnostics' ? 'active' : ''} onClick={() => setPage('diagnostics')}><Icon name="diagnostics" size={15} />Diagnostics</button>
        </div>
      </nav>
      <div className="page-stage" key={page}>
        {page === 'home' && <HomePage setPage={setPage} />}
        {page === 'crawler' && <CrawlerPage setPage={setPage} />}
        {page === 'obligations' && <ObligationPage setPage={setPage} />}
        {page === 'gap' && <GapPage setPage={setPage} />}
        {page === 'diagnostics' && <DiagnosticsPage setPage={setPage} />}
      </div>
      <footer><span><b>Regula</b>Mosaic</span><small className="ai-disclaimer"><Icon name="info" size={15} />AI-generated. A qualified compliance professional must review all outputs.</small></footer>
    </main>
  );
}
