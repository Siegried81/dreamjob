/**
 * Opportunities - the ranked list (FR-281..285, FR-383, FR-263).
 *
 * This is the screen the whole pipeline exists to produce, and the one place
 * where the machine's opinion and the job seeker's opinion meet. Three rules
 * shape everything below:
 *
 *   FR-263  a speculative opening is never dressed up as a vacancy. Every row
 *           carries <KindBadge>, and the CSS gives speculative rows their own
 *           left border and colour so the distinction survives a glance.
 *   FR-284  the seeker's controls - check, pin, drag, reject with a reason -
 *           belong to the seeker. A recalculation writes scores and nothing
 *           else, so a manual position outlives it.
 *   NFR-305 the score is advisory. It orders a list; it decides nothing.
 *
 * The backend does the filtering, sorting and ordering (GET /api/opportunities
 * with `respect_manual_order`), so what the screen shows is what the database
 * would return to any other client - no client-side re-ranking that could
 * disagree with the stored order.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import { useLiveEvent } from '../api/liveEvents'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap from '../components/WorkflowMap'
import { useSession } from '../session'
import { EmployerCompanyLine } from './employers'
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  KindBadge,
  Loading,
  Meter,
  Modal,
  SubScores,
  formatDate,
  formatMoney,
  useFetch,
} from '../components/ui'

/** repo.SORT_EXPRESSIONS - the sort keys the ranked list accepts (FR-283). */
const SORT_OPTIONS = [
  { value: 'score', label: 'Overall score, best first' },
  { value: 'score_asc', label: 'Overall score, worst first' },
  { value: 'dream_fit', label: 'Dream-job fit' },
  { value: 'profile_fit', label: 'Profile fit' },
  { value: 'compensation', label: 'Compensation, upper estimate' },
  { value: 'plausibility', label: 'Plausibility' },
  { value: 'posted', label: 'Most recently posted' },
  { value: 'created', label: 'Most recently found' },
  { value: 'title', label: 'Title A–Z' },
  { value: 'company', label: 'Company A–Z' },
]

/** FR-382: the two tags the intelligence screens read back. */
const TAGS = [
  { value: 'destination', label: 'Destination' },
  { value: 'stepping_stone', label: 'Stepping stone' },
]

/** signals.timing_flag_for() writes one of these, or nothing (FR-402). */
const TIMING_LABELS = { apply_now: 'Apply now', favourable: 'Favourable window' }

const PAGE_SIZE = 50

/** Live arrivals are batched: a recheck can add a dozen rows in a second. */
const LIVE_REFRESH_MS = 1500

/**
 * "New since your last visit" is remembered per browser and per account, in
 * localStorage. Storage can be missing or refuse access (private windows,
 * blocked site data), so every access is guarded and a failure simply means
 * nothing is marked.
 */
function lastVisitKey(seekerId) {
  return `dreamjob.opportunities.lastVisit.${seekerId || 'anon'}`
}

function readLastVisit(seekerId) {
  try {
    const value = window.localStorage.getItem(lastVisitKey(seekerId))
    return value && Number.isFinite(Date.parse(value)) ? value : null
  } catch {
    return null
  }
}

function writeLastVisit(seekerId, iso) {
  try {
    window.localStorage.setItem(lastVisitKey(seekerId), iso)
  } catch {
    /* unavailable storage only costs the marker */
  }
}

/** True when the row was created after the previous visit (never on a first visit). */
function isNewSince(item, lastVisit) {
  if (!lastVisit || !item.created_at) return false
  const created = Date.parse(item.created_at)
  return Number.isFinite(created) && created > Date.parse(lastVisit)
}

/** The autopilot's stages, in the words a job seeker would use (FR-162). */
const AUTOPILOT_STAGE = {
  composite: 'Reading your profile',
  dream_job: 'Understanding what you want',
  directives: 'Deciding what to search for',
  campaign: 'Setting up the search',
  plan: 'Choosing which sources to use',
  collection: 'Collecting and ranking opportunities',
  profiling: 'Looking into the companies',
  notify: 'Finishing up',
}

/**
 * The ranked-opportunity count on a campaign row, when the API carries one.
 * GET /campaigns currently returns the raw campaign columns, so most rows have
 * no count; the screen then falls back to "Every campaign" rather than risk
 * opening on a campaign whose search found nothing.
 */
function campaignOpportunityCount(campaign) {
  const candidates = [
    campaign?.opportunities,
    campaign?.opportunity_count,
    campaign?.opportunities_count,
    campaign?.count,
    campaign?.counts?.opportunities,
  ]
  for (const value of candidates) {
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

/**
 * What a zero-result run had to say about the sources. The backend marks such
 * a run with `report.reason = "no_records"` and a `report.sources` summary;
 * runs recorded before that fix carry neither, so every read here tolerates
 * absence and the screen falls back to plain prose.
 */
function emptySearchSummary(report) {
  const source = report?.sources
  const counts = report?.counts || {}
  const errors = []
  let failed = 0
  let blocked = 0

  const number = (value) => {
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (Array.isArray(value)) return value.length
    return 0
  }
  const addError = (label, text) => {
    const message = String(text ?? '').trim()
    if (!message) return
    const name = String(label ?? '').trim()
    errors.push(name ? `${name}: ${message}` : message)
  }
  const blockedOutcome = (outcome) => {
    const value = String(outcome ?? '').toLowerCase()
    return value.includes('block') || value.includes('consent') || value.includes('gone')
  }
  const failedOutcome = (outcome) => {
    const value = String(outcome ?? '').toLowerCase()
    return value === 'failed' || value === 'error' || value.includes('timeout')
  }

  if (Array.isArray(source)) {
    for (const item of source) {
      const outcome = item?.outcome ?? item?.status ?? item?.state
      if (blockedOutcome(outcome)) blocked += 1
      else if (failedOutcome(outcome)) failed += 1
      else continue
      addError(
        item?.adapter_key || item?.source || item?.name || item?.site,
        item?.last_error || item?.error || item?.reason,
      )
    }
  } else if (source && typeof source === 'object') {
    failed = number(source.failed ?? source.failed_count)
    blocked =
      number(source.blocked ?? source.blocked_count) + number(source.gone ?? source.gone_count)
    const listed = source.errors || source.top_errors || source.failures || source.problems
    if (Array.isArray(listed)) {
      for (const item of listed) {
        if (typeof item === 'string') addError('', item)
        else {
          addError(
            item?.adapter_key || item?.source || item?.name || item?.site,
            item?.last_error || item?.error || item?.reason,
          )
        }
      }
    } else if (listed && typeof listed === 'object') {
      for (const [name, value] of Object.entries(listed)) {
        addError(
          name,
          typeof value === 'string' ? value : value?.error || value?.last_error || value?.reason,
        )
      }
    }
  }

  const sourceCounts = source && typeof source === 'object' && !Array.isArray(source) ? source : {}
  const rejected = number(
    counts.rejected_out_of_scope ??
      counts.out_of_scope ??
      counts.rejected ??
      sourceCounts.rejected_out_of_scope ??
      sourceCounts.out_of_scope ??
      sourceCounts.rejected ??
      report?.rejected_out_of_scope ??
      report?.rejected,
  )
  return { failed, blocked, errors: errors.slice(0, 3), rejected }
}

/** The plain sentence under "no opportunity passed your directives". */
function describeEmptySearch({ failed, blocked, rejected }) {
  const parts = []
  const problem = failed + blocked
  if (problem > 0) {
    parts.push(`${problem} source${problem === 1 ? ' was' : 's were'} blocked or failed`)
  }
  if (rejected > 0) {
    parts.push(
      `${rejected} result${rejected === 1 ? ' was' : 's were'} rejected as out of scope`,
    )
  }
  if (!parts.length) return 'Every source it planned returned nothing usable.'
  return `${parts.join(', and ')}.`
}

const EMPTY_FILTERS = {
  q: '',
  kind: '',
  tag: '',
  timing_flag: '',
  work_arrangement: '',
  contract_type: '',
  seniority: '',
  function_family: '',
  country: '',
  min_score: 0,
  exclude_not_interested: false,
}

function human(value) {
  if (!value) return ''
  return String(value).replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
}

/** FR-383: the meter counts, whether or not the criteria list is expanded. */
function fitCounts(detail) {
  const c = detail?.counts || {}
  return { met: c.met || 0, partial: c.partial || 0, violated: c.violated || 0 }
}

/* --- Filters (FR-283) ------------------------------------------------------ */

function FacetSelect({ label, name, facet, value, onChange }) {
  const options = facet || []
  if (!options.length) return null
  return (
    <Field label={label}>
      <select value={value} onChange={(e) => onChange(name, e.target.value)}>
        <option value="">Any {label.toLowerCase()}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {human(o.value)} ({o.count})
          </option>
        ))}
      </select>
    </Field>
  )
}

