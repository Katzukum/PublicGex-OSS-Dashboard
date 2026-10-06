import { cloneElement, Component as ReactComponent, isValidElement, useId } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

export function Panel({
  title,
  eyebrow,
  action,
  children,
  className = '',
}: {
  title?: string;
  eyebrow?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || action) && (
        <header className="panel-head">
          <div>
            {eyebrow && <span className="eyebrow">{eyebrow}</span>}
            <h2>{title}</h2>
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}
export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'positive' | 'negative' | 'warning' | 'neutral' | 'accent';
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
export function Metric({
  label,
  value,
  detail,
  tone = '',
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: string;
}) {
  return (
    <div className={`metric ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </div>
  );
}
export function Empty({
  title = 'No data yet',
  children,
  action,
}: {
  title?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-symbol">∿</div>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function Notice({
  children,
  tone = 'info',
}: {
  children: ReactNode;
  tone?: 'info' | 'error' | 'success' | 'warning';
}) {
  return (
    <div className={`notice ${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      {children}
    </div>
  );
}
export function RequestState({
  loading,
  error,
  reload,
  hasData = false,
}: {
  loading: boolean;
  error?: string;
  reload: () => void;
  hasData?: boolean;
}) {
  return (
    <>
      {error && (
        <Notice tone="error">
          <span>
            {hasData ? 'Refresh failed. Previously loaded data is shown. ' : ''}
            {error}
          </span>
          <button className="text-button" onClick={reload}>
            Retry
          </button>
        </Notice>
      )}
      {loading && !hasData && (
        <div className="loading-state" role="status">
          <span className="spinner" /> Loading saved market data…
        </div>
      )}
    </>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  const id = useId();
  const control = isValidElement<{ id?: string; 'aria-describedby'?: string }>(children)
    ? cloneElement(children, { id, 'aria-describedby': hint ? `${id}-hint` : undefined })
    : children;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {control}
      {hint && <small id={`${id}-hint`}>{hint}</small>}
    </div>
  );
}
export function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
}) {
  return (
    <label className="toggle">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}
export class ErrorBoundary extends ReactComponent<{ children: ReactNode }, { error?: string }> {
  state: { error?: string } = {};
  static getDerivedStateFromError(error: Error) {
    return { error: error.message };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('View error', error, info.componentStack);
  }
  render() {
    return this.state.error ? (
      <Panel title="This view could not render">
        <Notice tone="error">{this.state.error}</Notice>
        <button onClick={() => this.setState({ error: undefined })}>Try again</button>
      </Panel>
    ) : (
      this.props.children
    );
  }
}
