/**
 * Application shell and routing (CR-407).
 *
 * The navigation mirrors the pipeline in section 2.3 of the specification -
 * profile, then directives, then campaign, then results, then applying, then
 * what happens afterwards - because that ordering is the mental model the
 * whole product is built on.
 */

import { Suspense, lazy, useEffect, useState } from 'react'
import {
  BrowserRouter,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom'

import { api, setUnauthorizedHandler } from './api/client'
import { closeLiveEvents } from './api/liveEvents'
import { HelpButton, HelpPanel } from './components/Help'
import Icon from './components/Icon'
import { LiveToasts, NotificationBell } from './components/LiveNotifications'
import TopbarCounters from './components/TopbarCounters'
import { SessionContext } from './session'
import SignIn from './pages/SignIn'

// Route-level code splitting (NFR-101). The shell and the screen you asked
// for load; the other twenty do not. Without this every visitor downloads
// the company profile, the PDF previews and the whole administration area
// before the profile page can paint.
const HomePage = lazy(() => import('./pages/HomePage'))
const OverviewPage = lazy(() => import('./pages/OverviewPage'))
const ContactsPage = lazy(() => import('./pages/ContactsPage'))
const ProfilePage = lazy(() => import('./pages/ProfilePage'))
const CompositePage = lazy(() => import('./pages/CompositePage'))
const DreamJobPage = lazy(() => import('./pages/DreamJobPage'))
const DirectivesPage = lazy(() => import('./pages/DirectivesPage'))
const CampaignsPage = lazy(() => import('./pages/CampaignsPage'))
const CampaignDetailPage = lazy(() => import('./pages/CampaignDetailPage'))
const BrowserPage = lazy(() => import('./pages/BrowserPage'))
const OpportunitiesPage = lazy(() => import('./pages/OpportunitiesPage'))
const OpportunityDetailPage = lazy(() => import('./pages/OpportunityDetailPage'))
const CompaniesPage = lazy(() => import('./pages/CompaniesPage'))
const CompanyDetailPage = lazy(() => import('./pages/CompanyDetailPage'))
const ApplicationsPage = lazy(() => import('./pages/ApplicationsPage'))
const ApplyBrowserPage = lazy(() => import('./pages/ApplyBrowserPage'))
const PipelinePage = lazy(() => import('./pages/PipelinePage'))
const ResponsesPage = lazy(() => import('./pages/ResponsesPage'))
const InsightsPage = lazy(() => import('./pages/InsightsPage'))
const MonitoringPage = lazy(() => import('./pages/MonitoringPage'))
const IntelligencePage = lazy(() => import('./pages/IntelligencePage'))
const NetworkingPage = lazy(() => import('./pages/NetworkingPage'))
const MailSettingsPage = lazy(() => import('./pages/MailSettingsPage'))
const AdminPage = lazy(() => import('./pages/AdminPage'))


// Navigation follows the three things a job seeker actually does, not the
// fifteen internal stages they do not think in. Every screen is still here; the
// ones that are configuration rather than a step live under Advanced, folded
// away by default. Nothing was removed.
const STEPS = [
  { to: '/home', icon: 'overview', label: 'Start here' },
  { to: '/profile', icon: 'profile', label: '1 · Your profile' },
  { to: '/opportunities', icon: 'opportunities', label: '2 · Opportunities' },
  { to: '/applications', icon: 'send', label: '3 · Apply' },
  { to: '/pipeline', icon: 'pipeline', label: 'Follow up' },
]

// The detail screens, folded into four groups rather than six. Every
// destination is still here; each item carries the phase hue of the screen it
// opens, so regrouping a group never recolours a screen.
const ADVANCED = [
  {
    label: 'Profile & search',
    phase: 'phase-1',
    items: [
      { to: '/composite', icon: 'composite', label: 'Composite profile', phase: 'phase-1' },
      { to: '/dream-job', icon: 'dream', label: 'Dream job', phase: 'phase-1' },
      { to: '/directives', icon: 'directives', label: 'Directives', phase: 'phase-2' },
      { to: '/campaigns', icon: 'campaign', label: 'Campaigns', phase: 'phase-2' },
      { to: '/browser', icon: 'browser', label: 'Browser session', phase: 'phase-2' },
    ],
  },
  {
    label: 'Research & apply',
    phase: 'phase-3',
    items: [
      { to: '/companies', icon: 'companies', label: 'Companies', phase: 'phase-3' },
      { to: '/intelligence', icon: 'intelligence', label: 'Dream-job gap', phase: 'phase-3' },
      { to: '/contacts', icon: 'contacts', label: 'Contacts', phase: 'phase-3' },
      { to: '/apply', icon: 'send', label: 'Apply browser', phase: 'phase-4' },
      { to: '/networking', icon: 'networking', label: 'Networking', phase: 'phase-4' },
    ],
  },
  {
    label: 'Journey & follow up',
    phase: 'phase-5',
    items: [
      { to: '/overview', icon: 'overview', label: 'Where I am', phase: 'phase-2' },
      { to: '/responses', icon: 'responses', label: 'Responses', phase: 'phase-5' },
      { to: '/insights', icon: 'insights', label: 'What works', phase: 'phase-5' },
    ],
  },
  {
    label: 'System',
    phase: 'phase-0',
    items: [
      { to: '/monitoring', icon: 'monitoring', label: 'Monitoring', phase: 'phase-0' },
      { to: '/mail', icon: 'mailsetup', label: 'Mail setup', phase: 'phase-0' },
      { to: '/admin', icon: 'admin', label: 'Administration', phase: 'phase-0' },
    ],
  },
]

// Kept as one list for the header hue and icon lookups.
const NAV = [
  { label: 'Steps', phase: 'phase-0', items: STEPS },
  ...ADVANCED,
]

// Which phase hue a route belongs to, for the header and page furniture. The
// steps keep the shell's slate; every Advanced item carries its own phase.
const ROUTE_PHASE = Object.fromEntries(
  NAV.flatMap((g) => g.items.map((i) => [i.to, i.phase || g.phase])),
)

const ROUTE_ICON = Object.fromEntries(
  NAV.flatMap((g) => g.items.map((i) => [i.to, i.icon])),
)

// One name per screen. `/overview` is the detailed journey map ("Where I am"),
// not a second landing: `/home` is the single landing that carries the next
// action, and the map is the detail behind it.
const TITLES = {
  '/home': 'Start here',
  '/overview': 'Where I am',
  '/responses': 'Responses received',
  '/insights': 'What works',
  '/profile': 'Profile',
  '/composite': 'Composite profile',
  '/dream-job': 'Dream job',
  '/directives': 'Search directives',
  '/campaigns': 'Campaigns',
  '/browser': 'Browser session',
  '/opportunities': 'Opportunities',
  '/companies': 'Companies',
  '/intelligence': 'Dream-job intelligence',
  '/apply': 'Apply browser',
  '/contacts': 'Hiring contacts',
  '/applications': 'Applications',
  '/networking': 'Networking and export',
  '/pipeline': 'Application pipeline',
  '/monitoring': 'Monitoring',
  '/mail': 'Mail setup',
  '/admin': 'Administration',
}

function Shell({ session, onSignOut }) {
  const location = useLocation()
  const [helpOpen, setHelpOpen] = useState(false)

  // Opening a different screen closes the drawer: help is about where you are.
  useEffect(() => setHelpOpen(false), [location.pathname])

  const base = '/' + location.pathname.split('/')[1]
  const title = TITLES[base] || 'Dream Job'
  const phase = ROUTE_PHASE[base] || 'phase-0'

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="row" style={{ gap: 8, alignItems: 'center' }}>
            <img src="/icon-48.png" alt="" width={22} height={22} />
            <h1>Dream Job</h1>
          </div>
          <p>{session.display_name}</p>
        </div>
        {/* The three steps, always visible. */}
        <nav className="nav-group phase-0">
          <div className="nav-label">Do this</div>
          {STEPS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}
            >
              <Icon name={item.icon} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        {/* Everything else. Expanded by default so every destination is one
            click away and reachable by name; still collapsible, so it never
            competes with the steps above it. */}
        <details className="nav-advanced" open>
          <summary className="nav-label">Advanced</summary>
          {ADVANCED.map((group) => (
            <nav className={`nav-group ${group.phase}`} key={group.label}>
              <div className="nav-sub-label">{group.label}</div>
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `nav-item ${item.phase || ''}` + (isActive ? ' active' : '')
                  }
                >
                  <Icon name={item.icon} />
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </nav>
          ))}
        </details>
      </aside>

      <div className={`main ${phase}`}>
        <header className="topbar">
          <h2>
            <Icon name={ROUTE_ICON[base] || 'overview'} />
            {title}
          </h2>
          <div className="spacer" />
          <TopbarCounters />
          <NotificationBell />
          <span className="small muted">{session.email}</span>
          <HelpButton onOpen={() => setHelpOpen(true)} />
          <button className="btn btn-sm btn-ghost" onClick={onSignOut}>
            Sign out
          </button>
        </header>

        {session.discretion_mode && (
          <div className="discretion-bar">
            <Icon name="lock" />
            <span>
              Discretion mode is active — the current employer, its group entities and
              exposing contacts are excluded from every search, ranking and message.
            </span>
          </div>
        )}

        <div className="content">
          <Suspense
            fallback={
              <div className="empty" style={{ paddingTop: 80 }}>
                <span className="spinner" />
              </div>
            }
          >
            <Routes>
              <Route path="/" element={<Navigate to="/home" replace />} />
              <Route path="/home" element={<HomePage />} />
              <Route path="/overview" element={<OverviewPage />} />
              <Route path="/profile" element={<ProfilePage />} />
              <Route path="/composite" element={<CompositePage />} />
              <Route path="/dream-job" element={<DreamJobPage />} />
              <Route path="/directives" element={<DirectivesPage />} />
              <Route path="/campaigns" element={<CampaignsPage />} />
              <Route path="/campaigns/:id" element={<CampaignDetailPage />} />
              <Route path="/browser" element={<BrowserPage />} />
              <Route path="/opportunities" element={<OpportunitiesPage />} />
              <Route path="/opportunities/:id" element={<OpportunityDetailPage />} />
              <Route path="/companies" element={<CompaniesPage />} />
              <Route path="/companies/:id" element={<CompanyDetailPage />} />
              <Route path="/apply" element={<ApplyBrowserPage />} />
              <Route path="/contacts" element={<ContactsPage />} />
              <Route path="/applications" element={<ApplicationsPage />} />
              <Route path="/pipeline" element={<PipelinePage />} />
              <Route path="/responses" element={<ResponsesPage />} />
              <Route path="/insights" element={<InsightsPage />} />
              <Route path="/monitoring" element={<MonitoringPage />} />
              <Route path="/intelligence" element={<IntelligencePage />} />
              <Route path="/networking" element={<NetworkingPage />} />
              <Route path="/mail" element={<MailSettingsPage />} />
              <Route path="/admin" element={<AdminPage />} />
              <Route path="*" element={<div className="empty"><h3>Not found</h3></div>} />
            </Routes>
          </Suspense>
        </div>
      </div>

      <HelpPanel
        pathname={location.pathname}
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
      />
      <LiveToasts />
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState(undefined) // undefined = still checking

  useEffect(() => {
    // A lapsed session also ends the live stream, so it never retries under a
    // cookie the backend has already refused.
    setUnauthorizedHandler(() => {
      closeLiveEvents()
      setSession(null)
    })
    api
      .get('/auth/me')
      .then(setSession)
      .catch(() => setSession(null))
  }, [])

  async function signOut() {
    try {
      await api.post('/auth/logout')
    } finally {
      closeLiveEvents()
      setSession(null)
    }
  }

  if (session === undefined) {
    return (
      <div className="empty" style={{ paddingTop: 120 }}>
        <span className="spinner" />
      </div>
    )
  }

  if (session === null) {
    return <SignIn onSignedIn={setSession} />
  }

  return (
    <SessionContext.Provider value={{ session, setSession }}>
      <BrowserRouter>
        <Shell session={session} onSignOut={signOut} />
      </BrowserRouter>
    </SessionContext.Provider>
  )
}
