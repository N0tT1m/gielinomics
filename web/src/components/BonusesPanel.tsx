import type { ItemBonuses } from '../api/client'
import { Card } from './ui'

/** A bonus and its label, used to lay the three blocks out uniformly. */
interface Stat {
  readonly label: string
  readonly value: number | null
}

function Block({ title, stats }: { readonly title: string; readonly stats: readonly Stat[] }) {
  const shown = stats.filter((stat) => stat.value !== null)
  if (shown.length === 0) return null

  return (
    <div>
      <h3>{title}</h3>
      <table>
        <tbody>
          {shown.map((stat) => (
            <tr key={stat.label}>
              <td className="muted">{stat.label}</td>
              <td className="num">
                {/* Signed: a negative bonus is real and common, and dropping the sign would
                    turn a penalty into an advantage. */}
                {stat.value! > 0 ? `+${stat.value}` : stat.value}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Equipment bonuses, grouped the way the game presents them. */
export function BonusesPanel({ bonuses }: { readonly bonuses: ItemBonuses }) {
  return (
    <Card
      title="Equipment"
      note={[bonuses.equipmentSlot, bonuses.combatStyle, bonuses.weaponAttackSpeed ? `${bonuses.weaponAttackSpeed} tick` : null]
        .filter(Boolean)
        .join(' · ')}
    >
      <div className="grid cols-4">
        <Block
          title="Attack"
          stats={[
            { label: 'Stab', value: bonuses.stabAttack },
            { label: 'Slash', value: bonuses.slashAttack },
            { label: 'Crush', value: bonuses.crushAttack },
            { label: 'Magic', value: bonuses.magicAttack },
            { label: 'Ranged', value: bonuses.rangeAttack },
          ]}
        />
        <Block
          title="Defence"
          stats={[
            { label: 'Stab', value: bonuses.stabDefence },
            { label: 'Slash', value: bonuses.slashDefence },
            { label: 'Crush', value: bonuses.crushDefence },
            { label: 'Magic', value: bonuses.magicDefence },
            { label: 'Ranged', value: bonuses.rangeDefence },
          ]}
        />
        <Block
          title="Other"
          stats={[
            { label: 'Strength', value: bonuses.strengthBonus },
            { label: 'Ranged str', value: bonuses.rangedStrengthBonus },
            { label: 'Magic dmg %', value: bonuses.magicDamageBonus },
            { label: 'Prayer', value: bonuses.prayerBonus },
          ]}
        />
      </div>
    </Card>
  )
}
