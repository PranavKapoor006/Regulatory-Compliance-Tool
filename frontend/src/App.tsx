import { FormEvent, useEffect, useMemo, useState, type ReactNode } from 'react';

type Page = 'home' | 'crawler' | 'obligations' | 'gap';
type TabName = string;

type DirectiveRecord = {
  id: string;
  title: string;
  section: string;
  category: string;
  year: string;
  source_link: string;
  filename?: string;
  cached?: boolean;
  downloaded?: boolean;
};

type Kpi = { label: string; value: string | number };
type LogRow = { stage: string; status: string; message: string; row_count: number };
type Results = { kpis: Kpi[]; tabs: Record<string, any>; logs: LogRow[]; output_files?: Record<string, string> };

const API_BASE = '';

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
  return (
    <div className="kpi-grid">
      {kpis.map((kpi) => (
        <div className="kpi-card" key={kpi.label}>
          <span>{kpi.label}</span>
          <strong>{kpi.value}</strong>
        </div>
      ))}
    </div>
  );
}

function DataTable({ rows, maxRows = 25 }: { rows: any[]; maxRows?: number }) {
  const columns = useMemo(() => {
    const first = rows?.[0] || {};
    return Object.keys(first);
  }, [rows]);

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
            <tr key={rowIndex}>
              {columns.map((col) => <td key={col}>{String(row[col] ?? '')}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > maxRows && <p className="table-note">Showing first {maxRows} of {rows.length} rows. Download the output for the full register.</p>}
    </div>
  );
}

function HomeButton({ setPage }: { setPage: (page: Page) => void }) {
  return <button className="ghost-button compact" onClick={() => setPage('home')}>Home</button>;
}

