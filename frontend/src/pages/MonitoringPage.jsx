/**
 * Monitoring — the watchlist, notifications, timing and the weekly digest
 * (FR-401, FR-402, FR-403).
 *
 * This is the only screen that keeps working after you have stopped working.
 * A campaign is a one-off look at the market; a watch is a standing one, and
 * the digest is what turns a fortnight of small changes into a page you can
 * read on a Monday morning.
 *
 * The four panels are tabs rather than one long page because they are read at
 * different moments: the digest weekly, notifications daily, the watchlist when
 * something needs changing, timing when you are deciding whom to approach now.
 * The scheduler sits underneath all of them, which is why its tab is last and
 * its status line is visible from every other tab.
 */

import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import { FirstRun, ScreenIntro } from '../components/Help'
import WorkflowMap from '../components/WorkflowMap'
import { ErrorBox, Loading, Tabs, useFetch } from '../components/ui'
import { useSession } from '../session'
import Digest from './monitoring/Digest'
import Notifications from './monitoring/Notifications'
import Scheduler from './monitoring/Scheduler'
import Timing from './monitoring/Timing'
import Watchlist from './monitoring/Watchlist'
import { AddWatchModal } from './monitoring/WatchModals'

const TAB_KEYS = ['watchlist', 'notifications', 'timing', 'digest', 'scheduler']

export default function MonitoringPage() {
  const { session } = useSession()
  // `?tab=` lets the top-bar bell open straight onto the notifications.
  const [searchParams] = useSearchParams()
  const [tab, setTab] = useState(() =>
    TAB_KEYS.includes(searchParams.get('tab')) ? searchParams.get('tab') : 'watchlist',
  )
  // Clicking the bell while already here changes only the query string.
  const tabParam = searchParams.get('tab')
  useEffect(() => {
    if (TAB_KEYS.includes(tabParam)) setTab(tabParam)
  }, [tabParam])
  const [adding, setAdding] = useState(false)

  const watchlist = useFetch(() => api.get('/monitoring/watchlist'))
  const notifications = useFetch(() => api.get('/monitoring/notifications?limit=100'))
  const digests = useFetch(() => api.get('/monitoring/digests?limit=20'))
  const scheduler = useFetch(() => api.get('/monitoring/scheduler'))

  // Reference data and context. Neither is worth failing the screen over: the
  // channel list has a known default and the campaign list only fills a picker.
  const channels = useFetch(() => api.get('/monitoring/watchlist/channels').catch(() => null))
  const campaigns = useFetch(() => api.get('/campaigns').catch(() => []))

  const entries = watchlist.data || []
  const unread = notifications.data?.unread ?? 0

  // Three distinct states, and "settled" is what separates the second from the
  // third: `useFetch` keeps the previous data across a reload, so an empty
  // screen is only genuinely empty once all three reads have answered at least
  // once. Without that, a reload would flash the first-run guidance.
  const settled = Boolean(watchlist.data && notifications.data && digests.data)
  const firstLoad =
    !settled && (watchlist.loading || notifications.loading || digests.loading)

  // Every private read failed. Almost always the session rather than the data,
  // so it is shown once at the top instead of three times inside the tabs.
  const fatal =
    watchlist.error && notifications.error && digests.error ? watchlist.error : null

  const nothingAtAll =
    settled &&
    !entries.length &&
    !(notifications.data?.notifications || []).length &&
    !(digests.data || []).length

  function reloadAll() {
    watchlist.reload()
    notifications.reload()
    digests.reload()
  }

  const tabs = [
    { key: 'watchlist', label: 'Watchlist', count: entries.length || undefined },
    { key: 'notifications', label: 'Notifications', count: unread || undefined },
    { key: 'timing', label: 'Timing' },
    { key: 'digest', label: 'Weekly digest', count: (digests.data || []).length || undefined },
    { key: 'scheduler', label: 'Scheduler' },
  ]

  return (
    <div className="content-wide">
      {/* Monitoring is not a stage of the pipeline; it is the loop that keeps
          feeding it. The strip is here for orientation, marked at the phase
          whose work it continues. */}
      <WorkflowMap journey={{}} compact current="pipeline" />

      <ScreenIntro pathname="/monitoring" />

      {fatal && <ErrorBox error={fatal} onRetry={reloadAll} />}
      {firstLoad && <Loading rows={5} />}

      {nothingAtAll && (
        <FirstRun
          pathname="/monitoring"
          action={
            <button className="btn btn-primary" onClick={() => setAdding(true)}>
              Watch a company
            </button>
          }
        >
          Nothing is being watched yet, so there is nothing to report. Pick a company you would
          take a job at, and it will be rechecked on a schedule for new vacancies, hiring
          signals, news and newly filed accounts — the weekly digest then tells you what changed
          and what to do about it.
        </FirstRun>
      )}

      {!firstLoad && !fatal && !nothingAtAll && (
        <>
          <Tabs tabs={tabs} active={tab} onChange={setTab} />

          {tab === 'watchlist' && (
            <Watchlist
              entries={entries}
              channels={channels.data}
              campaigns={campaigns.data}
              loading={watchlist.loading}
              error={watchlist.error}
              reload={watchlist.reload}
              onAdd={() => setAdding(true)}
            />
          )}

          {tab === 'notifications' && (
            <Notifications
              data={notifications.data}
              loading={notifications.loading}
              error={notifications.error}
              reload={notifications.reload}
              onAdd={() => setAdding(true)}
            />
          )}

          {tab === 'timing' && (
            <Timing
              entries={entries}
              watchLoading={watchlist.loading}
              watchError={watchlist.error}
              onAdd={() => setAdding(true)}
            />
          )}

          {tab === 'digest' && (
            <Digest
              digests={digests.data}
              loading={digests.loading}
              error={digests.error}
              reload={digests.reload}
            />
          )}

          {tab === 'scheduler' && (
            <Scheduler
              status={scheduler.data}
              loading={scheduler.loading}
              error={scheduler.error}
              reload={scheduler.reload}
              isAdmin={Boolean(session?.is_admin)}
            />
          )}
        </>
      )}

      {/* Kept at page level so both the empty state and the watchlist header
          open the same dialog. */}
      {adding && (
        <AddWatchModal
          channels={channels.data}
          campaigns={campaigns.data}
          onClose={() => setAdding(false)}
          onAdded={() => {
            setAdding(false)
            reloadAll()
          }}
        />
      )}

      {/* Visible from every tab: a stopped loop explains a quiet screen better
          than any empty state can. */}
      {!firstLoad && scheduler.data && !scheduler.data.running && tab !== 'scheduler' && (
        <p className="small muted" style={{ marginTop: 14 }}>
          The scheduler is not running inside this process, so rechecks and digests happen only
          when you ask for them here or when a cron job drives them. See the Scheduler tab.
        </p>
      )}
    </div>
  )
}
