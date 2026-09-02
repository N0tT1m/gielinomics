import type { components, operations } from './schema'

/** Shorthand for a generated response schema. */
type Schema<K extends keyof components['schemas']> = components['schemas'][K]

export type ItemSummary = Schema<'ItemSummary'>
export type ItemDetail = Schema<'ItemDetail'>
export type ItemPriceSeries = Schema<'ItemPriceSeries'>
export type ItemStats = Schema<'ItemStats'>
export type PricePoint = Schema<'PricePoint'>
export type MoversResponse = Schema<'MoversResponse'>
export type MarketMover = Schema<'MarketMover'>
export type SpreadScan = Schema<'SpreadScan'>
export type MarginCandidate = Schema<'MarginCandidate'>
export type IngestStatusResponse = Schema<'IngestStatusResponse'>
export type FeedStatus = Schema<'FeedStatus'>
export type CoverageReport = Schema<'CoverageReport'>
export type PlayerResponse = Schema<'PlayerResponse'>
export type PlayerGainsResponse = Schema<'PlayerGainsResponse'>
export type PlayerHistoryResponse = Schema<'PlayerHistoryResponse'>
export type SkillGain = Schema<'SkillGain'>
export type Player = Schema<'Player'>

/** A page of search results. The generated name carries the element type. */
export type ItemPage = Schema<'PageOfItemSummary'>

/** Query parameters, taken from the generated operation rather than restated. */
export type ItemSearchQuery = NonNullable<operations['SearchItems']['parameters']['query']>

/** A failed request, carrying whatever the server said about why. */
export class ApiError extends Error {
  // Assigned in the body rather than declared as constructor parameter properties: the
  // tsconfig enables `erasableSyntaxOnly`, which rules that syntax out.
  readonly status: number
  readonly detail: string | undefined

  constructor(message: string, status: number, detail?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** Drops undefined entries so they do not appear in the query string as "undefined". */
function toQuery(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const rendered = search.toString()
  return rendered ? `?${rendered}` : ''
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, {
    signal,
    headers: { accept: 'application/json' },
  })

  if (!response.ok) {
    // The API returns RFC 9457 problem details on every handled failure, so the useful
    // sentence is in `detail` rather than the status text.
    let detail: string | undefined
    try {
      const problem = (await response.json()) as { detail?: string; title?: string }
      detail = problem.detail ?? problem.title
    } catch {
      detail = undefined
    }
    throw new ApiError(detail ?? `Request failed with ${response.status}`, response.status, detail)
  }

  return (await response.json()) as T
}

export const api = {
  searchItems: (query: ItemSearchQuery, signal?: AbortSignal) =>
    get<ItemPage>(`/items${toQuery(query)}`, signal),

  getItem: (id: number, signal?: AbortSignal) => get<ItemDetail>(`/items/${id}`, signal),

  getPrices: (
    id: number,
    query: { from?: string; to?: string; interval?: string; limit?: number },
    signal?: AbortSignal,
  ) => get<ItemPriceSeries>(`/items/${id}/prices${toQuery(query)}`, signal),

  getStats: (id: number, query: { window?: string; interval?: string }, signal?: AbortSignal) =>
    get<ItemStats>(`/items/${id}/stats${toQuery(query)}`, signal),

  getMovers: (
    query: { window?: string; interval?: string; minVolume?: number; limit?: number },
    signal?: AbortSignal,
  ) => get<MoversResponse>(`/market/movers${toQuery(query)}`, signal),

  getSpreads: (query: { minVolume?: number; limit?: number }, signal?: AbortSignal) =>
    get<SpreadScan>(`/market/spreads${toQuery(query)}`, signal),

  getIngestStatus: (signal?: AbortSignal) => get<IngestStatusResponse>('/ingest/status', signal),

  getCoverage: (query: { interval?: string; window?: string }, signal?: AbortSignal) =>
    get<CoverageReport>(`/ingest/coverage${toQuery(query)}`, signal),

  getPlayer: (name: string, signal?: AbortSignal) =>
    get<PlayerResponse>(`/players/${encodeURIComponent(name)}`, signal),

  getPlayerGains: (name: string, period: string, signal?: AbortSignal) =>
    get<PlayerGainsResponse>(`/players/${encodeURIComponent(name)}/gains${toQuery({ period })}`, signal),

  getPlayerHistory: (
    name: string,
    query: { skill?: number; from?: string; limit?: number },
    signal?: AbortSignal,
  ) => get<PlayerHistoryResponse>(`/players/${encodeURIComponent(name)}/history${toQuery(query)}`, signal),
}