function PageHeader({ utility, title, description, setPage }: { utility: string; title: string; description: string; setPage: (page: Page) => void }) {
  return (
    <div className="page-header glass-panel">
      <div>
        <p className="eyebrow">{utility}</p>
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

function HomePage({ setPage }: { setPage: (page: Page) => void }) {
  const cards = [
    {
      page: 'crawler' as Page,
      label: 'Utility 01',
      title: 'Web Crawler',
      subtitle: 'Regulatory source intake',
      body: 'Filter FSCA directives by category and year, select relevant documents, and store them for downstream review.',
      bullets: ['Category and year filters', 'Multi-select downloads', 'Crawl evidence log'],
    },
    {
      page: 'obligations' as Page,
      label: 'Utility 02',
      title: 'Obligation Extraction',
      subtitle: 'Directive-to-obligation conversion',
      body: 'Break directive text into clause-level sections and generate an obligation register with categories and ownership fields.',
      bullets: ['Clause-wise breakdown', 'Obligation register', 'Excel and CSV export'],
    },
    {
      page: 'gap' as Page,
      label: 'Utility 03',
      title: 'Policy Gap Reviewer',
      subtitle: 'Policy alignment assessment',
      body: 'Compare obligations against internal policy evidence and produce coverage findings with targeted recommendations.',
      bullets: ['Coverage status review', 'Policy evidence mapping', 'Gap recommendations'],
    },
  ];

  return (
    <section className="hero">
      <div className="hero-layout">
        <div className="hero-copy">
          <p className="eyebrow">EY regulatory compliance workspace</p>
          <h1>EY Regulatory Compliance Tool</h1>
          <p>
            A professional workflow for crawling FSCA directives, extracting regulatory obligations, and assessing internal policy alignment with evidence-backed outputs.
          </p>
          <div className="hero-actions">
            <button className="primary-button" onClick={() => setPage('crawler')}>Start with Web Crawler</button>
            <button className="secondary-button" onClick={() => setPage('obligations')}>Upload Directive PDF</button>
          </div>
        </div>
        <div className="workflow-card glass-panel">
          <span>Configured source</span>
          <strong>FSCA Directives Review</strong>
          <p>Use the utilities independently or move through the connected review path from directive discovery to gap assessment.</p>
          <div className="workflow-steps">
            <small>01 Crawl</small>
            <small>02 Extract</small>
            <small>03 Review</small>
          </div>
        </div>
      </div>

      <div className="utility-grid">
        {cards.map((card) => (
          <button className="utility-card" key={card.title} onClick={() => setPage(card.page)}>
            <span>{card.label}</span>
            <h2>{card.title}</h2>
            <h3>{card.subtitle}</h3>
            <p>{card.body}</p>
            <ul>{card.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}</ul>
          </button>
        ))}
      </div>
    </section>
  );
}

function CrawlerPage({ setPage }: { setPage: (page: Page) => void }) {
  const [sections, setSections] = useState<string[]>(['All']);
  const [years, setYears] = useState<string[]>(['All']);
  const [section, setSection] = useState('All');
  const [year, setYear] = useState('All');
  const [directives, setDirectives] = useState<DirectiveRecord[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [activeStep, setActiveStep] = useState(0);
  const [tab, setTab] = useState<TabName>('Documents');
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');

  useEffect(() => {
    fetch(`${API_BASE}/api/crawler/metadata`)
      .then((res) => res.json())
      .then((data) => {
        setSections(data.sections?.length ? data.sections : ['All']);
        setYears(data.years?.length ? data.years : ['All']);
      })
      .catch(() => undefined);
  }, []);

  const runCrawl = async () => {
    setLoading(true);
    setErrorMessage('');
    setActiveStep(1);
    try {
      const response = await fetch(`${API_BASE}/api/crawler/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section, year }),
      });
      if (!response.ok) throw new Error(await readApiError(response));
      const data = await response.json();
      setDirectives(data.records || []);
      setLogs(data.logs || []);
      setSelected([]);
      setTab('Documents');
      setActiveStep(2);
    } catch (error) {
      setErrorMessage(`Crawler failed: ${friendlyApiError(error)}`);
      setActiveStep(0);
    } finally {
      setLoading(false);
    }
  };

  const downloadSelected = async () => {
    if (!selected.length) return;
    setLoading(true);
    setErrorMessage('');
    try {
      const response = await fetch(`${API_BASE}/api/crawler/download`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directive_ids: selected }),
      });
      if (!response.ok) throw new Error(await readApiError(response));
      const data = await response.json();
      const downloadedIds = new Set((data.downloaded || []).map((item: DirectiveRecord) => item.id));
      setLogs([...(logs || []), ...(data.logs || [])]);
      setDirectives((prev) => prev.map((item) => downloadedIds.has(item.id) ? { ...item, downloaded: true, cached: true } : item));
      if ((data.downloaded || []).length > 0) setSelected([]);
    } catch (error) {
      setErrorMessage(`Download failed: ${friendlyApiError(error)}`);
    } finally {
      setLoading(false);
    }
  };

  const kpis: Kpi[] = [
    { label: 'Total Directives', value: directives.length },
    { label: 'Domains', value: new Set(directives.map((d) => d.section)).size || 0 },
    { label: 'Downloaded', value: directives.filter((d) => d.downloaded).length },
    { label: 'Cached', value: directives.filter((d) => d.cached).length },
  ];

  const dataRows = directives.map((d, index) => ({
    'Doc no.': index + 1,
    Title: d.title,
    Section: d.section,
    Year: d.year,
    File: d.filename || 'Not downloaded',
    Source: (d as any).source_type || 'FSCA',
  }));

  return (
    <section className="page-shell">
      <PageHeader
        utility="Utility 1"
        title="Web Crawler"
        description="Browse, filter, select, and download FSCA directives for downstream obligation extraction."
        setPage={setPage}
      />
      <ProgressSteps steps={['Configure', 'Crawl', 'Results']} activeIndex={activeStep} />

      <div className="control-panel glass-panel">
        <label>Section / Category<select value={section} onChange={(e) => setSection(e.target.value)}>{sections.map((x) => <option key={x}>{x}</option>)}</select></label>
        <label>Year<select value={year} onChange={(e) => setYear(e.target.value)}>{years.map((x) => <option key={x}>{x}</option>)}</select></label>
        <button className="primary-button" onClick={runCrawl}>{loading ? 'Working...' : 'Start Crawling'}</button>
        <button className="ghost-button" onClick={() => { setSection('All'); setYear('All'); setSelected([]); }}>Reset Filters</button>
      </div>

      {errorMessage && <div className="error-panel"><strong>Unable to complete crawler action</strong><p>{errorMessage}</p></div>}

      {directives.length === 0 ? (
        <EmptyGuide title="No directives loaded" body="Choose a section and year, then start crawling to display matching FSCA directives." />
      ) : (
        <ResultsPanel
          title="Crawler results"
          summary="Review matching directives, confirm selected documents, and download files for obligation extraction."
        >
          <KpiGrid kpis={kpis} />
          <div className="tabs">
            {['Documents', 'Data Table', 'Crawl Log'].map((name) => <button key={name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}>{name}</button>)}
          </div>
          <TabScroll>
            {tab === 'Documents' && <div className="directive-list">
              {directives.map((directive) => (
                <label className="directive-row" key={directive.id}>
                  <input type="checkbox" checked={selected.includes(directive.id)} onChange={(e) => setSelected((prev) => e.target.checked ? [...prev, directive.id] : prev.filter((id) => id !== directive.id))} />
                  <span><strong>{directive.title}</strong><small>{directive.section} | {directive.year} | {(directive as any).source_type || 'FSCA'} | {directive.cached ? 'Cached' : 'Not cached'}</small></span>
                  <a href={directive.source_link} target="_blank" rel="noreferrer">Source</a>
                </label>
              ))}
            </div>}
            {tab === 'Data Table' && <DataTable rows={dataRows} />}
            {tab === 'Crawl Log' && <DataTable rows={logs} />}
          </TabScroll>
          <div className="action-row compact-actions">
            <button className="primary-button" onClick={downloadSelected} disabled={!selected.length}>Download Selected</button>
            <button className="secondary-button" onClick={() => setPage('obligations')}>Proceed to Obligation Extraction</button>
            <button className="ghost-button" onClick={() => { setDirectives([]); setSelected([]); setLogs([]); setActiveStep(0); }}>New Crawl</button>
            <HomeButton setPage={setPage} />
          </div>
        </ResultsPanel>
      )}
    </section>
  );
}

function ObligationPage({ setPage }: { setPage: (page: Page) => void }) {
  const [available, setAvailable] = useState<any[]>([]);
  const [directiveName, setDirectiveName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [results, setResults] = useState<Results | null>(null);
  const [activeStep, setActiveStep] = useState(0);
  const [tab, setTab] = useState('Obligations');
  const [categoryFilter, setCategoryFilter] = useState('All');
  const [departmentFilter, setDepartmentFilter] = useState('All');
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');

  useEffect(() => {
    fetch(`${API_BASE}/api/obligations/available-directives`).then((res) => res.json()).then((data) => setAvailable(data.documents || [])).catch(() => undefined);
  }, []);

  const extract = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setErrorMessage('');
    setActiveStep(1);
    try {
      const form = new FormData();
      if (file) form.append('file', file);
      if (directiveName) form.append('directive_name', directiveName);
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

  return (
    <section className="page-shell">
      <PageHeader
        utility="Utility 2"
        title="Obligation Extraction"
        description="Generate a regulatory text breakdown and obligation register from an FSCA directive."
        setPage={setPage}
      />
      <ProgressSteps steps={['Select PDF', 'Breakdown', 'Extraction', 'Results']} activeIndex={activeStep} />
      <form className="control-panel glass-panel" onSubmit={extract}>
        <label>Select crawler directive<select value={directiveName} onChange={(e) => { setDirectiveName(e.target.value); setFile(null); }}><option value="">Upload new PDF instead</option>{available.map((doc) => <option key={doc.name} value={doc.name}>{doc.name}</option>)}</select></label>
        <label>Upload PDF<input type="file" accept="application/pdf" onChange={(e) => { setFile(e.target.files?.[0] || null); setDirectiveName(''); }} /></label>
        <button className="primary-button" type="submit" disabled={loading}>{loading ? 'Extracting...' : 'Start Extraction'}</button>
      </form>
      {errorMessage && <div className="error-panel"><strong>Unable to complete extraction</strong><p>{errorMessage}</p></div>}

      {results ? (
        <ResultsPanel
          title="Obligation extraction results"
          summary="Review the generated register, clause breakdown, statistics, and process log inside this workspace."
        >
          <KpiGrid kpis={results.kpis} />
          <div className="tabs">{['Obligations', 'Text Breakdown', 'Statistics', 'Process Log'].map((name) => <button key={name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}>{name}</button>)}</div>
          <TabScroll>
            {tab === 'Obligations' && <><div className="filter-row inline-filters"><select value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>{categories.map((x) => <option key={String(x)}>{String(x)}</option>)}</select><select value={departmentFilter} onChange={(e) => setDepartmentFilter(e.target.value)}>{departments.map((x) => <option key={String(x)}>{String(x)}</option>)}</select></div><DataTable rows={filteredObligations} /></>}
            {tab === 'Text Breakdown' && <DataTable rows={results.tabs.text_breakdown || []} />}
            {tab === 'Statistics' && <div className="stats-grid"><DataTable rows={results.tabs.statistics?.category || []} /><DataTable rows={results.tabs.statistics?.department || []} /><DataTable rows={results.tabs.statistics?.priority || []} /><DataTable rows={results.tabs.statistics?.actionable || []} /></div>}
            {tab === 'Process Log' && <DataTable rows={results.logs || []} />}
          </TabScroll>
          <div className="action-row compact-actions">
            {results.output_files?.excel && <a className="primary-button" href={`${API_BASE}/api/obligations/outputs/${results.output_files.excel}`}>Download Excel</a>}
            {results.output_files?.csv && <a className="secondary-button" href={`${API_BASE}/api/obligations/outputs/${results.output_files.csv}`}>Download CSV</a>}
            <button className="ghost-button" onClick={() => { setResults(null); setActiveStep(0); }}>New Extraction</button>
            <button className="secondary-button" onClick={() => setPage('gap')}>Proceed to Policy Gap Reviewer</button>
            <HomeButton setPage={setPage} />
          </div>
        </ResultsPanel>
      ) : <EmptyGuide title="Ready for extraction" body="Select a downloaded directive or upload a new PDF to generate the text breakdown and obligation register." />}
    </section>
  );
}

function GapPage({ setPage }: { setPage: (page: Page) => void }) {
  const [register, setRegister] = useState<File | null>(null);
  const [policy, setPolicy] = useState<File | null>(null);
  const [results, setResults] = useState<Results | null>(null);
  const [activeStep, setActiveStep] = useState(0);
  const [tab, setTab] = useState('Gap Assessment');
  const [statusFilter, setStatusFilter] = useState('All');
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');

  const runReview = async (event: FormEvent) => {
    event.preventDefault();
    if (!register || !policy) return;
    setLoading(true);
    setErrorMessage('');
    setActiveStep(1);
    try {
      const form = new FormData();
      form.append('register', register);
      form.append('policy', policy);
      const response = await fetch(`${API_BASE}/api/gap/review`, { method: 'POST', body: form });
      if (!response.ok) throw new Error(await readApiError(response));
      const data = await response.json();
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
  const filteredRows = rows.filter((row: any) => statusFilter === 'All' || row['Coverage Status'] === statusFilter);

  return (
    <section className="page-shell">
      <PageHeader
        utility="Utility 3"
        title="Policy Gap Reviewer"
        description="Validate obligations against uploaded internal policy evidence and generate coverage findings."
        setPage={setPage}
      />
      <ProgressSteps steps={['Select Inputs', 'Gap Analysis', 'Results']} activeIndex={activeStep} />
      <form className="control-panel glass-panel" onSubmit={runReview}>
        <label>Obligation register<input type="file" accept=".xlsx,.xls,.csv" onChange={(e) => setRegister(e.target.files?.[0] || null)} /></label>
        <label>Internal policy PDF<input type="file" accept="application/pdf" onChange={(e) => setPolicy(e.target.files?.[0] || null)} /></label>
        <button className="primary-button" type="submit" disabled={!register || !policy || loading}>{loading ? 'Reviewing...' : 'Start Gap Assessment'}</button>
      </form>
      {errorMessage && <div className="error-panel"><strong>Unable to complete gap review</strong><p>{errorMessage}</p></div>}
      {results ? (
        <ResultsPanel
          title="Policy gap review results"
          summary="Review coverage status, supporting policy evidence, statistics, and processing notes in a compact workspace."
        >
          <KpiGrid kpis={results.kpis} />
          <div className="tabs">{['Gap Assessment', 'Statistics', 'Process Log'].map((name) => <button key={name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}>{name}</button>)}</div>
          <TabScroll>
            {tab === 'Gap Assessment' && <><div className="filter-row inline-filters"><select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>{statuses.map((x) => <option key={String(x)}>{String(x)}</option>)}</select></div><DataTable rows={filteredRows} /></>}
            {tab === 'Statistics' && <div className="stats-grid"><DataTable rows={results.tabs.statistics?.status || []} /><DataTable rows={results.tabs.statistics?.category || []} /><DataTable rows={results.tabs.statistics?.department || []} /><DataTable rows={results.tabs.statistics?.priority || []} /></div>}
            {tab === 'Process Log' && <DataTable rows={results.logs || []} />}
          </TabScroll>
          <div className="action-row compact-actions">
            {results.output_files?.excel && <a className="primary-button" href={`${API_BASE}/api/gap/outputs/${results.output_files.excel}`}>Download Excel</a>}
            {results.output_files?.csv && <a className="secondary-button" href={`${API_BASE}/api/gap/outputs/${results.output_files.csv}`}>Download CSV</a>}
            <button className="ghost-button" onClick={() => { setResults(null); setActiveStep(0); }}>New Gap Assessment</button>
            <button className="secondary-button" onClick={() => setPage('obligations')}>Back to Obligation Extraction</button>
            <HomeButton setPage={setPage} />
          </div>
        </ResultsPanel>
      ) : <EmptyGuide title="Ready for gap assessment" body="Upload an obligation register and internal policy PDF to validate coverage and generate a gap assessment." />}
    </section>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>('home');

  return (
    <main>
      <nav className="topbar">
        <button className="brand-mark" onClick={() => setPage('home')} aria-label="Go to home">EY</button>
        <div className="topbar-title"><strong>EY Regulatory Compliance Tool</strong><span>FSCA Directive Review Workspace</span></div>
        <div className="topbar-pills">
          <button className={page === 'crawler' ? 'active' : ''} onClick={() => setPage('crawler')}>Crawler</button>
          <button className={page === 'obligations' ? 'active' : ''} onClick={() => setPage('obligations')}>Obligations</button>
          <button className={page === 'gap' ? 'active' : ''} onClick={() => setPage('gap')}>Gap Review</button>
        </div>
      </nav>
      {page === 'home' && <HomePage setPage={setPage} />}
      {page === 'crawler' && <CrawlerPage setPage={setPage} />}
      {page === 'obligations' && <ObligationPage setPage={setPage} />}
      {page === 'gap' && <GapPage setPage={setPage} />}
    </main>
  );
}
