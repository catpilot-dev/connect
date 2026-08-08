// Pure formatting utilities — no side effects, no imports

/** Format distance in meters to readable string (metric) */
export function formatDistance(meters) {
  if (meters == null) return '--'
  if (meters < 1000) return `${meters.toFixed(0)} m`
  return `${(meters / 1000).toFixed(1)} km`
}

/** Format duration in minutes to readable string */
export function formatDuration(minutes) {
  if (minutes == null) return '--'
  const hrs = Math.floor(minutes / 60)
  const min = Math.round(minutes % 60)
  return hrs > 0 ? `${hrs}h ${min}m` : `${min}m`
}

/** Format duration in milliseconds */
export function formatDurationMs(ms) {
  return formatDuration(ms / 60000)
}

/** Get route duration in milliseconds from start/end time */
export function getRouteDurationMs(route) {
  if (!route?.start_time || !route?.end_time) {
    // Fallback: estimate from segment count (60s per segment)
    if (route?.maxqlog != null) return (route.maxqlog + 1) * 60000
    return 0
  }
  return new Date(route.end_time).getTime() - new Date(route.start_time).getTime()
}

/** Format epoch timestamp to local date string — "Friday Feb 20" */
export function formatDate(epoch) {
  if (!epoch) return '--'
  // Counter-based create_time (unenriched routes) — not a real epoch
  if (typeof epoch === 'number' && epoch < 1_000_000_000) return 'Pending...'
  const d = typeof epoch === 'string' ? new Date(epoch) : new Date(epoch * 1000)
  const now = new Date()

  const weekday = d.toLocaleDateString('en-US', { weekday: 'long' })
  const month = d.toLocaleDateString('en-US', { month: 'short' })
  const day = d.getDate()
  const yearSuffix = d.getFullYear() !== now.getFullYear() ? ` ${d.getFullYear()}` : ''

  let prefix = ''
  if (d.toDateString() === now.toDateString()) {
    prefix = 'Today \u2013 '
  } else {
    const yesterday = new Date(now)
    yesterday.setDate(yesterday.getDate() - 1)
    if (d.toDateString() === yesterday.toDateString()) {
      prefix = 'Yesterday \u2013 '
    }
  }
  return `${prefix}${weekday} ${month} ${day}${yearSuffix}`
}

/** Format epoch or ISO string to local time string */
export function formatTime(epoch) {
  if (!epoch) return ''
  // Counter-based or invalid small epoch — not a real timestamp
  if (typeof epoch === 'number' && epoch < 1_000_000_000) return ''
  const d = typeof epoch === 'string' ? new Date(epoch) : new Date(epoch * 1000)
  if (isNaN(d.getTime()) || d.getFullYear() < 2000) return ''
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

/** Format start/end time range — "@ 15:39 - 15:45" */
export function formatTimeRange(startTime, endTime) {
  const start = formatTime(startTime)
  const end = formatTime(endTime)
  if (!start) return ''
  return end ? `@ ${start} - ${end}` : `@ ${start}`
}

/** Format seconds to M:SS video time */
export function formatVideoTime(totalSeconds) {
  const sec = Math.max(0, Math.floor(totalSeconds))
  const m = Math.floor(sec / 60)
  const s = String(sec % 60).padStart(2, '0')
  return `${m}:${s}`
}

/** Format seconds to HH:MM:SS video time */
export function formatVideoTimeHMS(totalSeconds) {
  const sec = Math.max(0, Math.floor(totalSeconds))
  const h = String(Math.floor(sec / 3600)).padStart(2, '0')
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0')
  const s = String(sec % 60).padStart(2, '0')
  return `${h}:${m}:${s}`
}

/** Format start time (epoch or ISO string) + offset seconds to HH:MM:SS local time */
export function formatAbsoluteTime(start, offsetSeconds) {
  if (!start) return null
  const baseMs = typeof start === 'number'
    ? (start < 1_000_000_000 ? null : start * 1000)
    : new Date(start).getTime()
  if (!baseMs || isNaN(baseMs)) return null
  const d = new Date(baseMs + offsetSeconds * 1000)
  if (isNaN(d.getTime()) || d.getFullYear() < 2000) return null
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** Format an absolute epoch (seconds) — used for log-derived frame times */
export function formatEpochTime(epochSeconds) {
  if (!epochSeconds) return null
  const d = new Date(epochSeconds * 1000)
  if (isNaN(d.getTime()) || d.getFullYear() < 2000) return null
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** Format start time + offset to HH:MM (no seconds) */
export function formatAbsoluteTimeHM(start, offsetSeconds) {
  if (!start) return null
  const baseMs = typeof start === 'number'
    ? (start < 1_000_000_000 ? null : start * 1000)
    : new Date(start).getTime()
  if (!baseMs || isNaN(baseMs)) return null
  const d = new Date(baseMs + offsetSeconds * 1000)
  if (isNaN(d.getTime()) || d.getFullYear() < 2000) return null
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

/** Format bytes to human-readable */
export function formatBytes(bytes) {
  if (bytes == null || bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  const val = bytes / Math.pow(1024, i)
  return `${val.toFixed(i > 0 ? 1 : 0)} ${units[i]}`
}

/** Format a storage percent as colored level */
export function storageLevel(percentFree) {
  if (percentFree < 10) return 'critical'
  if (percentFree < 25) return 'warning'
  return 'ok'
}
