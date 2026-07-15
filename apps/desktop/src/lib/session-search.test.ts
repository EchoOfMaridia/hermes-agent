import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { parseSearchMode, sessionMatchesSearch, sessionTitleMatches } from './session-search'

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: false,
    cwd: '/home/user/projects/hermes-agent',
    ended_at: null,
    id: '20260603_090200_abcd12',
    input_tokens: 0,
    is_active: false,
    last_active: 1_000,
    message_count: 2,
    model: 'claude',
    output_tokens: 0,
    preview: 'Fix Desktop session search',
    source: 'cli',
    started_at: 1_000,
    title: 'Desktop Search Feature',
    tool_call_count: 0,
    ...overrides
  }
}

describe('parseSearchMode', () => {
  it('returns title mode for empty query', () => {
    expect(parseSearchMode('')).toEqual({ mode: 'title', needle: '' })
  })

  it('returns title mode for a plain phrase', () => {
    expect(parseSearchMode('autogenesis mobile')).toEqual({ mode: 'title', needle: 'autogenesis mobile' })
  })

  it('returns content mode when the query is wrapped in double-quotes', () => {
    expect(parseSearchMode('"foo"')).toEqual({ mode: 'content', needle: 'foo' })
    expect(parseSearchMode('"foo bar"')).toEqual({ mode: 'content', needle: 'foo bar' })
  })

  it('treats mismatched quote characters as title mode', () => {
    // Lone opening quote with no closing pair → not quoted.
    expect(parseSearchMode('foo"bar')).toEqual({ mode: 'title', needle: 'foo"bar' })
    // Closing quote without opening → not quoted.
    expect(parseSearchMode('foo"')).toEqual({ mode: 'title', needle: 'foo"' })
  })

  it('lowercases and trims the returned needle', () => {
    expect(parseSearchMode('  Autogenesis Mobile  ').needle).toBe('autogenesis mobile')
    expect(parseSearchMode('"  Mixed Case Phrase  "').needle).toBe('mixed case phrase')
  })
})

describe('sessionTitleMatches', () => {
  it('matches sessions by title (case-insensitive)', () => {
    const session = makeSession({ title: 'Autogenesis mobile UI session' })

    expect(sessionTitleMatches(session, 'autogenesis mobile')).toBe(true)
    expect(sessionTitleMatches(session, 'AUTOGENESIS MOBILE')).toBe(true)
    expect(sessionTitleMatches(session, 'AuToGeNeSiS MoBiLe')).toBe(true)
  })

  it('does NOT match when the substring appears only in the preview', () => {
    const session = makeSession({
      title: 'Unrelated chat',
      preview: 'Mention of autogenesis mobile here in passing'
    })

    expect(sessionTitleMatches(session, 'autogenesis mobile')).toBe(false)
  })

  it('still matches by id and lineage root', () => {
    const session = makeSession({ id: '20260603_090200_abcd12' })

    expect(sessionTitleMatches(session, '20260603_090200_abcd12')).toBe(true)
    expect(sessionTitleMatches(session, '090200')).toBe(true)
    expect(sessionTitleMatches(session, 'ABCD12')).toBe(true)

    const branched = makeSession({
      id: '20260603_010000_tip01',
      _lineage_root_id: '20260602_235959_root99'
    })

    expect(sessionTitleMatches(branched, 'root99')).toBe(true)
    expect(sessionTitleMatches(branched, '20260602')).toBe(true)
  })

  it('returns true for an empty query (matches everything)', () => {
    const session = makeSession()

    expect(sessionTitleMatches(session, '')).toBe(true)
    expect(sessionTitleMatches(session, '   ')).toBe(true)
  })

  it('does NOT match on cwd, source, or preview (title-only contract)', () => {
    const session = makeSession({
      title: 'Some other chat',
      preview: 'autogenesis mobile mentioned in body',
      cwd: '/home/cage/projects/autogenesis-mobile',
      source: 'autogenesis-mobile-platform'
    })

    expect(sessionTitleMatches(session, 'autogenesis-mobile')).toBe(false)
  })
})

describe('sessionMatchesSearch', () => {
  it('matches loaded sessions by full and partial session id', () => {
    const session = makeSession()

    expect(sessionMatchesSearch(session, '20260603_090200_abcd12')).toBe(true)
    expect(sessionMatchesSearch(session, '090200')).toBe(true)
    expect(sessionMatchesSearch(session, 'ABCD12')).toBe(true)
  })

  it('matches projected compression sessions by lineage root id', () => {
    const session = makeSession({
      _lineage_root_id: '20260602_235959_root99',
      id: '20260603_010000_tip01'
    })

    expect(sessionMatchesSearch(session, 'root99')).toBe(true)
    expect(sessionMatchesSearch(session, '20260602')).toBe(true)
  })

  it('preserves title, preview, and workspace matching', () => {
    const session = makeSession()

    expect(sessionMatchesSearch(session, 'desktop search')).toBe(true)
    expect(sessionMatchesSearch(session, 'session search')).toBe(true)
    expect(sessionMatchesSearch(session, 'hermes-agent')).toBe(true)
  })

  it('matches sessions by source platform and aliases', () => {
    expect(sessionMatchesSearch(makeSession({ source: 'telegram' }), 'Telegram')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'whatsapp' }), 'WhatsApp')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'whatsapp' }), 'wa')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'slack' }), 'slack')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'bluebubbles' }), 'imessage')).toBe(true)
  })

  it('matches case-insensitively across all checked fields', () => {
    const session = makeSession({
      title: 'Autogenesis mobile UI',
      preview: '...',
      cwd: '/home/cage/projects/autogenesis'
    })

    expect(sessionMatchesSearch(session, 'AUTOGENESIS')).toBe(true)
    expect(sessionMatchesSearch(session, 'AuToGeNeSiS')).toBe(true)
    expect(sessionMatchesSearch(session, 'HOME/CAGE/PROJECTS/AUTOGENESIS')).toBe(true)
  })

  it('does not match unrelated queries', () => {
    expect(sessionMatchesSearch(makeSession(), 'totally-unrelated')).toBe(false)
  })
})
