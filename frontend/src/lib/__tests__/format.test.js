import { describe, it, expect } from 'vitest'
import {
  formatDistance,
  formatDuration,
  formatDurationMs,
  getRouteDurationMs,
  formatDate,
  formatTime,
  formatVideoTime,
  formatBytes,
  storageLevel,
} from '../format.js'

// ── formatDistance ──────────────────────────────────────────

describe('formatDistance', () => {
  it('null returns --', () => {
    expect(formatDistance(null)).toBe('--')
    expect(formatDistance(undefined)).toBe('--')
  })

  it('0 returns 0 m', () => {
    expect(formatDistance(0)).toBe('0 m')
  })

  it('sub-km returns meters', () => {
    const result = formatDistance(483)
    expect(result).toMatch(/\d+ m/)
    expect(result).not.toContain('km')
  })

  it('over-km returns km', () => {
    const result = formatDistance(8050)
    expect(result).toMatch(/[\d.]+ km/)
  })
})

// ── formatDuration ─────────────────────────────────────────

describe('formatDuration', () => {
  it('null returns --', () => {
    expect(formatDuration(null)).toBe('--')
  })

  it('minutes only', () => {
    expect(formatDuration(45)).toBe('45m')
  })

  it('hours + minutes', () => {
    expect(formatDuration(90)).toBe('1h 30m')
  })

  it('0 returns 0m', () => {
    expect(formatDuration(0)).toBe('0m')
  })
})

// ── formatDurationMs ───────────────────────────────────────

describe('formatDurationMs', () => {
  it('delegates to formatDuration', () => {
    // 2700000ms = 45 minutes
    expect(formatDurationMs(2700000)).toBe('45m')
  })
})

// ── getRouteDurationMs ─────────────────────────────────────

describe('getRouteDurationMs', () => {
  it('from ISO times', () => {
    const route = {
      start_time: '2025-01-01T00:00:00Z',
      end_time: '2025-01-01T01:00:00Z',
    }
    expect(getRouteDurationMs(route)).toBe(3600000)
  })

  it('fallback to maxqlog', () => {
    const route = { maxqlog: 4 }
    expect(getRouteDurationMs(route)).toBe(300000) // (4+1)*60000
  })

  it('null route returns 0', () => {
    expect(getRouteDurationMs(null)).toBe(0)
    expect(getRouteDurationMs({})).toBe(0)
  })
})

// ── formatDate ─────────────────────────────────────────────

describe('formatDate', () => {
  it('null/0 returns --', () => {
    expect(formatDate(null)).toBe('--')
    expect(formatDate(0)).toBe('--')
    expect(formatDate(undefined)).toBe('--')
  })

  it('counter < 1B returns Pending...', () => {
    expect(formatDate(42)).toBe('Pending...')
    expect(formatDate(999999999)).toBe('Pending...')
  })

  it('valid epoch returns date string', () => {
    // 1700000000 = 2023-11-14
    const result = formatDate(1700000000)
    expect(result).not.toBe('--')
    expect(result).not.toBe('Pending...')
    expect(typeof result).toBe('string')
    expect(result.length).toBeGreaterThan(3)
  })

  it('ISO string works', () => {
    const result = formatDate('2025-06-15T10:30:00Z')
    expect(result).not.toBe('--')
    expect(typeof result).toBe('string')
  })
})

// ── formatTime ─────────────────────────────────────────────

describe('formatTime', () => {
  it('null returns empty', () => {
    expect(formatTime(null)).toBe('')
    expect(formatTime(undefined)).toBe('')
    expect(formatTime(0)).toBe('')
  })

  it('counter returns empty', () => {
    expect(formatTime(42)).toBe('')
  })

  it('valid epoch returns HH:MM', () => {
    const result = formatTime(1700000000)
    expect(result).toMatch(/\d{1,2}:\d{2}/)
  })

  it('1970 ISO returns empty', () => {
    expect(formatTime('1970-01-01T00:00:00Z')).toBe('')
  })
})

// ── formatVideoTime ────────────────────────────────────────

describe('formatVideoTime', () => {
  it('0 returns 0:00', () => {
    expect(formatVideoTime(0)).toBe('0:00')
  })

  it('90 returns 1:30', () => {
    expect(formatVideoTime(90)).toBe('1:30')
  })

  it('negative returns 0:00', () => {
    expect(formatVideoTime(-5)).toBe('0:00')
  })

  it('fractional seconds truncated', () => {
    expect(formatVideoTime(61.9)).toBe('1:01')
  })
})

// ── formatBytes ────────────────────────────────────────────

describe('formatBytes', () => {
  it('0 returns 0 B', () => {
    expect(formatBytes(0)).toBe('0 B')
  })

  it('null returns 0 B', () => {
    expect(formatBytes(null)).toBe('0 B')
  })

  it('KB', () => {
    expect(formatBytes(1536)).toBe('1.5 KB')
  })

  it('GB', () => {
    const gb = 2.5 * 1024 * 1024 * 1024
    expect(formatBytes(gb)).toBe('2.5 GB')
  })
})

// ── storageLevel ───────────────────────────────────────────

describe('storageLevel', () => {
  it('critical below 10', () => {
    expect(storageLevel(5)).toBe('critical')
    expect(storageLevel(9.9)).toBe('critical')
  })

  it('warning at 10-24', () => {
    expect(storageLevel(10)).toBe('warning')
    expect(storageLevel(24.9)).toBe('warning')
  })

  it('ok at 25+', () => {
    expect(storageLevel(25)).toBe('ok')
    expect(storageLevel(80)).toBe('ok')
  })
})
