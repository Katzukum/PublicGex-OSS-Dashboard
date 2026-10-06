import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { ApiMap } from './types';

export function useRemote<K extends keyof ApiMap>(
  method: K,
  args: ApiMap[K]['args'],
  options: { enabled?: boolean; interval?: number; revision?: number } = {},
) {
  const key = JSON.stringify(args),
    enabled = options.enabled ?? true,
    interval = options.interval ?? 0,
    revision = options.revision ?? 0;
  const [state, setState] = useState<{
    key: string;
    data?: ApiMap[K]['result'];
    error?: string;
    loading: boolean;
    updatedAt?: number;
  }>({ key, loading: enabled });
  const [refresh, setRefresh] = useState(0);
  const reload = useCallback(() => setRefresh((n) => n + 1), []);
  useEffect(() => {
    if (!enabled) {
      setState((previous) =>
        previous.key === key ? { ...previous, loading: false } : { key, loading: false },
      );
      return;
    }
    let active = true,
      busy = false;
    const controller = new AbortController();
    const load = async () => {
      if (busy) return;
      busy = true;
      setState((previous) => ({
        ...(previous.key === key ? previous : { key }),
        loading: true,
        error: undefined,
      }));
      try {
        const data = await api(method, JSON.parse(key) as ApiMap[K]['args'], controller.signal);
        if (active) setState({ key, data, loading: false, updatedAt: Date.now() });
      } catch (error) {
        if (active)
          setState((previous) => ({
            ...previous,
            key,
            loading: false,
            error: error instanceof Error ? error.message : String(error),
          }));
      } finally {
        busy = false;
      }
    };
    void load();
    const timer = interval
      ? setInterval(() => {
          if (!document.hidden) void load();
        }, interval)
      : undefined;
    return () => {
      active = false;
      controller.abort();
      if (timer) clearInterval(timer);
    };
  }, [method, key, enabled, interval, revision, refresh]);
  const current = state.key === key ? state : { key, loading: enabled };
  return {
    data: current.data,
    error: current.error,
    loading: current.loading,
    updatedAt: current.updatedAt,
    reload,
  };
}

export function useAction() {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<string>(),
    [message, setMessage] = useState<string>();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const run = async <T>(work: () => Promise<T>, success?: string): Promise<T | undefined> => {
    if (busy) return;
    setBusy(true);
    setError(undefined);
    setMessage(undefined);
    try {
      const result = await work();
      if (alive.current) setMessage(success);
      return result;
    } catch (err) {
      if (alive.current) setError(err instanceof Error ? err.message : String(err));
      return undefined;
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  return { busy, error, message, run };
}
