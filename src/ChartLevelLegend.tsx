import { number } from './models';
import type { ChartLevel } from './profilePrimitives';

/** Keep label backgrounds out of the profile's data area. */
export function ChartLevelLegend({ levels }: { levels: readonly ChartLevel[] }) {
  return (
    <div className="profile-level-legend" aria-label="Profile guide levels">
      {levels.map((level) => (
        <span key={`${level.label}:${level.value}`} style={{ color: level.color }}>
          {level.label} {number(level.value)}
        </span>
      ))}
    </div>
  );
}
