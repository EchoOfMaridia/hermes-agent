import { normalize } from '@/lib/text'
import type { SessionInfo } from '@/types/hermes'

import { sessionTitle } from './chat-runtime'
import { sessionSourceSearchTerms } from './session-source'

/** Title-vs-content search mode. The quote-toggle rule: a query wrapped in
 *  double-quotes searches session content (FTS over message bodies);
 *  anything else searches session titles (case-insensitive substring). */
export type SearchMode = 'title' | 'content'

export interface SearchModeResult {
  mode: SearchMode
  /** Already trimmed + lowercased — safe to pass to a substring check. */
  needle: string
}

export function parseSearchMode(query: string): SearchModeResult {
  const trimmed = (query ?? '').trim()
  const isQuoted = trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"')

  if (isQuoted) {
    return { mode: 'content', needle: normalize(trimmed.slice(1, -1)) }
  }

  return { mode: 'title', needle: normalize(trimmed) }
}

/** Title-mode client-side filter. Matches only against the session id,
 *  lineage root, and title. Use this for the default unquoted-query path
 *  so a session whose preview contains the query but whose title doesn't
 *  does NOT appear in the result list. */
export function sessionTitleMatches(session: SessionInfo, query: string): boolean {
  const needle = normalize(query)

  if (!needle) {
    return true
  }

  return [
    session.id,
    session._lineage_root_id ?? '',
    sessionTitle(session)
  ].some(value => value.toLowerCase().includes(needle))
}

export function sessionMatchesSearch(session: SessionInfo, query: string): boolean {
  const needle = normalize(query)

  if (!needle) {
    return true
  }

  return [
    session.id,
    session._lineage_root_id ?? '',
    sessionTitle(session),
    session.preview ?? '',
    session.cwd ?? '',
    session.git_branch ?? '',
    ...sessionSourceSearchTerms(session.source)
  ].some(value => value.toLowerCase().includes(needle))
}
