import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Card, ErrorNote, Loading } from '../components/ui'
import { useApi } from '../hooks/useApi'

/**
 * Wiki search, against the AI service.
 *
 * A different question from the Items tab, which is why it is a different tab. Items searches
 * this platform's own catalogue by name substring — you have to know roughly what the thing is
 * called. This searches the wiki by meaning, so "boss that heals itself when you use the wrong
 * attack style" finds a page whose title contains none of those words.
 *
 * Results link out to the wiki rather than into the app. They are articles, not rows: this
 * platform has price history for an item, and the wiki has the explanation of what it does, and
 * pretending the second is the first would mean reproducing 35,000 pages here.
 */
export function WikiView() {
  const [term, setTerm] = useState('')
  const [debounced, setDebounced] = useState('')

  // Debounced like the item search, though for a different reason: this one embeds the query
  // and fans out to the wiki's own search API, so a request per keystroke is somebody else's
  // rate limit as well as ours.
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(term.trim()), 300)
    return () => clearTimeout(timer)
  }, [term])

  const health = useApi((signal) => api.getAiHealth(signal), [])
  const results = useApi(
    (signal) => (debounced ? api.searchWiki({ q: debounced, limit: 12 }, signal) : Promise.resolve(null)),
    [debounced],
  )

  // Two different absences, and they need different sentences. The service not being there at
  // all is the expected state for anyone running only the .NET half; the service being there
  // with no index is the expected state of a fresh deployment, whose index volume starts empty.
  // Both get a sentence and the command that fixes them rather than a red error box.
  if (health.error) {
    return (
      <Card title="Wiki" note="Search the Old School RuneScape wiki by meaning.">
        <p className="empty">
          The AI service is not reachable. Start it with <code>docker compose up ai</code>, or
          run <code>reldo serve</code> in <code>src/Gielinomics.Ai</code>.
        </p>
      </Card>
    )
  }

  if (health.data && !health.data.search) {
    return (
      <Card title="Wiki" note="Search the Old School RuneScape wiki by meaning.">
        <p className="empty">
          The AI service is running but has no search index yet. Build one with{' '}
          <code>docker compose run --rm ai reldo build</code>. It takes about twenty minutes and
          only has to happen once.
        </p>
      </Card>
    )
  }

  return (
    <Card
      title="Wiki"
      note="Search the wiki by meaning, not by name. No model runs — this is retrieval only."
    >
      <div className="filters">
        <input
          type="search"
          value={term}
          placeholder="Describe what you are looking for…"
          aria-label="Search the wiki"
          onChange={(event) => setTerm(event.target.value)}
          style={{ flex: '1 1 320px' }}
        />
      </div>

      {results.loading && !results.data ? (
        <Loading what="wiki pages" />
      ) : results.error ? (
        <ErrorNote error={results.error} />
      ) : results.data && results.data.results.length > 0 ? (
        <ul className="hits">
          {results.data.results.map((hit) => (
            <li key={hit.title}>
              <a href={hit.url} target="_blank" rel="noreferrer">
                {hit.title}
              </a>
              {/* Which ranker found it. Worth showing: the semantic half finds pages that
                  never contain your words, and knowing that is why you trust the hit. */}
              <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                {hit.foundBy.join(' + ')}
              </span>
              <p className="muted" style={{ margin: '4px 0 0' }}>
                {hit.summary}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty">
          {debounced
            ? `Nothing matches “${debounced}”.`
            : 'Try “boss that heals itself when you use the wrong attack style”.'}
        </p>
      )}
    </Card>
  )
}
