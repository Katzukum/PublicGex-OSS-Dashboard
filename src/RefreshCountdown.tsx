import { useEffect, useState } from 'react';

export function RefreshCountdown({
  interval,
  updatedAt,
  paused,
}: {
  interval: number;
  updatedAt?: number;
  paused: boolean;
}) {
  const seconds = Math.max(5, Math.round(interval / 1000));
  const [remaining, setRemaining] = useState(seconds);
  useEffect(() => {
    setRemaining(seconds);
    if (paused) return;
    const timer = setInterval(() => {
      if (!document.hidden) setRemaining((value) => (value <= 1 ? seconds : value - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [seconds, updatedAt, paused]);
  return (
    <span className="refresh-countdown" data-testid="refresh-countdown">
      Auto refresh <b>{paused ? 'paused' : `${remaining}s`}</b>
    </span>
  );
}