function FilterBar({ facets, filters, onChange, onReset }) {
  const active = Object.keys(EMPTY_FILTERS).some(
    (k) => String(filters[k] ?? '') !== String(EMPTY_FILTERS[k]),
  )
  return (
    <div className="card" style={{ marginBottom: 14 }}>
      {/* Primary filters. A grid, not a wrapping flex row: with a flex row the
          one field that carries helper text bottom-aligns against controls that
          do not, which drops every other label and input to a different line.
          Every control here is the same height and shares one baseline. */}
      <div className="filter-grid">
        <div className="filter-grid-wide">
          <Field label="Company or keyword">
            <input
              type="text"
              value={filters.q}
              placeholder="e.g. Colruyt, platform, Ghent"
              onChange={(e) => onChange('q', e.target.value)}
            />
          </Field>
        </div>

        <div className="field">
          <label>
            Kind
            <HelpTip term="speculative_opening" />
          </label>
          <select value={filters.kind} onChange={(e) => onChange('kind', e.target.value)}>
            <option value="">Vacancies and speculative openings</option>
            <option value="vacancy">Advertised vacancies only</option>
            <option value="speculative">Speculative openings only</option>
          </select>
        </div>

        <div className="field">
          <label>
            Tag
            <HelpTip term="stepping_stone" />
          </label>
          <select value={filters.tag} onChange={(e) => onChange('tag', e.target.value)}>
            <option value="">Any tag</option>
            {TAGS.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>
            Timing
            <HelpTip term="timing_window" />
          </label>
          <select
            value={filters.timing_flag}
            onChange={(e) => onChange('timing_flag', e.target.value)}
          >
            <option value="">Any timing</option>
            {(facets?.timing_flag || []).map((o) => (
              <option key={o.value} value={o.value}>
                {TIMING_LABELS[o.value] || human(o.value)} ({o.count})
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>Minimum score: {filters.min_score}</label>
          <input
            type="range"
            min="0"
            max="100"
            step="5"
            value={filters.min_score}
            onChange={(e) => onChange('min_score', Number(e.target.value))}
          />
        </div>
      </div>

      <p className="filter-note small muted">
        Keyword matches the title, the description and the company name. There is no
        upper score bound — sort descending to see the strongest matches first.
      </p>

      {/* Facet selects, in their own grid so they line up with each other. */}
      <div className="filter-grid" style={{ marginTop: 12 }}>
        <FacetSelect
          label="Work arrangement"
          name="work_arrangement"
          facet={facets?.work_arrangement}
          value={filters.work_arrangement}
          onChange={onChange}
        />
        <FacetSelect
          label="Contract"
          name="contract_type"
          facet={facets?.contract_type}
          value={filters.contract_type}
          onChange={onChange}
        />
        <FacetSelect
          label="Seniority"
          name="seniority"
          facet={facets?.seniority}
          value={filters.seniority}
          onChange={onChange}
        />
        <FacetSelect
          label="Function"
          name="function_family"
          facet={facets?.function_family}
          value={filters.function_family}
          onChange={onChange}
        />
        <FacetSelect
          label="Country"
          name="country"
          facet={facets?.country}
          value={filters.country}
          onChange={onChange}
        />
      </div>

      <div className="row row-wrap" style={{ marginTop: 12, gap: 14, alignItems: 'center' }}>
        <label className="checkline">
          <input
            type="checkbox"
            checked={filters.exclude_not_interested}
            onChange={(e) => onChange('exclude_not_interested', e.target.checked)}
          />
          Hide the ones I rejected
        </label>
        <div className="spacer" />
        {active && (
          <button className="btn btn-sm btn-ghost" onClick={onReset}>
            Clear filters
          </button>
        )}
      </div>
    </div>
  )
}

/* --- One row (FR-263, FR-282, FR-284, FR-383) ------------------------------ */

function OpportunityRow({
  item,
  isNew,
  lastVisit,
  expanded,
  onToggleExpand,
  onPatch,
  onReject,
  onDelete,
  draggable,
  dragOver,
  onDragStart,
  onDragOver,
  onDragLeave,
  onDrop,
}) {
  const fit = fitCounts(item.dream_fit_detail)
  const rejected = item.user_status === 'not_interested'
  const tags = item.tags || []

  const subscores = {
    score_profile_fit: item.score_profile_fit,
    score_dream_fit: item.score_dream_fit,
    score_directive_fit: item.score_directive_fit,
    score_company: item.score_company,
    score_compensation: item.score_compensation,
    score_plausibility: item.score_plausibility,
    score_reachability: item.score_reachability,
  }

  function toggleTag(tag) {
    const next = tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag]
    onPatch(item.id, { tags: next })
  }

  return (
    <>
      <div
        className={
          'opp-row' +
          (item.is_speculative ? ' speculative' : '') +
          (item.pinned ? ' pinned' : '') +
          (dragOver ? ' drag-over' : '')
        }
        style={rejected ? { opacity: 0.55 } : undefined}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        <div className="col" style={{ gap: 4, alignItems: 'center' }}>
          {/* FR-284: check/uncheck is the seeker's shortlist, stored server-side. */}
          <input
            type="checkbox"
            checked={Boolean(item.selected)}
            aria-label={`Select ${item.title}`}
            onChange={(e) => onPatch(item.id, { selected: e.target.checked })}
          />
          {draggable && (
            <span
              className="drag-handle"
              draggable
              title="Drag to place this row by hand. Your position sticks."
              aria-label="Reorder by hand"
              onDragStart={onDragStart}
            >
              ⠿
            </span>
          )}
        </div>

        <div style={{ minWidth: 0 }}>
          <div className="row row-wrap" style={{ gap: 7 }}>
            <Link
              className="opp-title"
              style={{ color: 'inherit' }}
              to={`/opportunities/${item.id}`}
            >
              {item.title}
            </Link>
            {/* FR-263: the distinction is a component, never an ad-hoc label. */}
            <KindBadge kind={item.kind} />
            {isNew && (
              <span
                className="opp-new"
                title={
                  lastVisit
                    ? `Found since your last visit (${formatDate(lastVisit)})`
                    : 'Found while this page was open'
                }
              >
                New
              </span>
            )}
            {/* A published posting that names no role. Real, but not a specific
                job, so it is called out rather than left to look like one. */}
            {item.open_application && (
              <Badge tone="info">
                <HelpTip title="Open application">
                  The employer published this on its own careers page, so it is a real
                  posting — but it names no particular role. It invites you to write
                  without applying for a specific job, so treat it as a way in rather
                  than a role to be scored against.
                </HelpTip>
                Open application
              </Badge>
            )}
            {item.timing_flag && (
              <Badge tone="warn">{TIMING_LABELS[item.timing_flag] || human(item.timing_flag)}</Badge>
            )}
            {item.manual_rank != null && (
              <Badge tone="accent">Your position #{item.manual_rank}</Badge>
            )}
            {item.pinned && <Badge tone="accent">Pinned</Badge>}
            {rejected && <Badge tone="danger">Not interested</Badge>}
            {item.user_status === 'applied' && <Badge tone="ok">Applied</Badge>}
          </div>

          <div className="opp-company">
            {/*
              FR-143/FR-263: who is actually hiring, on the line that names the
              company.  An agency row says so where the employer's name would
              have been, and a row nobody has researched says *that* rather than
              reading as a direct employer.
            */}
            <EmployerCompanyLine
              tag={item.employer}
              companyId={item.company_id}
              companyName={item.company_name || 'Company not identified'}
            />
            {item.location ? ` · ${item.location}` : ''}
            {item.work_arrangement ? ` · ${human(item.work_arrangement)}` : ''}
            {/* FR-142/FR-144: the two fields that make a row obviously in or
                out of scope.  Shown on the row so the list can be judged at a
                glance rather than only through the filter bar. */}
            {item.function_family ? ` · ${human(item.function_family)}` : ''}
            {item.country ? ` · ${item.country}` : ''}
            {item.comp_max != null
              ? ` · ${formatMoney(item.comp_min, item.comp_currency || 'EUR')}–${formatMoney(
                  item.comp_max,
                  item.comp_currency || 'EUR',
                )}`
              : ''}
          </div>

          {/* FR-282: the one paragraph that explains the number next to it. */}
          {item.rationale && <p className="opp-rationale">{item.rationale}</p>}

          <div className="row row-wrap" style={{ gap: 5, marginTop: 7 }}>
            {TAGS.map((t) => (
              <span
                key={t.value}
                className={`chip clickable${tags.includes(t.value) ? ' on' : ''}`}
                onClick={() => toggleTag(t.value)}
              >
                {t.label}
              </span>
            ))}
            {tags
              .filter((t) => !TAGS.some((k) => k.value === t))
              .map((t) => (
                <span key={t} className="chip">
                  {t}
                </span>
              ))}
          </div>
        </div>

        <div className="col" style={{ gap: 6 }}>
          <Meter value={item.score} />
          <div className="row small muted" style={{ gap: 6 }}>
            <span>Dream fit</span>
            <HelpTip term="dream_job_fit" />
          </div>
          {/* FR-383: a second meter, deliberately not folded into the first. */}
          <Meter value={item.dream_fit_detail?.score ?? item.score_dream_fit} />
          {item.dream_fit_detail && (
            <div className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
              <Badge tone="ok">{fit.met} met</Badge>
              <Badge tone="warn">{fit.partial} partial</Badge>
              <Badge tone={fit.violated ? 'danger' : undefined}>{fit.violated} violated</Badge>
            </div>
          )}
          <button className="btn btn-sm btn-ghost" onClick={() => onToggleExpand(item.id)}>
            {expanded ? 'Hide the breakdown' : 'Why this rank?'}
          </button>
        </div>

        <div className="col" style={{ gap: 5 }}>
          <button
            className="btn btn-sm"
            onClick={() => onPatch(item.id, { pinned: !item.pinned })}
            title="Pinned rows sit above the computed order"
          >
            {item.pinned ? 'Unpin' : 'Pin'}
          </button>
          {rejected ? (
            <button
              className="btn btn-sm"
              onClick={() => onPatch(item.id, { user_status: 'interested' })}
            >
              Reconsider
            </button>
          ) : (
            <button className="btn btn-sm btn-danger" onClick={() => onReject(item)}>
              Not interested
            </button>
          )}
          <Link className="btn btn-sm btn-ghost" to={`/opportunities/${item.id}`}>
            Open
          </Link>
          <button
            className="btn btn-sm btn-ghost"
            title="Delete this opportunity"
            onClick={() => onDelete(item)}
          >
            <Icon name="trash" /> Delete
          </button>
        </div>
      </div>

      {expanded && (
        <div
          style={{
            padding: '14px 18px 18px 58px',
            borderBottom: '1px solid var(--line)',
            background: 'var(--surface-2)',
          }}
        >
          <div className="grid grid-2">
            <div>
              <h4>
                Sub-scores
                <HelpTip term="reachability" />
              </h4>
              {/* FR-282: the seven components behind the single number. */}
              <SubScores scores={subscores} />
              {item.is_speculative && (
                <p className="small muted" style={{ marginTop: 10 }}>
                  {item.disclosure_note}
                </p>
              )}
            </div>

            <div>
              <h4>
                Dream-job fit
                <HelpTip term="dream_job_fit" />
              </h4>
              <FitCriteria detail={item.dream_fit_detail} />
            </div>
          </div>
        </div>
      )}
    </>
  )
}

/** FR-383: criteria met, partly met and violated - the meter as a list. */
function FitCriteria({ detail }) {
  if (!detail) {
    return <p className="small muted">Not scored yet, so there is no fit meter to show.</p>
  }
  const groups = [
    { key: 'violated', label: 'Violated', tone: 'danger' },
    { key: 'partially_met', label: 'Partly met', tone: 'warn' },
    { key: 'met', label: 'Met', tone: 'ok' },
    { key: 'unknown', label: 'Not recorded either way', tone: undefined },
  ]
  return (
    <div className="col" style={{ gap: 8 }}>
      {groups.map((g) => {
        const list = detail[g.key] || []
        if (!list.length) return null
        return (
          <div key={g.key}>
            <div className="row" style={{ gap: 6, marginBottom: 3 }}>
              <Badge tone={g.tone}>
                {g.label} · {list.length}
              </Badge>
            </div>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {list.slice(0, 6).map((c) => (
                <li key={c.id} className="small" style={{ color: 'var(--ink-2)', lineHeight: 1.5 }}>
                  {c.criterion}
                  {c.explanation && <span className="muted"> — {c.explanation}</span>}
                </li>
              ))}
              {list.length > 6 && (
                <li className="small muted">…and {list.length - 6} more, on the detail page.</li>
              )}
            </ul>
          </div>
        )
      })}
      {detail.note && (
        <p className="tiny muted" style={{ margin: 0 }}>
          {detail.note}
        </p>
      )}
    </div>
  )
}

/* --- Side-by-side comparison (FR-283) -------------------------------------- */

const COMPARE_LABELS = {
  title: 'Title',
  company_name: 'Company',
  kind: 'Kind',
  score: 'Overall score',
  score_profile_fit: 'Profile fit',
  score_dream_fit: 'Dream-job fit',
  score_directive_fit: 'Directive fit',
  score_company: 'Company',
  score_compensation: 'Compensation',
  score_plausibility: 'Plausibility',
  score_reachability: 'Reachability',
  seniority: 'Seniority',
  function_family: 'Function',
  location: 'Location',
  country: 'Country',
  work_arrangement: 'Work arrangement',
  remote_days: 'Remote days',
  contract_type: 'Contract',
  fte_percentage: 'FTE %',
  comp_min: 'Compensation, low',
  comp_max: 'Compensation, high',
  comp_currency: 'Currency',
  comp_confidence: 'Compensation confidence',
  comp_is_stated: 'Stated by the employer',
  employer_rating: 'Employer rating',
  plausibility: 'Plausibility (0–1)',
  posted_at: 'Posted',
  timing_flag: 'Timing',
  user_status: 'My status',
}

function cell(field, value) {
  if (value == null || value === '') return '–'
  if (field === 'posted_at') return formatDate(value)
  if (field === 'comp_is_stated') return value ? 'Yes' : 'Estimated'
  if (field === 'timing_flag') return TIMING_LABELS[value] || human(value)
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'number') return Number.isInteger(value) ? value : value.toFixed(2)
  const text = String(value)
  return text.includes('_') ? human(text) : text
}

function CompareModal({ result, onClose }) {
  const items = result.items || []
  return (
    <Modal title={`Comparing ${items.length} opportunities`} onClose={onClose} wide>
      <p className="section-intro">{result.advisory}</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th />
              {items.map((it) => (
                <th key={it.id}>
                  <div className="row" style={{ gap: 6 }}>
                    <Link to={`/opportunities/${it.id}`}>{it.title}</Link>
                    <KindBadge kind={it.kind} />
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(result.fields || [])
              .filter((f) => f !== 'title' && f !== 'kind')
              .map((f) => (
                <tr key={f}>
                  <th style={{ position: 'sticky', left: 0 }}>{COMPARE_LABELS[f] || human(f)}</th>
                  {(result.matrix?.[f] || []).map((v, i) => (
                    <td key={i}>{cell(f, v)}</td>
                  ))}
                </tr>
              ))}
            <tr>
              <th style={{ position: 'sticky', left: 0 }}>
                Dream-job criteria
                <HelpTip term="dream_job_fit" />
              </th>
              {(result.dream_fit_detail || []).map((d, i) => {
                const c = fitCounts(d)
                return (
                  <td key={i}>
                    <div className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
                      <Badge tone="ok">{c.met} met</Badge>
                      <Badge tone="warn">{c.partial} partial</Badge>
                      <Badge tone={c.violated ? 'danger' : undefined}>{c.violated} violated</Badge>
                    </div>
                  </td>
                )
              })}
            </tr>
          </tbody>
        </table>
      </div>
    </Modal>
  )
}

/* --- The screen ------------------------------------------------------------ */

export default function OpportunitiesPage() {
  // The workflow map links straight to the unadvertised roles (?kind=speculative),
  // so the screen opens on the filter it was asked for rather than on everything.
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { session } = useSession()

  // The previous visit is read once, before this visit overwrites it, so the
  // markers stay put for as long as the page is open.
  const [lastVisit] = useState(() => readLastVisit(session?.id))
  useEffect(() => {
    writeLastVisit(session?.id, new Date().toISOString())
  }, [session?.id])

  // New vacancies announced by the live stream while the page is open.
  const [liveNew, setLiveNew] = useState(0)
  const [liveIds, setLiveIds] = useState(() => new Set())
  const liveSeenRef = useRef(new Set())
  const liveTimerRef = useRef(null)
  const topRef = useRef(null)

  const [campaignId, setCampaignId] = useState('')
  const [filters, setFilters] = useState(() => {
    const kind = searchParams.get('kind')
    return kind === 'speculative' || kind === 'vacancy'
      ? { ...EMPTY_FILTERS, kind }
      : EMPTY_FILTERS
  })
  const [debouncedQ, setDebouncedQ] = useState('')
  const [sort, setSort] = useState('score')
  const [manualOrder, setManualOrder] = useState(true)
  const [offset, setOffset] = useState(0)

  const [expanded, setExpanded] = useState(null)
  const [rejecting, setRejecting] = useState(null)
  const [reason, setReason] = useState('')
  const [generating, setGenerating] = useState(false)
  const [comparison, setComparison] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [finding, setFinding] = useState(null)
  const [busy, setBusy] = useState(false)
  const [recalculating, setRecalculating] = useState(null)
  const [selectionNonce, setSelectionNonce] = useState(0)
  // FR-142/FR-144 deletion: one row at a time, or the whole selection. A row
  // touching a user decision is refused once (409) and needs a second confirm.
  const [deleting, setDeleting] = useState(null)
  const [deleteForce, setDeleteForce] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [bulkDelete, setBulkDelete] = useState(false)
  const [bulkRefused, setBulkRefused] = useState(null)

  // Which campaign the screen auto-selected, and whether the seeker has since
  // chosen one themselves. The empty-campaign fallback (below) may only move
  // the selector while the choice is still the screen's own.
  const autoCampaignRef = useRef(null)
  const userPickedCampaignRef = useRef(false)

  const [dragId, setDragId] = useState(null)
  const [overId, setOverId] = useState(null)

  // Which campaign the first-run screen should build from. The list filter
  // defaults to "every campaign", and synthesis is per-campaign, so without
  // this the one button that unblocks an empty screen had nothing to aim at.
  const [buildFrom, setBuildFrom] = useState('')

  // Poll the run while it is in flight, then refresh what is on screen. The
  // chain takes minutes and reports its stage, so the bar advances rather than
  // sitting at "working".
  useEffect(() => {
    if (!finding || ['done', 'failed', 'cancelled'].includes(finding.status)) return
    const t = setInterval(async () => {
      try {
        const res = await api.get('/autopilot/status')
        const run = res?.run
        if (!run) return
        setFinding(run)
        if (['done', 'failed', 'cancelled'].includes(run.status)) {
          list.reload()
          campaigns.reload()
          if (run.status === 'done') {
            setNotice(
              run.report?.counts?.opportunities === 0 || run.report?.reason === 'no_records'
                ? 'The search finished, but found nothing to rank. The panel above explains why.'
                : 'The search finished. The campaign selector above lists the new run.',
            )
          }
        }
      } catch {
        /* a poll that fails is retried on the next tick */
      }
    }, 3000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finding?.status, finding?.job_id])

  // A run outlives the page, so ask about it on mount too. Without this a
  // reload hid an in-flight search - and the explanation of one that found
  // nothing - behind a button that had already been pressed.
  useEffect(() => {
    let live = true
    ;(async () => {
      try {
        const res = await api.get('/autopilot/status')
        const run = res?.run
        if (!live || !run) return
        if (!['done', 'failed', 'cancelled'].includes(run.status)) {
          setFinding(run)
          return
        }
        // A finished run is only worth surfacing while it is recent: a failed
        // run, and a completed one that found nothing, both explain an empty
        // list. A completed run with results would just repeat the list.
        const finishedAt = Date.parse(run.finished_at)
        const recent =
          Number.isFinite(finishedAt) && Date.now() - finishedAt < 24 * 60 * 60 * 1000
        const empty =
          run.report?.reason === 'no_records' || run.report?.counts?.opportunities === 0
        if (recent && (run.status === 'failed' || empty)) setFinding(run)
      } catch {
        /* the status route is orientation; the button still starts a run */
      }
    })()
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Typing in the search box should not fire a request per keystroke.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(filters.q), 350)
    return () => clearTimeout(t)
  }, [filters.q])

  const campaigns = useFetch(() => api.get('/campaigns').catch(() => []))
  // The strip is orientation, not data the screen depends on: never block on it.
  const journey = useFetch(() => api.get('/overview/journey').catch(() => null))

  // Open on the most recent campaign that finished, not on "every campaign".
  // The union mixes separate searches - and the separate directives behind
  // them - into one list, which is exactly the state in which an out-of-scope
  // dump hides: it makes 43,000 rows look like the answer. The selector still
  // offers the union for anyone who wants it.
  useEffect(() => {
    if (userPickedCampaignRef.current || campaignId) return
    const list = Array.isArray(campaigns.data) ? campaigns.data : []
    if (!list.length) return
    const finished = list.filter((c) => c.status === 'completed')
    const countsKnown = finished.some((c) => campaignOpportunityCount(c) != null)
    // When the rows carry a count, never open on a campaign whose search found
    // nothing: take the newest completed one with results, and open on the
    // union rather than on an empty list when none has any. Without counts the
    // choice is provisional - the summary fetch below turns it away from an
    // empty campaign once that is known.
    const pick = countsKnown
      ? finished.find((c) => (campaignOpportunityCount(c) ?? 0) > 0) || null
      : finished[0] || list[0] || null
    const pickId = pick?.id || ''
    autoCampaignRef.current = pickId
    setCampaignId(pickId)
    setOffset(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaigns.data])

  const facets = useFetch(
    () => api.get(`/opportunities/facets${campaignId ? `?campaign_id=${campaignId}` : ''}`),
    [campaignId],
  )

  const summary = useFetch(
    () => (campaignId ? api.get(`/opportunities/summary?campaign_id=${campaignId}`) : null),
    [campaignId, selectionNonce],
  )

  // The auto-selected campaign turned out to have no opportunities. The list
  // does not always carry a count, so this is only knowable once the summary
  // arrives; move to the newest completed campaign that has results, or to the
  // union, instead of leaving the seeker on an empty list. A campaign the
  // seeker chose themselves is always left alone.
  useEffect(() => {
    if (userPickedCampaignRef.current) return
    if (!campaignId || campaignId !== autoCampaignRef.current) return
    const total = summary.data?.total
    if (typeof total !== 'number' || total > 0) return
    const list = Array.isArray(campaigns.data) ? campaigns.data : []
    // A running campaign legitimately has no opportunities yet; only a
    // finished one with nothing to show strands the seeker.
    const picked = list.find((c) => c.id === campaignId)
    if (picked?.status !== 'completed') return
    const next = list.find(
      (c) =>
        c.id !== campaignId && c.status === 'completed' && (campaignOpportunityCount(c) ?? 0) > 0,
    )
    autoCampaignRef.current = next?.id || ''
    setCampaignId(next?.id || '')
    setOffset(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summary.data, campaignId, campaigns.data])

  const query = useMemo(() => {
    const p = new URLSearchParams()
    if (campaignId) p.set('campaign_id', campaignId)
    if (debouncedQ.trim()) p.set('q', debouncedQ.trim())
    for (const k of [
      'kind',
      'tag',
      'timing_flag',
      'work_arrangement',
      'contract_type',
      'seniority',
      'function_family',
      'country',
    ]) {
      if (filters[k]) p.set(k, filters[k])
    }
    if (filters.min_score > 0) p.set('min_score', String(filters.min_score))
    if (filters.exclude_not_interested) p.set('exclude_not_interested', 'true')
    p.set('sort', sort)
    p.set('respect_manual_order', manualOrder ? 'true' : 'false')
    p.set('limit', String(PAGE_SIZE))
    p.set('offset', String(offset))
    return p.toString()
  }, [campaignId, debouncedQ, filters, sort, manualOrder, offset])

  const list = useFetch(() => api.get(`/opportunities?${query}`), [query])

  // The shortlist lives on the server, so "the selected" is a query, not local
  // state - it stays correct across pages and filters (FR-284, FR-321).
  const selection = useFetch(
    () =>
      api.get(
        `/opportunities?selected=true&limit=200${campaignId ? `&campaign_id=${campaignId}` : ''}`,
      ),
    [campaignId, selectionNonce],
  )

  // Refresh the rows in place when a new vacancy joins the ranked list. The
  // fetch goes around `list.reload()` on purpose: a reload swaps the list for
  // a skeleton, which would close an expanded row under the seeker's cursor.
  // A drag in progress is never refreshed under; the refresh waits for it.
  const queryRef = useRef(query)
  queryRef.current = query
  const dragRef = useRef(null)
  dragRef.current = dragId

  function refreshLive() {
    clearTimeout(liveTimerRef.current)
    liveTimerRef.current = setTimeout(async () => {
      if (dragRef.current) return refreshLive()
      const asked = queryRef.current
      try {
        const next = await api.get(`/opportunities?${asked}`)
        if (asked === queryRef.current) list.setData(next)
      } catch {
        /* the next arrival, or any reload, tries again */
      }
    }, LIVE_REFRESH_MS)
  }

  useEffect(() => () => clearTimeout(liveTimerRef.current), [])

  useLiveEvent('notification', (n) => {
    if (!n || n.kind !== 'new_vacancy' || n.id == null) return
    if (liveSeenRef.current.has(n.id)) return
    liveSeenRef.current.add(n.id)
    setLiveNew((c) => c + 1)
    const oppId = n.payload?.opportunity_id
    if (oppId) setLiveIds((ids) => new Set(ids).add(oppId))
    refreshLive()
  })

  function showLiveNew() {
    setLiveNew(0)
    topRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const items = list.data?.items ?? []
  const total = list.data?.total ?? 0
  const selected = selection.data?.items ?? []
  const filtersActive = Object.keys(EMPTY_FILTERS).some(
    (k) => String(filters[k] ?? '') !== String(EMPTY_FILTERS[k]),
  )

  // A finished run that found nothing carries the explanation the empty list
  // needs. Older runs have no report, so only an explicit zero or the
  // backend's "no_records" marker triggers it.
  const zeroResults =
    finding?.status === 'done' &&
    (finding.report?.reason === 'no_records' || finding.report?.counts?.opportunities === 0)
  const emptySearch = zeroResults ? emptySearchSummary(finding.report) : null

  // FR-284: set_manual_order() writes positions 1..n for the ids it is given,
  // so a drag on page two would renumber over page one. Reordering is offered
  // on the first page of one campaign, where the numbers mean what they say.
  const canReorder = Boolean(campaignId) && manualOrder && offset === 0 && items.length > 1

  function change(name, value) {
    setOffset(0)
    setFilters((f) => ({ ...f, [name]: value }))
  }

  async function patch(id, body) {
    setActionError(null)
    try {
      const updated = await api.patch(`/opportunities/${id}`, body)
      list.setData((d) =>
        d ? { ...d, items: d.items.map((i) => (i.id === id ? { ...i, ...updated } : i)) } : d,
      )
      if ('selected' in body || 'user_status' in body || 'pinned' in body) {
        setSelectionNonce((n) => n + 1)
      }
      return true
    } catch (e) {
      setActionError(e)
      return false
    }
  }

  async function reject() {
    if (!reason.trim()) return
    // FR-285 learns from the reason, so the API refuses an unexplained
    // rejection - the reason travels in the same request as the status.  Only
    // close and clear the dialog once it actually saved, or the user believes
    // a rejection was recorded that never left the browser.
    const saved = await patch(rejecting.id, {
      user_status: 'not_interested',
      not_interested_reason: reason.trim(),
    })
    if (!saved) return
    setRejecting(null)
    setReason('')
  }

  /** Everything a deletion changes: the page, the counts and the shortlist. */
  function refreshAfterDelete() {
    list.reload()
    summary.reload()
    facets.reload()
    setSelectionNonce((n) => n + 1)
  }

  /**
   * FR-142 / NFR-305: delete one row. The first call leaving a user decision
   * intact is refused with a 409; that answer is what opens the second confirm
   * ("delete anyway?"), and only the retry carries force=true.
   */
  async function deleteOpportunity(force = false) {
    if (!deleting) return
    setDeleteBusy(true)
    setActionError(null)
    try {
      const res = await api.del(`/opportunities/${deleting.id}${force ? '?force=true' : ''}`)
      setNotice(
        res?.deleted
          ? `Deleted “${deleting.title}”.`
          : `“${deleting.title}” was not deleted.`,
      )
      setDeleting(null)
      setDeleteForce(false)
      refreshAfterDelete()
    } catch (e) {
      if (e.status === 409 && !force) {
        setDeleteForce(true)
      } else {
        setActionError(e)
        setDeleting(null)
        setDeleteForce(false)
      }
    } finally {
      setDeleteBusy(false)
    }
  }

  /**
   * FR-144: the same rules over the shortlist. The bulk route answers with its
   * own `refused` list instead of a 409, so the reasons are shown and force is
   * offered as a second, explicit confirmation rather than guessed at.
   */
  async function deleteSelected(force = false) {
    const ids = selected.map((s) => s.id)
    if (!ids.length) return
    const titles = new Map(selected.map((s) => [s.id, s.title]))
    setDeleteBusy(true)
    setActionError(null)
    try {
      const res = await api.post('/opportunities/delete', {
        opportunity_ids: ids,
        force,
      })
      const refused = res?.refused || []
      const deleted = res?.deleted ?? 0
      refreshAfterDelete()
      if (refused.length && !force) {
        setBulkRefused(
          refused.map((r) => ({
            id: r.id,
            reason: r.reason,
            title: titles.get(r.id) || r.id,
          })),
        )
      } else {
        setBulkDelete(false)
        setBulkRefused(null)
        setNotice(
          `${deleted} ${deleted === 1 ? 'opportunity' : 'opportunities'} deleted` +
            (refused.length ? `, ${refused.length} kept.` : '.'),
        )
      }
    } catch (e) {
      setActionError(e)
      setBulkDelete(false)
      setBulkRefused(null)
    } finally {
      setDeleteBusy(false)
    }
  }

  async function dropOn(targetId) {
    setOverId(null)
    if (!dragId || dragId === targetId || !canReorder) return
    const ids = items.map((i) => i.id)
    const from = ids.indexOf(dragId)
    const to = ids.indexOf(targetId)
    setDragId(null)
    if (from < 0 || to < 0) return
    ids.splice(to, 0, ids.splice(from, 1)[0])

    const byId = new Map(items.map((i) => [i.id, i]))
    list.setData((d) =>
      d ? { ...d, items: ids.map((id, i) => ({ ...byId.get(id), manual_rank: i + 1 })) } : d,
    )
    try {
      const res = await api.post('/opportunities/reorder', {
        campaign_id: campaignId,
        ordered_ids: ids,
      })
      setNotice(res?.note || 'Your order is saved.')
    } catch (e) {
      setActionError(e)
      list.reload()
    }
  }

  async function clearOrder() {
    setBusy(true)
    setActionError(null)
    try {
      await api.post('/opportunities/reorder/clear', { campaign_id: campaignId })
      setNotice('Your manual positions are cleared; the computed ranking applies again.')
      list.reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  async function compare() {
    setActionError(null)
    try {
      const res = await api.post('/opportunities/compare', {
        opportunity_ids: selected.slice(0, 6).map((s) => s.id),
      })
      setComparison(res)
    } catch (e) {
      setActionError(e)
    }
  }

  async function generate() {
    setBusy(true)
    setActionError(null)
    try {
      const res = await api.post('/applications/generate', {
        opportunity_ids: selected.map((s) => s.id),
        campaign_id: campaignId || undefined,
      })
      setGenerating(false)
      // The batch runs in the background, so the work has to be visible where
      // it lands: hand the batch size to the Applications screen, which shows
      // the running progress banner while the packages are written.
      navigate('/applications', {
        state: { startedGeneration: res?.count ?? selected.length },
      })
    } catch (e) {
      setGenerating(false)
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  async function findMore() {
    setBusy(true)
    setActionError(null)
    try {
      const res = await api.post('/autopilot/start', {})
      // The start route reports a run, not a stage: a fresh run has not
      // checkpointed yet, and an existing one is somewhere else entirely. So
      // never invent a stage_index here - leave it unset until the status
      // route says where the run actually is.
      const startState = {
        status: res?.status || 'running',
        stage: res?.stage ?? null,
        stage_index: res?.stage_index ?? null,
        total_steps: res?.total_steps || 8,
        job_id: res?.job_id,
      }
      if (res?.already_running) {
        // The start route only says a run exists; the status route carries
        // its real stage. Without this the bar restarted at 0/8 for a run
        // that was already at the collection stage.
        try {
          const status = await api.get('/autopilot/status')
          const run = status?.run
          if (run && (!startState.job_id || run.job_id === startState.job_id)) {
            setFinding(run)
            return
          }
        } catch {
          /* fall back to what the start route did tell us; the poll fills in */
        }
      }
      setFinding(startState)
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  async function cancelFind() {
    if (!finding?.job_id) return
    try {
      await api.post(`/autopilot/${finding.job_id}/cancel`, {})
    } catch (e) {
      setActionError(e)
    }
  }

  async function recalculate() {
    setBusy(true)
    setActionError(null)
    try {
      const res = await api.post('/opportunities/recalculate', {
        campaign_id: campaignId,
        background: true,
      })
      setRecalculating({ job_id: res?.job_id, since: summary.data?.last_scored_at ?? null })
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  async function synthesise(target) {
    const id = target || campaignId || buildFrom
    if (!id) return
    setBusy(true)
    setActionError(null)
    try {
      await api.post('/opportunities/synthesise', { campaign_id: id, background: true })
      // Point the list at what is being built, so the result lands on screen
      // instead of behind the campaign filter the seeker never changed.
      if (id !== campaignId) {
        // An explicit aim at one campaign: the empty-campaign fallback must
        // not move it while the synthesis pass is still filling it.
        userPickedCampaignRef.current = true
        setCampaignId(id)
        setOffset(0)
      }
      setNotice('Building the list from what this campaign collected. Reload in a moment.')
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  /**
   * FR-262: the spontaneous-application track. For every interesting company
   * with no matching vacancy, propose the roles it is likely to need. Nothing
   * called this before, so the whole track existed only in the API - which is
   * why a `spontaneous_only` campaign (FR-149) produced an empty list.
   */
  async function findUnadvertised() {
    setBusy(true)
    setActionError(null)
    try {
      const res = await api.post('/opportunities/speculative', {
        campaign_id: campaignId,
        background: true,
      })
      if (res?.error === 'consent_required') {
        setActionError(
          new Error(
            'Generating unadvertised roles sends profile data to the model provider, and that ' +
              'consent has not been recorded yet (CR-410). Record it on the Composite profile ' +
              'screen, then try again.',
          ),
        )
        return
      }
      setNotice(
        'Looking for roles these companies have not advertised. They appear here marked ' +
          '“Speculative opening”. Reload in a moment.',
      )
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  // The scoring pass runs as a background job. There is no job-status route to
  // poll, so the screen watches the one thing that does change - the campaign's
  // last_scored_at - and refreshes the list when it moves (NFR-502, best effort).
  useEffect(() => {
    if (!recalculating || !campaignId) return
    let tries = 0
    const id = setInterval(async () => {
      tries += 1
      try {
        const s = await api.get(`/opportunities/summary?campaign_id=${campaignId}`)
        if (s?.last_scored_at && s.last_scored_at !== recalculating.since) {
          setRecalculating(null)
          setNotice('Scores refreshed. Your manual order, pins and selections are untouched.')
          list.reload()
          summary.reload()
          return
        }
      } catch {
        /* a failed poll is not a failed recalculation; keep watching */
      }
      if (tries >= 40) setRecalculating(null)
    }, 4000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recalculating, campaignId])

  return (
    <div className="content-wide" ref={topRef}>
      {liveNew > 0 && (
        <button type="button" className="opp-live-new" onClick={showLiveNew}>
          <Icon name="refresh" />
          {liveNew === 1
            ? '1 new opportunity since you opened this page'
            : `${liveNew} new opportunities since you opened this page`}
        </button>
      )}
      <WorkflowMap journey={journey.data?.journey || {}} compact current="opportunities" />
      <ScreenIntro pathname="/opportunities" />

      {/* NFR-305 / CR-405: the score orders a list; it decides nothing. */}
      <Caution title="Scores are advisory">
        {list.data?.advisory ||
          'Scores are advisory. They order a list; they do not decide anything. Every decision to keep, reject or apply is yours.'}
      </Caution>

      {actionError && <ErrorBox error={actionError} />}
      {notice && (
        <div className="alert alert-ok">
          <div style={{ flex: 1 }}>{notice}</div>
          <button className="btn btn-sm btn-ghost" onClick={() => setNotice(null)}>
            ✕
          </button>
        </div>
      )}
      {recalculating && (
        <div className="alert alert-info">
          <span className="spinner" />
          <div style={{ flex: 1 }}>
            <strong>Recalculating the scores.</strong> Your manual order, pins, tags and
            selections are not touched by a recalculation (FR-284). The list refreshes itself when
            the pass finishes.
            <div style={{ marginTop: 8 }}>
              <button className="btn btn-sm" onClick={() => setRecalculating(null)}>
                Stop watching
              </button>
              <span className="tiny muted" style={{ marginLeft: 8 }}>
                Stopping the watch does not stop the pass — there is no cancel route for it.
              </span>
            </div>
          </div>
        </div>
      )}

      <div className="card" style={{ marginBottom: 14 }}>
        {/* A grid, not a wrapping flex row: the campaign field carries helper
            text and the others do not, so bottom-aligning them dropped every
            other label and control onto a different line. */}
        <div className="filter-grid">
          <div>
            <Field label="Campaign">
              <select
                value={campaignId}
                onChange={(e) => {
                  userPickedCampaignRef.current = true
                  setCampaignId(e.target.value)
                  setOffset(0)
                }}
              >
                <option value="">Every campaign</option>
                {(campaigns.data || []).map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} · {c.status}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <div>
            <Field label="Sort">
              <select
                value={sort}
                onChange={(e) => {
                  setSort(e.target.value)
                  setOffset(0)
                }}
              >
                {SORT_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <div className="field">
            <label>&nbsp;</label>
            <label className="checkline" style={{ minHeight: 34 }}>
              <input
                type="checkbox"
                checked={manualOrder}
                onChange={(e) => {
                  setManualOrder(e.target.checked)
                  setOffset(0)
                }}
              />
              My order first
              <HelpTip term="manual_order" />
            </label>
          </div>

          <div className="field">
            <label>&nbsp;</label>
            <button
              className="btn btn-primary"
              style={{ minHeight: 34 }}
              disabled={busy || Boolean(finding)}
              onClick={findMore}
            >
              {finding ? <span className="spinner" /> : <Icon name="search" />}
              Find more opportunities
              <HelpTip title="What this does">
                One click: reads your profile and dream job, decides what to search for and
                which sources to use, collects, then ranks what came back and looks into the
                companies. It is the campaign screen, without the campaign screen.
              </HelpTip>
            </button>
          </div>

          {campaignId && (
            <div className="field">
              <label>&nbsp;</label>
              <div className="row" style={{ minHeight: 34, gap: 8 }}>
                <button
                  className="btn btn-sm"
                  disabled={busy || Boolean(recalculating) || Boolean(finding)}
                  onClick={recalculate}
                >
                  Recalculate scores
                </button>
                <button
                  className="btn btn-sm"
                  disabled={busy || Boolean(finding)}
                  onClick={clearOrder}
                >
                  Clear my order
                </button>
              </div>
            </div>
          )}
        </div>

        <p className="filter-note small muted">
          Pinning, rejecting and tagging work everywhere; hand-ordering needs one campaign.
        </p>

        {/* One progress bar for the whole chain, because "find more" is one
            action: if it reported per stage there would be nothing to say. */}
        {finding && (
          <div className="card" style={{ marginTop: 12, marginBottom: 0 }}>
            <div className="row" style={{ marginBottom: 8 }}>
              {['done', 'failed', 'cancelled'].includes(finding.status) ? (
                <Icon
                  name={
                    finding.status === 'done'
                      ? 'success'
                      : finding.status === 'cancelled'
                        ? 'stop'
                        : 'error'
                  }
                />
              ) : (
                <span className="spinner" />
              )}
              <strong>{AUTOPILOT_STAGE[finding.stage] || 'Finding opportunities'}</strong>
              <div className="spacer" />
              <span className="small muted">
                {finding.stage_index != null
                  ? `step ${Math.min(finding.stage_index + 1, finding.total_steps || 8)} of ${
                      finding.total_steps || 8
                    }`
                  : `${finding.total_steps || 8} steps`}
              </span>
              {!['done', 'failed', 'cancelled'].includes(finding.status) && (
                <button className="btn btn-sm btn-danger" onClick={cancelFind}>
                  Stop
                </button>
              )}
              {['done', 'failed', 'cancelled'].includes(finding.status) && (
                <button className="btn btn-sm" onClick={() => setFinding(null)}>
                  Dismiss
                </button>
              )}
            </div>
            <div className="progress-track">
              <div
                className="progress-fill"
                style={{
                  width: `${
                    finding.status === 'done'
                      ? 100
                      : Math.min(
                          100,
                          Math.round(
                            ((finding.stage_index || 0) / (finding.total_steps || 8)) * 100,
                          ),
                        )
                  }%`,
                }}
              />
            </div>
            {finding.status === 'done' &&
              (zeroResults ? (
                <div className="alert alert-warn" style={{ margin: '10px 0 0' }}>
                  <Icon name="warning" />
                  <div>
                    <p style={{ margin: 0 }}>
                      <strong>The search finished, but no opportunity passed your directives.</strong>{' '}
                      {describeEmptySearch(emptySearch)}
                    </p>
                    {emptySearch.errors.length > 0 && (
                      <ul className="small" style={{ margin: '6px 0 0 16px' }}>
                        {emptySearch.errors.map((message, i) => (
                          <li key={i}>{message}</li>
                        ))}
                      </ul>
                    )}
                    <p className="small muted" style={{ margin: '6px 0 0' }}>
                      Check the campaign&apos;s sources or widen your directives, then run the
                      search again. Earlier campaigns keep what they found; the selector above
                      stays off the empty list.
                    </p>
                  </div>
                </div>
              ) : (
                <p className="small muted" style={{ margin: '8px 0 0' }}>
                  {finding.report?.counts?.opportunities
                    ? `${finding.report.counts.opportunities} opportunities in the new search.`
                    : 'The search finished; the list below is refreshed.'}{' '}
                  Switch the campaign above to the new one to see it.
                </p>
              ))}
            {finding.status === 'failed' && (
              <p className="small" style={{ margin: '8px 0 0', color: 'var(--danger)' }}>
                {finding.last_error || 'The search did not finish.'}
              </p>
            )}
          </div>
        )}

        {summary.data && (
          <div className="grid grid-4" style={{ marginTop: 4 }}>
            {[
              { label: 'Opportunities', value: summary.data.total },
              { label: 'Advertised vacancies', value: summary.data.vacancies },
              { label: 'Speculative openings', value: summary.data.speculative },
              { label: 'On my shortlist', value: summary.data.selected },
            ].map((s) => (
              <div className="stat-tile" key={s.label}>
                <div className="stat-value">{s.value ?? 0}</div>
                <div className="stat-label">{s.label}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <FilterBar
        facets={facets.data}
        filters={filters}
        onChange={change}
        onReset={() => {
          setFilters(EMPTY_FILTERS)
          setOffset(0)
        }}
      />

      {/* FR-284: say plainly what hand-ordering does, where it is done. */}
      <div className="alert alert-info">
        <div style={{ flex: 1 }}>
          {canReorder ? (
            <>
              <strong>Drag the ⠿ handle to place a row by hand.</strong> A row you place keeps its
              position above everything the score decided, and a recalculation will not move it.
              “Clear my order” gives the computed ranking back.
            </>
          ) : (
            <>
              <strong>Hand-ordering needs one campaign and the first page.</strong> Positions are
              written as 1…n within a campaign, so pick a campaign above, keep “My order first” on,
              and stay on the first page. Pinning, tagging and rejecting work everywhere.
            </>
          )}
        </div>
      </div>

      <div className="card" style={{ padding: 0, marginBottom: 14 }}>
        <div className="card-header" style={{ margin: 0, borderRadius: 'var(--radius)' }}>
          <strong>{total}</strong>
          <span className="muted small">
            {total === 1 ? 'opportunity' : 'opportunities'}
            {filtersActive ? ' matching these filters' : ''}
          </span>
          <div className="spacer" />
          <span className="small muted">{selected.length} on the shortlist</span>
          {/* FR-262: the spontaneous track, reachable rather than API-only. */}
          <button
            className="btn btn-sm"
            disabled={busy || !campaignId}
            title={
              campaignId
                ? 'Propose roles these companies have not advertised, from their finances, ' +
                  'hiring signals and department map'
                : 'Pick a campaign first'
            }
            onClick={findUnadvertised}
          >
            <Icon name="speculative" /> Find unadvertised roles
          </button>
          <button
            className="btn btn-sm"
            disabled={selected.length < 2 || selected.length > 6}
            title={
              selected.length > 6
                ? 'Comparison takes at most six; two or three read best.'
                : 'Compare the selected opportunities side by side'
            }
            onClick={compare}
          >
            Compare selected
          </button>
          <button
            className="btn btn-sm btn-primary"
            disabled={selected.length === 0}
            onClick={() => setGenerating(true)}
          >
            Generate applications for the selected
          </button>
          <button
            className="btn btn-sm btn-danger"
            disabled={selected.length === 0 || deleteBusy}
            title={
              selected.length
                ? 'Delete the selected opportunities from every list'
                : 'Tick the opportunities you want to delete first'
            }
            onClick={() => {
              setBulkRefused(null)
              setBulkDelete(true)
            }}
          >
            <Icon name="trash" /> Delete selected
          </button>
        </div>

        {list.loading && (
          <div style={{ padding: 18 }}>
            <Loading rows={6} />
          </div>
        )}

        {!list.loading && list.error && (
          <div style={{ padding: 18 }}>
            <ErrorBox error={list.error} onRetry={list.reload} />
          </div>
        )}

        {!list.loading && !list.error && items.length === 0 && filtersActive && (
          <Empty
            title="Nothing matches these filters"
            action={
              <button
                className="btn"
                onClick={() => {
                  setFilters(EMPTY_FILTERS)
                  setOffset(0)
                }}
              >
                Clear the filters
              </button>
            }
          >
            The filters are narrower than the result set. Widen the score floor or clear the kind
            and tag filters to see what the campaign actually found.
          </Empty>
        )}

        {/* With a zero-result run the panel above already explains the empty
            list; the generic "nothing has been ranked yet" would contradict
            it. Dismissing the panel brings the first-run guidance back. */}
        {!list.loading && !list.error && items.length === 0 && !filtersActive && !zeroResults && (
          <div style={{ padding: 18 }}>
            <FirstRun
              pathname="/opportunities"
              action={
                campaignId ? (
                  <button className="btn btn-primary" disabled={busy} onClick={() => synthesise()}>
                    Build the list from this campaign
                  </button>
                ) : (campaigns.data || []).length > 0 ? (
                  // No campaign is selected, which is the default. Rather than
                  // send the seeker off to another screen to find out that the
                  // button lives here, let them pick the campaign right here.
                  <div className="row row-wrap" style={{ gap: 10, alignItems: 'center' }}>
                    <select
                      value={buildFrom}
                      disabled={busy}
                      onChange={(e) => setBuildFrom(e.target.value)}
                    >
                      <option value="">Choose a campaign…</option>
                      {(campaigns.data || []).map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name} · {c.status}
                        </option>
                      ))}
                    </select>
                    <button
                      className="btn btn-primary"
                      disabled={busy || !buildFrom}
                      onClick={() => synthesise(buildFrom)}
                    >
                      Build the list from this campaign
                    </button>
                  </div>
                ) : (
                  <Link className="btn btn-primary" to="/campaigns">
                    Open campaigns
                  </Link>
                )
              }
            >
              Nothing has been ranked yet. Opportunities appear here once a campaign has collected
              vacancies and the synthesis pass has normalised them. Roles nobody advertised are a
              separate pass — “Find unadvertised roles” — and are marked as speculative everywhere.
            </FirstRun>
          </div>
        )}

        {!list.loading &&
          !list.error &&
          items.map((item) => (
            <OpportunityRow
              key={item.id}
              item={item}
              isNew={liveIds.has(item.id) || isNewSince(item, lastVisit)}
              lastVisit={lastVisit}
              expanded={expanded === item.id}
              onToggleExpand={(id) => setExpanded((e) => (e === id ? null : id))}
              onPatch={patch}
              onReject={(o) => {
                setRejecting(o)
                setReason('')
              }}
              onDelete={(o) => {
                setDeleting(o)
                setDeleteForce(false)
              }}
              draggable={canReorder}
              dragOver={overId === item.id}
              onDragStart={(e) => {
                setDragId(item.id)
                e.dataTransfer.effectAllowed = 'move'
                e.dataTransfer.setData('text/plain', item.id)
              }}
              onDragOver={(e) => {
                if (!canReorder || !dragId) return
                e.preventDefault()
                e.dataTransfer.dropEffect = 'move'
                if (overId !== item.id) setOverId(item.id)
              }}
              onDragLeave={() => setOverId((o) => (o === item.id ? null : o))}
              onDrop={(e) => {
                e.preventDefault()
                dropOn(item.id)
              }}
            />
          ))}

        {total > PAGE_SIZE && (
          <div className="row" style={{ padding: 12 }}>
            <button
              className="btn btn-sm"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </button>
            <span className="small muted">
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
            </span>
            <button
              className="btn btn-sm"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        )}
      </div>

      {rejecting && (
        <Modal
          title={`Not interested: ${rejecting.title}`}
          onClose={() => setRejecting(null)}
          actions={
            <>
              <button className="btn" onClick={() => setRejecting(null)}>
                Cancel
              </button>
              <button className="btn btn-danger" disabled={!reason.trim()} onClick={reject}>
                Mark not interested
              </button>
            </>
          }
        >
          <p className="section-intro">
            The reason is required, and it is not bookkeeping: FR-285 re-tunes your scoring weights
            from what you reject and why, so an unexplained rejection teaches the ranking nothing.
          </p>
          <Field label="Why not?" hint="A sentence is enough. Only you ever read it.">
            <textarea
              value={reason}
              autoFocus
              placeholder="Too junior; the commute is impossible; I do not want agency work."
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
        </Modal>
      )}

      {deleting && (
        <Modal
          title={deleteForce ? 'Delete anyway?' : `Delete “${deleting.title}”?`}
          onClose={() => {
            if (deleteBusy) return
            setDeleting(null)
            setDeleteForce(false)
          }}
          actions={
            <>
              <button
                className="btn"
                disabled={deleteBusy}
                onClick={() => {
                  setDeleting(null)
                  setDeleteForce(false)
                }}
              >
                Cancel
              </button>
              <button
                className="btn btn-danger"
                disabled={deleteBusy}
                onClick={() => deleteOpportunity(deleteForce)}
              >
                {deleteBusy ? (
                  <span className="spinner" />
                ) : deleteForce ? (
                  'Delete anyway'
                ) : (
                  'Delete'
                )}
              </button>
            </>
          }
        >
          {deleteForce ? (
            <p className="section-intro">
              This opportunity has decisions attached — a pin, a position, a rejection, an
              application or a package. Deleting it removes those with it, and this cannot
              be undone (NFR-305).
            </p>
          ) : (
            <p className="section-intro">
              Delete <strong>{deleting.title}</strong>
              {deleting.company_name ? ` at ${deleting.company_name}` : ''}? It disappears
              from every list, and this cannot be undone.
            </p>
          )}
        </Modal>
      )}

      {bulkDelete && (
        <Modal
          title={
            bulkRefused
              ? `${bulkRefused.length} of the selected were kept`
              : `Delete ${selected.length} selected ${
                  selected.length === 1 ? 'opportunity' : 'opportunities'
                }?`
          }
          onClose={() => {
            if (deleteBusy) return
            setBulkDelete(false)
            setBulkRefused(null)
          }}
          actions={
            bulkRefused ? (
              <>
                <button
                  className="btn"
                  disabled={deleteBusy}
                  onClick={() => {
                    setBulkDelete(false)
                    setBulkRefused(null)
                  }}
                >
                  Keep them
                </button>
                <button
                  className="btn btn-danger"
                  disabled={deleteBusy}
                  onClick={() => deleteSelected(true)}
                >
                  {deleteBusy ? <span className="spinner" /> : 'Delete anyway'}
                </button>
              </>
            ) : (
              <>
                <button className="btn" disabled={deleteBusy} onClick={() => setBulkDelete(false)}>
                  Cancel
                </button>
                <button
                  className="btn btn-danger"
                  disabled={deleteBusy}
                  onClick={() => deleteSelected(false)}
                >
                  {deleteBusy ? <span className="spinner" /> : 'Delete selected'}
                </button>
              </>
            )
          }
        >
          {bulkRefused ? (
            <>
              <p className="section-intro">
                These carry a decision of yours — a pin, a position, a rejection, an
                application or a package — so they were not deleted. Deleting them anyway
                removes the decision with them (NFR-305).
              </p>
              <ul className="help-tips">
                {bulkRefused.map((r) => (
                  <li key={r.id}>
                    <strong>{r.title}</strong>{' '}
                    <span className="muted small">
                      —{' '}
                      {r.reason === 'user_decision'
                        ? 'you have decided about this one'
                        : String(r.reason || 'not deleted').replace(/_/g, ' ')}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <>
              <p className="section-intro">
                This permanently removes the {selected.length} selected{' '}
                {selected.length === 1 ? 'opportunity' : 'opportunities'} from every list.
                Pins, positions and rejections on those rows are removed with them
                (NFR-305).
              </p>
              <ul className="help-tips">
                {selected.slice(0, 12).map((s) => (
                  <li key={s.id}>
                    {s.title} — {s.company_name || 'company not identified'}
                  </li>
                ))}
                {selected.length > 12 && <li>…and {selected.length - 12} more.</li>}
              </ul>
            </>
          )}
        </Modal>
      )}

      {generating && (
        <Modal
          title={`Generate applications for ${selected.length} opportunities`}
          onClose={() => setGenerating(false)}
          actions={
            <>
              <button className="btn" onClick={() => setGenerating(false)}>
                Cancel
              </button>
              <button className="btn btn-primary" disabled={busy} onClick={generate}>
                {busy ? <span className="spinner" /> : 'Generate'}
              </button>
            </>
          }
        >
          <p className="section-intro">
            A tailored CV, a company and job briefing, a motivation document and an introduction
            email are written for each of these. Nothing is sent: everything waits for you to read
            and approve it on the Applications screen.
          </p>
          <ul className="help-tips">
            {selected.slice(0, 12).map((s) => (
              <li key={s.id}>
                {s.title} — {s.company_name || 'company not identified'}{' '}
                {s.is_speculative ? '(speculative opening)' : ''}
              </li>
            ))}
            {selected.length > 12 && <li>…and {selected.length - 12} more.</li>}
          </ul>
          {selected.some((s) => s.is_speculative) && (
            <div className="alert alert-warn" style={{ marginTop: 12 }}>
              <div>
                Some of these are speculative openings. Their emails are written as spontaneous
                applications and never claim an advertised vacancy exists (FR-263).
              </div>
            </div>
          )}
        </Modal>
      )}

      {comparison && <CompareModal result={comparison} onClose={() => setComparison(null)} />}
    </div>
  )
}
