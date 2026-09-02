import { useState } from 'react'
import { api } from '../api/client'
import { exact } from '../components/charts/format'
import { Card, ErrorNote, Loading, Segmented, StatTile } from '../components/ui'
import { useApi } from '../hooks/useApi'

const PERIODS = [
  { value: 'day', label: 'Day' },
  { value: 'week', label: 'Week' },
  { value: 'month', label: 'Month' },
  { value: 'year', label: 'Year' },
] as const

export function PlayersView() {
  const [input, setInput] = useState('')
  const [name, setName] = useState('')
  const [period, setPeriod] = useState<string>('week')

  const player = useApi(
    (signal) => (name ? api.getPlayer(name, signal) : Promise.resolve(undefined)),
    [name],
  )
  const gains = useApi(
    (signal) => (name ? api.getPlayerGains(name, period, signal) : Promise.resolve(undefined)),
    [name, period],
  )

  return (
    <>
      <Card
        title="Accounts"
        note="Resolves by any name the account has ever used, so a rename does not 404 or split the timeline in two."
      >
        <form
          className="filters"
          onSubmit={(event) => {
            event.preventDefault()
            setName(input.trim())
          }}
        >
          <input
            type="search"
            value={input}
            placeholder="Player name…"
            aria-label="Player name"
            maxLength={12}
            onChange={(event) => setInput(event.target.value)}
            style={{ flex: '1 1 200px' }}
          />
          <button className="ghost" type="submit">
            Look up
          </button>
          {name && <Segmented options={PERIODS} value={period} onChange={setPeriod} label="Period" />}
        </form>

        {!name && (
          <p className="empty">
            Only tracked accounts have history here. Tracking is an authenticated write — nothing is
            polled that somebody did not ask for.
          </p>
        )}

        {name && player.loading && !player.data ? (
          <Loading what={name} />
        ) : player.error ? (
          <ErrorNote error={player.error} />
        ) : player.data ? (
          <>
            <div className="grid cols-4" style={{ marginTop: 4 }}>
              <StatTile label="Account" value={player.data.player.displayName} sub={player.data.player.accountType} />
              <StatTile
                label="Tracked since"
                value={new Date(player.data.player.addedAt).toLocaleDateString()}
              />
              <StatTile
                label="Overall gained"
                value={gains.data?.overall ? exact(gains.data.overall.gainedXp) : '—'}
                sub={`xp this ${period}`}
              />
              <StatTile
                label="Levels gained"
                value={gains.data?.overall ? exact(gains.data.overall.gainedLevels) : '—'}
                sub={`this ${period}`}
              />
            </div>

            {player.data.names.length > 1 && (
              <p className="footnote">
                Also known as{' '}
                {player.data.names
                  .filter((entry) => entry.seenTo !== null)
                  .map((entry) => entry.name)
                  .join(', ')}
                .
              </p>
            )}
          </>
        ) : null}
      </Card>

      {name && gains.data && (
        <>
          <div style={{ height: 16 }} />
          <Card title={`Gains this ${period}`} note="Skills with no movement are reported as zero, not omitted.">
            {gains.data.skills.length === 0 ? (
              <p className="empty">No samples in this window yet.</p>
            ) : (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Skill</th>
                      <th className="num">Level</th>
                      <th className="num">XP gained</th>
                      <th className="num">Levels gained</th>
                    </tr>
                  </thead>
                  <tbody>
                    {gains.data.skills.map((skill) => (
                      <tr key={skill.skill}>
                        <td>{skill.name ?? `Skill ${skill.skill}`}</td>
                        <td className="num">{skill.endLevel ?? '—'}</td>
                        <td className="num">
                          {skill.gainedXp > 0 ? <b>{exact(skill.gainedXp)}</b> : <span className="muted">0</span>}
                        </td>
                        <td className="num">
                          {skill.gainedLevels > 0 ? skill.gainedLevels : <span className="muted">0</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </>
  )
}
